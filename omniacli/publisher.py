# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The producer side of a puller: an instance of this OmniaCLI published on
a puller of a relay it links to (config.Publication, the Pullers page).
Nobody reaches this OmniaCLI; it reaches the relay.

For every publication a loop asks the relay for the consumers' commands
(puller.py: held open up to WAIT seconds, POLL_BUSY while a job it started
runs, so the job's progress goes out as it happens), sending with every ask
a report:

- the instance: its state, whether a job runs on it, its type and what
  its backup asks, the commands exposed and their hours;
- this machine's disks (and their daily history, when it changed);
- the CPU and memory samples of the machine and of the instance taken since
  the last report the relay took (the sampler keeps an hour of them);
- the jobs the consumers' commands started: their new log lines, status,
  steps and result;
- the answer to every command taken: the job it started, or why not.

Whatever the relay did not take is sent again with the next report. With
a key every report is sealed, every command must be sealed and is checked
against replays (pullcrypto.ReplayGuard, kept on disk), and every backup is
encrypted before it is uploaded. A command runs only when the publication
exposes it, within its hours by this machine's clock; otherwise it is
refused at once, the answer saying why.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from . import disks as _disks
from . import ops
from .config import PUBLISHED_COMMANDS, Instance, Publication, Settings
from .jobs import Job, JobFailed, JobManager
from .linkcopy import _said
from .links import LinkClients, describe_error, link_headers
from .pullcrypto import (
    COMMAND,
    REPORT,
    Command,
    CommandRefused,
    PullerCryptoError,
    ReplayGuard,
    Sealer,
    encrypt_file,
    parse_key,
)

log = logging.getLogger(__name__)

WAIT = 25  # seconds the relay holds an ask open for a command
POLL_BUSY = 2  # while a job a command started runs
RETRY = (5, 10, 20, 40, 60)  # seconds after a failed ask, then 60 each time
LINES_PER_REPORT = 2000  # log lines of a job in one report; the rest in the next
CHUNK = 1 << 20
# the user OmniaCLI acts as on the relay: a puller needs only the role user
RELAY_USER, RELAY_ROLE = "puller", "user"
STOP_JOB = "stop-job"


class Publisher:
    """The loops of the publications of settings.publications. describe(inst)
    gives what the report says of the instance (state, busy, the backup's
    fields); backup_options(inst, params) the options of a backup from the
    params of a command (ActionRefused when they are not valid)."""

    def __init__(self, settings: Settings, jobs: JobManager, sampler, disk_history, link_clients: LinkClients,
                 folder: Path, describe: Callable[[Instance], Awaitable[dict]],
                 backup_options: Callable[[Instance, dict], dict], version: str = ""):
        self.settings = settings
        self.jobs = jobs
        self.sampler = sampler
        self.disk_history = disk_history
        self.link_clients = link_clients
        self.folder = folder
        self.describe = describe
        self.backup_options = backup_options
        self.version = version
        self._tasks: dict[str, asyncio.Task] = {}
        self._runs: dict[str, _Run] = {}

    def status(self, key: str) -> dict:
        run = self._runs.get(key)
        return dict(run.status) if run else {}

    def start(self) -> None:
        self.reload()

    def reload(self) -> None:
        """A loop for every publication, none for a publication gone."""
        for key in [k for k in self._tasks if k not in self.settings.publications]:
            self._tasks.pop(key).cancel()
            self._runs.pop(key, None)
        for key in self.settings.publications:
            if key not in self._tasks:
                run = self._runs[key] = _Run(self, key)
                self._tasks[key] = asyncio.get_running_loop().create_task(run.loop())

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks, self._runs = {}, {}


class _Run:
    """One publication's loop, and what it still has to send."""

    def __init__(self, publisher: Publisher, key: str):
        self.p = publisher
        self.key = key
        self.state_file = publisher.folder / (key.replace("/", "_") + ".json")
        self.status: dict = {"ok": None, "at": None, "error": "", "seq": None, "commands": 0}
        # what the relay took last: the time of the last samples sent, the
        # last day of the disk history sent (None: not sent yet, even empty)
        self.machine_t = 0
        self.instance_t = 0
        self.history_day: Optional[str] = None
        self.answers: list[dict] = []
        # {job id: {"command": its command id, "sent": lines sent, "status": status sent}}
        self.followed: dict[str, dict] = {}
        seen = {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            seen = data.get("seen") or {}
            self.followed = data.get("jobs") or {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("publication %s: %s unreadable: %s", key, self.state_file, exc)
        self.guard = ReplayGuard(seen=seen)

    def save(self) -> None:
        self.guard.prune()
        self.p.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps({"seen": self.guard.state(), "jobs": self.followed}), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @property
    def publication(self) -> Publication:
        return self.p.settings.publications[self.key]

    def sealer(self, pub: Publication) -> Sealer:
        return Sealer(parse_key(pub.key) if pub.key else None, pub.puller)

    # --- the loop ---------------------------------------------------------------

    async def loop(self) -> None:
        failures = 0
        while True:
            try:
                await self.cycle()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must not die
                failures += 1
                self.status.update(ok=False, at=time.time(), error=str(exc) or type(exc).__name__)
                if not isinstance(exc, _Refused):
                    log.warning("publication %s: %s", self.key, self.status["error"])
                await asyncio.sleep(RETRY[min(failures, len(RETRY)) - 1])

    async def cycle(self) -> None:
        pub = self.publication
        instance = self.p.settings.instance(*pub.instance.split("/", 1))
        if instance is None:
            raise _Refused(f"{pub.instance} is not a live instance here")
        link = self.p.settings.link_targets.get(pub.link)
        if link is None:
            raise _Refused(f"no link {pub.link} on the Links page")
        sealer = self.sealer(pub)
        payload, commit = await self.report(pub, instance)
        client = self.p.link_clients.client(link)
        headers = link_headers(link, RELAY_USER, RELAY_ROLE)
        wait = POLL_BUSY if self.busy() else WAIT
        try:
            resp = await client.post(f"/api/pullers/{pub.puller}/producer", params={"wait": wait},
                                     headers=headers, json={"report": sealer.seal(REPORT, payload)},
                                     timeout=httpx.Timeout(WAIT + 30, connect=10))
        except httpx.HTTPError as exc:
            raise _Refused(f"{pub.link}: {describe_error(exc)}") from exc
        if resp.status_code != 200:
            raise _Refused(f"{pub.link} answered HTTP {resp.status_code}: {_said(resp)}")
        answer = resp.json()
        commit()
        self.status.update(ok=True, at=time.time(), error="", seq=answer.get("seq"))
        for entry in answer.get("commands") or []:
            self.status["commands"] += 1
            await self.take(pub, instance, sealer, entry)

    def busy(self) -> bool:
        """A job started by a command is not finished yet."""
        for jid in self.followed:
            job = self.p.jobs.get(jid)
            if job is not None and not job.finished_flag:
                return True
        return False

    # --- the report ---------------------------------------------------------------

    async def report(self, pub: Publication, instance: Instance):
        """The report, and what to note once the relay took it."""
        sampler, history = self.p.sampler, self.p.disk_history
        machine = sampler.machine_series(self.machine_t)
        points = sampler.series(instance.key, self.instance_t)
        found = await _disks.disks(self.p.settings)
        for disk in found:
            disk["forecast"] = history.forecast_of(disk["mount"])
        series = history.series()
        last_day = max((p["day"] for d in series for p in d["points"]), default="")
        jobs, notes = [], {}
        for jid, seen in list(self.followed.items()):
            job = self.p.jobs.get(jid)
            if job is None:
                # gone from the jobs kept
                jobs.append({"id": jid, "command": seen.get("command"), "status": "unknown", "lines_from": 0,
                             "lines": [], "gone": True})
                notes[jid] = None
                continue
            lines = job.lines[seen["sent"]:seen["sent"] + LINES_PER_REPORT]
            if not lines and seen.get("status") == job.status and job.status != "running":
                continue
            jobs.append({"id": job.id, "command": seen.get("command"), "title": job.title, "status": job.status,
                         "source": job.source, "created": job.created, "started": job.started,
                         "finished": job.finished, "error": job.error, "result": job.result,
                         "lines_from": seen["sent"], "lines": lines})
            sent = seen["sent"] + len(lines)
            # finished and sent whole: no longer followed
            notes[jid] = None if job.finished_flag and sent >= len(job.lines) else {**seen, "sent": sent,
                                                                                     "status": job.status}
        answers = list(self.answers)
        payload = {
            "at": time.time(),
            "instance": instance.key,
            "type": instance.type,
            "label": instance.plugin.label,
            "omniacli": self.p.version,
            **await self.p.describe(instance),
            "commands": {a: {"enabled": pub.exposed(a).enabled, "window": pub.exposed(a).window_label()}
                         for a in PUBLISHED_COMMANDS},
            "encrypted": bool(pub.key),
            "disks": found,
            "metrics": {"cores": sampler.cores, "interval": 5, "machine": machine, "instance": points,
                        "source": sampler.source.get(instance.key, "")},
            "jobs": jobs,
            "answers": answers,
        }
        if last_day != self.history_day:
            payload["disk_history"] = series

        def commit():
            if machine:
                self.machine_t = machine[-1]["t"]
            if points:
                self.instance_t = points[-1]["t"]
            self.history_day = last_day
            del self.answers[:len(answers)]
            changed = False
            for jid, note in notes.items():
                if note is None:
                    changed |= self.followed.pop(jid, None) is not None
                elif self.followed.get(jid) != note:
                    self.followed[jid] = note
                    changed = True
            if changed:
                self.save()

        return payload, commit

    # --- the commands ---------------------------------------------------------------

    def answer(self, entry: dict, cmd: Optional[Command], **fields) -> None:
        self.answers.append({"qid": entry.get("qid"), "command": cmd.id if cmd else None,
                             "action": cmd.action if cmd else None, **fields})

    async def take(self, pub: Publication, instance: Instance, sealer: Sealer, entry: dict) -> None:
        try:
            cmd = Command.from_dict(sealer.open(COMMAND, entry.get("envelope")))
        except PullerCryptoError as exc:
            log.warning("publication %s: command %s from %s refused: %s", self.key, entry.get("qid"),
                        entry.get("by"), exc)
            self.answer(entry, None, ok=False, error=f"refused: {exc}")
            return
        try:
            self.guard.check(cmd)
        except CommandRefused as exc:
            log.warning("publication %s: %s %s refused: %s", self.key, cmd.action, cmd.id, exc)
            self.answer(entry, cmd, ok=False, error=f"refused: {exc}")
            return
        self.save()
        try:
            job = self.run(pub, instance, cmd)
        except _Refused as exc:
            log.info("publication %s: %s from %s refused: %s", self.key, cmd.action, cmd.requester, exc)
            self.answer(entry, cmd, ok=False, error=str(exc))
            return
        if job is None:
            self.answer(entry, cmd, ok=True)
            return
        self.followed[job.id] = {"command": cmd.id, "sent": 0, "status": ""}
        self.save()
        self.answer(entry, cmd, ok=True, job=job.id)

    def run(self, pub: Publication, instance: Instance, cmd: Command) -> Optional[Job]:
        """The job of a command; _Refused when it is not taken."""
        source = f"puller {pub.puller} ({cmd.requester})"
        if cmd.action == STOP_JOB:
            jid = str(cmd.params.get("job") or "")
            if jid not in self.followed:
                raise _Refused(f"job {jid or '?'} was not started through puller {pub.puller}")
            if not self.p.jobs.stop(jid, source):
                raise _Refused(f"job {jid} is not running")
            return None
        if cmd.action not in PUBLISHED_COMMANDS:
            raise _Refused(f"{cmd.action} is not a command of a puller")
        rule = pub.exposed(cmd.action)
        if not rule.enabled:
            raise _Refused(f"{cmd.action} is not exposed by {instance.key} on puller {pub.puller}")
        now = datetime.datetime.now()
        if not rule.in_window(now.time()):
            raise _Refused(f"{cmd.action} is taken {rule.window_label()} only, it is {now:%H:%M} on "
                           f"{instance.key}: schedule it in its hours")
        if cmd.action in ("start", "stop") and instance.restart.type not in ("systemd", "process"):
            raise _Refused(f"{cmd.action} is for a systemd service or an instance OmniaCLI starts itself")
        jobs = self.p.jobs
        if cmd.action == "backup":
            try:
                options = self.p.backup_options(instance, cmd.params)
            except Exception as exc:  # ActionRefused: a parameter that is not valid
                raise _Refused(str(exc)) from None
            return jobs.submit(f"Backup {instance.key} for puller {pub.puller}", [instance.key], source,
                               lambda job: self.backup(job, pub, instance, options))
        action = {"start": ops.start, "stop": ops.stop, "restart": ops.restart}[cmd.action]
        return jobs.submit(f"{cmd.action.capitalize()} {instance.key}", [instance.key], source,
                           lambda job: action(job, instance))

    async def backup(self, job: Job, pub: Publication, instance: Instance, options: dict) -> None:
        """A backup, encrypted with the puller's key (if any), uploaded to the relay."""
        key = parse_key(pub.key) if pub.key else None
        steps = [{"id": "backup", "label": "Backup", "where": instance.key}]
        if key:
            steps.append({"id": "encrypt", "label": "Encrypt", "where": instance.key})
        steps.append({"id": "upload", "label": "Upload", "where": f"here → {pub.link}", "bytes": True})
        progress = job.result["progress"] = {"steps": steps, "step": "backup", "done": 0, "total": 0}
        dest = await ops.backup(job, instance, self.p.settings.backup_dir, options)
        job.result.update(backup=dest.name)
        upload = dest
        if key:
            progress["step"] = "encrypt"
            upload = self.p.folder / "uploads" / (dest.name + ".enc")
            upload.parent.mkdir(parents=True, exist_ok=True)
            job.log(f"--- encrypt {dest.name} with the key of puller {pub.puller}")
            await asyncio.to_thread(encrypt_file, key, pub.puller, dest, upload)
        try:
            progress["step"] = "upload"
            await self.upload(job, pub, upload, progress)
        finally:
            if upload != dest:
                upload.unlink(missing_ok=True)
        job.result.update(file=upload.name, size=progress["total"], encrypted=bool(key))

    async def upload(self, job: Job, pub: Publication, path: Path, progress: dict) -> None:
        link = self.p.settings.link_targets.get(pub.link)
        if link is None:
            raise JobFailed(f"no link {pub.link} on the Links page")
        size = path.stat().st_size
        progress.update(done=0, total=size, since=time.time())
        job.log(f"--- upload {path.name} ({size >> 20} MiB) to puller {pub.puller} on {pub.link}")
        started = time.monotonic()

        async def chunks():
            with open(path, "rb") as fh:
                while True:
                    block = await asyncio.to_thread(fh.read, CHUNK)
                    if not block:
                        return
                    progress["done"] += len(block)
                    yield block

        headers = {**link_headers(link, RELAY_USER, RELAY_ROLE), "Content-Length": str(size),
                   "Content-Type": "application/octet-stream"}
        try:
            resp = await self.p.link_clients.client(link).put(
                f"/api/pullers/{pub.puller}/producer/files/{path.name}", headers=headers, content=chunks())
        except httpx.HTTPError as exc:
            raise JobFailed(f"{pub.link}: {describe_error(exc)} while uploading {path.name}") from exc
        if resp.status_code != 200:
            raise JobFailed(f"{pub.link} refused {path.name} (HTTP {resp.status_code}): {_said(resp)}")
        job.log(f"{size >> 20} MiB in {time.monotonic() - started:.0f} s")


class _Refused(Exception):
    """What a cycle or a command could not do, said as it is (no traceback)."""
