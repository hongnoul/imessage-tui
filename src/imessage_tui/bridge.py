"""Daemon bridge — thin client over iphonebridge.

Mirrors the web app's bridge.py: shells out to busctl for D-Bus calls,
tails events.jsonl for live push, reads contacts.sqlite for name resolution,
plus a local SQLite for *hide* state (since the iPhone won't honor MAP deletes —
see imessage-web design notes).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from dataclasses import dataclass, field
from typing import AsyncIterator

# ── Paths ─────────────────────────────────────────────────────────────
DAEMON_STATE = pathlib.Path.home() / ".local" / "state" / "iphonebridge"
EVENTS_LOG   = DAEMON_STATE / "events.jsonl"
CONTACTS_DB  = DAEMON_STATE / "contacts.sqlite"

APP_STATE = pathlib.Path.home() / ".local" / "state" / "imessage-tui"
HIDES_DB  = APP_STATE / "hides.sqlite"

# ── D-Bus ─────────────────────────────────────────────────────────────
BUS_SERVICE   = "com.gabriel.iphonebridge"
BUS_PATH      = "/com/gabriel/iphonebridge"
BUS_IFACE_MSG = "com.gabriel.iphonebridge.Messages1"


# ── Data model ────────────────────────────────────────────────────────
@dataclass
class Message:
    handle: str
    phone: str              # raw +1...
    phone_norm: str         # digits only, country code preserved
    body: str
    timestamp: str          # ISO with tz
    is_read: bool
    direction: str          # "in" | "out"
    kind: str               # "sms" | "imessage" (heuristic)
    contact_name: str | None = None

    @classmethod
    def from_event(cls, evt: dict) -> "Message | None":
        ek = evt.get("kind", "")
        if ek not in ("sms_received", "sms_sent"):
            return None
        direction = "out" if ek == "sms_sent" else "in"
        raw_type = (evt.get("raw_type") or "").upper()
        is_sms = raw_type in ("SMS_GSM", "SMS_CDMA")
        return cls(
            handle=evt.get("handle", "") or "",
            phone=evt.get("sender_phone") or evt.get("recipient_phone") or "",
            phone_norm=evt.get("sender_phone_norm") or evt.get("recipient_phone_norm") or "",
            body=evt.get("body") or "",
            timestamp=evt.get("timestamp") or evt.get("seen_at") or "",
            is_read=bool(evt.get("is_read", False)),
            direction=direction,
            kind="sms" if is_sms else "imessage",
            contact_name=evt.get("contact_name"),
        )


@dataclass
class Conversation:
    peer_key: str           # phone_norm or unique handle
    display_name: str
    last_body: str
    last_ts: str
    last_direction: str
    unread: int = 0
    messages: list[Message] = field(default_factory=list)


# ── Hides DB (local-only soft delete) ─────────────────────────────────
def _open_hides() -> sqlite3.Connection:
    APP_STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(HIDES_DB)
    db.execute("CREATE TABLE IF NOT EXISTS hidden_messages (handle TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS hidden_peers   (peer_key TEXT PRIMARY KEY)")
    db.commit()
    return db


def hidden_handles() -> set[str]:
    with _open_hides() as db:
        return {r[0] for r in db.execute("SELECT handle FROM hidden_messages")}


def hidden_peers() -> set[str]:
    with _open_hides() as db:
        return {r[0] for r in db.execute("SELECT peer_key FROM hidden_peers")}


def hide_message(handle: str) -> None:
    with _open_hides() as db:
        db.execute("INSERT OR IGNORE INTO hidden_messages(handle) VALUES (?)", (handle,))
        db.commit()


def unhide_message(handle: str) -> None:
    with _open_hides() as db:
        db.execute("DELETE FROM hidden_messages WHERE handle = ?", (handle,))
        db.commit()


def hide_peer(peer_key: str) -> None:
    with _open_hides() as db:
        db.execute("INSERT OR IGNORE INTO hidden_peers(peer_key) VALUES (?)", (peer_key,))
        db.commit()


def unhide_peer(peer_key: str) -> None:
    with _open_hides() as db:
        db.execute("DELETE FROM hidden_peers WHERE peer_key = ?", (peer_key,))
        db.commit()


# ── Contact resolution ────────────────────────────────────────────────
_CONTACT_CACHE: dict[str, str | None] = {}


def _norm_variants(phone: str) -> list[str]:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return []
    out = [digits]
    if digits.startswith("1") and len(digits) == 11:
        out.append(digits[1:])
    elif len(digits) == 10:
        out.append("1" + digits)
    return out


def resolve_contact(phone_norm: str) -> str | None:
    if not phone_norm:
        return None
    if phone_norm in _CONTACT_CACHE:
        return _CONTACT_CACHE[phone_norm]
    if not CONTACTS_DB.exists():
        _CONTACT_CACHE[phone_norm] = None
        return None
    variants = _norm_variants(phone_norm)
    if not variants:
        _CONTACT_CACHE[phone_norm] = None
        return None
    try:
        with sqlite3.connect(f"file:{CONTACTS_DB}?mode=ro", uri=True) as c:
            placeholders = ",".join("?" * len(variants))
            row = c.execute(
                f"SELECT contacts.full_name FROM phones JOIN contacts "
                f"ON phones.contact_id = contacts.id "
                f"WHERE phones.phone_norm IN ({placeholders}) LIMIT 1",
                variants,
            ).fetchone()
            name = row[0] if row else None
    except sqlite3.DatabaseError:
        name = None
    _CONTACT_CACHE[phone_norm] = name
    return name


def invalidate_contact_cache() -> None:
    _CONTACT_CACHE.clear()


# ── Event log readers ─────────────────────────────────────────────────
def read_history() -> list[Message]:
    """Read the full event log (newest events appear at the end of file).
    Returns messages in chronological order."""
    if not EVENTS_LOG.exists():
        return []
    hidden = hidden_handles()
    out: list[Message] = []
    with EVENTS_LOG.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = Message.from_event(evt)
            if m and m.handle not in hidden:
                if not m.contact_name:
                    m.contact_name = resolve_contact(m.phone_norm)
                out.append(m)
    out.sort(key=lambda m: m.timestamp)
    return out


def group_conversations(messages: list[Message]) -> list[Conversation]:
    """Group messages by peer (normalized phone, else raw, else display name).
    Drops peers in the hidden set."""
    hidden_peer = hidden_peers()
    by_peer: dict[str, Conversation] = {}
    for m in messages:
        key = m.phone_norm or m.phone or m.contact_name or "unknown"
        if key in hidden_peer:
            continue
        name = m.contact_name or resolve_contact(m.phone_norm) or m.phone or key
        conv = by_peer.setdefault(key, Conversation(
            peer_key=key, display_name=name,
            last_body="", last_ts="", last_direction=m.direction,
        ))
        conv.messages.append(m)
        if m.timestamp >= conv.last_ts:
            conv.last_ts = m.timestamp
            conv.last_body = m.body
            conv.last_direction = m.direction
        if m.direction == "in" and not m.is_read:
            conv.unread += 1
    # newest-first
    return sorted(by_peer.values(), key=lambda c: c.last_ts, reverse=True)


# ── Live tail ─────────────────────────────────────────────────────────
async def tail_events() -> AsyncIterator[Message]:
    while not EVENTS_LOG.exists():
        await asyncio.sleep(1)
    hidden = hidden_handles()
    with EVENTS_LOG.open("r") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.5)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = Message.from_event(evt)
            if m and m.handle not in hidden:
                if not m.contact_name:
                    m.contact_name = resolve_contact(m.phone_norm)
                yield m


# ── D-Bus calls ───────────────────────────────────────────────────────
async def _busctl(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "busctl", "--user", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip() or "busctl failed")
    return out.decode()


async def is_healthy() -> bool:
    try:
        out = await _busctl("call", BUS_SERVICE, BUS_PATH, BUS_IFACE_MSG, "IsHealthy")
        return "true" in out.lower()
    except RuntimeError:
        return False


async def send_message(recipient: str, body: str) -> tuple[bool, str]:
    try:
        out = await _busctl(
            "call", BUS_SERVICE, BUS_PATH, BUS_IFACE_MSG, "Send",
            "ss", recipient, body,
        )
        return True, out.strip()
    except RuntimeError as e:
        return False, str(e)


async def resync_contacts() -> tuple[bool, str]:
    """Trigger a fresh PBAP pull via the iphonebridge CLI."""
    proc = await asyncio.create_subprocess_exec(
        "iphonebridge", "contacts-sync",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    invalidate_contact_cache()
    ok = proc.returncode == 0
    return ok, (out.decode() + err.decode())[-2000:]
