# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The disks a restore into an instance writes to: what the plugin's
restore_room tells (the database's folder, the files', the folder where the
backup is unpacked) and the instance's backup folder (the zip), each placed
on its disk of this machine with the room free there.

A role whose folder is not known (PostgreSQL on another machine) is listed
as unknown: its disk is not checked. The dialogs add up, per disk, what the
backup needs there (templates/_room.html)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import disks

ROLES = ("backups", "temp", "database", "files")


async def room(paths: dict, freed: dict, notes: list) -> dict:
    """{"disks": [{"mount", "free", "roles"}], "unknown": [role], "freed",
    "notes"} for paths {role: folder or None}."""
    found = await asyncio.to_thread(disks.mounts)
    points = [m[1] for m in found]
    by_mount: dict[str, dict] = {}
    unknown = []
    for role in ROLES:
        path = paths.get(role)
        point = disks.holder(str(path), points) if path else None
        if point is None:
            unknown.append(role)
            continue
        if point not in by_mount:
            try:
                usage = await asyncio.wait_for(asyncio.to_thread(disks._usage, point), disks.TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                unknown.append(role)
                continue
            by_mount[point] = {"mount": point, "free": usage["free"], "roles": [], "paths": {}}
        by_mount[point]["roles"].append(role)
        by_mount[point]["paths"][role] = str(Path(path))
    return {"disks": list(by_mount.values()), "unknown": unknown,
            "freed": {k: int(v or 0) for k, v in (freed or {}).items()}, "notes": list(notes or [])}
