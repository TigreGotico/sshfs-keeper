# AUDIT — sshfs-keeper

## Known issues / technical debt

### AUDIT-001 — `main.py` async entry point untested
`sshfs_keeper/main.py:103-155` — `_run()`, `_cmd_start()`, and `_cmd_install_service()` are not covered by unit tests. Subcommand helpers (`_cmd_status`, `_cmd_mount`, `_cmd_unmount`, `_cmd_reload`, `_cmd_syncs`) are now tested (64% coverage).
- **Impact**: Regressions in daemon startup won't be caught
- **Mitigation**: `install-service` requires mocking `platform.system` + filesystem; `_cmd_start` requires an event loop and mock uvicorn

### AUDIT-002 — SSE reconnect on network failure
`sshfs_keeper/api.py:178-199` — SSE stream drops silently if the server restarts. The client reconnects via browser `EventSource` retry logic (browser-dependent; typically 3s).
- **Impact**: Brief reconnect gap; UI may be stale for up to 5s

### AUDIT-003 — Passphrase stored in plaintext
`sshfs_keeper/config.py` — `identity_passphrase` is stored in `config.toml` in plaintext. No encryption or keyring integration.
- **Impact**: Passphrase exposed to any process that can read `config.toml`
- **Mitigation**: Install scripts set config.toml to mode 600

### AUDIT-004 — ~~No retry logic for sync jobs~~ FIXED
Exponential backoff added to `SyncManager._run_job()` — `sshfs_keeper/sync.py:302-314`.
After `fail_count >= max_retries` the next run is scheduled at `backoff_base * 2^(fail_count - max_retries)` seconds.

### AUDIT-005 — ~~Config save drops notifications without webhook~~ FIXED
`[notifications]` block (including `on_failure`, `on_recovery`, `on_backoff`) is always written by `config.save()` regardless of `webhook_url` — `sshfs_keeper/config.py:127-132`.

### AUDIT-006 — Duplicate `GET /api/syncs` route (FastAPI silently shadowed)
`sshfs_keeper/api.py` previously had two `@app.get("/api/syncs")` registrations; FastAPI silently served the first. FIXED: the orphan route returning a bare list has been removed; the canonical route returning `{"syncs": [...]}` remains at line ~569.
