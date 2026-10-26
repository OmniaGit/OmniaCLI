# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The relay side of a puller: the post office between a producer (an
OmniaCLI that cannot be reached, which links out to this one) and its
consumers (other OmniaCLI that link here too). Nobody connects to the
producer: it asks here for the consumers' commands, waiting up to WAIT
seconds for one (long-poll), and leaves its reports and its files here.

What passes is sealed by the two ends when the puller has a key
(pullcrypto): the relay keeps envelopes and files it cannot read, and
checks only who may put and take them (the links of the puller's
definition, config.PullerDef) and how much (the limits below).

- a command waits for the producer at most PICKUP seconds, then expires;
  once handed to the producer it is gone from here;
- a report gets a sequence number, and stays keep_hours for every consumer
  to read it, from the number it read last;
- a file stays until a consumer says it has it whole (DELETE), or
  keep_hours.

Every puller keeps its state in a folder of its own: state.json (commands,
last contacts), reports.jsonl and files/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

from .config import PullerDef
from .pullcrypto import WINDOW

log = logging.getLogger(__name__)

WAIT = 25.0  # the longest a poll is held open, seconds
PICKUP = WINDOW  # a command not taken by the producer by then expires
MAX_PENDING = 20  # commands waiting, per puller
MAX_MESSAGE = 4 << 20  # bytes of the body of a command or of a report
MAX_REPORTS = 50_000  # kept per puller, whatever their age
MAX_BATCH = 500  # reports in one answer to a consumer
MAX_FILES = 5  # files kept per puller
MIN_FREE = 1 << 30  # bytes a file upload leaves free on the disk
EXPIRED_KEPT = 50  # ids of expired commands still told to the consumers
CLEAN_EVERY = 30.0  # seconds between two cleanups (and saves of the contacts)
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CHUNK = 1 << 20


class PullerRefused(Exception):
    """A request the relay does not take: the message says why; status is
    the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Queued:
    qid: int
    at: float
    by: str  # the consumer's link
    envelope: dict

    def to_dict(self) -> dict:
        return {"qid": self.qid, "at": self.at, "by": self.by, "envelope": self.envelope}


@dataclass
class Report:
    seq: int
    at: float
    envelope: dict

    def to_dict(self) -> dict:
        return {"seq": self.seq, "at": self.at, "envelope": self.envelope}


@dataclass
class PullerState:
    """The live state of one puller, kept in folder."""

    name: str
    folder: Path
    commands: list[Queued] = field(default_factory=list)
    next_qid: int = 1
    expired: list[dict] = field(default_factory=list)  # {qid, at, by}, the newest last
    reports: list[Report] = field(default_factory=list)
    next_seq: int = 1
    contacts: dict[str, float] = field(default_factory=dict)  # {link: time of its last request}
    # reports dropped from memory since reports.jsonl was last written whole
    _dropped: int = 0
    _commands_event: Optional[asyncio.Event] = None
    _reports_event: Optional[asyncio.Event] = None

    @property
    def files_dir(self) -> Path:
        return self.folder / "files"

    @property
    def reports_file(self) -> Path:
        return self.folder / "reports.jsonl"

    @property
    def state_file(self) -> Path:
        return self.folder / "state.json"

    # --- waiting (the events are made in the loop: Python 3.9 binds them) ---

    def _event(self, which: str) -> asyncio.Event:
        attr = f"_{which}_event"
        if getattr(self, attr) is None:
            setattr(self, attr, asyncio.Event())
        return getattr(self, attr)

    def _notify(self, which: str) -> None:
        event = getattr(self, f"_{which}_event")
        if event is not None:
            event.set()
        setattr(self, f"_{which}_event", None)

    async def _wait(self, which: str, seconds: float) -> None:
        if seconds > 0:
            try:
                await asyncio.wait_for(self._event(which).wait(), seconds)
            except asyncio.TimeoutError:
                pass

    # --- on disk ------------------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            log.warning("puller %s: %s unreadable, started empty: %s", self.name, self.state_file, exc)
            data = {}
        self.commands = [Queued(int(c["qid"]), float(c["at"]), str(c["by"]), c["envelope"])
                         for c in data.get("commands") or [] if isinstance(c, dict)]
        self.next_qid = int(data.get("next_qid") or 1)
        self.expired = [e for e in data.get("expired") or [] if isinstance(e, dict)]
        self.contacts = {str(k): float(v) for k, v in (data.get("contacts") or {}).items()}
        self.next_seq = int(data.get("next_seq") or 1)
        self.reports = []
        try:
            with open(self.reports_file, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        item = json.loads(line)
                        self.reports.append(Report(int(item["seq"]), float(item["at"]), item["envelope"]))
                    except (ValueError, KeyError, TypeError):
                        continue  # a line cut by a crash
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("puller %s: %s unreadable: %s", self.name, self.reports_file, exc)
        if self.reports:
            self.next_seq = max(self.next_seq, self.reports[-1].seq + 1)

    def save(self) -> None:
        """state.json, atomically (the reports are appended as they come)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        data = {"commands": [c.to_dict() for c in self.commands], "next_qid": self.next_qid,
                "expired": self.expired, "contacts": self.contacts, "next_seq": self.next_seq}
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _append_report(self, report: Report) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.reports_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report.to_dict()) + "\n")

    def _rewrite_reports(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.reports_file.with_name(self.reports_file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for report in self.reports:
                fh.write(json.dumps(report.to_dict()) + "\n")
        os.replace(tmp, self.reports_file)
        self._dropped = 0

    # --- the producer ---------------------------------------------------------

    def add_report(self, envelope: dict, now: float) -> int:
        report = Report(self.next_seq, now, envelope)
        self.next_seq += 1
        self.reports.append(report)
        self._append_report(report)
        if len(self.reports) > MAX_REPORTS:
            self._dropped += len(self.reports) - MAX_REPORTS
            del self.reports[:len(self.reports) - MAX_REPORTS]
        self._notify("reports")
        return report.seq

    def take_commands(self) -> list[Queued]:
        taken, self.commands = self.commands, []
        if taken:
            self.save()
        return taken

    # --- the consumers --------------------------------------------------------

    def add_command(self, by: str, envelope: dict, now: float) -> Queued:
        if len(self.commands) >= MAX_PENDING:
            raise PullerRefused(f"{len(self.commands)} commands already wait for the producer of "
                                f"{self.name}: it does not take them", 429)
        cmd = Queued(self.next_qid, now, by, envelope)
        self.next_qid += 1
        self.commands.append(cmd)
        self.save()
        self._notify("commands")
        return cmd

    def reports_since(self, since: int) -> dict:
        """The reports after since (at most MAX_BATCH), and where they stand:
        first is the oldest still kept (a consumer that read less than first-1
        lost some); reset tells a since beyond the last one given (the
        puller's reports began again), answered from the oldest."""
        last = self.next_seq - 1
        reset = since > last
        if reset:
            since = 0
        found = [r for r in self.reports if r.seq > since][:MAX_BATCH]
        return {"reports": [r.to_dict() for r in found],
                "next": found[-1].seq if found else since,
                "first": self.reports[0].seq if self.reports else self.next_seq,
                "last": last, "reset": reset}

    # --- the files ------------------------------------------------------------

    def files(self) -> list[dict]:
        found = []
        try:
            entries = list(os.scandir(self.files_dir))
        except FileNotFoundError:
            return []
        for entry in entries:
            if FILE_NAME.fullmatch(entry.name) and entry.is_file():
                st = entry.stat()
                found.append({"name": entry.name, "size": st.st_size, "at": st.st_mtime})
        return sorted(found, key=lambda f: f["at"])

    def file_path(self, name: str) -> Path:
        if not FILE_NAME.fullmatch(name):
            raise PullerRefused(f"{name!r} is not a file name of letters, digits and . _ -")
        return self.files_dir / name

    # --- cleanup --------------------------------------------------------------

    def clean(self, keep_hours: int, now: float) -> None:
        """Commands past PICKUP, reports and files older than keep_hours."""
        late = [c for c in self.commands if now - c.at > PICKUP]
        if late:
            self.commands = [c for c in self.commands if now - c.at <= PICKUP]
            self.expired = (self.expired + [{"qid": c.qid, "at": c.at, "by": c.by} for c in late])[-EXPIRED_KEPT:]
        keep = keep_hours * 3600
        kept = [r for r in self.reports if now - r.at <= keep]
        if len(kept) < len(self.reports):
            self._dropped += len(self.reports) - len(kept)
            self.reports = kept
        # the file is written anew once a tenth of it is gone, not at every cleanup
        if self._dropped and self._dropped >= max(100, len(self.reports) // 10):
            self._rewrite_reports()
        for f in self.files():
            if now - f["at"] > keep:
                (self.files_dir / f["name"]).unlink(missing_ok=True)
        for part in self.files_dir.glob("*.part") if self.files_dir.is_dir() else ():
            # an upload is written by the request that holds it: one this old was cut off
            try:
                if now - part.stat().st_mtime > 3600:
                    part.unlink(missing_ok=True)
            except OSError:
                pass
        self.save()


class PullerHub:
    """The pullers of this instance (settings.pullers) and their state, in
    folder/<puller name>/."""

    def __init__(self, settings, folder: Path):
        self.settings = settings
        self.folder = folder
        self.states: dict[str, PullerState] = {}
        self._task: Optional[asyncio.Task] = None
        # uploads under way: {puller: number}, they count against MAX_FILES
        self._uploading: dict[str, int] = {}

    def definition(self, name: str) -> PullerDef:
        found = self.settings.pullers.get(name)
        if found is None:
            raise PullerRefused(f"No puller {name}", 404)
        return found

    def state(self, name: str) -> PullerState:
        self.definition(name)
        known = self.states.get(name)
        if known is None:
            known = PullerState(name, self.folder / name)
            known.load()
            self.states[name] = known
        return known

    def member(self, name: str, link: str, part: str) -> PullerState:
        """The state of puller name, for link acting as part (producer or
        consumer); a link that is not in the puller does not learn it exists."""
        found = self.settings.pullers.get(name)
        if found is None or found.part(link) is None:
            raise PullerRefused(f"No puller {name}", 404)
        if found.part(link) != part:
            raise PullerRefused(f"{link} is the {found.part(link)} of puller {name}, not its {part}", 403)
        state = self.state(name)
        state.contacts[link] = time.time()
        return state

    def forget(self, name: str) -> None:
        """A puller deleted: its state goes, files included."""
        state = self.states.pop(name, None)
        if state is not None:
            state._notify("commands")
            state._notify("reports")
        shutil.rmtree(self.folder / name, ignore_errors=True)

    # --- what the routes call -------------------------------------------------

    async def producer_poll(self, name: str, link: str, report: Optional[dict], wait: float) -> dict:
        """Leave report (if any), then wait up to wait seconds for commands."""
        state = self.member(name, link, "producer")
        now = time.time()
        seq = state.add_report(report, now) if report is not None else None
        wait = max(0.0, min(float(wait), WAIT))
        if not state.commands:
            await state._wait("commands", wait)
            if self.states.get(name) is not state:
                raise PullerRefused(f"No puller {name}", 404)
        return {"seq": seq, "commands": [c.to_dict() for c in state.take_commands()]}

    async def consumer_command(self, name: str, link: str, envelope: dict) -> dict:
        state = self.member(name, link, "consumer")
        cmd = state.add_command(link, envelope, time.time())
        return {"qid": cmd.qid, "expires": cmd.at + PICKUP}

    async def consumer_reports(self, name: str, link: str, since: int, wait: float) -> dict:
        state = self.member(name, link, "consumer")
        found = state.reports_since(since)
        if not found["reports"] and not found["reset"]:
            await state._wait("reports", max(0.0, min(float(wait), WAIT)))
            if self.states.get(name) is not state:
                raise PullerRefused(f"No puller {name}", 404)
            found = state.reports_since(since)
        return {**found, **self.info(name)}

    def info(self, name: str) -> dict:
        """What the relay itself knows of a puller: no contents."""
        definition, state = self.definition(name), self.state(name)
        return {
            "puller": name,
            "producer": {"link": definition.producer, "seen": state.contacts.get(definition.producer)},
            "consumers": [{"link": c, "seen": state.contacts.get(c)} for c in definition.consumers],
            "pending": [{"qid": c.qid, "at": c.at, "by": c.by} for c in state.commands],
            "expired": state.expired,
            "files": state.files(),
            "keep_hours": definition.keep_hours,
            "max_file_mib": definition.max_file_mib,
        }

    async def upload(self, name: str, link: str, file: str, chunks: AsyncIterator[bytes],
                     length: Optional[int]) -> dict:
        """A file of the producer, through file.part: whole or absent."""
        state = self.member(name, link, "producer")
        definition = self.definition(name)
        dest = state.file_path(file)
        limit = definition.max_file_mib << 20
        if length is not None and length > limit:
            raise PullerRefused(f"{file} is {length >> 20} MiB, puller {name} takes at most "
                                f"{definition.max_file_mib} MiB", 413)
        if len(state.files()) + self._uploading.get(name, 0) >= MAX_FILES:
            raise PullerRefused(f"puller {name} already keeps {MAX_FILES} files: they are taken "
                                f"by a consumer or deleted after {definition.keep_hours} h", 429)
        state.files_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(state.files_dir).free
        if length is not None and free - length < MIN_FREE:
            raise PullerRefused(f"{file}: {free >> 20} MiB free here, not enough for {length >> 20} MiB", 507)
        part = dest.with_name(dest.name + ".part")
        size = 0
        self._uploading[name] = self._uploading.get(name, 0) + 1
        try:
            with open(part, "wb") as fh:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise PullerRefused(f"{file} is over the {definition.max_file_mib} MiB of puller {name}", 413)
                    fh.write(chunk)
            if length is not None and size != length:
                raise PullerRefused(f"{file}: {size} of {length} bytes received")
            if self.states.get(name) is not state:
                raise PullerRefused(f"No puller {name}", 404)
            os.replace(part, dest)
        finally:
            self._uploading[name] -= 1
            part.unlink(missing_ok=True)
        log.info("puller %s: %s received, %d bytes", name, file, size)
        return {"file": file, "size": size}

    def download_path(self, name: str, link: str, file: str) -> Path:
        state = self.member(name, link, "consumer")
        path = state.file_path(file)
        if not path.is_file():
            raise PullerRefused(f"puller {name} has no file {file}", 404)
        return path

    def taken(self, name: str, link: str, file: str) -> dict:
        """A consumer has the file whole: it goes."""
        path = self.download_path(name, link, file)
        path.unlink(missing_ok=True)
        log.info("puller %s: %s taken by %s", name, file, link)
        return {"ok": True}

    # --- the cleanup loop -----------------------------------------------------

    def clean(self) -> None:
        now = time.time()
        for name, definition in list(self.settings.pullers.items()):
            try:
                self.state(name).clean(definition.keep_hours, now)
            except OSError as exc:
                log.warning("puller %s: cleanup failed: %s", name, exc)
        for name in [n for n in self.states if n not in self.settings.pullers]:
            self.states.pop(name, None)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for state in self.states.values():
            try:
                state.save()
            except OSError:
                pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(CLEAN_EVERY)
            try:
                # in the loop, not a thread: the routes change the same lists
                self.clean()
            except Exception:  # the loop must not die
                log.exception("pullers: cleanup failed")
