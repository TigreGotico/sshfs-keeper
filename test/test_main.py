"""Tests for sshfs_keeper/main.py CLI helpers."""

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sshfs_keeper.config import AppConfig, DaemonConfig, MountConfig
from sshfs_keeper.main import (
    _do_reload,
    _read_daemon_pid,
    _remove_pid,
    _setup_logging,
    _write_pid,
    _cmd_reload,
    _cmd_status,
    _cmd_mount,
    _cmd_unmount,
)
from sshfs_keeper.monitor import Monitor, MountState


# ------------------------------------------------------------------
# PID file helpers
# ------------------------------------------------------------------


def test_write_and_read_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr("sshfs_keeper.main.CONFIG_DIR", tmp_path)

    _write_pid()
    pid = _read_daemon_pid()
    assert pid == os.getpid()


def test_read_pid_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", tmp_path / "no.pid")
    assert _read_daemon_pid() is None


def test_read_pid_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("not-a-number")
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", pid_file)
    assert _read_daemon_pid() is None


def test_remove_pid_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", tmp_path / "daemon.pid")
    _remove_pid()  # must not raise


# ------------------------------------------------------------------
# _setup_logging
# ------------------------------------------------------------------


def test_setup_logging_plain(tmp_path: Path) -> None:
    _setup_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_setup_logging_with_file(tmp_path: Path) -> None:
    log_path = str(tmp_path / "app.log")
    _setup_logging("INFO", log_file=log_path)
    root = logging.getLogger()
    handler_types = [type(h).__name__ for h in root.handlers]
    assert "RotatingFileHandler" in handler_types


def test_setup_logging_json() -> None:
    _setup_logging("WARNING", json_logs=True)
    root = logging.getLogger()
    # At least one handler with a JSON-aware formatter
    formatter_classes = [type(h.formatter).__name__ for h in root.handlers if h.formatter]
    assert any("Json" in c for c in formatter_classes)


# ------------------------------------------------------------------
# _do_reload
# ------------------------------------------------------------------


def _make_monitor_for_reload() -> Monitor:
    cfg = AppConfig(
        daemon=DaemonConfig(),
        mounts=[MountConfig(name="a", remote="u@h:/p", local="/mnt/a")],
    )
    return Monitor(cfg)


def test_do_reload_adds_new_mount(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        "[daemon]\n[[mount]]\nname=\"a\"\nremote=\"u@h:/p\"\nlocal=\"/mnt/a\"\n"
        "[[mount]]\nname=\"b\"\nremote=\"u@h:/q\"\nlocal=\"/mnt/b\"\n"
    )
    m = _make_monitor_for_reload()
    m._cfg._path = toml
    _do_reload(m._cfg, m)
    assert "b" in m.states


def test_do_reload_removes_deleted_mount(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text("[daemon]\n")  # no mounts
    m = _make_monitor_for_reload()
    m._cfg._path = toml
    _do_reload(m._cfg, m)
    assert "a" not in m.states


def test_do_reload_preserves_existing_state(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        "[daemon]\n[[mount]]\nname=\"a\"\nremote=\"u@h:/p\"\nlocal=\"/mnt/a\"\n"
    )
    m = _make_monitor_for_reload()
    m.states["a"].mount_count = 42  # runtime state
    m._cfg._path = toml
    _do_reload(m._cfg, m)
    assert m.states["a"].mount_count == 42


def test_do_reload_handles_bad_toml(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text("this is not valid toml !!!")
    m = _make_monitor_for_reload()
    m._cfg._path = toml
    with caplog.at_level(logging.WARNING):
        _do_reload(m._cfg, m)  # must not raise
    assert "a" in m.states  # unchanged


# ------------------------------------------------------------------
# _cmd_reload
# ------------------------------------------------------------------


def test_cmd_reload_sends_sighup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("99999")
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", pid_file)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    args = SimpleNamespace()
    _cmd_reload(args)
    assert killed == [(99999, signal.SIGHUP)]


def test_cmd_reload_no_pid_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", tmp_path / "no.pid")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_reload(SimpleNamespace())
    assert exc_info.value.code == 1


def test_cmd_reload_dead_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("99999")
    monkeypatch.setattr("sshfs_keeper.main._PID_FILE", pid_file)
    monkeypatch.setattr(os, "kill", MagicMock(side_effect=ProcessLookupError))
    with pytest.raises(SystemExit) as exc_info:
        _cmd_reload(SimpleNamespace())
    assert exc_info.value.code == 1


# ------------------------------------------------------------------
# _cmd_status
# ------------------------------------------------------------------


def _make_args(**kwargs: object) -> SimpleNamespace:
    defaults = {"port": None, "api_key": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_cmd_status_prints_table(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    import httpx

    fake_resp = MagicMock()
    fake_resp.json.return_value = {
        "mounts": [
            {
                "name": "nas",
                "status": "healthy",
                "retry_count": 0,
                "mount_count": 5,
                "last_error": None,
            }
        ]
    }
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=fake_resp):
        _cmd_status(_make_args())

    out = capsys.readouterr().out
    assert "nas" in out
    assert "healthy" in out


def test_cmd_status_no_mounts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"mounts": []}
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=fake_resp):
        _cmd_status(_make_args())

    out = capsys.readouterr().out
    assert "No mounts" in out


def test_cmd_status_connection_error(capsys: pytest.CaptureFixture) -> None:
    with patch("httpx.get", side_effect=Exception("refused")):
        with pytest.raises(SystemExit) as exc_info:
            _cmd_status(_make_args())
    assert exc_info.value.code == 1


# ------------------------------------------------------------------
# _cmd_mount / _cmd_unmount
# ------------------------------------------------------------------


def test_cmd_mount_posts_remount(capsys: pytest.CaptureFixture) -> None:
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"success": True}
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=fake_resp):
        _cmd_mount(_make_args(name="nas"))

    out = capsys.readouterr().out
    assert "True" in out or "success" in out


def test_cmd_mount_error(capsys: pytest.CaptureFixture) -> None:
    with patch("httpx.post", side_effect=Exception("timeout")):
        with pytest.raises(SystemExit) as exc_info:
            _cmd_mount(_make_args(name="nas"))
    assert exc_info.value.code == 1


def test_cmd_unmount_posts_unmount(capsys: pytest.CaptureFixture) -> None:
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"success": True}
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=fake_resp):
        _cmd_unmount(_make_args(name="nas"))

    out = capsys.readouterr().out
    assert "True" in out or "success" in out
