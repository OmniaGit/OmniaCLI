"""The OmniaCI web application."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from . import checks, ops
from .odooconf import read_odoo_config
from .auth import ROLES as ACCESS_ROLES
from .auth import Authenticator, Principal, hash_password, hash_token, new_token, verify_password
from .config import (
    ROLES,
    SERVER_NAME,
    Access,
    ConfigError,
    Instance,
    LinkAccess,
    LinkTarget,
    Settings,
    load_settings,
    matches,
    parse_link,
    parse_servers,
    save_access,
    save_links,
    save_servers,
    server_text,
    server_to_dict,
)
from .jobs import JobManager, filter_jobs
from .links import LIST_TIMEOUT, LinkClients, describe_error, link_headers
from .logtail import read_since
from .metrics import Sampler
from .scheduler import AutoUpdater


def _page_context(request: Request) -> dict:
    """In every template: prefix, the path the pages are served under (a
    linked instance's, empty otherwise), and what the caller may do."""
    user = getattr(request.state, "user", None)
    return {
        "prefix": getattr(request.state, "prefix", ""),
        "can_act": bool(user and user.can("developer")),
        "is_admin": bool(user and user.can("admin")),
        # a user who logged in with a password (not a token or a link)
        "account": user.name if user and user.kind == "user" else None,
    }


_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"),
                             context_processors=[_page_context])
# The OmniaSolutions "O" (Client2019 Setup/dist/images/omnia.ico, 355x280,
# centred on a transparent square); served without login, like any favicon.
_STATIC = Path(__file__).parent / "static"
_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")
_ICONS = {"/favicon.ico": ("favicon.ico", "image/x-icon"),
          "/icon-180.png": ("icon-180.png", "image/png")}
_basic = HTTPBasic(realm="OmniaCI", auto_error=False)
# what a linked instance sends: the path it serves our pages under, and the
# name of its user acting through the link
_PREFIX_HEADER = "x-omniaci-prefix"
_ON_BEHALF_HEADER = "x-omniaci-user"
# the role of that user there: a link acts with the lower of it and its own
_ROLE_CAP_HEADER = "x-omniaci-role-cap"
_PREFIX = re.compile(r"(/[A-Za-z0-9._~-]+)+")
_ON_BEHALF = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# users, links and MCP tokens created from the web interface
_ACCESS_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_MIN_PASSWORD = 8
# what the proxy of a link passes on, each way (never the caller's credentials,
# never the other instance's WWW-Authenticate: the browser would ask for them)
_LINK_REQUEST_HEADERS = ("content-type", "accept", "accept-language")
_LINK_RESPONSE_HEADERS = ("content-type", "content-disposition", "content-length",
                          "content-encoding", "location", "cache-control")


def _version() -> str:
    try:
        return importlib.metadata.version("omniaci")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts: the browser sends Basic credentials anyway."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlsplit(source).netloc == request.headers.get("host")


def create_app(settings: Settings, link_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """link_transport replaces the network to the linked instances (tests)."""
    def link_hashes() -> dict[str, str]:
        return {name: link.token for name, link in settings.links.items()}

    authenticator = Authenticator(settings.users, settings.tokens, link_hashes())
    jobs = JobManager(settings.jobs_dir)
    updater = AutoUpdater(settings, jobs)
    sampler = Sampler(settings)
    link_clients = LinkClients(link_transport)

    def identify(request: Request, credentials: Optional[HTTPBasicCredentials]) -> Principal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            token = token.strip()
            name = authenticator.token_name(token)
            if name is not None:
                # an MCP client's token: it may read (and download backups);
                # its actions go through the MCP tools only
                if request.method not in ("GET", "HEAD"):
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "A token may only read")
                return Principal(name, "admin", "token")
            name = authenticator.link_name(token)
            if name is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token",
                                    headers={"WWW-Authenticate": "Bearer"})
            # a linked instance: no browser sends its token by itself, so no
            # cross-site check; it serves our pages under its own prefix
            prefix = request.headers.get(_PREFIX_HEADER, "")
            request.state.prefix = prefix if _PREFIX.fullmatch(prefix) else ""
            on_behalf = request.headers.get(_ON_BEHALF_HEADER, "")
            role = settings.links[name].role
            cap = request.headers.get(_ROLE_CAP_HEADER, "")
            if cap in ACCESS_ROLES and ACCESS_ROLES.index(cap) < ACCESS_ROLES.index(role):
                role = cap
            return Principal(name, role, "link", on_behalf if _ON_BEHALF.fullmatch(on_behalf) else "")
        if credentials is None or not authenticator.check(credentials.username, credentials.password):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="OmniaCI"'},
            )
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")
        return Principal(credentials.username, settings.user_role(credentials.username))

    def current_user(
        request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(_basic)
    ) -> Principal:
        user = identify(request, credentials)
        request.state.user = user
        return user

    def needs(role: str):
        """A dependency: the caller, refused unless its role reaches role."""
        def check(user: Principal = Depends(current_user)) -> Principal:
            if not user.can(role):
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {role} role is required")
            return user
        return check

    developer, admin = needs("developer"), needs("admin")

    if importlib.util.find_spec("mcp") is None:
        # the mcp SDK needs Python 3.10: no /mcp without it
        mcp_route, mcp_manager = None, None
    else:
        from .mcp_server import create_mcp

        mcp_route, mcp_manager = create_mcp(settings, jobs, updater, sampler, authenticator)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        updater.start()
        sampler.start()
        # not nullcontext(): it is an async context manager only from Python 3.10
        if mcp_manager:
            async with mcp_manager.run():
                yield
        else:
            yield
        await sampler.stop()
        await updater.stop()
        await link_clients.close()

    app = FastAPI(
        title="OmniaCI",
        lifespan=lifespan,
        dependencies=[Depends(current_user)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings, app.state.jobs, app.state.updater = settings, jobs, updater
    app.state.sampler = sampler

    async def icon(request: Request):
        name, media_type = _ICONS[request.url.path]
        return FileResponse(_STATIC / name, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=86400"})

    # Starlette routes: the app's login dependency does not apply to them
    for path in _ICONS:
        app.add_route(path, icon, methods=["GET"])
    if mcp_route is not None:
        app.router.routes.append(mcp_route)  # its own bearer token check

    def get_instance(server: str, role: str) -> Instance:
        instance = settings.instance(server, role)
        if instance is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{role}")
        return instance

    def started(request: Request, job, query: str = "") -> RedirectResponse:
        return RedirectResponse(f"{prefix_of(request)}/jobs/{job.id}{query}", status.HTTP_303_SEE_OTHER)

    def prefix_of(request: Request) -> str:
        return getattr(request.state, "prefix", "")

    def active_server(name: str):
        srv = settings.servers.get(name)
        if srv is None or not srv.active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No active server {name}")
        return srv

    async def remote_servers(me: Principal) -> list[dict]:
        """The servers of every link, as Servers page rows: an error row for a
        link that does not answer. Only for a user who logs in here."""
        if me.kind != "user":
            return []

        async def one(link: LinkTarget) -> list[dict]:
            try:
                resp = await link_clients.client(link).get(
                    "/api/servers", headers=link_headers(link, me.name, me.role), timeout=LIST_TIMEOUT)
                if resp.status_code != 200:
                    return [{"link": link.name, "error": f"answered {resp.status_code}"}]
                found = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return [{"link": link.name, "error": describe_error(exc)}]
            rows = []
            for srv in found:
                instances = srv.get("instances") or {}
                text = [link.name, srv["name"]]
                for inst in instances.values():
                    text += [inst.get("database", ""), inst.get("service", "")]
                    text += [f"{r['name']} {r['branch']}" for r in inst.get("repositories", [])]
                rows.append({
                    "link": link.name, "name": srv["name"], "label": f"{link.name} › {srv['name']}",
                    "base": f"/links/{link.name}",
                    "roles": [r for r in ("production", "test") if r in instances],
                    "busy": {r: bool(i.get("busy")) for r, i in instances.items()},
                    "text": " ".join(text),
                })
            return rows

        found = await asyncio.gather(*(one(link) for link in settings.link_targets.values()))
        return [row for rows in found for row in rows]

    @app.get("/")
    async def index(request: Request, q: str = "", me: Principal = Depends(current_user)):
        """The live servers, this instance's and its links', filtered by text
        (link, name, database, service, repositories and branches)."""
        rows = [
            {
                "link": "", "name": srv.name, "label": srv.name, "base": prefix_of(request),
                "roles": [r for r in ("production", "test") if r in srv.instances],
                "busy": {r: jobs.is_busy(i.key) for r, i in srv.instances.items()},
                "text": server_text(srv),
            }
            for srv in settings.active_servers()
        ]
        remote = await remote_servers(me)
        errors = [r for r in remote if "error" in r]
        rows += [r for r in remote if "error" not in r]
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "rows": [r for r in rows if matches(r["text"], q)],
                "link_errors": [e for e in errors if matches(e["link"], q)],
                "total": len(rows),
                "q": q,
                "drafts": [s for s in settings.servers.values() if not s.active],
                "jobs": jobs.recent(10),
            },
        )

    @app.get("/servers/{server}")
    async def server_page(request: Request, server: str, view: str = "both"):
        srv = active_server(server)
        roles = [r for r in ("production", "test") if r in srv.instances]
        if view in srv.instances:
            roles = [view]
        elif view != "both":
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{view}")
        shown = [srv.instances[r] for r in roles]
        statuses = await asyncio.gather(
            *(asyncio.gather(*(ops.repository_status(r) for r in i.repositories)) for i in shown)
        )
        return _TEMPLATES.TemplateResponse(
            request,
            "server.html",
            {
                "server": srv,
                "view": view if view in srv.instances else "both",
                "instances": shown,
                "repo_status": {i.key: s for i, s in zip(shown, statuses)},
                "auto_state": updater.state,
                "busy": {i.key: jobs.is_busy(i.key) for i in srv.instances.values()},
                # Odoo started by OmniaCI: its pid, None when stopped
                "running": {i.key: ops.managed_pid(i) for i in shown if i.restart.type == "process"},
                "jobs": [j for j in jobs.recent(200) if any(k.startswith(f"{server}/") for k in j.instances)][:10],
            },
        )

    @app.get("/api/info")
    async def info(user: Principal = Depends(current_user)):
        """What a linking instance checks: this version and the caller's role."""
        return {"app": "OmniaCI", "version": _version(), "caller": str(user), "role": user.role}

    @app.get("/api/servers")
    async def server_list():
        """The live servers, with what the Servers page shows and filters on."""
        return [
            {
                "name": srv.name,
                "instances": {
                    role: {
                        "database": inst.database,
                        "service": inst.restart.service,
                        "repositories": [{"name": r.name, "branch": r.branch} for r in inst.repositories],
                        "busy": jobs.is_busy(inst.key),
                    }
                    for role, inst in srv.instances.items()
                },
            }
            for srv in settings.active_servers()
        ]

    @app.get("/api/metrics")
    async def metrics(since: int = 0, server: Optional[str] = None):
        """{cores, interval, instances: {key: {source, points}}}."""
        data = {}
        for inst in settings.all_instances():
            if server is None or inst.server == server:
                data[inst.key] = {
                    "source": sampler.source.get(inst.key, ""),
                    "points": sampler.series(inst.key, since),
                }
        return {"cores": sampler.cores, "interval": 5, "instances": data}

    @app.post("/servers/{server}/{role}/pull")
    async def action_pull(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"Git pull {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.pull(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/pull-restart")
    async def action_pull_restart(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"Git pull and restart {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.pull_restart(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/pip-requirements")
    async def action_pip_requirements(request: Request, server: str, role: str,
                                      user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"pip install requirements {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.install_requirements(
                    job, instance.venv_bin, instance.odoo_bin, [r.path for r in instance.repositories]),
            )
        )

    @app.post("/servers/{server}/{role}/pip-cli")
    async def action_pip_cli(request: Request, server: str, role: str,
                             user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"pip install {ops.CLI_PACKAGE} {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.install_cli(job, instance.venv_bin),
            )
        )

    @app.post("/api/venv/pip")
    async def form_pip(payload: dict = Body(...), user: Principal = Depends(admin)):
        """The pip buttons of the server form: on the venv, odoo-bin and
        repository folders typed there, saved or not (a draft too)."""
        kind = payload.get("kind")
        if kind not in ("requirements", "cli"):
            return JSONResponse({"error": "kind must be 'requirements' or 'cli'"}, status_code=400)
        venv_bin = Path(str(payload.get("venv_bin") or "").strip())
        if not venv_bin.is_absolute() or not os.access(venv_bin / "python", os.X_OK):
            return JSONResponse({"error": f"No python in {venv_bin}: set the venv's bin folder"},
                                status_code=400)
        odoo_text = str(payload.get("odoo_bin") or "").strip()
        odoo_bin = Path(odoo_text) if odoo_text else None
        folders = [Path(str(p).strip()) for p in payload.get("repositories") or [] if str(p).strip()]
        folders = [p for p in folders if p.is_absolute() and p.is_dir()]
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        # the instance's key when the form names one: its jobs wait for each other
        key = f"{name if SERVER_NAME.fullmatch(name) else 'form'}/{role if role in ROLES else 'venv'}"
        if kind == "requirements":
            job = jobs.submit(f"pip install requirements {key}", [key], f"manual: {user}",
                              lambda job: ops.install_requirements(job, venv_bin, odoo_bin, folders))
        else:
            job = jobs.submit(f"pip install {ops.CLI_PACKAGE} {key}", [key], f"manual: {user}",
                              lambda job: ops.install_cli(job, venv_bin))
        return {"job": job.id}

    @app.get("/api/servers/{server}/{role}/addons")
    async def addons(server: str, role: str):
        instance = get_instance(server, role)
        odoo_bin = ops.find_odoo_bin(instance)
        return {
            "addons": await asyncio.to_thread(ops.list_addons, instance),
            "odoo_bin": str(odoo_bin) if odoo_bin else None,
        }

    @app.post("/api/servers/{server}/{role}/update")
    async def action_update(
        server: str, role: str, payload: dict = Body(...), user: Principal = Depends(developer)
    ):
        instance = get_instance(server, role)
        mode = payload.get("mode")
        modules = [str(m).strip() for m in payload.get("modules") or [] if str(m).strip()]
        restart_after = bool(payload.get("restart"))
        if mode == "chosen":
            bad = [m for m in modules if not ops.ADDON_NAME.fullmatch(m)]
            if not modules or bad:
                return JSONResponse(
                    {"error": f"Invalid modules: {', '.join(bad)}" if bad else "Choose at least one module"},
                    status_code=400,
                )
            title = f"Update {', '.join(modules)} on {instance.key}"
        elif mode == "changed":
            title = f"Update changed modules on {instance.key}"
        else:
            return JSONResponse({"error": "mode must be 'changed' or 'chosen'"}, status_code=400)

        async def action(job):
            if mode == "chosen":
                await ops.update_chosen_modules(job, instance, modules)
            else:
                await ops.update_modules(job, instance, force=True)
            if restart_after:
                await ops.restart(job, instance)

        if restart_after:
            title += " + restart"
        job = jobs.submit(title, [instance.key], f"manual: {user}", action)
        return {"job": job.id}

    @app.post("/servers/{server}/{role}/restart")
    async def action_restart(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"Restart {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.restart(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/stop")
    async def action_stop(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        if instance.restart.type != "process":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only an Odoo started by OmniaCI can be stopped")
        return started(
            request,
            jobs.submit(
                f"Stop {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.stop(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/backup")
    async def action_backup(
        request: Request, server: str, role: str, download: bool = False,
        user: Principal = Depends(developer),
    ):
        instance = get_instance(server, role)

        async def action(job):
            dest = await ops.backup(job, instance, settings.backup_dir)
            job.result = {
                "file": dest.name,
                "size": dest.stat().st_size,
                "download": f"/backups/{instance.server}/{instance.role}/{dest.name}",
            }

        job = jobs.submit(
            f"Backup {instance.key}" + (" + download" if download else ""),
            [instance.key],
            f"manual: {user}",
            action,
        )
        # the job page starts the download when the backup is ready
        return started(request, job, "?download=1" if download else "")

    def backup_folder(instance: Instance) -> Path:
        return settings.backup_dir / instance.server / instance.role

    @app.get("/api/servers/{server}/{role}/backups")
    async def backup_list(request: Request, server: str, role: str):
        instance = get_instance(server, role)
        folder = backup_folder(instance)
        files = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if folder.is_dir() else []
        return [
            {
                "file": p.name,
                "size": p.stat().st_size,
                "time": int(p.stat().st_mtime),
                "download": f"{prefix_of(request)}/backups/{server}/{role}/{p.name}",
            }
            for p in files
        ]

    @app.get("/backups/{server}/{role}/{name}", dependencies=[Depends(developer)])
    async def backup_download(server: str, role: str, name: str):
        instance = get_instance(server, role)
        folder = backup_folder(instance).resolve()
        path = (folder / name).resolve()
        # only a zip directly in this instance's backup folder
        if not _BACKUP_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such backup")
        return FileResponse(path, media_type="application/zip", filename=name)

    @app.post("/servers/{server}/merge")
    async def action_merge(request: Request, server: str, user: Principal = Depends(admin)):
        srv = settings.servers.get(server)
        if srv is None or not srv.active or srv.production is None or srv.test is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No server {server} with production and test"
            )
        return started(
            request,
            jobs.submit(
                f"Merge test to production {server}",
                [srv.production.key, srv.test.key],
                f"manual: {user}",
                lambda job: ops.merge_to_production(job, srv, settings.backup_dir),
            )
        )

    @app.get("/servers/{server}/{role}/log")
    async def log_page(request: Request, server: str, role: str):
        return _TEMPLATES.TemplateResponse(
            request, "log.html", {"instance": get_instance(server, role)}
        )

    @app.get("/api/servers/{server}/{role}/log")
    async def log_read(
        server: str, role: str, pos: Optional[int] = None, inode: Optional[int] = None
    ):
        """The log lines after pos (the last 300 when pos is missing)."""
        instance = get_instance(server, role)
        try:
            return await asyncio.to_thread(read_since, instance.log_file, pos, inode)
        except OSError as exc:
            return JSONResponse({"error": f"Cannot read {instance.log_file}: {exc.strerror}"},
                                status_code=404)

    @app.get("/jobs")
    async def job_list(request: Request, server: str = "", q: str = ""):
        """Every job kept, filtered by server and by text (title, started
        by, error, server)."""
        found = jobs.recent(len(jobs.jobs))
        names = sorted(set(settings.servers) | {n for j in found for n in j.servers})
        found = filter_jobs(found, server, q)
        return _TEMPLATES.TemplateResponse(
            request, "jobs.html",
            {"jobs": found[:200], "total": len(found), "servers": names, "server": server, "q": q},
        )

    @app.get("/jobs/{job_id}")
    async def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        return _TEMPLATES.TemplateResponse(request, "job.html", {"job": job})

    @app.get("/api/jobs/{job_id}")
    async def job_json(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        data = asdict(job)
        data["lines"] = job.lines[since:]
        data["next"] = len(job.lines)
        return JSONResponse(data)

    # --- server configuration -------------------------------------------

    config_lock = asyncio.Lock()

    def server_busy(name: str) -> bool:
        return any(jobs.is_busy(f"{name}/{role}") for role in ("production", "test"))

    def server_form(request: Request, title: str, original, name: str, data: dict):
        return _TEMPLATES.TemplateResponse(
            request,
            "server_form.html",
            {
                "title": title,
                "model": {
                    "original": original,
                    "name": name,
                    "server": data,
                    "active": bool(original and settings.servers[original].active),
                    "logs_dir": str(settings.logs_dir),
                },
            },
        )

    def configured(name: str):
        srv = settings.servers.get(name)
        if srv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No server {name}")
        return srv

    @app.get("/config", dependencies=[Depends(admin)])
    async def config_page(request: Request):
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "servers": settings.servers.values(),
                "servers_file": settings.servers_file,
                "busy": {name: server_busy(name) for name in settings.servers},
            },
        )

    @app.get("/config/new", dependencies=[Depends(admin)])
    async def config_new(request: Request):
        return server_form(request, "New server", None, "", {})

    @app.get("/config/{name}/edit", dependencies=[Depends(admin)])
    async def config_edit(request: Request, name: str):
        srv = configured(name)
        return server_form(request, f"Edit {name}", name, name, server_to_dict(srv))

    @app.get("/config/{name}/clone", dependencies=[Depends(admin)])
    async def config_clone(request: Request, name: str):
        srv = configured(name)
        new_name = f"{name}_copy"
        while new_name in settings.servers:
            new_name += "_copy"
        return server_form(request, f"Clone of {name}", None, new_name, server_to_dict(srv))

    def refuse(message: str, code: int = status.HTTP_400_BAD_REQUEST):
        return JSONResponse({"error": message}, status_code=code)

    @app.post("/api/odoo-config", dependencies=[Depends(admin)])
    async def odoo_config_read(payload: dict = Body(...)):
        """The database, log file and repositories an Odoo config gives."""
        path = str(payload.get("path") or "").strip()
        if not path:
            return refuse("Set the Odoo config file first")
        try:
            return await read_odoo_config(path)
        except ConfigError as exc:
            return refuse(str(exc))

    @app.post("/api/servers/save", dependencies=[Depends(admin)])
    async def config_save(payload: dict = Body(...)):
        original = payload.get("original") or None
        name = str(payload.get("name") or "").strip()
        async with config_lock:
            if original is not None and original not in settings.servers:
                return refuse(f"Server {original} no longer exists", status.HTTP_404_NOT_FOUND)
            if name != original and name in settings.servers:
                return refuse(f"A server named {name} already exists")
            for busy_name in {original, name} - {None}:
                if server_busy(busy_name):
                    return refuse(f"A job is running on {busy_name}: try again when it ends")
            server_data = dict(payload.get("server") or {})
            # the status is never taken from the form: a save makes a draft,
            # only passing checks make it active
            server_data["status"] = "draft"
            try:
                parsed = parse_servers({name: server_data}, settings.logs_dir)[name]
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            was_active = original is not None and settings.servers[original].active
            report = None
            if payload.get("activate"):
                report = await checks.check_server(parsed, settings.backup_dir)
                if checks.passed(report):
                    parsed.status = "active"
                elif was_active:
                    # the live server stays as it was
                    return JSONResponse(
                        {"error": "The checks failed: nothing was saved, the server is "
                                  "still live with its previous settings",
                         "checks": [c.to_dict() for c in report]},
                        status_code=400,
                    )
            servers = {}
            for key, srv in settings.servers.items():
                # an edited server keeps its place in the list
                servers[name if key == original else key] = parsed if key == original else srv
            servers.setdefault(name, parsed)
            error = await store(servers)
            if error:
                return error
        return {
            "ok": True,
            "status": parsed.status,
            "checks": [c.to_dict() for c in report] if report is not None else None,
        }

    async def store(servers):
        """Write the servers file and make it current; a response on error."""
        try:
            save_servers(settings.servers_file, servers)
        except OSError as exc:
            return refuse(f"Cannot write {settings.servers_file}: {exc}", 500)
        settings.servers = servers
        await updater.reload()
        return None

    @app.post("/api/servers/{name}/test", dependencies=[Depends(admin)])
    async def config_test(name: str):
        """Run the checks; a draft that passes them becomes active."""
        async with config_lock:
            srv = settings.servers.get(name)
            if srv is None:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            report = await checks.check_server(srv, settings.backup_dir)
            ok = checks.passed(report)
            if ok and not srv.active:
                srv.status = "active"
                error = await store(dict(settings.servers))
                if error:
                    srv.status = "draft"
                    return error
        return {"ok": ok, "status": srv.status, "checks": [c.to_dict() for c in report]}

    @app.post("/api/servers/{name}/deactivate", dependencies=[Depends(admin)])
    async def config_deactivate(name: str):
        async with config_lock:
            srv = settings.servers.get(name)
            if srv is None:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            if server_busy(name):
                return refuse(f"A job is running on {name}: try again when it ends")
            srv.status = "draft"
            error = await store(dict(settings.servers))
            if error:
                srv.status = "active"
                return error
        return {"ok": True, "status": "draft"}

    @app.post("/api/servers/delete", dependencies=[Depends(admin)])
    async def config_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with config_lock:
            if name not in settings.servers:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            if server_busy(name):
                return refuse(f"A job is running on {name}: try again when it ends")
            servers = {k: v for k, v in settings.servers.items() if k != name}
            error = await store(servers)
            if error:
                return error
        return {"ok": True}

    # --- users, links and MCP tokens ------------------------------------

    access_lock = asyncio.Lock()

    def web_copy() -> Access:
        web = settings.web
        return Access(dict(web.users), dict(web.roles), dict(web.tokens), dict(web.links))

    async def store_access(web: Access):
        """Write the users file and make it current; a response on error."""
        try:
            await asyncio.to_thread(save_access, settings.users_file, web)
        except OSError as exc:
            return refuse(f"Cannot write {settings.users_file}: {exc}", 500)
        settings.set_web_access(web)
        authenticator.update(settings.users, settings.tokens, link_hashes())
        return None

    def other_admins(name: str) -> int:
        return sum(1 for n in settings.users if n != name and settings.user_role(n) == "admin")

    def check_name(kind: str, name: str, fixed: dict, current: dict, new: bool):
        """None when name may be created (new) or edited, a response otherwise."""
        if not _ACCESS_NAME.fullmatch(name):
            return refuse("A name is 1-64 letters, digits or . _ @ -")
        if name in fixed:
            return refuse(f"{kind} {name} is set in the main configuration: change it there")
        if new and name in current:
            return refuse(f"A {kind.lower()} named {name} already exists")
        if not new and name not in current:
            return refuse(f"No {kind.lower()} {name}", status.HTTP_404_NOT_FOUND)
        return None

    def check_role(role: str):
        return None if role in ACCESS_ROLES else refuse(f"The role must be one of {', '.join(ACCESS_ROLES)}")

    @app.get("/users", dependencies=[Depends(admin)])
    async def users_page(request: Request):
        fixed = settings.fixed
        return _TEMPLATES.TemplateResponse(
            request,
            "users.html",
            {
                "users": [{"name": n, "role": settings.user_role(n), "fixed": n in fixed.users}
                          for n in sorted(settings.users)],
                "links": [{"name": n, "role": link.role, "fixed": n in fixed.links}
                          for n, link in sorted(settings.links.items())],
                "tokens": [{"name": n, "fixed": n in fixed.tokens} for n in sorted(settings.tokens)],
                "users_file": settings.users_file,
                "roles": ACCESS_ROLES,
            },
        )

    @app.post("/api/access/users/save")
    async def user_save(payload: dict = Body(...), me: Principal = Depends(admin)):
        """Create a user (new: true, with a password) or change its role and,
        when given, its password."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        password, new = str(payload.get("password") or ""), bool(payload.get("new"))
        async with access_lock:
            web = web_copy()
            refused = check_name("User", name, settings.fixed.users, settings.users, new) or check_role(role)
            if refused:
                return refused
            if (new or password) and len(password) < _MIN_PASSWORD:
                return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            if not new:
                if me.kind == "user" and name == me.name and role != me.role:
                    return refuse("You cannot change your own role")
                if role != "admin" and settings.user_role(name) == "admin" and not other_admins(name):
                    return refuse(f"{name} is the last admin")
            if password:
                web.users[name] = await asyncio.to_thread(hash_password, password)
            web.roles[name] = role
            return await store_access(web) or {"ok": True}

    @app.post("/api/access/users/delete")
    async def user_delete(payload: dict = Body(...), me: Principal = Depends(admin)):
        name = str(payload.get("name") or "")
        async with access_lock:
            refused = check_name("User", name, settings.fixed.users, settings.users, False)
            if refused:
                return refused
            if me.kind == "user" and name == me.name:
                return refuse("You cannot delete yourself")
            if settings.user_role(name) == "admin" and not other_admins(name):
                return refuse(f"{name} is the last admin")
            web = web_copy()
            del web.users[name]
            web.roles.pop(name, None)
            return await store_access(web) or {"ok": True}

    @app.post("/api/access/links/save", dependencies=[Depends(admin)])
    async def link_save(payload: dict = Body(...)):
        """Create a link (new: true; its token is in the answer, once) or
        change its role."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        new = bool(payload.get("new"))
        async with access_lock:
            refused = check_name("Link", name, settings.fixed.links, settings.links, new) or check_role(role)
            if refused:
                return refused
            web = web_copy()
            token = new_token() if new else None
            web.links[name] = LinkAccess(hash_token(token) if token else web.links[name].token, role)
            return await store_access(web) or {"ok": True, "token": token}

    @app.post("/api/access/tokens/save", dependencies=[Depends(admin)])
    async def token_create(payload: dict = Body(...)):
        """Create an MCP token: it is in the answer, once."""
        name = str(payload.get("name") or "").strip()
        async with access_lock:
            refused = check_name("Token", name, settings.fixed.tokens, settings.tokens, True)
            if refused:
                return refused
            web = web_copy()
            token = new_token()
            web.tokens[name] = hash_token(token)
            return await store_access(web) or {"ok": True, "token": token}

    @app.post("/api/access/{kind}/delete", dependencies=[Depends(admin)])
    async def access_delete(kind: str, payload: dict = Body(...)):
        """Revoke a link or an MCP token."""
        if kind not in ("links", "tokens"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {kind}")
        name = str(payload.get("name") or "")
        label = "Link" if kind == "links" else "Token"
        async with access_lock:
            fixed, current = getattr(settings.fixed, kind), getattr(settings, kind)
            refused = check_name(label, name, fixed, current, False)
            if refused:
                return refused
            web = web_copy()
            del getattr(web, kind)[name]
            return await store_access(web) or {"ok": True}

    # --- links to other OmniaCI instances -------------------------------

    links_lock = asyncio.Lock()

    def a_link(name: str) -> LinkTarget:
        link = settings.link_targets.get(name)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        return link

    async def store_links(links: dict[str, LinkTarget]):
        try:
            await asyncio.to_thread(save_links, settings.links_file, links)
        except OSError as exc:
            return refuse(f"Cannot write {settings.links_file}: {exc}", 500)
        settings.link_targets = links
        return None

    @app.get("/links", dependencies=[Depends(admin)])
    async def links_page(request: Request):
        return _TEMPLATES.TemplateResponse(
            request, "links.html",
            {"links": list(settings.link_targets.values()), "links_file": settings.links_file},
        )

    @app.post("/api/links/save", dependencies=[Depends(admin)])
    async def link_target_save(payload: dict = Body(...)):
        """Create a link (new: true) or change it; a blank token keeps the one saved."""
        name, new = str(payload.get("name") or "").strip(), bool(payload.get("new"))
        async with links_lock:
            if new and name in settings.link_targets:
                return refuse(f"A link named {name} already exists")
            if not new and name not in settings.link_targets:
                return refuse(f"No link {name}", status.HTTP_404_NOT_FOUND)
            data = {k: payload.get(k) for k in ("url", "token", "ca_file")}
            if not new and not str(data["token"] or "").strip():
                data["token"] = settings.link_targets[name].token
            try:
                link = parse_link(name, data)
            except ConfigError as exc:
                return refuse(str(exc))
            return await store_links({**settings.link_targets, name: link}) or {"ok": True}

    @app.post("/api/links/delete", dependencies=[Depends(admin)])
    async def link_target_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with links_lock:
            a_link(name)
            links = {k: v for k, v in settings.link_targets.items() if k != name}
            return await store_links(links) or {"ok": True}

    @app.post("/api/links/{name}/test")
    async def link_target_test(name: str, me: Principal = Depends(admin)):
        """Ask the other instance who we are there: its version and our role."""
        link = a_link(name)
        try:
            resp = await link_clients.client(link).get(
                "/api/info", headers=link_headers(link, me.name, me.role), timeout=LIST_TIMEOUT)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": describe_error(exc)}
        if resp.status_code == 401:
            return {"ok": False, "error": "the other instance refused the token"}
        try:
            info = resp.json() if resp.status_code == 200 else None
        except ValueError:
            info = None
        if not info or info.get("app") != "OmniaCI":
            return {"ok": False, "error": f"no OmniaCI with links there (HTTP {resp.status_code})"}
        return {"ok": True, **info}

    def link_failure(request: Request, link: LinkTarget, message: str):
        """A link that cannot answer: JSON for the pages' scripts, a page otherwise."""
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"error": f"link {link.name}: {message}"}, status_code=502)
        return _TEMPLATES.TemplateResponse(
            request, "link_error.html", {"link": link, "message": message}, status_code=502)

    @app.get("/links/{name}")
    async def link_root(name: str):
        a_link(name)
        return RedirectResponse(f"/links/{name}/", status.HTTP_307_TEMPORARY_REDIRECT)

    @app.api_route("/links/{name}/{path:path}", methods=["GET", "HEAD", "POST"])
    async def link_proxy(request: Request, name: str, path: str, me: Principal = Depends(current_user)):
        """The other instance's page or API, as the link, with at most our role."""
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may use a link")
        link = a_link(name)
        client = link_clients.client(link)
        headers = {k: request.headers[k] for k in _LINK_REQUEST_HEADERS if k in request.headers}
        headers.update(link_headers(link, me.name, me.role))
        url = "/" + path + (f"?{request.url.query}" if request.url.query else "")
        outgoing = client.build_request(request.method, url, headers=headers,
                                        content=await request.body() or None)
        try:
            resp = await client.send(outgoing, stream=True)
        except httpx.HTTPError as exc:
            return link_failure(request, link, describe_error(exc))
        if resp.status_code == 401:
            await resp.aclose()
            return link_failure(request, link, "the other instance refused the token: ask for a new one")
        kept = {k: resp.headers[k] for k in _LINK_RESPONSE_HEADERS if k in resp.headers}
        return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=kept,
                                 background=BackgroundTask(resp.aclose))

    # --- the caller's own password --------------------------------------

    def password_user(me: Principal) -> Principal:
        if me.kind != "user":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Only a user who logs in has an account")
        return me

    @app.get("/account")
    async def account_page(request: Request, me: Principal = Depends(current_user)):
        password_user(me)
        return _TEMPLATES.TemplateResponse(
            request, "account.html",
            {"name": me.name, "role": me.role, "fixed": me.name in settings.fixed.users},
        )

    @app.post("/api/account/password")
    async def account_password(payload: dict = Body(...), me: Principal = Depends(current_user)):
        password_user(me)
        current, password = str(payload.get("current") or ""), str(payload.get("password") or "")
        async with access_lock:
            if me.name in settings.fixed.users:
                return refuse("Your user is set in the main configuration: change its password there")
            if not await asyncio.to_thread(verify_password, current, settings.users.get(me.name, "")):
                return refuse("The current password is wrong")
            if len(password) < _MIN_PASSWORD:
                return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            web = web_copy()
            web.users[me.name] = await asyncio.to_thread(hash_password, password)
            return await store_access(web) or {"ok": True}

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniaci.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACI_CONFIG", "omniaci.yaml")))
