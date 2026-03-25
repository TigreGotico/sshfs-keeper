"""Mount/unmount/health-check operations for SSHFS mounts."""

import asyncio
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

from sshfs_keeper.config import MountConfig

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 10
IS_MACOS = platform.system() == "Darwin"


def _parse_proc_mounts() -> set[str]:
    """Return the set of local paths that are currently sshfs/rclone-mounted."""
    if IS_MACOS:
        return _parse_mounts_macos()
    return _parse_mounts_linux()


def is_autofs_managed(local_path: str) -> bool:
    """Return True if *local_path* or any of its ancestors is managed by autofs.

    When a mount point is under autofs control the daemon should skip its
    own remount logic and let autofs handle on-demand mounting.

    Args:
        local_path: Absolute local path to check.

    Returns:
        ``True`` if an autofs entry covers *local_path* or a parent directory.
    """
    if IS_MACOS:
        return False  # autofs behaviour on macOS differs; skip for now
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[2] == "autofs":
                    autofs_mount = parts[1].rstrip("/")
                    check = local_path.rstrip("/")
                    if check == autofs_mount or check.startswith(autofs_mount + "/"):
                        return True
    except OSError:
        pass
    return False


_FUSE_TYPES = {"fuse.sshfs", "fuse.rclone", "osxfuse", "macfuse"}
_FUSE_PREFIXES = ("sshfs#", "rclone:")


def _parse_mounts_linux() -> set[str]:
    mounted: set[str] = set()
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    device, mountpoint, fstype = parts[0], parts[1], parts[2]
                    if fstype in _FUSE_TYPES or any(device.startswith(p) for p in _FUSE_PREFIXES):
                        mounted.add(mountpoint)
    except OSError:
        pass
    return mounted


def _parse_mounts_macos() -> set[str]:
    """Parse `mount` output on macOS — sshfs entries look like:
    sshfs@user@host:/path on /local/path (osxfuse, ...)
    """
    mounted: set[str] = set()
    try:
        out = subprocess.check_output(["mount"], text=True, timeout=5)
        for line in out.splitlines():
            if line.startswith("sshfs@") or "osxfuse" in line or "macfuse" in line:
                # format: <device> on <mountpoint> (<options>)
                parts = line.split(" on ", 1)
                if len(parts) == 2:
                    mountpoint = parts[1].split(" (")[0].strip()
                    mounted.add(mountpoint)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return mounted


async def _probe_path(path: str) -> bool:
    """Return True if *path* is accessible (non-stale) within PROBE_TIMEOUT."""
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, os.statvfs, path),
            timeout=PROBE_TIMEOUT,
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def is_healthy(cfg: MountConfig) -> bool:
    """Return True if the mount is listed AND accessible."""
    if cfg.local not in _parse_proc_mounts():
        return False
    return await _probe_path(cfg.local)


async def unmount(cfg: MountConfig) -> bool:
    """Force-unmount. Uses fusermount3/fusermount on Linux, umount on macOS."""
    log.info("[%s] Unmounting %s", cfg.name, cfg.local)

    if IS_MACOS:
        candidates = [["umount", "-f", cfg.local], ["diskutil", "unmount", "force", cfg.local]]
    else:
        candidates = [["fusermount3", "-uz", cfg.local], ["fusermount", "-uz", cfg.local]]

    for cmd in candidates:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                return True
            log.debug("[%s] %s failed: %s", cfg.name, cmd[0], stderr.decode().strip())
        except (asyncio.TimeoutError, FileNotFoundError):
            continue

    log.warning("[%s] all unmount attempts failed", cfg.name)
    return False


def get_usage(local_path: str) -> Optional[dict[str, float]]:
    """Return filesystem usage for *local_path* or ``None`` if unavailable.

    Args:
        local_path: Absolute path to an SSHFS mount point.

    Returns:
        Dict with keys ``total_gb``, ``used_gb``, ``free_gb``, ``percent_used``,
        or ``None`` if ``os.statvfs`` raises.
    """
    try:
        st = os.statvfs(local_path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        gb = 1024 ** 3
        return {
            "total_gb": round(total / gb, 2),
            "used_gb": round(used / gb, 2),
            "free_gb": round(free / gb, 2),
            "percent_used": round(used / total * 100, 1) if total else 0.0,
        }
    except OSError:
        return None


def _ssh_remote_to_rclone(remote: str) -> str:
    """Convert an SSH-style remote ``user@host:/path`` to rclone inline SFTP format.

    If *remote* already contains a colon preceded by a letter (rclone named-remote
    format like ``myremote:path``) it is returned unchanged.

    Args:
        remote: SSH-style remote string or rclone remote string.

    Returns:
        rclone-compatible remote string such as ``:sftp,host=HOST,user=USER:PATH``.
    """
    import re
    # Already rclone format: e.g. "myremote:/path" or ":sftp,host=…"
    if remote.startswith(":") or re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*:", remote):
        return remote
    # SSH format: [user@]host:/path
    m = re.match(r"^(?:([^@]+)@)?([^:]+):(.*)$", remote)
    if not m:
        return remote
    user, host, path = m.group(1), m.group(2), m.group(3)
    user_part = f",user={user}" if user else ""
    return f":sftp,host={host}{user_part}:{path}"


async def mount(cfg: MountConfig) -> tuple[bool, Optional[str]]:
    """Mount via the configured mount_tool.

    Returns:
        ``(True, None)`` on success, ``(False, error_message)`` on failure.
    """
    if cfg.mount_tool == "rclone":
        return await _mount_rclone(cfg)
    return await _mount_sshfs(cfg)


async def _mount_sshfs(cfg: MountConfig) -> tuple[bool, Optional[str]]:
    """Mount via sshfs. Returns ``(success, error_or_None)``."""
    local_path = Path(cfg.local)
    local_path.mkdir(parents=True, exist_ok=True)

    cmd = ["sshfs", cfg.remote, cfg.local]

    if cfg.options:
        cmd += ["-o", cfg.options]

    if cfg.identity:
        cmd += ["-o", f"IdentityFile={cfg.identity}"]

    cmd += ["-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]

    # If a passphrase is configured, pre-load the key into ssh-agent
    if cfg.identity and cfg.identity_passphrase:
        await _add_key_to_agent(cfg.identity, cfg.identity_passphrase, cfg.name)

    log.info("[%s] Mounting (sshfs): %s", cfg.name, " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            err = stderr.decode().strip() or f"sshfs exited {proc.returncode}"
            log.warning("[%s] sshfs failed: %s", cfg.name, err)
            return False, err
        log.info("[%s] sshfs mount succeeded", cfg.name)
        return True, None
    except asyncio.TimeoutError:
        err = "sshfs timed out after 30s"
        log.warning("[%s] %s", cfg.name, err)
        return False, err
    except FileNotFoundError:
        err = "sshfs not found — install openssh-fuse or sshfs"
        log.warning("[%s] %s", cfg.name, err)
        return False, err


async def _mount_rclone(cfg: MountConfig) -> tuple[bool, Optional[str]]:
    """Mount via rclone. Returns ``(success, error_or_None)``.

    Converts SSH-style ``user@host:/path`` remotes to rclone inline SFTP format
    automatically. The ``options`` field is ignored (rclone uses its own flags).
    Use ``rclone config`` to set per-remote options.
    """
    local_path = Path(cfg.local)
    local_path.mkdir(parents=True, exist_ok=True)

    rclone_remote = _ssh_remote_to_rclone(cfg.remote)

    cmd = [
        "rclone", "mount",
        rclone_remote, cfg.local,
        "--daemon",
        "--allow-other",
        "--vfs-cache-mode", "writes",
    ]

    if cfg.identity:
        cmd += ["--sftp-key-file", cfg.identity]

    log.info("[%s] Mounting (rclone): %s", cfg.name, " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raw = stderr.decode().strip()
            # Provide actionable hints for common rclone failures
            if "allow_other" in raw or "fusermount" in raw.lower():
                err = "rclone: allow_other not permitted — add 'user_allow_other' to /etc/fuse.conf"
            elif raw:
                err = f"rclone: {raw}"
            else:
                err = f"rclone exited {proc.returncode}"
            log.warning("[%s] rclone mount failed: %s", cfg.name, err)
            return False, err
        log.info("[%s] rclone mount succeeded", cfg.name)
        return True, None
    except asyncio.TimeoutError:
        err = "rclone timed out after 30s"
        log.warning("[%s] %s", cfg.name, err)
        return False, err
    except FileNotFoundError:
        err = "rclone not found — install rclone"
        log.warning("[%s] %s", cfg.name, err)
        return False, err


async def _add_key_to_agent(identity: str, passphrase: str, mount_name: str) -> None:
    """Add *identity* key to ssh-agent using *passphrase* via ``ssh-add``.

    Args:
        identity: Path to the SSH private key file.
        passphrase: Passphrase for the key.
        mount_name: Mount name used in log messages.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh-add", identity,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=(passphrase + "\n").encode()), timeout=10
        )
        if proc.returncode != 0:
            log.warning("[%s] ssh-add failed: %s", mount_name, stderr.decode().strip())
        else:
            log.debug("[%s] ssh-add succeeded for %s", mount_name, identity)
    except (asyncio.TimeoutError, FileNotFoundError) as exc:
        log.warning("[%s] ssh-add error: %s", mount_name, exc)
