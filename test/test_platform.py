"""Tests for rclone backend, lsyncd sync, autofs detection, and service installer."""

import asyncio
import os
import sys
import platform
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

from sshfs_keeper.config import MountConfig, SyncConfig as CfgSyncConfig
from sshfs_keeper.mount import (
    _parse_mounts_linux,
    _ssh_remote_to_rclone,
    is_autofs_managed,
)
from sshfs_keeper.sync import SyncConfig, SyncState, SyncManager, _build_rsync_cmd, _build_lsyncd_cmd
from sshfs_keeper.main import _cmd_install_service


def _make_stream_mock(data: bytes) -> asyncio.StreamReader:
    """Create a real asyncio.StreamReader pre-loaded with data for testing."""
    stream = asyncio.StreamReader()
    stream.feed_data(data)
    stream.feed_eof()
    return stream


def _make_proc_mock(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Create a mock subprocess with real asyncio.StreamReader stdout/stderr."""
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.stdout = _make_stream_mock(stdout)
    mock_proc.stderr = _make_stream_mock(stderr)
    mock_proc.wait = AsyncMock()
    return mock_proc


# ------------------------------------------------------------------
# _ssh_remote_to_rclone
# ------------------------------------------------------------------

def test_ssh_remote_to_rclone_with_user() -> None:
    result = _ssh_remote_to_rclone("miro@192.168.1.10:/media/nas")
    assert result == ":sftp,host=192.168.1.10,user=miro:/media/nas"


def test_ssh_remote_to_rclone_no_user() -> None:
    result = _ssh_remote_to_rclone("192.168.1.10:/data")
    assert result == ":sftp,host=192.168.1.10:/data"


def test_ssh_remote_to_rclone_already_rclone() -> None:
    result = _ssh_remote_to_rclone("myremote:/path")
    assert result == "myremote:/path"


def test_ssh_remote_to_rclone_inline_sftp() -> None:
    result = _ssh_remote_to_rclone(":sftp,host=h:/p")
    assert result == ":sftp,host=h:/p"


# ------------------------------------------------------------------
# _parse_mounts_linux — rclone detection
# ------------------------------------------------------------------

_PROC_MOUNTS_RCLONE = (
    "rclone: /mnt/cloud fuse.rclone rw,nosuid,nodev,relatime,user_id=1000 0 0\n"
    "sshfs#user@host:/path /mnt/ssh fuse.sshfs rw 0 0\n"
    "tmpfs /tmp tmpfs rw 0 0\n"
)


def test_parse_mounts_linux_detects_rclone() -> None:
    with patch("builtins.open", mock_open(read_data=_PROC_MOUNTS_RCLONE)):
        mounts = _parse_mounts_linux()
    assert "/mnt/cloud" in mounts
    assert "/mnt/ssh" in mounts
    assert "/tmp" not in mounts


# ------------------------------------------------------------------
# is_autofs_managed
# ------------------------------------------------------------------

_PROC_MOUNTS_AUTOFS = (
    "automount /net autofs rw 0 0\n"
    "sshfs#u@h:/p /mnt/ssh fuse.sshfs rw 0 0\n"
)


def test_is_autofs_managed_direct_match() -> None:
    with patch("builtins.open", mock_open(read_data=_PROC_MOUNTS_AUTOFS)):
        assert is_autofs_managed("/net") is True


def test_is_autofs_managed_child_path() -> None:
    with patch("builtins.open", mock_open(read_data=_PROC_MOUNTS_AUTOFS)):
        assert is_autofs_managed("/net/server/share") is True


def test_is_autofs_managed_unrelated() -> None:
    with patch("builtins.open", mock_open(read_data=_PROC_MOUNTS_AUTOFS)):
        assert is_autofs_managed("/mnt/ssh") is False


def test_is_autofs_managed_oserror() -> None:
    with patch("builtins.open", side_effect=OSError):
        assert is_autofs_managed("/net") is False


# ------------------------------------------------------------------
# rclone mount call
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mount_rclone_invokes_rclone() -> None:
    from sshfs_keeper.mount import mount as do_mount

    cfg = MountConfig(name="cloud", remote="miro@host:/data", local="/mnt/cloud", mount_tool="rclone")
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    called_with: list[str] = []

    async def fake_exec(*args: str, **_kw: object) -> AsyncMock:
        called_with.extend(args)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok, err = await do_mount(cfg)

    assert ok is True
    assert err is None
    assert "rclone" in called_with
    assert "mount" in called_with


@pytest.mark.asyncio
async def test_mount_sshfs_still_works() -> None:
    from sshfs_keeper.mount import mount as do_mount

    cfg = MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas", mount_tool="sshfs")
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    called_with: list[str] = []

    async def fake_exec(*args: str, **_kw: object) -> AsyncMock:
        called_with.extend(args)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok, err = await do_mount(cfg)

    assert ok is True
    assert err is None
    assert "sshfs" in called_with


# ------------------------------------------------------------------
# _build_rsync_cmd / _build_lsyncd_cmd
# ------------------------------------------------------------------

def test_build_rsync_cmd_basic() -> None:
    sc = SyncConfig(name="j", source="/src", target="/dst")
    cmd = _build_rsync_cmd(sc)
    assert cmd[0] == "rsync"
    assert "/src" in cmd
    assert "/dst" in cmd


def test_build_rsync_cmd_with_identity() -> None:
    # identity is only injected via -e ssh for remote paths
    sc = SyncConfig(name="j", source="/src", target="user@host:/dst", identity="/key")
    cmd = _build_rsync_cmd(sc)
    assert any("ssh" in part and "-i /key" in part for part in cmd)


def test_build_lsyncd_cmd_local_to_local(tmp_path: Path) -> None:
    sc = SyncConfig(name="j", source="/src", target="/dst", sync_tool="lsyncd")
    cmd, tmp_file = _build_lsyncd_cmd(sc)
    try:
        assert cmd[0] == "lsyncd"
        assert "--oneshot" in cmd
        assert Path(tmp_file).exists()
        content = Path(tmp_file).read_text()
        assert "default.rsync" in content
        assert "/src" in content
        assert "/dst" in content
    finally:
        Path(tmp_file).unlink(missing_ok=True)


def test_build_lsyncd_cmd_remote_target(tmp_path: Path) -> None:
    sc = SyncConfig(name="j", source="/src", target="miro@host:/dst", sync_tool="lsyncd")
    cmd, tmp_file = _build_lsyncd_cmd(sc)
    try:
        content = Path(tmp_file).read_text()
        assert "rsyncssh" in content
        assert "miro@host" in content
        assert "/dst" in content
    finally:
        Path(tmp_file).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_sync_lsyncd_job_runs() -> None:
    sc = SyncConfig(name="j", source="/src", target="/dst", sync_tool="lsyncd", interval=9999)
    state = SyncState(config=sc)
    sm = SyncManager({"j": state})

    mock_proc = _make_proc_mock(returncode=0, stdout=b"", stderr=b"")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        await sm._run_job(state)

    assert state.status.value == "ok"


# ------------------------------------------------------------------
# install-service
# ------------------------------------------------------------------

def test_install_service_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/sshfs-keeper")

    _cmd_install_service(SimpleNamespace())

    unit = tmp_path / ".config" / "systemd" / "user" / "sshfs-keeper.service"
    assert unit.exists()
    content = unit.read_text()
    assert "ExecStart=" in content
    assert "sshfs-keeper" in content


def test_install_service_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/sshfs-keeper")

    _cmd_install_service(SimpleNamespace())

    plist = tmp_path / "Library" / "LaunchAgents" / "com.sshfs-keeper.plist"
    assert plist.exists()
    content = plist.read_text()
    assert "<key>Label</key>" in content
    assert "sshfs-keeper" in content


def test_install_service_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _: None)

    _cmd_install_service(SimpleNamespace())

    bat = tmp_path / "sshfs-keeper" / "install-service.bat"
    assert bat.exists()
    content = bat.read_text()
    assert "nssm" in content.lower()


def test_install_service_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_install_service(SimpleNamespace())
    assert exc_info.value.code == 1
