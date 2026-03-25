"""FastAPI application — status dashboard and control endpoints."""

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sshfs_keeper import metrics as _metrics_module
from sshfs_keeper.config import AppConfig, CONFIG_DIR, MountConfig, SyncConfig, KEYS_DIR
from sshfs_keeper.monitor import Monitor, MountState
from sshfs_keeper.sync import SyncManager, SyncState

TEMPLATES_DIR = Path(__file__).parent / "templates"
_VERSION = "0.1.0"

app = FastAPI(title="sshfs-keeper", version=_VERSION)
class _NoCache:
    """Drop-in replacement for Jinja2's LRUCache that disables caching.

    Newer Jinja2 includes template globals (a dict) in the cache key, making
    it unhashable and raising TypeError. Replacing the cache object avoids this.
    """

    def get(self, key: object) -> None:  # type: ignore[override]
        return None

    def __setitem__(self, key: object, value: object) -> None:
        pass


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.cache = _NoCache()  # type: ignore[assignment]

_monitor: Optional[Monitor] = None
_config: Optional[AppConfig] = None
_sync_manager: Optional[SyncManager] = None
_sse_queues: list[asyncio.Queue[Optional[dict[str, Any]]]] = []


def setup(monitor: Monitor, config: AppConfig, sync_manager: Optional[SyncManager] = None) -> None:
    """Wire global module-level singletons and register the SSE event listener.

    Args:
        monitor: Running :class:`~sshfs_keeper.monitor.Monitor` instance.
        config: Loaded :class:`~sshfs_keeper.config.AppConfig`.
        sync_manager: Optional running :class:`~sshfs_keeper.sync.SyncManager` instance.
    """
    global _monitor, _config, _sync_manager
    _monitor = monitor
    _config = config
    _sync_manager = sync_manager
    monitor.add_event_listener(_broadcast_event)


def _htmx_json(data: dict[str, Any], toast: Optional[str] = None, *, ok: bool = True) -> JSONResponse:
    """Return a JSON response with an optional ``HX-Trigger`` header for toast display.

    Args:
        data: JSON-serialisable response body.
        toast: Human-readable message shown in the dashboard toast notification.
        ok: ``True`` for success (green), ``False`` for error (red) styling.

    Returns:
        :class:`fastapi.responses.JSONResponse` with ``HX-Trigger`` header when *toast* is set.
    """
    headers: dict[str, str] = {}
    if toast:
        headers["HX-Trigger"] = json.dumps({"showToast": toast, "toastOk": ok})
    return JSONResponse(data, headers=headers)


def _broadcast_event(event: "dict[str, Any]") -> None:
    """Push *event* to all active SSE subscriber queues.

    Args:
        event: Dict payload with keys ``event``, ``mount``, ``status``, ``timestamp``.
    """
    for q in list(_sse_queues):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _get_monitor() -> Monitor:
    if _monitor is None:
        raise RuntimeError("Monitor not initialised")
    return _monitor


def _get_sync_manager() -> SyncManager:
    if _sync_manager is None:
        raise RuntimeError("SyncManager not initialised")
    return _sync_manager


def _get_config() -> AppConfig:
    if _config is None:
        raise RuntimeError("Config not initialised")
    return _config


def _check_api_key(request: Request) -> None:
    if _config and _config.api.api_key:
        key = request.headers.get("X-API-Key", "")
        if key != _config.api.api_key:
            raise HTTPException(status_code=401, detail="Unauthorized")


# ------------------------------------------------------------------
# Pydantic models for request bodies
# ------------------------------------------------------------------

class MountPayload(BaseModel):
    name: str
    remote: str
    local: str
    options: str = "cache=yes,compression=yes,ServerAliveInterval=15,ServerAliveCountMax=3,reconnect"
    identity: Optional[str] = None
    enabled: bool = True
    mount_tool: str = "sshfs"


class SyncPayload(BaseModel):
    name: str
    source: str
    target: str
    interval: int = 3600
    options: str = "-az --delete --stats"
    identity: Optional[str] = None
    enabled: bool = True
    sync_tool: str = "rsync"


class DaemonSettingsPayload(BaseModel):
    check_interval: Optional[int] = None
    remount_delay: Optional[int] = None
    max_retries: Optional[int] = None
    backoff_base: Optional[int] = None
    log_level: Optional[str] = None
    json_logs: Optional[bool] = None


class NotificationsPayload(BaseModel):
    webhook_url: Optional[str] = None
    on_failure: bool = True
    on_recovery: bool = True
    on_backoff: bool = False


# ------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    monitor = _get_monitor()
    cfg = _get_config()
    keys = _list_keys()
    sm = _sync_manager
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "mounts": monitor.get_snapshot(),
            "syncs": sm.get_snapshot() if sm else [],
            "daemon": cfg.daemon,
            "notifications": cfg.notifications,
            "now": time.time(),
            "keys": keys,
            "config_dir": str(CONFIG_DIR),
        },
    )


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------


@app.get("/version")
async def version_simple() -> dict:  # type: ignore[type-arg]
    """Quick version check for deployment verification."""
    return {"version": _VERSION}


@app.get("/api/version")
async def api_version() -> dict:  # type: ignore[type-arg]
    """Return the running daemon version and deployment info."""
    import subprocess as _sp
    import time as _time
    from pathlib import Path as _Path

    result = {
        "version": _VERSION,
        "timestamp": _time.time(),
    }

    # Get git commit hash if available
    try:
        repo_root = _Path(__file__).parent.parent.parent
        commit = _sp.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            stderr=_sp.DEVNULL,
            text=True,
        ).strip()[:8]
        result["commit"] = commit
    except Exception:
        pass

    # Get service start time from /proc
    try:
        pidfile = _Path.home() / ".config" / "sshfs-keeper" / "daemon.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            proc_stat = _Path(f"/proc/{pid}/stat").read_text().split()
            # Field 21 (0-indexed) is starttime in jiffies since boot
            starttime_jiffies = int(proc_stat[21])
            # Get clock ticks per second to convert
            import os as _os
            ticks_per_sec = _os.sysconf("SC_CLK_TCK")
            # Get boot time by reading /proc/stat
            boottime_s = 0
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime"):
                        boottime_s = int(line.split()[1])
                        break
            if boottime_s:
                start_s = boottime_s + (starttime_jiffies / ticks_per_sec)
                result["started_at"] = start_s
                result["uptime_seconds"] = int(_time.time() - start_s)
    except Exception:
        pass

    return result


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """Return Prometheus-format metrics (text/plain; version=0.0.4)."""
    monitor = _get_monitor()
    body = _metrics_module.generate(monitor, _sync_manager)
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4")


@app.get("/api/events")
async def sse_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time mount status changes."""
    queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(maxsize=100)
    _sse_queues.append(queue)

    async def _generate() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    if event is None:
                        break
                    # Emit both the generic event name AND a per-mount event
                    # so HTMX can target individual cards via sse:mount_update_{name}
                    sse_event_name = event.get("event", "mount_update")
                    mount_name = event.get("mount", "")
                    data = json.dumps(event)
                    yield f"event: {sse_event_name}\ndata: {data}\n\n"
                    if mount_name:
                        yield f"event: mount_update_{mount_name}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE comment keeps connection alive
        finally:
            _sse_queues.remove(queue)

    return StreamingResponse(_generate(), media_type="text/event-stream")


@app.get("/api/status")
async def api_status() -> dict:  # type: ignore[type-arg]
    monitor = _get_monitor()
    return {"mounts": monitor.get_snapshot(), "timestamp": time.time()}



@app.get("/health")
async def health_check() -> JSONResponse:
    """Return 200 if all enabled mounts are healthy, 503 otherwise.

    Useful as a load-balancer or monitoring probe endpoint.
    """
    monitor = _get_monitor()
    unhealthy = [
        s for s in monitor.states.values()
        if s.config.enabled and s.status.value not in ("healthy", "mounting", "disabled")
    ]
    if unhealthy:
        names = [s.config.name for s in unhealthy]
        return JSONResponse({"ok": False, "unhealthy": names}, status_code=503)
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------
# HTMX HTML fragment endpoints
# ------------------------------------------------------------------

@app.get("/fragments/mounts", response_class=HTMLResponse)
async def fragment_mounts(request: Request) -> HTMLResponse:
    """Return the mounts grid HTML for HTMX in-place swap.

    Used by the dashboard when an SSE event or user action triggers a refresh.
    """
    monitor = _get_monitor()
    return templates.TemplateResponse(
        request,
        "_mount_cards.html",
        {"mounts": monitor.get_snapshot(), "now": time.time(), "keys": _list_keys()},
    )


@app.get("/fragments/syncs", response_class=HTMLResponse)
async def fragment_syncs(request: Request) -> HTMLResponse:
    """Return the syncs grid HTML for HTMX in-place swap."""
    sm = _sync_manager
    return templates.TemplateResponse(
        request,
        "_sync_cards.html",
        {"syncs": sm.get_snapshot() if sm else [], "now": time.time()},
    )


@app.get("/fragments/mounts/{name}", response_class=HTMLResponse)
async def fragment_mount_card(name: str, request: Request) -> HTMLResponse:
    """Return a single mount card HTML for HTMX out-of-band swap.

    Args:
        name: Mount name.
    """
    monitor = _get_monitor()
    state = monitor.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    snapshot = monitor.get_snapshot()
    m = next((s for s in snapshot if s["name"] == name), None)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    return templates.TemplateResponse(
        request,
        "_mount_card.html",
        {"m": m, "now": time.time(), "keys": _list_keys()},
    )


@app.get("/fragments/syncs/{name}", response_class=HTMLResponse)
async def fragment_sync_card(name: str, request: Request) -> HTMLResponse:
    """Return a single sync card HTML for HTMX out-of-band swap.

    Args:
        name: Sync job name.
    """
    sm = _get_sync_manager()
    state = sm.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    snapshot = sm.get_snapshot()
    s = next((x for x in snapshot if x["name"] == name), None)
    if s is None:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    return templates.TemplateResponse(
        request,
        "_sync_card.html",
        {"s": s, "now": time.time()},
    )


@app.get("/fragments/keys", response_class=HTMLResponse)
async def fragment_keys(request: Request) -> HTMLResponse:
    """Return the SSH keys list HTML for HTMX in-place swap."""
    return templates.TemplateResponse(
        request,
        "_keys_list.html",
        {"keys": _list_keys()},
    )


# ------------------------------------------------------------------
# Mount CRUD
# ------------------------------------------------------------------

@app.post("/api/mounts")
async def api_add_mount(payload: MountPayload, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    monitor = _get_monitor()
    cfg = _get_config()

    if payload.name in monitor.states:
        raise HTTPException(status_code=409, detail=f"Mount '{payload.name}' already exists")

    mc = MountConfig(
        name=payload.name,
        remote=payload.remote,
        local=payload.local,
        options=payload.options,
        identity=payload.identity or None,
        enabled=payload.enabled,
        mount_tool=payload.mount_tool,
    )
    cfg.mounts.append(mc)
    monitor.states[mc.name] = MountState(config=mc)
    cfg.save()
    return _htmx_json({"name": mc.name, "created": True}, toast=f"{mc.name} added ✔")


@app.put("/api/mounts/{name}")
async def api_update_mount(name: str, payload: MountPayload, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    monitor = _get_monitor()
    cfg = _get_config()

    state = monitor.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")

    # Update in-memory config
    mc = state.config
    mc.remote = payload.remote
    mc.local = payload.local
    mc.options = payload.options
    mc.identity = payload.identity or None
    mc.enabled = payload.enabled
    mc.mount_tool = payload.mount_tool

    # Rename: update dict key and cfg list entry
    if payload.name != name:
        if payload.name in monitor.states:
            raise HTTPException(status_code=409, detail=f"Mount '{payload.name}' already exists")
        mc.name = payload.name
        monitor.states[payload.name] = monitor.states.pop(name)
        for i, m in enumerate(cfg.mounts):
            if m.name == name:
                cfg.mounts[i] = mc
                break
    cfg.save()
    return _htmx_json({"name": mc.name, "updated": True}, toast=f"{mc.name} updated ✔")


@app.delete("/api/mounts/{name}")
async def api_delete_mount(name: str, request: Request) -> JSONResponse:
    """Delete a mount config and return HX-Trigger toast header."""
    _check_api_key(request)
    monitor = _get_monitor()
    cfg = _get_config()

    if name not in monitor.states:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")

    monitor.states.pop(name)
    cfg.mounts = [m for m in cfg.mounts if m.name != name]
    cfg.save()
    return _htmx_json({"name": name, "deleted": True}, toast=f"{name} deleted")


@app.post("/api/mounts/{name}/remount")
async def api_remount(name: str, request: Request) -> JSONResponse:
    """Trigger a manual remount and return HX-Trigger toast header."""
    _check_api_key(request)
    monitor = _get_monitor()
    if name not in monitor.states:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    ok = await monitor.trigger_remount(name)
    msg = f"{name} remounted ✔" if ok else f"{name} remount failed"
    return _htmx_json({"name": name, "success": ok}, toast=msg, ok=ok)


@app.post("/api/mounts/{name}/unmount")
async def api_unmount(name: str, request: Request) -> dict:  # type: ignore[type-arg]
    """Force-unmount a named SSHFS mount."""
    _check_api_key(request)
    monitor = _get_monitor()
    state = monitor.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    from sshfs_keeper import mount as mnt_ops
    ok = await mnt_ops.unmount(state.config)
    return {"name": name, "success": ok}


@app.post("/api/mounts/{name}/enable")
async def api_enable(name: str, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    monitor = _get_monitor()
    state = monitor.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    state.config.enabled = True
    _get_config().save()
    return {"name": name, "enabled": True}


@app.post("/api/mounts/{name}/disable")
async def api_disable(name: str, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    monitor = _get_monitor()
    state = monitor.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    state.config.enabled = False
    _get_config().save()
    return {"name": name, "enabled": False}


@app.patch("/api/mounts/{name}/backend")
async def api_switch_backend(name: str, request: Request) -> JSONResponse:
    """Cycle the mount backend between sshfs and rclone.

    Each call toggles: sshfs → rclone → sshfs.

    Args:
        name: Mount name.
    """
    _check_api_key(request)
    monitor = _get_monitor()
    state = monitor.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Mount '{name}' not found")
    _cycle = {"sshfs": "rclone", "rclone": "sshfs"}
    state.config.mount_tool = _cycle.get(state.config.mount_tool, "sshfs")
    _get_config().save()
    return _htmx_json(
        {"name": name, "mount_tool": state.config.mount_tool},
        toast=f"{name} backend → {state.config.mount_tool} ✔",
    )


# ------------------------------------------------------------------
# Daemon settings
# ------------------------------------------------------------------

@app.put("/api/settings")
async def api_update_settings(payload: DaemonSettingsPayload, request: Request) -> JSONResponse:
    """Update daemon settings and return HX-Trigger toast header."""
    _check_api_key(request)
    cfg = _get_config()
    if payload.check_interval is not None:
        cfg.daemon.check_interval = payload.check_interval
    if payload.remount_delay is not None:
        cfg.daemon.remount_delay = payload.remount_delay
    if payload.max_retries is not None:
        cfg.daemon.max_retries = payload.max_retries
    if payload.backoff_base is not None:
        cfg.daemon.backoff_base = payload.backoff_base
    if payload.log_level is not None:
        cfg.daemon.log_level = payload.log_level
    if payload.json_logs is not None:
        cfg.daemon.json_logs = payload.json_logs
    cfg.save()
    return _htmx_json({"updated": True}, toast="Settings saved ✔")


@app.get("/api/notifications")
async def api_get_notifications() -> dict:  # type: ignore[type-arg]
    """Return current notification settings."""
    cfg = _get_config()
    n = cfg.notifications
    return {
        "webhook_url": n.webhook_url,
        "on_failure": n.on_failure,
        "on_recovery": n.on_recovery,
        "on_backoff": n.on_backoff,
    }


@app.put("/api/notifications")
async def api_update_notifications(payload: NotificationsPayload, request: Request) -> JSONResponse:
    """Update notification settings and return HX-Trigger toast header."""
    _check_api_key(request)
    cfg = _get_config()
    cfg.notifications.webhook_url = payload.webhook_url or None
    cfg.notifications.on_failure = payload.on_failure
    cfg.notifications.on_recovery = payload.on_recovery
    cfg.notifications.on_backoff = payload.on_backoff
    cfg.save()
    return _htmx_json({"updated": True}, toast="Notification settings saved ✔")


# ------------------------------------------------------------------
# SSH key management
# ------------------------------------------------------------------

def _list_keys() -> list[str]:
    if not KEYS_DIR.exists():
        return []
    return sorted(p.name for p in KEYS_DIR.iterdir() if p.is_file() and not p.name.endswith(".pub"))


@app.get("/api/keys")
async def api_list_keys() -> dict:  # type: ignore[type-arg]
    return {"keys": _list_keys()}


@app.post("/api/keys")
async def api_upload_key(request: Request, file: UploadFile = File(...)) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)

    name = Path(file.filename or "uploaded_key").name  # strip any path component
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid key filename")

    content = await file.read()

    # Basic sanity: must look like a PEM private key or OpenSSH key
    first_line = content.lstrip()[:40].decode("utf-8", errors="ignore")
    if "PRIVATE KEY" not in first_line and "OPENSSH" not in first_line:
        raise HTTPException(status_code=400, detail="File does not appear to be a private key")

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    dest = KEYS_DIR / name
    dest.write_bytes(content)
    # 600 — readable only by owner (root)
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)

    return {"name": name, "path": str(dest)}


@app.delete("/api/keys/{name}")
async def api_delete_key(name: str, request: Request) -> JSONResponse:
    """Delete an SSH key and return HX-Trigger toast header."""
    _check_api_key(request)
    name = Path(name).name  # strip path traversal
    dest = KEYS_DIR / name
    if not dest.exists():
        raise HTTPException(status_code=404, detail=f"Key '{name}' not found")
    dest.unlink()
    return _htmx_json({"name": name, "deleted": True}, toast=f'Key "{name}" deleted')


# ------------------------------------------------------------------
# Sync jobs
# ------------------------------------------------------------------

@app.get("/api/syncs")
async def api_list_syncs() -> dict:  # type: ignore[type-arg]
    return {"syncs": _get_sync_manager().get_snapshot()}


@app.post("/api/syncs")
async def api_add_sync(payload: SyncPayload, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    sm = _get_sync_manager()
    cfg = _get_config()
    if payload.name in sm.states:
        raise HTTPException(status_code=409, detail=f"Sync '{payload.name}' already exists")
    sc = SyncConfig(
        name=payload.name, source=payload.source, target=payload.target,
        interval=payload.interval, options=payload.options,
        identity=payload.identity or None, enabled=payload.enabled,
        sync_tool=payload.sync_tool,
    )
    cfg.syncs.append(sc)
    sm.states[sc.name] = SyncState(config=sc)
    cfg.save()
    return _htmx_json({"name": sc.name, "created": True}, toast=f"{sc.name} added ✔")


@app.put("/api/syncs/{name}")
async def api_update_sync(name: str, payload: SyncPayload, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    sm = _get_sync_manager()
    cfg = _get_config()
    state = sm.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    sc = state.config
    sc.source = payload.source
    sc.target = payload.target
    sc.interval = payload.interval
    sc.options = payload.options
    sc.identity = payload.identity or None
    sc.enabled = payload.enabled
    sc.sync_tool = payload.sync_tool
    if payload.name != name:
        if payload.name in sm.states:
            raise HTTPException(status_code=409, detail=f"Sync '{payload.name}' already exists")
        sc.name = payload.name
        sm.states[payload.name] = sm.states.pop(name)
        for i, s in enumerate(cfg.syncs):
            if s.name == name:
                cfg.syncs[i] = sc
                break
    cfg.save()
    return _htmx_json({"name": sc.name, "updated": True}, toast=f"{sc.name} updated ✔")


@app.delete("/api/syncs/{name}")
async def api_delete_sync(name: str, request: Request) -> JSONResponse:
    """Delete a sync job config and return HX-Trigger toast header."""
    _check_api_key(request)
    sm = _get_sync_manager()
    cfg = _get_config()
    if name not in sm.states:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    sm.states.pop(name)
    cfg.syncs = [s for s in cfg.syncs if s.name != name]
    cfg.save()
    return _htmx_json({"name": name, "deleted": True}, toast=f"{name} deleted")


@app.post("/api/syncs/{name}/trigger")
async def api_trigger_sync(name: str, request: Request) -> JSONResponse:
    """Trigger a sync job immediately and return HX-Trigger toast header."""
    _check_api_key(request)
    ok = await _get_sync_manager().trigger(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    return _htmx_json({"name": name, "triggered": True}, toast=f"{name} started ✔")


@app.get("/api/syncs/{name}/log")
async def api_sync_log(name: str, request: Request) -> Any:
    """Return rsync output for a sync job.

    Returns HTML fragment when called from HTMX (``HX-Request`` header present),
    otherwise returns JSON for API clients.

    Args:
        name: Sync job name.
        request: Incoming HTTP request (used to detect HTMX).
    """
    sm = _get_sync_manager()
    state = sm.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    if request.headers.get("HX-Request"):
        lines = state.last_output
        content = "\n".join(lines) if lines else "(no output yet)"
        return HTMLResponse(content)
    return {"name": name, "lines": state.last_output}


@app.post("/api/syncs/{name}/enable")
async def api_enable_sync(name: str, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    sm = _get_sync_manager()
    state = sm.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    state.config.enabled = True
    _get_config().save()
    return {"name": name, "enabled": True}


@app.post("/api/syncs/{name}/disable")
async def api_disable_sync(name: str, request: Request) -> dict:  # type: ignore[type-arg]
    _check_api_key(request)
    sm = _get_sync_manager()
    state = sm.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Sync '{name}' not found")
    state.config.enabled = False
    _get_config().save()
    return {"name": name, "enabled": False}
