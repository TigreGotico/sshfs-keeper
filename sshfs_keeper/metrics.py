"""Prometheus-format metrics generator (no external library required)."""

from typing import Optional

from sshfs_keeper.monitor import Monitor, MountStatus
from sshfs_keeper.sync import SyncManager


def _gauge(name: str, labels: dict[str, str], value: float) -> str:
    """Format a single Prometheus gauge line.

    Args:
        name: Metric name.
        labels: Label key-value pairs.
        value: Metric value.

    Returns:
        Prometheus text-format line, e.g. ``foo{a="b"} 1.0``.
    """
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f'{name}{{{label_str}}} {value}'


def generate(monitor: Monitor, sync_manager: Optional[SyncManager]) -> str:
    """Return a Prometheus text-format metrics page.

    Args:
        monitor: Running :class:`~sshfs_keeper.monitor.Monitor` instance.
        sync_manager: Optional running :class:`~sshfs_keeper.sync.SyncManager` instance.

    Returns:
        Multi-line string in Prometheus exposition format (``text/plain; version=0.0.4``).
    """
    lines: list[str] = []

    # --- mount metrics ---
    lines.append("# HELP sshfs_keeper_mount_healthy 1 if mount is healthy, 0 otherwise")
    lines.append("# TYPE sshfs_keeper_mount_healthy gauge")
    lines.append("# HELP sshfs_keeper_mount_count Total successful mounts since daemon start")
    lines.append("# TYPE sshfs_keeper_mount_count counter")
    lines.append("# HELP sshfs_keeper_mount_retry_count Current consecutive retry count")
    lines.append("# TYPE sshfs_keeper_mount_retry_count gauge")
    lines.append("# HELP sshfs_keeper_mount_duration_seconds Last mount operation duration in seconds")
    lines.append("# TYPE sshfs_keeper_mount_duration_seconds gauge")

    for state in monitor.states.values():
        lbl = {"name": state.config.name}
        healthy = 1 if state.status == MountStatus.HEALTHY else 0
        lines.append(_gauge("sshfs_keeper_mount_healthy", lbl, healthy))
        lines.append(_gauge("sshfs_keeper_mount_count", lbl, state.mount_count))
        lines.append(_gauge("sshfs_keeper_mount_retry_count", lbl, state.retry_count))
        duration = getattr(state, "mount_duration_seconds", None)
        if duration is not None:
            lines.append(_gauge("sshfs_keeper_mount_duration_seconds", lbl, duration))

    if sync_manager:
        lines.append("")
        lines.append("# HELP sshfs_keeper_sync_run_count Total sync job runs since daemon start")
        lines.append("# TYPE sshfs_keeper_sync_run_count counter")
        lines.append("# HELP sshfs_keeper_sync_bytes_sent Bytes sent in last successful run")
        lines.append("# TYPE sshfs_keeper_sync_bytes_sent gauge")
        lines.append("# HELP sshfs_keeper_sync_fail_count Consecutive failure count (resets on success)")
        lines.append("# TYPE sshfs_keeper_sync_fail_count gauge")
        lines.append("# HELP sshfs_keeper_sync_last_duration_seconds Duration of last sync run in seconds")
        lines.append("# TYPE sshfs_keeper_sync_last_duration_seconds gauge")

        for state in sync_manager.states.values():
            lbl = {"name": state.config.name}
            lines.append(_gauge("sshfs_keeper_sync_run_count", lbl, state.run_count))
            lines.append(_gauge("sshfs_keeper_sync_fail_count", lbl, state.fail_count))
            if state.bytes_sent is not None:
                lines.append(_gauge("sshfs_keeper_sync_bytes_sent", lbl, state.bytes_sent))
            if state.last_duration is not None:
                lines.append(_gauge("sshfs_keeper_sync_last_duration_seconds", lbl, state.last_duration))

    lines.append("")  # trailing newline required by Prometheus spec
    return "\n".join(lines)
