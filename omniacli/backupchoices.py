# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the backup dialog of an instance chose last time: the dialog opens
with it, and the ways to back up that have no dialog (a copy from a link, a
schedule, MCP, the copy of production to test) take from it the fields
they do not give, a checklist's ticks above all.

One file per instance, in its backup folder (the backups listed there are
*.zip): {"values": {key: value}, "shown": {checklist key: [rows shown]},
"user", "saved"}.

A checklist's rows ticked: those ticked last time; not those shown last time
and left unticked; a row not shown last time (new since) is ticked when its
size reaches the threshold, as the dialog ticks it."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

FILE_NAME = ".backup_choices.json"
MB = 1 << 20


def load(folder: Path) -> dict:
    """The last choice of the backup dialog of the instance whose backups
    are in folder; {} when there is none or it cannot be read."""
    try:
        data = json.loads((folder / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
        return {}
    shown = data.get("shown")
    data["shown"] = {k: [str(v) for v in rows] for k, rows in shown.items() if isinstance(rows, list)} \
        if isinstance(shown, dict) else {}
    return data


def save(folder: Path, values: dict, shown: dict, user: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = {"values": values, "shown": shown, "user": user,
            "saved": datetime.datetime.now().isoformat(timespec="seconds")}
    tmp = folder / (FILE_NAME + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, folder / FILE_NAME)


def threshold_bytes(megabytes) -> int | None:
    """A threshold in megabytes as bytes; None when it is not a number."""
    try:
        value = float(megabytes)
    except (TypeError, ValueError):
        return None
    return int(value * MB) if value >= 0 else None


def ticked(rows: list[dict], remembered: dict, key: str, threshold_mb) -> list[str]:
    """The values of rows ticked for checklist key, by the rule above."""
    chosen = (remembered.get("values") or {}).get(key)
    chosen = set(chosen) if isinstance(chosen, list) else set()
    shown = set((remembered.get("shown") or {}).get(key) or [])
    limit = threshold_bytes(threshold_mb)
    out = []
    for row in rows:
        value = row["value"]
        if value in chosen:
            out.append(value)
        elif value not in shown and limit is not None and int(row.get("size") or 0) >= limit:
            out.append(value)
    return out
