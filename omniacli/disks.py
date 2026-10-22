# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The free space of this machine's disks, and what OmniaCLI keeps on each.

The disks are the mounted filesystems of a real disk or network share type
(DISK_TYPES): snap squashfs images, overlays, tmpfs and the kernel's own
filesystems are left out. A disk mounted more than once (a bind mount) is
listed once, at its shortest mount point. A path is placed on the disk with
the longest mount point that holds it, by name only: no permission is needed
on the path, and it does not need to exist yet.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .config import Settings

DISK_TYPES = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "nfs", "nfs4", "cifs", "smb3",
}
# free space under these percents shows as a warning, then as an error
WARN_PERCENT = 15
ERROR_PERCENT = 5
TIMEOUT = 10  # seconds: a network share that does not answer is left out


def _unescape(field: str) -> str:
    """/proc/mounts writes space, tab, newline and backslash as octal escapes."""
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(code, char)
    return field


def mounts(path: Path = Path("/proc/self/mounts")) -> list[tuple[str, str, str]]:
    """[(device, mount point, type)] of the disks, one per device."""
    found: dict[str, tuple[str, str, str]] = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[2] not in DISK_TYPES:
            continue
        device, point = _unescape(fields[0]), _unescape(fields[1])
        if device not in found or len(point) < len(found[device][1]):
            found[device] = (device, point, fields[2])
    return sorted(found.values(), key=lambda m: m[1])


def known_paths(settings: Settings) -> list[tuple[str, str]]:
    """[(what, path)] that OmniaCLI and the live instances write to."""
    paths = [("backups", str(settings.backup_dir)), ("OmniaCLI jobs", str(settings.jobs_dir))]
    for inst in settings.all_instances():
        # what the instance's type writes to (Odoo: the filestore)
        paths += [(f"{inst.key} {what}", path) for what, path in inst.plugin.disk_paths(inst)]
        paths.append((f"{inst.key} log", str(inst.log_file)))
    return paths


def _holder(path: str, points: list[str]) -> str | None:
    """The longest mount point that holds path."""
    path = os.path.normpath(os.path.abspath(path))
    best = None
    for point in points:
        inside = path == point or path.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) > len(best)):
            best = point
    return best


def _usage(point: str) -> dict:
    st = os.statvfs(point)
    total = st.f_blocks * st.f_frsize
    # f_bavail: what a user who is not root may still write
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    return {"total": total, "used": used, "free": free}


async def disks(settings: Settings) -> list[dict]:
    """[{device, mount, type, total, used, free, free_percent, level, holds}]."""
    found = await asyncio.to_thread(mounts)
    points = [m[1] for m in found]
    holds: dict[str, list[str]] = {}
    for what, path in await asyncio.to_thread(known_paths, settings):
        point = _holder(path, points)
        if point:
            holds.setdefault(point, []).append(what)
    result = []
    for device, point, kind in found:
        try:
            usage = await asyncio.wait_for(asyncio.to_thread(_usage, point), TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            continue
        if not usage["total"]:
            continue
        percent = round(usage["free"] / usage["total"] * 100, 1)
        level = "error" if percent < ERROR_PERCENT else "warn" if percent < WARN_PERCENT else "ok"
        result.append({"device": device, "mount": point, "type": kind, **usage,
                       "free_percent": percent, "level": level, "holds": holds.get(point, [])})
    return result
