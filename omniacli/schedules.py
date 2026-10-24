# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduled actions: at the times of a crontab expression, on one instance,
a git pull of its repositories, a copy of a linked instance's database (as
Copy from link does) and a Python script of the scripts folder, each step
optional. Saved in their own file (schedules.yaml, next to the servers); the
last runs of each in a history file next to the jobs."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from croniter import croniter

from . import linkcopy, ops
from .config import ROLES, SERVER_NAME, ConfigError, LinkSource
from .jobs import FINISHED, Job, JobFailed, JobManager
from .links import link_headers

_logger = logging.getLogger(__name__)

SCHEDULE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
SCRIPT_NAME = re.compile(r"[A-Za-z0-9._-]+\.py")
# who acts toward the link when a schedule names nobody
DEFAULT_USER = "omniacli"
_USER = re.compile(r"[A-Za-z0-9._@-]{1,64}")
DEFAULT_HISTORY = 30
DEFAULT_KEEP_BACKUPS = 3
# the routes a copy calls there need the developer role, no more
LINK_ROLE = "developer"


@dataclass
class CopyStep:
    source: LinkSource
    # the choices of the backup there (Odoo: no_filestore) and of the restore
    # here (Odoo: neutralize), as the Copy from link dialog posts them
    backup_options: dict = field(default_factory=dict)
    restore_options: dict = field(default_factory=dict)
    # the zips of the link kept in the instance's backups, the new one
    # included: the older ones are deleted before the new backup starts
    keep_backups: int = DEFAULT_KEEP_BACKUPS


@dataclass
class ScriptStep:
    name: str
    args: str = ""


@dataclass
class Schedule:
    name: str
    instance: str  # server/role
    cron: str
    enabled: bool = True
    user: str = ""  # empty: DEFAULT_USER
    pull: bool = False
    copy: CopyStep | None = None
    script: ScriptStep | None = None
    history: int = DEFAULT_HISTORY

    @property
    def acting_user(self) -> str:
        return self.user or DEFAULT_USER

    @property
    def steps(self) -> list[str]:
        return [label for label, on in (("git pull", self.pull),
                                        (f"copy from {self.copy.source.label}" if self.copy else "", self.copy),
                                        (f"script {self.script.name}" if self.script else "", self.script))
                if on]


def check_cron(expr: str, where: str) -> str:
    """A crontab expression of 5 fields (minute hour day month weekday)."""
    expr = " ".join(str(expr or "").split())
    if len(expr.split(" ")) != 5 or not croniter.is_valid(expr):
        raise ConfigError(f"{where}: {expr!r} is not a crontab expression of 5 fields "
                          "(minute hour day-of-month month day-of-week)")
    return expr


def next_runs(expr: str, count: int = 3, now: datetime.datetime | None = None) -> list[datetime.datetime]:
    it = croniter(expr, now or datetime.datetime.now())
    return [it.get_next(datetime.datetime) for _ in range(count)]


def _positive(value, default: int, where: str) -> int:
    try:
        number = int(default if value in (None, "") else value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: {value!r} is not a number") from None
    if number < 1:
        raise ConfigError(f"{where}: at least 1")
    return number


def _options(value, where: str) -> dict:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: a mapping")
    return {str(k): v for k, v in value.items()}


def parse_schedule(name: str, data: dict) -> Schedule:
    where = f"schedules.{name}"
    if not SCHEDULE_NAME.fullmatch(name):
        raise ConfigError(f"{where}: the name is letters, digits and . _ - (at most 64)")
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping")
    instance = str(data.get("instance") or "").strip()
    server, _, role = instance.partition("/")
    if not SERVER_NAME.fullmatch(server) or role not in ROLES:
        raise ConfigError(f"{where}.instance: server/role, for example erp/test")
    user = str(data.get("user") or "").strip()
    if user and not _USER.fullmatch(user):
        raise ConfigError(f"{where}.user: {user!r} is not a user name")
    copy = None
    if data.get("copy"):
        raw = data["copy"]
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}.copy: a mapping")
        link = str(raw.get("link") or "").strip()
        src_server = str(raw.get("server") or "").strip()
        src_role = str(raw.get("role") or "production").strip()
        for key, value in (("link", link), ("server", src_server)):
            if not SERVER_NAME.fullmatch(value):
                raise ConfigError(f"{where}.copy.{key}: {value!r} is not a name of letters, digits and . _ -")
        if src_role not in ROLES:
            raise ConfigError(f"{where}.copy.role: one of {', '.join(ROLES)}")
        copy = CopyStep(LinkSource(link, src_server, src_role),
                        _options(raw.get("backup_options"), f"{where}.copy.backup_options"),
                        _options(raw.get("restore_options"), f"{where}.copy.restore_options"),
                        _positive(raw.get("keep_backups"), DEFAULT_KEEP_BACKUPS, f"{where}.copy.keep_backups"))
    script = None
    if data.get("script"):
        raw = data["script"]
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}.script: a mapping")
        script_name = str(raw.get("name") or "").strip()
        if not SCRIPT_NAME.fullmatch(script_name):
            raise ConfigError(f"{where}.script.name: a .py file of the scripts folder")
        args = str(raw.get("args") or "").strip()
        try:
            shlex.split(args)
        except ValueError as exc:
            raise ConfigError(f"{where}.script.args: {exc}") from None
        script = ScriptStep(script_name, args)
    schedule = Schedule(
        name=name, instance=instance, cron=check_cron(data.get("cron"), f"{where}.cron"),
        enabled=bool(data.get("enabled", True)), user=user, pull=bool(data.get("pull", False)),
        copy=copy, script=script, history=_positive(data.get("history"), DEFAULT_HISTORY, f"{where}.history"),
    )
    if not (schedule.pull or schedule.copy or schedule.script):
        raise ConfigError(f"{where}: choose at least one step (git pull, copy from link, script)")
    return schedule


def schedule_to_dict(schedule: Schedule) -> dict:
    data = {"instance": schedule.instance, "cron": schedule.cron, "enabled": schedule.enabled}
    if schedule.user:
        data["user"] = schedule.user
    data["pull"] = schedule.pull
    if schedule.copy:
        src = schedule.copy.source
        data["copy"] = {"link": src.link, "server": src.server, "role": src.role,
                        "backup_options": schedule.copy.backup_options,
                        "restore_options": schedule.copy.restore_options,
                        "keep_backups": schedule.copy.keep_backups}
    if schedule.script:
        data["script"] = {"name": schedule.script.name, "args": schedule.script.args}
    data["history"] = schedule.history
    return data


def parse_schedules(data) -> dict[str, Schedule]:
    if not data:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("schedules: a mapping of name: schedule")
    return {str(name): parse_schedule(str(name), item) for name, item in data.items()}


def load_schedules(path: Path) -> dict[str, Schedule]:
    """The schedules file; none yet is no schedules."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_schedules(data.get("schedules"))
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_schedules(path: Path, schedules: dict[str, Schedule]) -> None:
    text = yaml.safe_dump({"schedules": {n: schedule_to_dict(s) for n, s in schedules.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def list_scripts(folder: Path) -> list[str]:
    """The .py files directly in the scripts folder."""
    try:
        return sorted(p.name for p in folder.iterdir() if p.is_file() and SCRIPT_NAME.fullmatch(p.name))
    except OSError:
        return []


def prune_backups(job: Job, folder: Path, link: str, keep: int) -> None:
    """Delete the oldest zips brought from link (<link>_*.zip) in folder, so
    that the new one makes keep."""
    found = sorted((p for p in folder.glob(f"{link}_*.zip") if p.is_file()), key=lambda p: p.stat().st_mtime)
    for path in found[:max(0, len(found) - (keep - 1))]:
        job.log(f"--- delete the old backup {path.name} (keep {keep} from {link})")
        path.unlink(missing_ok=True)


# where the copy of a script another user runs is put: that user may not
# enter OmniaCLI's own folder (/var/lib/omniacli, mode 750)
SCRIPT_STAGING = Path("/var/tmp")


async def run_script(job: Job, instance, folder: Path, step: ScriptStep) -> None:
    """The script with the python the instance's plugin gives (Odoo: its
    venv's, as its run_as user), the instance's facts in OMNIACLI_* variables,
    in its first repository's folder. A non-zero exit fails the job."""
    path = folder / step.name
    if not SCRIPT_NAME.fullmatch(step.name) or not path.is_file():
        raise JobFailed(f"no script {step.name} in {folder}")
    python, run_as, base = instance.plugin.script_runner(instance)
    variables = {"OMNIACLI_SERVER": instance.server, "OMNIACLI_ROLE": instance.role,
                 "OMNIACLI_REPOSITORIES": json.dumps({r.name: str(r.path) for r in instance.repositories}),
                 **instance.plugin.script_env(instance)}
    env = {**base, **variables}
    cwd = instance.repositories[0].path if instance.repositories else None
    job.log(f"--- script {step.name}" + (f" as {run_as}" if run_as else ""))
    staging = None
    try:
        script = path
        if run_as:
            staging = Path(tempfile.mkdtemp(prefix="omniacli-script-", dir=SCRIPT_STAGING))
            staging.chmod(0o755)
            script = staging / step.name
            shutil.copyfile(path, script)
            script.chmod(0o644)
            # sudo resets the environment: the variables go on its command
            # line (sudoers needs SETENV, as for PYTHONPATH)
            extra = [f"{k}={v}" for k, v in variables.items()]
            if env.get("PYTHONPATH"):
                extra.insert(0, f"PYTHONPATH={env['PYTHONPATH']}")
            cmd = ["sudo", "-n", "-u", run_as, *extra, python, script, *shlex.split(step.args)]
        else:
            cmd = [python, script, *shlex.split(step.args)]
        await ops.run(job, cmd, cwd=cwd, env=env)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


class ScheduleHistory:
    """The last runs of each schedule: {name: [{time, trigger, job, status,
    message}]}, newest first, in a JSON file."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, list[dict]] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def add(self, schedule: Schedule, entry: dict) -> None:
        runs = self.data.setdefault(schedule.name, [])
        runs.insert(0, entry)
        del runs[schedule.history:]
        self._save()

    def runs(self, name: str, jobs: JobManager) -> list[dict]:
        """The runs, with the status of their jobs now (kept once finished)."""
        changed = False
        for entry in self.data.get(name, []):
            job = jobs.get(entry.get("job") or "")
            if job is not None and entry.get("status") != job.status:
                entry["status"], entry["message"] = job.status, job.error or entry.get("message", "")
                changed = changed or job.status in FINISHED
        if changed:
            self._save()
        return self.data.get(name, [])

    def keep_only(self, names) -> None:
        if set(self.data) - set(names):
            self.data = {n: r for n, r in self.data.items() if n in names}
            self._save()

    def rename(self, old: str, new: str) -> None:
        if old in self.data:
            self.data[new] = self.data.pop(old)
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            _logger.warning("Cannot write %s: %s", self.path, exc)


class Scheduler:
    """Fires each enabled schedule at the minutes its crontab matches. A run
    missed while OmniaCLI was stopped is not made up; one that finds a job
    on its instance is skipped."""

    def __init__(self, settings, jobs: JobManager, link_clients, history: ScheduleHistory):
        self.settings = settings
        self.jobs = jobs
        self.link_clients = link_clients
        self.history = history
        self.schedules: dict[str, Schedule] = {}
        self._task: asyncio.Task | None = None

    def load(self) -> None:
        self.schedules = load_schedules(self.settings.schedules_file)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        last = None
        while True:
            now = datetime.datetime.now()
            minute = now.replace(second=0, microsecond=0)
            # each minute once, however early a sleep ends
            due = [] if minute == last else list(self.schedules.values())
            last = minute
            for schedule in due:
                try:
                    if schedule.enabled and croniter.match(schedule.cron, minute):
                        self.fire(schedule, "cron")
                except Exception:  # noqa: BLE001 - the loop must survive
                    _logger.exception("Schedule %s crashed", schedule.name)
            # just past the next minute
            await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 0.5)

    def instance_of(self, schedule: Schedule):
        server, _, role = schedule.instance.partition("/")
        srv = self.settings.servers.get(server)
        if srv is None or not srv.active:
            return None
        return srv.instances.get(role)

    def fire(self, schedule: Schedule, trigger: str) -> Job | None:
        """Start the schedule's job now; None (and a history entry saying
        why) when it cannot start."""
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        instance = self.instance_of(schedule)
        problem = None
        if instance is None:
            problem = f"no live instance {schedule.instance}"
        elif self.jobs.is_busy(instance.key):
            problem = "skipped: another job is running on the instance"
        elif schedule.copy and not instance.plugin.has_restore:
            problem = f"{instance.plugin.label} cannot restore a database"
        elif schedule.copy and schedule.copy.source.link not in self.settings.link_targets:
            problem = f"no link {schedule.copy.source.link}: add it on the Links page"
        if problem:
            self.history.add(schedule, {"time": stamp, "trigger": trigger, "job": None,
                                        "status": "skipped", "message": problem})
            return None
        job = self.jobs.submit(f"Schedule {schedule.name} on {instance.key} ({', '.join(schedule.steps)})",
                               [instance.key], f"schedule {schedule.name} ({schedule.acting_user})",
                               lambda job: self.run(job, schedule, instance))
        self.history.add(schedule, {"time": stamp, "trigger": trigger, "job": job.id,
                                    "status": job.status, "message": ""})
        return job

    async def run(self, job: Job, schedule: Schedule, instance) -> None:
        if schedule.pull and not schedule.copy:
            # as the auto update: the module update and the restart only when
            # something new came
            await ops.pull_restart(job, instance, only_if_changed=True)
        elif schedule.pull:
            # the restore that follows updates the modules and starts it
            await ops.pull(job, instance)
        if schedule.copy:
            await self._copy(job, schedule.copy, instance, schedule.acting_user)
        if schedule.script:
            await run_script(job, instance, self.settings.scripts_dir, schedule.script)

    async def _copy(self, job: Job, step: CopyStep, instance, user: str) -> None:
        link = self.settings.link_targets.get(step.source.link)
        if link is None:
            raise JobFailed(f"no link {step.source.link}: add it on the Links page")
        folder = self.settings.backup_dir / instance.server / instance.role
        if folder.is_dir():
            prune_backups(job, folder, link.name, step.keep_backups)
        headers = link_headers(link, user, LINK_ROLE)
        progress = job.result["progress"] = {
            "steps": [{"id": "backup", "label": "Backup", "where": link.name},
                      {"id": "download", "label": "Transfer", "where": f"{link.name} → here"},
                      {"id": "restore", "label": "Restore", "where": instance.key}],
            "step": "backup", "done": 0, "total": 0,
        }
        dest = await linkcopy.fetch_backup(job, self.link_clients.client(link), link, headers, step.source,
                                           "", folder, progress, step.backup_options)
        progress["step"] = "restore"
        await instance.plugin.restore(job, instance, dest, step.restore_options)
