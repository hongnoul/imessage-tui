# imessage-tui

A terminal UI for the [iphonebridge](https://github.com/gabrielmeir53/iphonebridge) daemon. spotify-player-style panes, vim-style keybinds, iOS-flavored bubbles in a kitty terminal.

## Install

```
cd ~/git/imessage-tui
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/imessage-tui
```

Or one-off without installing:
```
PYTHONPATH=src .venv/bin/python -m imessage_tui.app
```

Requires the iphonebridge daemon running (`systemctl --user status iphonebridge`).

## Keybinds

| key       | action                              |
|-----------|-------------------------------------|
| ↑ ↓ j k   | navigate conversations              |
| enter     | open conversation                   |
| `c`       | compose new message                 |
| `r`       | focus reply input                   |
| `/`       | search conversations                |
| esc       | clear search / close modal          |
| `d`       | hide latest message (local-only)    |
| `D`       | hide entire conversation (local)    |
| `g`       | resync contacts via PBAP            |
| ctrl+r    | reload history from events.jsonl    |
| `?`       | show help                           |
| `q`       | quit                                |

## Hides vs deletes

iOS does not honor MAP `SetMessageStatus(Deleted)`, so this app cannot delete from your phone — `d` / `D` only hide locally (in `~/.local/state/imessage-tui/hides.sqlite`). The iPhone still has every message.

## Architecture

```
events.jsonl ─tail──┐
contacts.sqlite ────┤
                    ├──> bridge.py ──> Textual app
hides.sqlite ───────┤
busctl --user ──────┘   (Send / IsHealthy / contacts-sync)
```

No Node, no Electron, no web server. Pure Python + Textual.
