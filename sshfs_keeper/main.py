"""Entry point — wires config, monitor, and FastAPI together."""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from sshfs_keeper.config import AppConfig, CONFIG_DIR
from sshfs_keeper.monitor import Monitor
from sshfs_keeper.sync import SyncManager, SyncState
from sshfs_keeper.transfer import TransferManager
from sshfs_keeper import api as api_module

_PID_FILE = CONFIG_DIR / "daemon.pid"


def _setup_logging(
    level: str,
    log_file: Optional[str] = None,
    json_logs: bool = False,
) -> None:
    """Configure root logger with optional rotating file handler and JSON formatting.

    Args:
        level: Log level string (e.g. ``"INFO"``).
        log_file: Optional path to a log file. If set, a 5 MB rotating file
            handler (3 backups) is added alongside the console handler.
        json_logs: When ``True`` use ``python-json-logger`` for structured JSON
            output on all handlers. Each log record is a single JSON object with
            keys ``asctime``, ``levelname``, ``name``, and ``message``.
    """
    int_level = getattr(logging, level.upper(), logging.INFO)

    if json_logs:
        try:
            from pythonjsonlogger.json import JsonFormatter  # type: ignore[import-untyped]
            formatter: logging.Formatter = JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        except ImportError:  # pragma: no cover
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3
            )
        )
    for h in handlers:
        h.setFormatter(formatter)

    logging.basicConfig(level=int_level, handlers=handlers, force=True)


def _write_pid() -> None:
    """Write the current PID to :data:`_PID_FILE`."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _remove_pid() -> None:
    """Remove :data:`_PID_FILE` if it exists."""
    try:
        _PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _read_daemon_pid() -> Optional[int]:
    """Return the PID from :data:`_PID_FILE` or ``None`` if absent/invalid.

    Returns:
        Integer PID or ``None``.
    """
    try:
        return int(_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


async def _run(config: AppConfig) -> None:
    """Run the full daemon: monitor + sync manager + API server.

    Args:
        config: Loaded application configuration.
    """
    monitor = Monitor(config)
    sync_states = {s.name: SyncState(config=s) for s in config.syncs}
    sync_manager = SyncManager(sync_states, daemon_cfg=config.daemon)
    transfer_manager = TransferManager()
    api_module.setup(monitor, config, sync_manager, transfer_manager)

    await monitor.start()
    await sync_manager.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    reload_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    def _sighup_handler() -> None:
        reload_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)
    loop.add_signal_handler(signal.SIGHUP, _sighup_handler)

    server_config = uvicorn.Config(
        app=api_module.app,
        host=config.api.host,
        port=config.api.port,
        log_level="warning",
        access_log=True,
        ssl_certfile=config.api.ssl_certfile or None,
        ssl_keyfile=config.api.ssl_keyfile or None,
    )
    server = uvicorn.Server(server_config)
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    log = logging.getLogger(__name__)

    async def _reload_watcher() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
            if reload_event.is_set():
                reload_event.clear()
                _do_reload(config, monitor)

    reload_task = asyncio.create_task(_reload_watcher(), name="reload-watcher")

    await stop_event.wait()
    log.info("Shutting down…")

    reload_task.cancel()
    server.should_exit = True
    await server_task
    await monitor.stop()
    await sync_manager.stop()


def _do_reload(config: AppConfig, monitor: Monitor) -> None:
    """Re-read config from disk and update the monitor's mount states in-place.

    New mounts are added; removed mounts are deleted; existing mounts retain
    their runtime state so retry counts and backoff are preserved.

    Args:
        config: Currently running :class:`~sshfs_keeper.config.AppConfig` (mutated in-place).
        monitor: Running :class:`~sshfs_keeper.monitor.Monitor` (states dict mutated in-place).
    """
    log = logging.getLogger(__name__)
    try:
        fresh = AppConfig.load(config._path)
    except Exception as exc:
        log.warning("Config reload failed: %s", exc)
        return

    # Update daemon settings
    config.daemon = fresh.daemon
    config.api = fresh.api
    config.notifications = fresh.notifications

    # Add new mounts
    fresh_names = {m.name for m in fresh.mounts}
    existing_names = set(monitor.states.keys())
    for mc in fresh.mounts:
        if mc.name not in existing_names:
            from sshfs_keeper.monitor import MountState
            monitor.states[mc.name] = MountState(config=mc)
            log.info("[reload] added mount '%s'", mc.name)

    # Remove deleted mounts
    for name in list(existing_names):
        if name not in fresh_names:
            del monitor.states[name]
            log.info("[reload] removed mount '%s'", name)

    config.mounts = fresh.mounts
    config.syncs = fresh.syncs
    log.info("Config reloaded from disk")


def _cmd_start(args: argparse.Namespace) -> None:
    """Start the daemon process.

    Args:
        args: Parsed CLI arguments.
    """
    config = AppConfig.load(args.config)
    if args.check_interval is not None:
        config.daemon.check_interval = args.check_interval
    if args.port is not None:
        config.api.port = args.port

    _setup_logging(config.daemon.log_level, config.daemon.log_file, config.daemon.json_logs)
    log = logging.getLogger(__name__)

    errors = config.validate()
    if errors:
        for e in errors:
            log.error("Config error: %s", e)
        print(f"Config has {len(errors)} error(s) — fix before starting:", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        sys.exit(1)

    log.info(
        "sshfs-keeper starting — %d mount(s), web UI on http://%s:%d",
        len(config.mounts),
        config.api.host,
        config.api.port,
    )

    _write_pid()
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass
    finally:
        _remove_pid()
    sys.exit(0)


def _cmd_status(args: argparse.Namespace) -> None:
    """Print mount status from the running daemon.

    Args:
        args: Parsed CLI arguments (uses ``--port`` override if provided).
    """
    import httpx

    port = args.port or 8765
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        resp = httpx.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    mounts = data.get("mounts", [])
    if not mounts:
        print("No mounts configured.")
        return

    col_w = [24, 12, 10, 8, 20]
    hdr = f"{'NAME':<{col_w[0]}}{'STATUS':<{col_w[1]}}{'RETRIES':<{col_w[2]}}{'COUNT':<{col_w[3]}}{'LAST ERROR'}"
    print(hdr)
    print("-" * sum(col_w))
    for m in mounts:
        err = (m.get("last_error") or "")[:30]
        print(
            f"{m['name']:<{col_w[0]}}{m['status']:<{col_w[1]}}{m['retry_count']:<{col_w[2]}}{m['mount_count']:<{col_w[3]}}{err}"
        )


def _cmd_syncs(args: argparse.Namespace) -> None:
    """List sync job status or trigger a named job via the running daemon API.

    Args:
        args: Parsed CLI arguments. ``args.trigger`` is an optional job name to
            run immediately; ``--port`` overrides the API port.
    """
    import httpx

    port = args.port or 8765
    headers = {"X-API-Key": args.api_key} if args.api_key else {}

    if args.trigger:
        url = f"http://127.0.0.1:{port}/api/syncs/{args.trigger}/trigger"
        try:
            resp = httpx.post(url, headers=headers, timeout=10)
            resp.raise_for_status()
            print(resp.json())
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    url = f"http://127.0.0.1:{port}/api/syncs"
    try:
        resp = httpx.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        syncs = resp.json()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not syncs:
        print("No sync jobs configured.")
        return

    col_w = [24, 10, 8, 8, 12, 20]
    hdr = (
        f"{'NAME':<{col_w[0]}}{'STATUS':<{col_w[1]}}{'RUNS':<{col_w[2]}}"
        f"{'FAILS':<{col_w[3]}}{'NEXT IN':<{col_w[4]}}{'LAST ERROR'}"
    )
    print(hdr)
    print("-" * sum(col_w))
    for s in syncs:
        next_in = s.get("next_run_in")
        next_str = f"{int(next_in)}s" if next_in is not None else "—"
        err = (s.get("last_error") or "")[:30]
        print(
            f"{s['name']:<{col_w[0]}}{s['status']:<{col_w[1]}}{s['run_count']:<{col_w[2]}}"
            f"{s['fail_count']:<{col_w[3]}}{next_str:<{col_w[4]}}{err}"
        )


def _cmd_mount(args: argparse.Namespace) -> None:
    """Trigger a remount of the named mount via the running daemon API.

    Args:
        args: Parsed CLI arguments; uses ``args.name`` and optional ``--port``.
    """
    import httpx

    port = args.port or 8765
    url = f"http://127.0.0.1:{port}/api/mounts/{args.name}/remount"
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    try:
        resp = httpx.post(url, headers=headers, timeout=10)
        resp.raise_for_status()
        print(resp.json())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_unmount(args: argparse.Namespace) -> None:
    """Force-unmount the named mount via the running daemon API.

    Args:
        args: Parsed CLI arguments; uses ``args.name`` and optional ``--port``.
    """
    import httpx

    port = args.port or 8765
    url = f"http://127.0.0.1:{port}/api/mounts/{args.name}/unmount"
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    try:
        resp = httpx.post(url, headers=headers, timeout=10)
        resp.raise_for_status()
        print(resp.json())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_install_service(args: argparse.Namespace) -> None:
    """Write an OS-appropriate service unit for the sshfs-keeper daemon.

    - **Linux**: systemd user unit written to
      ``~/.config/systemd/user/sshfs-keeper.service``.
      Enable with ``systemctl --user enable --now sshfs-keeper``.
    - **macOS**: launchd plist written to
      ``~/Library/LaunchAgents/com.sshfs-keeper.plist``.
      Load with ``launchctl load ~/Library/LaunchAgents/com.sshfs-keeper.plist``.
    - **Windows**: batch script written to
      ``%APPDATA%\\sshfs-keeper\\install-service.bat``.
      Requires `NSSM <https://nssm.cc/>`_; the script calls ``nssm install``.

    Args:
        args: Parsed CLI arguments (unused beyond the parsed namespace).
    """
    import shutil
    import platform as _platform

    exe = shutil.which("sshfs-keeper") or sys.executable + " -m sshfs_keeper.main"
    system = _platform.system()

    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / "sshfs-keeper.service"
        unit_path.write_text(
            "[Unit]\n"
            "Description=sshfs-keeper — self-healing SSHFS mount daemon\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exe} start\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        print(f"Wrote {unit_path}")
        print("Enable with: systemctl --user enable --now sshfs-keeper")

    elif system == "Darwin":
        launch_dir = Path.home() / "Library" / "LaunchAgents"
        launch_dir.mkdir(parents=True, exist_ok=True)
        plist_path = launch_dir / "com.sshfs-keeper.plist"
        plist_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            '  <key>Label</key><string>com.sshfs-keeper</string>\n'
            '  <key>ProgramArguments</key>\n'
            '  <array>\n'
            f'    <string>{exe}</string>\n'
            '    <string>start</string>\n'
            '  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            '  <key>StandardErrorPath</key>'
            '<string>/tmp/sshfs-keeper.err</string>\n'
            '</dict>\n'
            '</plist>\n'
        )
        print(f"Wrote {plist_path}")
        print(f"Load with: launchctl load {plist_path}")

    elif system == "Windows":
        bat_dir = Path(os.environ.get("APPDATA", Path.home())) / "sshfs-keeper"
        bat_dir.mkdir(parents=True, exist_ok=True)
        bat_path = bat_dir / "install-service.bat"
        bat_path.write_text(
            "@echo off\n"
            "REM Requires NSSM: https://nssm.cc/\n"
            f'nssm install sshfs-keeper "{exe}" start\n'
            "nssm set sshfs-keeper AppRestartDelay 10000\n"
            "nssm start sshfs-keeper\n"
            'echo Service installed. Run "nssm edit sshfs-keeper" to adjust.\n'
        )
        print(f"Wrote {bat_path}")
        print("Run install-service.bat as Administrator (requires NSSM).")

    else:
        print(f"Unsupported platform: {system}", file=sys.stderr)
        sys.exit(1)


def _cmd_reload(_args: argparse.Namespace) -> None:
    """Send SIGHUP to the running daemon to trigger a config reload.

    Args:
        _args: Unused parsed CLI arguments.
    """
    pid = _read_daemon_pid()
    if pid is None:
        print("No running daemon found (no PID file).", file=sys.stderr)
        sys.exit(1)
    try:
        os.kill(pid, signal.SIGHUP)
        print(f"Sent SIGHUP to daemon PID {pid}")
    except ProcessLookupError:
        print(f"No process with PID {pid}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """CLI entry point — dispatches to subcommand handlers."""
    parser = argparse.ArgumentParser(description="sshfs-keeper daemon and control CLI")
    parser.add_argument("-c", "--config", type=Path, default=None, help="Path to config.toml")
    parser.add_argument("--port", type=int, default=None, help="API port (default: 8765)")
    parser.add_argument("--api-key", default=None, help="X-API-Key header value for write commands")

    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start the daemon (default if no subcommand)")
    start_p.add_argument("--check-interval", type=int, default=None, help="Override check_interval")

    sub.add_parser("status", help="Show mount status from the running daemon")
    sub.add_parser("reload", help="Send SIGHUP to reload config without restarting")

    mount_p = sub.add_parser("mount", help="Trigger a remount for a named mount")
    mount_p.add_argument("name", help="Mount name")

    unmount_p = sub.add_parser("unmount", help="Force-unmount a named mount")
    unmount_p.add_argument("name", help="Mount name")

    syncs_p = sub.add_parser("syncs", help="List sync job status or trigger a job")
    syncs_p.add_argument("--trigger", metavar="NAME", default=None, help="Trigger a named sync job immediately")

    sub.add_parser("install-service", help="Write a systemd/launchd/NSSM service unit for the current OS")

    args = parser.parse_args()

    if args.command is None or args.command == "start":
        # Ensure start_p defaults are available even when invoked without subcommand
        if not hasattr(args, "check_interval"):
            args.check_interval = None
        _cmd_start(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "mount":
        _cmd_mount(args)
    elif args.command == "unmount":
        _cmd_unmount(args)
    elif args.command == "reload":
        _cmd_reload(args)
    elif args.command == "syncs":
        _cmd_syncs(args)
    elif args.command == "install-service":
        _cmd_install_service(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
