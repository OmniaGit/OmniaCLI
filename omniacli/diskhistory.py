# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The daily history of this machine's disks, and when each will be full.

Once a day (the first check of a new day, and at start when today is not
recorded yet) every disk of the disks panel is recorded: its used and free
space and its size, by mount point (a device may change its name). The
history is kept KEEP_DAYS, in one JSON file next to the jobs.

The forecast is the least-squares line of the used space over the last
WINDOW_DAYS days (at least MIN_DAYS days of them): the day it reaches the
space a service may fill, used + free (free as a user who is not root sees
it: ext4 keeps 5% for root).

The workspaces' disks are recorded the same way, apart from the machine's
(the Disks page lists both, the publisher sends the machine's only): the
space their image takes, and its capacity, the workspace's disk_gb (its
image's size when it has none). Their forecast is the day the image fills
the workspace's limit.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from pathlib import Path

from . import disks as _disks
from .config import Settings

_logger = logging.getLogger(__name__)

KEEP_DAYS = 3 * 366  # three years
WINDOW_DAYS = 90
MIN_DAYS = 7
CHECK_EVERY = 600  # seconds between two looks at the date
# a forecast further away is no date (and past what datetime.date holds)
HORIZON_DAYS = 100 * 366


class DiskHistory:
    """{mount: {device, type, points: [[day, used, free, total]]}} in a JSON
    file, and {workspace: {points}} the same way."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.disks: dict[str, dict] = {}
        self.workspaces: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data.get("disks"), dict):
                self.disks = data["disks"]
            if isinstance(data.get("workspaces"), dict):
                self.workspaces = data["workspaces"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            _logger.warning("Disk history %s not read, starting a new one: %s", self.path, exc)

    @staticmethod
    def _has(entries: dict[str, dict], day: datetime.date) -> bool:
        text = day.isoformat()
        return any(e["points"] and e["points"][-1][0] == text for e in entries.values())

    def recorded(self, day: datetime.date) -> bool:
        """Whether day is recorded (any disk has it)."""
        return self._has(self.disks, day)

    def workspaces_recorded(self, day: datetime.date) -> bool:
        """Whether day is recorded for the workspaces (any has it)."""
        return self._has(self.workspaces, day)

    @staticmethod
    def _add(entries: dict[str, dict], key: str, point: list, day: datetime.date) -> dict:
        entry = entries.setdefault(key, {"points": []})
        entry["points"] = sorted([p for p in entry["points"] if p[0] != day.isoformat()] + [point])
        return entry

    def record(self, found: list[dict], day: datetime.date) -> None:
        """The disks found (disks.disks) as day's points; older than KEEP_DAYS dropped."""
        for disk in found:
            entry = self._add(self.disks, disk["mount"], [day.isoformat(), disk["used"], disk["free"], disk["total"]], day)
            entry["device"], entry["type"] = disk["device"], disk["type"]
        self._save(day)

    def record_workspaces(self, found: list[dict], day: datetime.date) -> None:
        """The workspaces' disks found ([{name, used, capacity}]) as day's points."""
        for disk in found:
            free = max(disk["capacity"] - disk["used"], 0)
            self._add(self.workspaces, disk["name"], [day.isoformat(), disk["used"], free, disk["capacity"]], day)
        self._save(day)

    def _save(self, day: datetime.date) -> None:
        oldest = (day - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        for entries in (self.disks, self.workspaces):
            for entry in entries.values():
                entry["points"] = [p for p in entry["points"] if p[0] >= oldest]
        self.disks = {m: e for m, e in self.disks.items() if e["points"]}
        self.workspaces = {m: e for m, e in self.workspaces.items() if e["points"]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"disks": self.disks, "workspaces": self.workspaces}), encoding="utf-8")
        tmp.replace(self.path)

    def series(self) -> list[dict]:
        """Every disk with its points and its forecast, by mount point."""
        return [
            {"mount": mount, "device": e.get("device", ""), "type": e.get("type", ""),
             "points": [{"day": p[0], "used": p[1], "free": p[2], "total": p[3]} for p in e["points"]],
             "forecast": forecast(e["points"])}
            for mount, e in sorted(self.disks.items())
        ]

    def forecast_of(self, mount: str) -> dict | None:
        entry = self.disks.get(mount)
        return forecast(entry["points"]) if entry else None

    def workspace_series(self, names=None) -> list[dict]:
        """Every workspace's disk (of names, when given) with its points and forecast."""
        return [
            {"name": name, "points": [{"day": p[0], "used": p[1], "free": p[2], "total": p[3]} for p in e["points"]],
             "forecast": forecast(e["points"])}
            for name, e in sorted(self.workspaces.items()) if names is None or name in names
        ]

    def workspace_forecast(self, name: str) -> dict | None:
        entry = self.workspaces.get(name)
        return forecast(entry["points"]) if entry else None


def forecast(points: list[list]) -> dict:
    """{full: ISO day or None, per_day: bytes a day, days: days of data used,
    reason: why there is no date}. points: [[day, used, free, total]], sorted."""
    if not points:
        return {"full": None, "per_day": None, "days": 0, "reason": "no data yet"}
    last_day = datetime.date.fromisoformat(points[-1][0])
    start = (last_day - datetime.timedelta(days=WINDOW_DAYS)).isoformat()
    window = [p for p in points if p[0] >= start]
    days = [(datetime.date.fromisoformat(p[0]) - last_day).days for p in window]
    span = days[-1] - days[0] + 1
    if len(window) < 2 or span < MIN_DAYS:
        return {"full": None, "per_day": None, "days": span,
                "reason": f"a forecast needs {MIN_DAYS} days of data"}
    used = [p[1] for p in window]
    mean_x, mean_y = sum(days) / len(days), sum(used) / len(used)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(days, used)) / \
        sum((x - mean_x) ** 2 for x in days)
    result = {"full": None, "per_day": round(slope), "days": span}
    if slope <= 0:
        return {**result, "reason": "the used space is not growing"}
    capacity = points[-1][1] + points[-1][2]
    # the line at the last day (x = 0), then the days until it reaches capacity
    used_now = mean_y - slope * mean_x
    remaining = (capacity - used_now) / slope
    if remaining > HORIZON_DAYS:
        return {**result, "reason": "not within 100 years at this pace"}
    full = last_day + datetime.timedelta(days=max(0, round(remaining)))
    return {**result, "full": full.isoformat()}


class DiskRecorder:
    """Records the disks once a day while OmniaCLI runs; workspaces, an async
    callable, gives the workspaces' disks ([{name, used, capacity}])."""

    def __init__(self, settings: Settings, history: DiskHistory, workspaces=None):
        self.settings, self.history = settings, history
        self._workspaces = workspaces
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def check(self, today: datetime.date | None = None) -> bool:
        """Record today if it is not yet; True when it recorded."""
        today = today or datetime.date.today()
        done = False
        if not self.history.recorded(today):
            found = await _disks.disks(self.settings)
            if found:
                await asyncio.to_thread(self.history.record, found, today)
                done = True
        if self._workspaces is not None and self.settings.workspaces and not self.history.workspaces_recorded(today):
            try:
                found = await self._workspaces()
            except Exception as exc:  # noqa: BLE001 - no helper: the machine's disks are still recorded
                _logger.warning("The workspaces' disks not recorded: %s", exc)
                found = []
            if found:
                await asyncio.to_thread(self.history.record_workspaces, found, today)
                done = True
        return done

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the recording must not stop
                _logger.exception("Disk history not recorded")
            await asyncio.sleep(CHECK_EVERY)
