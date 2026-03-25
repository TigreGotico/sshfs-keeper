#!/usr/bin/env bash
# Install sshfs-keeper as a systemd user service (no sudo needed).
# Run as the user who will own the mounts: bash install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/sshfs-keeper"
SYSTEMD_DIR="$HOME/.config/systemd/user"

# Check dependencies
for cmd in sshfs fusermount3; do
    command -v "$cmd" &>/dev/null || echo "WARNING: '$cmd' not found — install it (e.g. apt install sshfs)"
done

# Install as an isolated uv tool (puts binary in ~/.local/bin, no venv conflicts)
if command -v uv &>/dev/null; then
    uv tool install --reinstall "$SCRIPT_DIR"
else
    pip install --user --quiet "$SCRIPT_DIR"
fi
echo "Installed sshfs-keeper to ~/.local/bin/sshfs-keeper"

# Config
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    cp "$SCRIPT_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
    echo "Example config written to $CONFIG_DIR/config.toml — edit before starting"
fi

# Systemd user service
mkdir -p "$SYSTEMD_DIR"
cp "$SCRIPT_DIR/systemd/sshfs-keeper.service" "$SYSTEMD_DIR/sshfs-keeper.service"
systemctl --user daemon-reload
systemctl --user enable sshfs-keeper

# Enable lingering so the service starts at boot without a login session
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: 'loginctl enable-linger' failed — run it manually if you want the service to start at boot without logging in"

echo ""
echo "Done. Next steps:"
echo "  1. Edit $CONFIG_DIR/config.toml  (or use the web UI)"
echo "  2. systemctl --user start sshfs-keeper"
echo "  3. Open http://localhost:8765"
echo ""
echo "Useful commands:"
echo "  systemctl --user status sshfs-keeper"
echo "  journalctl --user -u sshfs-keeper -f"
