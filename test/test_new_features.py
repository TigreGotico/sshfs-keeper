"""Tests for new features: notifications, metrics, SSE, CLI helpers, mount usage."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sshfs_keeper import api as api_module
from sshfs_keeper.config import AppConfig, DaemonConfig, MountConfig, NotificationsConfig
from sshfs_keeper.monitor import Monitor, MountStatus
from sshfs_keeper.sync import SyncManager, SyncState, SyncConfig


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_stream_mock(data: bytes) -> asyncio.StreamReader:
    """Create a real asyncio.StreamReader pre-loaded with *data* for _drain tests."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _make_proc_mock(returncode: int, stdout: bytes, stderr: bytes) -> AsyncMock:
    """Build a mock subprocess whose stdout/stderr are real StreamReaders."""
    mock_proc = AsyncMock()
    mock_proc.returncode = returncode
    mock_proc.stdout = _make_stream_mock(stdout)
    mock_proc.stderr = _make_stream_mock(stderr)
    mock_proc.wait = AsyncMock()
    return mock_proc


def _make_monitor(notifications: NotificationsConfig | None = None) -> Monitor:
    n = notifications or NotificationsConfig()
    cfg = AppConfig(
        daemon=DaemonConfig(),
        notifications=n,
        mounts=[MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas")],
    )
    return Monitor(cfg)


def _make_sync_manager() -> tuple[SyncManager, dict[str, SyncState]]:
    sc = SyncConfig(name="bak", source="/src", target="/dst")
    states = {"bak": SyncState(config=sc)}
    sm = SyncManager(states)
    return sm, states


@pytest.fixture
def monitor() -> Monitor:
    m = _make_monitor()
    sm, _ = _make_sync_manager()
    api_module.setup(m, m._cfg, sm)
    return m


@pytest.fixture
def client(monitor: Monitor) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=api_module.app), base_url="http://test")


# ------------------------------------------------------------------
# NotificationsConfig
# ------------------------------------------------------------------

def test_notifications_config_defaults() -> None:
    n = NotificationsConfig()
    assert n.webhook_url is None
    assert n.on_failure is True
    assert n.on_recovery is True
    assert n.on_backoff is False


# ------------------------------------------------------------------
# notify module
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_skips_when_no_url() -> None:
    """No HTTP call is made when webhook_url is None."""
    from sshfs_keeper import notify
    with patch("sshfs_keeper.notify.send_webhook", new=AsyncMock()) as mock_send:
        await notify.notify(
            webhook_url=None, on_failure=True, on_recovery=True, on_backoff=True,
            event="failure", mount="nas",
        )
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_skips_when_event_disabled() -> None:
    from sshfs_keeper import notify
    with patch("sshfs_keeper.notify.send_webhook", new=AsyncMock()) as mock_send:
        await notify.notify(
            webhook_url="http://hook", on_failure=False, on_recovery=True, on_backoff=False,
            event="failure", mount="nas",
        )
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_sends_when_enabled() -> None:
    from sshfs_keeper import notify

    # create_task needs a running event loop; use asyncio.ensure_future pattern
    created: list[asyncio.Task] = []  # type: ignore[type-arg]

    def _fake_create_task(coro, **_kw):  # type: ignore[type-arg]
        task = asyncio.ensure_future(coro)
        created.append(task)
        return task

    with patch("asyncio.create_task", side_effect=_fake_create_task):
        with patch("sshfs_keeper.notify.send_webhook", new=AsyncMock()) as mock_send:
            await notify.notify(
                webhook_url="http://hook", on_failure=True, on_recovery=True, on_backoff=False,
                event="failure", mount="nas", error="oops",
            )
            # drain the created tasks
            if created:
                await asyncio.gather(*created)

    mock_send.assert_called_once_with("http://hook", "failure", "nas", "oops")


# ------------------------------------------------------------------
# Metrics endpoint
# ------------------------------------------------------------------

async def test_metrics_endpoint(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "sshfs_keeper_mount_healthy" in r.text
    assert "sshfs_keeper_mount_count" in r.text


async def test_metrics_content_type(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/metrics")
    assert "text/plain" in r.headers["content-type"]


# ------------------------------------------------------------------
# Version endpoint
# ------------------------------------------------------------------

async def test_version_endpoint(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/version")
    assert r.status_code == 200
    assert "version" in r.json()


# ------------------------------------------------------------------
# Mount usage in snapshot
# ------------------------------------------------------------------

def test_snapshot_includes_usage_when_healthy() -> None:
    m = _make_monitor()
    state = m.states["nas"]
    state.status = MountStatus.HEALTHY

    fake_usage = {"total_gb": 100.0, "used_gb": 40.0, "free_gb": 60.0, "percent_used": 40.0}
    with patch("sshfs_keeper.monitor.mnt.get_usage", return_value=fake_usage):
        snap = m.get_snapshot()

    assert snap[0]["usage"] == fake_usage


def test_snapshot_usage_none_when_not_healthy() -> None:
    m = _make_monitor()
    state = m.states["nas"]
    state.status = MountStatus.ERROR

    with patch("sshfs_keeper.monitor.mnt.get_usage", return_value=None) as mock_usage:
        snap = m.get_snapshot()

    mock_usage.assert_not_called()  # get_usage only called for HEALTHY mounts
    assert snap[0]["usage"] is None


# ------------------------------------------------------------------
# mount.get_usage
# ------------------------------------------------------------------

def test_get_usage_returns_dict() -> None:
    from sshfs_keeper.mount import get_usage
    from types import SimpleNamespace

    # 1 TB total (262144 blocks × 4 MiB), 40% free
    fake_stat = SimpleNamespace(f_blocks=262144, f_frsize=4 * 1024 * 1024, f_bavail=104858)

    with patch("sshfs_keeper.mount.os.statvfs", return_value=fake_stat):
        result = get_usage("/mnt/nas")

    assert result is not None
    assert result["total_gb"] > 0
    assert result["free_gb"] < result["total_gb"]
    assert 0 <= result["percent_used"] <= 100


def test_get_usage_returns_none_on_error() -> None:
    from sshfs_keeper.mount import get_usage

    with patch("sshfs_keeper.mount.os.statvfs", side_effect=OSError("stale")):
        result = get_usage("/mnt/nas")

    assert result is None


# ------------------------------------------------------------------
# Sync last_output
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_captures_output() -> None:
    from sshfs_keeper.sync import SyncManager, SyncState, SyncConfig

    sc = SyncConfig(name="job", source="/src", target="/dst", interval=9999)
    state = SyncState(config=sc)
    sm = SyncManager({"job": state})

    mock_proc = _make_proc_mock(
        returncode=0,
        stdout=b"Number of regular files transferred: 5\nTotal bytes sent: 1,234\n",
        stderr=b"",
    )

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        await sm._run_job(state)

    assert len(state.last_output) > 0
    assert any("files transferred" in line for line in state.last_output)


# ------------------------------------------------------------------
# Sync retry backoff
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_fail_count_increments() -> None:
    from sshfs_keeper.sync import SyncManager, SyncState, SyncConfig

    sc = SyncConfig(name="job", source="/src", target="/dst", interval=9999)
    state = SyncState(config=sc)
    sm = SyncManager({"job": state})

    mock_proc = _make_proc_mock(returncode=1, stdout=b"", stderr=b"some error")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        await sm._run_job(state)

    assert state.fail_count == 1


@pytest.mark.asyncio
async def test_sync_fail_count_resets_on_success() -> None:
    from sshfs_keeper.sync import SyncManager, SyncState, SyncConfig

    sc = SyncConfig(name="job", source="/src", target="/dst", interval=9999)
    state = SyncState(config=sc)
    state.fail_count = 5
    sm = SyncManager({"job": state})

    mock_proc = _make_proc_mock(
        returncode=0,
        stdout=b"Total bytes sent: 0\nNumber of regular files transferred: 0\n",
        stderr=b"",
    )

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        await sm._run_job(state)

    assert state.fail_count == 0


@pytest.mark.asyncio
async def test_sync_backoff_applied_after_max_retries() -> None:
    """After fail_count >= max_retries, next_run uses backoff not interval."""
    import time as _time
    from sshfs_keeper.sync import SyncManager, SyncState, SyncConfig
    from sshfs_keeper.config import DaemonConfig

    sc = SyncConfig(name="job", source="/src", target="/dst", interval=3600)
    state = SyncState(config=sc)
    state.fail_count = 2  # will become 3 = max_retries
    daemon_cfg = DaemonConfig(max_retries=3, backoff_base=60)
    sm = SyncManager({"job": state}, daemon_cfg=daemon_cfg)

    mock_proc = _make_proc_mock(returncode=1, stdout=b"", stderr=b"err")

    before = _time.time()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        await sm._run_job(state)

    # fail_count is now 3 >= max_retries=3, backoff = 60 * 2^0 = 60
    assert state._next_run < before + 3600  # less than full interval
    assert state._next_run >= before + 55   # roughly 60s


@pytest.mark.asyncio
async def test_sync_normal_interval_when_below_max_retries() -> None:
    """Before max_retries is reached, use normal interval (not backoff)."""
    import time as _time
    from sshfs_keeper.sync import SyncManager, SyncState, SyncConfig
    from sshfs_keeper.config import DaemonConfig

    sc = SyncConfig(name="job", source="/src", target="/dst", interval=3600)
    state = SyncState(config=sc)
    daemon_cfg = DaemonConfig(max_retries=3, backoff_base=60)
    sm = SyncManager({"job": state}, daemon_cfg=daemon_cfg)

    mock_proc = _make_proc_mock(returncode=1, stdout=b"", stderr=b"err")

    before = _time.time()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        await sm._run_job(state)

    # fail_count=1 < max_retries=3: use interval
    assert state._next_run >= before + 3590


# ------------------------------------------------------------------
# Notifications API endpoints
# ------------------------------------------------------------------

async def test_get_notifications(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/notifications")
    assert r.status_code == 200
    data = r.json()
    assert "on_failure" in data
    assert "on_recovery" in data
    assert "on_backoff" in data


async def test_put_notifications(client: AsyncClient) -> None:
    payload = {
        "webhook_url": "https://ntfy.sh/test",
        "on_failure": True,
        "on_recovery": False,
        "on_backoff": True,
    }
    with patch("sshfs_keeper.config.AppConfig.save"):
        async with client as c:
            r = await c.put("/api/notifications", json=payload)
    assert r.status_code == 200
    assert r.json()["updated"] is True
    assert "HX-Trigger" in r.headers


async def test_put_notifications_clears_webhook(client: AsyncClient) -> None:
    payload = {"webhook_url": "", "on_failure": True, "on_recovery": True, "on_backoff": False}
    with patch("sshfs_keeper.config.AppConfig.save"):
        async with client as c:
            r = await c.put("/api/notifications", json=payload)
    assert r.status_code == 200
    # empty string should be stored as None
    from sshfs_keeper import api as _api
    assert _api._config.notifications.webhook_url is None


# ------------------------------------------------------------------
# Sync log endpoint
# ------------------------------------------------------------------

async def test_sync_log_endpoint(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/syncs/bak/log")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "bak"
    assert "lines" in data


async def test_sync_log_endpoint_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/syncs/nonexistent/log")
    assert r.status_code == 404


# ------------------------------------------------------------------
# Unmount endpoint
# ------------------------------------------------------------------

async def test_unmount_endpoint(client: AsyncClient) -> None:
    with patch("sshfs_keeper.mount.unmount", new=AsyncMock(return_value=True)):
        async with client as c:
            r = await c.post("/api/mounts/nas/unmount")
    assert r.status_code == 200
    assert r.json()["success"] is True


async def test_unmount_endpoint_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.post("/api/mounts/ghost/unmount")
    assert r.status_code == 404


# ------------------------------------------------------------------
# HTMX fragment endpoints
# ------------------------------------------------------------------

async def test_fragment_mounts(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/mounts")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_fragment_mount_card_single(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/mounts/nas")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "card-nas" in r.text


async def test_fragment_mount_card_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/mounts/ghost")
    assert r.status_code == 404


async def test_fragment_syncs(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/syncs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_fragment_sync_card_single(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/syncs/bak")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "sync-card-bak" in r.text


async def test_fragment_sync_card_not_found(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/syncs/ghost")
    assert r.status_code == 404


async def test_fragment_keys(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/fragments/keys")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ------------------------------------------------------------------
# HX-Trigger toast headers
# ------------------------------------------------------------------

async def test_remount_returns_hx_trigger(client: AsyncClient) -> None:
    with patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(True, None))):
        async with client as c:
            r = await c.post("/api/mounts/nas/remount")
    assert r.status_code == 200
    assert "HX-Trigger" in r.headers
    import json as _json
    trigger = _json.loads(r.headers["HX-Trigger"])
    assert "showToast" in trigger


async def test_delete_mount_returns_hx_trigger(client: AsyncClient) -> None:
    async with client as c:
        r = await c.delete("/api/mounts/nas")
    assert r.status_code == 200
    assert "HX-Trigger" in r.headers


async def test_sync_log_html_when_htmx_request(client: AsyncClient) -> None:
    """When HX-Request header is present, sync log returns plain text (not JSON)."""
    async with client as c:
        r = await c.get("/api/syncs/bak/log", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_sync_log_json_without_htmx(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/syncs/bak/log")
    assert r.status_code == 200
    assert r.json()["name"] == "bak"


# ------------------------------------------------------------------
# Monitor event listener
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_listener_called_on_recovery() -> None:
    cfg = AppConfig(
        daemon=DaemonConfig(remount_delay=0),
        mounts=[MountConfig(name="t", remote="u@h:/p", local="/mnt/t")],
    )
    m = Monitor(cfg)
    received: list[dict] = []  # type: ignore[type-arg]
    m.add_event_listener(received.append)

    with (
        patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(True, None))),
    ):
        await m._remount(m.states["t"])

    assert any(e["event"] == "mount_healthy" for e in received)


@pytest.mark.asyncio
async def test_event_listener_called_on_failure() -> None:
    cfg = AppConfig(
        daemon=DaemonConfig(remount_delay=0),
        mounts=[MountConfig(name="t", remote="u@h:/p", local="/mnt/t")],
    )
    m = Monitor(cfg)
    received: list[dict] = []  # type: ignore[type-arg]
    m.add_event_listener(received.append)

    with patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(False, "sshfs failed"))):
        await m._remount(m.states["t"])

    assert any(e["event"] == "mount_error" for e in received)


# ------------------------------------------------------------------
# mount_duration_seconds
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mount_duration_recorded() -> None:
    cfg = AppConfig(
        daemon=DaemonConfig(remount_delay=0),
        mounts=[MountConfig(name="t", remote="u@h:/p", local="/mnt/t")],
    )
    m = Monitor(cfg)

    with patch("sshfs_keeper.monitor.mnt.mount", new=AsyncMock(return_value=(True, None))):
        await m._remount(m.states["t"])

    assert m.states["t"].mount_duration_seconds is not None
    assert m.states["t"].mount_duration_seconds >= 0


def test_snapshot_includes_duration() -> None:
    from sshfs_keeper.monitor import MountState
    m = _make_monitor()
    m.states["nas"].mount_duration_seconds = 1.23
    m.states["nas"].status = MountStatus.UNMOUNTED  # not healthy so no usage call

    snap = m.get_snapshot()
    assert snap[0]["mount_duration_seconds"] == 1.23


# ------------------------------------------------------------------
# notify.send_webhook — HTTP path
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_webhook_posts_payload() -> None:
    from sshfs_keeper import notify

    captured: list[tuple] = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            captured.append((url, json))
            return FakeResp()

    with patch("httpx.AsyncClient", return_value=FakeClient()):
        await notify.send_webhook("http://hook", "failure", "nas", "err")

    assert len(captured) == 1
    url, payload = captured[0]
    assert url == "http://hook"
    assert payload["event"] == "failure"
    assert payload["mount"] == "nas"
    assert payload["error"] == "err"
    assert "timestamp" in payload


@pytest.mark.asyncio
async def test_send_webhook_handles_http_error() -> None:
    """HTTP errors are logged, not raised."""
    from sshfs_keeper import notify
    import httpx

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            raise httpx.HTTPError("boom")

    with patch("httpx.AsyncClient", return_value=FakeClient()):
        # must not raise
        await notify.send_webhook("http://hook", "recovery", "nas")


# ------------------------------------------------------------------
# Mount unmount path coverage
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unmount_tries_fusermount() -> None:
    from sshfs_keeper.mount import unmount
    from sshfs_keeper.config import MountConfig

    cfg = MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas")

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("sshfs_keeper.mount.IS_MACOS", False):
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await unmount(cfg)

    assert result is True


@pytest.mark.asyncio
async def test_unmount_returns_false_when_all_fail() -> None:
    from sshfs_keeper.mount import unmount
    from sshfs_keeper.config import MountConfig

    cfg = MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas")

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"not found"))

    with patch("sshfs_keeper.mount.IS_MACOS", False):
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await unmount(cfg)

    assert result is False


@pytest.mark.asyncio
async def test_mount_sshfs_failure() -> None:
    from sshfs_keeper.mount import mount
    from sshfs_keeper.config import MountConfig

    cfg = MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas")
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Connection refused"))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        result = await mount(cfg)

    ok, err = result
    assert ok is False
    assert err


@pytest.mark.asyncio
async def test_mount_sshfs_file_not_found() -> None:
    from sshfs_keeper.mount import mount
    from sshfs_keeper.config import MountConfig

    cfg = MountConfig(name="nas", remote="u@h:/p", local="/mnt/nas")

    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await mount(cfg)

    ok, err = result
    assert ok is False
    assert err


@pytest.mark.asyncio
async def test_mount_rclone_failure() -> None:
    from sshfs_keeper.mount import mount
    from sshfs_keeper.config import MountConfig

    cfg = MountConfig(name="cloud", remote="u@h:/p", local="/mnt/cloud", mount_tool="rclone")
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"rclone error"))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        result = await mount(cfg)

    ok, err = result
    assert ok is False
    assert err


# ------------------------------------------------------------------
# _cmd_syncs CLI
# ------------------------------------------------------------------

def test_cmd_syncs_prints_table(capsys) -> None:
    from sshfs_keeper.main import _cmd_syncs
    from types import SimpleNamespace

    fake_resp = MagicMock()
    fake_resp.json.return_value = [
        {
            "name": "bak",
            "status": "ok",
            "run_count": 3,
            "fail_count": 0,
            "next_run_in": 120,
            "last_error": None,
        }
    ]
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=fake_resp):
        _cmd_syncs(SimpleNamespace(port=None, api_key=None, trigger=None))

    out = capsys.readouterr().out
    assert "bak" in out


def test_cmd_syncs_trigger(capsys) -> None:
    from sshfs_keeper.main import _cmd_syncs
    from types import SimpleNamespace

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"triggered": True}
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=fake_resp):
        _cmd_syncs(SimpleNamespace(port=None, api_key=None, trigger="bak"))

    out = capsys.readouterr().out
    assert "True" in out or "triggered" in out.lower()


def test_cmd_syncs_no_jobs(capsys) -> None:
    from sshfs_keeper.main import _cmd_syncs
    from types import SimpleNamespace

    fake_resp = MagicMock()
    fake_resp.json.return_value = []
    fake_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=fake_resp):
        _cmd_syncs(SimpleNamespace(port=None, api_key=None, trigger=None))

    out = capsys.readouterr().out
    assert "No sync" in out


# ------------------------------------------------------------------
# API: GET /api/syncs
# ------------------------------------------------------------------

async def test_api_list_syncs(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/syncs")
    assert r.status_code == 200
    # Returns list of sync states
    data = r.json()
    assert isinstance(data, dict) or isinstance(data, list)
    items = data["syncs"] if isinstance(data, dict) else data
    assert items[0]["name"] == "bak"


# ------------------------------------------------------------------
# _parse_rclone_stats
# ------------------------------------------------------------------

def test_parse_rclone_stats_gib() -> None:
    from sshfs_keeper.sync import _parse_rclone_stats

    output = (
        "Transferred:   1.234 GiB / 5.000 GiB, 25%, 100 MiB/s, ETA 40s\n"
        "Transferred:   42 / 100, 42%\n"
    )
    bytes_sent, files_xfr = _parse_rclone_stats(output)
    assert files_xfr == 42
    assert bytes_sent is not None and bytes_sent > 0


def test_parse_rclone_stats_empty() -> None:
    from sshfs_keeper.sync import _parse_rclone_stats

    bytes_sent, files_xfr = _parse_rclone_stats("")
    assert bytes_sent is None
    assert files_xfr is None
