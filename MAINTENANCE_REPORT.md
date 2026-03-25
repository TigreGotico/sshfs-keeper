# Maintenance Report

## 2026-03-25 — Sync progress tracking and config wipe protection

**AI Model**: claude-sonnet-4-6
**Actions Taken**:
- **Hook fix**: Updated PostToolUse hook to stop service before install/reinstall, preventing config corruption when service writes during package installation
- **Config protection**: `save()` now refuses to write 0 mounts when on-disk config has mounts (logs ERROR with stack trace)
- **Config auto-restore**: `load()` automatically restores from `.toml.bak` if config is empty but backup has mounts (logs WARNING)
- **Sync progress tracking**: Added `last_progress`, `progress_pct`, `started_at` to `SyncState`; streams rsync/rclone output in real-time instead of buffering
- **Progress UI**: Sync cards now show live progress bar (0-100%), elapsed time, and last progress line when running
- **Auto-polling**: Syncs grid polls every 3s when Sync tab is active (HTMX `every` trigger with guard)
- **Transport fix**: Re-applied direct SSH transport for remote rsync/lsyncd jobs (was lost in earlier hook reinstall)
- **Progress flags**: Added `--progress` to rsync and rclone commands for live output emission
- **Test helpers**: Created `_make_stream_mock()` and `_make_proc_mock()` for proper async stream mocking in tests
- **Build cleanup**: Added `build/` to `.gitignore`; hook now cleans build artifacts before install

**Oversight**: User reports mount loss on restart was root-caused to hook corruption; fix prevents service from writing config during package install. Config auto-restore adds recovery layer. All critical sync/mount tests pass.

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
