# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background jobs: one per action, with a log, a status and a history on disk."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import traceback
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

_logger = logging.getLogger(__name__)

QUEUED, RUNNING, SUCCESS, FAILED, INTERRUPTED = (
    "queued",
    "running",
    "success",
    "failed",
    "interrupted",
)
FINISHED = (SUCCESS, FAILED, INTERRUPTED)


class JobFailed(Exception):
    """An action failed for a reason it already explained in the job log."""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    title: str
    instances: list[str]
    source: str
    status: str = QUEUED
    created: str = field(default_factory=_now)
    started: str | None = None
    finished: str | None = None
    error: str | None = None
    lines: list[str] = field(default_factory=list)
    # what the action produced, e.g. {"download": url} for a backup
    result: dict = field(default_factory=dict)

    def log(self, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        for line in str(text).splitlines() or [""]:
            self.lines.append(f"{stamp} {line}")

    @property
    def servers(self) -> list[str]:
        """The servers of the instances (server/role) the job ran on."""
        return list(dict.fromkeys(key.split("/", 1)[0] for key in self.instances))

    @property
    def finished_flag(self) -> bool:
        return self.status in FINISHED


def filter_jobs(jobs: list[Job], server: str = "", query: str = "") -> list[Job]:
    """The jobs of a server and containing every word of query (title,
    started by, error, server/role), case insensitive."""
    if server:
        jobs = [j for j in jobs if server in j.servers]
    words = query.lower().split()
    if words:
        def text(job):
            return " ".join([job.title, job.source, job.error or "", *job.instances]).lower()
        jobs = [j for j in jobs if all(w in text(j) for w in words)]
    return jobs


class JobManager:
    def __init__(self, jobs_dir: Path, keep: int = 500):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._load()

    def _load(self) -> None:
        files = sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in files[-self.keep :]:
            try:
                job = Job(**json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError) as exc:
                _logger.warning("Skipping unreadable job file %s: %s", path, exc)
                continue
            if not job.finished_flag:
                # OmniaCLI stopped while the job was running
                job.status = INTERRUPTED
                job.log("OmniaCLI was stopped before this job finished")
                self._save(job)
            self.jobs[job.id] = job

    def _save(self, job: Job) -> None:
        path = self.jobs_dir / f"{job.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(job)), encoding="utf-8")
        tmp.replace(path)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def is_busy(self, key: str) -> bool:
        return self.lock(key).locked()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 100) -> list[Job]:
        return list(reversed(self.jobs.values()))[:limit]

    def submit(
        self,
        title: str,
        instances: list[str],
        source: str,
        action: Callable[[Job], Awaitable[None]],
    ) -> Job:
        job = Job(
            id=datetime.datetime.now().strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:6],
            title=title,
            instances=sorted(instances),
            source=source,
        )
        self.jobs[job.id] = job
        while len(self.jobs) > self.keep:
            old_id, _ = self.jobs.popitem(last=False)
            (self.jobs_dir / f"{old_id}.json").unlink(missing_ok=True)
        self._save(job)
        task = asyncio.get_running_loop().create_task(self._run(job, action))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, action: Callable[[Job], Awaitable[None]]) -> None:
        locks = [self.lock(key) for key in job.instances]  # sorted: no deadlock
        if any(lock.locked() for lock in locks):
            job.log("Waiting for another job on the same instance to finish")
        for lock in locks:
            await lock.acquire()
        try:
            job.status, job.started = RUNNING, _now()
            self._save(job)
            await action(job)
            job.status = SUCCESS
        except JobFailed as exc:
            job.status, job.error = FAILED, str(exc)
            job.log(f"FAILED: {exc}")
        except Exception as exc:  # noqa: BLE001 - every error must reach the job log
            job.status, job.error = FAILED, f"{type(exc).__name__}: {exc}"
            job.log(traceback.format_exc())
            _logger.exception("Job %s crashed", job.id)
        finally:
            job.finished = _now()
            self._save(job)
            for lock in reversed(locks):
                lock.release()
