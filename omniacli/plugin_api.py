# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contract between OmniaCLI and its plugins.

A plugin adds a type of server (Odoo, an API, nginx...). It is a Python
package that declares, in its pyproject.toml, an entry point of the group
"omniacli.plugins" naming a subclass of ServicePlugin:

    [project.entry-points."omniacli.plugins"]
    odoo = "omniacli_odoo:OdooPlugin"

A plugin imports from OmniaCLI only this module: what is here is what
OmniaCLI keeps working across its versions for a given PLUGIN_API.

The core does, for every type, what is common: the git repositories (pull,
pending commits, merge of test into production), the service (restart, start,
stop of a systemd or init.d service, or a process OmniaCLI starts itself), the
jobs, the auto update, the log page, the users and the links. The plugin does
what only its type knows: its own settings of an instance, its checks, what
follows a pull, the backup, and the command of the process OmniaCLI starts.

A plugin may also give: the fields of its settings in the server form
(fields), buttons of its own on the server page (actions), routes of its own
under /x/<plugin>/ (router, with templates and static files of its own), and
its part of what the MCP tool dev_setup tells (dev_setup).
"""

from __future__ import annotations

import inspect
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Union

from . import ops as _ops
from .config import ROLES, SERVER_NAME, USER_NAME, ConfigError, Instance, Repository, Server
from .jobs import Job, JobFailed

# the version of this contract; a plugin declares the one it was written for
PLUGIN_API = 1
ENTRY_POINT_GROUP = "omniacli.plugins"

# what a plugin may run its programs with: output to the job, quietly, as
# another user, and the environment OmniaCLI runs programs with
run = _ops.run
capture = _ops.capture
as_user = _ops.as_user
# restart, start and stop an instance as the core does (its service, or the
# process it starts; start and stop: systemd or process only)
restart = _ops.restart
start = _ops.start
stop = _ops.stop
service_state = _ops.service_state

__all__ = [
    "PLUGIN_API", "ENTRY_POINT_GROUP", "ServicePlugin", "Check", "ERROR", "WARNING",
    "ConfigError", "Instance", "Repository", "Server", "Job", "JobFailed",
    "run", "capture", "as_user", "restart", "start", "stop", "service_state", "base_env", "system_user",
    "ROLES", "SERVER_NAME", "USER_NAME", "Field", "FormButton", "Action", "ActionRefused", "Host",
]


def base_env() -> dict:
    """The environment of the programs OmniaCLI runs (read at every call:
    it is not a copy taken when the plugin was imported)."""
    return _ops._ENV


def system_user() -> str:
    """The system user OmniaCLI runs as: the one a sudoers line names."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER", "omniacli")


ERROR, WARNING = "error", "warning"


@dataclass
class Check:
    """One line of the report of Test & activate. target: the instance's
    role (production, test) or "server"; a failed check of level error
    keeps the server a draft, a warning does not."""

    target: str
    name: str
    ok: bool
    detail: str
    level: str = ERROR

    def to_dict(self) -> dict:
        return asdict(self)


# --- the server form ------------------------------------------------------

FIELD_KINDS = ("text", "path", "number", "checkbox", "select")
# the keys of an instance's mapping that the core reads: no field may take them
CORE_KEYS = ("repositories", "restart", "log_file", "run_as", "from_link", "auto_update", "status",
             "type")
ROLE_NAMES = ("user", "developer", "admin")


@dataclass
class FormButton:
    """A button next to a field of the server form. It posts, as JSON
    {"name", "role", "values"}, what is typed in the form for the instance
    (saved or not; values has the plugin's fields, run_as and the repositories)
    to the plugin's route /x/<plugin>/<route>. The route answers either
    {"job": id}, whose page opens in a new tab, or {"fill": {key: value},
    "repositories": [...], "notes": [...]}, which the form takes (any of the
    three may be missing). It shows only while its field has a value."""

    label: str
    route: str
    title: str = ""
    # a question asked first; {key} is replaced by the form's value of key
    confirm: str = ""
    # False: shown also while its field is empty
    only_with_value: bool = True


@dataclass
class Field:
    """One setting of an instance: a key of its mapping in the servers file,
    shown in the server form and on the server page."""

    key: str
    label: str
    kind: str = "text"  # one of FIELD_KINDS; path: text shown in monospace
    default: Any = None
    # the form keeps an instance without a value of any required field a
    # draft (parse_options says what is missing)
    required: bool = False
    placeholder: str = ""
    help: str = ""
    choices: tuple = ()  # select: ((value, label), ...)
    # only with these restart types (empty: with any); disabled with the others
    restart_types: tuple = ()
    # placeholder while it is disabled
    disabled_placeholder: str = ""
    buttons: tuple = ()  # FormButton


# --- the buttons of the server page ------------------------------------------


class ActionRefused(Exception):
    """An action's parameters cannot be used: the caller gets the message (400)."""


Text = Union[str, Callable[..., str]]


@dataclass
class Action:
    """A button of the plugin on the server page. The core checks the
    caller's role, starts run as a job of the instance (or of every instance
    of the server: scope "server") and shows it; a job running on the
    instance waits, as every job does.

    run(job, target, params): target is the Instance (or the Server),
    params what prepare returned. It is posted to
    /servers/<server>/<role>/x/<plugin>/<id> (scope server:
    /servers/<server>/x/<plugin>/<id>) as a form or as JSON."""

    id: str
    label: str
    run: Callable
    # buttons with the same group stand together; the core's groups are
    # "service", "git" and "backup": an action may join them
    group: str = ""
    icon: str = ""  # an icon of the core: start stop restart github backup download python
    role: str = "developer"
    scope: str = "instance"
    tooltip: str = ""
    # a question before it runs; a callable gets the target
    confirm: Text = ""
    # the job's title; a callable gets (target, params); default: "<label> <key>"
    title: Text = ""
    # asked in a dialog of the core before it runs
    params: tuple = ()  # Field
    # check and complete the parameters: prepare(target, params) -> params;
    # raise ActionRefused when they cannot be used
    prepare: Callable | None = None
    # a template of the plugin (under its templates_dir) drawn once on the
    # server page: it opens its own dialog for the buttons of this action
    # (data-action="<plugin>.<id>", data-url, data-key, data-info: the
    # plugin's describe of the instance as JSON) and posts JSON to data-url,
    # which answers {"job": id}
    dialog: str = ""
    # whether the action applies to target (its button shows, it may run);
    # None: always
    available: Callable | None = None

    def applies_to(self, target) -> bool:
        return self.available is None or bool(self.available(target))

    def title_of(self, target, params: dict) -> str:
        if callable(self.title):
            return self.title(target, params)
        return self.title or f"{self.label} {getattr(target, 'key', getattr(target, 'name', ''))}"

    def confirm_of(self, target) -> str:
        return self.confirm(target) if callable(self.confirm) else self.confirm


# --- what the core gives the plugin's routes --------------------------------


class Host(ABC):
    """What OmniaCLI gives the plugin's router. Every route of the router
    needs a login (the core checks it); a route that needs more depends on
    needs(role)."""

    @abstractmethod
    def needs(self, role: str) -> Callable:
        """A FastAPI dependency, to use as Depends(host.needs(role)): the
        caller, refused (403) unless its role reaches role (user, developer,
        admin). The caller has name, role and can(role)."""

    @abstractmethod
    def instance(self, server: str, role: str) -> Instance:
        """An instance of a live server of the plugin's type; 404 otherwise."""

    @abstractmethod
    def submit(self, request, title: str, keys: list[str], action: Callable) -> Job:
        """Start action(job) as a job of the instances keys, by the caller."""

    @abstractmethod
    def backup_folder(self, instance: Instance) -> Path:
        """The folder of the instance's backups (the core's backup writes
        there, the server page lists them); it may not exist yet."""

    @abstractmethod
    def render(self, request, template: str, context: dict) -> Any:
        """A template of the plugin (its name under templates_dir), which may
        extend the core's base.html."""


class ServicePlugin(ABC):
    """A type of server. OmniaCLI makes one object of the class, without
    arguments, and calls it for every instance of that type.

    The settings of an instance are one mapping in the servers file: the core
    reads its own keys (repositories, restart, log_file, run_as, auto_update)
    and gives the plugin the whole mapping, of which it reads its own keys
    (parse_options). What parse_options returns is kept as instance.options.
    """

    # the type, as servers.yaml names it ("type: odoo"): lower case letters,
    # digits and _, at most 32
    name: str = ""
    # the name shown to people ("Odoo")
    label: str = ""
    # the PLUGIN_API this plugin was written for
    api_version: int = 0

    # --- what every plugin does ----------------------------------------

    @abstractmethod
    def parse_options(self, data: dict, where: str, restart_type: str) -> Any:
        """The plugin's settings of an instance, read from its mapping in the
        servers file. where: the place to name in a ConfigError
        ("servers.<name>.<role>"); restart_type: systemd, initd or process."""

    @abstractmethod
    def options_to_dict(self, options: Any) -> dict:
        """The settings back as the mapping parse_options reads (servers file)."""

    @abstractmethod
    async def checks(self, instance: Instance) -> list[Check]:
        """The plugin's checks of an instance for Test & activate (the core
        checks the log, the repositories and the service)."""

    @abstractmethod
    async def after_pull(self, job: Job, instance: Instance) -> None:
        """What follows a pull, before the restart (Odoo: the module update).
        Raise JobFailed to stop the deployment."""

    # --- what a plugin may do: the core offers it only if it is defined ---

    def server_checks(self, production: Instance, test: Instance) -> list[Check]:
        """Checks of a server having both instances (target "server")."""
        return []

    async def backup(self, job: Job, instance: Instance, dest_dir: Path, options: dict | None = None) -> Path:
        """Back up the instance into dest_dir (it exists), return the file.
        options: the values of backup_fields; None or a key left out: its
        default (the backup before a merge passes none)."""
        raise JobFailed(f"{self.label} has no backup")

    def backup_fields(self) -> list[Field]:
        """What may be chosen for a backup (checkbox or text), given as
        options. A checkbox not posted is off: its off must be the backup
        made without any choice."""
        return []

    async def restore(self, job: Job, instance: Instance, backup_zip: Path, options: dict) -> None:
        """Load a file of backup (made here or by a linked instance) into the
        instance, whose data it replaces. options: the values of
        restore_fields. Raise JobFailed before touching anything when the
        file does not fit the instance."""
        raise JobFailed(f"{self.label} has no restore")

    def restore_fields(self) -> list[Field]:
        """What is asked before a restore (checkbox or text), given as options."""
        return []

    def process_command(self, instance: Instance) -> tuple[list[str], Path]:
        """(command, working directory) of the process OmniaCLI starts itself
        (restart type process). Its output goes to the instance's log."""
        raise JobFailed(f"{self.label} cannot be started by OmniaCLI")

    def process_config(self, instance: Instance) -> Path | None:
        """The config file the instance's processes are started with (-c or
        --config <file>): how OmniaCLI finds them, to stop them and measure
        them. None: they cannot be found."""
        return None

    def web_port(self, instance: Instance) -> int | None:
        """The port of the instance's web interface (the Dashboard's link)."""
        return None

    def search_text(self, instance: Instance) -> list[str]:
        """Words the Dashboard's filter finds the instance by."""
        return []

    def describe(self, instance: Instance) -> dict:
        """What a linked instance and the MCP clients are told of the
        instance besides the core's fields (JSON values)."""
        return {}

    def disk_paths(self, instance: Instance) -> list[tuple[str, str]]:
        """[(what, path)] the instance writes to, for the disks panel."""
        return []

    def self_test(self) -> list[str]:
        """Problems that keep the plugin from working here; any disables it."""
        return []

    def fields(self) -> list[Field]:
        """The plugin's settings of an instance, as the server form shows
        them (parse_options reads them)."""
        return []

    def actions(self) -> list[Action]:
        """The plugin's buttons on the server page."""
        return []

    def router(self, host: Host) -> Any:
        """A FastAPI APIRouter, served under /x/<plugin>/; None: no routes."""
        return None

    def templates_dir(self) -> Path | None:
        """The folder of the plugin's templates (dialog, render)."""
        return None

    def static_dir(self) -> Path | None:
        """The folder of files served at /x/<plugin>/static/ (login needed)."""
        return None

    async def dev_setup(self, instance: Instance, repositories: list[dict]) -> dict:
        """What the MCP tool dev_setup tells of the instance besides the
        core's (repositories: remote URL, branch, commit; backups). It may
        add keys to the dicts of repositories."""
        return {}

    # what the MCP clients are told of the type (the server's instructions)
    mcp_help: str = ""

    # --- what the core asks ---------------------------------------------

    @property
    def has_backup(self) -> bool:
        return type(self).backup is not ServicePlugin.backup

    @property
    def has_process(self) -> bool:
        return type(self).process_command is not ServicePlugin.process_command

    @property
    def has_restore(self) -> bool:
        return type(self).restore is not ServicePlugin.restore


# the methods a plugin must write as async def
ASYNC_METHODS = ("checks", "after_pull", "backup", "restore", "dev_setup")
_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def contract_problems(cls) -> list[str]:
    """What keeps cls from being a plugin of this contract; empty when it is one."""
    if not (inspect.isclass(cls) and issubclass(cls, ServicePlugin)):
        return [f"{cls!r} is not a subclass of omniacli.plugin_api.ServicePlugin"]
    problems = []
    missing = sorted(getattr(cls, "__abstractmethods__", ()))
    if missing:
        problems.append(f"does not implement {', '.join(missing)}")
    if cls.api_version != PLUGIN_API:
        problems.append(f"written for plugin API {cls.api_version!r}, this OmniaCLI has {PLUGIN_API}")
    for method in ASYNC_METHODS:
        if not inspect.iscoroutinefunction(getattr(cls, method)):
            problems.append(f"{method} must be async def")
    return problems


def parts_problems(plugin: ServicePlugin) -> list[str]:
    """What is wrong in the fields and actions of a plugin made from a class
    that passed contract_problems."""
    problems = []
    keys = set()
    for f in plugin.fields():
        if not isinstance(f, Field):
            problems.append(f"field {f!r} is not a plugin_api.Field")
            continue
        if f.key in CORE_KEYS:
            problems.append(f"field {f.key} is a key of the core")
        if f.key in keys:
            problems.append(f"field {f.key} given twice")
        if f.kind not in FIELD_KINDS:
            problems.append(f"field {f.key}: kind {f.kind!r} is not one of {', '.join(FIELD_KINDS)}")
        keys.add(f.key)
    ids = set()
    templates = plugin.templates_dir()
    for a in plugin.actions():
        if not isinstance(a, Action):
            problems.append(f"action {a!r} is not a plugin_api.Action")
            continue
        if not _ID.fullmatch(a.id) or a.id in ids:
            problems.append(f"action id {a.id!r} is not unique lower case letters, digits, _ and -")
        ids.add(a.id)
        if not inspect.iscoroutinefunction(a.run):
            problems.append(f"action {a.id}: run must be async def")
        if a.role not in ROLE_NAMES:
            problems.append(f"action {a.id}: role {a.role!r} is not one of {', '.join(ROLE_NAMES)}")
        if a.scope not in ("instance", "server"):
            problems.append(f"action {a.id}: scope must be instance or server")
        if a.dialog and (templates is None or not (Path(templates) / a.dialog).is_file()):
            problems.append(f"action {a.id}: no template {a.dialog} in the plugin's templates_dir")
        if a.dialog and a.params:
            problems.append(f"action {a.id}: a dialog of its own or params, not both")
    for what, fields in (("backup", plugin.backup_fields()), ("restore", plugin.restore_fields())):
        for f in fields:
            if not isinstance(f, Field) or f.kind not in ("checkbox", "text"):
                problems.append(f"{what} field {f!r} is not a plugin_api.Field of kind checkbox or text")
    return problems
