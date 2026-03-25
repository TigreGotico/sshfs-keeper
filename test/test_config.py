"""Unit tests for config loading."""

import tempfile
from pathlib import Path

import pytest

from sshfs_keeper.config import AppConfig


SAMPLE_TOML = """
[daemon]
check_interval = 15
log_level = "DEBUG"

[api]
host = "127.0.0.1"
port = 9999

[[mount]]
name = "media"
remote = "user@nas:/media"
local = "/mnt/media"
options = "reconnect"

[[mount]]
name = "backup"
remote = "admin@server:/backup"
local = "/mnt/backup"
enabled = false
"""


def test_load_from_file():
    with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as fh:
        fh.write(SAMPLE_TOML)
        path = Path(fh.name)

    cfg = AppConfig.load(path)

    assert cfg.daemon.check_interval == 15
    assert cfg.daemon.log_level == "DEBUG"
    assert cfg.api.host == "127.0.0.1"
    assert cfg.api.port == 9999
    assert len(cfg.mounts) == 2
    assert cfg.mounts[0].name == "media"
    assert cfg.mounts[1].enabled is False


def test_load_defaults_when_no_file():
    cfg = AppConfig.load(Path("/nonexistent/path.toml"))
    assert cfg.daemon.check_interval == 30
    assert cfg.mounts == []


# ------------------------------------------------------------------
# AppConfig.save()
# ------------------------------------------------------------------

def test_save_and_reload_roundtrip():
    """save() then load() preserves all fields."""
    from sshfs_keeper.config import (
        AppConfig, DaemonConfig, ApiConfig, NotificationsConfig,
        MountConfig, SyncConfig,
    )
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        cfg = AppConfig(
            daemon=DaemonConfig(check_interval=20, log_level="DEBUG", json_logs=True),
            api=ApiConfig(host="127.0.0.1", port=9000),
            notifications=NotificationsConfig(
                webhook_url="https://ntfy.sh/test",
                on_failure=True, on_recovery=False, on_backoff=True,
            ),
            mounts=[MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas",
                                options="reconnect", identity="/key", enabled=False)],
            syncs=[SyncConfig(name="bak", source="/src", target="/dst",
                              interval=1800, enabled=False)],
        )
        cfg._path = path
        cfg.save()

        loaded = AppConfig.load(path)

    assert loaded.daemon.check_interval == 20
    assert loaded.daemon.log_level == "DEBUG"
    assert loaded.daemon.json_logs is True
    assert loaded.api.port == 9000
    assert loaded.notifications.webhook_url == "https://ntfy.sh/test"
    assert loaded.notifications.on_recovery is False
    assert loaded.notifications.on_backoff is True
    assert loaded.mounts[0].name == "nas"
    assert loaded.mounts[0].identity == "/key"
    assert loaded.mounts[0].enabled is False
    assert loaded.syncs[0].name == "bak"
    assert loaded.syncs[0].interval == 1800


def test_save_notifications_without_webhook():
    """Notification settings are persisted even without a webhook_url."""
    from sshfs_keeper.config import AppConfig, NotificationsConfig
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        cfg = AppConfig(
            notifications=NotificationsConfig(on_failure=False, on_recovery=False),
        )
        cfg._path = path
        cfg.save()

        loaded = AppConfig.load(path)

    assert loaded.notifications.on_failure is False
    assert loaded.notifications.on_recovery is False
    assert loaded.notifications.webhook_url is None


def test_save_uses_default_path_when_path_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When _path is None and no search path exists, save to CONFIG_DIR/config.toml."""
    from sshfs_keeper.config import AppConfig
    monkeypatch.setattr("sshfs_keeper.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sshfs_keeper.config.CONFIG_SEARCH_PATHS", [tmp_path / "config.toml"])
    cfg = AppConfig()
    cfg._path = None
    cfg.save()
    assert (tmp_path / "config.toml").exists()


def test_save_rclone_mount_tool(tmp_path: Path):
    """mount_tool != 'sshfs' is written to the TOML."""
    from sshfs_keeper.config import AppConfig, MountConfig
    path = tmp_path / "cfg.toml"
    cfg = AppConfig(mounts=[MountConfig(name="c", remote="r:p", local="/mnt/c", mount_tool="rclone")])
    cfg._path = path
    cfg.save()
    content = path.read_text()
    assert 'mount_tool = "rclone"' in content


def test_save_lsyncd_sync_tool(tmp_path: Path):
    """sync_tool != 'rsync' is written to the TOML."""
    from sshfs_keeper.config import AppConfig, SyncConfig
    path = tmp_path / "cfg.toml"
    cfg = AppConfig(syncs=[SyncConfig(name="s", source="/a", target="/b", sync_tool="lsyncd")])
    cfg._path = path
    cfg.save()
    content = path.read_text()
    assert 'sync_tool = "lsyncd"' in content


# ------------------------------------------------------------------
# AppConfig.validate()
# ------------------------------------------------------------------

def test_validate_clean_config():
    from sshfs_keeper.config import AppConfig, MountConfig
    cfg = AppConfig(mounts=[MountConfig(name="a", remote="u@h:/p", local="/m/a")])
    assert cfg.validate() == []


def test_validate_duplicate_mount_names():
    from sshfs_keeper.config import AppConfig, MountConfig
    cfg = AppConfig(mounts=[
        MountConfig(name="a", remote="u@h:/p", local="/m/a"),
        MountConfig(name="a", remote="u@h:/q", local="/m/b"),
    ])
    errors = cfg.validate()
    assert any("Duplicate mount name" in e for e in errors)


def test_validate_missing_remote():
    from sshfs_keeper.config import AppConfig, MountConfig
    cfg = AppConfig(mounts=[MountConfig(name="x", remote="", local="/m/x")])
    errors = cfg.validate()
    assert any("remote is required" in e for e in errors)


def test_validate_missing_local():
    from sshfs_keeper.config import AppConfig, MountConfig
    cfg = AppConfig(mounts=[MountConfig(name="x", remote="u@h:/p", local="")])
    errors = cfg.validate()
    assert any("local is required" in e for e in errors)


def test_validate_invalid_mount_tool():
    from sshfs_keeper.config import AppConfig, MountConfig
    cfg = AppConfig(mounts=[MountConfig(name="x", remote="u@h:/p", local="/m", mount_tool="nfs")])
    errors = cfg.validate()
    assert any("mount_tool" in e for e in errors)


def test_validate_duplicate_sync_names():
    from sshfs_keeper.config import AppConfig, SyncConfig
    cfg = AppConfig(syncs=[
        SyncConfig(name="j", source="/a", target="/b"),
        SyncConfig(name="j", source="/c", target="/d"),
    ])
    errors = cfg.validate()
    assert any("Duplicate sync name" in e for e in errors)


def test_validate_sync_interval_too_small():
    from sshfs_keeper.config import AppConfig, SyncConfig
    cfg = AppConfig(syncs=[SyncConfig(name="j", source="/a", target="/b", interval=0)])
    errors = cfg.validate()
    assert any("interval" in e for e in errors)


def test_validate_invalid_sync_tool():
    from sshfs_keeper.config import AppConfig, SyncConfig
    cfg = AppConfig(syncs=[SyncConfig(name="j", source="/a", target="/b", sync_tool="bad")])
    errors = cfg.validate()
    assert any("sync_tool" in e for e in errors)


def test_validate_empty_sync_source():
    from sshfs_keeper.config import AppConfig, SyncConfig
    cfg = AppConfig(syncs=[SyncConfig(name="j", source="", target="/b")])
    errors = cfg.validate()
    assert any("source is required" in e for e in errors)
