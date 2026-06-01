#!/usr/bin/env bash
# install.sh — one-shot installer for imessage-tui.
# Usage: curl -fsSL https://raw.githubusercontent.com/hongnoul/imessage-tui/main/install.sh | bash
set -euo pipefail

REPO="hongnoul/imessage-tui"
GIT_URL="https://github.com/${REPO}.git"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }

bold "▸ imessage-tui installer"

# --- 1. Python check ---------------------------------------------------------
if ! command -v python3 >/dev/null; then
    red "Python 3 not found. Install it via your distro:"
    red "  Arch:   sudo pacman -S python"
    red "  Debian: sudo apt install python3 python3-venv"
    red "  Fedora: sudo dnf install python3"
    exit 1
fi

pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
pymajor=$(python3 -c 'import sys; print(sys.version_info[0])')
pyminor=$(python3 -c 'import sys; print(sys.version_info[1])')
if [[ "$pymajor" -lt 3 ]] || { [[ "$pymajor" -eq 3 ]] && [[ "$pyminor" -lt 11 ]]; }; then
    red "Python 3.11+ required (you have $pyver)."
    exit 1
fi
green "  ✓ python $pyver"

# --- 2. pipx (preferred) -----------------------------------------------------
if command -v pipx >/dev/null; then
    green "  ✓ pipx found — using it for a clean global install"
    pipx install --force "git+${GIT_URL}"
    green "▸ installed. Run: imessage-tui"
    yellow "  ↳ Make sure the iphonebridge daemon is running:"
    yellow "      systemctl --user status iphonebridge"
    exit 0
fi

yellow "  ! pipx not found — falling back to a user-local venv"
yellow "    (recommend: install pipx for the cleanest setup)"
yellow "      Arch:   sudo pacman -S python-pipx"
yellow "      Debian: sudo apt install pipx"
yellow "      Fedora: sudo dnf install pipx"

# --- 3. venv fallback --------------------------------------------------------
TARGET="${IMESSAGE_TUI_HOME:-$HOME/.local/share/imessage-tui}"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

if [[ -d "$TARGET" ]]; then
    yellow "  removing existing install at $TARGET"
    rm -rf "$TARGET"
fi

if ! command -v git >/dev/null; then
    red "git not found. Install it first."
    exit 1
fi

git clone --depth 1 "${GIT_URL}" "$TARGET" >/dev/null
python3 -m venv "${TARGET}/.venv"
"${TARGET}/.venv/bin/pip" install --quiet --upgrade pip
"${TARGET}/.venv/bin/pip" install --quiet -e "$TARGET"

ln -sf "${TARGET}/.venv/bin/imessage-tui" "${BIN_DIR}/imessage-tui"
green "  ✓ installed → ${BIN_DIR}/imessage-tui"

# --- 4. PATH check -----------------------------------------------------------
if ! printf ':%s:' "$PATH" | grep -q ":${BIN_DIR}:"; then
    yellow "  ! ${BIN_DIR} isn't on your PATH"
    yellow "    Add this to your shell rc file:"
    yellow "      export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

green "▸ done. Run: imessage-tui"
yellow "  ↳ Make sure the iphonebridge daemon is running:"
yellow "      systemctl --user status iphonebridge"
