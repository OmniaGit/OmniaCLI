# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo plugin: servers of type odoo.

It imports from OmniaCLI only omniacli.plugin_api, as a plugin of its own
package does.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..plugin_api import (
    PLUGIN_API,
    ROLES,
    SERVER_NAME,
    USER_NAME,
    Action,
    ActionRefused,
    Check,
    ConfigError,
    Field,
    FormButton,
    Host,
    Instance,
    Job,
    JobFailed,
    Server,
    ServicePlugin,
    capture,
    restart,
    start,
    stop,
)
from . import checks, discover, odooconf, ops

# Odoo config options that are secrets or belong to the server's own layout:
# never given out by dev_setup
_PRIVATE = re.compile(r"passw|secret|token|key|^(db_host|db_port|db_user|data_dir|logfile|pidfile"
                      r"|addons_path|upgrade_path|pre_upgrade_scripts)$")

# a backup the copy of production may load: a zip of the production's folder
_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")

MCP_HELP = """Servers of type odoo: update_test also updates the changed modules before
the restart. dev_setup gives the Odoo and Python versions, the addons_path as
repository folders and the Odoo config options without secrets: clone every
repository at its branch, write an odoo.conf with those options and an
addons_path of the local clones. A backup zip holds the database dump and the
filestore: restore it with click-odoo-restoredb or Odoo's database manager."""


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


def _on(value) -> bool:
    """A posted checkbox or JSON flag: off when missing or false-like."""
    return value not in (None, False, "", "0", "false", "off")


def _copy_what(params: dict) -> str:
    """What a copy of production loads, for the job title."""
    if params["backup"]:
        return params["backup"]
    if params.get("share_filestore"):
        return "new backup, filestore shared with production"
    return "new backup" + (" without filestore" if params["no_filestore"] else "")


def _filestores(production: Instance, test: Instance) -> tuple[Path, Path]:
    """The filestore folders of production and test, <data_dir>/filestore/<database>
    as Odoo makes them; refused when an Odoo config sets no data_dir (Odoo's
    default depends on the home of the user running it)."""
    found = []
    for inst in (production, test):
        data_dir = _data_dir(inst.options.odoo_config)
        if not data_dir:
            raise JobFailed(f"{inst.options.odoo_config} of {inst.key} sets no data_dir: "
                            "its filestore cannot be found to share it")
        found.append(Path(data_dir) / "filestore" / inst.options.database)
    return found[0], found[1]


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

    def __init__(self):
        # what the core gives the routes; the copy of production uses it too
        # (router, called by the core when it serves the plugin)
        self._host: Host | None = None

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

    async def backup(self, job: Job, instance: Instance, dest_dir: Path, options: dict | None = None) -> Path:
        return await ops.backup(job, instance, dest_dir, filestore=not (options or {}).get("no_filestore"))

    def backup_fields(self) -> list[Field]:
        return [Field("no_filestore", "Without filestore", kind="checkbox", default=False,
                      help="Database only, no attachments: a smaller backup. Restored, the instance has no "
                           "attachments (the restore drops its filestore)")]

    def process_command(self, instance: Instance) -> tuple[list[str], Path]:
        return ops.process_command(instance)

    def process_config(self, instance: Instance) -> Path | None:
        return instance.options.odoo_config

    def web_port(self, instance: Instance) -> int | None:
        return instance.options.http_port or odooconf.http_port(instance.options.odoo_config)

    def tunnel_address(self, instance: Instance, path: str) -> tuple[str, int] | None:
        port = self.web_port(instance)
        if not port:
            return None
        found = odooconf.tunnel_settings(instance.options.odoo_config)
        # Odoo listens on http_interface, all of them when it is unset
        host = found.get("interface") or "127.0.0.1"
        host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        # with workers, the bus (/websocket from Odoo 16, /longpolling
        # before) is served by the evented server on its own port
        if found.get("workers") and (path == "/websocket" or path.startswith(("/websocket/", "/longpolling"))):
            return host, found["evented_port"]
        return host, port

    def search_text(self, instance: Instance) -> list[str]:
        return [instance.options.database]

    def describe(self, instance: Instance) -> dict:
        return {"database": instance.options.database}

    def db_shell(self, instance: Instance) -> tuple[str, dict] | None:
        # the connection Odoo makes: its config's db_* options, in the
        # environment (a password is not on the command line)
        env = {"PGDATABASE": instance.options.database, "PGAPPNAME": "omniacli psql"}
        for key, value in odooconf.db_connection(instance.options.odoo_config).items():
            env[{"db_host": "PGHOST", "db_port": "PGPORT", "db_user": "PGUSER",
                 "db_password": "PGPASSWORD"}[key]] = value
        if instance.run_as is None:
            return "psql", env
        # with run_as, as the user Odoo runs as: PostgreSQL's peer login on
        # the local socket takes the system user; sudo keeps the PG*
        # variables (sudoers needs SETENV, the Run as check prints the line)
        return f"sudo -n -u {instance.run_as} --preserve-env={','.join(env)} {ops.psql_path()}", env

    def script_runner(self, instance: Instance) -> tuple[Path, str | None, dict]:
        # the venv's python, which imports odoo as click-odoo does
        return instance.options.venv_bin / "python", instance.run_as, ops.odoo_env(instance)

    def script_env(self, instance: Instance) -> dict:
        return {"OMNIACLI_DATABASE": instance.options.database,
                "OMNIACLI_ODOO_CONFIG": str(instance.options.odoo_config)}

    async def discover(self, known: list[Instance]) -> list:
        return await discover.discover(known)

    def disk_paths(self, instance: Instance) -> list[tuple[str, str]]:
        data_dir = _data_dir(instance.options.odoo_config)
        return [("filestore", data_dir)] if data_dir else []

    # --- the server form, the server page, the routes, MCP --------------

    mcp_help = MCP_HELP

    def templates_dir(self) -> Path:
        return Path(__file__).parent / "templates"

    def fields(self) -> list[Field]:
        return [
            Field("database", "Database", required=True),
            Field("odoo_config", "Odoo config file", "path", required=True, buttons=(
                FormButton("Reload", "form/config", only_with_value=False,
                           title="Read database, log file and repositories from this file"),)),
            Field("venv_bin", "Venv bin (click-odoo-contrib)", "path", required=True, buttons=(
                FormButton("pip requirements", "form/pip-requirements",
                           title="pip install, in this venv, the requirements.txt of Odoo and of the repositories below",
                           confirm="pip install the requirements.txt of Odoo and of the repositories in {venv_bin}?"),
                FormButton("pip click-odoo", "form/pip-cli",
                           title="pip install click-odoo-contrib, the scripts OmniaCLI runs, in this venv",
                           confirm="pip install click-odoo-contrib in {venv_bin}?"))),
            Field("http_port", "HTTP port", "number", restart_types=("process",),
                  placeholder="empty: the port of the Odoo config",
                  disabled_placeholder="only with restart type process",
                  help="Restart type process only: OmniaCLI starts odoo-bin with --http-port, "
                       "which wins over the Odoo config"),
            Field("odoo_bin", "odoo-bin (update of chosen modules)", "path",
                  placeholder="empty: odoo-bin found in a repository"),
            Field("update_modules", "Update changed modules", "checkbox", default=True),
        ]

    def actions(self) -> list[Action]:
        def as_whom(inst):
            return f" as {inst.run_as}" if inst.run_as else ""

        return [
            Action("update", "Update modules", self._update, group="git", icon="github",
                   dialog="update_dialog.html", prepare=_update_params, title=_update_title),
            Action("pip-requirements", "pip requirements", self._pip_requirements, group="python",
                   icon="python",
                   tooltip="pip install the requirements.txt of Odoo and of the repositories in the venv",
                   confirm=lambda inst: f"pip install, in {inst.options.venv_bin}{as_whom(inst)}, the "
                                        f"requirements.txt of Odoo and of every repository of {inst.key}?",
                   title=lambda inst, params: f"pip install requirements {inst.key}"),
            Action("pip-cli", "pip click-odoo", self._pip_cli, group="python", icon="python",
                   tooltip="pip install click-odoo-contrib, the scripts OmniaCLI runs in the venv",
                   confirm=lambda inst: f"pip install {ops.CLI_PACKAGE} in "
                                        f"{inst.options.venv_bin}{as_whom(inst)}?",
                   title=lambda inst, params: f"pip install {ops.CLI_PACKAGE} {inst.key}"),
            Action("copy-production", "Copy production to test", self._copy_production, scope="server",
                   icon="backup", dialog="copy_dialog.html", prepare=self._copy_params,
                   available=lambda srv: srv.production is not None and srv.test is not None,
                   tooltip="Load a backup of production (a new one or one already made) into the "
                           "test database, neutralized, then update the modules and start test",
                   title=lambda srv, params: f"Copy production to test {srv.name} ({_copy_what(params)})"),
        ]

    def _copy_params(self, server: Server, params: dict) -> dict:
        """The backup to load: empty for a new one, else a zip of production's backups."""
        name = str(params.get("backup") or "").strip()
        if name:
            if self._host is None:
                raise ActionRefused("the routes of the Odoo plugin are not served")
            path = self._host.backup_folder(server.production) / name
            if not _BACKUP_NAME.fullmatch(name) or not path.is_file():
                raise ActionRefused(f"No backup {name} of {server.production.key}")
        # without filestore, or sharing production's: of a new backup only
        share = not name and _on(params.get("share_filestore"))
        no_filestore = not name and not share and _on(params.get("no_filestore"))
        return {"backup": name, "no_filestore": no_filestore, "share_filestore": share}

    async def _copy_production(self, job: Job, server: Server, params: dict) -> None:
        """Back up production (or take a backup of it), stop test, restore the
        backup into the test database, update the changed modules, start test."""
        prod, test = server.production, server.test
        if self._host is None:
            raise JobFailed("the routes of the Odoo plugin are not served: no backup folder")
        majors = {}
        for inst in (prod, test):
            odoo_bin = ops.find_odoo_bin(inst)
            majors[inst.role] = ops.odoo_major(odoo_bin) if odoo_bin else None
        if None not in majors.values() and majors["production"] != majors["test"]:
            raise JobFailed(f"production runs Odoo {majors['production']}, test Odoo {majors['test']}: "
                            "a database is not loaded into another version")
        if majors["test"] is not None and majors["test"] >= 20:
            raise JobFailed("click-odoo-restoredb does not restore Odoo 20 and later")
        shared = None
        if params.get("share_filestore"):
            shared = _filestores(prod, test)
            await ops.share_filestore(job, test, *shared, check=True)
        folder = self._host.backup_folder(prod)
        if params["backup"]:
            backup_zip = folder / params["backup"]
            if not backup_zip.is_file():
                raise JobFailed(f"No backup {params['backup']} of {prod.key}")
            job.log(f"--- backup of production: {backup_zip.name}")
        else:
            folder.mkdir(parents=True, exist_ok=True)
            backup_zip = await self.backup(job, prod, folder,
                                           {"no_filestore": params["no_filestore"] or shared is not None})
        await self._restore(job, test, backup_zip, True, shared)

    def restore_fields(self) -> list[Field]:
        return [Field("neutralize", "Neutralize (Odoo 16 and later): no scheduled actions, no outgoing mail",
                      kind="checkbox", default=True),
                Field("reset_passwords", "Every user's password set to admin", kind="checkbox", default=True),
                Field("load_only", "Only load the database: not stopped, no module update, no restart "
                      "(development)", kind="checkbox", default=False)]

    async def restore(self, job: Job, instance: Instance, backup_zip: Path, options: dict) -> None:
        """Stop the instance, restore the backup into its database, update the
        changed modules, start it. Refused, before anything is touched, for a
        backup of another Odoo version and for Odoo 20 and later."""
        # a schedule saved before the option keeps its passwords
        await self._restore(job, instance, backup_zip, bool(options.get("neutralize", True)),
                            reset_passwords=bool(options.get("reset_passwords", False)),
                            load_only=bool(options.get("load_only", False)))

    async def _restore(self, job: Job, instance: Instance, backup_zip: Path, neutralize: bool,
                       shared: tuple[Path, Path] | None = None, reset_passwords: bool = False,
                       load_only: bool = False) -> None:
        """restore(); with shared (production's filestore, the instance's), the
        instance's filestore is made of hard links to production's files after
        the restore of a backup without filestore; with reset_passwords every
        user's password becomes admin; with load_only the instance is neither
        stopped nor updated nor restarted."""
        odoo_bin = ops.find_odoo_bin(instance)
        major = ops.odoo_major(odoo_bin) if odoo_bin else None
        found = ops.backup_major(backup_zip)
        if found is None:
            job.log(f"WARNING: {backup_zip.name} names no Odoo version (no manifest.json): not compared "
                    "with the instance's")
        elif major is not None and found != major:
            raise JobFailed(f"{backup_zip.name} is a database of Odoo {found}, {instance.key} runs Odoo "
                            f"{major}: a database is not loaded into another version")
        major = major if major is not None else found
        if major is not None and major >= 20:
            raise JobFailed("click-odoo-restoredb does not restore Odoo 20 and later")
        if reset_passwords and major is None:
            raise JobFailed("the Odoo version is unknown (no odoo-bin, no manifest.json in the backup): "
                            "the passwords cannot be set to admin")
        if neutralize and major is not None and major < 16:
            neutralize = False
            job.log(f"WARNING: Odoo {major} cannot be neutralized (Odoo 16 and later): the copy keeps "
                    "the scheduled actions and outgoing mail servers of the backup")
        elif not neutralize:
            job.log("WARNING: not neutralized: the copy keeps the scheduled actions and outgoing mail "
                    "servers of the backup")
        # init.d: no stop from here; Odoo's drop of the database ends its
        # connections, and the restart at the end starts it on the copy
        stoppable = instance.restart.type in ("systemd", "process") and not load_only
        if load_only:
            job.log(f"--- only the database is loaded: {instance.key} is not stopped, updated or restarted")
        elif stoppable:
            await stop(job, instance)
        else:
            job.log(f"--- {instance.key} is an init.d service: not stopped, restarted at the end")
        try:
            await ops.restore(job, instance, backup_zip, neutralize)
            if shared:
                await ops.share_filestore(job, instance, *shared)
            if reset_passwords:
                await ops.reset_passwords(job, instance, major)
        except JobFailed:
            job.log(f"{instance.key}" + (" is left stopped" if stoppable else " is not restarted")
                    + ": its database may be missing or half restored")
            raise
        if load_only:
            job.log(f"WARNING: the modules of {instance.key} are not updated and it is not restarted: "
                    "do it by hand when the code differs from the backup's")
            return
        await ops.update_modules(job, instance, force=True)
        if stoppable:
            await start(job, instance)
        else:
            await restart(job, instance)

    async def _update(self, job: Job, instance: Instance, params: dict) -> None:
        if params["mode"] == "chosen":
            await ops.update_chosen_modules(job, instance, params["modules"])
        else:
            await ops.update_modules(job, instance, force=True)
        if params["restart"]:
            await restart(job, instance)

    async def _pip_requirements(self, job: Job, instance: Instance, params: dict) -> None:
        await ops.install_requirements(job, instance.options.venv_bin, instance.options.odoo_bin,
                                       [r.path for r in instance.repositories], instance.run_as)

    async def _pip_cli(self, job: Job, instance: Instance, params: dict) -> None:
        await ops.install_cli(job, instance.options.venv_bin, instance.run_as)

    def router(self, host: Host):
        self._host = host
        router = APIRouter()

        def refuse(message: str):
            return JSONResponse({"error": message}, status_code=400)

        @router.get("/servers/{server}/{role}/addons")
        async def addons(server: str, role: str):
            """The modules the update dialog offers."""
            instance = host.instance(server, role)
            odoo_bin = ops.find_odoo_bin(instance)
            return {
                "addons": await asyncio.to_thread(ops.list_addons, instance),
                "odoo_bin": str(odoo_bin) if odoo_bin else None,
            }

        @router.post("/form/config", dependencies=[Depends(host.needs("admin"))])
        async def form_config(payload: dict = Body(...)):
            """The database, log file and repositories an Odoo config gives."""
            path = str((payload.get("values") or {}).get("odoo_config") or "").strip()
            if not path:
                return refuse("Set the Odoo config file first")
            try:
                found = await odooconf.read_odoo_config(path)
            except ConfigError as exc:
                return refuse(str(exc))
            return {"fill": {k: found[k] for k in ("database", "log_file") if found[k]},
                    "repositories": found["repositories"], "notes": found["notes"]}

        async def form_pip(request, payload: dict, kind: str):
            """The pip buttons of the server form: on the venv, odoo-bin and
            repository folders typed there, saved or not (a draft too)."""
            values = payload.get("values") or {}
            venv_bin = Path(str(values.get("venv_bin") or "").strip())
            if not venv_bin.is_absolute() or not os.access(venv_bin / "python", os.X_OK):
                return refuse(f"No python in {venv_bin}: set the venv's bin folder")
            odoo_text = str(values.get("odoo_bin") or "").strip()
            odoo_bin = Path(odoo_text) if odoo_text else None
            folders = [Path(str(r.get("path") or "").strip()) for r in values.get("repositories") or []
                       if isinstance(r, dict) and str(r.get("path") or "").strip()]
            folders = [p for p in folders if p.is_absolute() and p.is_dir()]
            run_as = str(values.get("run_as") or "").strip() or None
            if run_as is not None and not USER_NAME.fullmatch(run_as):
                return refuse(f"{run_as!r} is not a user name")
            name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
            # the instance's key when the form names one: its jobs wait for each other
            key = f"{name if SERVER_NAME.fullmatch(name) else 'form'}/{role if role in ROLES else 'venv'}"
            if kind == "requirements":
                job = host.submit(request, f"pip install requirements {key}", [key],
                                  lambda job: ops.install_requirements(job, venv_bin, odoo_bin, folders, run_as))
            else:
                job = host.submit(request, f"pip install {ops.CLI_PACKAGE} {key}", [key],
                                  lambda job: ops.install_cli(job, venv_bin, run_as))
            return {"job": job.id}

        @router.post("/form/pip-requirements", dependencies=[Depends(host.needs("admin"))])
        async def form_pip_requirements(request: Request, payload: dict = Body(...)):
            return await form_pip(request, payload, "requirements")

        @router.post("/form/pip-cli", dependencies=[Depends(host.needs("admin"))])
        async def form_pip_cli(request: Request, payload: dict = Body(...)):
            return await form_pip(request, payload, "cli")

        return router

    async def dev_setup(self, instance: Instance, repositories: list[dict]) -> dict:
        for repo, data in zip(instance.repositories, repositories):
            data["has_odoo_bin"] = (repo.path / "odoo-bin").is_file()
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(instance.options.odoo_config, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            pass
        options = dict(parser.items("options")) if parser.has_section("options") else {}
        addons = []
        for raw in options.get("addons_path", "").replace("\n", ",").split(","):
            if not raw.strip():
                continue
            folder = Path(raw.strip()).resolve()
            owner = next((r for r in instance.repositories
                          if folder == r.path.resolve() or r.path.resolve() in folder.parents), None)
            addons.append({"repository": owner.name, "subpath": str(folder.relative_to(owner.path.resolve()))}
                          if owner else {"outside_repositories": str(folder)})
        odoo_bin = ops.find_odoo_bin(instance)
        _, python = await capture([instance.options.venv_bin / "python", "-c",
                                   "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        return {
            "database": instance.options.database,
            "odoo_version": ops.odoo_major(odoo_bin) if odoo_bin else None,
            "python": python or None,
            "addons_path": addons,
            "odoo_conf_options": {k: v for k, v in options.items() if not _PRIVATE.search(k)},
        }


def _update_params(instance: Instance, params: dict) -> dict:
    """The update dialog's choice: the changed modules, or those chosen."""
    mode = params.get("mode")
    modules = [str(m).strip() for m in params.get("modules") or [] if str(m).strip()]
    if mode == "chosen":
        bad = [m for m in modules if not ops.ADDON_NAME.fullmatch(m)]
        if not modules or bad:
            raise ActionRefused(f"Invalid modules: {', '.join(bad)}" if bad else "Choose at least one module")
    elif mode != "changed":
        raise ActionRefused("mode must be 'changed' or 'chosen'")
    return {"mode": mode, "modules": modules if mode == "chosen" else [], "restart": bool(params.get("restart"))}


def _update_title(instance: Instance, params: dict) -> str:
    if params["mode"] == "chosen":
        title = f"Update {', '.join(params['modules'])} on {instance.key}"
    else:
        title = f"Update changed modules on {instance.key}"
    return title + (" + restart" if params["restart"] else "")
