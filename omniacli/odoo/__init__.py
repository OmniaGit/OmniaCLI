# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo plugin: servers of type odoo.

It imports from OmniaCLI only omniacli.plugin_api, as a plugin of its own
package does.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

from ..plugin_api import PLUGIN_API, Check, ConfigError, Instance, Job, ServicePlugin
from . import checks, odooconf, ops


@dataclass
class OdooOptions:
    database: str
    odoo_config: Path
    # bin directory of the venv that runs this Odoo, where click-odoo-contrib
    # is installed (its scripts must import the instance's own odoo package)
    venv_bin: Path
    update_modules: bool = True
    # odoo-bin for the update of chosen modules; None: look in the repositories
    odoo_bin: Path | None = None
    # the port Odoo listens on, passed to the odoo-bin OmniaCLI starts (restart
    # type process only); None: the port of the Odoo config
    http_port: int | None = None


def _require(data: dict, key: str, where: str):
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: missing '{key}'")
    return data[key]


def _data_dir(odoo_config: Path) -> str | None:
    """The data_dir option of an Odoo config, None when it is not set or not readable."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(odoo_config, encoding="utf-8") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    value = (parser.get("options", "data_dir", fallback="") or "").strip()
    return None if value in ("", "False", "None") else os.path.expanduser(value)


class OdooPlugin(ServicePlugin):
    name = "odoo"
    label = "Odoo"
    api_version = PLUGIN_API

    def parse_options(self, data: dict, where: str, restart_type: str) -> OdooOptions:
        http_port = data.get("http_port")
        if http_port in (None, "", 0):
            http_port = None
        else:
            if isinstance(http_port, bool) or not str(http_port).isdigit() or not 1 <= int(http_port) <= 65535:
                raise ConfigError(f"{where}.http_port: {http_port!r} is not a port (1-65535)")
            http_port = int(http_port)
            if restart_type != "process":
                # with a service, its unit or script starts odoo-bin, not OmniaCLI
                raise ConfigError(f"{where}.http_port: only with restart type process; "
                                  "for a service set it in the Odoo config")
        return OdooOptions(
            database=str(_require(data, "database", where)),
            odoo_config=Path(_require(data, "odoo_config", where)),
            venv_bin=Path(_require(data, "venv_bin", where)),
            update_modules=bool(data.get("update_modules", True)),
            odoo_bin=Path(data["odoo_bin"]) if data.get("odoo_bin") else None,
            http_port=http_port,
        )

    def options_to_dict(self, options: OdooOptions) -> dict:
        data = {
            "database": options.database,
            "odoo_config": str(options.odoo_config),
            "venv_bin": str(options.venv_bin),
            "update_modules": options.update_modules,
        }
        if options.odoo_bin is not None:
            data["odoo_bin"] = str(options.odoo_bin)
        if options.http_port is not None:
            data["http_port"] = options.http_port
        return data

    async def checks(self, instance: Instance) -> list[Check]:
        return await checks.instance_checks(instance)

    def server_checks(self, production: Instance, test: Instance) -> list[Check]:
        return checks.server_checks(production, test)

    async def after_pull(self, job: Job, instance: Instance) -> None:
        await ops.update_modules(job, instance)

    async def backup(self, job: Job, instance: Instance, dest_dir: Path) -> Path:
        return await ops.backup(job, instance, dest_dir)

    def process_command(self, instance: Instance) -> tuple[list[str], Path]:
        return ops.process_command(instance)

    def process_config(self, instance: Instance) -> Path | None:
        return instance.options.odoo_config

    def web_port(self, instance: Instance) -> int | None:
        return instance.options.http_port or odooconf.http_port(instance.options.odoo_config)

    def search_text(self, instance: Instance) -> list[str]:
        return [instance.options.database]

    def describe(self, instance: Instance) -> dict:
        return {"database": instance.options.database}

    def disk_paths(self, instance: Instance) -> list[tuple[str, str]]:
        data_dir = _data_dir(instance.options.odoo_config)
        return [("filestore", data_dir)] if data_dir else []
