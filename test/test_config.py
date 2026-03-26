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


# ------------------------------------------------------------------
# Config Migration (old format → structured hosts)
# ------------------------------------------------------------------

def test_migrate_mount_old_format_to_host():
    """Old-format mounts (free-text remote) auto-migrate to host references."""
    from sshfs_keeper.config import AppConfig, MountConfig

    # Simulate old-format mount (no host_name/path)
    old_mounts = [
        MountConfig(name="media", remote="miro@nas:/media/photos", local="/mnt/photos"),
    ]
    cfg = AppConfig._migrate_to_hosts([], old_mounts, [])
    hosts, mounts, _ = cfg

    # Should create a host for nas
    assert len(hosts) == 1
    assert hosts[0].name == "nas"
    assert hosts[0].hostname == "nas"
    assert hosts[0].user == "miro"

    # Mount should now reference the host
    assert mounts[0].host_name == "nas"
    assert mounts[0].path == "/media/photos"
    assert mounts[0].remote == "miro@nas:/media/photos"  # preserved


def test_migrate_sync_old_format_to_host():
    """Old-format syncs (free-text source/target) auto-migrate to host references."""
    from sshfs_keeper.config import AppConfig, SyncConfig

    old_syncs = [
        SyncConfig(
            name="backup",
            source="/local/data",
            target="admin@backup-server:/backups/data",
        ),
    ]
    hosts, _, syncs = AppConfig._migrate_to_hosts([], [], old_syncs)

    # Should create a host for backup-server
    assert len(hosts) == 1
    assert hosts[0].name == "backup-server"
    assert hosts[0].hostname == "backup-server"
    assert hosts[0].user == "admin"

    # Sync should now reference the host
    assert syncs[0].target_host == "backup-server"
    assert syncs[0].target_path == "/backups/data"
    assert syncs[0].target == "admin@backup-server:/backups/data"  # preserved


def test_migrate_reuses_existing_host():
    """Multiple mounts referencing same user@host reuse a single HostConfig."""
    from sshfs_keeper.config import AppConfig, MountConfig, HostConfig

    # Two mounts pointing to same host
    old_mounts = [
        MountConfig(name="photos", remote="miro@nas:/media/photos", local="/mnt/photos"),
        MountConfig(name="music", remote="miro@nas:/media/music", local="/mnt/music"),
    ]
    hosts, mounts, _ = AppConfig._migrate_to_hosts([], old_mounts, [])

    # Should create only one host
    assert len(hosts) == 1
    assert hosts[0].name == "nas"

    # Both mounts reference the same host
    assert mounts[0].host_name == "nas"
    assert mounts[1].host_name == "nas"
    assert mounts[0].path == "/media/photos"
    assert mounts[1].path == "/media/music"


def test_migrate_skips_already_structured():
    """Mounts with host_name already set are not re-migrated."""
    from sshfs_keeper.config import AppConfig, MountConfig, HostConfig

    # Pre-existing host
    host = HostConfig(name="nas", hostname="192.168.1.10", user="miro")

    # Mount already using host reference
    mount = MountConfig(
        name="photos",
        remote="miro@192.168.1.10:/media/photos",
        local="/mnt/photos",
        host_name="nas",
        path="/media/photos",
    )

    hosts, mounts, _ = AppConfig._migrate_to_hosts([host], [mount], [])

    # No new hosts created
    assert len(hosts) == 1
    assert hosts[0] == host

    # Mount fields unchanged
    assert mounts[0].host_name == "nas"
    assert mounts[0].path == "/media/photos"


def test_migrate_local_paths_unchanged():
    """Local-only paths (no user@host) are left unchanged."""
    from sshfs_keeper.config import AppConfig, SyncConfig

    sync = SyncConfig(
        name="local_backup",
        source="/home/user/data",
        target="/backup/data",
    )
    hosts, _, syncs = AppConfig._migrate_to_hosts([], [], [sync])

    # No hosts created
    assert len(hosts) == 0

    # Sync unchanged
    assert syncs[0].source_host == ""
    assert syncs[0].target_host == ""
    assert syncs[0].source == "/home/user/data"
    assert syncs[0].target == "/backup/data"


def test_migrate_load_old_config_file():
    """End-to-end: loading a pre-migration config auto-creates hosts and references."""
    # Simulate an old config file (before host entities existed)
    OLD_CONFIG_TOML = """
[daemon]
check_interval = 30

[[mount]]
name = "photos"
remote = "miro@nas:/media/photos"
local = "/mnt/photos"
enabled = true

[[mount]]
name = "music"
remote = "miro@nas:/media/music"
local = "/mnt/music"
identity = "/home/miro/.ssh/nas_key"

[[sync]]
name = "backup"
source = "/home/user/documents"
target = "backup@server:/backups/docs"
interval = 3600
"""

    with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as fh:
        fh.write(OLD_CONFIG_TOML)
        path = Path(fh.name)

    try:
        cfg = AppConfig.load(path)

        # Should have created 2 hosts
        assert len(cfg.hosts) == 2
        host_names = {h.name for h in cfg.hosts}
        assert "nas" in host_names
        assert "server" in host_names

        # Find hosts
        nas_host = next(h for h in cfg.hosts if h.name == "nas")
        server_host = next(h for h in cfg.hosts if h.name == "server")

        assert nas_host.hostname == "nas"
        assert nas_host.user == "miro"
        assert server_host.hostname == "server"
        assert server_host.user == "backup"

        # Mounts should reference the hosts
        assert cfg.mounts[0].name == "photos"
        assert cfg.mounts[0].host_name == "nas"
        assert cfg.mounts[0].path == "/media/photos"
        assert cfg.mounts[0].remote == "miro@nas:/media/photos"  # preserved

        assert cfg.mounts[1].name == "music"
        assert cfg.mounts[1].host_name == "nas"
        assert cfg.mounts[1].path == "/media/music"
        assert cfg.mounts[1].identity == "/home/miro/.ssh/nas_key"

        # Sync should reference the target host
        assert cfg.syncs[0].name == "backup"
        assert cfg.syncs[0].source == "/home/user/documents"
        assert cfg.syncs[0].target == "backup@server:/backups/docs"
        assert cfg.syncs[0].target_host == "server"
        assert cfg.syncs[0].target_path == "/backups/docs"
        assert cfg.syncs[0].source_host == ""  # local path, no host

    finally:
        path.unlink()
