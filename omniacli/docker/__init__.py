# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo in Docker Compose plugin: servers of type docker.

An instance is an Odoo 18 or later that runs in a compose project (restart
type compose, its service the project's folder), from a DockerOdooPLM stack
or the official odoo image alike. Everything it does goes through the
project's root helper (omniacli compose-helper, composehelper.py), which
refuses a project the caller could change:

- after a pull, click-odoo-update in a container of its own of the Odoo
  service, OmniaCLI's tools mounted (no image has to carry them);
- the backup is odoo db dump, the zip named as the Odoo plugin names it;
  the restore odoo db load --force, neutralized or not, between a stop and
  a start, then the modules updated;
- the log is docker compose logs of the Odoo service; the database shell
  psql in the database service.

Copy production to test: a new backup of production (kept with its
backups), loaded into test's database neutralized (odoo db load: a new
database.uuid), the changed modules updated, test started; test's filestore
shared with production through hard links (the default: the backup without
filestore, then the helper's link-filestore; Odoo 19 and later), copied, or
none.

Not in it yet: setting every password to admin, discover, the CPU and
memory charts.

It imports from OmniaCLI only omniacli.plugin_api, as a plugin of its own
package does.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import shlex
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..plugin_api import (
    COMPOSE_TOOLS,
    ERROR,
    PLUGIN_API,
    WARNING,
    Action,
    ActionRefused,
    Check,
    ConfigError,
    Field,
    Instance,
    Job,
    JobFailed,
    Server,
    ServicePlugin,
    capture,
    restart,
    run,
    start,
    stop,
)

# the first Odoo with odoo db dump/load in its image's command line
MIN_MAJOR = 18
# odoo db dump --no-filestore: from Odoo 19
NO_FILESTORE_MAJOR = 19
# the port Odoo listens on in its container
ODOO_PORT = 8069
# the name of a backup made without the filestore ends so (as the Odoo plugin's)
NO_FILESTORE = "_nofilestore"
DATABASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
MODULE = re.compile(r"[a-z0-9][a-z0-9_]{0,99}")
_VERSION = re.compile(r"Odoo Server (?:saas~)?(\d+)")
HELPER_TIMEOUT = 120
# a dump or a load of a big database
DATA_TIMEOUT = 6 * 3600
# Copy production to test: what test's filestore becomes
COPY_FILESTORE = (("share", "shared with production (hard links, no extra disk space)"),
                  ("copy", "a copy of production's"),
                  ("none", "none (test without attachments)"))

MCP_HELP = """Servers of type docker: Odoo 18 or later in a Docker Compose project, driven
through OmniaCLI's root helper. update_test updates the changed modules
(click-odoo-update) before the restart. A backup zip is odoo db dump's: the
database dump and the filestore, restored with odoo db load or Odoo's
database manager. The log is docker compose logs of the Odoo service."""


@dataclass
class DockerOptions:
    database: str
    odoo_service: str = "odoo"
    db_service: str = "db"
    http_port: int | None = None
    update_modules: bool = True


def _require(data: dict, key: str, where: str):
    value = data.get(key)
    if value in (None, ""):
        raise ConfigError(f"{where}: {key} is required")
    return value


def odoo_major(version: str) -> int | None:
    """From odoo --version: Odoo Server 20.0-20260926 -> 20."""
    found = _VERSION.search(version)
    return int(found.group(1)) if found else None


def backup_major(backup_zip: Path) -> int | None:
    """The major Odoo version of the database in a backup zip, from its
    manifest.json (odoo db dump and Odoo's database manager write one)."""
    try:
        with zipfile.ZipFile(backup_zip) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if not isinstance(manifest, dict):
        return None
    found = re.match(r"(?:saas~)?(\d+)", str(manifest.get("major_version") or manifest.get("version") or ""))
    return int(found.group(1)) if found else None


async def _helper(job: Job, instance: Instance, action: str, *args: str,
                  stdin: Path | None = None, stdout: Path | None = None,
                  timeout: float = DATA_TIMEOUT) -> None:
    """The helper's action with its output in the job log, or a file to
    the helper's input (stdin) or its output into a file (stdout): the zip
    of a load or of a dump. Its error output always goes to the job log."""
    cmd = instance.restart.command(action, *args)
    if stdin is None and stdout is None:
        await run(job, cmd, timeout=timeout)
        return
    job.log(f"$ {shlex.join(cmd)}" + (f" < {stdin}" if stdin else "") + (f" > {stdout}" if stdout else ""))
    with open(stdin, "rb") if stdin else open("/dev/null", "rb") as source, \
            open(stdout, "wb") if stdout else open("/dev/null", "wb") as dest:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=source, stdout=dest if stdout else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if stdout else asyncio.subprocess.STDOUT,
            start_new_session=True)
        stream = proc.stderr if stdout else proc.stdout

        async def follow():
            async for line in stream:
                job.log(line.decode(errors="replace").rstrip())

        try:
            await asyncio.wait_for(asyncio.gather(follow(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise JobFailed(f"{action} did not end within {timeout:.0f} s")
    if proc.returncode:
        raise JobFailed(f"{action} ended with exit code {proc.returncode}")


async def instance_major(instance: Instance) -> int | None:
    """The major Odoo version the instance's Odoo service runs (None: unknown)."""
    code, out = await capture(instance.restart.command("version", instance.options.odoo_service),
                              timeout=HELPER_TIMEOUT)
    return odoo_major(out) if code == 0 else None


class DockerPlugin(ServicePlugin):
    name = "docker"
    label = "Odoo (Docker Compose)"
    api_version = PLUGIN_API
    mcp_help = MCP_HELP

    @property
    def has_compose(self) -> bool:
        return True

    def parse_options(self, data: dict, where: str, restart_type: str) -> DockerOptions:
        if restart_type != "compose":
            raise ConfigError(f"{where}.restart: {self.label} runs in Docker Compose: restart type compose")
        database = str(_require(data, "database", where))
        if not DATABASE.fullmatch(database):
            raise ConfigError(f"{where}.database: {database!r} is not a database name")
        services = {}
        for key, default in (("odoo_service", "odoo"), ("db_service", "db")):
            services[key] = str(data.get(key) or default)
            if not SERVICE.fullmatch(services[key]):
                raise ConfigError(f"{where}.{key}: {services[key]!r} is not a compose service name")
        http_port = data.get("http_port")
        if http_port in (None, "", 0):
            http_port = None
        elif isinstance(http_port, bool) or not str(http_port).isdigit() or not 1 <= int(http_port) <= 65535:
            raise ConfigError(f"{where}.http_port: {http_port!r} is not a port (1-65535)")
        else:
            http_port = int(http_port)
        return DockerOptions(database=database, http_port=http_port,
                             update_modules=bool(data.get("update_modules", True)), **services)

    def options_to_dict(self, options: DockerOptions) -> dict:
        data = {"database": options.database, "odoo_service": options.odoo_service,
                "db_service": options.db_service, "update_modules": options.update_modules}
        if options.http_port is not None:
            data["http_port"] = options.http_port
        return data

    # --- checks ----------------------------------------------------------

    async def checks(self, instance: Instance) -> list[Check]:
        target = instance.key
        opts = instance.options
        found = []
        code, out = await capture(instance.restart.command("version", opts.odoo_service), timeout=HELPER_TIMEOUT)
        major = odoo_major(out) if code == 0 else None
        if code:
            found.append(Check(target, "Odoo", False, f"odoo --version in {opts.odoo_service}: "
                               f"{out.splitlines()[-1] if out else code}"))
        elif major is None or major < MIN_MAJOR:
            found.append(Check(target, "Odoo", False, f"{out.strip()}: {self.label} needs Odoo {MIN_MAJOR} or later"))
        else:
            found.append(Check(target, "Odoo", True, out.strip()))
        if opts.http_port is not None:
            code, out = await capture(instance.restart.command("port", opts.odoo_service, str(ODOO_PORT)),
                                      timeout=HELPER_TIMEOUT)
            published = out.strip().rsplit(":", 1)[-1] if code == 0 else ""
            found.append(Check(target, "HTTP port", published == str(opts.http_port),
                               f"{opts.odoo_service}:{ODOO_PORT} is published on {out.strip()}"
                               if code == 0 else f"{opts.odoo_service}:{ODOO_PORT} is not published: {out.strip()}"))
        tool = COMPOSE_TOOLS / "bin" / "click-odoo-update"
        found.append(Check(target, "Tools", tool.is_file(),
                           f"{tool} is there" if tool.is_file() else
                           f"{tool} is missing: the update of the changed modules fails (the package installs it)",
                           ERROR if opts.update_modules else WARNING))
        code, out = await capture(instance.restart.command("state"), timeout=HELPER_TIMEOUT)
        if code == 0:
            present = set()
            for line in out.splitlines():
                try:
                    present.add(json.loads(line).get("Service"))
                except (ValueError, AttributeError):
                    continue  # a warning of docker compose
            missing = [s for s in (opts.odoo_service, opts.db_service) if s not in present]
            found.append(Check(target, "Services", not missing,
                               f"{opts.odoo_service} and {opts.db_service} are services of the project" if not missing
                               else f"no container of {', '.join(missing)} in the project (never started, "
                                    "or not one of its services)"))
        return found

    # --- what follows a pull, backup, restore ------------------------------

    async def after_pull(self, job: Job, instance: Instance) -> None:
        await self._update_changed(job, instance)

    async def _update_changed(self, job: Job, instance: Instance, force: bool = False) -> None:
        if not instance.options.update_modules and not force:
            job.log("Module update disabled for this instance")
            return
        job.log("--- update changed modules")
        await _helper(job, instance, "update", instance.options.odoo_service, instance.options.database)

    async def backup(self, job: Job, instance: Instance, dest_dir: Path, options: dict | None = None) -> Path:
        filestore = not (options or {}).get("no_filestore")
        opts = instance.options
        if not filestore:
            major = await instance_major(instance)
            if major is not None and major < NO_FILESTORE_MAJOR:
                raise JobFailed(f"Odoo {major} cannot dump a database without its filestore "
                                f"(from Odoo {NO_FILESTORE_MAJOR}): make a backup with the filestore")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{opts.database}_{stamp}{'' if filestore else NO_FILESTORE}.zip"
        job.log(f"--- backup database {opts.database}" + ("" if filestore else " without its filestore")
                + f" to {dest}")
        partial = dest.with_suffix(".zip.part")
        try:
            await _helper(job, instance, "dump", opts.odoo_service, opts.database,
                          *([] if filestore else ["--no-filestore"]), stdout=partial)
            if not zipfile.is_zipfile(partial):
                raise JobFailed(f"the dump of {opts.database} is not a zip")
            partial.rename(dest)
        finally:
            partial.unlink(missing_ok=True)
        return dest

    def backup_fields(self) -> list[Field]:
        return [Field("no_filestore", "Without filestore", kind="checkbox", default=False,
                      help=f"Database only, no attachments: a smaller backup (Odoo {NO_FILESTORE_MAJOR} and "
                           "later). Restored, the instance has no attachments")]

    def restore_fields(self) -> list[Field]:
        return [Field("neutralize", "Neutralize: no scheduled actions, no outgoing mail", kind="checkbox",
                      default=True),
                Field("load_only", "Only load the database: not stopped, no module update, no restart "
                      "(development)", kind="checkbox", default=False)]

    async def restore(self, job: Job, instance: Instance, backup_zip: Path, options: dict) -> None:
        """Stop the instance, load the backup into its database (dropped
        first), update the changed modules, start it. Refused, before
        anything is touched, for a backup of another Odoo version."""
        opts = instance.options
        neutralize = bool(options.get("neutralize", True))
        load_only = bool(options.get("load_only", False))
        major = await instance_major(instance)
        found = backup_major(backup_zip)
        if found is None:
            job.log(f"WARNING: {backup_zip.name} names no Odoo version (no manifest.json): not compared "
                    "with the instance's")
        elif major is not None and found != major:
            raise JobFailed(f"{backup_zip.name} is a database of Odoo {found}, {instance.key} runs Odoo "
                            f"{major}: a database is not loaded into another version")
        if not neutralize:
            job.log("WARNING: not neutralized: the copy keeps the scheduled actions and outgoing mail "
                    "servers of the backup")
        if load_only:
            job.log(f"--- only the database is loaded: {instance.key} is not stopped, updated or restarted")
        else:
            await stop(job, instance)
        job.log(f"--- restore {backup_zip.name} into database {opts.database}"
                + (" (neutralized)" if neutralize else ""))
        try:
            # in a container of its own, which starts the database it needs
            await _helper(job, instance, "load", opts.odoo_service, opts.database,
                          *(["--neutralize"] if neutralize else []), stdin=backup_zip)
            if not load_only:
                await self._update_changed(job, instance, force=True)
        except JobFailed:
            job.log(f"{instance.key}" + ("" if load_only else " is left stopped")
                    + ": its database may be missing or half restored")
            raise
        if load_only:
            job.log(f"WARNING: the modules of {instance.key} are not updated and it is not restarted: "
                    "do it by hand when the code differs from the backup's")
            return
        await start(job, instance)

    # --- the pages ---------------------------------------------------------

    def log_service(self, instance: Instance) -> str | None:
        return instance.options.odoo_service

    def web_port(self, instance: Instance) -> int | None:
        return instance.options.http_port

    def tunnel_address(self, instance: Instance, path: str) -> tuple[str, int] | None:
        port = self.web_port(instance)
        return ("127.0.0.1", port) if port else None

    def search_text(self, instance: Instance) -> list[str]:
        return [instance.options.database, instance.restart.service]

    def describe(self, instance: Instance) -> dict:
        return {"database": instance.options.database, "project": instance.restart.service}

    def db_shell(self, instance: Instance) -> tuple[str, dict] | None:
        opts = instance.options
        return shlex.join(instance.restart.command("psql", opts.db_service, opts.database)), {}

    def fields(self) -> list[Field]:
        return [
            Field("database", "Database", required=True),
            Field("odoo_service", "Odoo service", default="odoo", placeholder="odoo",
                  help="The service of the compose project that runs Odoo: its log, its backups, its updates"),
            Field("db_service", "Database service", default="db", placeholder="db",
                  help="The PostgreSQL service of the compose project: the database shell"),
            Field("http_port", "HTTP port", "number", placeholder="the port the machine publishes Odoo on",
                  help=f"Where this machine publishes the Odoo service's port {ODOO_PORT}: the link of the "
                       "instance and its tunnel; the HTTP port check compares it with docker's"),
            Field("update_modules", "Update changed modules", "checkbox", default=True),
        ]

    _host = None

    def router(self, host):
        # no routes: the host gives the folder of production's backups
        self._host = host
        return None

    def actions(self) -> list[Action]:
        return [
            Action("copy-production", "Copy production to test", self._copy_production, scope="server",
                   icon="backup",
                   params=(Field("filestore", "Filestore of test", "select", default="share",
                                 choices=COPY_FILESTORE,
                                 help="Shared: production and test on the same filesystem, Odoo "
                                      f"{NO_FILESTORE_MAJOR} and later"),),
                   prepare=_copy_params,
                   available=lambda srv: srv.production is not None and srv.test is not None,
                   tooltip="A new backup of production loaded into the test database, neutralized, "
                           "then the changed modules updated and test started",
                   confirm=lambda srv: f"Replace the database and the filestore of {srv.name} test with "
                                       "a copy of production's?",
                   title=lambda srv, params: f"Copy production to test {srv.name} "
                                             f"(filestore: {params['filestore']})"),
            Action("update", "Update modules", self._update, group="git", icon="github",
                   params=(Field("modules", "Modules, comma separated",
                                 placeholder="empty: the changed ones (click-odoo-update)"),
                           Field("restart", "Restart after the update", "checkbox", default=False)),
                   prepare=_update_params, title=_update_title),
            Action("pull-image", "Pull image", self._pull_image, group="service", icon="download",
                   tooltip="docker compose pull, then up -d: the containers made again with the newer images",
                   confirm=lambda inst: f"Pull the newer images of {inst.restart.service} and make its "
                                        "containers again?",
                   title=lambda inst, params: f"Pull image {inst.key}"),
        ]

    async def _update(self, job: Job, instance: Instance, params: dict) -> None:
        opts = instance.options
        if params["modules"]:
            job.log(f"--- update {', '.join(params['modules'])}")
            await _helper(job, instance, "update", opts.odoo_service, opts.database,
                          "--modules", ",".join(params["modules"]))
        else:
            await self._update_changed(job, instance, force=True)
        if params["restart"]:
            await restart(job, instance)

    async def _copy_production(self, job: Job, server: Server, params: dict) -> None:
        """Back up production, stop test, load the backup into its database
        neutralized, share or not production's filestore, update the
        changed modules, start test."""
        prod, test = server.production, server.test
        mode = params["filestore"]
        if self._host is None:
            raise JobFailed("the plugin was given no host: no folder for production's backup")
        major = await instance_major(prod)
        if major is None:
            raise JobFailed(f"the Odoo version of {prod.key} is not known: is it running?")
        test_major = await instance_major(test)
        if test_major is not None and test_major != major:
            raise JobFailed(f"production runs Odoo {major}, test Odoo {test_major}: "
                            "a database is not loaded into another version")
        if mode != "copy" and major < NO_FILESTORE_MAJOR:
            raise JobFailed(f"Odoo {major} cannot back up a database without its filestore "
                            f"(from Odoo {NO_FILESTORE_MAJOR}): copy the filestore")
        folder = self._host.backup_folder(prod)
        folder.mkdir(parents=True, exist_ok=True)
        backup_zip = await self.backup(job, prod, folder, {"no_filestore": mode != "copy"})
        await stop(job, test)
        opts = test.options
        job.log(f"--- load {backup_zip.name} into database {opts.database} of test (neutralized)")
        try:
            await _helper(job, test, "load", opts.odoo_service, opts.database, "--neutralize", stdin=backup_zip)
            if mode == "share":
                job.log(f"--- the filestore of test: hard links to production's ({prod.options.database})")
                await _helper(job, test, "link-filestore", opts.odoo_service, opts.database,
                              prod.restart.service, prod.options.database)
            await self._update_changed(job, test, force=True)
        except JobFailed:
            job.log(f"{test.key} is left stopped: its database may be missing or half loaded")
            raise
        await start(job, test)

    async def _pull_image(self, job: Job, instance: Instance, params: dict) -> None:
        job.log(f"--- pull the images of {instance.restart.service}")
        await _helper(job, instance, "pull")


def _copy_params(server: Server, params: dict) -> dict:
    mode = str(params.get("filestore") or "share")
    if mode not in dict(COPY_FILESTORE):
        raise ActionRefused(f"The filestore of test is one of {', '.join(dict(COPY_FILESTORE))}")
    for inst in (server.production, server.test):
        if inst is None or inst.restart.type != "compose":
            raise ActionRefused(f"{server.name}: production and test must both be compose projects")
    return {"filestore": mode}


def _update_params(instance: Instance, params: dict) -> dict:
    """The modules named (empty: the changed ones) and whether to restart."""
    raw = params.get("modules") or ""
    modules = [m.strip() for m in (raw if isinstance(raw, list) else str(raw).split(",")) if str(m).strip()]
    bad = [m for m in modules if not MODULE.fullmatch(m)]
    if bad:
        raise ActionRefused(f"Invalid modules: {', '.join(bad)}")
    return {"modules": modules, "restart": params.get("restart") in (True, "on", "true", "1", 1)}


def _update_title(instance: Instance, params: dict) -> str:
    what = ", ".join(params["modules"]) if params["modules"] else "changed modules"
    return f"Update {what} on {instance.key}" + (" + restart" if params["restart"] else "")
