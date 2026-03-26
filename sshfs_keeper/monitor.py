"""Background monitoring loop — checks mounts, remounts as needed."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from sshfs_keeper.config import AppConfig, MountConfig
from sshfs_keeper import mount as mnt

log = logging.getLogger(__name__)


class MountStatus(str, Enum):
    HEALTHY = "healthy"
    UNMOUNTED = "unmounted"
    STALE = "stale"
    MOUNTING = "mounting"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass
class MountState:
    config: MountConfig
    status: MountStatus = MountStatus.UNMOUNTED
    last_check: Optional[float] = None
    last_mounted: Optional[float] = None
    last_error: Optional[str] = None
    retry_count: int = 0
    backoff_until: float = 0.0
    mount_count: int = 0
    mount_duration_seconds: Optional[float] = None
    """Duration of the last successful mount operation in seconds."""


class Monitor:
    """Async background service that keeps SSHFS mounts alive."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        self.states: dict[str, MountState] = {
            m.name: MountState(config=m) for m in config.mounts
        }
        self._running = False
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []
        """Registered SSE listeners; each receives a dict event payload."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the monitoring loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="sshfs-monitor")
        log.info("Monitor started — watching %d mount(s)", len(self.states))

    async def stop(self) -> None:
        """Gracefully stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Monitor stopped")

    def add_event_listener(self, listener: "Callable[[dict[str, Any]], None]") -> None:
        """Register a callback invoked on every mount status-change event.

        Args:
            listener: Callable receiving a dict with keys ``event``, ``mount``,
                ``status``, and ``timestamp``.
        """
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener: "Callable[[dict[str, Any]], None]") -> None:
        """Unregister a previously added listener.

        Args:
            listener: The callable to remove.
        """
        try:
            self._event_listeners.remove(listener)
        except ValueError:
            pass

    async def trigger_remount(self, name: str) -> bool:
        """Manually trigger a remount for a named mount. Returns success."""
        state = self.states.get(name)
        if state is None:
            return False
        state.backoff_until = 0.0
        state.retry_count = 0
        return await self._remount(state)

    def get_snapshot(self) -> list[dict]:  # type: ignore[type-arg]
        """Return a JSON-serialisable snapshot of all mount states."""
        now = time.time()
        out = []
        for state in self.states.values():
            usage = None
            if state.status == MountStatus.HEALTHY:
                usage = mnt.get_usage(state.config.local)
            out.append(
                {
                    "name": state.config.name,
                    "remote": state.config.remote,
                    "local": state.config.local,
                    "enabled": state.config.enabled,
                    "mount_tool": state.config.mount_tool,
                    "host_name": state.config.host_name,
                    "path": state.config.path,
                    "identity": state.config.identity,
                    "status": state.status.value,
                    "last_check": state.last_check,
                    "last_mounted": state.last_mounted,
                    "last_error": state.last_error,
                    "retry_count": state.retry_count,
                    "mount_count": state.mount_count,
                    "backoff_remaining": max(0.0, state.backoff_until - now),
                    "mount_duration_seconds": state.mount_duration_seconds,
                    "usage": usage,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            await self._check_all()
            await asyncio.sleep(self._cfg.daemon.check_interval)

    async def _check_all(self) -> None:
        tasks = [self._check_one(state) for state in self.states.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_one(self, state: MountState) -> None:
        cfg = state.config
        now = time.time()
        state.last_check = now

        if not cfg.enabled:
            state.status = MountStatus.DISABLED
            return

        # If autofs manages this path, skip self-remount to avoid conflicts
        if mnt.is_autofs_managed(cfg.local):
            log.debug("[%s] autofs-managed — skipping self-remount", cfg.name)
            state.status = MountStatus.HEALTHY
            return

        prev_status = state.status
        healthy = await mnt.is_healthy(cfg)
        if healthy:
            state.status = MountStatus.HEALTHY
            state.retry_count = 0
            state.backoff_until = 0.0
            if prev_status != MountStatus.HEALTHY:
                self._emit("mount_healthy", state)
            return

        # Determine whether it is unmounted (clean) or stale
        from sshfs_keeper.mount import _parse_proc_mounts
        if cfg.local in _parse_proc_mounts():
            state.status = MountStatus.STALE
            log.warning("[%s] stale mount detected — will force-unmount", cfg.name)
            await mnt.unmount(cfg)
        else:
            state.status = MountStatus.UNMOUNTED

        # Respect backoff
        if now < state.backoff_until:
            remaining = state.backoff_until - now
            log.debug("[%s] in backoff, %.0fs remaining", cfg.name, remaining)
            return

        await asyncio.sleep(self._cfg.daemon.remount_delay)
        await self._remount(state)

    def _emit(self, event: str, state: MountState) -> None:
        """Deliver a status-change event to all registered listeners.

        Args:
            event: Event type string (e.g. ``"mount_healthy"``).
            state: Current mount state.
        """
        if not self._event_listeners:
            return
        payload: dict[str, Any] = {
            "event": event,
            "mount": state.config.name,
            "status": state.status.value,
            "timestamp": time.time(),
        }
        for cb in list(self._event_listeners):
            try:
                cb(payload)
            except Exception:  # pragma: no cover
                pass

    async def _remount(self, state: MountState) -> bool:
        cfg = state.config
        state.status = MountStatus.MOUNTING
        t0 = time.time()
        ok, mount_err = await mnt.mount(cfg)
        duration = time.time() - t0

        if ok:
            state.status = MountStatus.HEALTHY
            state.last_mounted = time.time()
            state.mount_count += 1
            state.retry_count = 0
            state.backoff_until = 0.0
            state.last_error = None
            state.mount_duration_seconds = duration
            self._emit("mount_healthy", state)
            await self._send_notification("recovery", cfg.name)
            return True

        state.retry_count += 1
        state.last_error = mount_err or f"Mount failed (attempt {state.retry_count})"
        state.status = MountStatus.ERROR
        self._emit("mount_error", state)
        await self._send_notification("failure", cfg.name, state.last_error)

        if state.retry_count >= self._cfg.daemon.max_retries:
            backoff = self._cfg.daemon.backoff_base * (2 ** (state.retry_count - self._cfg.daemon.max_retries))
            state.backoff_until = time.time() + backoff
            log.warning(
                "[%s] %d failed attempts; backing off for %ds",
                cfg.name,
                state.retry_count,
                backoff,
            )
            await self._send_notification("backoff", cfg.name, f"Backing off for {backoff}s")
        return False

    async def _send_notification(self, event: str, mount: str, error: Optional[str] = None) -> None:
        """Fire a webhook notification via :mod:`sshfs_keeper.notify`.

        Args:
            event: One of ``"failure"``, ``"recovery"``, or ``"backoff"``.
            mount: Mount name.
            error: Optional error detail.
        """
        from sshfs_keeper import notify as _notify  # deferred to avoid circular import
        n = self._cfg.notifications
        await _notify.notify(
            webhook_url=n.webhook_url,
            on_failure=n.on_failure,
            on_recovery=n.on_recovery,
            on_backoff=n.on_backoff,
            event=event,
            mount=mount,
            error=error,
        )
