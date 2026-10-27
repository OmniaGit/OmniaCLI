# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consumer side of a puller: this OmniaCLI operating, through a relay
it links to, the instance a puller's producer publishes (publisher.py),
which nobody can reach (config.PullerConnection, the Pullers page).

For every connection a loop reads the producer's reports from the relay
(puller.py: held open up to WAIT seconds, woken by a new one), from the last
one read, and keeps:

- the last state of the instance (state, busy, type, its backup's options,
  the commands exposed and their hours, the disks and their history);
- the CPU and memory samples, KEEP_DAYS on file, one file a day;
- the jobs the commands started there, with their log as it comes;
- the commands sent from here and the producer's answer to each.

A command is sealed with the connection's key (if any) and queued on the
relay; the answer comes with a later report. Copy from link from a puller
(fetch_backup) asks a backup there, follows its job, downloads the file the
producer uploaded, decrypts it and deletes it from the relay.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import socket
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from .config import PullerConnection, Settings
from .jobs import Job, JobFailed
from .linkcopy import BACKUP_NAME, _said
from .links import LinkClients, describe_error, link_headers
from .puller import FILE_NAME, PICKUP
from .pullcrypto import (
    COMMAND,
    REPORT,
    Command,
    PullerCryptoError,
    Sealer,
    decrypt_file,
    is_encrypted_file,
    parse_key,
)

log = logging.getLogger(__name__)

WAIT = 25  # seconds the relay holds a read open for a report
RETRY = (5, 10, 20, 40, 60)
KEEP_DAYS = 7  # of the CPU and memory samples
LIVE = 720  # samples kept in memory per series: an hour, as the sampler
KEEP_JOBS = 30  # jobs of the producer kept, with their log
KEEP_LINES = 5000  # lines of a job's log kept
KEEP_SENT = 50  # commands sent kept with their answer
POLL_EVERY = 1.0  # seconds between two looks at an answer or a job
CHUNK = 1 << 20
_FINISHED = ("success", "failed", "stopped", "interrupted")
REFUSED = "a report refused"


def copy_steps(connection: PullerConnection, instance: str) -> list[dict]:
    """The steps of a copy from a puller, for the job page."""
    steps = [{"id": "backup", "label": "Backup", "where": f"puller {connection.puller}"},
             {"id": "download", "label": "Transfer", "where": f"{connection.link} → here"}]
    if connection.key:
        steps.append({"id": "decrypt", "label": "Decrypt", "where": "here"})
    return steps + [{"id": "restore", "label": "Restore", "where": instance}]


class ConsumerError(Exception):
    """A command that cannot be sent: the message says why."""


def requester(user: str) -> str:
    """Who asks, as the producer records it: this computer and the user."""
    return f"{socket.gethostname()}: {user}"[:200]


class Consumers:
    """The loops of the connections of settings.connections."""

    def __init__(self, settings: Settings, link_clients: LinkClients, folder: Path):
        self.settings = settings
        self.link_clients = link_clients
        self.folder = folder
        self._tasks: dict[str, asyncio.Task] = {}
        self.runs: dict[str, _Conn] = {}

    def conn(self, name: str) -> "_Conn":
        """The state of a connection (made when first asked: before the loop runs too)."""
        if name not in self.settings.connections:
            raise ConsumerError(f"No puller connection {name}")
        run = self.runs.get(name)
        if run is None:
            run = self.runs[name] = _Conn(self, name)
        return run

    def start(self) -> None:
        self.reload()

    def reload(self) -> None:
        for name in [n for n in self._tasks if n not in self.settings.connections]:
            self._tasks.pop(name).cancel()
            self.runs.pop(name, None)
        for name in self.settings.connections:
            if name not in self._tasks:
                self._tasks[name] = asyncio.get_running_loop().create_task(self.conn(name).loop())

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for run in self.runs.values():
            try:
                run.save()
            except OSError:
                pass
        self._tasks = {}

    async def send(self, name: str, action: str, params: dict, user: str) -> dict:
        return await self.conn(name).send(action, params, user)

    async def fetch_backup(self, job: Job, name: str, folder: Path, progress: dict, options: dict,
                           user: str) -> Path:
        return await self.conn(name).fetch_backup(job, folder, progress, options, user)


class _Conn:
    def __init__(self, hub: Consumers, name: str):
        self.hub = hub
        self.name = name
        self.dir = hub.folder / name
        self.status: dict = {"ok": None, "at": None, "error": ""}
        self.relay: dict = {}  # what the relay knows: last contacts, commands waiting, expired, files
        self.since = 0
        self.latest: dict = {}
        self.disk_history: list = []
        self.jobs: dict[str, dict] = {}
        self.sent: dict[str, dict] = {}  # {command id: {qid, action, at, by, answer, job}}
        self.machine: deque = deque(maxlen=LIVE)
        self.instance: deque = deque(maxlen=LIVE)
        self.load()

    @property
    def connection(self) -> PullerConnection:
        return self.hub.settings.connections[self.name]

    def sealer(self) -> Sealer:
        c = self.connection
        return Sealer(parse_key(c.key) if c.key else None, c.puller)

    # --- on disk ------------------------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.dir / "state.json"

    def load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            log.warning("puller connection %s: %s unreadable: %s", self.name, self.state_file, exc)
            data = {}
        self.since = int(data.get("since") or 0)
        self.latest = data.get("latest") or {}
        self.disk_history = data.get("disk_history") or []
        self.jobs = data.get("jobs") or {}
        self.sent = data.get("sent") or {}
        for point in self.metrics(time.time() - 3600):
            (self.machine if point["s"] == "m" else self.instance).append(point["p"])

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"since": self.since, "latest": self.latest, "disk_history": self.disk_history,
                "jobs": self.jobs, "sent": self.sent}
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _day_file(self, day: datetime.date) -> Path:
        return self.dir / f"metrics-{day.isoformat()}.jsonl"

    def _append_metrics(self, series: str, points: list[dict]) -> None:
        """New samples (after the last one kept) to memory and to the day's file."""
        live = self.machine if series == "m" else self.instance
        last = live[-1]["t"] if live else 0
        fresh = [p for p in points if isinstance(p, dict) and isinstance(p.get("t"), (int, float)) and p["t"] > last]
        if not fresh:
            return
        live.extend(fresh)
        self.dir.mkdir(parents=True, exist_ok=True)
        by_day: dict[datetime.date, list[str]] = {}
        for p in fresh:
            day = datetime.date.fromtimestamp(p["t"])
            by_day.setdefault(day, []).append(json.dumps({"s": series, "p": p}))
        for day, lines in by_day.items():
            with open(self._day_file(day), "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    def prune_metrics(self, today: Optional[datetime.date] = None) -> None:
        oldest = (today or datetime.date.today()) - datetime.timedelta(days=KEEP_DAYS)
        for path in self.dir.glob("metrics-*.jsonl") if self.dir.is_dir() else ():
            try:
                if datetime.date.fromisoformat(path.stem[len("metrics-"):]) < oldest:
                    path.unlink(missing_ok=True)
            except ValueError:
                continue

    def metrics(self, since: float) -> list[dict]:
        """The samples after since, from the files: [{"s": "m"|"i", "p": point}]."""
        found = []
        first = datetime.date.fromtimestamp(max(since, 0))
        for path in sorted(self.dir.glob("metrics-*.jsonl")) if self.dir.is_dir() else ():
            try:
                if datetime.date.fromisoformat(path.stem[len("metrics-"):]) < first:
                    continue
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if item.get("p", {}).get("t", 0) > since:
                            found.append(item)
            except (ValueError, OSError):
                continue
        return found

    # --- the loop -----------------------------------------------------------------

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
                await asyncio.sleep(RETRY[min(failures, len(RETRY)) - 1])

    def client(self):
        c = self.connection
        link = self.hub.settings.link_targets.get(c.link)
        if link is None:
            raise ConsumerError(f"no link {c.link} on the Links page")
        return self.hub.link_clients.client(link), link

    async def cycle(self, wait: float = WAIT) -> None:
        client, link = self.client()
        c = self.connection
        try:
            resp = await client.get(f"/api/pullers/{c.puller}/reports", params={"since": self.since, "wait": wait},
                                    headers=link_headers(link, "puller", "user"),
                                    timeout=httpx.Timeout(wait + 30, connect=10))
        except httpx.HTTPError as exc:
            raise ConsumerError(f"{c.link}: {describe_error(exc)}") from exc
        if resp.status_code != 200:
            raise ConsumerError(f"{c.link} answered HTTP {resp.status_code}: {_said(resp)}")
        data = resp.json()
        self.relay = {k: data.get(k) for k in ("producer", "consumers", "pending", "expired", "files", "first", "last")}
        if data.get("reset"):
            log.info("puller connection %s: the relay's reports began again", self.name)
        sealer, refused = self.sealer(), ""
        for item in data.get("reports") or []:
            try:
                self.take(sealer.open(REPORT, item.get("envelope")))
            except PullerCryptoError as exc:
                refused = f"{REFUSED}: {exc}"
                log.warning("puller connection %s: report %s refused: %s", self.name, item.get("seq"), exc)
        expired = {e.get("qid") for e in data.get("expired") or []}
        for sent in self.sent.values():
            if sent.get("answer") is None and sent.get("qid") in expired:
                sent["answer"] = {"ok": False, "error": "expired: the producer did not take it in "
                                                        f"{PICKUP // 60} minutes"}
        self.since = data.get("next", self.since)
        self.prune_metrics()
        self.save()
        self.status.update(ok=not refused, at=time.time(), error=refused)

    def take(self, report: dict) -> None:
        """A report opened: what it says, kept."""
        metrics = report.get("metrics") or {}
        self._append_metrics("m", metrics.get("machine") or [])
        self._append_metrics("i", metrics.get("instance") or [])
        if "disk_history" in report:
            self.disk_history = report["disk_history"] or []
        self.latest = {k: v for k, v in report.items() if k not in ("metrics", "jobs", "answers", "disk_history")}
        self.latest["metrics"] = {k: metrics.get(k) for k in ("cores", "interval", "source")}
        for j in report.get("jobs") or []:
            self._take_job(j)
        for answer in report.get("answers") or []:
            sent = self.sent.get(answer.get("command") or "")
            if sent is None:
                sent = next((s for s in self.sent.values() if s.get("qid") == answer.get("qid")), None)
            if sent is not None:
                sent["answer"] = {k: answer.get(k) for k in ("ok", "error", "job")}

    def _take_job(self, j: dict) -> None:
        """A job of a report: its new lines after those kept (lines_from is
        the producer's count of the lines before them), its status, steps
        and result."""
        jid = str(j.get("id") or "")
        if not jid:
            return
        # next: the producer's count of the lines received; dropped: the
        # oldest of them no longer kept
        kept = self.jobs.setdefault(jid, {"id": jid, "lines": [], "dropped": 0, "next": 0})
        lines, first = list(j.get("lines") or []), int(j.get("lines_from") or 0)
        if first > kept["next"]:
            kept["lines"].append(f"... {first - kept['next']} lines not received")
            kept["next"] = first
        new = lines[kept["next"] - first:]
        kept["lines"].extend(new)
        kept["next"] += len(new)
        if len(kept["lines"]) > KEEP_LINES:
            cut = len(kept["lines"]) - KEEP_LINES
            kept["lines"], kept["dropped"] = kept["lines"][cut:], kept["dropped"] + cut
        for key in ("command", "title", "status", "source", "created", "started", "finished", "error",
                    "result", "gone"):
            if key in j:
                kept[key] = j[key]
        kept["seen"] = time.time()
        if len(self.jobs) > KEEP_JOBS:
            for old in sorted(self.jobs, key=lambda k: self.jobs[k].get("seen", 0))[:len(self.jobs) - KEEP_JOBS]:
                if old != jid:
                    self.jobs.pop(old, None)

    def job_lines(self, jid: str, since: int = 0) -> dict:
        """A producer's job and its lines from since (a position counted from
        the first line received, as next answers it)."""
        kept = self.jobs.get(jid)
        if kept is None:
            raise ConsumerError(f"no job {jid} from puller connection {self.name}")
        lines = kept["lines"][max(0, since - kept["dropped"]):]
        return {**{k: v for k, v in kept.items() if k not in ("lines", "dropped", "next")},
                "lines": lines, "next": kept["dropped"] + len(kept["lines"])}

    # --- commands -------------------------------------------------------------------

    async def send(self, action: str, params: dict, user: str) -> dict:
        client, link = self.client()
        c = self.connection
        cmd = Command(action, requester(user), params)
        try:
            resp = await client.post(f"/api/pullers/{c.puller}/commands", headers=link_headers(link, user, "user"),
                                     json={"command": self.sealer().seal(COMMAND, cmd.to_dict())})
        except httpx.HTTPError as exc:
            raise ConsumerError(f"{c.link}: {describe_error(exc)}") from exc
        if resp.status_code != 200:
            raise ConsumerError(f"{c.link} refused the command (HTTP {resp.status_code}): {_said(resp)}")
        qid = resp.json().get("qid")
        self.sent[cmd.id] = {"command": cmd.id, "qid": qid, "action": action, "params": params, "at": cmd.created,
                             "by": user, "answer": None}
        if len(self.sent) > KEEP_SENT:
            for old in sorted(self.sent, key=lambda k: self.sent[k]["at"])[:len(self.sent) - KEEP_SENT]:
                self.sent.pop(old, None)
        self.save()
        return self.sent[cmd.id]

    async def _answer(self, job: Job, cmd_id: str) -> dict:
        """The producer's answer to a command sent, as it comes."""
        deadline = time.monotonic() + PICKUP + 120
        while time.monotonic() < deadline:
            answer = (self.sent.get(cmd_id) or {}).get("answer")
            if answer is not None:
                return answer
            if self.status.get("error", "").startswith(REFUSED):
                # the answer would come in a report this side cannot open
                raise JobFailed(f"{self.status['error']}: is the key the one of the producer's publication?")
            await asyncio.sleep(POLL_EVERY)
        seen = (self.relay.get("producer") or {}).get("seen")
        when = datetime.datetime.fromtimestamp(seen).strftime("%Y-%m-%d %H:%M") if seen else "never"
        raise JobFailed(f"no answer from the producer of puller {self.connection.puller} "
                        f"(last seen by the relay: {when})")

    async def fetch_backup(self, job: Job, folder: Path, progress: dict, options: dict, user: str) -> Path:
        """A new backup there, through the relay into folder as <connection>_<name>.zip."""
        c = self.connection
        progress["step"] = "backup"
        job.log(f"--- backup on puller {c.puller} (link {c.link})")
        sent = await self.send("backup", options, user)
        answer = await self._answer(job, sent["command"])
        if not answer.get("ok"):
            raise JobFailed(f"the producer of puller {c.puller} refused the backup: {answer.get('error')}")
        remote = str(answer.get("job") or "")
        job.log(f"its job there: {remote}")
        shown = 0
        try:
            while True:
                kept = self.jobs.get(remote) or {}
                lines, dropped = kept.get("lines") or [], kept.get("dropped", 0)
                for line in lines[max(0, shown - dropped):]:
                    job.log(f"  {c.puller}: {line}")
                shown = dropped + len(lines)
                if kept.get("status") in _FINISHED or kept.get("gone"):
                    break
                await asyncio.sleep(POLL_EVERY)
        except asyncio.CancelledError:
            job.log(f"--- stop asked to the producer for its job {remote}")
            try:
                await asyncio.shield(self.send("stop-job", {"job": remote}, user))
            except Exception as exc:  # the local job stops anyway
                job.log(f"the stop could not be sent: {exc}")
            raise
        if kept.get("status") != "success":
            raise JobFailed(f"the backup on puller {c.puller} {kept.get('status', 'is gone')}: "
                            f"{kept.get('error') or 'no reason given'}")
        result = kept.get("result") or {}
        file, backup = str(result.get("file") or ""), str(result.get("backup") or "")
        if not FILE_NAME.fullmatch(file) or not BACKUP_NAME.fullmatch(backup):
            raise JobFailed(f"the backup on puller {c.puller} names no file (job {remote})")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{self.name}_{backup}"
        progress["step"] = "download"
        got = await self._download(job, file, folder / f".{self.name}_{file}", progress)
        try:
            if c.key:
                if not is_encrypted_file(got):
                    raise JobFailed(f"{file} is not encrypted, and this connection has a key: refused")
                progress["step"] = "decrypt"
                job.log(f"--- decrypt {file}")
                try:
                    await asyncio.to_thread(decrypt_file, parse_key(c.key), c.puller, got, dest)
                except PullerCryptoError as exc:
                    raise JobFailed(f"{file}: {exc}") from None
            elif is_encrypted_file(got):
                raise JobFailed(f"{file} is encrypted: give this connection the puller's key")
            else:
                got.replace(dest)
        finally:
            got.unlink(missing_ok=True)
        await self._taken(job, file)
        return dest

    async def _download(self, job: Job, file: str, dest: Path, progress: dict) -> Path:
        client, link = self.client()
        c = self.connection
        part = dest.with_name(dest.name + ".part")
        job.log(f"--- download {file} from the relay {c.link}")
        started, size = time.monotonic(), 0
        try:
            async with client.stream("GET", f"/api/pullers/{c.puller}/files/{file}",
                                     headers=link_headers(link, "puller", "user")) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise JobFailed(f"{c.link} refused {file} (HTTP {resp.status_code}): {_said(resp)}")
                length = int(resp.headers.get("content-length") or 0)
                progress.update(done=0, total=length, since=time.time())
                with open(part, "wb") as fh:
                    async for chunk in resp.aiter_bytes(CHUNK):
                        fh.write(chunk)
                        size += len(chunk)
                        progress["done"] = size
            if length and size != length:
                raise JobFailed(f"{file}: {size} of {length} bytes downloaded")
            part.replace(dest)
        except httpx.HTTPError as exc:
            raise JobFailed(f"{c.link}: {describe_error(exc)} while downloading {file}") from exc
        finally:
            part.unlink(missing_ok=True)
        job.log(f"{size >> 20} MiB in {time.monotonic() - started:.0f} s")
        return dest

    async def _taken(self, job: Job, file: str) -> None:
        """The file is here whole: it goes from the relay."""
        client, link = self.client()
        c = self.connection
        try:
            resp = await client.delete(f"/api/pullers/{c.puller}/files/{file}",
                                       headers=link_headers(link, "puller", "user"))
            if resp.status_code != 200:
                job.log(f"the relay kept {file} (HTTP {resp.status_code}): it goes after its hours")
        except httpx.HTTPError as exc:
            job.log(f"the relay kept {file} ({describe_error(exc)}): it goes after its hours")
