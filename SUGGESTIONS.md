# SUGGESTIONS — sshfs-keeper

## SUG-001 — Retry logic for sync jobs
Add exponential backoff to `SyncManager._run_job()` mirroring the mount retry logic. A failed rsync should retry at `backoff_base * 2^n` seconds rather than waiting the full `interval`.

## SUG-002 — Keyring integration for passphrases
Replace plaintext `identity_passphrase` in config.toml with a reference to a system keyring entry (e.g. `secretstorage` on Linux, `keychain` on macOS). Avoids passphrase exposure.

## SUG-003 — Partial page refresh via HTMX
Instead of a full page reload on SSE events, use HTMX `hx-swap` to update only the affected mount card. Would make the UI feel snappier and avoid losing scroll position.

## SUG-004 — Bandwidth limiting for rsync
Expose `--bwlimit=KBPS` as a per-sync config option. Prevents sync jobs saturating the SSHFS connection.

## SUG-005 — Structured JSON logging
Replace the current plain-text log format with structured JSON logging (e.g. using `python-json-logger`). Enables log aggregation with Loki, Elastic, etc.
