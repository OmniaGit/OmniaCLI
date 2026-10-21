# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP interface: monitor the servers, update a test instance, and
describe an instance so that a client can rebuild its development setup.

Served at /mcp (streamable HTTP, stateless, JSON responses) for clients with
a bearer token from auth.tokens. Needs the mcp SDK, which needs Python 3.10:
without it OmniaCLI runs without /mcp.
"""

from __future__ import annotations

import asyncio
import configparser
import contextvars
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import ops
from .accesslog import AccessLog
from .auth import ROLES as ACCESS_ROLES
from .auth import Authenticator
from .config import ConfigError, Instance, Settings, server_to_dict
from .jobs import FINISHED, Job, JobFailed, JobManager, filter_jobs
from .logtail import last_lines
from .metrics import Sampler
from .scheduler import AutoUpdater

MAX_WAIT = 600  # seconds a tool may wait for its job
_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \d+ (\w+) ")
# Odoo config options that are secrets or belong to the server's own layout:
# never given out
_PRIVATE = re.compile(r"passw|secret|token|key|^(db_host|db_port|db_user|data_dir|logfile|pidfile"
                      r"|addons_path|upgrade_path|pre_upgrade_scripts)$")

INSTRUCTIONS = """OmniaCLI deploys Odoo instances from git: each server has a production and a
test instance. Read with list_servers, server_status, read_log, list_jobs,
get_job, pending_updates. The only action is update_test (git pull, update of
the changed modules, restart of the test instance). dev_setup describes an
instance (repositories, branches, commits, addons_path, Odoo version, config
options without secrets, backups) so that you can rebuild it locally:
clone every repository at its branch, write an odoo.conf with the given
options and an addons_path of the local clones; for the data, download a
backup zip (database dump and filestore) with an HTTP GET to the server's
base URL + its path, with the same Authorization: Bearer header, and restore
it (click-odoo-restoredb, or Odoo's database manager). backup_test makes a
fresh backup of a test instance. With an admin token, backup backs up any
instance (production too) and list_backups lists an instance's backups, each
with the path to download it; these two and dev_setup also reach the draft
servers (complete ones) with an admin token. read_server gives a
server's whole configuration and add_draft_server adds a server as a draft:
an administrator makes it live from the web interface (Test & activate)."""

# the role an MCP token needs for a tool; the others: any token
TOOL_ROLES = {"update_test": "developer", "backup_test": "developer",
              "backup": "admin", "list_backups": "admin",
              "read_server": "admin", "add_draft_server": "admin"}
# the role of the token of the request being served, for the list of tools
_ROLE = contextvars.ContextVar("omniacli_mcp_role", default="user")


def _public_url(url: str) -> str:
    """A remote URL without the credentials an https URL may carry."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and (parts.username or parts.password):
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def _errors_only(lines: list[str]) -> list[str]:
    """The ERROR and CRITICAL records, with their continuation lines
    (tracebacks), as the log page filters them."""
    level, kept = "INFO", []
    for line in lines:
        match = _RECORD.match(line)
        if match:
            level = match.group(1)
        if level in ("ERROR", "CRITICAL"):
            kept.append(line)
    return kept


def _job_dict(job: Job, tail: int = 0) -> dict:
    data = {
        "id": job.id, "title": job.title, "servers": job.servers, "instances": job.instances,
        "status": job.status, "source": job.source, "created": job.created,
        "started": job.started, "finished": job.finished, "error": job.error,
        "result": job.result,
    }
    if tail:
        data["log"] = job.lines[-tail:]
    return data


async def _wait(job: Job, seconds: int) -> None:
    for _ in range(min(max(seconds, 0), MAX_WAIT) * 2):
        if job.status in FINISHED:
            return
        await asyncio.sleep(0.5)


def _rpc_summary(body: bytes) -> str:
    """The JSON-RPC method of an MCP request, with the tool it calls."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return ""
    calls = data if isinstance(data, list) else [data]
    found = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("method"), str):
            continue
        method = call["method"]
        params = call.get("params") if isinstance(call.get("params"), dict) else {}
        if method == "tools/call" and isinstance(params.get("name"), str):
            method += " " + params["name"]
        found.append(method[:100])
    return ", ".join(found)


def create_mcp(settings: Settings, jobs: JobManager, updater: AutoUpdater, sampler: Sampler,
               authenticator: Authenticator, access_log: AccessLog, add_draft) -> tuple[Route, object]:
    """The /mcp route and the session manager to run in the app's lifespan.
    add_draft(name, data): the app's own way to add a draft server."""
    server = MCPServer(name="OmniaCLI", instructions=INSTRUCTIONS)

    def caller(ctx: Context) -> str:
        # the route checked this very header before the request got here
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        return authenticator.token_name(token.strip()) or "unknown"

    def allowed(tool: str, role: str) -> bool:
        return ACCESS_ROLES.index(role) >= ACCESS_ROLES.index(TOOL_ROLES.get(tool, "user"))

    def need(ctx: Context, tool: str) -> None:
        role = settings.token_role(caller(ctx))
        if not allowed(tool, role):
            raise ToolError(f"{tool} needs an MCP token with role {TOOL_ROLES[tool]}; this one is {role}")

    every_tool = server.list_tools

    async def list_tools():
        """Only the tools the caller's token may use."""
        role = _ROLE.get()
        return [t for t in await every_tool() if allowed(t.name, role)]
    server.list_tools = list_tools

    def instance(name: str, role: str, ctx: Context | None = None) -> Instance:
        """An instance of an active server; with ctx of an admin token, also
        of a draft (to rebuild it locally and to back it up)."""
        drafts = ctx is not None and settings.token_role(caller(ctx)) == "admin"
        found = settings.instance(name, role, drafts)
        if found is None:
            srv = settings.servers.get(name)
            if drafts and srv is not None and srv.error:
                raise ToolError(f"{name} is a draft saved incomplete: {srv.error}")
            if srv is not None and not srv.active and not drafts:
                raise ToolError(f"{name} is a draft: not live until Test & activate on the web; "
                                "with an admin token dev_setup, backup and list_backups reach it")
            raise ToolError(f"No server {name} with a {role} instance" if drafts
                            else f"No active server {name} with a {role} instance")
        return found

    @server.tool()
    def list_servers() -> list[dict]:
        """Every configured server: status (active or draft), its instances
        with database and how Odoo is restarted, and the drafts' problems."""
        return [
            {
                "name": srv.name, "status": srv.status, "incomplete": srv.error,
                "instances": {
                    role: {"database": i.database, "restart": i.restart.type,
                           "service": i.restart.service or None,
                           "repositories": [r.name for r in i.repositories]}
                    for role, i in srv.instances.items()
                },
            }
            for srv in settings.servers.values()
        ]

    @server.tool()
    async def server_status(server_name: str) -> dict:
        """State of an active server's instances: repositories (branch, last
        commit), whether Odoo runs, CPU and memory, auto update, running job,
        and its last jobs."""
        srv = settings.servers.get(server_name)
        if srv is None or not srv.active:
            raise ToolError(f"No active server {server_name}")
        result = {}
        for role, inst in srv.instances.items():
            repos = await asyncio.gather(*(ops.repository_status(r) for r in inst.repositories))
            series = sampler.series(inst.key)
            odoo = None
            if inst.restart.type == "process":
                pid = ops.managed_pid(inst)
                odoo = {"started_by": "OmniaCLI", "running": pid is not None, "pid": pid}
            result[role] = {
                "database": inst.database,
                "repositories": {r.name: s for r, s in zip(inst.repositories, repos)},
                "odoo": odoo or {"started_by": f"{inst.restart.type} {inst.restart.service}"},
                "load": series[-1] if series else None,
                "load_measured_on": sampler.source.get(inst.key),
                "auto_update": {"enabled": inst.auto_update.enabled,
                                "window": inst.auto_update.window_label(),
                                "last_check": updater.state.get(inst.key)},
                "job_running": jobs.is_busy(inst.key),
            }
        result["last_jobs"] = [_job_dict(j) for j in filter_jobs(jobs.recent(len(jobs.jobs)), server_name)[:5]]
        return result

    @server.tool()
    async def pending_updates(server_name: str, role: str = "test") -> dict:
        """Commits on the remotes not yet pulled, per repository (runs git fetch)."""
        try:
            return await ops.pending_updates(instance(server_name, role))
        except JobFailed as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def read_log(server_name: str, role: str = "test", lines: int = 200,
                       errors_only: bool = False) -> dict:
        """The last lines of an instance's Odoo log; errors_only keeps the
        ERROR and CRITICAL records with their tracebacks."""
        inst = instance(server_name, role)
        count = min(max(lines, 1), 5000)
        try:
            # read more when filtering, so that errors_only still finds some
            read, _, _ = await asyncio.to_thread(last_lines, inst.log_file, count * (10 if errors_only else 1))
        except OSError as exc:
            raise ToolError(f"Cannot read {inst.log_file}: {exc.strerror}") from exc
        if errors_only:
            read = _errors_only(read)
        return {"log_file": str(inst.log_file), "lines": read[-count:]}

    @server.tool()
    def list_jobs(server_name: str = "", query: str = "", limit: int = 20) -> list[dict]:
        """The last jobs, newest first, of a server and/or containing every
        word of query (title, started by, error, server/role)."""
        found = filter_jobs(jobs.recent(len(jobs.jobs)), server_name, query)
        return [_job_dict(j) for j in found[:min(max(limit, 1), 200)]]

    @server.tool()
    def get_job(job_id: str, tail: int = 200) -> dict:
        """A job with the last lines of its log."""
        job = jobs.get(job_id)
        if job is None:
            raise ToolError(f"No job {job_id}")
        return _job_dict(job, tail=min(max(tail, 1), 5000))

    @server.tool()
    async def update_test(server_name: str, ctx: Context, wait_seconds: int = 0) -> dict:
        """Update the test instance: git pull of every repository, update of
        the changed modules, restart. Waits up to wait_seconds (max 600) for
        the job; follow it with get_job otherwise."""
        need(ctx, "update_test")
        inst = instance(server_name, "test")
        job = jobs.submit(f"Git pull and restart {inst.key}", [inst.key], f"mcp: {caller(ctx)}",
                          lambda job: ops.pull_restart(job, inst))
        await _wait(job, wait_seconds)
        return _job_dict(job, tail=50)

    async def backup_job(inst: Instance, ctx: Context, wait_seconds: int) -> dict:
        async def action(job):
            dest = await ops.backup(job, inst, settings.backup_dir)
            job.result = {"file": dest.name, "size": dest.stat().st_size,
                          "download": f"/backups/{inst.server}/{inst.role}/{dest.name}"}
        job = jobs.submit(f"Backup {inst.key}", [inst.key], f"mcp: {caller(ctx)}", action)
        await _wait(job, wait_seconds)
        return _job_dict(job, tail=20)

    def backups_of(inst: Instance, limit: int) -> list[dict]:
        """The instance's backup zips, newest first, with their download path."""
        folder = settings.backup_dir / inst.server / inst.role
        found = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if folder.is_dir() else []
        return [{"file": p.name, "size": p.stat().st_size, "time": int(p.stat().st_mtime),
                 "download": f"/backups/{inst.server}/{inst.role}/{p.name}"} for p in found[:limit]]

    @server.tool()
    async def backup_test(server_name: str, ctx: Context, wait_seconds: int = 0) -> dict:
        """Back up the database and filestore of a test instance; the result
        gives the path to download the zip (GET with the same bearer token)."""
        need(ctx, "backup_test")
        return await backup_job(instance(server_name, "test"), ctx, wait_seconds)

    @server.tool()
    async def backup(server_name: str, role: str, ctx: Context, wait_seconds: int = 0) -> dict:
        """(admin token) Back up the database and filestore of an instance,
        production or test. Waits up to wait_seconds (max 600); the finished
        job's result gives the path to download the zip: an HTTP GET to the
        server's base URL + that path, with the same Authorization: Bearer
        header."""
        need(ctx, "backup")
        return await backup_job(instance(server_name, role, ctx), ctx, wait_seconds)

    @server.tool()
    def list_backups(server_name: str, role: str, ctx: Context, limit: int = 20) -> list[dict]:
        """(admin token) An instance's backups, newest first: file, size,
        time (Unix) and the path to download it (HTTP GET to the server's base
        URL + that path, with the same Authorization: Bearer header)."""
        need(ctx, "list_backups")
        return backups_of(instance(server_name, role, ctx), min(max(limit, 1), 200))

    @server.tool()
    def read_server(server_name: str, ctx: Context) -> dict:
        """(admin token) The whole configuration of a server, active or
        draft, as the web form saves it: status, and per instance (production,
        test) database, odoo_config, log_file, venv_bin, restart, repositories,
        auto_update, update_modules, odoo_bin, run_as, http_port. A draft saved
        incomplete has error, which says what is missing."""
        need(ctx, "read_server")
        srv = settings.servers.get(server_name)
        if srv is None:
            raise ToolError(f"No server {server_name}")
        data = {"name": srv.name, **server_to_dict(srv)}
        if srv.error:
            data["error"] = srv.error
        return data

    @server.tool()
    async def add_draft_server(server_name: str, ctx: Context, production: dict | None = None,
                               test: dict | None = None) -> dict:
        """(admin token) Add a new server as a draft: it is not live, has no
        actions and no auto update until an administrator runs Test & activate
        on the web interface. production and test take the fields read_server
        gives; at least one is needed. A draft may be saved incomplete: then
        the answer has error, which says what to fix on the web form."""
        need(ctx, "add_draft_server")
        data = {role: value for role, value in (("production", production), ("test", test)) if value}
        if not data:
            raise ToolError("Give production, test or both")
        try:
            srv = await add_draft(server_name, data)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        result = {"name": srv.name, **server_to_dict(srv)}
        if srv.error:
            result["error"] = srv.error
        return result

    @server.tool()
    async def dev_setup(server_name: str, ctx: Context, role: str = "test") -> dict:
        """What is needed to rebuild an instance locally: Odoo and Python
        versions, every repository (remote URL, branch, commit), the
        addons_path as repository folders, the Odoo config options without
        secrets nor server paths, and the backups (database + filestore).
        Of an active server; with an admin token, of a draft too."""
        inst = instance(server_name, role, ctx)
        repos = []
        for repo in inst.repositories:
            _, url = await ops.capture(["git", "remote", "get-url", repo.remote], cwd=repo.path)
            _, commit = await ops.capture(["git", "rev-parse", "HEAD"], cwd=repo.path)
            repos.append({
                "name": repo.name, "url": _public_url(url), "remote": repo.remote,
                "branch": repo.branch, "commit": commit,
                "requirements": sorted(p.name for p in repo.path.glob("requirements*.txt")),
                "has_odoo_bin": (repo.path / "odoo-bin").is_file(),
            })
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(inst.odoo_config, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            pass
        options = dict(parser.items("options")) if parser.has_section("options") else {}
        addons = []
        for raw in options.get("addons_path", "").replace("\n", ",").split(","):
            if not raw.strip():
                continue
            folder = Path(raw.strip()).resolve()
            owner = next((r for r in inst.repositories
                          if folder == r.path.resolve() or r.path.resolve() in folder.parents), None)
            addons.append({"repository": owner.name, "subpath": str(folder.relative_to(owner.path.resolve()))}
                          if owner else {"outside_repositories": str(folder)})
        odoo_bin = ops.find_odoo_bin(inst)
        _, python = await ops.capture([inst.venv_bin / "python", "-c",
                                       "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        return {
            "server": inst.server, "role": role, "database": inst.database,
            "odoo_version": ops.odoo_major(odoo_bin) if odoo_bin else None,
            "python": python or None,
            "repositories": repos,
            "addons_path": addons,
            "odoo_conf_options": {k: v for k, v in options.items() if not _PRIVATE.search(k)},
            "backups": backups_of(inst, 10),
        }

    server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        # the bearer token already stops what DNS rebinding protection is for,
        # and it would refuse every Host but localhost behind a reverse proxy
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = server.session_manager
    from mcp.server.streamable_http_manager import StreamableHTTPASGIApp

    endpoint = StreamableHTTPASGIApp(manager)

    class Guarded:
        """The MCP endpoint behind the bearer token check (an ASGI app: a
        plain function would be taken by Starlette for a request handler)."""

        async def __call__(self, scope, receive, send):
            headers = dict(scope.get("headers") or [])
            scheme, _, token = headers.get(b"authorization", b"").decode(errors="replace").partition(" ")
            name = authenticator.token_name(token.strip()) if scheme.lower() == "bearer" else None
            ip = (scope.get("client") or ("",))[0]
            if name is None:
                access_log.record("failed", "mcp", "", ip, f"{scope.get('method', '')} /mcp")
                response = JSONResponse({"error": "A bearer token from auth.tokens is required"},
                                        status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
            # read the body to log its method, then hand it on unchanged
            chunks, more = [], True
            while more:
                message = await receive()
                if message["type"] != "http.request":
                    break
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
            body = b"".join(chunks)
            access_log.record("mcp", "mcp", name, ip, _rpc_summary(body) or f"{scope.get('method', '')} /mcp")
            _ROLE.set(settings.token_role(name))
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await endpoint(scope, replay, send)

    return Route("/mcp", endpoint=Guarded(), methods=["GET", "POST", "DELETE"]), manager
