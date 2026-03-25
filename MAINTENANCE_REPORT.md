# Maintenance Report

## 2026-03-25 — Transfers feature

**AI Model**: claude-sonnet-4-6
**Actions Taken**:
- Created `sshfs_keeper/transfer.py`: `TransferManager`, `TransferRequest`, `TransferState`, `_build_cmd`; supports rsync-over-SSH, rclone, SCP, local rsync; progress streaming; bounded history (20 entries); cancel via SIGTERM
- Extended `api.py`: `TransferPayload` model; `POST /api/transfers`, `GET /api/transfers`, `DELETE /api/transfers/{id}`, `GET /api/transfers/{id}/log`, `GET /fragments/transfers`
- Extended `main.py`: creates `TransferManager()` and passes to `api.setup()`
- Added Transfers tab to `dashboard.html`: protocol dropdown, move toggle, dynamic hints, 3s polling history table
- Created `_transfer_rows.html` fragment template
- Created `test/test_transfer.py`: 18 tests, 90% coverage
**Oversight**: AI-generated; all tests pass (152 total)

## 2026-03-24 — Comprehensive feature additions

**AI Model**: claude-sonnet-4-6
**Actions Taken**:
- Added `NotificationsConfig` dataclass with webhook URL and per-event flags
- Created `sshfs_keeper/notify.py` — fire-and-forget webhook delivery via httpx
- Created `sshfs_keeper/metrics.py` — hand-rolled Prometheus text exposition (no extra dep)
- Added `GET /metrics`, `GET /api/version`, `GET /api/events` (SSE), `POST /api/mounts/{name}/unmount`, `GET /api/syncs/{name}/log` to `api.py`
- Added `mount_duration_seconds` tracking in `MountState` and `_remount()`
- Added `last_output` (last 50 rsync lines) capture in `SyncState`
- Added `get_usage()` to `mount.py` (disk usage via `os.statvfs`)
- Added passphrase support in `MountConfig` + `_add_key_to_agent()` in `mount.py`
- Added `log_file` to `DaemonConfig` with `RotatingFileHandler`
- Rewrote `main.py` CLI with subcommands: `start`, `status`, `mount`, `unmount`, `reload`
- Added PID file (`~/.config/sshfs-keeper/daemon.pid`) and SIGHUP config hot-reload
- Updated dashboard: SSE live-update indicator, usage bars on healthy mount cards, sync log modal, "📋 Log" button on sync cards
- Promoted `httpx` from dev dep to runtime dep in `pyproject.toml`
- Created `docs/index.md`, `FAQ.md`, `AUDIT.md`, `SUGGESTIONS.md`
- Added 20 new tests in `test/test_new_features.py`; 45/45 tests pass

**Oversight**: Human-approved plan; all changes reviewed via git diff before commit
