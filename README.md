![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)
![Status: Beta](https://img.shields.io/badge/status-beta-yellow)
![Vibe Coded](https://img.shields.io/badge/vibe%20coded-%F0%9F%A4%96-blueviolet)

> **WARNING:** This software was vibe-coded with AI assistance and has not been reviewed by humans.
> It may contain bugs, bad ideas, and code that was written at 2am by a language model with no skin in the game.
> No warranty. No guarantee. Might explode. Use at your own risk.
> The authors accept no liability for lost data, unmounted drives, confused NAS devices, or existential dread.

# sshfs-keeper

A daemon that monitors SSHFS (and rclone) mounts, automatically remounts them when they drop, and exposes a live web dashboard, REST API, Prometheus metrics, and webhook notifications. It also schedules directory-sync jobs via rsync, lsyncd, or rclone.

## Features

- Auto-remount on disconnect with exponential backoff
- Stale-mount detection (FUSE device present but inaccessible): force-unmounts before remounting
- autofs-aware: skips mounts managed by autofs
- Passphrase-protected SSH keys: pre-loads via `ssh-add`
- Mount backends: `sshfs` or `rclone` (per-mount, hot-switchable via API)
- Sync jobs: rsync / lsyncd / rclone on configurable intervals
- Live web dashboard (HTMX + SSE, no page reload required)
- REST API with optional `X-API-Key` authentication and optional TLS
- Prometheus metrics at `GET /metrics`
- Webhook notifications (Slack, Discord, ntfy.sh, any HTTP POST endpoint)
- Atomic config saves: `os.replace()` + `.bak` — survives SIGKILL mid-write
- SIGHUP config reload: adds/removes mounts without restarting
- `install-service`: generates systemd user unit, launchd plist, or NSSM batch script

## Install

```bash
uv tool install .
```

Requires `sshfs` and/or `rclone` to be installed separately.

## Minimal config

`~/.config/sshfs-keeper/config.toml`:

```toml
[[mount]]
name   = "nas"
remote = "user@host:/path"
local  = "/mnt/nas"
```

## Start

```bash
sshfs-keeper start            # daemon + web UI on http://0.0.0.0:8765
sshfs-keeper install-service  # write systemd/launchd/NSSM service file
```

## Web UI

<img width="1159" height="837" alt="image" src="https://github.com/user-attachments/assets/e6b7bf49-5578-4e9a-a767-3b188c9a8db9" />

> open http://localhost:8765 after starting the daemon.

## Docs

- [docs/index.md](docs/index.md) — overview, key classes, CLI reference, endpoint table
- [docs/configuration.md](docs/configuration.md) — full config.toml field reference
- [docs/api.md](docs/api.md) — REST API reference
- [docs/architecture.md](docs/architecture.md) — component overview and data flow
- [FAQ.md](FAQ.md) — common questions
