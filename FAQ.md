# sshfs-keeper FAQ

## How do I get notified when a mount fails?

Set `webhook_url` in `[notifications]`. Any HTTP POST endpoint works: Slack incoming webhooks, Discord webhooks, ntfy.sh topic URLs, etc.

```toml
[notifications]
webhook_url = "https://ntfy.sh/my-topic"
on_failure = true
on_recovery = true
on_backoff = false
```

Payload: `{"event": "failure"|"recovery"|"backoff", "mount": "name", "error": "...", "timestamp": "..."}`

## How do I scrape Prometheus metrics?

`GET /metrics` returns plain text in Prometheus exposition format. Metrics include:
- `sshfs_keeper_mount_healthy{name}` — 1 if healthy
- `sshfs_keeper_mount_count{name}` — total successful mounts
- `sshfs_keeper_mount_retry_count{name}` — current retries
- `sshfs_keeper_mount_duration_seconds{name}` — last mount duration
- `sshfs_keeper_sync_run_count{name}` — total rsync runs
- `sshfs_keeper_sync_bytes_sent{name}` — bytes sent last run

## Why does the web UI update without a page reload?

The dashboard subscribes to `GET /api/events` (Server-Sent Events). When a mount status changes the server pushes an event; the browser reloads the page. The SSE indicator dot in the header turns green when connected.

## How do I use a passphrase-protected SSH key?

Add `identity_passphrase` to the mount config. The daemon calls `ssh-add` before each mount attempt.

```toml
[[mount]]
name = "nas"
remote = "user@host:/path"
local = "/mnt/nas"
identity = "/home/user/.config/sshfs-keeper/keys/id_ed25519"
identity_passphrase = "my passphrase"
```

Note: This stores the passphrase in plaintext in config.toml — use file permissions (600) to restrict access.

## How do I reload config without restarting?

```bash
sshfs-keeper reload
```

Sends SIGHUP to the running daemon. Adds new mounts, removes deleted ones; preserves runtime state (retry counts, backoff) for existing mounts.

## How do I use rclone instead of sshfs for a mount?

Set `mount_tool = "rclone"` in the mount config. The `remote` field accepts either SSH format (`user@host:/path`, auto-converted to `:sftp,host=…,user=…:path`) or a pre-configured rclone remote (`myremote:/path`):

```toml
[[mount]]
name = "nas"
remote = "miro@192.168.1.10:/media/hdd"
local = "/mnt/nas"
mount_tool = "rclone"
```

rclone must be installed and `--allow-other` must be permitted in `/etc/fuse.conf`. This is the recommended backend for macOS and Windows (requires WinFsp).

## How do I use lsyncd for real-time sync?

Set `sync_tool = "lsyncd"` in the sync config. lsyncd is invoked with `--oneshot` so it performs one sync pass and exits, matching the interval-based scheduler:

```toml
[[sync]]
name = "mirror"
source = "/local/data"
target = "user@host:/remote/data"
interval = 300
sync_tool = "lsyncd"
```

lsyncd must be installed. For local-to-local sync `default.rsync` is used; for remote targets `default.rsyncssh` is used.

## How do I install sshfs-keeper as a system service?

```bash
sshfs-keeper install-service
```

Detects your OS and writes the appropriate service file:
- **Linux**: `~/.config/systemd/user/sshfs-keeper.service` → `systemctl --user enable --now sshfs-keeper`
- **macOS**: `~/Library/LaunchAgents/com.sshfs-keeper.plist` → `launchctl load ~/Library/LaunchAgents/com.sshfs-keeper.plist`
- **Windows**: `%APPDATA%\sshfs-keeper\install-service.bat` (requires NSSM)

## What happens if a mount point is managed by autofs?

If `/proc/mounts` contains an `autofs` entry covering the mount point (or any parent directory), sshfs-keeper skips its own remount logic and marks the mount `HEALTHY`. autofs handles on-demand mounting; keeper and autofs would conflict if both tried to remount.

## How does sync retry backoff work?

When a sync job fails, the consecutive `fail_count` is incremented. Once `fail_count >= max_retries` (default 3 from `[daemon]`), the next retry is scheduled at `backoff_base * 2^(fail_count - max_retries)` seconds instead of the full `interval`. On success, `fail_count` resets to 0 and normal scheduling resumes.

The `fail_count` is exposed in `GET /api/syncs` (snapshot) so the dashboard can show it.

## How do I view rsync output for a sync job?

Click the **📋 Log** button on a sync card in the web UI, or:

```bash
curl http://localhost:8765/api/syncs/<name>/log
```

Returns the last 50 lines of stdout+stderr from the most recent run.

## How do I see disk usage on mount cards?

The dashboard automatically shows a usage bar on healthy mounts. It calls `os.statvfs()` on the local mount point. If the mount point is inaccessible the bar is hidden.

## Where is the PID file?

`~/.config/sshfs-keeper/daemon.pid` — written at startup, removed on shutdown.

## How do I write logs to a file?

Set `log_file` in `[daemon]`. Uses a 5 MB rotating file handler with 3 backups.

```toml
[daemon]
log_file = "/var/log/sshfs-keeper.log"
```

## Are notification flags (on_failure, on_recovery) persisted without a webhook URL?

Yes — the `[notifications]` block is always written to config.toml. Previously it was only written when `webhook_url` was set, silently losing `on_failure=false` etc. on next save.

## What does the version endpoint return?

```bash
curl http://localhost:8765/api/version
# {"version": "0.1.0"}
```


## Why does the dashboard have a moon/sun button?
Dark mode is the default. The 🌙/🌕 button in the header toggles light mode. The preference is saved in `localStorage` and persisted across sessions.

## What does the rclone badge on a mount card mean?
When a mount uses `mount_tool = "rclone"` (instead of the default `sshfs`), the card header shows a `rclone` badge in addition to the health status badge.

## How does sync exponential backoff work?
After `max_retries` consecutive failures (default: 3), the sync manager applies exponential backoff: `backoff_base * 2^(fail_count - max_retries)` seconds between retries (default base: 60s). The fail count resets to 0 on the first successful run.

## Why does /api/syncs return an object not a list?
`GET /api/syncs` returns `{"syncs": [...]}` (not a bare list). The earlier bare-list variant was a duplicate route that was silently shadowed by FastAPI — it has been removed.

## Why do my mounts disappear after a daemon restart?

Root cause: `AppConfig.save()` previously used `path.write_text()` which is not atomic. If systemd sends SIGKILL (after a SIGTERM timeout), the file write is interrupted mid-way, leaving a truncated config.toml with 0 mounts.

Fix (deployed in commit d1caa14): `save()` now writes to a sibling `.tmp` file, calls `os.fsync()`, then `os.replace()` — which is atomic on Linux. Also keeps a `.bak` of the previous config. Combined with `TimeoutStopSec=30` in the systemd unit, this prevents corruption.

## Why doesn't `uv tool install --force` pick up my source changes?

`uv tool install` builds a wheel and copies files to `~/.local/share/uv/tools/sshfs-keeper/`. It may use a cached build. Always run with `--no-cache` after changing source:

```bash
uv tool install --force . --no-cache
```
