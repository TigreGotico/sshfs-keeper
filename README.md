![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)
![Status: Beta](https://img.shields.io/badge/status-beta-yellow)

# sshfs-keeper

A daemon that monitors SSHFS (and rclone) mounts, automatically remounts them when they drop, and exposes a live web dashboard, REST API, Prometheus metrics, and webhook notifications. It also schedules directory-sync jobs via rsync, lsyncd, or rclone.

## Features

- Auto-remount on disconnect with exponential backoff
- Stale-mount detection (FUSE device present but inaccessible): force-unmounts before remounting
- autofs-aware: skips mounts managed by autofs
- Passphrase-protected SSH keys: pre-loads via `ssh-add`
- Mount backends: `sshfs` or `rclone` (per-mount, hot-switchable via API)
- Sync jobs: rsync / lsyncd / rclone on configurable intervals (with multi-target support)
- One-shot file transfers: copy/move files between hosts using rsync, rclone, or SCP
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

_Web UI screenshot — open http://localhost:8765 after starting the daemon._

## Docs

- [docs/index.md](docs/index.md) — overview, key classes, CLI reference, endpoint table
- [docs/configuration.md](docs/configuration.md) — full config.toml field reference
- [docs/api.md](docs/api.md) — REST API reference
- [docs/architecture.md](docs/architecture.md) — component overview and data flow
- [FAQ.md](FAQ.md) — common questions
