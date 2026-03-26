# Maintenance Report

## 2026-03-26 — Host entities and config auto-migration

**AI Model**: claude-haiku-4-5
**Actions Taken**:
- **Added HostConfig dataclass** (`sshfs_keeper/config.py`): Structured SSH host definition with `name`, `hostname`, `user`, `port`, `identity` fields
- **Extended AppConfig**: Added `hosts: list[HostConfig]` to centralize host definitions
- **Extended MountConfig/SyncConfig**: Added `host_name`, `path` (and `source_host/source_path/target_host/target_path` for syncs) fields to reference hosts instead of embedding `user@host` in free-text strings
- **Implemented config auto-migration** (`AppConfig._migrate_to_hosts()`): When loading old configs without explicit hosts, the loader parses free-text `remote`/`source`/`target` strings, creates `HostConfig` entries for unique hosts, and updates mounts/syncs to reference them — transparent to users
- **Updated save() method**: TOML serialization now writes `[[host]]` sections before mounts/syncs; preserves `remote`/`source`/`target` strings for readability
- **API changes**: Updated `HostPayload`, `MountPayload`, `SyncPayload` models; added endpoints for host CRUD (`GET/POST /api/hosts`, `PUT/DELETE /api/hosts/{name}`)
- **File browsing**: Added remote file browser endpoints (`GET /api/hosts/{name}/browse?path=/` for SSH, `GET /api/browse?path=/` for local)
- **Frontend refactor**: Mount/sync/transfer modals replaced free-text remote inputs with host dropdown + path input pairs; added file browser modal for path selection
- **Test coverage**: Added 6 new tests in `test/test_config.py` verifying migration logic (single host parse, multi-host reuse, end-to-end load of old config file)
- **Updated FAQ**: Added "How do I define remote hosts explicitly?" and "What happens to my old config when I upgrade?" sections documenting host entities and auto-migration

**Result**: Users can define hosts once and reuse across mounts/syncs; web UI provides structured host selection + file browser instead of manual `user@host:/path` entry. Existing configs auto-migrate transparently on load with no user action required.

**Oversight**: All 158 tests pass (6 new migration tests + 152 prior tests); end-to-end migration tested via `test_migrate_load_old_config_file` loading a pre-migration config and verifying host creation and mount/sync reference updates.

## 2026-03-26 — Transfers feature restoration and verification

**AI Model**: claude-haiku-4-5
**Actions Taken**:
- **Restored Transfers tab**: Retrieved full one-shot file transfer UI from earlier implementation
- **Restored Transfer API endpoints**: All transfer endpoints re-integrated (`GET /fragments/transfers`, `GET /api/transfers`, `POST /api/transfers`, `GET /api/transfers/{tid}/log`, `DELETE /api/transfers/{tid}`)
- **TransferManager integration**: Verified TransferManager initialization in `main.py` and proper setup in `api_module.setup()`
- **Protocol support**: Verified support for rsync (SSH), local copy/move, rclone, and SCP protocols
- **Test coverage**: All 152 tests passing, including 18 transfer-specific tests
- **UI completeness**: Dashboard displays Transfers tab with form for copy/move operations and history table

**Result**: Users can now perform one-shot file transfers between hosts using their preferred protocol with progress tracking and error display.

## 2026-03-26 — Sync test feature and error display

**AI Model**: claude-haiku-4-5
**Actions Taken**:
- **Added /api/syncs/test endpoint**: Tests sync configurations with dry-run rsync/rclone commands
- **Test button in modal**: Added 🧪 Test button to sync modal for pre-validation
- **Error display**: Shows detailed error messages when test fails
- **Success indicator**: Shows green success message when test passes
- **Form data collection**: JavaScript properly collects all form fields including targets array

**Result**: Users can now validate sync configurations before saving; helpful for debugging SSH key issues, path problems, and permission errors.

## 2026-03-26 — Fix sync edit form identity field handling

**AI Model**: claude-haiku-4-5
**Actions Taken**:
- **Fixed sync edit form**: Updated `openSyncModal()` to accept and preserve `identity` parameter when editing existing syncs
- **Updated sync card**: Modified `_sync_card.html` Edit button to pass `s.identity` value when opening the modal
- **Result**: Syncs with SSH key identity settings now retain their configuration when edited; 422 validation errors resolved

**Oversight**: All 152 tests pass; syncs can now be created and edited without losing SSH key identity settings.

## 2026-03-25 — Multi-target sync implementation (continued)

**AI Model**: claude-haiku-4-5
**Actions Taken**:
- **Dual SyncConfig issue**: Discovered two `SyncConfig` class definitions (in `config.py` and `sync.py`). API was importing from `config.py` which lacked the `targets` field despite the fix being in `sync.py`.
- **Fixed config.py**: Added `targets: list[str] = field(default_factory=list)` to `SyncConfig` dataclass in `config.py` to match `sync.py`.
- **Fixed TOML serialization**: Updated `save()` method to serialize `targets` array to TOML format when non-empty.
- **Fixed API response**: Added `targets` field to `get_snapshot()` so API responses expose the multi-target destinations.
- **Verified multi-target**: Tested creating sync jobs with multiple targets via `POST /api/syncs`; all targets are properly stored and returned.
- **Updated FAQ**: Added "How do I sync to multiple backup destinations?" section documenting the `targets` config field and UI workflow.
- **Cleanup**: Removed test sync jobs (`test_multi_target`, `test_multi_target2`).

**Oversight**: Root cause analysis identified the dual SyncConfig definitions; fix was incremental (config.py field → TOML serialization → snapshot response). Multi-target feature now end-to-end tested via API and dashboard.

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
