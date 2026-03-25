---
name: sshfs-keeper-api
description: >
  Interact with a running sshfs-keeper daemon via its REST API using curl.
  Use when the user asks to check mount status, add/remove/remount mounts,
  manage sync jobs, or configure notifications on a sshfs-keeper instance.
triggers:
  - sshfs-keeper
  - sshfs keeper
  - mount status
  - remount
  - sshfs api
---

# sshfs-keeper API — Agent Quick Reference

## Defaults

```
BASE=http://localhost:8765
# If api_key is set in config:
AUTH="-H 'X-API-Key: YOUR_KEY'"
```

Read endpoints (GET) never require auth. Write endpoints (POST/PUT/DELETE/PATCH) require `X-API-Key` only when `api.api_key` is configured.

---

## Health & status

```bash
# Is the daemon healthy? (200 = all mounts up, 503 = something down)
curl -s $BASE/health

# Full mount snapshot (status, errors, disk usage, retry counts)
curl -s $BASE/api/status | python3 -m json.tool

# Daemon version
curl -s $BASE/api/version

# Prometheus metrics (plain text)
curl -s $BASE/metrics

# Live event stream (SSE — Ctrl-C to stop)
curl -sN $BASE/api/events
```

**Mount status values:** `healthy` `unmounted` `stale` `mounting` `disabled` `error`

---

## Mount operations

```bash
# List all mounts and their state
curl -s $BASE/api/status | python3 -c "
import sys,json
for m in json.load(sys.stdin)['mounts']:
    print(m['name'], m['status'], m.get('last_error') or '')
"

# Trigger immediate remount (resets backoff + retry counter)
curl -s -X POST $BASE/api/mounts/NAME/remount

# Force unmount
curl -s -X POST $BASE/api/mounts/NAME/unmount

# Pause monitoring (won't remount, won't alert)
curl -s -X POST $BASE/api/mounts/NAME/disable

# Resume monitoring
curl -s -X POST $BASE/api/mounts/NAME/enable

# Toggle backend sshfs ↔ rclone
curl -s -X PATCH $BASE/api/mounts/NAME/backend
```

---

## Add a mount

```bash
curl -s -X POST $BASE/api/mounts \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "nas",
    "remote": "user@192.168.1.10:/media/data",
    "local": "/mnt/nas",
    "options": "cache=yes,compression=yes,ServerAliveInterval=15,ServerAliveCountMax=3,reconnect",
    "identity": "/home/user/.config/sshfs-keeper/keys/id_ed25519",
    "mount_tool": "sshfs",
    "enabled": true
  }'
```

**FTP via rclone:**
```bash
-d '{"name":"ftp-server","remote":":ftp,host=HOST,user=USER,pass=PASS:/path","local":"/mnt/ftp","mount_tool":"rclone","enabled":true}'
```

**WebDAV via rclone:**
```bash
-d '{"name":"nextcloud","remote":":webdav,url=http://HOST/dav,user=USER,pass=PASS:","local":"/mnt/nc","mount_tool":"rclone","enabled":true}'
```

**SMB via rclone:**
```bash
-d '{"name":"winshare","remote":":smb,host=HOST,user=USER,pass=PASS:/share","local":"/mnt/smb","mount_tool":"rclone","enabled":true}'
```

---

## Update / rename a mount

```bash
curl -s -X PUT $BASE/api/mounts/OLD_NAME \
  -H 'Content-Type: application/json' \
  -d '{"name":"new-name","remote":"user@host:/path","local":"/mnt/new","options":"reconnect","enabled":true,"mount_tool":"sshfs"}'
```

All fields required in PUT body (same schema as POST).

---

## Delete a mount

```bash
# Removes from config — does NOT unmount the filesystem
curl -s -X DELETE $BASE/api/mounts/NAME

# To unmount first:
curl -s -X POST $BASE/api/mounts/NAME/unmount
curl -s -X DELETE $BASE/api/mounts/NAME
```

---

## Sync jobs

```bash
# List sync jobs
curl -s $BASE/api/syncs | python3 -m json.tool

# Trigger a sync job immediately
curl -s -X POST $BASE/api/syncs/JOBNAME/trigger

# View last sync output (last 50 lines of rsync/rclone stdout)
curl -s $BASE/api/syncs/JOBNAME/log

# Add a sync job
curl -s -X POST $BASE/api/syncs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "photos-backup",
    "source": "/mnt/nas/photos/",
    "target": "/mnt/backup/photos/",
    "interval": 3600,
    "options": "-az --delete --stats",
    "sync_tool": "rsync",
    "enabled": true
  }'

# Enable / disable
curl -s -X POST $BASE/api/syncs/JOBNAME/enable
curl -s -X POST $BASE/api/syncs/JOBNAME/disable

# Delete
curl -s -X DELETE $BASE/api/syncs/JOBNAME
```

**Sync status values:** `idle` `running` `ok` `failed` `disabled`

---

## Daemon settings

```bash
# Read current settings (embedded in /api/status response under daemon key)
# Update settings (all fields optional):
curl -s -X PUT $BASE/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"check_interval":30,"remount_delay":5,"max_retries":3,"backoff_base":60,"log_level":"INFO","json_logs":false}'
```

---

## Notifications (webhooks)

```bash
# Read
curl -s $BASE/api/notifications

# Configure (ntfy.sh example)
curl -s -X PUT $BASE/api/notifications \
  -H 'Content-Type: application/json' \
  -d '{"webhook_url":"https://ntfy.sh/my-topic","on_failure":true,"on_recovery":true,"on_backoff":false}'

# Disable
curl -s -X PUT $BASE/api/notifications \
  -H 'Content-Type: application/json' \
  -d '{"webhook_url":null,"on_failure":true,"on_recovery":true,"on_backoff":false}'
```

---

## File transfers

```bash
# Start a transfer (rsync over SSH)
curl -s -X POST $BASE/api/transfers \
  -H 'Content-Type: application/json' \
  -d '{
    "protocol": "rsync_ssh",
    "source": "user@host:/data/archive.tar.gz",
    "dest": "/local/archive.tar.gz"
  }'

# Start a move (deletes source after copy)
curl -s -X POST $BASE/api/transfers \
  -H 'Content-Type: application/json' \
  -d '{"protocol":"rsync_ssh","source":"/local/old/","dest":"user@host:/archive/","move":true}'

# List all transfers (newest first)
curl -s $BASE/api/transfers | python3 -m json.tool

# View transfer output/log
curl -s $BASE/api/transfers/TRANSFER_ID/log

# Cancel a running transfer
curl -s -X DELETE $BASE/api/transfers/TRANSFER_ID

# Resume a failed/cancelled rsync transfer (uses --partial --append-verify)
curl -s -X POST $BASE/api/transfers/TRANSFER_ID/resume
```

**Protocol values:** `rsync_ssh` (recommended), `scp`, `rclone`, `local` (rsync between local paths)
**Transfer status values:** `running` `done` `failed` `cancelled`

---

## File browser

```bash
# Browse local directory
curl -s '$BASE/api/browse?path=/home/user' | python3 -m json.tool

# Browse remote directory via SSH
curl -s '$BASE/api/browse?path=/data&host=user@server&identity=mykey'
```

Response: `{"path": "/data", "entries": [{"name": "subdir/", "is_dir": true, "size": null}, {"name": "file.txt", "is_dir": false, "size": 1024}]}`

---

## Logs

```bash
# Recent daemon logs (default 300 lines)
curl -s '$BASE/api/logs?lines=100'

# Live tail (SSE stream — Ctrl-C to stop)
curl -sN $BASE/api/logs/stream
```

---

## SSH key management

```bash
# List stored keys
curl -s $BASE/api/keys

# Upload a private key
curl -s -X POST $BASE/api/keys \
  -F "file=@/path/to/id_ed25519"

# Delete a key
curl -s -X DELETE $BASE/api/keys/id_ed25519
```

Keys are stored at `~/.config/sshfs-keeper/keys/` (mode 600). Reference them in mount configs as the full path.

---

## Common patterns

**Check if a specific mount is healthy:**
```bash
curl -s $BASE/api/status | python3 -c "
import sys,json
m = next(m for m in json.load(sys.stdin)['mounts'] if m['name']=='NAME')
print(m['status'], m.get('last_error') or 'no error')
"
```

**Remount all unhealthy mounts:**
```bash
curl -s $BASE/api/status | python3 -c "
import sys,json,subprocess
for m in json.load(sys.stdin)['mounts']:
    if m['status'] not in ('healthy','disabled','mounting'):
        subprocess.run(['curl','-s','-X','POST',f'$BASE/api/mounts/{m[\"name\"]}/remount'])
        print('remounted', m['name'])
"
```

**Wait for a mount to become healthy (polling):**
```bash
while true; do
  status=$(curl -s $BASE/api/status | python3 -c "import sys,json; print(next(m['status'] for m in json.load(sys.stdin)['mounts'] if m['name']=='NAME'))")
  echo "$(date): $status"
  [ "$status" = "healthy" ] && break
  sleep 5
done
```

**Bulk-add mounts from a shell array:**
```bash
declare -A MOUNTS=(
  ["nas"]="user@192.168.1.10:/media/nas /mnt/nas"
  ["backup"]="user@192.168.1.10:/media/backup /mnt/backup"
)
for name in "${!MOUNTS[@]}"; do
  read remote local <<< "${MOUNTS[$name]}"
  curl -s -X POST $BASE/api/mounts \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\",\"remote\":\"$remote\",\"local\":\"$local\",\"enabled\":true,\"mount_tool\":\"sshfs\"}"
done
```

---

## Error responses

| HTTP | Meaning |
|------|---------|
| 400 | Invalid request (bad protocol, public key upload, etc.) |
| 401 | Missing or wrong `X-API-Key` |
| 403 | Permission denied (file browser) |
| 404 | Mount/sync/transfer not found |
| 409 | Name already exists (duplicate) |
| 502 | SSH error (remote file browse failed) |
| 503 | `/health` — one or more mounts unhealthy |
| 504 | SSH timeout (remote browse) |

All errors return `{"detail": "message"}`.
