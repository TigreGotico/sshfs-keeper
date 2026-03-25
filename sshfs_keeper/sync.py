"""Directory mirroring — runs rsync or lsyncd jobs on a per-job interval."""

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sshfs_keeper.config import DaemonConfig

log = logging.getLogger(__name__)

# Rsync exit codes that mean "success with warnings" (partial transfer etc.)
_SOFT_EXIT_CODES = {0, 24}  # 24 = vanished source files (non-fatal)


class SyncStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class SyncConfig:
    name: str
    source: str          # local path or user@host:/path
    target: str          # local path or user@host:/path
    interval: int = 3600 # seconds between runs
    options: str = "-az --delete --stats"
    identity: Optional[str] = None
    enabled: bool = True
    sync_tool: str = "rsync"  # "rsync" | "lsyncd"


_MAX_OUTPUT_LINES = 50


@dataclass
class SyncState:
    config: SyncConfig
    status: SyncStatus = SyncStatus.IDLE
    last_run: Optional[float] = None
    last_duration: Optional[float] = None
    last_error: Optional[str] = None
    bytes_sent: Optional[int] = None
    files_transferred: Optional[int] = None
    run_count: int = 0
    fail_count: int = 0
    """Consecutive failure count; resets to 0 on success."""
    last_output: list[str] = field(default_factory=list, repr=False)
    """Last ``_MAX_OUTPUT_LINES`` lines of rsync stdout+stderr from the most recent run."""
    _next_run: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_REMOTE_RE = re.compile(r"^[^/][^:]*:")


def _is_remote(path: str) -> bool:
    """Return True if *path* looks like a remote rsync path (user@host:/…)."""
    return bool(_REMOTE_RE.match(path))


def _build_rsync_cmd(cfg: "SyncConfig") -> list[str]:  # type: ignore[name-defined]
    """Build the rsync command list for *cfg*.

    Automatically injects ``-e ssh`` with BatchMode and StrictHostKeyChecking
    when either source or target is a remote path, so rsync always transfers
    directly over SSH rather than going through the FUSE mount.

    Args:
        cfg: Sync job configuration.

    Returns:
        Argument list suitable for :func:`asyncio.create_subprocess_exec`.
    """
    cmd = ["rsync"] + cfg.options.split()
    if _is_remote(cfg.source) or _is_remote(cfg.target):
        ssh = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        if cfg.identity:
            ssh += f" -i {cfg.identity}"
        cmd += ["-e", ssh]
    cmd += [cfg.source, cfg.target]
    return cmd


def _build_lsyncd_cmd(cfg: "SyncConfig") -> tuple[list[str], str]:  # type: ignore[name-defined]
    """Build an lsyncd command and a temporary Lua config path for *cfg*.

    Writes a minimal lsyncd Lua config to a temp file and returns the command
    list together with the temp file path (caller is responsible for deletion).

    lsyncd is invoked with ``--oneshot`` so it performs a one-time sync and
    exits — matching the interval-based scheduler used for rsync jobs.

    Args:
        cfg: Sync job configuration.

    Returns:
        Tuple of (command list, tmp file path).
    """
    # Build ssh options string for lsyncd
    ssh_opts = "-o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if cfg.identity:
        ssh_opts += f" -i {cfg.identity}"

    # Determine whether target is remote (user@host:/path) or local
    import re as _re
    remote_m = _re.match(r"^(?:([^@]+)@)?([^:]+):(.+)$", cfg.target)
    if remote_m:
        user_host = (f"{remote_m.group(1)}@" if remote_m.group(1) else "") + remote_m.group(2)
        remote_path = remote_m.group(3)
        sync_block = (
            "sync{\n"
            "  default.rsyncssh,\n"
            f'  source = "{cfg.source}",\n'
            f'  host = "{user_host}",\n'
            f'  targetdir = "{remote_path}",\n'
            f'  rsync = {{archive=true, compress=true}},\n'
            f'  ssh = {{options = "{ssh_opts}"}},\n'
            "}"
        )
    else:
        # Local-to-local
        sync_block = (
            "sync{\n"
            "  default.rsync,\n"
            f'  source = "{cfg.source}",\n'
            f'  target = "{cfg.target}",\n'
            f'  rsync = {{archive=true, compress=true}},\n'
            "}"
        )

    lua = f'settings {{\n  logfile = "/dev/null",\n  statusFile = "/dev/null",\n}}\n{sync_block}\n'

    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f"lsyncd-{cfg.name}-", suffix=".lua")
    try:
        Path(tmp_path).write_text(lua)
    finally:
        import os as _os
        _os.close(tmp_fd)

    return ["lsyncd", "--oneshot", tmp_path], tmp_path


def _build_rclone_sync_cmd(cfg: "SyncConfig") -> list[str]:  # type: ignore[name-defined]
    """Build a ``rclone sync`` command list for *cfg*.

    Converts SSH-style ``user@host:/path`` remotes to rclone inline SFTP format
    automatically via :func:`sshfs_keeper.mount._ssh_remote_to_rclone`.

    Args:
        cfg: Sync job configuration.

    Returns:
        Argument list suitable for :func:`asyncio.create_subprocess_exec`.
    """
    from sshfs_keeper.mount import _ssh_remote_to_rclone

    source = _ssh_remote_to_rclone(cfg.source) if ":" in cfg.source else cfg.source
    target = _ssh_remote_to_rclone(cfg.target) if ":" in cfg.target else cfg.target

    cmd = [
        "rclone", "sync",
        source, target,
        "--stats-one-line",
        "--stats", "0",
    ]
    if cfg.identity:
        cmd += ["--sftp-key-file", cfg.identity]
    return cmd


def _parse_rclone_stats(output: str) -> tuple[Optional[int], Optional[int]]:
    """Return (bytes_sent, files_transferred) from rclone --stats output.

    rclone --stats-one-line format: ``Transferred: X.XXX GiB, Y%, X.XXX GiB/s, ETA ...``
    and a separate ``Transferred: N / N, 100%`` line.

    Args:
        output: Combined rclone stdout+stderr.

    Returns:
        Tuple of (bytes_sent, files_transferred), either may be ``None``.
    """
    bytes_sent = None
    files_transferred = None
    for line in output.splitlines():
        # "Transferred:   N / N, 100%"  (file count line)
        m = re.search(r"Transferred:\s+(\d+)\s*/\s*\d+", line)
        if m:
            files_transferred = int(m.group(1))
        # "Transferred:  X.XXX KiB" or "X.XXX GiB" (bytes line — last occurrence wins)
        m = re.search(r"Transferred:\s+([\d.]+)\s*(B|KiB|MiB|GiB|TiB)", line)
        if m:
            val, unit = float(m.group(1)), m.group(2)
            multipliers = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
            bytes_sent = int(val * multipliers.get(unit, 1))
    return bytes_sent, files_transferred


def _parse_stats(output: str) -> tuple[Optional[int], Optional[int]]:
    """Return (bytes_sent, files_transferred) from rsync --stats output."""
    bytes_sent = None
    files_transferred = None
    for line in output.splitlines():
        m = re.search(r"Total bytes sent:\s+([\d,]+)", line)
        if m:
            bytes_sent = int(m.group(1).replace(",", ""))
        m = re.search(r"Number of regular files transferred:\s+([\d,]+)", line)
        if m:
            files_transferred = int(m.group(1).replace(",", ""))
    return bytes_sent, files_transferred


class SyncManager:
    """Runs rsync jobs on their configured intervals.

    Args:
        states: Mapping of job name → :class:`SyncState`.
        daemon_cfg: Optional daemon config supplying ``max_retries`` and
            ``backoff_base`` for exponential back-off on consecutive failures.
            When *None* the defaults from :class:`~sshfs_keeper.config.DaemonConfig`
            (3 retries, 60 s base) are used.
    """

    def __init__(
        self,
        states: dict[str, SyncState],
        daemon_cfg: "Optional[DaemonConfig]" = None,
    ) -> None:
        self.states = states
        self._max_retries: int = daemon_cfg.max_retries if daemon_cfg else 3
        self._backoff_base: int = daemon_cfg.backoff_base if daemon_cfg else 60
        self._running = False
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    async def start(self) -> None:
        self._running = True
        # Stagger initial runs so they don't all fire at once
        now = time.time()
        for i, state in enumerate(self.states.values()):
            state._next_run = now + i * 5
        self._task = asyncio.create_task(self._loop(), name="sync-manager")
        log.info("SyncManager started — %d job(s)", len(self.states))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def trigger(self, name: str) -> bool:
        """Manually trigger a sync job immediately. Returns True if it ran."""
        state = self.states.get(name)
        if state is None:
            return False
        state._next_run = 0.0
        return True

    def get_snapshot(self) -> list[dict]:  # type: ignore[type-arg]
        now = time.time()
        return [
            {
                "name": s.config.name,
                "source": s.config.source,
                "target": s.config.target,
                "interval": s.config.interval,
                "options": s.config.options,
                "enabled": s.config.enabled,
                "status": s.status.value,
                "last_run": s.last_run,
                "last_duration": s.last_duration,
                "last_error": s.last_error,
                "bytes_sent": s.bytes_sent,
                "files_transferred": s.files_transferred,
                "run_count": s.run_count,
                "fail_count": s.fail_count,
                "sync_tool": s.config.sync_tool,
                "next_run_in": max(0.0, s._next_run - now) if s.config.enabled else None,
            }
            for s in self.states.values()
        ]

    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            now = time.time()
            due = [s for s in self.states.values() if s.config.enabled and now >= s._next_run]
            if due:
                await asyncio.gather(*[self._run_job(s) for s in due], return_exceptions=True)
            await asyncio.sleep(5)

    async def _run_job(self, state: SyncState) -> None:
        if state._lock.locked():
            return  # already running
        async with state._lock:
            cfg = state.config
            state.status = SyncStatus.RUNNING
            start = time.time()
            log.info("[sync:%s] starting %s → %s", cfg.name, cfg.source, cfg.target)

            if cfg.sync_tool == "lsyncd":
                cmd, _tmp = _build_lsyncd_cmd(cfg)
            elif cfg.sync_tool == "rclone":
                cmd, _tmp = _build_rclone_sync_cmd(cfg), None
            else:
                cmd, _tmp = _build_rsync_cmd(cfg), None

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
                duration = time.time() - start

                combined = (stdout.decode() + stderr.decode()).strip()
                all_lines = combined.splitlines()
                state.last_output = all_lines[-_MAX_OUTPUT_LINES:]

                if proc.returncode in _SOFT_EXIT_CODES:
                    parser = _parse_rclone_stats if cfg.sync_tool == "rclone" else _parse_stats
                    bytes_sent, files_xfr = parser(stdout.decode())
                    state.status = SyncStatus.OK
                    state.last_error = None
                    state.bytes_sent = bytes_sent
                    state.files_transferred = files_xfr
                    state.fail_count = 0
                    log.info("[sync:%s] done in %.1fs — %s file(s)", cfg.name, duration, files_xfr)
                else:
                    state.status = SyncStatus.FAILED
                    state.last_error = stderr.decode().strip().splitlines()[-1] if stderr else f"exit {proc.returncode}"
                    state.fail_count += 1
                    log.warning("[sync:%s] failed (exit %d): %s", cfg.name, proc.returncode, state.last_error)

                state.last_run = start
                state.last_duration = duration
                state.run_count += 1

            except asyncio.TimeoutError:
                state.status = SyncStatus.FAILED
                state.last_error = "Timed out after 1h"
                state.fail_count += 1
                log.warning("[sync:%s] timed out", cfg.name)
            except Exception as exc:
                state.status = SyncStatus.FAILED
                state.last_error = str(exc)
                state.fail_count += 1
                log.warning("[sync:%s] error: %s", cfg.name, exc)
            finally:
                # Clean up lsyncd temp config file
                if _tmp:
                    import os as _os
                    try:
                        _os.unlink(_tmp)
                    except OSError:
                        pass
                if state.status == SyncStatus.FAILED and state.fail_count >= self._max_retries:
                    backoff = self._backoff_base * (2 ** (state.fail_count - self._max_retries))
                    state._next_run = time.time() + backoff
                    log.info(
                        "[sync:%s] backoff — retry in %ds (fail_count=%d)",
                        cfg.name, backoff, state.fail_count,
                    )
                else:
                    state._next_run = time.time() + cfg.interval
