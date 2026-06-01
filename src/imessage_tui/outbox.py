"""Outgoing-message outbox.

A small SQLite-backed queue that decouples the user's "press enter" from the
daemon actually sending. Lets us:
  - keep the UI snappy (insert + render immediately, never block on D-Bus)
  - survive transient daemon/bluetooth outages (retry with backoff)
  - show pending bubbles in the thread until the daemon confirms send
  - mark permanent failures so the user can retry/discard

Schema (single table, intentionally small):
  outgoing(
    id          INTEGER PRIMARY KEY,
    recipient   TEXT,
    body        TEXT,
    peer_key    TEXT,    -- so we can render in the right thread w/o re-resolving
    queued_at   REAL,    -- unix ts when the user pressed enter
    status      TEXT,    -- queued | sending | sent | failed
    attempts    INTEGER,
    next_try    REAL,    -- earliest unix ts to retry (backoff)
    last_error  TEXT,
    sent_handle TEXT     -- daemon's PushMessage transfer return on success
  )

Statuses transition: queued → sending → (sent | failed-with-retry-pending | failed)
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
import time
from dataclasses import dataclass

# Reuse the app state dir from bridge.py via a small import-time path.
APP_STATE = pathlib.Path.home() / ".local" / "state" / "imessage-tui"
OUTBOX_DB = APP_STATE / "outbox.sqlite"

MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SEC = 5.0
BACKOFF_FACTOR = 2.0
SENT_RETENTION_SEC = 24 * 3600  # purge 'sent' rows older than 24h


@dataclass
class Outgoing:
    id: int
    recipient: str
    body: str
    peer_key: str
    queued_at: float
    status: str
    attempts: int
    next_try: float
    last_error: str | None
    sent_handle: str | None


def _connect() -> sqlite3.Connection:
    APP_STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(OUTBOX_DB)
    db.execute("""
        CREATE TABLE IF NOT EXISTS outgoing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,
            body TEXT NOT NULL,
            peer_key TEXT NOT NULL DEFAULT '',
            queued_at REAL NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_try REAL NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_handle TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_outgoing_status ON outgoing(status, next_try)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_outgoing_peer ON outgoing(peer_key)")
    db.commit()
    return db


def enqueue(recipient: str, body: str, peer_key: str = "") -> int:
    """Insert a new queued message. Returns the row id."""
    with contextlib.closing(_connect()) as db:
        now = time.time()
        cur = db.execute(
            "INSERT INTO outgoing(recipient, body, peer_key, queued_at, status, next_try) "
            "VALUES (?, ?, ?, ?, 'queued', ?)",
            (recipient, body, peer_key, now, now),
        )
        db.commit()
        return cur.lastrowid or 0


def pending_for_peer(peer_key: str) -> list[Outgoing]:
    """Return all not-yet-sent messages addressed to a given peer.
    Used to render pending bubbles in the thread."""
    with contextlib.closing(_connect()) as db:
        rows = db.execute(
            "SELECT id, recipient, body, peer_key, queued_at, status, attempts, "
            "       next_try, last_error, sent_handle "
            "FROM outgoing WHERE peer_key = ? AND status != 'sent' "
            "ORDER BY queued_at ASC",
            (peer_key,),
        ).fetchall()
        return [Outgoing(*r) for r in rows]


def claim_one_ready(now: float | None = None) -> Outgoing | None:
    """Atomically pick the next message ready to send and mark it 'sending'.
    Returns None if nothing is ready. Workers race-safe via UPDATE WHERE id."""
    now = now or time.time()
    with contextlib.closing(_connect()) as db:
        row = db.execute(
            "SELECT id FROM outgoing "
            "WHERE status = 'queued' AND next_try <= ? "
            "ORDER BY queued_at ASC LIMIT 1",
            (now,),
        ).fetchone()
        if not row:
            return None
        rid = row[0]
        # Mark 'sending'. If another worker grabbed it first, this affects 0 rows.
        cur = db.execute(
            "UPDATE outgoing SET status='sending' WHERE id = ? AND status = 'queued'",
            (rid,),
        )
        if cur.rowcount == 0:
            db.commit()
            return None
        full = db.execute(
            "SELECT id, recipient, body, peer_key, queued_at, status, attempts, "
            "       next_try, last_error, sent_handle FROM outgoing WHERE id = ?",
            (rid,),
        ).fetchone()
        db.commit()
        return Outgoing(*full) if full else None


def mark_sent(rid: int, handle: str) -> None:
    with contextlib.closing(_connect()) as db:
        db.execute(
            "UPDATE outgoing SET status='sent', sent_handle=?, last_error=NULL WHERE id=?",
            (handle, rid),
        )
        db.commit()


def mark_failure(rid: int, error: str) -> None:
    """Bump attempts. If under MAX_ATTEMPTS, go back to 'queued' with backoff.
    Otherwise mark permanently 'failed'."""
    with contextlib.closing(_connect()) as db:
        row = db.execute("SELECT attempts FROM outgoing WHERE id=?", (rid,)).fetchone()
        if not row:
            return
        attempts = (row[0] or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            db.execute(
                "UPDATE outgoing SET status='failed', attempts=?, last_error=? WHERE id=?",
                (attempts, error, rid),
            )
        else:
            backoff = INITIAL_BACKOFF_SEC * (BACKOFF_FACTOR ** (attempts - 1))
            db.execute(
                "UPDATE outgoing SET status='queued', attempts=?, next_try=?, last_error=? "
                "WHERE id=?",
                (attempts, time.time() + backoff, error, rid),
            )
        db.commit()


def discard(rid: int) -> None:
    """Remove a queued/failed row — user gave up on it."""
    with contextlib.closing(_connect()) as db:
        db.execute("DELETE FROM outgoing WHERE id=?", (rid,))
        db.commit()


def retry_failed(rid: int) -> None:
    """Reset a 'failed' row back to 'queued' so the worker tries it again."""
    with contextlib.closing(_connect()) as db:
        db.execute(
            "UPDATE outgoing SET status='queued', attempts=0, next_try=?, last_error=NULL "
            "WHERE id=? AND status='failed'",
            (time.time(), rid),
        )
        db.commit()


def purge_old_sent() -> int:
    """Drop sent rows older than SENT_RETENTION_SEC. Returns rows deleted."""
    with contextlib.closing(_connect()) as db:
        cutoff = time.time() - SENT_RETENTION_SEC
        cur = db.execute(
            "DELETE FROM outgoing WHERE status='sent' AND queued_at < ?",
            (cutoff,),
        )
        db.commit()
        return cur.rowcount


def counts() -> dict[str, int]:
    """Status histogram — used by the footer indicator."""
    with contextlib.closing(_connect()) as db:
        rows = db.execute(
            "SELECT status, COUNT(*) FROM outgoing GROUP BY status"
        ).fetchall()
        return {status: n for status, n in rows}
