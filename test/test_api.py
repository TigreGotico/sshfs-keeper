"""Unit tests for the FastAPI routes."""

import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi.responses import HTMLResponse

from sshfs_keeper.config import AppConfig, MountConfig, DaemonConfig, ApiConfig
from sshfs_keeper.monitor import Monitor
from sshfs_keeper import api as api_module


def _make_monitor() -> Monitor:
    cfg = AppConfig(
        daemon=DaemonConfig(),
        api=ApiConfig(),
        mounts=[MountConfig(name="nas", remote="miro@192.168.1.200:/media/nas", local="/mnt/nas")],
    )
    return Monitor(cfg)


@pytest.fixture
def monitor() -> Monitor:
    m = _make_monitor()
    api_module.setup(m, m._cfg)
    return m


@pytest.fixture
def client(monitor: Monitor) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=api_module.app), base_url="http://test")


# ------------------------------------------------------------------
# Health / status
# ------------------------------------------------------------------

async def test_health_unhealthy(client: AsyncClient) -> None:
    """With an unmounted (non-healthy) mount, /health returns 503."""
    async with client as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert "unhealthy" in r.json()


async def test_health_all_healthy(client: AsyncClient) -> None:
    from sshfs_keeper.monitor import MountStatus
    from sshfs_keeper import api as _api
    _api._monitor.states["nas"].status = MountStatus.HEALTHY
    async with client as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_status_returns_mounts(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["mounts"][0]["name"] == "nas"


# ------------------------------------------------------------------
# Remount / enable / disable
# ------------------------------------------------------------------

async def test_remount_success(client: AsyncClient, monitor: Monitor) -> None:
    with patch.object(monitor, "trigger_remount", new=AsyncMock(return_value=True)):
        async with client as c:
            r = await c.post("/api/mounts/nas/remount")
    assert r.status_code == 200
    assert r.json()["success"] is True


async def test_remount_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.post("/api/mounts/nonexistent/remount")
    assert r.status_code == 404


async def test_disable_enable(client: AsyncClient, monitor: Monitor) -> None:
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.post("/api/mounts/nas/disable")
            assert r.status_code == 200
            assert monitor.states["nas"].config.enabled is False
            r = await c.post("/api/mounts/nas/enable")
            assert r.status_code == 200
            assert monitor.states["nas"].config.enabled is True


# ------------------------------------------------------------------
# Mount CRUD
# ------------------------------------------------------------------

async def test_add_mount(client: AsyncClient, monitor: Monitor) -> None:
    payload = {
        "name": "backup",
        "remote": "miro@192.168.1.200:/media/backup",
        "local": "/mnt/backup",
        "options": "reconnect",
        "enabled": True,
    }
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.post("/api/mounts", json=payload)
    assert r.status_code == 200
    assert r.json()["created"] is True
    assert "backup" in monitor.states


async def test_add_mount_duplicate(client: AsyncClient, monitor: Monitor) -> None:
    payload = {"name": "nas", "remote": "x@y:/z", "local": "/mnt/z", "options": "", "enabled": True}
    async with client as c:
        r = await c.post("/api/mounts", json=payload)
    assert r.status_code == 409


async def test_update_mount(client: AsyncClient, monitor: Monitor) -> None:
    payload = {
        "name": "nas",
        "remote": "miro@192.168.1.200:/media/nas2",
        "local": "/mnt/nas",
        "options": "reconnect",
        "enabled": True,
    }
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.put("/api/mounts/nas", json=payload)
    assert r.status_code == 200
    assert monitor.states["nas"].config.remote == "miro@192.168.1.200:/media/nas2"


async def test_delete_mount(client: AsyncClient, monitor: Monitor) -> None:
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.delete("/api/mounts/nas")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert "nas" not in monitor.states


async def test_delete_mount_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.delete("/api/mounts/ghost")
    assert r.status_code == 404


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

async def test_update_settings(client: AsyncClient, monitor: Monitor) -> None:
    payload = {"check_interval": 60, "log_level": "DEBUG"}
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.put("/api/settings", json=payload)
    assert r.status_code == 200
    assert monitor._cfg.daemon.check_interval == 60
    assert monitor._cfg.daemon.log_level == "DEBUG"


# ------------------------------------------------------------------
# SSH keys
# ------------------------------------------------------------------

async def test_list_keys_empty(client: AsyncClient) -> None:
    with patch("sshfs_keeper.api._list_keys", return_value=[]):
        async with client as c:
            r = await c.get("/api/keys")
    assert r.json()["keys"] == []


async def test_upload_key(client: AsyncClient) -> None:
    key_content = b"-----BEGIN OPENSSH PRIVATE KEY-----\nfakedata\n-----END OPENSSH PRIVATE KEY-----\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("sshfs_keeper.api.KEYS_DIR", Path(tmpdir)):
            async with client as c:
                r = await c.post(
                    "/api/keys",
                    files={"file": ("id_ed25519", io.BytesIO(key_content), "application/octet-stream")},
                )
    assert r.status_code == 200
    assert r.json()["name"] == "id_ed25519"


async def test_upload_key_rejected_if_not_private_key(client: AsyncClient) -> None:
    async with client as c:
        r = await c.post(
            "/api/keys",
            files={"file": ("id_ed25519.pub", io.BytesIO(b"ssh-ed25519 AAAA..."), "text/plain")},
        )
    assert r.status_code == 400


async def test_delete_key(client: AsyncClient) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "mykey"
        key_path.write_text("fake")
        with patch("sshfs_keeper.api.KEYS_DIR", Path(tmpdir)):
            async with client as c:
                r = await c.delete("/api/keys/mykey")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


# ------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------

async def test_dashboard_renders(client: AsyncClient, monitor: Monitor) -> None:
    fake_response = HTMLResponse("<html>sshfs-keeper nas</html>")
    with patch("sshfs_keeper.api.templates") as mock_tpl:
        mock_tpl.TemplateResponse.return_value = fake_response
        async with client as c:
            r = await c.get("/")
    assert r.status_code == 200
    assert b"sshfs-keeper" in r.content


async def test_switch_backend_sshfs_to_rclone(client: AsyncClient, monitor: Monitor) -> None:
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.patch("/api/mounts/nas/backend")
    assert r.status_code == 200
    assert r.json()["mount_tool"] == "rclone"
    assert monitor.states["nas"].config.mount_tool == "rclone"


async def test_switch_backend_rclone_to_sshfs(client: AsyncClient, monitor: Monitor) -> None:
    monitor.states["nas"].config.mount_tool = "rclone"
    with patch.object(monitor._cfg, "save"):
        async with client as c:
            r = await c.patch("/api/mounts/nas/backend")
    assert r.status_code == 200
    assert r.json()["mount_tool"] == "sshfs"


async def test_switch_backend_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.patch("/api/mounts/ghost/backend")
    assert r.status_code == 404
