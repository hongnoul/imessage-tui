"""TUI app — Textual.

Layout:
  ConversationsPanel   |   ThreadPanel
                       |   Composer
  Footer: keybinds + status indicator
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Footer, Header, Input, Label, ListItem, ListView, Static, TextArea
)

from . import bridge, outbox


# ── Helpers ───────────────────────────────────────────────────────────
def fmt_relative(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc).astimezone(dt.tzinfo)
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 60:    return "just now"
    if secs < 3600:  return f"{int(secs // 60)}m"
    if secs < 86400: return dt.strftime("%H:%M")
    if secs < 7 * 86400: return dt.strftime("%a")
    return dt.strftime("%m/%d")


def fmt_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%a %b %d, %H:%M")
    except ValueError:
        return iso[:16]


def initials(name: str) -> str:
    if not name:
        return "?"
    parts = name.strip().split()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


# ── Conversation list ─────────────────────────────────────────────────
class ConvItem(ListItem):
    def __init__(self, conv: bridge.Conversation) -> None:
        super().__init__()
        self.conv = conv

    def compose(self) -> ComposeResult:
        c = self.conv
        unread_mark = "● " if c.unread else "  "
        time_str = fmt_relative(c.last_ts)
        name_line = f"{unread_mark}[b]{c.display_name}[/]"
        preview = c.last_body[:60].replace("\n", " ") if c.last_body else "(no preview)"
        if c.last_direction == "out":
            preview = f"You: {preview}"
        yield Static(f"{name_line}\n  [dim]{preview}[/]\n  [dim italic]{time_str}[/]")


# ── Message bubble ────────────────────────────────────────────────────
class Bubble(Static):
    def __init__(self, msg: bridge.Message) -> None:
        super().__init__()
        self.msg = msg
        self.add_class("bubble")
        self.add_class(f"dir-{msg.direction}")
        if msg.kind == "sms":
            self.add_class("sms")

    def render(self) -> str:
        body = self.msg.body or "(empty)"
        meta = fmt_time(self.msg.timestamp)
        if self.msg.direction == "out":
            return f"[b]{body}[/]\n[dim italic right]{meta}[/]"
        return f"{body}\n[dim italic]{meta}[/]"


# ── Pending bubble (outbox row not yet sent) ──────────────────────────
class PendingBubble(Static):
    def __init__(self, row: outbox.Outgoing) -> None:
        super().__init__()
        self.row = row
        self.add_class("bubble")
        self.add_class("dir-out")
        self.add_class(f"pending-{row.status}")

    def render(self) -> str:
        icon = {
            "queued":  "⋯",
            "sending": "↻",
            "failed":  "⚠",
        }.get(self.row.status, "•")
        meta = self.row.status
        if self.row.status == "queued" and self.row.attempts:
            meta = f"retry #{self.row.attempts}"
        elif self.row.status == "failed":
            err = (self.row.last_error or "")[:60]
            meta = f"failed — {err}"
        return f"[dim]{icon}[/] {self.row.body}\n[dim italic]{meta}[/]"


# ── Compose modal ─────────────────────────────────────────────────────
class ComposeModal(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "send", "Send"),
    ]

    def __init__(self, recipient_default: str = "") -> None:
        super().__init__()
        self.recipient_default = recipient_default

    def compose(self) -> ComposeResult:
        with Vertical(id="compose-modal"):
            yield Label("[b]New Message[/]  ([dim]Ctrl+S send · Esc cancel[/])")
            yield Input(value=self.recipient_default, placeholder="to: phone or contact name", id="compose-to")
            yield TextArea("", id="compose-body")
            yield Label("", id="compose-status")

    def on_mount(self) -> None:
        target = "compose-body" if self.recipient_default else "compose-to"
        self.query_one(f"#{target}").focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_send(self) -> None:
        to = self.query_one("#compose-to", Input).value.strip()
        body = self.query_one("#compose-body", TextArea).text.strip()
        if not to or not body:
            self.query_one("#compose-status", Label).update("[red]need recipient + body[/]")
            return
        self.dismiss((to, body))


# ── Confirm modal ─────────────────────────────────────────────────────
class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel",  "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-modal"):
            yield Label(self.prompt, id="confirm-prompt")
            yield Label("[dim]y / n[/]")

    def action_confirm(self) -> None: self.dismiss(True)
    def action_cancel(self) -> None:  self.dismiss(False)


# ── Main app ──────────────────────────────────────────────────────────
class IMessageTUI(App):
    CSS_PATH = "app.tcss"
    TITLE = "imessage-tui"
    SUB_TITLE = "iPhone messages, in a terminal"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("c", "compose", "Compose"),
        Binding("r", "reply",   "Reply"),
        Binding("/", "search",  "Search"),
        Binding("d", "hide_msg",  "Hide msg"),
        Binding("D", "hide_peer", "Hide thread"),
        Binding("g", "refresh_contacts", "Resync contacts"),
        Binding("ctrl+r", "refresh", "Refresh history"),
        Binding("escape", "clear_search", "Clear search"),
    ]

    daemon_healthy: reactive[bool] = reactive(False)
    search_query:   reactive[str]  = reactive("")
    active_peer:    reactive[str]  = reactive("")
    outbox_pending: reactive[int]  = reactive(0)
    outbox_failed:  reactive[int]  = reactive(0)

    def __init__(self) -> None:
        super().__init__()
        self._messages: list[bridge.Message] = []
        self._conversations: list[bridge.Conversation] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="root"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="search…", id="search", classes="hidden")
                yield ListView(id="conv-list")
            with Vertical(id="thread-pane"):
                yield Label("", id="thread-header")
                yield Vertical(id="thread-body")
                yield Input(placeholder="↩ reply (c to compose new)", id="reply-input", disabled=True)
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────
    async def on_mount(self) -> None:
        await self._refresh_history()
        self._poll_health()
        self.set_interval(15, self._poll_health)
        self.set_interval(3, self._refresh_outbox_status)
        self.tail_events()
        self.outbox_worker()

    @work(thread=False)
    async def _poll_health(self) -> None:
        self.daemon_healthy = await bridge.is_healthy()
        self._render_status_bar()

    def _refresh_outbox_status(self) -> None:
        counts = outbox.counts()
        self.outbox_pending = counts.get("queued", 0) + counts.get("sending", 0)
        self.outbox_failed = counts.get("failed", 0)
        self._render_status_bar()
        # Also re-render the active thread so pending bubbles update.
        if self.active_peer:
            self._render_thread()

    def _render_status_bar(self) -> None:
        """Render connection + outbox state into the header subtitle.

        Single source of truth. Was duplicated in a status-bar widget above
        the reply input; that widget is gone — header carries it all."""
        parts = []
        parts.append("● connected" if self.daemon_healthy else "○ disconnected")
        if self.outbox_pending:
            parts.append(f"⋯ {self.outbox_pending} pending")
        if self.outbox_failed:
            parts.append(f"⚠ {self.outbox_failed} failed")
        self.sub_title = " · ".join(parts)

    @work(thread=False)
    async def outbox_worker(self) -> None:
        """Drain the outbox: pick the next ready row, try to send, retry on
        failure with backoff. Runs forever; sleeps when idle."""
        while True:
            if not self.daemon_healthy:
                # Don't bother trying when we know the daemon is down.
                await asyncio.sleep(2)
                continue
            row = await asyncio.to_thread(outbox.claim_one_ready)
            if row is None:
                await asyncio.sleep(1)
                continue
            ok, detail = await bridge.send_message(row.recipient, row.body)
            if ok:
                await asyncio.to_thread(outbox.mark_sent, row.id, detail)
            else:
                await asyncio.to_thread(outbox.mark_failure, row.id, detail[:300])
                if row.attempts + 1 >= outbox.MAX_ATTEMPTS:
                    self.notify(
                        f"giving up after {outbox.MAX_ATTEMPTS} tries: {detail[:120]}",
                        title="send failed",
                        severity="error",
                        timeout=8,
                    )
            self._refresh_outbox_status()

    @work(thread=False)
    async def tail_events(self) -> None:
        async for msg in bridge.tail_events():
            self._messages.append(msg)
            self._rebuild_conversations()
            # Re-render the active thread on any matching event — including
            # outgoing ones we just sent ourselves (so the bubble appears
            # without needing ctrl+r).
            peer_match = (
                msg.phone_norm == self.active_peer
                or msg.phone == self.active_peer
            )
            if peer_match:
                self._render_thread()
            # Only notify on incoming. Outgoing was the user's own action.
            if msg.direction == "in":
                self.notify(
                    f"{msg.contact_name or msg.phone}: {msg.body[:80]}",
                    title="↘ new message",
                    timeout=3,
                )

    async def _refresh_history(self) -> None:
        # Run in a thread to avoid blocking on a large events.jsonl.
        self._messages = await asyncio.to_thread(bridge.read_history)
        self._rebuild_conversations()

    def _rebuild_conversations(self) -> None:
        self._conversations = bridge.group_conversations(self._messages)
        self._render_conv_list()

    def _render_conv_list(self) -> None:
        lv = self.query_one("#conv-list", ListView)
        lv.clear()
        q = self.search_query.lower()
        for c in self._conversations:
            if q and q not in c.display_name.lower() and q not in c.last_body.lower():
                continue
            lv.append(ConvItem(c))

    def _render_thread(self) -> None:
        header = self.query_one("#thread-header", Label)
        body = self.query_one("#thread-body")
        reply = self.query_one("#reply-input", Input)
        body.remove_children()

        if not self.active_peer:
            header.update("[dim]Select a conversation (j/k or click)[/]")
            reply.disabled = True
            reply.placeholder = "↩ reply (c to compose new)"
            return

        conv = next((c for c in self._conversations if c.peer_key == self.active_peer), None)
        pending = outbox.pending_for_peer(self.active_peer)
        if not conv and not pending:
            header.update("[red]conversation gone[/]")
            return
        name = conv.display_name if conv else self.active_peer
        msg_count = len(conv.messages) if conv else 0
        header.update(
            f"[b]{name}[/]   "
            f"[dim]{self.active_peer}  ·  {msg_count} msgs"
            + (f"  ·  {len(pending)} pending" if pending else "")
            + "[/]"
        )
        # Always allow typing — outbox absorbs disconnects.
        reply.disabled = False
        if not self.daemon_healthy:
            reply.placeholder = f"↩ reply to {name} (offline — will queue)"
        else:
            reply.placeholder = f"↩ reply to {name}"

        # Render real bubbles oldest-first.
        last_day = None
        if conv:
            for m in conv.messages:
                day = m.timestamp[:10]
                if day != last_day:
                    body.mount(Static(f"\n[dim]── {day} ──[/]\n", classes="day-sep"))
                    last_day = day
                body.mount(Bubble(m))
        # Pending bubbles at the end.
        for row in pending:
            body.mount(PendingBubble(row))
        body.scroll_end(animate=False)

    # ── Events ────────────────────────────────────────────────────────
    @on(ListView.Selected, "#conv-list")
    def on_conv_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, ConvItem):
            self.active_peer = item.conv.peer_key
            self._render_thread()

    @on(ListView.Highlighted, "#conv-list")
    def on_conv_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, ConvItem):
            self.active_peer = item.conv.peer_key
            self._render_thread()

    @on(Input.Submitted, "#reply-input")
    async def on_reply_submitted(self, event: Input.Submitted) -> None:
        body = event.value.strip()
        if not body or not self.active_peer:
            return
        recipient = self._send_recipient_for_active()
        event.input.value = ""
        # Enqueue — worker drains. The thread re-renders on the next tick.
        await asyncio.to_thread(outbox.enqueue, recipient, body, self.active_peer)
        self._refresh_outbox_status()

    @on(Input.Changed, "#search")
    def on_search_changed(self, event: Input.Changed) -> None:
        self.search_query = event.value
        self._render_conv_list()

    # ── Actions ───────────────────────────────────────────────────────
    def _send_recipient_for_active(self) -> str:
        conv = next((c for c in self._conversations if c.peer_key == self.active_peer), None)
        if not conv:
            return self.active_peer
        # Prefer a phone number for sending (the daemon also accepts names).
        for m in reversed(conv.messages):
            if m.phone:
                return m.phone
        return conv.display_name

    async def _do_send(self, recipient: str, body: str) -> None:
        """Used by the compose modal. Routes through the outbox like the
        inline reply does, so disconnects don't lose the message."""
        await asyncio.to_thread(outbox.enqueue, recipient, body, "")
        self._refresh_outbox_status()

    @work(thread=False)
    async def action_compose(self) -> None:
        result = await self.push_screen_wait(ComposeModal())
        if result is None:
            return
        to, body = result
        await self._do_send(to, body)

    async def action_reply(self) -> None:
        self.query_one("#reply-input", Input).focus()

    def action_search(self) -> None:
        s = self.query_one("#search", Input)
        s.remove_class("hidden")
        s.focus()

    def action_clear_search(self) -> None:
        s = self.query_one("#search", Input)
        s.value = ""
        s.add_class("hidden")
        self.search_query = ""
        self._render_conv_list()
        self.query_one("#conv-list", ListView).focus()

    async def action_refresh(self) -> None:
        bridge.invalidate_contact_cache()
        await self._refresh_history()
        self.notify("history refreshed")

    async def action_refresh_contacts(self) -> None:
        self.notify("resyncing contacts via PBAP…")
        ok, detail = await bridge.resync_contacts()
        if ok:
            bridge.invalidate_contact_cache()
            await self._refresh_history()
            self.notify("contacts resynced")
        else:
            self.notify(detail[:300], title="resync failed", severity="error", timeout=8)

    @work(thread=False)
    async def action_hide_msg(self) -> None:
        # Hide the latest bubble in the active thread (handle-based).
        if not self.active_peer:
            return
        conv = next((c for c in self._conversations if c.peer_key == self.active_peer), None)
        if not conv or not conv.messages:
            return
        latest = conv.messages[-1]
        if not latest.handle:
            self.notify("no handle on that message", severity="warning")
            return
        confirm = await self.push_screen_wait(
            ConfirmModal(f"Hide latest message from this view?\n[dim](still on iPhone)[/]\n\n  {latest.body[:80]}")
        )
        if not confirm:
            return
        bridge.hide_message(latest.handle)
        await self._refresh_history()
        self.notify("hidden (local-only)")

    @work(thread=False)
    async def action_hide_peer(self) -> None:
        if not self.active_peer:
            return
        conv = next((c for c in self._conversations if c.peer_key == self.active_peer), None)
        if not conv:
            return
        confirm = await self.push_screen_wait(
            ConfirmModal(
                f"Hide [b]entire conversation[/] with {conv.display_name}?\n"
                f"[dim]Local-only — messages still on iPhone.[/]"
            )
        )
        if not confirm:
            return
        bridge.hide_peer(conv.peer_key)
        self.active_peer = ""
        await self._refresh_history()
        self._render_thread()
        self.notify("conversation hidden (local-only)")

    def action_help(self) -> None:
        self.notify(
            "j/k or ↑↓: navigate · enter/click: open · c: compose · r: reply · "
            "/: search · d: hide msg · D: hide thread · g: resync contacts · "
            "ctrl+r: refresh · q: quit",
            title="keybinds",
            timeout=10,
        )


def main() -> int:
    try:
        IMessageTUI().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
