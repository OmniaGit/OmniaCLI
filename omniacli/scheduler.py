# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Automatic update: check every instance's remotes and deploy what is new."""

from __future__ import annotations

import asyncio
import datetime
import logging

from . import ops
from .config import Instance, Settings
from .jobs import JobFailed, JobManager

_logger = logging.getLogger(__name__)


class AutoUpdater:
    def __init__(self, settings: Settings, jobs: JobManager):
        self.settings = settings
        self.jobs = jobs
        # instance key -> {"checked": iso time, "message": text, "error": bool}
        self.state: dict[str, dict] = {}
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for instance in self.settings.all_instances():
            if instance.auto_update.enabled:
                self._tasks.append(asyncio.create_task(self._loop(instance)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def reload(self) -> None:
        """Restart the loops after the servers changed."""
        await self.stop()
        self.state = {
            key: value
            for key, value in self.state.items()
            if key in {i.key for i in self.settings.all_instances()}
        }
        self.start()

    def _set(self, instance: Instance, message: str, error: bool = False) -> None:
        self.state[instance.key] = {
            "checked": datetime.datetime.now().isoformat(timespec="seconds"),
            "message": message,
            "error": error,
        }

    async def _loop(self, instance: Instance) -> None:
        while True:
            try:
                await self.check(instance)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                _logger.exception("Auto update check of %s crashed", instance.key)
                self._set(instance, f"{type(exc).__name__}: {exc}", error=True)
            await asyncio.sleep(instance.auto_update.interval_minutes * 60)

    async def check(self, instance: Instance, now: datetime.time | None = None) -> None:
        now = now or datetime.datetime.now().time()
        if not instance.auto_update.in_window(now):
            self._set(instance, f"outside window {instance.auto_update.window_label()}")
            return
        lock = self.jobs.lock(instance.key)
        if lock.locked():
            self._set(instance, "skipped: another job is running")
            return
        async with lock:
            try:
                pending = await ops.pending_updates(instance)
            except JobFailed as exc:
                self._set(instance, str(exc), error=True)
                return
        if not pending:
            self._set(instance, "up to date")
            return
        summary = ", ".join(f"{name} +{count}" for name, count in pending.items())
        job = self.jobs.submit(
            f"Auto update {instance.key} ({summary})",
            [instance.key],
            "auto",
            lambda job: ops.pull_restart(job, instance, only_if_changed=True),
        )
        self._set(instance, f"new commits ({summary}): started job {job.id}")
