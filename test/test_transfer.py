"""Tests for sshfs_keeper.transfer — TransferManager and command builder."""

import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sshfs_keeper.transfer import (
    TransferManager,
    TransferRequest,
    TransferStatus,
    _build_cmd,
)


# ------------------------------------------------------------------
# _build_cmd
# ------------------------------------------------------------------


def test_build_cmd_rsync_ssh_copy() -> None:
    req = TransferRequest(protocol="rsync_ssh", source="user@host:/src", dest="/dst")
    cmd = _build_cmd(req)
    assert cmd[0] == "rsync"
    assert "--progress" in cmd
    assert "--remove-source-files" not in cmd
    assert "/dst" in cmd
    assert "user@host:/src" in cmd


def test_build_cmd_rsync_ssh_move() -> None:
    req = TransferRequest(protocol="rsync_ssh", source="/src", dest="/dst", move=True)
    cmd = _build_cmd(req)
    assert "--remove-source-files" in cmd


def test_build_cmd_rsync_ssh_with_identity() -> None:
    req = TransferRequest(protocol="rsync_ssh", source="/s", dest="/d", identity="/key")
    cmd = _build_cmd(req)
    e_idx = cmd.index("-e")
    assert "-i /key" in cmd[e_idx + 1]


def test_build_cmd_local() -> None:
    req = TransferRequest(protocol="local", source="/a", dest="/b")
    cmd = _build_cmd(req)
    assert cmd[0] == "rsync"
    assert "-e" not in cmd
    assert "/a" in cmd and "/b" in cmd


def test_build_cmd_scp() -> None:
    req = TransferRequest(protocol="scp", source="user@host:/s", dest="/d")
    cmd = _build_cmd(req)
    assert cmd[0] == "scp"
    assert "-r" in cmd


def test_build_cmd_scp_identity() -> None:
    req = TransferRequest(protocol="scp", source="/s", dest="/d", identity="/k")
    cmd = _build_cmd(req)
    assert "-i" in cmd
    assert "/k" in cmd


def test_build_cmd_rclone_copy() -> None:
    req = TransferRequest(protocol="rclone", source="remote:/s", dest="/d")
    cmd = _build_cmd(req)
    assert cmd[0] == "rclone"
    assert "copy" in cmd


def test_build_cmd_rclone_move() -> None:
    req = TransferRequest(protocol="rclone", source="remote:/s", dest="/d", move=True)
    cmd = _build_cmd(req)
    assert "move" in cmd


def test_build_cmd_unknown_protocol() -> None:
    req = TransferRequest(protocol="magic", source="/s", dest="/d")
    with pytest.raises(ValueError, match="Unknown transfer protocol"):
        _build_cmd(req)


def test_build_cmd_extra_options() -> None:
    req = TransferRequest(protocol="local", source="/a", dest="/b", options="--bwlimit=1M --exclude=.DS_Store")
    cmd = _build_cmd(req)
    assert "--bwlimit=1M" in cmd
    assert "--exclude=.DS_Store" in cmd


# ------------------------------------------------------------------
# TransferManager
# ------------------------------------------------------------------


@pytest.fixture
def manager() -> TransferManager:
    return TransferManager()


async def _async_lines(*lines: bytes) -> AsyncIterator[bytes]:
    """Helper: yield bytes lines as an async iterator."""
    for line in lines:
        yield line


def _make_proc(returncode: int, *output_lines: bytes) -> AsyncMock:
    """Build a mock asyncio.subprocess.Process for testing."""
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    # Make stdout an async iterable
    mock_proc.stdout = _async_lines(*output_lines)
    mock_proc.wait = AsyncMock(return_value=returncode)
    mock_proc.terminate = MagicMock()
    return mock_proc


@pytest.mark.asyncio
async def test_start_creates_transfer(manager: TransferManager) -> None:
    req = TransferRequest(protocol="local", source="/s", dest="/d")

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc(0, b"file1\n", b"100%  1.0MB/s\n")):
        tid = await manager.start(req)
        await asyncio.sleep(0.05)

    assert tid in manager._transfers
    state = manager._transfers[tid]
    assert state.status == TransferStatus.DONE


@pytest.mark.asyncio
async def test_failed_transfer(manager: TransferManager) -> None:
    req = TransferRequest(protocol="rsync_ssh", source="host:/s", dest="/d")

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc(1, b"error: connection refused\n")):
        tid = await manager.start(req)
        await asyncio.sleep(0.05)

    state = manager._transfers[tid]
    assert state.status == TransferStatus.FAILED
    assert state.error is not None


@pytest.mark.asyncio
async def test_cancel_running_transfer(manager: TransferManager) -> None:
    req = TransferRequest(protocol="local", source="/s", dest="/d")

    async def _slow_stdout() -> AsyncIterator[bytes]:
        await asyncio.sleep(10)
        return
        yield b""  # make it an async generator

    slow_proc = MagicMock()
    slow_proc.returncode = None
    slow_proc.stdout = _slow_stdout()
    slow_proc.wait = AsyncMock(return_value=0)
    slow_proc.terminate = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=slow_proc):
        tid = await manager.start(req)
        await asyncio.sleep(0.01)

    ok = await manager.cancel(tid)
    assert ok is True
    assert manager._transfers[tid].status == TransferStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_finished_transfer(manager: TransferManager) -> None:
    req = TransferRequest(protocol="local", source="/s", dest="/d")

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc(0)):
        tid = await manager.start(req)
        await asyncio.sleep(0.05)

    ok = await manager.cancel(tid)
    assert ok is False  # already done


@pytest.mark.asyncio
async def test_tool_not_found(manager: TransferManager) -> None:
    req = TransferRequest(protocol="rclone", source="remote:/s", dest="/d")

    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        tid = await manager.start(req)
        await asyncio.sleep(0.05)

    state = manager._transfers[tid]
    assert state.status == TransferStatus.FAILED
    assert "rclone" in (state.error or "").lower()


@pytest.mark.asyncio
async def test_history_limit(manager: TransferManager) -> None:
    """History is capped at _MAX_HISTORY entries."""
    from sshfs_keeper.transfer import _MAX_HISTORY

    req = TransferRequest(protocol="local", source="/s", dest="/d")
    with patch("asyncio.create_subprocess_exec", side_effect=lambda *a, **k: _make_proc(0)):
        for _ in range(_MAX_HISTORY + 5):
            await manager.start(req)

    assert len(manager._history) == _MAX_HISTORY
    assert len(manager._transfers) == _MAX_HISTORY


def test_get_output_missing(manager: TransferManager) -> None:
    assert manager.get_output("nonexistent") is None


@pytest.mark.asyncio
async def test_get_snapshot_order(manager: TransferManager) -> None:
    """Snapshot returns newest-first."""
    req = TransferRequest(protocol="local", source="/s", dest="/d")
    ids = []
    with patch("asyncio.create_subprocess_exec", side_effect=lambda *a, **k: _make_proc(0)):
        for _ in range(3):
            ids.append(await manager.start(req))
            await asyncio.sleep(0.01)

    snap = manager.get_snapshot()
    assert [s["id"] for s in snap] == list(reversed(ids))
