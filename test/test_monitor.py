"""Unit tests for the monitor module."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from sshfs_keeper.config import AppConfig, DaemonConfig, MountConfig
from sshfs_keeper.monitor import Monitor, MountStatus


def _make_config(*mounts: MountConfig) -> AppConfig:
    cfg = AppConfig()
    cfg.daemon = DaemonConfig(check_interval=5, remount_delay=0, max_retries=2, backoff_base=10)
    cfg.mounts = list(mounts)
    return cfg


def _mount(name: str = "test", enabled: bool = True) -> MountConfig:
    return MountConfig(name=name, remote="user@host:/path", local="/mnt/test", enabled=enabled)


@pytest.mark.asyncio
async def test_healthy_mount_sets_status():
    cfg = _make_config(_mount())
    monitor = Monitor(cfg)

    with patch("sshfs_keeper.monitor.mnt.is_healthy", new=AsyncMock(return_value=True)):
        await monitor._check_all()

    assert monitor.states["test"].status == MountStatus.HEALTHY


@pytest.mark.asyncio
async def test_disabled_mount_not_checked():
    cfg = _make_config(_mount(enabled=False))
    monitor = Monitor(cfg)

    with patch("sshfs_keeper.monitor.mnt.is_healthy", new=AsyncMock(return_value=False)) as mock:
        await monitor._check_all()

    mock.assert_not_called()
    assert monitor.states["test"].status == MountStatus.DISABLED


@pytest.mark.asyncio
async def test_unmounted_triggers_remount():
    cfg = _make_config(_mount())
    monitor = Monitor(cfg)

    with (
        patch("sshfs_keeper.monitor.mnt.is_healthy", new=AsyncMock(return_value=False)),
        patch("sshfs_keeper.monitor.mnt._parse_proc_mounts", return_value=set()),
        patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(True, None))) as mock_mount,
    ):
        await monitor._check_all()

    mock_mount.assert_called_once()
    assert monitor.states["test"].status == MountStatus.HEALTHY
    assert monitor.states["test"].mount_count == 1


@pytest.mark.asyncio
async def test_stale_mount_unmounts_then_remounts():
    cfg = _make_config(_mount())
    monitor = Monitor(cfg)

    with (
        patch("sshfs_keeper.monitor.mnt.is_healthy", new=AsyncMock(return_value=False)),
        patch("sshfs_keeper.monitor.mnt._parse_proc_mounts", return_value={"/mnt/test"}),
        patch("sshfs_keeper.monitor.mnt.unmount", new=AsyncMock(return_value=True)) as mock_unmount,
        patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(True, None))) as mock_mount,
    ):
        await monitor._check_all()

    mock_unmount.assert_called_once()
    mock_mount.assert_called_once()


@pytest.mark.asyncio
async def test_backoff_after_max_retries():
    cfg = _make_config(_mount())
    monitor = Monitor(cfg)
    state = monitor.states["test"]

    with (
        patch("sshfs_keeper.monitor.mnt.is_healthy", new=AsyncMock(return_value=False)),
        patch("sshfs_keeper.monitor.mnt._parse_proc_mounts", return_value=set()),
        patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(False, "sshfs failed"))),
    ):
        # First two attempts hit max_retries=2
        await monitor._check_all()
        await monitor._check_all()
        # Third check should be in backoff
        await monitor._check_all()

    assert state.backoff_until > time.time()


@pytest.mark.asyncio
async def test_trigger_remount_resets_backoff():
    cfg = _make_config(_mount())
    monitor = Monitor(cfg)
    state = monitor.states["test"]
    state.backoff_until = time.time() + 9999

    with patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(True, None))):
        ok = await monitor.trigger_remount("test")

    assert ok
    assert state.backoff_until == 0.0


def test_get_snapshot_structure():
    cfg = _make_config(_mount("a"), _mount("b"))
    monitor = Monitor(cfg)
    snap = monitor.get_snapshot()
    assert len(snap) == 2
    keys = {"name", "remote", "local", "enabled", "status", "last_check", "mount_count", "backoff_remaining"}
    for item in snap:
        assert keys.issubset(item.keys())
