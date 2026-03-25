# Configuration reference

Config is read from the first existing path in this order — `AppConfig.load — config.py:242`:

1. `~/.config/sshfs-keeper/config.toml`
2. `./config.toml`

Override at runtime with `sshfs-keeper -c /path/to/config.toml start`.

Config is written back atomically: written to a `.tmp` sibling, `os.fsync()`d, then `os.replace()`d. The previous file is kept as `.bak` — `AppConfig.save — config.py:93`.

Validation (`AppConfig.validate — config.py:197`) runs at startup; duplicate names, unknown tool values, and missing required fields are rejected.

---

## [daemon]

Defined in `DaemonConfig — config.py:38`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `check_interval` | int | `30` | Seconds between health-check rounds |
| `remount_delay` | int | `5` | Seconds to wait before a remount attempt |
| `max_retries` | int | `3` | Consecutive failures before exponential backoff activates |
| `backoff_base` | int | `60` | Backoff interval = `backoff_base * 2^(retry_count - max_retries)` seconds |
| `log_level` | str | `"INFO"` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `log_file` | str \| null | `null` | Path for a rotating log file (5 MB, 3 backups). Console handler always active |
| `json_logs` | bool | `false` | Structured JSON log lines via `python-json-logger` |

---

## [api]

Defined in `ApiConfig — config.py:48`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | str | `"0.0.0.0"` | Bind address for the uvicorn server |
| `port` | int | `8765` | TCP port |
| `api_key` | str \| null | `null` | When set, all write endpoints require `X-API-Key: <value>`. Read endpoints are unauthenticated |
| `ssl_certfile` | str \| null | `null` | Path to TLS certificate (PEM). Both `ssl_certfile` and `ssl_keyfile` must be set to enable TLS |
| `ssl_keyfile` | str \| null | `null` | Path to TLS private key (PEM) |

---

## [notifications]

Defined in `NotificationsConfig — config.py:57`.

The webhook receives a JSON `POST` with body: `{"event": "failure"|"recovery"|"backoff", "mount": "<name>", "error": "<msg>"|null, "timestamp": "<ISO-8601>"}` — `send_webhook — notify.py:16`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `webhook_url` | str \| null | `null` | HTTP/HTTPS endpoint (Slack, Discord, ntfy.sh, any POST). Notifications are disabled when `null` |
| `on_failure` | bool | `true` | Send when a mount fails to remount |
| `on_recovery` | bool | `true` | Send when a mount returns to healthy |
| `on_backoff` | bool | `false` | Send when exponential backoff activates |

---

## [[mount]]

Repeatable; defined in `MountConfig — config.py:26`. `name` must be unique across all `[[mount]]` entries.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | **required** | Unique identifier used in API paths and log messages |
| `remote` | str | **required** | SSH-style `user@host:/path` or rclone remote `myremote:/path` |
| `local` | str | **required** | Absolute local mount point. Created automatically if absent |
| `mount_tool` | str | `"sshfs"` | `"sshfs"` or `"rclone"`. rclone auto-converts SSH remotes to `:sftp,host=…` format — `_ssh_remote_to_rclone — mount.py:165` |
| `options` | str | `"cache=yes,compression=yes,ServerAliveInterval=15,ServerAliveCountMax=3,reconnect"` | Passed verbatim as `-o <options>` to sshfs. Ignored by rclone (use `rclone config` instead) |
| `identity` | str \| null | `null` | Path to SSH private key. Stored in `~/.config/sshfs-keeper/keys/` by convention |
| `identity_passphrase` | str \| null | `null` | Passphrase for `identity`. Pre-loaded into ssh-agent via `ssh-add` before each mount attempt — `_add_key_to_agent — mount.py:298`. Stored plaintext; restrict config.toml to mode 600 |
| `enabled` | bool | `true` | When `false`, monitor marks the mount `DISABLED` and skips all health checks and remounts |

**sshfs** always adds `-o StrictHostKeyChecking=accept-new -o BatchMode=yes` — `_mount_sshfs — mount.py:201`.

**rclone** always adds `--daemon --allow-other --vfs-cache-mode writes`. Requires `user_allow_other` in `/etc/fuse.conf` — `_mount_rclone — mount.py:244`.

---

## [[sync]]

Repeatable; defined in `SyncConfig — config.py:73`. `name` must be unique across all `[[sync]]` entries.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | **required** | Unique identifier |
| `source` | str | **required** | Source path: local or `user@host:/path` |
| `target` | str | **required** | Target path: local or `user@host:/path` |
| `interval` | int | `3600` | Seconds between runs. Must be >= 1 |
| `sync_tool` | str | `"rsync"` | `"rsync"`, `"lsyncd"`, or `"rclone"` |
| `options` | str | `"-az --delete --stats"` | Passed verbatim to rsync. Ignored by lsyncd and rclone |
| `identity` | str \| null | `null` | SSH key path; passed as `-e "ssh -i …"` (rsync), via Lua config (lsyncd), or `--sftp-key-file` (rclone) |
| `enabled` | bool | `true` | When `false`, job is skipped |

**rsync** exit codes 0 and 24 (vanished source files) are treated as success — `_SOFT_EXIT_CODES — sync.py:19`.

**lsyncd** is invoked with `--oneshot` — one sync pass then exit — and a temporary Lua config written to `tempfile.mkstemp` — `_build_lsyncd_cmd — sync.py:78`.

**rclone** uses `rclone sync --stats-one-line --stats 0` — `_build_rclone_sync_cmd — sync.py:137`.

Sync jobs are staggered by 5 seconds at startup to avoid simultaneous first runs — `SyncManager.start — sync.py:229`.

Backoff for sync failures mirrors mount backoff: after `max_retries` consecutive failures, next retry = `backoff_base * 2^(fail_count - max_retries)` seconds — `SyncManager._run_job — sync.py:289`.
