# sshfs-keeper — documentation

Self-healing SSHFS/rclone mount daemon with a FastAPI web UI, sync jobs, Prometheus metrics, and webhook notifications.

## Contents

| File | Description |
|------|-------------|
| [configuration.md](configuration.md) | All config.toml fields with types and defaults |
| [api.md](api.md) | REST API reference (endpoints, payloads, responses) |
| [architecture.md](architecture.md) | Component overview, data flow, key classes |

## Quick start

```bash
uv tool install .
sshfs-keeper start            # starts daemon + web UI on :8765
sshfs-keeper install-service  # write systemd / launchd / NSSM service file
```

Minimal config (`~/.config/sshfs-keeper/config.toml`):

```toml
[[mount]]
name = "nas"
remote = "user@host:/path"
local = "/mnt/nas"
```

## Key classes

| Class | File | Role |
|-------|------|------|
| `AppConfig` | `config.py:80` | Top-level config; load / save / validate |
| `Monitor` | `monitor.py:39` | Mount health-check + remount loop |
| `MountState` | `monitor.py:25` | Per-mount runtime state |
| `SyncManager` | `sync.py:131` | Interval-based rsync / lsyncd / rclone jobs |
| `SyncState` | `sync.py:51` | Per-job runtime state |
| `NotificationsConfig` | `config.py:54` | Webhook notification settings |

## CLI subcommands

```
sshfs-keeper start              start daemon
sshfs-keeper status             show mount status table
sshfs-keeper syncs              show sync job status table
sshfs-keeper syncs --trigger N  trigger named sync job
sshfs-keeper mount <name>       trigger remount via running daemon
sshfs-keeper unmount <name>     force unmount via running daemon
sshfs-keeper reload             reload config (SIGHUP)
sshfs-keeper install-service    write OS service file
```

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard |
| GET | `/health` | 200 OK / 503 if any enabled mount unhealthy |
| GET | `/api/version` | Daemon version |
| GET | `/api/status` | Mount snapshot JSON |
| GET | `/api/syncs` | Sync job snapshot JSON |
| GET | `/metrics` | Prometheus text metrics |
| GET | `/api/events` | SSE live event stream |
| GET/PUT | `/api/notifications` | Read/write webhook settings |
| PUT | `/api/settings` | Write daemon settings |
| POST | `/api/mounts/{name}/remount` | Trigger remount |
| POST | `/api/mounts/{name}/unmount` | Force unmount |
| GET | `/api/syncs/{name}/log` | Last sync tool output (50 lines) |
| POST | `/api/syncs/{name}/trigger` | Run sync immediately |

## Configuration reference

```toml
[daemon]
check_interval = 30       # seconds between health checks
remount_delay  = 5        # wait before remount attempt
max_retries    = 3        # failures before exponential backoff
backoff_base   = 60       # backoff_base * 2^n seconds between retries
log_level      = "INFO"
log_file       = ""       # optional rotating file (5 MB x 3 backups)
json_logs      = false    # structured JSON log lines

[api]
host           = "0.0.0.0"
port           = 8765
api_key        = ""       # optional X-API-Key header
ssl_certfile   = ""       # optional TLS certificate path
ssl_keyfile    = ""       # optional TLS private key path

[notifications]
webhook_url    = ""       # Slack / Discord / ntfy.sh POST endpoint
on_failure     = true
on_recovery    = true
on_backoff     = false

[[mount]]
name           = "nas"
remote         = "user@host:/path"
local          = "/mnt/nas"
mount_tool     = "sshfs"  # "sshfs" | "rclone"
options        = "cache=yes,compression=yes,ServerAliveInterval=15,..."
identity       = ""       # path to SSH private key
identity_passphrase = ""  # pre-loads key into ssh-agent
enabled        = true

[[sync]]
name           = "backup"
source         = "/local/data/"
target         = "user@host:/remote/"
interval       = 3600     # seconds between runs
sync_tool      = "rsync"  # "rsync" | "lsyncd" | "rclone"
options        = "-az --delete --stats"
identity       = ""
enabled        = true
```
