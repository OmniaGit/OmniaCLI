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
"""

from __future__ import annotations

import inspect
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import ops as _ops
from .config import ConfigError, Instance, Repository, Server
from .jobs import Job, JobFailed

# the version of this contract; a plugin declares the one it was written for
PLUGIN_API = 1
ENTRY_POINT_GROUP = "omniacli.plugins"

# what a plugin may run its programs with: output to the job, quietly, as
# another user, and the environment OmniaCLI runs programs with
run = _ops.run
capture = _ops.capture
as_user = _ops.as_user

__all__ = [
    "PLUGIN_API", "ENTRY_POINT_GROUP", "ServicePlugin", "Check", "ERROR", "WARNING",
    "ConfigError", "Instance", "Repository", "Server", "Job", "JobFailed",
    "run", "capture", "as_user", "base_env", "system_user",
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

    async def backup(self, job: Job, instance: Instance, dest_dir: Path) -> Path:
        """Back up the instance into dest_dir (it exists), return the file."""
        raise JobFailed(f"{self.label} has no backup")

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

    # --- what the core asks ---------------------------------------------

    @property
    def has_backup(self) -> bool:
        return type(self).backup is not ServicePlugin.backup

    @property
    def has_process(self) -> bool:
        return type(self).process_command is not ServicePlugin.process_command


# the methods a plugin must write as async def
ASYNC_METHODS = ("checks", "after_pull", "backup")


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
