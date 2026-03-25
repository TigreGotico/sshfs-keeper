#!/usr/bin/env bash
# Install sshfs-keeper as a LaunchAgent on macOS (no sudo needed).
# Run as your regular user: bash install-macos.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/sshfs-keeper"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_SRC="$SCRIPT_DIR/launchd/com.sshfs-keeper.plist"
PLIST_DEST="$LAUNCH_AGENTS/com.sshfs-keeper.plist"
LABEL="com.sshfs-keeper"

# ---- dependency checks ----
missing=()
command -v sshfs     &>/dev/null || missing+=("sshfs")
command -v rsync     &>/dev/null || missing+=("rsync")

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing dependencies: ${missing[*]}"
    echo "Install with: brew install macfuse ${missing[*]}"
    echo "macFUSE also requires approving a kernel extension:"
    echo "  System Settings → Privacy & Security → allow macFUSE"
    echo ""
    read -rp "Continue anyway? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ---- install package ----
if command -v uv &>/dev/null; then
    uv tool install --reinstall "$SCRIPT_DIR"
else
    pip3 install --user "$SCRIPT_DIR"
fi

# Resolve the installed binary
BINARY="$(command -v sshfs-keeper 2>/dev/null || echo "$HOME/.local/bin/sshfs-keeper")"
echo "Binary: $BINARY"

# ---- config ----
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    # Write a macOS-appropriate example config
    cat > "$CONFIG_DIR/config.toml" <<TOML
# sshfs-keeper config — ~/.config/sshfs-keeper/config.toml

[daemon]
check_interval = 30
remount_delay = 5
max_retries = 3
backoff_base = 60
log_level = "INFO"

[api]
host = "127.0.0.1"
port = 8765

# [[mount]]
# name = "nas"
# remote = "user@192.168.1.1:/media/data"
# local = "/Users/$(whoami)/mnt/nas"
# options = "cache=yes,compression=yes,ServerAliveInterval=15,ServerAliveCountMax=3,reconnect"
# identity = "$HOME/.config/sshfs-keeper/keys/id_ed25519"
# enabled = true
TOML
    echo "Config written to $CONFIG_DIR/config.toml — edit before starting"
fi

# ---- LaunchAgent ----
mkdir -p "$LAUNCH_AGENTS"

sed "s|YOUR_USERNAME|$(whoami)|g; s|/Users/YOUR_USERNAME/.local/bin/sshfs-keeper|$BINARY|g" \
    "$PLIST_SRC" > "$PLIST_DEST"

# Unload first if already loaded (ignore errors)
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"

echo ""
echo "Done. sshfs-keeper is running."
echo ""
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.toml  (or use the web UI)"
echo "  2. Open http://127.0.0.1:8765"
echo ""
echo "Useful commands:"
echo "  launchctl start $LABEL"
echo "  launchctl stop  $LABEL"
echo "  tail -f /tmp/sshfs-keeper.log"
