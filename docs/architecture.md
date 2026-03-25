# Architecture

## Components

```
main.py (_run)
├── Monitor              — health-check loop, remount, SSE event emission
├── SyncManager          — interval-based sync jobs
└── FastAPI (uvicorn)    — REST API + dashboard + SSE broker
        └── api.py       — wires Monitor and SyncManager via setup()
```

All three subsystems run as `asyncio.Task`s in the same event loop — `_run — main.py:97`.

### AppConfig (`config.py:84`)

Loaded once at startup by `AppConfig.load — config.py:242`. Searched in order:

1. `~/.config/sshfs-keeper/config.toml`
2. `./config.toml`

Holds `DaemonConfig`, `ApiConfig`, `NotificationsConfig`, and lists of `MountConfig` and `SyncConfig`. Mutated in-place by API write endpoints; saved to disk via `AppConfig.save — config.py:93`.

### Monitor (`monitor.py:39`)

Owns a `dict[str, MountState]` keyed by mount name. Runs `_loop — monitor.py:134` as `asyncio.Task` named `"sshfs-monitor"`.

Each loop iteration calls `_check_all — monitor.py:139`, which `asyncio.gather`s one `_check_one` coroutine per mount, then sleeps `check_interval` seconds.

### SyncManager (`sync.py:207`)

Owns a `dict[str, SyncState]` keyed by job name. Runs `_loop — sync.py:281` as `asyncio.Task` named `"sync-manager"`.

Loop polls every 5 seconds, collects jobs whose `_next_run <= now`, and `asyncio.gather`s `_run_job` for each due job. Jobs are staggered by 5 s at startup — `SyncManager.start — sync.py:229`.

### FastAPI / api.py

Module-level singletons `_monitor`, `_config`, `_sync_manager` are set by `api.setup — api.py:48` before uvicorn starts. All route handlers access them via `_get_monitor()`, `_get_config()`, `_get_sync_manager()` guard functions.

---

## Startup sequence

1. `main()` parses CLI, calls `_cmd_start — main.py:200`.
2. `AppConfig.load` reads TOML, validates, or exits with errors.
3. `Monitor` and `SyncManager` instances created; `api.setup()` wires them and registers `_broadcast_event` as an SSE listener.
4. `await monitor.start()` → background task begins first health-check round.
5. `await sync_manager.start()` → background task schedules first sync pass.
6. `uvicorn.Server.serve()` started as `asyncio.Task` named `"uvicorn"`.
7. Signal handlers registered: `SIGTERM`/`SIGINT` → `stop_event.set()`; `SIGHUP` → `reload_event.set()`.
8. `stop_event.wait()` blocks until shutdown signal.
9. On shutdown: reload watcher cancelled, uvicorn signalled (`server.should_exit = True`), monitor and sync manager stopped gracefully.

Signal handling — `_run — main.py:97`, signal registration at `main.py:121`.

---

## Mount lifecycle

```
UNMOUNTED ──────────────────────────────────────────────┐
    │  (remount attempt)                                  │
    ▼                                                     │
MOUNTING ──► HEALTHY  (on success)                        │
    │                                                     │
    ▼ (on failure)                                        │
 ERROR                                                    │
    │  retry_count >= max_retries?                        │
    ├── No  ──► wait remount_delay → MOUNTING (retry)     │
    └── Yes ──► backoff_until = now + backoff_base*2^n   │
                (stays in ERROR; backoff checked each loop)
```

**Stale mount detection** (`_check_one — monitor.py:143`): if `is_healthy` returns `False` but the path is present in `/proc/mounts` (or `mount` on macOS), the mount is `STALE`. `unmount()` is called before the remount attempt.

**autofs skip**: `is_autofs_managed — mount.py:26` checks `/proc/mounts` for an `autofs` entry covering `local` or any parent; if found, status is set to `HEALTHY` and no remount is attempted.

**Health check** (`is_healthy — mount.py:105`): checks `/proc/mounts` membership AND calls `os.statvfs` via `_probe_path` (10 s timeout in a thread-pool executor). Both must succeed for `HEALTHY`.

**Backoff formula** — `_remount — monitor.py:207`:
```
backoff = backoff_base * 2 ** (retry_count - max_retries)
```
`backoff_until` is reset to 0 on success or on `trigger_remount` API call.

---

## SSE event flow

1. `Monitor._emit — monitor.py:186` is called when status transitions to `HEALTHY` or `ERROR`.
2. The payload dict (`event`, `mount`, `status`, `timestamp`) is passed to all registered listeners.
3. `api._broadcast_event — api.py:80` is the sole registered listener; it `put_nowait`s the payload into every active `asyncio.Queue` (one per connected SSE client, max 100 items each).
4. `sse_events._generate — api.py:209` dequeues payloads and yields two SSE frames: one named `<event_type>`, one named `mount_update_<name>`.
5. The dashboard HTMX listener on `mount_update_<name>` triggers a fetch of `GET /fragments/mounts/{name}` and swaps the card in-place.
6. A `: keepalive` comment is emitted every 30 s when the queue is empty.

---

## Config save and reload

**Save** (`AppConfig.save — config.py:93`): serialises the in-memory `AppConfig` to TOML manually (no third-party serialiser). Writes to `.config-<random>.tmp` via `tempfile.mkstemp`, calls `os.fsync`, then `os.replace` (atomic on Linux/macOS). Keeps `.bak` of the previous file.

**Reload** (`SIGHUP` → `_do_reload — main.py:158`): re-reads config from the same path. Merges changes into the live `AppConfig` in-place:

- New mounts: new `MountState` appended to `monitor.states`.
- Deleted mounts: removed from `monitor.states`.
- Existing mounts: config updated but runtime state (`retry_count`, `backoff_until`, `mount_count`) is preserved.
- Daemon/API/notification settings replaced wholesale.

API write endpoints (`POST /api/mounts`, `PUT /api/settings`, etc.) mutate `_config` and call `cfg.save()` immediately — changes survive restart without a separate reload step.

---

## Mount backends

| Backend | Entry point | Key behaviour |
|---------|-------------|---------------|
| sshfs | `_mount_sshfs — mount.py:201` | Runs `sshfs <remote> <local> -o <options> -o IdentityFile=… -o StrictHostKeyChecking=accept-new -o BatchMode=yes`. 30 s timeout |
| rclone (mount) | `_mount_rclone — mount.py:244` | Converts SSH remotes via `_ssh_remote_to_rclone — mount.py:165`. Runs `rclone mount --daemon --allow-other --vfs-cache-mode writes`. 30 s timeout |

Unmount: `fusermount3 -uz` then `fusermount -uz` (Linux), or `umount -f` then `diskutil unmount force` (macOS) — `unmount — mount.py:112`.

## Sync backends

| Backend | Builder | Notes |
|---------|---------|-------|
| rsync | `_build_rsync_cmd — sync.py:63` | Options passed verbatim. Exit 24 treated as success |
| lsyncd | `_build_lsyncd_cmd — sync.py:78` | Writes a temp Lua config; `--oneshot` for one-pass behaviour |
| rclone | `_build_rclone_sync_cmd — sync.py:137` | `rclone sync --stats-one-line --stats 0`; SSH remotes auto-converted |

All sync runs have a 1-hour timeout — `SyncManager._run_job — sync.py:289`.
