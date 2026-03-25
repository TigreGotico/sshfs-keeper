"""One-shot file/directory transfers via rsync-over-SSH, SCP, rclone, or local rsync."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

_MAX_HISTORY = 20
_MAX_OUTPUT_LINES = 200


class TransferStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TransferRequest:
    """Input parameters for a one-shot transfer.

    Attributes:
        protocol: One of ``"rsync_ssh"``, ``"scp"``, ``"rclone"``, ``"local"``.
        source: Source path (local or remote, format depends on protocol).
        dest: Destination path (local or remote).
        move: When ``True`` removes source files after successful transfer.
        identity: Optional path to SSH private key.
        options: Extra flags appended to the command (space-separated).
    """

    protocol: str
    source: str
    dest: str
    move: bool = False
    identity: Optional[str] = None
    options: str = ""


@dataclass
class TransferState:
    """Runtime state for a running or completed transfer.

    Attributes:
        id: Short hex ID unique within this daemon lifetime.
        request: Original :class:`TransferRequest`.
        status: Current :class:`TransferStatus`.
        started_at: Unix timestamp when the subprocess was launched.
        ended_at: Unix timestamp when the subprocess exited or was cancelled.
        output: Last ``_MAX_OUTPUT_LINES`` lines of combined stdout/stderr.
        last_progress: Most recent progress line (contains ``%`` or ``xfr#``).
        error: Human-readable failure reason; ``None`` on success.
    """

    id: str
    request: TransferRequest
    status: TransferStatus = TransferStatus.PENDING
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    output: list[str] = field(default_factory=list)
    last_progress: str = ""
    error: Optional[str] = None
    _proc: Optional["asyncio.subprocess.Process"] = field(default=None, repr=False)  # type: ignore[type-arg]


def _build_cmd(req: TransferRequest) -> list[str]:
    """Build the subprocess command list for *req*.

    Args:
        req: Transfer request describing protocol, paths, and options.

    Returns:
        Argument list for :func:`asyncio.create_subprocess_exec`.

    Raises:
        ValueError: When ``req.protocol`` is not recognised.
    """
    extra = req.options.split() if req.options.strip() else []

    if req.protocol == "local":
        cmd = ["rsync", "-az", "--progress", "-v"]
        if req.move:
            cmd.append("--remove-source-files")
        return cmd + extra + [req.source, req.dest]

    if req.protocol == "rsync_ssh":
        cmd = ["rsync", "-az", "--progress", "-v"]
        if req.move:
            cmd.append("--remove-source-files")
        ssh = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        if req.identity:
            ssh += f" -i {req.identity}"
        cmd += ["-e", ssh]
        return cmd + extra + [req.source, req.dest]

    if req.protocol == "scp":
        cmd = ["scp", "-r"]
        if req.identity:
            cmd += ["-i", req.identity]
        return cmd + extra + [req.source, req.dest]

    if req.protocol == "rclone":
        verb = "move" if req.move else "copy"
        cmd = ["rclone", verb, "--progress", "--stats-one-line"]
        if req.identity:
            cmd += ["--sftp-key-file", req.identity]
        return cmd + extra + [req.source, req.dest]

    raise ValueError(f"Unknown transfer protocol: {req.protocol!r}")


class TransferManager:
    """Manages a bounded history of one-shot file transfers.

    At most ``_MAX_HISTORY`` transfers are retained in memory; the oldest
    entry is evicted when the limit is exceeded.  No state is persisted to
    disk — history is lost on daemon restart.
    """

    def __init__(self) -> None:
        self._transfers: dict[str, TransferState] = {}
        self._history: list[str] = []  # newest-first list of IDs

    # ------------------------------------------------------------------

    def get_snapshot(self) -> list[dict]:  # type: ignore[type-arg]
        """Return a JSON-serialisable list of all transfers, newest first."""
        result = []
        for tid in self._history:
            t = self._transfers.get(tid)
            if t is None:
                continue
            duration: Optional[float] = None
            if t.started_at and t.ended_at:
                duration = round(t.ended_at - t.started_at, 1)
            result.append(
                {
                    "id": t.id,
                    "protocol": t.request.protocol,
                    "source": t.request.source,
                    "dest": t.request.dest,
                    "move": t.request.move,
                    "status": t.status.value,
                    "started_at": t.started_at,
                    "ended_at": t.ended_at,
                    "duration": duration,
                    "last_progress": t.last_progress,
                    "error": t.error,
                }
            )
        return result

    def get_output(self, tid: str) -> Optional[list[str]]:
        """Return captured output lines for transfer *tid*, or ``None`` if not found.

        Args:
            tid: Transfer ID returned by :meth:`start`.

        Returns:
            List of output lines or ``None``.
        """
        t = self._transfers.get(tid)
        return t.output if t else None

    async def start(self, req: TransferRequest) -> str:
        """Enqueue and start a transfer asynchronously.

        Args:
            req: Transfer parameters.

        Returns:
            Short hex ID string for the new transfer.
        """
        tid = uuid.uuid4().hex[:8]
        state = TransferState(id=tid, request=req)
        self._transfers[tid] = state
        self._history.insert(0, tid)

        if len(self._history) > _MAX_HISTORY:
            old = self._history.pop()
            self._transfers.pop(old, None)

        asyncio.create_task(self._run(state), name=f"transfer-{tid}")
        return tid

    async def cancel(self, tid: str) -> bool:
        """Cancel a pending or running transfer.

        Sends SIGTERM to the subprocess if it is still running.

        Args:
            tid: Transfer ID.

        Returns:
            ``True`` if the transfer was found and cancellation was requested;
            ``False`` if it was already finished or not found.
        """
        state = self._transfers.get(tid)
        if state is None or state.status not in (
            TransferStatus.PENDING,
            TransferStatus.RUNNING,
        ):
            return False
        if state._proc and state._proc.returncode is None:
            state._proc.terminate()
        state.status = TransferStatus.CANCELLED
        state.ended_at = time.time()
        return True

    # ------------------------------------------------------------------

    async def _run(self, state: TransferState) -> None:
        state.status = TransferStatus.RUNNING
        state.started_at = time.time()

        try:
            cmd = _build_cmd(state.request)
        except ValueError as exc:
            state.status = TransferStatus.FAILED
            state.error = str(exc)
            state.ended_at = time.time()
            return

        log.info(
            "[transfer:%s] %s → %s via %s",
            state.id,
            state.request.source,
            state.request.dest,
            state.request.protocol,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            state._proc = proc

            async def _drain(stream: object) -> None:
                """Read stream chunks (split on \\n or \\r) into state.

                Supports both asyncio.StreamReader (real subprocess) and
                async iterables of bytes (test mocks).
                """
                buf = b""

                from typing import AsyncGenerator as _AG

                async def _chunks() -> _AG[bytes, None]:
                    if isinstance(stream, asyncio.StreamReader):
                        # Real asyncio.StreamReader
                        while True:
                            chunk = await stream.read(4096)
                            if not chunk:
                                return
                            yield chunk
                    else:
                        # Async iterable (test mock yields whole lines)
                        async for line in stream:  # type: ignore[union-attr]
                            yield line

                async for chunk in _chunks():
                    buf += chunk
                    while True:
                        next_sep = -1
                        for sep in (b"\n", b"\r"):
                            idx = buf.find(sep)
                            if idx >= 0 and (next_sep < 0 or idx < next_sep):
                                next_sep = idx
                        if next_sep < 0:
                            break
                        raw_line = buf[:next_sep]
                        buf = buf[next_sep + 1:]
                        line = raw_line.decode(errors="replace").strip()
                        if not line:
                            continue
                        state.output.append(line)
                        if len(state.output) > _MAX_OUTPUT_LINES:
                            state.output.pop(0)
                        if "%" in line or "xfr#" in line or "Transferred" in line:
                            state.last_progress = line
                        if state.status == TransferStatus.CANCELLED:
                            return
                # flush remainder
                if buf:
                    line = buf.decode(errors="replace").strip()
                    if line:
                        state.output.append(line)

            assert proc.stdout is not None
            assert proc.stderr is not None
            await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
            await proc.wait()

            if state.status == TransferStatus.CANCELLED:
                return

            # rsync exit 24 = "vanished source files" — treated as success
            if proc.returncode in (0, 24):
                state.status = TransferStatus.DONE
                log.info("[transfer:%s] done (exit %d)", state.id, proc.returncode)
            else:
                state.status = TransferStatus.FAILED
                state.error = (
                    state.output[-1] if state.output else f"exit {proc.returncode}"
                )
                log.warning(
                    "[transfer:%s] failed (exit %d): %s",
                    state.id,
                    proc.returncode,
                    state.error,
                )

        except FileNotFoundError:
            proto_tool = {
                "rsync_ssh": "rsync",
                "scp": "scp",
                "rclone": "rclone",
                "local": "rsync",
            }.get(state.request.protocol, state.request.protocol)
            state.status = TransferStatus.FAILED
            state.error = f"{proto_tool} not found — is it installed?"
            log.error("[transfer:%s] %s", state.id, state.error)

        except Exception as exc:
            state.status = TransferStatus.FAILED
            state.error = str(exc)
            log.error("[transfer:%s] unexpected error: %s", state.id, exc)

        finally:
            if state.ended_at is None:
                state.ended_at = time.time()
