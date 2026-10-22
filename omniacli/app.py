# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OmniaCLI web application."""

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
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from . import checks, disks, ops, plugins
from .accesslog import AccessLog
# Odoo's own routes below (modules, pip, its config file) call the Odoo plugin
# directly: the other routes reach any type through instance.plugin
from .odoo import ops as odoo_ops
from .odoo.odooconf import read_odoo_config
from .auth import ROLES as ACCESS_ROLES
from .auth import Authenticator, Principal, hash_password, hash_token, new_token, verify_password
from .config import (
    ROLES,
    SERVER_NAME,
    USER_NAME,
    Access,
    ConfigError,
    Instance,
    LinkAccess,
    LinkTarget,
    Settings,
    TOKEN_DEFAULT_ROLE,
    load_settings,
    matches,
    parse_groups,
    parse_link,
    parse_servers,
    save_access,
    save_groups,
    save_links,
    save_servers,
    server_text,
    server_to_dict,
)
from .jobs import JobManager, filter_jobs
from .plugin_api import ENTRY_POINT_GROUP, PLUGIN_API
from .links import LIST_TIMEOUT, LinkClients, describe_error, link_headers
from .logtail import read_since
from .metrics import Sampler
from .scheduler import AutoUpdater
from .sessions import COOKIE, LIFETIME, LoginLimiter, SessionStore


def _page_context(request: Request) -> dict:
    """In every template: prefix, the path the pages are served under (a
    linked instance's, empty otherwise), and what the caller may do."""
    user = getattr(request.state, "user", None)
    return {
        "prefix": getattr(request.state, "prefix", ""),
        "can_act": bool(user and user.can("developer")),
        # restart, start and stop: every role; an MCP token only reads
        "can_restart": bool(user and user.kind != "token"),
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
# served without login: the icons, and the background of the login page
_PUBLIC_FILES = {"/favicon.ico": ("favicon.ico", "image/x-icon"),
                 "/icon-180.png": ("icon-180.png", "image/png"),
                 "/login-background.jpg": ("login-background.jpg", "image/jpeg")}
# what a linked instance sends: the path it serves our pages under, and the
# name of its user acting through the link
_PREFIX_HEADER = "x-omniacli-prefix"
_ON_BEHALF_HEADER = "x-omniacli-user"
# the role of that user there: a link acts with the lower of it and its own
_ROLE_CAP_HEADER = "x-omniacli-role-cap"
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
        return importlib.metadata.version("omniacli")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts: the browser may send the session cookie
    with them (SameSite=Strict should stop it; this does whatever the browser)."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlsplit(source).netloc == request.headers.get("host")


def _next_path(value: str) -> str:
    """Where to go after the login: a path of this site only, never another site."""
    value = value or "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def create_app(settings: Settings, link_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """link_transport replaces the network to the linked instances (tests)."""
    def link_hashes() -> dict[str, str]:
        return {name: link.token for name, link in settings.links.items()}

    authenticator = Authenticator(settings.users, settings.tokens, link_hashes())
    jobs = JobManager(settings.jobs_dir)
    updater = AutoUpdater(settings, jobs)
    sampler = Sampler(settings)
    link_clients = LinkClients(link_transport)
    access_log = AccessLog(settings.logs_dir / "access.jsonl")
    # the browsers' logins: next to the jobs, like the access log
    sessions = SessionStore(settings.jobs_dir.parent / "sessions.json")
    limiter = LoginLimiter()

    def identify(request: Request) -> Principal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            token = token.strip()
            name = authenticator.token_name(token)
            if name is not None:
                # an MCP client's token: it may read what its role allows (and
                # download backups); its actions go through the MCP tools only
                if request.method not in ("GET", "HEAD"):
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "A token may only read")
                return Principal(name, settings.token_role(name), "token")
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
        # a browser: the session of the login form
        name = sessions.user(request.cookies.get(COOKIE), settings.users)
        if name is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required")
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")
        return Principal(name, settings.user_role(name))

    def current_user(request: Request) -> Principal:
        ip = request.client.host if request.client else ""
        try:
            user = identify(request)
        except HTTPException as exc:
            # a wrong token; a missing session is no failure (the login form counts those)
            if exc.status_code == 401 and "bearer" in (exc.headers or {}).get("WWW-Authenticate", "").lower():
                access_log.record("failed", "token", "", ip, f"{request.method} {request.url.path}"[:200])
            raise
        if user.kind != "user":
            # a link (with who acts there through it) or a token: a login is
            # its first request after a pause; a user's login is the form's
            access_log.seen(user.kind, user.name, ip, user.on_behalf)
        request.state.user = user
        return user

    def needs(role: str):
        """A dependency: the caller, refused unless its role reaches role."""
        def check(user: Principal = Depends(current_user)) -> Principal:
            if not user.can(role):
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {role} role is required")
            return user
        return check

    anyone, developer, admin = needs("user"), needs("developer"), needs("admin")

    if importlib.util.find_spec("mcp") is None:
        # the mcp SDK needs Python 3.10: no /mcp without it
        mcp_route, mcp_manager = None, None
    else:
        from .mcp_server import create_mcp

        # add_draft_server is defined further down, with the configuration's lock
        mcp_route, mcp_manager = create_mcp(settings, jobs, updater, sampler, authenticator, access_log,
                                            lambda name, data: add_draft_server(name, data))

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
        title="OmniaCLI",
        lifespan=lifespan,
        dependencies=[Depends(current_user)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings, app.state.jobs, app.state.updater = settings, jobs, updater
    app.state.sampler = sampler
    app.state.access_log = access_log
    app.state.sessions, app.state.authenticator = sessions, authenticator

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # no page of OmniaCLI inside another site's frame (clickjacking)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def to_login(request: Request, exc: StarletteHTTPException):
        """A browser without a session opening a page goes to the login form."""
        if (exc.status_code == 401 and request.method == "GET"
                and "text/html" in request.headers.get("accept", "")
                and not request.headers.get("authorization")):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target)}", status.HTTP_303_SEE_OTHER)
        return await http_exception_handler(request, exc)

    async def public_file(request: Request):
        name, media_type = _PUBLIC_FILES[request.url.path]
        return FileResponse(_STATIC / name, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=86400"})

    def login_page(request: Request, next_path: str, error: str = "", code: int = 200):
        return _TEMPLATES.TemplateResponse(request, "login.html", {"next": next_path, "error": error},
                                           status_code=code)

    async def login(request: Request):
        """The login form (GET) and its check (POST): a session cookie of
        LIFETIME, failed tries limited per address and user name."""
        if request.method == "GET":
            next_path = _next_path(request.query_params.get("next", "/"))
            if sessions.user(request.cookies.get(COOKIE), settings.users):
                return RedirectResponse(next_path, status.HTTP_303_SEE_OTHER)
            return login_page(request, next_path)
        if not _same_origin(request):
            return HTMLResponse("Cross-site login refused", status_code=403)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        name = (form.get("username") or [""])[0].strip()[:64]
        password = (form.get("password") or [""])[0]
        next_path = _next_path((form.get("next") or ["/"])[0])
        ip = request.client.host if request.client else ""
        wait = limiter.wait(ip, name)
        if wait:
            access_log.record("failed", "user", name, ip, f"too many failed logins: wait {wait} s")
            return login_page(request, next_path, f"Too many failed logins: try again in {wait} seconds.", 429)
        if not name or not await asyncio.to_thread(authenticator.check, name, password):
            limiter.failed(ip, name)
            access_log.record("failed", "user", name, ip, "login form")
            return login_page(request, next_path, "Wrong user name or password.", 401)
        limiter.succeeded(ip, name)
        access_log.record("login", "user", name, ip, "login form")
        response = RedirectResponse(next_path, status.HTTP_303_SEE_OTHER)
        set_session_cookie(request, response, sessions.create(name, settings.users[name]))
        return response

    def set_session_cookie(request: Request, response, token: str) -> None:
        response.set_cookie(COOKIE, token, max_age=LIFETIME, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")

    # Starlette routes: the app's login dependency does not apply to them
    for path in _PUBLIC_FILES:
        app.add_route(path, public_file, methods=["GET"])
    app.add_route("/login", login, methods=["GET", "POST"])

    @app.post("/logout")
    async def logout(request: Request, me: Principal = Depends(current_user)):
        sessions.drop(request.cookies.get(COOKIE))
        access_log.record("logout", me.kind, me.name, request.client.host if request.client else "")
        response = RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
        response.delete_cookie(COOKIE, path="/")
        return response
    if mcp_route is not None:
        app.router.routes.append(mcp_route)  # its own bearer token check

    def get_instance(server: str, role: str, drafts: bool = False) -> Instance:
        instance = settings.instance(server, role, drafts)
        if instance is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{role}")
        return instance

    def odoo_instance(server: str, role: str) -> Instance:
        """An instance of an Odoo server, for Odoo's own routes."""
        instance = get_instance(server, role)
        if instance.type != "odoo":
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{server} is not an Odoo server")
        return instance

    def started(request: Request, job, query: str = "") -> RedirectResponse:
        return RedirectResponse(f"{prefix_of(request)}/jobs/{job.id}{query}", status.HTTP_303_SEE_OTHER)

    async def started_or_back(request: Request, job) -> RedirectResponse:
        """Start, stop and restart from a button: back to the page of the
        button (its form's next) instead of the job's page; that page shows
        the job running and its end."""
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        back = (form.get("next") or [""])[0]
        if back:
            return RedirectResponse(_next_path(back), status.HTTP_303_SEE_OTHER)
        return started(request, job)

    def prefix_of(request: Request) -> str:
        return getattr(request.state, "prefix", "")

    def active_server(name: str):
        srv = settings.servers.get(name)
        if srv is None or not srv.active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No active server {name}")
        return srv

    async def run_state(inst: Instance) -> str:
        """running, stopped or unknown (an init.d service, a unit not found)."""
        if inst.restart.type == "process":
            return "running" if ops.managed_pid(inst) else "stopped"
        state = await ops.service_state(inst)
        if state in ("active", "activating", "reloading"):
            return "running"
        if state in ("inactive", "failed", "deactivating"):
            return "stopped"
        return "unknown"

    async def run_states(servers) -> dict[str, str]:
        """{instance key: run_state} of the servers' instances, asked together."""
        insts = [i for srv in servers for i in srv.instances.values()]
        return dict(zip([i.key for i in insts], await asyncio.gather(*(run_state(i) for i in insts))))

    async def remote_servers(me: Principal) -> list[dict]:
        """The servers of every link, as Dashboard rows: an error row for a
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
                    # a linked OmniaCLI older than the state: no buttons
                    "state": {r: i["state"] for r, i in instances.items() if i.get("state")},
                    "restart": {r: i.get("restart", "") for r, i in instances.items()},
                    "web": {r: i["port"] for r, i in instances.items() if i.get("port")},
                    "text": " ".join(text),
                })
            return rows

        found = await asyncio.gather(*(one(link) for link in settings.link_targets.values()))
        return [row for rows in found for row in rows]

    async def server_sections(request: Request, q: str, me: Principal, with_links: bool) -> dict:
        """The Dashboard's server tables: this instance's live servers, and
        with_links those of every link (asked to them), filtered by text (link,
        name, database, service, repositories and branches)."""
        live = settings.active_servers()
        states = await run_states(live)
        rows = [
            {
                "link": "", "name": srv.name, "label": srv.name, "base": prefix_of(request),
                "roles": [r for r in ("production", "test") if r in srv.instances],
                "busy": {r: jobs.is_busy(i.key) for r, i in srv.instances.items()},
                "state": {r: states[i.key] for r, i in srv.instances.items()},
                "restart": {r: i.restart.type for r, i in srv.instances.items()},
                "web": {r: port for r, i in srv.instances.items() if (port := i.plugin.web_port(i))},
                "text": server_text(srv),
            }
            for srv in live
        ]
        remote = await remote_servers(me) if with_links else []
        errors = [r for r in remote if "error" in r]
        rows += [r for r in remote if "error" not in r]
        # each row under the first group that has it, the rest under "Other"
        for row in rows:
            row["group"] = next((g.name for g in settings.groups if g.contains(row["link"], row["name"])), "")
            row["text"] += " " + row["group"]
        shown = [r for r in rows if matches(r["text"], q)]
        sections = [{"name": g.name, "rows": [r for r in shown if r["group"] == g.name]} for g in settings.groups]
        sections.append({"name": "Other servers" if settings.groups else "",
                         "rows": [r for r in shown if not r["group"]]})
        return {
            "rows": shown,
            "sections": [sec for sec in sections if sec["rows"]],
            "link_errors": [e for e in errors if matches(e["link"], q)],
            "total": len(rows),
            "q": q,
            "lazy": False,
        }

    @app.get("/")
    async def index(request: Request, q: str = "", me: Principal = Depends(current_user)):
        """The Dashboard. With links, their servers come after the page
        (/dashboard/servers): a link that answers slowly never holds it back."""
        lazy = me.kind == "user" and bool(settings.link_targets)
        context = await server_sections(request, q, me, with_links=False)
        context.update({
            "lazy": lazy,
            # the load and disks of this machine, and of every link (a link
            # that does not answer says so in its panel)
            "machine_links": list(settings.link_targets) if me.kind == "user" else [],
            "jobs": jobs.recent(10),
        })
        return _TEMPLATES.TemplateResponse(request, "index.html", context)

    @app.get("/dashboard/servers")
    async def dashboard_servers(request: Request, q: str = "", me: Principal = Depends(current_user)):
        """The Dashboard's server tables with the links' servers (lazy part)."""
        context = await server_sections(request, q, me, with_links=True)
        return _TEMPLATES.TemplateResponse(request, "_servers_part.html", context)

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
                # Odoo started by OmniaCLI: its pid, None when stopped
                "running": {i.key: ops.managed_pid(i) for i in shown if i.restart.type == "process"},
                # a systemd service: its ActiveState, None when unknown
                "service_state": dict(zip([i.key for i in shown],
                                          await asyncio.gather(*(ops.service_state(i) for i in shown)))),
                "jobs": [j for j in jobs.recent(200) if any(k.startswith(f"{server}/") for k in j.instances)][:10],
            },
        )

    @app.get("/api/info")
    async def info(user: Principal = Depends(current_user)):
        """What a linking instance checks: this version and the caller's role."""
        return {"app": "OmniaCLI", "version": _version(), "caller": str(user), "role": user.role}

    @app.get("/api/servers")
    async def server_list():
        """The live servers, with what the Dashboard shows and filters on."""
        live = settings.active_servers()
        states = await run_states(live)
        return [
            {
                "name": srv.name,
                "type": srv.type,
                "instances": {
                    role: {
                        # the plugin's fields (Odoo: database)
                        **inst.plugin.describe(inst),
                        "service": inst.restart.service,
                        "repositories": [{"name": r.name, "branch": r.branch} for r in inst.repositories],
                        "busy": jobs.is_busy(inst.key),
                        "state": states[inst.key],
                        "restart": inst.restart.type,
                        "port": inst.plugin.web_port(inst),
                    }
                    for role, inst in srv.instances.items()
                },
            }
            for srv in live
        ]

    @app.get("/api/metrics")
    async def metrics(since: int = 0, server: Optional[str] = None, machine_only: bool = False):
        """{cores, interval, machine: [points], instances: {key: {source, points}}};
        machine_only: no instances (the Dashboard's machine charts)."""
        data = {}
        for inst in [] if machine_only else settings.all_instances():
            if server is None or inst.server == server:
                data[inst.key] = {
                    "source": sampler.source.get(inst.key, ""),
                    "points": sampler.series(inst.key, since),
                }
        return {"cores": sampler.cores, "interval": 5, "machine": sampler.machine_series(since),
                "instances": data}

    @app.get("/api/disks")
    async def disk_space():
        """This machine's disks: size, free space and what is kept on each."""
        return {"disks": await disks.disks(settings)}

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
        instance = odoo_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"pip install requirements {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: odoo_ops.install_requirements(
                    job, instance.options.venv_bin, instance.options.odoo_bin,
                    [r.path for r in instance.repositories], instance.run_as),
            )
        )

    @app.post("/servers/{server}/{role}/pip-cli")
    async def action_pip_cli(request: Request, server: str, role: str,
                             user: Principal = Depends(developer)):
        instance = odoo_instance(server, role)
        return started(
            request,
            jobs.submit(
                f"pip install {odoo_ops.CLI_PACKAGE} {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: odoo_ops.install_cli(job, instance.options.venv_bin, instance.run_as),
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
        run_as = str(payload.get("run_as") or "").strip() or None
        if run_as is not None and not USER_NAME.fullmatch(run_as):
            return JSONResponse({"error": f"{run_as!r} is not a user name"}, status_code=400)
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        # the instance's key when the form names one: its jobs wait for each other
        key = f"{name if SERVER_NAME.fullmatch(name) else 'form'}/{role if role in ROLES else 'venv'}"
        if kind == "requirements":
            job = jobs.submit(f"pip install requirements {key}", [key], f"manual: {user}",
                              lambda job: odoo_ops.install_requirements(job, venv_bin, odoo_bin, folders, run_as))
        else:
            job = jobs.submit(f"pip install {odoo_ops.CLI_PACKAGE} {key}", [key], f"manual: {user}",
                              lambda job: odoo_ops.install_cli(job, venv_bin, run_as))
        return {"job": job.id}

    @app.get("/api/servers/{server}/{role}/addons")
    async def addons(server: str, role: str):
        instance = odoo_instance(server, role)
        odoo_bin = odoo_ops.find_odoo_bin(instance)
        return {
            "addons": await asyncio.to_thread(odoo_ops.list_addons, instance),
            "odoo_bin": str(odoo_bin) if odoo_bin else None,
        }

    @app.post("/api/servers/{server}/{role}/update")
    async def action_update(
        server: str, role: str, payload: dict = Body(...), user: Principal = Depends(developer)
    ):
        instance = odoo_instance(server, role)
        mode = payload.get("mode")
        modules = [str(m).strip() for m in payload.get("modules") or [] if str(m).strip()]
        restart_after = bool(payload.get("restart"))
        if mode == "chosen":
            bad = [m for m in modules if not odoo_ops.ADDON_NAME.fullmatch(m)]
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
                await odoo_ops.update_chosen_modules(job, instance, modules)
            else:
                await odoo_ops.update_modules(job, instance, force=True)
            if restart_after:
                await ops.restart(job, instance)

        if restart_after:
            title += " + restart"
        job = jobs.submit(title, [instance.key], f"manual: {user}", action)
        return {"job": job.id}

    @app.post("/servers/{server}/{role}/restart")
    async def action_restart(request: Request, server: str, role: str, user: Principal = Depends(anyone)):
        instance = get_instance(server, role)
        return await started_or_back(
            request,
            jobs.submit(
                f"Restart {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.restart(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/start")
    async def action_start(request: Request, server: str, role: str, user: Principal = Depends(anyone)):
        instance = get_instance(server, role)
        if instance.restart.type not in ("systemd", "process"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Start is for a systemd service or an {instance.plugin.label} OmniaCLI starts itself")
        return await started_or_back(
            request,
            jobs.submit(
                f"Start {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.start(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/stop")
    async def action_stop(request: Request, server: str, role: str, user: Principal = Depends(anyone)):
        instance = get_instance(server, role)
        if instance.restart.type not in ("systemd", "process"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Stop is for a systemd service or an {instance.plugin.label} OmniaCLI starts itself")
        return await started_or_back(
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
    async def backup_download(server: str, role: str, name: str, me: Principal = Depends(current_user)):
        # an admin also backs up draft servers (MCP), and downloads their backups
        instance = get_instance(server, role, drafts=me.can("admin"))
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
        # a draft or deleted server has no page: its crumb goes to its jobs
        live = [srv.name for srv in settings.active_servers()]
        return _TEMPLATES.TemplateResponse(request, "job.html", {"job": job, "live": live})

    @app.get("/api/jobs")
    async def jobs_json(limit: int = 20):
        """The last jobs, without their logs, and the instances busy now: the
        Dashboard follows them (runners, end notices)."""
        found = jobs.recent(min(max(limit, 1), 200))
        return {
            "jobs": [{"id": j.id, "title": j.title, "status": j.status, "instances": j.instances,
                      "servers": j.servers, "source": j.source, "created": j.created,
                      "finished": j.finished, "error": j.error} for j in found],
            "busy": sorted({key for j in jobs.jobs.values() if j.status == "running" for key in j.instances}),
        }

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

    def editable(name: str):
        """A server the form edits: the form is Odoo's, so an Odoo server
        (one whose plugin is missing is left as written)."""
        srv = configured(name)
        if srv.missing_plugin:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} cannot be edited: {srv.error}")
        if srv.type != "odoo":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} is of type {srv.type}: "
                                "the form edits Odoo servers only")
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
        srv = editable(name)
        return server_form(request, f"Edit {name}", name, name, server_to_dict(srv))

    @app.get("/config/{name}/clone", dependencies=[Depends(admin)])
    async def config_clone(request: Request, name: str):
        srv = editable(name)
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

    async def write_servers(servers):
        """Write the servers file and make it current (OSError when it cannot)."""
        save_servers(settings.servers_file, servers)
        settings.servers = servers
        await updater.reload()

    async def store(servers):
        """write_servers; a response on error."""
        try:
            await write_servers(servers)
        except OSError as exc:
            return refuse(f"Cannot write {settings.servers_file}: {exc}", 500)
        return None

    async def add_draft_server(name: str, data: dict):
        """A new server, always a draft (from MCP): it goes live only with
        Test & activate on the web. ConfigError when it cannot be added."""
        async with config_lock:
            if name in settings.servers:
                raise ConfigError(f"A server named {name} already exists")
            if not isinstance(data, dict):
                raise ConfigError("the server must be a mapping of production and test")
            try:
                parsed = parse_servers({name: {**data, "status": "draft"}}, settings.logs_dir)[name]
            except (TypeError, ValueError, AttributeError) as exc:
                raise ConfigError(str(exc)) from exc
            try:
                await write_servers({**settings.servers, name: parsed})
            except OSError as exc:
                raise ConfigError(f"Cannot write {settings.servers_file}: {exc}") from exc
            return parsed

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
        return Access(dict(web.users), dict(web.roles), dict(web.tokens), dict(web.links), dict(web.token_roles))

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
                "tokens": [{"name": n, "role": settings.token_role(n), "fixed": n in fixed.tokens}
                           for n in sorted(settings.tokens)],
                "users_file": settings.users_file,
                "roles": ACCESS_ROLES,
            },
        )

    @app.get("/help", dependencies=[Depends(developer)])
    async def help_page(request: Request):
        """How to create a link and how to connect an MCP client."""
        # seen through a link, the address the browser knows is the other instance's
        base = "https://<address of this OmniaCLI>" if prefix_of(request) else str(request.base_url).rstrip("/")
        return _TEMPLATES.TemplateResponse(request, "help.html", {"base_url": base})

    @app.get("/plugins", dependencies=[Depends(admin)])
    async def plugins_page(request: Request):
        """The plugins found, those disabled with the reason, and the servers
        whose plugin is missing."""
        found = plugins.registry().found
        count = {}
        for srv in settings.servers.values():
            count[srv.type] = count.get(srv.type, 0) + 1
        missing = [srv for srv in settings.servers.values() if srv.missing_plugin]
        return _TEMPLATES.TemplateResponse(
            request, "plugins.html",
            {"found": found, "count": count, "missing": missing, "api_version": PLUGIN_API,
             "group": ENTRY_POINT_GROUP},
        )

    @app.get("/access", dependencies=[Depends(admin)])
    async def access_page(request: Request, q: str = ""):
        """The access log: logins, failed logins and MCP calls, the newest first."""
        entries = await asyncio.to_thread(access_log.recent, 5000)
        found = [e for e in entries if matches(" ".join(str(v) for v in e.values()), q)]
        return _TEMPLATES.TemplateResponse(
            request, "access.html",
            {"entries": found[:500], "total": len(found), "q": q, "log_file": access_log.path},
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
    async def token_save(payload: dict = Body(...)):
        """Create an MCP token (new: true; it is in the answer, once) or change its role."""
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or TOKEN_DEFAULT_ROLE)
        new = bool(payload.get("new"))
        async with access_lock:
            refused = check_name("Token", name, settings.fixed.tokens, settings.tokens, new) or check_role(role)
            if refused:
                return refused
            web = web_copy()
            token = new_token() if new else None
            if token:
                web.tokens[name] = hash_token(token)
            web.token_roles[name] = role
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
            if kind == "tokens":
                web.token_roles.pop(name, None)
            return await store_access(web) or {"ok": True}

    # --- links to other OmniaCLI instances -------------------------------

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
        if not info or info.get("app") != "OmniaCLI":
            return {"ok": False, "error": f"no OmniaCLI with links there (HTTP {resp.status_code})"}
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
        # the body is passed on raw: ask only the encodings the browser accepts
        # (httpx would ask gzip by itself, and an nginx there would gzip)
        headers["accept-encoding"] = request.headers.get("accept-encoding", "identity")
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

    # --- groups of the Dashboard ----------------------------------------

    @app.get("/groups")
    async def groups_page(request: Request, me: Principal = Depends(admin)):
        known = [srv.name for srv in settings.active_servers()]
        known += [f"{r['link']}/{r['name']}" for r in await remote_servers(me) if "error" not in r]
        return _TEMPLATES.TemplateResponse(
            request, "groups.html",
            {"groups": [{"name": g.name, "members": g.members} for g in settings.groups],
             "known": known, "links": list(settings.link_targets), "groups_file": settings.groups_file},
        )

    @app.post("/api/groups/save", dependencies=[Depends(admin)])
    async def groups_save(payload: dict = Body(...)):
        """The whole list of groups, in order."""
        try:
            groups = parse_groups(payload.get("groups"))
        except ConfigError as exc:
            return refuse(str(exc))
        try:
            await asyncio.to_thread(save_groups, settings.groups_file, groups)
        except OSError as exc:
            return refuse(f"Cannot write {settings.groups_file}: {exc}", 500)
        settings.groups = groups
        return {"ok": True}

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
    async def account_password(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
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
            refused = await store_access(web)
            if refused:
                return refused
            # the new password ends every session of the user: a new one for this browser
            response = JSONResponse({"ok": True})
            set_session_cookie(request, response, sessions.create(me.name, settings.users[me.name]))
            return response

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniacli.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACLI_CONFIG", "omniacli.yaml")))
