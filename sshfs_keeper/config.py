"""Configuration loading and models."""

import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "sshfs-keeper"
KEYS_DIR = CONFIG_DIR / "keys"

CONFIG_SEARCH_PATHS = [
    Path.home() / ".config" / "sshfs-keeper" / "config.toml",
    Path("config.toml"),
]


@dataclass
class MountConfig:
    name: str
    remote: str
    local: str
    options: str = "cache=yes,compression=yes,ServerAliveInterval=15,ServerAliveCountMax=3,reconnect"
    identity: Optional[str] = None
    identity_passphrase: Optional[str] = None
    enabled: bool = True
    mount_tool: str = "sshfs"  # "sshfs" | "rclone"


@dataclass
class DaemonConfig:
    check_interval: int = 30
    remount_delay: int = 5
    max_retries: int = 3
    backoff_base: int = 60
    log_level: str = "INFO"
    log_file: Optional[str] = None
    json_logs: bool = False


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    api_key: Optional[str] = None
    ssl_certfile: Optional[str] = None
    ssl_keyfile: Optional[str] = None


@dataclass
class NotificationsConfig:
    """Webhook notification settings.

    ``webhook_url`` accepts any HTTP POST endpoint (Slack incoming webhook,
    Discord webhook, ntfy.sh topic URL, etc.).  The payload is a JSON object
    with keys: ``event``, ``mount``, ``error``, ``timestamp``.
    """

    webhook_url: Optional[str] = None
    on_failure: bool = True
    on_recovery: bool = True
    on_backoff: bool = False


@dataclass
class SyncConfig:
    name: str
    source: str
    target: str
    interval: int = 3600
    options: str = "-az --delete --stats"
    identity: Optional[str] = None
    enabled: bool = True
    sync_tool: str = "rsync"  # "rsync" | "lsyncd"


@dataclass
class AppConfig:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    mounts: list[MountConfig] = field(default_factory=list)
    syncs: list[SyncConfig] = field(default_factory=list)
    _path: Optional[Path] = field(default=None, repr=False, compare=False)

    def save(self) -> None:
        """Write the current config back to disk as TOML."""
        import traceback as _tb

        # Guard against wiping mounts: refuse to save 0 mounts if disk has mounts
        if not self.mounts:
            path = self._path
            if path is None:
                for candidate in CONFIG_SEARCH_PATHS:
                    if candidate.exists():
                        path = candidate
                        break
            if path and path.exists():
                try:
                    with open(path, "rb") as _fh:
                        _raw = tomllib.load(_fh)
                    if _raw.get("mount"):
                        log.error(
                            "save() called with 0 mounts but on-disk config at %s has %d mount(s) — refusing to overwrite! Stack:\n%s",
                            path, len(_raw["mount"]), "".join(_tb.format_stack()),
                        )
                        return
                except Exception as e:
                    log.warning("Could not check on-disk config during save guard: %s", e)
            log.warning(
                "save() called with 0 mounts — stack:\n%s",
                "".join(_tb.format_stack()),
            )
        path = self._path
        if path is None:
            for candidate in CONFIG_SEARCH_PATHS:
                if candidate.exists():
                    path = candidate
                    break
        if path is None:
            path = CONFIG_DIR / "config.toml"

        path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        lines.append("[daemon]")
        lines.append(f"check_interval = {self.daemon.check_interval}")
        lines.append(f"remount_delay = {self.daemon.remount_delay}")
        lines.append(f"max_retries = {self.daemon.max_retries}")
        lines.append(f"backoff_base = {self.daemon.backoff_base}")
        lines.append(f'log_level = "{self.daemon.log_level}"')
        if self.daemon.log_file:
            lines.append(f'log_file = "{self.daemon.log_file}"')
        lines.append(f"json_logs = {'true' if self.daemon.json_logs else 'false'}")
        lines.append("")
        lines.append("[api]")
        lines.append(f'host = "{self.api.host}"')
        lines.append(f"port = {self.api.port}")
        if self.api.api_key:
            lines.append(f'api_key = "{self.api.api_key}"')
        if self.api.ssl_certfile:
            lines.append(f'ssl_certfile = "{self.api.ssl_certfile}"')
        if self.api.ssl_keyfile:
            lines.append(f'ssl_keyfile = "{self.api.ssl_keyfile}"')
        lines.append("")
        lines.append("[notifications]")
        if self.notifications.webhook_url:
            lines.append(f'webhook_url = "{self.notifications.webhook_url}"')
        lines.append(f"on_failure = {'true' if self.notifications.on_failure else 'false'}")
        lines.append(f"on_recovery = {'true' if self.notifications.on_recovery else 'false'}")
        lines.append(f"on_backoff = {'true' if self.notifications.on_backoff else 'false'}")
        lines.append("")
        for m in self.mounts:
            lines.append("[[mount]]")
            lines.append(f'name = "{m.name}"')
            lines.append(f'remote = "{m.remote}"')
            lines.append(f'local = "{m.local}"')
            lines.append(f'options = "{m.options}"')
            if m.identity:
                lines.append(f'identity = "{m.identity}"')
            if m.identity_passphrase:
                lines.append(f'identity_passphrase = "{m.identity_passphrase}"')
            lines.append(f"enabled = {'true' if m.enabled else 'false'}")
            if m.mount_tool != "sshfs":
                lines.append(f'mount_tool = "{m.mount_tool}"')
            lines.append("")

        for s in self.syncs:
            lines.append("[[sync]]")
            lines.append(f'name = "{s.name}"')
            lines.append(f'source = "{s.source}"')
            lines.append(f'target = "{s.target}"')
            lines.append(f"interval = {s.interval}")
            lines.append(f'options = "{s.options}"')
            if s.identity:
                lines.append(f'identity = "{s.identity}"')
            lines.append(f"enabled = {'true' if s.enabled else 'false'}")
            if s.sync_tool != "rsync":
                lines.append(f'sync_tool = "{s.sync_tool}"')
            lines.append("")

        content = "\n".join(lines)
        # Atomic write: write to a sibling temp file then os.replace() so a
        # mid-write SIGKILL never leaves a truncated/empty config.toml.
        import os as _os
        import tempfile as _tempfile
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
        try:
            _os.write(fd, content.encode())
            _os.fsync(fd)
            _os.close(fd)
            # Keep one backup of the previous config before replacing
            if path.exists():
                import shutil as _shutil
                _shutil.copy2(path, path.with_suffix(".bak"))
            _os.replace(tmp, path)
        except Exception:
            try:
                _os.close(fd)
            except OSError:
                pass
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise
        log.info("Config saved to %s", path)

    def validate(self) -> list[str]:
        """Return a list of validation error strings (empty = valid).

        Checks for duplicate names, unknown tool values, and missing required
        fields. Does not raise — callers decide how to handle errors.
        """
        errors: list[str] = []
        _valid_mount_tools = {"sshfs", "rclone"}
        _valid_sync_tools = {"rsync", "lsyncd", "rclone"}

        mount_names: set[str] = set()
        for m in self.mounts:
            if not m.name:
                errors.append("Mount has empty name")
            elif m.name in mount_names:
                errors.append(f"Duplicate mount name: '{m.name}'")
            else:
                mount_names.add(m.name)
            if not m.remote:
                errors.append(f"Mount '{m.name}': remote is required")
            if not m.local:
                errors.append(f"Mount '{m.name}': local is required")
            if m.mount_tool not in _valid_mount_tools:
                errors.append(f"Mount '{m.name}': unknown mount_tool '{m.mount_tool}' (valid: {sorted(_valid_mount_tools)})")

        sync_names: set[str] = set()
        for s in self.syncs:
            if not s.name:
                errors.append("Sync job has empty name")
            elif s.name in sync_names:
                errors.append(f"Duplicate sync name: '{s.name}'")
            else:
                sync_names.add(s.name)
            if not s.source:
                errors.append(f"Sync '{s.name}': source is required")
            if not s.target:
                errors.append(f"Sync '{s.name}': target is required")
            if s.sync_tool not in _valid_sync_tools:
                errors.append(f"Sync '{s.name}': unknown sync_tool '{s.sync_tool}' (valid: {sorted(_valid_sync_tools)})")
            if s.interval < 1:
                errors.append(f"Sync '{s.name}': interval must be >= 1 second")

        return errors

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        """Load config from *path* or the first existing search path."""
        if path is None:
            for candidate in CONFIG_SEARCH_PATHS:
                if candidate.exists():
                    path = candidate
                    break

        if path is None or not path.exists():
            log.warning("No config file found; using defaults with no mounts.")
            return cls()

        log.info("Loading config from %s", path)
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)

        daemon = DaemonConfig(**raw.get("daemon", {}))
        api = ApiConfig(**raw.get("api", {}))
        notifications = NotificationsConfig(**raw.get("notifications", {}))
        mounts = [MountConfig(**m) for m in raw.get("mount", [])]
        syncs = [SyncConfig(**s) for s in raw.get("sync", [])]

        # Auto-restore from backup if config was wiped but backup has mounts
        if not mounts:
            bak_path = path.with_suffix(".toml.bak")
            if bak_path.exists():
                try:
                    with open(bak_path, "rb") as fh:
                        bak_raw = tomllib.load(fh)
                    bak_mounts = [MountConfig(**m) for m in bak_raw.get("mount", [])]
                    if bak_mounts:
                        log.warning(
                            "Config at %s was wiped (0 mounts) but backup has %d mount(s) — auto-restoring",
                            path, len(bak_mounts),
                        )
                        import shutil as _shutil
                        _shutil.copy2(bak_path, path)
                        mounts = bak_mounts
                except Exception as e:
                    log.error("Failed to restore from backup: %s", e)

        obj = cls(daemon=daemon, api=api, notifications=notifications, mounts=mounts, syncs=syncs)
        obj._path = path
        return obj
