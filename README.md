# imessage-tui

A terminal UI for reading and sending iMessage/SMS from Linux. Pairs with the [iphonebridge](https://github.com/gabrielmeir53/iphonebridge) daemon, which relays your iPhone over standard Bluetooth — no Mac, no Apple ID login, no subscription, no jailbreak.

```
┌──────────────────────────────────────────────────────────────┐
│ imessage-tui                              iPhone messages    │
├──────────────────┬──────────────────────────────────────────┤
│ ● Mom            │  Mom                     +1415…  21 msgs  │
│   You: on my way ├──────────────────────────────────────────┤
│   2m             │  ── 2026-06-01 ──                         │
│                  │                                           │
│   +184****1959   │  Did u pay on ur card already             │
│   12:34          │  Mon Jun 01, 19:30                        │
│                  │                                           │
│   Partiful       │                            ┌────────────┐ │
│   124348 is...   │                            │ on my way  │ │
│   9:07           │                            │ Jun 01     │ │
│                  │                            └────────────┘ │
│                  │  ↩ reply to Mom                           │
├──────────────────┴──────────────────────────────────────────┤
│  q quit · c compose · r reply · / search · d hide · ? help   │
└──────────────────────────────────────────────────────────────┘
```

## Install

Pick whichever style suits you — they all produce a working `imessage-tui` on your PATH.

### one-line install (`curl | bash`)

```bash
curl -fsSL https://raw.githubusercontent.com/hongnoul/imessage-tui/main/install.sh | bash
```

The script auto-detects whether you have `pipx` (preferred — clean global install) and falls back to a user-local venv at `~/.local/share/imessage-tui` with a symlink in `~/.local/bin`. Adds zero permanent dependencies; you can uninstall by deleting that directory.

### pipx (most idiomatic for Python CLIs)

```bash
pipx install git+https://github.com/hongnoul/imessage-tui.git
```

Same model as `npm install -g` — pipx puts the CLI in its own venv but exposes the binary globally. Install `pipx` first:
- Arch: `sudo pacman -S python-pipx`
- Debian/Ubuntu: `sudo apt install pipx`
- Fedora: `sudo dnf install pipx`
- macOS: `brew install pipx`

### manual clone (for hacking on it)

```bash
git clone https://github.com/hongnoul/imessage-tui.git
cd imessage-tui
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/imessage-tui
```

### Prerequisite: iphonebridge daemon

All three methods require the [iphonebridge](https://github.com/gabrielmeir53/iphonebridge) daemon running on the same machine. Quick check:

```bash
systemctl --user status iphonebridge        # should say active (running)
iphonebridge sms-list | head                # should show your recent texts
```

If those don't work, follow iphonebridge's README first — pair iPhone over Bluetooth, enable "Show Message Notifications" in iPhone Bluetooth settings for the laptop, start the daemon.

## Keybinds

| key                 | action                                |
|---------------------|---------------------------------------|
| `↑` `↓` `j` `k`     | navigate conversations                |
| `enter` / click     | open conversation                     |
| `c`                 | compose new message (Ctrl+S to send)  |
| `r`                 | focus reply input                     |
| `/`                 | search conversations                  |
| `esc`               | clear search / close modal            |
| `d`                 | hide latest message (local-only)      |
| `D`                 | hide entire conversation (local-only) |
| `g`                 | resync contacts (PBAP pull)           |
| `ctrl+r`            | reload history from disk              |
| `?`                 | help                                  |
| `q`                 | quit                                  |

## Hide vs delete

iOS does not honor the Bluetooth MAP `SetMessageStatus("Deleted")` operation in 2026 — verified empirically, see [Architecture notes](#architecture). So `d` and `D` only **hide** messages from this app (state lives in `~/.local/state/imessage-tui/hides.sqlite`). Your iPhone keeps everything.

If you ever want unhide: delete that SQLite file and the messages reappear.

## Run as a systemd user service

If you want the TUI launchable from any terminal without re-cloning, the `pipx` install above already does that. If you want the *daemon* (iphonebridge) running on boot, that's covered in iphonebridge's own README — this project doesn't need its own service since the TUI is foreground-by-design.

## Architecture

```
              ┌─ events.jsonl ──── live tail ────────┐
iphonebridge ─┤                                       ├──> Textual app
              ├─ contacts.sqlite ─ name resolution ───┤
              └─ D-Bus (busctl) ── Send / IsHealthy ──┘

                                  + hides.sqlite (this app's local soft-delete)
```

- `src/imessage_tui/bridge.py` — daemon client. Reads the JSONL event log, resolves contacts, sends messages via `busctl --user`.
- `src/imessage_tui/app.py` — Textual app. Conversation list, thread view, compose/confirm modals, live tail in a worker.
- `src/imessage_tui/app.tcss` — styling.

No Node, no Electron, no daemon of its own, no web server. ~600 LOC.

### Why "hide" and not "delete"

The underlying Bluetooth MAP spec supports per-message deletion via `org.bluez.obex.Message1.SetMessageStatus("Deleted", "yes")`. We tested this against iOS 26.5 — the iPhone:

1. Returns `UnknownObject` on direct message-handle introspection (the daemon's MAP session doesn't materialize individual messages as D-Bus objects)
2. `ListMessages` returns empty against the INBOX folder, so we can't enumerate handles to target
3. Only exposes `SupportedTypes = ["SMS_GSM"]` — iMessage isn't even queryable

In short: iOS scopes the MAP server to a forward-only push of new messages. Existing history isn't addressable, so deletion isn't possible from a paired Linux box. Local-only hide is the honest design.

## Development

```bash
.venv/bin/pip install textual-dev
.venv/bin/python -m textual run --dev imessage_tui.app:IMessageTUI
```

Live-reloads on file save. Press **Ctrl+\`** in the running app to open the inline devtools (widget tree + computed CSS). Optional second terminal: `.venv/bin/python -m textual console` to stream logs.

Headless tests run without a TTY:
```python
from imessage_tui.app import IMessageTUI
async with IMessageTUI().run_test(headless=True) as pilot:
    await pilot.press("c")
    ...
```

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. Two upstream fixes I needed against current Arch + iphonebridge that aren't in upstream yet are worth knowing about if you hit them:

1. `iphonebridge`'s `bluez-obexd` doctor check hardcodes the Debian path `/usr/libexec/bluetooth/obexd`; on Arch/Fedora it's at `/usr/lib/bluetooth/obexd`.
2. `iphonebridge`'s `map_send.py` passes a bare `{}` to `PushMessage`, which fails on Python 3.14 with "unable to guess signature from an empty dict". Wrap as `dbus.Dictionary({}, signature="sv")`.

Both filed at [gabrielmeir53/iphonebridge](https://github.com/gabrielmeir53/iphonebridge) — vendored locally for now.
