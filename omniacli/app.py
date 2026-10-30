# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OmniaCLI web application."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import importlib.metadata
import importlib.util
import json
import logging
import os
import pwd
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import jinja2
from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketException, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection

from . import (checks, consumer, disks, gitcredentials, linkcopy, ops, packages, plugins, pluginstore, puller, publisher,
               schedules, selflog, shell, stacktemplates, templatestore, tunnel, workspacehelper, workspaceops)
from .accesslog import AccessLog
from .diskhistory import DiskHistory, DiskRecorder
from .auth import ROLES as ACCESS_ROLES
from .auth import WORKSPACE_ROLE, Authenticator, Principal, hash_password, hash_token, new_token, verify_password
from .config import (
    PUBLISHED_COMMANDS,
    PULLER_FILE_MIB,
    PULLER_KEEP_HOURS,
    ROLES,
    SERVER_NAME,
    SHELL_FONT_SIZE,
    SHELL_FONT_SIZES,
    TUNNEL_IDLE_HOURS,
    USER_ROLES,
    WORKSPACE_LIMITS,
    Access,
    CERTIFICATES,
    ConfigError,
    Instance,
    LinkAccess,
    LinkTarget,
    Settings,
    TEST_LABEL,
    TOKEN_DEFAULT_ROLE,
    WORKSPACE_NAME,
    Workspace,
    load_settings,
    matches,
    parse_groups,
    parse_link,
    parse_connection,
    parse_options,
    parse_publication,
    parse_puller,
    parse_servers,
    parse_workspaces,
    save_access,
    save_connections,
    save_groups,
    save_links,
    save_options,
    save_publications,
    web_names,
    save_pullers,
    save_servers,
    save_workspaces,
    server_text,
    server_to_dict,
)
from .jobs import JobFailed, JobManager, filter_jobs
from .plugin_api import ENTRY_POINT_GROUP, PLUGIN_API, ActionRefused, Host
from .pullcrypto import new_key
from .links import LIST_TIMEOUT, LinkClients, describe_error, link_headers, proxy_websocket
from .logtail import read_since
from .metrics import Sampler
from .scheduler import AutoUpdater
from .sessions import COOKIE, LIFETIME, LoginLimiter, SessionStore


def _page_context(request: Request) -> dict:
    """In every template: prefix, the path the pages are served under (a
    linked instance's, empty otherwise), and what the caller may do."""
    user = getattr(request.state, "user", None)
    settings = getattr(request.app.state, "settings", None)
    return {
        "prefix": getattr(request.state, "prefix", ""),
        # the text size the consoles (logs, terminals) open with: the Options
        # page's, within its limits (_console.html)
        "console_font": settings.options.shell_font_size if settings else SHELL_FONT_SIZE,
        "console_font_sizes": SHELL_FONT_SIZES,
        "can_act": bool(user and user.can("developer")),
        # restart, start and stop: every role; an MCP token only reads
        "can_restart": bool(user and user.kind != "token"),
        "is_admin": bool(user and user.can("admin")),
        # a user of workspaces: none of the machine's menus and consoles
        "scoped": bool(user and user.scoped),
        # the workspaces whose users it manages (admin there)
        "my_workspaces": [ws for ws, role in user.workspaces if role == "admin"] if user and user.scoped else [],
        # a user who logged in with a password (not a token or a link)
        "account": user.name if user and user.kind == "user" else None,
        # in the footer of every page after the login
        "version": _version(),
        # the instances operated through a relay (consumer.py), in the menu
        "puller_connections": sorted(settings.connections) if settings else [],
    }


class _TemplateLoader(jinja2.BaseLoader):
    """The core's templates, and "<plugin>/<name>" from the templates_dir of
    a loaded plugin (looked up at every load: the plugins may be replaced)."""

    def __init__(self, core: Path):
        self.core = jinja2.FileSystemLoader(str(core))

    def get_source(self, environment, template):
        name, _, rest = template.partition("/")
        plugin = plugins.registry().get(name) if rest else None
        folder = plugin.templates_dir() if plugin is not None else None
        if folder is not None:
            return jinja2.FileSystemLoader(str(folder)).get_source(environment, rest)
        return self.core.get_source(environment, template)


_TEMPLATES = Jinja2Templates(
    env=jinja2.Environment(loader=_TemplateLoader(Path(__file__).parent / "templates"),
                           autoescape=jinja2.select_autoescape()),
    context_processors=[_page_context])
# The OmniaSolutions "O" (Client2019 Setup/dist/images/omnia.ico, 355x280,
# centred on a transparent square); served without login, like any favicon.
_STATIC = Path(__file__).parent / "static"
_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")
# the terminal of the shell page: xterm.js 6.0.0 and its fit addon 0.11.0
# (npm @xterm/xterm and @xterm/addon-fit, MIT), served after the login
_STATIC_FILES = {"xterm.js": "text/javascript", "xterm.css": "text/css", "addon-fit.js": "text/javascript"}
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
# the only routes a user of workspaces reaches (in_workspaces): its account,
# the lists (they show its workspaces' servers only), and what a server of
# its workspaces offers, with its role in that workspace. Not the consoles
# (a shell of this machine), the copy from a link, the tunnels, the
# machine's pages and the configuration.
WORKSPACE_ROUTES = frozenset({
    "/logout", "/account", "/api/account/password", "/static/{name}", "/x/{plugin}/static/{path:path}",
    "/", "/dashboard/servers", "/api/servers", "/api/metrics",
    "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/stop", "/api/jobs", "/api/jobs/{job_id}",
    "/servers/{server}", "/servers/{server}/merge", "/servers/{server}/x/{plugin}/{action}",
    "/servers/{server}/{role}/pull", "/servers/{server}/{role}/pull-restart",
    "/servers/{server}/{role}/restart", "/servers/{server}/{role}/start", "/servers/{server}/{role}/stop",
    "/servers/{server}/{role}/backup", "/api/servers/{server}/{role}/backups",
    "/backups/{server}/{role}/{name}", "/servers/{server}/{role}/x/{plugin}/{action}",
    "/servers/{server}/{role}/log", "/api/servers/{server}/{role}/log",
    "/workspaces/{workspace}", "/api/workspaces/{workspace}/members/save",
    "/api/workspaces/{workspace}/members/delete", "/api/workspaces/{workspace}/instances/create",
    "/api/workspaces/{workspace}/instances/delete", "/api/workspaces/{workspace}/web/save",
    "/api/workspaces/{workspace}/web/publish", "/api/workspaces/{workspace}/web/certificate",
    "/api/workspaces/{workspace}/credentials/save", "/api/workspaces/{workspace}/credentials/delete",
    "/api/workspaces/{workspace}/repositories/add", "/api/workspaces/{workspace}/repositories/remove",
})


_logger = logging.getLogger(__name__)


def _field_value(field, raw):
    """A posted parameter as its Field's kind wants it (a form posts text,
    a dialog's script JSON); ActionRefused when it cannot be one."""
    if field.kind == "checkbox":
        return raw not in (None, False, "", "0", "false", "off")
    if raw is None:
        # not posted (a script that leaves it out): its default
        raw = field.default
    text = "" if raw is None else str(raw).strip()
    if field.kind == "number":
        if text == "":
            if field.required:
                raise ActionRefused(f"{field.label}: a number is required")
            return None
        try:
            return int(text)
        except ValueError:
            raise ActionRefused(f"{field.label}: {text!r} is not a whole number") from None
    if field.required and not text:
        raise ActionRefused(f"{field.label} is required")
    if field.kind == "select" and text and text not in [str(value) for value, _ in field.choices]:
        raise ActionRefused(f"{field.label}: {text!r} is not one of the choices")
    return text


def _field_dict(field) -> dict:
    """A Field for the server form's script."""
    return {"key": field.key, "label": field.label, "kind": field.kind, "default": field.default,
            "required": field.required, "placeholder": field.placeholder, "help": field.help,
            "choices": [list(c) for c in field.choices], "restart_types": list(field.restart_types),
            "disabled_placeholder": field.disabled_placeholder,
            "buttons": [{"label": b.label, "route": b.route, "title": b.title, "confirm": b.confirm,
                         "only_with_value": b.only_with_value} for b in field.buttons]}


@functools.lru_cache(maxsize=1)
def _version() -> str:
    """The installed package's version (read once: it changes only with a
    new install, which restarts OmniaCLI)."""
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
    gitcredentials.bind(settings)
    updater = AutoUpdater(settings, jobs)
    sampler = Sampler(settings)
    # the disks once a day, next to the jobs like the sessions
    disk_history = DiskHistory(settings.jobs_dir.parent / "disk_history.json")
    disk_recorder = DiskRecorder(settings, disk_history)
    link_clients = LinkClients(link_transport)
    # the pullers of relays this one is a consumer of: their state next to the jobs
    consumers = consumer.Consumers(settings, link_clients, settings.jobs_dir.parent / "connections")
    # the scheduled actions; a schedules file that cannot be read is shown on
    # their page, and OmniaCLI starts without them
    scheduler = schedules.Scheduler(settings, jobs, link_clients,
                                    schedules.ScheduleHistory(settings.jobs_dir.parent / "schedule_history.json"),
                                    consumers)
    schedules_error = ""
    try:
        scheduler.load()
    except ConfigError as exc:
        schedules_error = str(exc)
    access_log = AccessLog(settings.logs_dir / "access.jsonl")
    # OmniaCLI's own log (selflog), shown to the admins
    own_log_file = settings.logs_dir / "omniacli.log"
    # the browsers' logins: next to the jobs, like the access log
    sessions = SessionStore(settings.jobs_dir.parent / "sessions.json")
    tunnels = tunnel.TunnelManager(
        lambda: settings.options, link_headers,
        lambda user, detail: access_log.record("tunnel", "user", user, "", detail))
    limiter = LoginLimiter()
    # the pullers this instance relays: their state next to the jobs
    pullers = puller.PullerHub(settings, settings.jobs_dir.parent / "pullers")

    async def published_instance(inst: Instance) -> dict:
        """What the report of a publication says of its instance (publisher.py)."""
        return {"state": await run_state(inst), "busy": jobs.is_busy(inst.key), "restart": inst.restart.type,
                "backup_fields": [_field_dict(f) for f in inst.plugin.backup_fields()]}

    def published_backup_options(inst: Instance, params: dict) -> dict:
        return {f.key: _field_value(f, params.get(f.key)) for f in inst.plugin.backup_fields()}

    # the instances of this one published on a puller of a relay
    publications = publisher.Publisher(settings, jobs, sampler, disk_history, link_clients,
                                       settings.jobs_dir.parent / "publications", published_instance,
                                       published_backup_options, _version())

    def identify(request: HTTPConnection) -> Principal:
        # a WebSocket has no method: it is checked as a post (no token, same origin)
        method = request.scope.get("method", "WEBSOCKET")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            token = token.strip()
            name = authenticator.token_name(token)
            if name is not None:
                # an MCP client's token: it may read what its role allows (and
                # download backups); its actions go through the MCP tools only
                if method not in ("GET", "HEAD"):
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
        if method not in ("GET", "HEAD") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")
        role = settings.user_role(name)
        if role == WORKSPACE_ROLE:
            # its role is decided for each request (in_workspaces)
            return Principal(name, "user", workspaces=settings.memberships(name), scoped=True)
        return Principal(name, role)

    def in_workspaces(request: HTTPConnection, user: Principal) -> Principal:
        """A user of workspaces only reaches the routes of WORKSPACE_ROUTES,
        and of them only what is in its workspaces, with its role there; any
        other route is refused (a route added later is closed until it is
        listed there)."""
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        params = request.scope.get("path_params") or {}
        allowed = path in WORKSPACE_ROUTES or (
            # a plugin's own routes, of an instance only
            path is not None and path.startswith("/x/") and "server" in params and "role" in params)
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not open to the users of workspaces")
        if "server" in params:
            names = [str(params["server"])]
        elif "job_id" in params:
            job = jobs.get(str(params["job_id"]))
            # a job of none of its servers is refused like one that does not exist
            names = job.servers if job is not None and job.servers else [""]
        elif "workspace" in params:
            role = user.workspace_role(str(params["workspace"]))
            if role is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a workspace of yours")
            return dataclasses.replace(user, role=role)
        else:
            # a list (Dashboard, jobs, account): it shows only its workspaces' servers
            return user
        roles = []
        for name in names:
            workspace = settings.workspace_of(name)
            role = user.workspace_role(workspace) if workspace else None
            if role is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a server of your workspaces")
            roles.append(role)
        # a job of several servers: the lowest of its roles there
        return dataclasses.replace(user, role=min(roles, key=ACCESS_ROLES.index))

    def visible_servers(user: Principal | None) -> set[str] | None:
        """The servers user may see: None, all of them (a user of the machine)."""
        if user is None or not user.scoped:
            return None
        mine = {ws for ws, _ in user.workspaces}
        return {srv for ws in settings.workspaces.values() if ws.name in mine for srv in ws.servers}

    def visible_jobs(user: Principal | None, found: list) -> list:
        """Of found, the jobs user may see: for a user of workspaces, those
        whose every server is in its workspaces."""
        shown = visible_servers(user)
        if shown is None:
            return found
        return [j for j in found if j.servers and set(j.servers) <= shown]

    def current_user(request: HTTPConnection) -> Principal:
        ip = request.client.host if request.client else ""
        try:
            user = identify(request)
        except HTTPException as exc:
            # a wrong token; a missing session is no failure (the login form counts those)
            if exc.status_code == 401 and "bearer" in (exc.headers or {}).get("WWW-Authenticate", "").lower():
                method = request.scope.get("method", "WEBSOCKET")
                access_log.record("failed", "token", "", ip, f"{method} {request.url.path}"[:200])
            if isinstance(request, WebSocket):
                # refused before the handshake: the browser sees 403
                raise WebSocketException(1008, exc.detail) from None
            raise
        if user.kind != "user":
            # a link (with who acts there through it) or a token: a login is
            # its first request after a pause; a user's login is the form's
            access_log.seen(user.kind, user.name, ip, user.on_behalf)
        if user.scoped:
            try:
                user = in_workspaces(request, user)
            except HTTPException as exc:
                if isinstance(request, WebSocket):
                    raise WebSocketException(1008, exc.detail) from None
                raise
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
        own_log = selflog.attach(own_log_file)
        updater.start()
        scheduler.start()
        sampler.start()
        disk_recorder.start()
        tunnels.start()
        pullers.start()
        publications.start()
        consumers.start()
        # not nullcontext(): it is an async context manager only from Python 3.10
        if mcp_manager:
            async with mcp_manager.run():
                yield
        else:
            yield
        await consumers.stop()
        await publications.stop()
        await pullers.stop()
        await tunnels.stop()
        await disk_recorder.stop()
        await sampler.stop()
        await updater.stop()
        await scheduler.stop()
        await link_clients.close()
        selflog.detach(own_log)

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
    app.state.disk_history, app.state.disk_recorder = disk_history, disk_recorder
    app.state.access_log = access_log
    app.state.tunnels = tunnels
    app.state.pullers, app.state.publications, app.state.consumers = pullers, publications, consumers
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

    def prefix_of(request: HTTPConnection) -> str:
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
                    # its domain; an older OmniaCLI, or none set: localhost at its port
                    "web": {r: i.get("url") or f"http://localhost:{i['port']}"
                            for r, i in instances.items() if i.get("url") or i.get("port")},
                    "text": " ".join(text),
                })
            return rows

        found = await asyncio.gather(*(one(link) for link in settings.link_targets.values()))
        return [row for rows in found for row in rows]

    async def server_sections(request: Request, q: str, me: Principal, with_links: bool) -> dict:
        """The Dashboard's server tables: this instance's live servers, and
        with_links those of every link (asked to them), filtered by text (link,
        name, database, service, repositories and branches). A user of
        workspaces sees its workspaces' servers only, and no link's."""
        shown = visible_servers(me)
        live = [srv for srv in settings.active_servers() if shown is None or srv.name in shown]
        with_links = with_links and shown is None
        states = await run_states(live)
        rows = [
            {
                "link": "", "name": srv.name, "label": srv.name, "base": prefix_of(request),
                "roles": [r for r in ("production", "test") if r in srv.instances],
                "busy": {r: jobs.is_busy(i.key) for r, i in srv.instances.items()},
                "state": {r: states[i.key] for r, i in srv.instances.items()},
                "restart": {r: i.restart.type for r, i in srv.instances.items()},
                "web": {r: url for r, i in srv.instances.items() if (url := i.web_url)},
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
        shown = visible_servers(me)
        lazy = me.kind == "user" and bool(settings.link_targets) and shown is None
        context = await server_sections(request, q, me, with_links=False)
        context.update({
            "lazy": lazy,
            # the load and disks of this machine, and of every link (a link
            # that does not answer says so in its panel): not for a user of
            # workspaces, who sees its own servers only
            "machine": shown is None,
            "machine_links": list(settings.link_targets) if me.kind == "user" and shown is None else [],
            "jobs": visible_jobs(me, jobs.recent(len(jobs.jobs)))[:10],
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
        me = request.state.user
        base = f"{prefix_of(request)}/servers/{srv.name}"
        buttons = {i.key: action_buttons(i, i.plugin, "instance", me, f"{base}/{i.role}") for i in shown}
        plugin = plugins.registry().get(srv.type)
        server_buttons = action_buttons(srv, plugin, "server", me, base)
        # each dialog of the plugin's own, drawn once
        dialogs = sorted({f"{b['plugin']}/{b['action'].dialog}"
                          for groups in [*buttons.values(), server_buttons]
                          for group in groups.values() for b in group if b["action"].dialog})
        statuses = await asyncio.gather(
            *(asyncio.gather(*(ops.repository_status(r) for r in i.repositories)) for i in shown)
        )
        # Copy from link: for a user who logs in here, with the role it needs;
        # shown also without a usable link, disabled, saying how to get one
        from_link = {}
        for i in shown:
            # not for a user of workspaces: the links are the machine's
            if not (i.plugin.has_restore and me.kind == "user" and not me.scoped and me.can(from_link_role(i))):
                continue
            src = i.from_link
            if src is None:
                missing = ("No database from a link: "
                           + ("set 'Database from link' in the server's form (Edit)" if settings.link_targets
                              else "add a link on the Links page, then set 'Database from link' "
                                   "in the server's form (Edit)"))
                from_link[i.key] = {"disabled": missing}
                continue
            known = src.link in (settings.connections if src.is_puller else settings.link_targets)
            from_link[i.key] = {
                "url": f"{base}/{i.role}/from-link", "link": src.link, "server": src.server,
                "role": src.role, "label": src.label, "puller": src.is_puller, "known": known,
                "database": str(i.plugin.describe(i).get("database") or i.key) if i.role == "production" else ""}
            if src.is_puller and not known:
                from_link[i.key]["disabled"] = (f"No puller connection {src.link} here: add it on the Pullers "
                                                "page, or change 'Database from link' in the server's form (Edit)")
            elif not known:
                from_link[i.key]["disabled"] = (f"No link {src.link} here: add it on the Links page, "
                                                "or change 'Database from link' in the server's form (Edit)")
        return _TEMPLATES.TemplateResponse(
            request,
            "server.html",
            {
                "from_link": from_link,
                "restore_fields": [_field_dict(f) for f in plugin.restore_fields()] if plugin else [],
                "backup_fields": [_field_dict(f) for f in plugin.backup_fields()] if plugin else [],
                "server": srv,
                "view": view if view in srv.instances else "both",
                "instances": shown,
                "buttons": buttons,
                "server_buttons": server_buttons,
                "dialogs": dialogs,
                "core_groups": core_groups,
                # the plugin's settings of each instance, as its fields name them
                "settings_rows": {i.key: settings_rows(i) for i in shown},
                "repo_status": {i.key: s for i, s in zip(shown, statuses)},
                "package_state": {i.key: packages.installed_state(i.package) for i in shown if i.package},
                "auto_state": updater.state,
                "busy": {i.key: jobs.is_busy(i.key) for i in srv.instances.values()},
                "busy_job": {i.key: jobs.running_on(i.key) for i in shown},
                # Odoo started by OmniaCLI: its pid, None when stopped
                "running": {i.key: ops.managed_pid(i) for i in shown if i.restart.type == "process"},
                # a systemd service: its ActiveState, None when unknown
                "service_state": dict(zip([i.key for i in shown],
                                          await asyncio.gather(*(ops.service_state(i) for i in shown)))),
                "jobs": [j for j in jobs.recent(200) if any(k.startswith(f"{server}/") for k in j.instances)][:10],
            },
        )

    def settings_rows(inst: Instance) -> list[tuple]:
        """(label, value, kind) of the plugin's fields that apply to the instance."""
        values = inst.plugin.options_to_dict(inst.options)
        return [(f.label, values.get(f.key), f.kind) for f in inst.plugin.fields()
                if not f.restart_types or inst.restart.type in f.restart_types]

    @app.get("/api/info")
    async def info(user: Principal = Depends(current_user)):
        """What a linking instance checks: this version and the caller's role."""
        return {"app": "OmniaCLI", "version": _version(), "caller": str(user), "role": user.role}

    @app.get("/api/servers")
    async def server_list(request: Request):
        """The live servers, with what the Dashboard shows and filters on."""
        shown = visible_servers(request.state.user)
        live = [srv for srv in settings.active_servers() if shown is None or srv.name in shown]
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
                        # the domain set for it, None: none
                        "url": inst.domain,
                    }
                    for role, inst in srv.instances.items()
                },
            }
            for srv in live
        ]

    @app.get("/api/metrics")
    async def metrics(request: Request, since: int = 0, server: Optional[str] = None, machine_only: bool = False):
        """{cores, interval, machine: [points], instances: {key: {source, points}}};
        machine_only: no instances (the Dashboard's machine charts). A user
        of workspaces gets its instances only, and not the machine's load."""
        shown = visible_servers(request.state.user)
        data = {}
        for inst in [] if machine_only else settings.all_instances():
            if (server is None or inst.server == server) and (shown is None or inst.server in shown):
                data[inst.key] = {
                    "source": sampler.source.get(inst.key, ""),
                    "points": sampler.series(inst.key, since),
                }
        return {"cores": sampler.cores, "interval": 5,
                "machine": sampler.machine_series(since) if shown is None else [], "instances": data}

    @app.get("/api/disks")
    async def disk_space():
        """This machine's disks: size, free space, what is kept on each, and
        when each will be full (diskhistory.forecast)."""
        found = await disks.disks(settings)
        for disk in found:
            disk["forecast"] = disk_history.forecast_of(disk["mount"])
        return {"disks": found}

    @app.get("/api/disks/history")
    async def disk_history_json():
        """Every disk's daily points (up to three years) and its forecast."""
        return {"disks": disk_history.series()}

    @app.get("/disks")
    async def disks_page(request: Request):
        """The history of this machine's disks, and when each will be full."""
        return _TEMPLATES.TemplateResponse(request, "disks.html", {})

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
        if instance.restart.type not in ("systemd", "compose", "process"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Start is for a systemd service, a compose project or an {instance.plugin.label} "
                                "OmniaCLI starts itself")
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
        if instance.restart.type not in ("systemd", "compose", "process"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Stop is for a systemd service, a compose project or an {instance.plugin.label} "
                                "OmniaCLI starts itself")
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
        raw, _ = await posted_params(request)
        try:
            options = {f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.backup_fields()}
        except ActionRefused as exc:
            return refuse(str(exc))
        chosen = [f.label for f in instance.plugin.backup_fields() if f.kind == "checkbox" and options[f.key]]

        async def action(job):
            dest = await ops.backup(job, instance, settings.backup_dir, options)
            job.result = {
                "file": dest.name,
                "size": dest.stat().st_size,
                "download": f"/backups/{instance.server}/{instance.role}/{dest.name}",
            }

        job = jobs.submit(
            f"Backup {instance.key}" + (" + download" if download else "")
            + (f" ({', '.join(chosen).lower()})" if chosen else ""),
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

    def from_link_role(instance: Instance) -> str:
        """Who may copy a linked instance's database into instance: a production
        is an admin's."""
        return "admin" if instance.role == "production" else "developer"

    @app.post("/servers/{server}/{role}/from-link")
    async def action_from_link(request: Request, server: str, role: str,
                               user: Principal = Depends(developer)):
        """Back up the instance's from_link there (or take one of its backups),
        download it into the instance's backups and restore it here."""
        instance = get_instance(server, role)
        src = instance.from_link
        if src is None or not instance.plugin.has_restore:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{instance.key} has no database from a link")
        if user.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may use a link")
        if not user.can(from_link_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"The {from_link_role(instance)} role is required for a production")
        link = None if src.is_puller else settings.link_targets.get(src.link)
        if src.is_puller and src.link not in settings.connections:
            return refuse(f"No puller connection {src.link}: add it on the Pullers page, or change the server")
        if not src.is_puller and link is None:
            return refuse(f"No link {src.link}: add it on the Links page, or change the server")
        raw, _ = await posted_params(request)
        backup = str(raw.get("backup") or "").strip()
        if backup and src.is_puller:
            return refuse("A puller brings a new backup only")
        if backup and not linkcopy.BACKUP_NAME.fullmatch(backup):
            return refuse(f"{backup!r} is not the name of a backup zip")
        if instance.role == "production":
            # the database it replaces, typed
            expected = str(instance.plugin.describe(instance).get("database") or instance.key)
            if str(raw.get("confirm") or "").strip() != expected:
                return refuse(f"Type {expected} to replace the production database")
        try:
            options = {f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.restore_fields()}
            # the choices of a new backup there, asked as this instance's
            # plugin asks them (an OmniaCLI there without them backs up as usual)
            backup_options = {} if backup else {
                f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.backup_fields()}
        except ActionRefused as exc:
            return refuse(str(exc))
        chosen = [f.label.lower() for f in instance.plugin.backup_fields()
                  if f.kind == "checkbox" and backup_options.get(f.key)]
        what = backup or "new backup" + (f" {', '.join(chosen)}" if chosen else "")
        if src.is_puller:
            connection = settings.connections[src.link]

            async def from_puller(job):
                progress = job.result["progress"] = {"steps": consumer.copy_steps(connection, instance.key),
                                                     "step": "backup", "done": 0, "total": 0}
                dest = await consumers.fetch_backup(job, connection.name, backup_folder(instance), progress,
                                                    backup_options, user.name)
                progress["step"] = "restore"
                await instance.plugin.restore(job, instance, dest, options)

            job = jobs.submit(f"Copy from {src.label} to {instance.key} ({what})",
                              [instance.key], f"manual: {user}", from_puller)
            return {"job": job.id}
        headers = link_headers(link, user.name, user.role)

        async def action(job):
            # the steps, drawn by the job page as they go
            progress = job.result["progress"] = {
                "steps": [{"id": "backup", "label": f"Backup {backup}" if backup else "Backup",
                           "where": link.name},
                          {"id": "download", "label": "Transfer", "where": f"{link.name} → here"},
                          {"id": "restore", "label": "Restore", "where": instance.key}],
                "step": "backup", "done": 0, "total": 0,
            }
            dest = await linkcopy.fetch_backup(job, link_clients.client(link), link, headers, src,
                                               backup, backup_folder(instance), progress, backup_options)
            progress["step"] = "restore"
            await instance.plugin.restore(job, instance, dest, options)

        job = jobs.submit(f"Copy from {src.label} to {instance.key} ({what})",
                          [instance.key], f"manual: {user}", action)
        return {"job": job.id}

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

    # --- the plugins: their buttons on the server page, their routes -------

    # the core's own groups of buttons, which a plugin's action may join
    core_groups = ("service", "git", "backup")

    def action_buttons(target, plugin, scope: str, me: Principal, base: str) -> dict:
        """The plugin's actions the caller may run on target, by group:
        {group: [button]}, the core's groups first."""
        groups = {g: [] for g in core_groups}
        if me.kind == "token":
            return groups
        info = plugin.describe(target) if scope == "instance" else {}
        for action in plugin.actions():
            if action.scope != scope or not me.can(action.role) or not action.applies_to(target):
                continue
            groups.setdefault(action.group or plugin.name, []).append({
                "action": action, "plugin": plugin.name,
                "url": f"{base}/x/{plugin.name}/{action.id}",
                "confirm": action.confirm_of(target),
                "key": getattr(target, "key", getattr(target, "name", "")),
                "info": info,
                "dom_id": re.sub(r"[^A-Za-z0-9_-]", "-", f"act-{target_name(target)}-{plugin.name}-{action.id}"),
            })
        return groups

    def target_name(target) -> str:
        return getattr(target, "key", None) or target.name

    async def posted_params(request: Request) -> tuple[dict, bool]:
        """The parameters posted to an action: JSON (a dialog's script) or a
        form (a button, the core's dialog); True when JSON. No body: no
        parameters, answered as a form (a redirect), as the routes before the
        plugins' actions answered (test_deb.sh posts so)."""
        if request.headers.get("content-type", "").split(";")[0].strip() == "application/json":
            if not await request.body():
                return {}, False
            try:
                data = await request.json()
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body is not JSON") from None
            if not isinstance(data, dict):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body must be a JSON object")
            return data, True
        form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[-1] for k, v in form.items()}, False

    async def run_action(request: Request, target, keys: list[str], plugin, name: str,
                         action_id: str, scope: str, user: Principal):
        if plugin is None or plugin.name != name:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{target_name(target)} is not of type {name}")
        action = next((a for a in plugin.actions() if a.id == action_id and a.scope == scope), None)
        if action is None or not action.applies_to(target):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{plugin.label} has no action {action_id} "
                                                           f"for {target_name(target)}")
        if not user.can(action.role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {action.role} role is required")
        raw, as_json = await posted_params(request)
        try:
            if action.params:
                params = {f.key: _field_value(f, raw.get(f.key)) for f in action.params}
            else:
                # a dialog of the plugin's own: what its script posted
                params = {k: v for k, v in raw.items() if k != "next"} if action.dialog else {}
            if action.prepare is not None:
                params = action.prepare(target, params)
        except ActionRefused as exc:
            return refuse(str(exc))
        job = jobs.submit(action.title_of(target, params), keys, f"manual: {user}",
                          lambda job: action.run(job, target, params))
        if as_json:
            return {"job": job.id}
        back = raw.get("next")
        if back:
            return RedirectResponse(_next_path(back), status.HTTP_303_SEE_OTHER)
        return started(request, job)

    @app.post("/servers/{server}/{role}/x/{plugin}/{action}")
    async def instance_action(request: Request, server: str, role: str, plugin: str, action: str,
                              user: Principal = Depends(current_user)):
        """An action of the plugin of the server, on one of its instances."""
        instance = get_instance(server, role)
        return await run_action(request, instance, [instance.key], instance.plugin, plugin, action,
                                "instance", user)

    @app.post("/servers/{server}/x/{plugin}/{action}")
    async def server_action(request: Request, server: str, plugin: str, action: str,
                            user: Principal = Depends(current_user)):
        """An action of the plugin of the server, on the whole server."""
        srv = active_server(server)
        return await run_action(request, srv, [i.key for i in srv.instances.values()],
                                plugins.registry().get(srv.type), plugin, action, "server", user)

    class AppHost(Host):
        """What a plugin's routes get from this application."""

        def __init__(self, plugin):
            self.plugin = plugin

        def needs(self, role: str):
            return needs(role)

        def instance(self, server: str, role: str) -> Instance:
            instance = get_instance(server, role)
            if instance.plugin is not self.plugin:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    f"{server} is not a server of type {self.plugin.name}")
            return instance

        def submit(self, request, title: str, keys: list[str], action):
            return jobs.submit(title, keys, f"manual: {request.state.user}", action)

        def backup_folder(self, instance: Instance) -> Path:
            return backup_folder(instance)

        def render(self, request, template: str, context: dict):
            return _TEMPLATES.TemplateResponse(request, f"{self.plugin.name}/{template}", context)

    @app.get("/x/{plugin}/static/{path:path}")
    async def plugin_static(plugin: str, path: str):
        """A file of a plugin's static_dir (before its router: it wins)."""
        found = plugins.registry().get(plugin)
        folder = found.static_dir() if found is not None else None
        if folder is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
        folder = Path(folder).resolve()
        target = (folder / path).resolve()
        if folder not in target.parents or not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
        return FileResponse(target)

    # a plugin whose router cannot be made keeps its servers and buttons,
    # without its routes; the Plugins page says why
    route_errors: dict[str, str] = {}
    app.state.plugin_route_errors = route_errors
    for plugin_name, plugin in plugins.registry().plugins.items():
        try:
            router = plugin.router(AppHost(plugin))
            if router is not None:
                app.include_router(router, prefix=f"/x/{plugin_name}")
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop OmniaCLI
            route_errors[plugin_name] = f"{type(exc).__name__}: {exc}"
            _logger.warning("Plugin %s: its routes are not served: %s", plugin_name, route_errors[plugin_name])

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
        if instance.log_file is None:
            # compose: docker compose logs, pos is the time of the last line
            try:
                return await ops.compose_log(instance, pos)
            except ops.LogUnavailable as exc:
                return JSONResponse({"error": f"Cannot read {instance.log_label}: {exc}"}, status_code=404)
        try:
            return await asyncio.to_thread(read_since, instance.log_file, pos, inode)
        except OSError as exc:
            return JSONResponse({"error": f"Cannot read {instance.log_file}: {exc.strerror}"},
                                status_code=404)

    @app.get("/omniacli/log", dependencies=[Depends(admin)])
    async def own_log_page(request: Request):
        return _TEMPLATES.TemplateResponse(request, "own_log.html", {"log_file": own_log_file})

    @app.get("/api/omniacli/log", dependencies=[Depends(admin)])
    async def own_log_read(pos: Optional[int] = None, inode: Optional[int] = None):
        """OmniaCLI's own log lines after pos (the last 300 when pos is missing)."""
        try:
            return await asyncio.to_thread(read_since, own_log_file, pos, inode)
        except FileNotFoundError:
            return {"lines": [], "pos": None, "inode": None, "rotated": False, "more": False}
        except OSError as exc:
            return JSONResponse({"error": f"Cannot read {own_log_file}: {exc.strerror}"}, status_code=404)

    @app.get("/jobs")
    async def job_list(request: Request, server: str = "", q: str = ""):
        """Every job kept, filtered by server and by text (title, started
        by, error, server)."""
        me = request.state.user
        found = visible_jobs(me, jobs.recent(len(jobs.jobs)))
        shown = visible_servers(me)
        names = sorted((set(settings.servers) if shown is None else shown) | {n for j in found for n in j.servers})
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

    @app.post("/jobs/{job_id}/stop")
    async def job_stop(request: Request, job_id: str, user: Principal = Depends(admin)):
        """Stop a queued or running job (one that hangs or loops): its program
        is ended, what it did before stays done."""
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        if not jobs.stop(job_id, str(user)):
            raise HTTPException(status.HTTP_409_CONFLICT, f"The job is not running: it is {job.status}")
        return RedirectResponse(f"{prefix_of(request)}/jobs/{job.id}", status.HTTP_303_SEE_OTHER)

    @app.get("/api/jobs")
    async def jobs_json(request: Request, limit: int = 20):
        """The last jobs, without their logs, and the instances busy now: the
        Dashboard follows them (runners, end notices)."""
        me = request.state.user
        found = visible_jobs(me, jobs.recent(len(jobs.jobs)))[:min(max(limit, 1), 200)]
        running = visible_jobs(me, [j for j in jobs.jobs.values() if j.status == "running"])
        return {
            "jobs": [{"id": j.id, "title": j.title, "status": j.status, "instances": j.instances,
                      "servers": j.servers, "source": j.source, "created": j.created,
                      "finished": j.finished, "duration": j.duration, "error": j.error} for j in found],
            "busy": sorted({key for j in running for key in j.instances}),
        }

    @app.get("/api/jobs/{job_id}")
    async def job_json(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        data = asdict(job)
        data["duration"] = job.duration
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
                    # what the form offers: the loaded plugins, with their fields
                    "types": {n: {"label": p.label, "has_process": p.has_process, "has_compose": p.has_compose,
                                  "has_restore": p.has_restore,
                                  "fields": [_field_dict(f) for f in p.fields()]}
                              for n, p in plugins.registry().plugins.items()},
                    "default_type": plugins.DEFAULT_TYPE,
                    # where a database may come from (Copy from link)
                    "links": sorted(settings.link_targets),
                    # or a puller connection (Pullers page)
                    "connections": sorted(settings.connections),
                },
            },
        )

    def configured(name: str):
        srv = settings.servers.get(name)
        if srv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No server {name}")
        return srv

    def editable(name: str):
        """A server the form edits: one whose plugin is there (one whose
        plugin is missing is left as written)."""
        srv = configured(name)
        if srv.missing_plugin:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} cannot be edited: {srv.error}")
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
            # a renamed server stays in its workspace; a new one is in none,
            # whatever a workspace still names
            error = await rename_in_workspaces(original, name)
            if error:
                return error
        return {
            "ok": True,
            "status": parsed.status,
            "checks": [c.to_dict() for c in report] if report is not None else None,
        }

    @app.post("/api/servers/check", dependencies=[Depends(admin)])
    async def config_check(payload: dict = Body(...)):
        """The checks on what the form holds: nothing is saved, the form stays open."""
        name = str(payload.get("name") or "").strip() or "new"
        server_data = dict(payload.get("server") or {})
        server_data["status"] = "draft"
        try:
            parsed = parse_servers({name: server_data}, settings.logs_dir)[name]
        except (ConfigError, TypeError, ValueError, AttributeError) as exc:
            return refuse(str(exc))
        options = gitcredentials.options_for(settings, name)
        for inst in (parsed.production, parsed.test):
            for repo in (inst.repositories if inst is not None else []):
                repo.git_options = options
        report = await checks.check_server(parsed, settings.backup_dir)
        return {"ok": checks.passed(report), "checks": [c.to_dict() for c in report]}

    @app.post("/api/servers/branches", dependencies=[Depends(admin)])
    async def config_branches(payload: dict = Body(...)):
        """The branches of a repository row of the form (its path and remote)."""
        path = str(payload.get("path") or "").strip()
        remote = str(payload.get("remote") or "").strip() or "origin"
        if not path or not os.path.isabs(path):
            return refuse("Type the repository's full path first")
        if remote.startswith("-"):
            return refuse(f"Invalid remote {remote}")
        server = str(payload.get("server") or "")
        options = gitcredentials.options_for(settings, server) if SERVER_NAME.fullmatch(server) else ()
        data = await ops.remote_branches(Path(path), remote, checks.GIT_TIMEOUT, options)
        if "error" in data:
            return refuse(data["error"])
        return data

    async def write_servers(servers):
        """Write the servers file and make it current (OSError when it cannot)."""
        save_servers(settings.servers_file, servers)
        settings.servers = servers
        gitcredentials.bind(settings)
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

    @app.get("/config/discover", dependencies=[Depends(admin)])
    async def config_discover(request: Request):
        return _TEMPLATES.TemplateResponse(request, "discover.html", {"roles": ROLES})

    @app.get("/api/servers/discover", dependencies=[Depends(admin)])
    async def discover_list():
        """What every plugin finds on this machine (Discover page)."""
        known = [i for s in settings.servers.values() for i in s.instances.values()]
        found = []
        for name, plugin in plugins.registry().plugins.items():
            try:
                items = await plugin.discover([i for i in known if i.type == name])
            except Exception as exc:  # noqa: BLE001 - one plugin does not hide the others
                _logger.exception("discover of %s", name)
                found.append({"type": name, "type_label": plugin.label, "error": str(exc)})
                continue
            found.extend({"type": name, "type_label": plugin.label, **item.to_dict()} for item in items)
        return {"found": found, "names": sorted(settings.servers)}

    @app.post("/api/servers/discover", dependencies=[Depends(admin)])
    async def discover_save(payload: dict = Body(...)):
        """Save what the Discover page chose as drafts: items of {name, role,
        type, instance}; the items of one name are the instances of one server."""
        servers: dict[str, dict] = {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("instance"), dict):
                return refuse("Every item needs a name, a role, a type and its instance")
            name, role = str(item.get("name") or "").strip(), str(item.get("role") or "")
            kind = str(item.get("type") or "")
            if not SERVER_NAME.fullmatch(name):
                return refuse(f"{name!r}: a server name is letters, digits, '.', '_' and '-'")
            if role not in ROLES:
                return refuse(f"{name}: the role is {' or '.join(ROLES)}")
            server = servers.setdefault(name, {"type": kind, "status": "draft"})
            if server["type"] != kind:
                return refuse(f"{name}: its instances are of different types")
            if role in server:
                return refuse(f"{name}: two instances of role {role}")
            server[role] = item["instance"]
        if not servers:
            return refuse("Choose at least one instance")
        async with config_lock:
            taken = [n for n in servers if n in settings.servers]
            if taken:
                return refuse(f"A server named {', '.join(taken)} already exists")
            try:
                parsed = parse_servers(servers, settings.logs_dir)
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            error = await store({**settings.servers, **parsed})
            if error:
                return error
        return {"ok": True, "created": [{"name": n, "error": s.error} for n, s in parsed.items()]}

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
            # out of its workspace: a later server of the same name is not in it
            error = await rename_in_workspaces(name, None)
            if error:
                return error
        return {"ok": True}

    # --- workspaces -------------------------------------------------------

    workspaces_lock = asyncio.Lock()

    async def store_workspaces(found: dict[str, Workspace]):
        """Write the workspaces file and make it current; a response on error."""
        try:
            await asyncio.to_thread(save_workspaces, settings.workspaces_file, found)
        except OSError as exc:
            return refuse(f"Cannot write {settings.workspaces_file}: {exc}", 500)
        settings.workspaces = found
        gitcredentials.bind(settings)
        return None

    async def rename_in_workspaces(old: str | None, new: str | None):
        """The server old is now new (None: deleted); a new server (old None)
        leaves any workspace that still names it."""
        if old == new:
            return None
        async with workspaces_lock:
            changed = {}
            for ws in settings.workspaces.values():
                # a workspace naming new still (a deleted server's leftover) loses it
                names = [s for s in ws.servers if new is None or s != new]
                if old is not None:
                    names = [n for n in (new if s == old else s for s in names) if n is not None]
                changed[ws.name] = names_follow(ws, dataclasses.replace(ws, servers=names), old, new)
            if changed == settings.workspaces:
                return None
            return await store_workspaces(changed)

    def names_follow(before: Workspace, ws: Workspace, old: str | None, new: str | None) -> Workspace:
        """The main instance and the subdomains of ws after the server old
        became new (None: deleted): renamed with it, or dropped."""
        prefix = f"{ws.name}-"
        gone = old[len(prefix):] if old and old in before.servers and old.startswith(prefix) else None
        now = new[len(prefix):] if new and new in ws.servers and new.startswith(prefix) else None
        main = (now or "") if gone and ws.main == gone else ws.main
        subdomains = {sub: (now if gone and target == gone else target) for sub, target in ws.subdomains.items()}
        instances = ws.instances()
        return dataclasses.replace(
            ws, main=main if main in instances else "",
            subdomains={s: t for s, t in subdomains.items() if t in instances and s not in instances})

    def submit_publish(ws: Workspace, source: str):
        """A job: the workspace's nginx site and certificate, as it is when the job runs."""
        async def action(job):
            current = settings.workspaces.get(ws.name)
            if current is None:
                raise JobFailed(f"no workspace {ws.name}")
            job.result = await workspaceops.publish(job, current)
        return jobs.submit(f"Names of workspace {ws.name}", [f"{s}/{r}" for s in ws.servers for r in ROLES],
                           source, action)

    def site_names(ws: Workspace) -> list[dict]:
        try:
            return web_names(ws.domain, [i for i in ws.instances() if workspaceops.server_name(ws, i) in settings.servers],
                             ws.main, ws.subdomains) if ws.domain else []
        except ConfigError:
            return []

    def workspace_rows() -> list[dict]:
        return [{**ws.to_dict(), "name": ws.name} for ws in sorted(settings.workspaces.values(), key=lambda w: w.name)]

    # --- git credentials (gitcredentials.py) ---------------------------------

    credentials_lock = asyncio.Lock()

    def workspace_credentials(name: str) -> list[dict]:
        try:
            return credential_rows(gitcredentials.load(settings.credentials_file), f"workspace:{name}")
        except gitcredentials.CredentialError:
            return []

    def credential_rows(found, scope: str) -> list[dict]:
        return [c.public() for c in sorted(found.get(scope, {}).values(), key=lambda c: c.host)]

    async def change_credentials(scope: str, payload: dict, delete: bool):
        """Add or change (a token left empty keeps the one saved) or delete
        one host's credential of a scope; then every repository gets them."""
        host = str(payload.get("host") or "").strip().lower()
        async with credentials_lock:
            try:
                found = await asyncio.to_thread(gitcredentials.load, settings.credentials_file)
                entries = dict(found.get(scope, {}))
                if delete:
                    if entries.pop(host, None) is None:
                        return refuse(f"No credential for {host}", status.HTTP_404_NOT_FOUND)
                else:
                    token = str(payload.get("token") or "")
                    if not token and host in entries:
                        token = entries[host].token
                    cred = gitcredentials.check(host, str(payload.get("username") or "").strip(), token)
                    entries[cred.host] = cred
                found[scope] = entries
                await asyncio.to_thread(gitcredentials.save, settings.credentials_file, found)
            except gitcredentials.CredentialError as exc:
                return refuse(str(exc))
            except OSError as exc:
                return refuse(f"Cannot write {settings.credentials_file}: {exc}", 500)
            await asyncio.to_thread(gitcredentials.bind, settings)
        return {"ok": True, "credentials": credential_rows(found, scope)}

    @app.get("/credentials", dependencies=[Depends(admin)])
    async def credentials_page(request: Request):
        """The git credentials of every server and workspace (users of the machine only)."""
        try:
            found = await asyncio.to_thread(gitcredentials.load, settings.credentials_file)
            error = None
        except gitcredentials.CredentialError as exc:
            found, error = {}, str(exc)
        scopes = ([{"kind": "server", "name": n, "workspace": settings.workspace_of(n),
                    "credentials": credential_rows(found, f"server:{n}")} for n in sorted(settings.servers)]
                  + [{"kind": "workspace", "name": n, "workspace": None,
                      "credentials": credential_rows(found, f"workspace:{n}")} for n in sorted(settings.workspaces)])
        return _TEMPLATES.TemplateResponse(
            request, "credentials.html",
            {"scopes": scopes, "error": error, "credentials_file": settings.credentials_file})

    @app.post("/api/credentials/save", dependencies=[Depends(admin)])
    async def credentials_save(payload: dict = Body(...)):
        try:
            scope = gitcredentials.scope_of(str(payload.get("kind") or ""), str(payload.get("name") or ""))
        except gitcredentials.CredentialError as exc:
            return refuse(str(exc))
        name = scope.split(":", 1)[1]
        if name not in (settings.servers if scope.startswith("server:") else settings.workspaces):
            return refuse(f"No {scope.split(':', 1)[0]} {name}", status.HTTP_404_NOT_FOUND)
        return await change_credentials(scope, payload, delete=False)

    @app.post("/api/credentials/delete", dependencies=[Depends(admin)])
    async def credentials_delete(payload: dict = Body(...)):
        try:
            scope = gitcredentials.scope_of(str(payload.get("kind") or ""), str(payload.get("name") or ""))
        except gitcredentials.CredentialError as exc:
            return refuse(str(exc))
        return await change_credentials(scope, payload, delete=True)

    @app.post("/api/workspaces/{workspace}/credentials/save", dependencies=[Depends(admin)])
    async def workspace_credentials_save(workspace: str, payload: dict = Body(...)):
        """The workspace's git credentials, set by its admins."""
        ws = a_workspace(workspace)
        return await change_credentials(gitcredentials.scope_of("workspace", ws.name), payload, delete=False)

    @app.post("/api/workspaces/{workspace}/credentials/delete", dependencies=[Depends(admin)])
    async def workspace_credentials_delete(workspace: str, payload: dict = Body(...)):
        ws = a_workspace(workspace)
        return await change_credentials(gitcredentials.scope_of("workspace", ws.name), payload, delete=True)

    @app.get("/workspaces", dependencies=[Depends(admin)])
    async def workspaces_page(request: Request):
        """The customers' workspaces (users of the machine only)."""
        return _TEMPLATES.TemplateResponse(
            request, "workspaces.html",
            {"workspaces": workspace_rows(), "workspaces_file": settings.workspaces_file,
             "servers": sorted(settings.servers), "users": sorted(settings.users),
             "user_roles": {n: settings.user_role(n) for n in settings.users},
             "storage_roots": settings.storage_roots, "roles": ACCESS_ROLES, "limits": WORKSPACE_LIMITS,
             "templates": await asyncio.to_thread(template_rows),
             "templates_index": templatestore.index_url(settings.plugin_source)},
        )

    def template_rows() -> list[dict]:
        """The templates here: the package's and those downloaded, and which one is used."""
        package = stacktemplates.available([workspacehelper.TEMPLATES])
        downloaded = stacktemplates.available([templatestore.INSTALLED])
        used = workspaceops.templates()
        return [{"name": name, "label": used[name].label, "package": package[name].version if name in package else None,
                 "downloaded": downloaded[name].version if name in downloaded else None,
                 "used": used[name].version} for name in sorted(used)]

    @app.get("/api/templates/available", dependencies=[Depends(admin)])
    async def templates_available():
        """The templates published for download (signed), with the version used here."""
        try:
            found = await templatestore.available(settings.plugin_source)
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        used = await asyncio.to_thread(workspaceops.templates)
        return {"index": templatestore.index_url(settings.plugin_source),
                "templates": [{**t, "used": used[t["name"]].version if t["name"] in used else None} for t in found]}

    @app.post("/api/templates/{name}/install")
    async def template_install(name: str, user: Principal = Depends(admin)):
        """Download a published template, check it, and install it through
        the root helper: new instances are made from it (the higher version)."""
        try:
            found = {t["name"]: t for t in await templatestore.available(settings.plugin_source)}
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        template = found.get(name)
        if template is None:
            return refuse(f"The index publishes no template {name}", status.HTTP_404_NOT_FOUND)
        if template.get("error"):
            return refuse(f"{name}: {template['error']}")

        async def action(job):
            manifest = await templatestore.download(job, settings.plugin_source, template,
                                                    templatestore.templates_dir(settings.jobs_dir))
            await ops.run(job, workspaceops.helper("template-install", name), timeout=workspaceops.HELPER_TIMEOUT)
            job.result = {"template": name, "version": manifest["version"]}
            job.log(f"Template {name} {manifest['version']} installed: new instances are made from it")

        job = jobs.submit(f"Install template {name} {template.get('version', '')}".strip(), ["omniacli/templates"],
                          f"manual: {user}", action)
        return {"job": job.id}

    @app.post("/api/workspaces/save", dependencies=[Depends(admin)])
    async def workspace_save(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Create (new: true) or change a workspace: its servers and members
        must exist, a server and a domain are in one workspace only."""
        name = str(payload.get("name") or "").strip()
        new = bool(payload.get("new"))
        async with workspaces_lock:
            if new and name in settings.workspaces:
                return refuse(f"A workspace named {name} already exists")
            if not new and name not in settings.workspaces:
                return refuse(f"No workspace {name}", status.HTTP_404_NOT_FOUND)
            data = {key: payload.get(key) for key in ("label", "domain", "storage", "limits", "servers", "members")}
            before = settings.workspaces.get(name)
            if before is not None:
                # the names are the workspace's admins' (its page)
                data.update(main=before.main, subdomains=before.subdomains, certificate=before.certificate)
            try:
                found = parse_workspaces({**{n: ws.to_dict() for n, ws in settings.workspaces.items()}, name: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                return refuse(str(exc))
            ws = found[name]
            unknown = [s for s in ws.servers if s not in settings.servers]
            if unknown:
                return refuse(f"No server {', '.join(unknown)}")
            strangers = [u for u in ws.members if u not in settings.users]
            if strangers:
                return refuse(f"No user {', '.join(strangers)}")
            refused = await store_workspaces(found)
            if refused:
                return refused
        if before is not None and before.domain != ws.domain and ws.instances():
            return {"ok": True, "job": submit_publish(ws, f"manual: {me}").id}
        return {"ok": True}

    @app.post("/api/workspaces/delete", dependencies=[Depends(admin)])
    async def workspace_delete(payload: dict = Body(...)):
        """Delete a workspace: its servers stay, its users of workspaces only
        no longer see them."""
        name = str(payload.get("name") or "")
        async with workspaces_lock:
            if name not in settings.workspaces:
                return refuse(f"No workspace {name}", status.HTTP_404_NOT_FOUND)
            return await store_workspaces({n: ws for n, ws in settings.workspaces.items() if n != name}) \
                or {"ok": True}

    def a_workspace(name: str) -> Workspace:
        found = settings.workspaces.get(name)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No workspace {name}")
        return found

    @app.get("/workspaces/{workspace}", dependencies=[Depends(admin)])
    async def workspace_page(request: Request, workspace: str):
        """The users and the instances of a workspace, for its admins (and the machine's)."""
        ws = a_workspace(workspace)
        me = request.state.user
        members = [{"name": n, "role": r, "only_here": only_here(n, ws.name)} for n, r in sorted(ws.members.items())]
        found = await asyncio.to_thread(workspaceops.templates)
        return _TEMPLATES.TemplateResponse(
            request, "workspace.html",
            {"ws": ws, "members": members, "roles": ACCESS_ROLES, "me": me.name, "min_password": _MIN_PASSWORD,
             "servers": [n for n in ws.servers if n in settings.servers],
             "templates": [t.to_dict() for t in found.values()],
             "can_create": bool(ws.storage and ws.limits.get("disk_gb")),
             "instances": [i for i in ws.instances() if workspaceops.server_name(ws, i) in settings.servers],
             "repositories": {n: {role: [{"name": r.name, "branch": r.branch} for r in getattr(settings.servers[n], role).repositories]
                                  for role in ROLES if getattr(settings.servers[n], role) is not None}
                              for n in ws.servers if n in settings.servers},
             "sites": site_names(ws), "certificates": CERTIFICATES,
             "credentials": await asyncio.to_thread(workspace_credentials, ws.name),
             "instances_left": None if not ws.limits.get("instances")
             else int(ws.limits["instances"]) - len([n for n in ws.servers if n in settings.servers])},
        )

    @app.post("/api/workspaces/{workspace}/instances/create", dependencies=[Depends(admin)])
    async def instance_create(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new instance of the workspace, production and test, from a
        template: a job (the disk, the projects, the databases); then the
        server, of type docker, is the workspace's."""
        ws = a_workspace(workspace)
        instance = str(payload.get("instance") or "").strip()
        if not WORKSPACE_NAME.fullmatch(instance):
            return refuse("An instance's name is 1-32 lower case letters, digits and -")
        if instance == TEST_LABEL or instance in ws.subdomains:
            return refuse(f"{instance} is {'the name of the test sites' if instance == TEST_LABEL else 'a subdomain'} "
                          "of the workspace: an instance needs another name")
        name = workspaceops.server_name(ws, instance)
        template = (await asyncio.to_thread(workspaceops.templates)).get(str(payload.get("template") or ""))
        if template is None:
            return refuse(f"No template {payload.get('template')!r}")
        try:
            values = template.values({str(k): str(v) for k, v in (payload.get("values") or {}).items()})
        except stacktemplates.TemplateError as exc:
            return refuse(str(exc))
        if not ws.storage or not ws.limits.get("disk_gb"):
            return refuse(f"Workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
        async with workspaces_lock:
            ws = a_workspace(workspace)
            if name in settings.servers or name in ws.servers:
                return refuse(f"A server named {name} exists already")
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            # the server is the workspace's from now: its job is seen there
            refused = await store_workspaces({**settings.workspaces,
                                              ws.name: dataclasses.replace(ws, servers=[*ws.servers, name])})
            if refused:
                return refused

        async def action(job):
            try:
                data = await workspaceops.create_instance(job, ws, instance, template, values)
                parsed = parse_servers({name: {**data, "status": "active"}}, settings.logs_dir)[name]
                async with config_lock:
                    error = await store({**settings.servers, name: parsed})
                if error is not None:
                    raise JobFailed(f"the instance runs, but the servers file was not written: {error.body.decode()}")
            except BaseException:
                await rename_in_workspaces(name, None)
                raise
            job.result = {"server": name}
            job.log(f"Server {name}: production at http://127.0.0.1:{parsed.production.options.http_port}, "
                    f"test at http://127.0.0.1:{parsed.test.options.http_port}")
            current = settings.workspaces.get(ws.name)
            if current is not None and current.domain:
                try:
                    await workspaceops.publish(job, current)
                except JobFailed as exc:
                    job.log(f"The instance runs, its names are not published: {exc}")

        job = jobs.submit(f"Create {name} from {template.name}", [f"{name}/production", f"{name}/test"],
                          f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/workspaces/{workspace}/instances/delete", dependencies=[Depends(admin)])
    async def instance_delete(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """An instance of the workspace removed: its containers, data and
        folders, then its server."""
        ws = a_workspace(workspace)
        name = str(payload.get("server") or "")
        prefix = f"{ws.name}-"
        if name not in ws.servers or not name.startswith(prefix) or name not in settings.servers:
            return refuse(f"No instance {name} in workspace {ws.name}", status.HTTP_404_NOT_FOUND)
        if server_busy(name):
            return refuse(f"A job is running on {name}: try again when it ends")
        instance = name[len(prefix):]

        async def action(job):
            await workspaceops.remove_instance(job, ws, instance)
            async with config_lock:
                error = await store({k: v for k, v in settings.servers.items() if k != name})
            if error is not None:
                raise JobFailed(f"the instance is removed, but the servers file was not written: {error.body.decode()}")
            await rename_in_workspaces(name, None)
            job.log(f"Server {name} removed")
            current = settings.workspaces.get(ws.name)
            if current is not None:
                try:
                    await workspaceops.publish(job, current)
                except JobFailed as exc:
                    job.log(f"The names of the workspace are not published again: {exc}")

        job = jobs.submit(f"Remove {name}", [f"{name}/production", f"{name}/test"], f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/workspaces/{workspace}/web/save", dependencies=[Depends(admin)])
    async def web_save(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The names of the workspace (its admins): the main instance, the
        subdomains, where the certificate comes from; then a job publishes them."""
        async with workspaces_lock:
            ws = a_workspace(workspace)
            data = {**ws.to_dict(), "main": payload.get("main") or "",
                    "subdomains": payload.get("subdomains") or {}, "certificate": payload.get("certificate")}
            try:
                found = parse_workspaces({**{n: w.to_dict() for n, w in settings.workspaces.items()}, workspace: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                return refuse(str(exc))
            ws = found[workspace]
            refused = await store_workspaces(found)
            if refused:
                return refused
        if not ws.domain or not ws.instances():
            return {"ok": True}
        return {"ok": True, "job": submit_publish(ws, f"manual: {me}").id}

    @app.post("/api/workspaces/{workspace}/web/publish", dependencies=[Depends(admin)])
    async def web_publish(workspace: str, me: Principal = Depends(current_user)):
        """The names published again (after the DNS records were made, a certificate failed)."""
        ws = a_workspace(workspace)
        if not ws.storage:
            return refuse(f"Workspace {ws.name} has no storage: the machine's admins set it")
        return {"job": submit_publish(ws, f"manual: {me}").id}

    @app.post("/api/workspaces/{workspace}/web/certificate", dependencies=[Depends(admin)])
    async def web_certificate(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The customer's own certificate (PEM, with its chain) and key; the
        names are then published with it."""
        ws = a_workspace(workspace)
        if ws.certificate != "own":
            return refuse("The workspace takes its certificate from Let's Encrypt: choose its own certificate first")
        ok, out = await workspaceops.own_certificate(ws, str(payload.get("certificate") or ""),
                                                     str(payload.get("key") or ""))
        if not ok:
            return refuse(out.removeprefix("workspace-helper: "))
        try:
            made = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            made = {}
        return {"ok": True, **made, "job": submit_publish(ws, f"manual: {me}").id}

    def workspace_instance(ws: Workspace, payload: dict):
        """(server name, instance) of a request on an instance of ws; a response when refused."""
        name = str(payload.get("server") or "")
        prefix = f"{ws.name}-"
        if name not in ws.servers or not name.startswith(prefix) or name not in settings.servers:
            return refuse(f"No instance {name} in workspace {ws.name}", status.HTTP_404_NOT_FOUND)
        if any(getattr(settings.servers[name], role) is None for role in ROLES):
            return refuse(f"{name} has no production and test")
        if server_busy(name):
            return refuse(f"A job is running on {name}: try again when it ends")
        return name, name[len(prefix):]

    def repository_rows(repos) -> list[dict]:
        return [{"name": r.name, "path": str(r.path), "branch": r.branch, "remote": r.remote} for r in repos]

    async def set_repositories(name: str, repositories: dict[str, list[dict]]):
        """The repositories of the roles in the servers file; the server as it is then."""
        async with config_lock:
            data = server_to_dict(settings.servers[name])
            for role, repos in repositories.items():
                data[role]["repositories"] = repos
            try:
                parsed = parse_servers({name: data}, settings.logs_dir)[name]
            except ConfigError as exc:
                raise JobFailed(str(exc)) from None
            error = await store({**settings.servers, name: parsed})
        if error is not None:
            raise JobFailed(f"the servers file was not written: {error.body.decode()}")
        return settings.servers[name]

    @app.post("/api/workspaces/{workspace}/repositories/add", dependencies=[Depends(admin)])
    async def repository_add(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A customer's repository (https, the workspace's git credentials)
        cloned into production and test of an instance, each on its branch
        (a test branch is merged into production's as for every server),
        named in their odoo.conf, both restarted: its modules can then be
        installed from Odoo's Apps."""
        ws = a_workspace(workspace)
        found = workspace_instance(ws, payload)
        if not isinstance(found, tuple):
            return found
        name, instance = found
        repo, url = str(payload.get("name") or "").strip(), str(payload.get("url") or "").strip()
        branches = {role: str(payload.get(f"{role}_branch") or "").strip() for role in ROLES}
        try:
            for branch in branches.values():
                workspaceops.check_repository(repo, url, branch)
        except JobFailed as exc:
            return refuse(str(exc))
        server = settings.servers[name]
        if any(r.name == repo for role in ROLES for r in getattr(server, role).repositories):
            return refuse(f"{name} has a repository {repo} already")

        async def action(job):
            options = gitcredentials.options_for(settings, name)
            current = {role: list(getattr(server, role).repositories) for role in ROLES}
            cloned = {}
            try:
                for role in ROLES:
                    cloned[role] = await workspaceops.clone(job, ws, instance, role, repo, url, branches[role], options)
                for role in ROLES:
                    await workspaceops.write_conf(job, ws, instance, role, [*(r.name for r in current[role]), repo])
                done = await set_repositories(name, {role: [*repository_rows(current[role]), {
                    "name": repo, "path": str(cloned[role]), "branch": branches[role], "remote": "origin"}]
                    for role in ROLES})
            except BaseException:
                for role in ROLES:
                    await workspaceops.write_conf(job, ws, instance, role, [r.name for r in current[role]])
                    if role in cloned:
                        workspaceops.remove_clone(cloned[role])
                raise
            for role in ROLES:
                await ops.restart(job, getattr(done, role))
            job.log(f"{repo} is in {name}: install its modules from Odoo's Apps")

        job = jobs.submit(f"Add repository {repo} to {name}", [f"{name}/{role}" for role in ROLES],
                          f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/workspaces/{workspace}/repositories/remove", dependencies=[Depends(admin)])
    async def repository_remove(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A customer's repository out of an instance: out of the odoo.conf
        and of the server, both roles restarted, the clones deleted."""
        ws = a_workspace(workspace)
        found = workspace_instance(ws, payload)
        if not isinstance(found, tuple):
            return found
        name, instance = found
        repo = str(payload.get("name") or "")
        server = settings.servers[name]
        if not any(r.name == repo for role in ROLES for r in getattr(server, role).repositories):
            return refuse(f"{name} has no repository {repo}", status.HTTP_404_NOT_FOUND)

        async def action(job):
            kept = {role: [r for r in getattr(server, role).repositories if r.name != repo] for role in ROLES}
            for role in ROLES:
                await workspaceops.write_conf(job, ws, instance, role, [r.name for r in kept[role]])
            done = await set_repositories(name, {role: repository_rows(kept[role]) for role in ROLES})
            for role in ROLES:
                await ops.restart(job, getattr(done, role))
                workspaceops.remove_clone(workspaceops.addons_folder(ws, instance, role) / repo)
            job.log(f"{repo} removed from {name}")

        job = jobs.submit(f"Remove repository {repo} from {name}", [f"{name}/{role}" for role in ROLES],
                          f"manual: {me}", action)
        return {"job": job.id}

    def only_here(user: str, workspace: str) -> bool:
        """user is a user of workspaces only, and of this one only: its
        workspace's admins may change its password and delete it."""
        return (settings.user_role(user) == WORKSPACE_ROLE and user not in settings.fixed.users
                and [ws for ws, _ in settings.memberships(user)] == [workspace])

    @app.post("/api/workspaces/{workspace}/members/save", dependencies=[Depends(admin)])
    async def member_save(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A workspace's admin creates a user of it (new: true, with a
        password: a user of workspaces only), changes a member's role, and
        the password of a member of this workspace only."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        password, new = str(payload.get("password") or ""), bool(payload.get("new"))
        if role not in ACCESS_ROLES:
            return refuse(f"The role must be one of {', '.join(ACCESS_ROLES)}")
        async with access_lock, workspaces_lock:
            ws = a_workspace(workspace)
            if new:
                refused = check_name("User", name, settings.fixed.users, settings.users, True)
                if refused:
                    return refused
                if len(password) < _MIN_PASSWORD:
                    return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            else:
                if name not in ws.members:
                    return refuse(f"{name} is not a user of {workspace}", status.HTTP_404_NOT_FOUND)
                if me.kind == "user" and name == me.name and role != ws.members[name]:
                    return refuse("You cannot change your own role")
                if password and not only_here(name, workspace):
                    return refuse(f"{name} is also a user of other workspaces or of the machine: "
                                  "ask the administrator of the machine for its password")
                if password and len(password) < _MIN_PASSWORD:
                    return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            if new or password:
                web = web_copy()
                web.users[name] = await asyncio.to_thread(hash_password, password)
                if new:
                    web.roles[name] = WORKSPACE_ROLE
                refused = await store_access(web)
                if refused:
                    return refused
            found = dict(settings.workspaces)
            found[workspace] = dataclasses.replace(ws, members={**ws.members, name: role})
            return await store_workspaces(found) or {"ok": True}

    @app.post("/api/workspaces/{workspace}/members/delete", dependencies=[Depends(admin)])
    async def member_delete(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Out of the workspace; a user of this workspace only is deleted."""
        name = str(payload.get("name") or "")
        async with access_lock, workspaces_lock:
            ws = a_workspace(workspace)
            if name not in ws.members:
                return refuse(f"{name} is not a user of {workspace}", status.HTTP_404_NOT_FOUND)
            if me.kind == "user" and name == me.name:
                return refuse("You cannot remove yourself")
            delete_user = only_here(name, workspace)
            found = dict(settings.workspaces)
            found[workspace] = dataclasses.replace(ws, members={n: r for n, r in ws.members.items() if n != name})
            refused = await store_workspaces(found)
            if refused:
                return refused
            if delete_user:
                web = web_copy()
                web.users.pop(name, None)
                web.roles.pop(name, None)
                refused = await store_access(web)
                if refused:
                    return refused
            return {"ok": True, "deleted": delete_user}

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

    def check_role(role: str, roles: tuple[str, ...] = ACCESS_ROLES):
        return None if role in roles else refuse(f"The role must be one of {', '.join(roles)}")

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
                "user_roles": USER_ROLES,
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
             "group": ENTRY_POINT_GROUP, "route_errors": route_errors,
             "source": settings.plugin_source,
             "sudoers": pluginstore.sudoers_line(pwd.getpwuid(os.getuid()).pw_name),
             "systemd": bool(os.environ.get("INVOCATION_ID"))},
        )

    def installed_plugins() -> dict[str, str]:
        """{plugin name: version of its package} of the plugins found."""
        return {i.name: i.version for i in plugins.registry().found}

    @app.get("/api/plugins/available", dependencies=[Depends(admin)])
    async def plugins_available():
        """The plugins published for download (signed), with the version loaded here."""
        try:
            found = await pluginstore.available(settings.plugin_source)
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        loaded = installed_plugins()
        return {"index": settings.plugin_source.index,
                "plugins": [{**p, "installed": loaded.get(p["name"])} for p in found]}

    @app.post("/api/plugins/{name}/install")
    async def plugin_install(name: str, user: Principal = Depends(admin)):
        """Download a published plugin, check it, and install it through the
        root helper; OmniaCLI loads it once restarted."""
        try:
            found = {p["name"]: p for p in await pluginstore.available(settings.plugin_source)}
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        plugin = found.get(name)
        if plugin is None:
            return refuse(f"The index publishes no plugin {name}", status.HTTP_404_NOT_FOUND)
        if plugin.get("error"):
            return refuse(f"{name}: {plugin['error']}")

        async def action(job):
            manifest = await pluginstore.download(job, settings.plugin_source, plugin,
                                                  pluginstore.plugins_dir(settings.jobs_dir))
            await ops.run(job, pluginstore.install_command(name), timeout=pluginstore.INSTALL_TIMEOUT)
            job.result = {"plugin": name, "version": manifest["version"], "restart": True}
            job.log(f"{name} {manifest['version']} installed: it is loaded when OmniaCLI restarts "
                    "(Restart OmniaCLI on the Plugins page)")

        job = jobs.submit(f"Install plugin {name} {plugin.get('version', '')}".strip(), ["omniacli/plugins"],
                          f"manual: {user}", action)
        return {"job": job.id}

    @app.post("/api/omniacli/restart")
    async def omniacli_restart(user: Principal = Depends(admin)):
        """Restart OmniaCLI (a plugin installed is loaded): it exits, and
        systemd starts it again (Restart=on-failure)."""
        if not os.environ.get("INVOCATION_ID"):
            return refuse("OmniaCLI is not run by systemd here: restart it yourself")
        running = [j.title for j in jobs.recent(500) if not j.finished_flag]
        if running:
            return refuse(f"Jobs are running ({', '.join(running[:3])}): restart when they end")
        logging.getLogger(__name__).warning("restart asked by %s", user)
        asyncio.get_running_loop().call_later(1.0, os._exit, 75)
        return {"ok": True}

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
            refused = (check_name("User", name, settings.fixed.users, settings.users, new)
                       or check_role(role, USER_ROLES))
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

    # --- the shell of an instance ---------------------------------------

    @app.get("/static/{name}")
    async def static_file(name: str):
        if name not in _STATIC_FILES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
        return FileResponse(_STATIC / name, media_type=_STATIC_FILES[name],
                            headers={"Cache-Control": "private, max-age=86400"})

    def shell_folder(instance: Instance) -> Path | None:
        """Where the shell of an instance starts: its first repository."""
        return instance.repositories[0].path if instance.repositories else None

    def claude_run(instance: Instance):
        # Claude Code in the instance's Claude folder (no folder: no Claude)
        if instance.claude_dir is None:
            return f"{instance.key} has no Claude folder"
        return shell.CLAUDE_COMMAND, {}, True

    def psql_run(instance: Instance):
        # the plugin's database client; the page ends with it
        found = instance.plugin.db_shell(instance)
        if found is None:
            return f"{instance.key} has no database shell"
        command, env = found
        return command, env, False

    # the shells of an instance: kind -> (where it starts, what it runs:
    # (command first, its environment, the shell stays after it), or why
    # the instance has none of that kind)
    shell_kinds = {
        "shell": (shell_folder, lambda instance: (None, {}, True)),
        "claude": (lambda instance: instance.claude_dir, claude_run),
        "psql": (shell_folder, psql_run),
    }

    def shell_instance(server: str, role: str, kind: str) -> Instance:
        active_server(server)
        instance = get_instance(server, role)
        run = shell_kinds[kind][1](instance)
        if isinstance(run, str):
            raise HTTPException(status.HTTP_404_NOT_FOUND, run)
        return instance

    async def shell_page(request: Request, server: str, role: str, kind: str):
        instance = shell_instance(server, role, kind)
        folder, run = shell_kinds[kind]
        command, _, stay = run(instance)
        return _TEMPLATES.TemplateResponse(request, "shell.html", {
            "instance": instance, "folder": folder(instance), "kind": kind, "command": command, "stay": stay,
            "system_user": pwd.getpwuid(os.getuid()).pw_name, "shell": shell.login_shell()[0]})

    async def shell_socket(websocket: WebSocket, server: str, role: str, kind: str, me: Principal):
        """A login shell of the user running OmniaCLI, in the instance's first
        repository, or running claude in its Claude folder (developer: a user
        here, or a link acting for one)."""
        await websocket.accept()
        srv = settings.servers.get(server)
        instance = srv.instances.get(role) if srv is not None and srv.active else None
        if not me.can("developer"):
            await websocket.close(1008, "The developer role is required")
            return
        if instance is None:
            await websocket.close(1008, f"No instance {server}/{role}")
            return
        folder, run = shell_kinds[kind]
        run = run(instance)
        if isinstance(run, str):
            await websocket.close(1008, run)
            return
        command, env, stay = run
        ip = websocket.client.host if websocket.client else ""
        who = f", acting for {me.on_behalf}" if me.on_behalf else ""
        what = f"{instance.key} {kind}" if command else instance.key
        access_log.record("shell", me.kind, me.name, ip, f"{what} opened{who}")
        opened = time.monotonic()
        try:
            await shell.serve(websocket, folder(instance), command, env, stay)
        except OSError as exc:
            _logger.warning("shell of %s: %s", instance.key, exc)
            with contextlib.suppress(RuntimeError):
                await websocket.close(1011, f"The shell did not start: {exc}"[:120])
        finally:
            minutes = round((time.monotonic() - opened) / 60)
            access_log.record("shell", me.kind, me.name, ip, f"{what} closed after {minutes} min{who}")

    async def link_shell_socket(websocket: WebSocket, name: str, server: str, role: str, kind: str,
                                me: Principal):
        """The shell of a linked instance's server, as the link, with at most our role."""
        await websocket.accept()
        link = settings.link_targets.get(name)
        if me.kind != "user" or link is None:
            await websocket.close(1008, "Only a user who logs in here may use a link" if link else f"No link {name}")
            return
        try:
            await proxy_websocket(websocket, link, f"servers/{server}/{role}/{kind}/ws",
                                  link_headers(link, me.name, me.role))
        except Exception as exc:  # refused, unreachable, closed: said to the page
            with contextlib.suppress(RuntimeError):
                await websocket.close(1011, f"link {name}: {describe_error(exc)}"[:120])

    def shell_routes(kind: str) -> None:
        """The page and the WebSockets of one kind of shell (a path each: a
        /servers/{server}/{role}/{kind} would catch the plugins' routes)."""
        @app.get(f"/servers/{{server}}/{{role}}/{kind}", name=f"{kind}_page")
        async def page(request: Request, server: str, role: str, me: Principal = Depends(developer)):
            return await shell_page(request, server, role, kind)

        @app.websocket(f"/servers/{{server}}/{{role}}/{kind}/ws", name=f"{kind}_socket")
        async def socket(websocket: WebSocket, server: str, role: str, me: Principal = Depends(current_user)):
            await shell_socket(websocket, server, role, kind, me)

        @app.websocket(f"/links/{{name}}/servers/{{server}}/{{role}}/{kind}/ws", name=f"link_{kind}_socket")
        async def link_socket(websocket: WebSocket, name: str, server: str, role: str,
                              me: Principal = Depends(current_user)):
            await link_shell_socket(websocket, name, server, role, kind, me)

    for kind in shell_kinds:
        shell_routes(kind)

    # --- tunnels -----------------------------------------------------------

    @app.websocket("/servers/{server}/{role}/tunnel/ws")
    async def tunnel_socket(websocket: WebSocket, server: str, role: str,
                            me: Principal = Depends(current_user)):
        """One connection of a tunnel opened by a linked OmniaCLI (or by a
        user here), to the instance's web interface; ?probe=1 only says
        whether it can be reached."""
        await websocket.accept()
        if me.kind == "token":
            await websocket.close(1008, "An MCP token cannot open a tunnel")
            return
        srv = settings.servers.get(server)
        instance = srv.instances.get(role) if srv is not None and srv.active else None
        if instance is None:
            await websocket.close(1008, f"No instance {server}/{role}")
            return
        if instance.plugin.tunnel_address(instance, "/") is None:
            await websocket.close(1008, f"{instance.key} has no web port")
            return
        if websocket.query_params.get("probe"):
            ip = websocket.client.host if websocket.client else ""
            who = f", acting for {me.on_behalf}" if me.on_behalf else ""
            access_log.record("tunnel", me.kind, me.name, ip, f"{instance.key} opened{who}")
            await websocket.close(1000, "ready")
            return
        await tunnel.serve_far(websocket, lambda path: instance.plugin.tunnel_address(instance, path))

    def tunnel_url(request: Request, found: tunnel.Tunnel) -> str:
        """The address of a tunnel, as the browser reaches this OmniaCLI."""
        wildcard = found.bind in ("0.0.0.0", "::")
        host = (request.url.hostname or "localhost") if wildcard else found.bind
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{found.port}/"

    def tunnel_rows(request: Request, me: Principal) -> list[dict]:
        rows = []
        for found in tunnels.tunnels.values():
            row = found.to_dict()
            row["url"] = tunnel_url(request, found)
            row["can_close"] = found.owner == me.name or me.can("admin")
            rows.append(row)
        return rows

    def tunnel_user(me: Principal) -> Principal:
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may open a tunnel")
        return me

    @app.get("/tunnels")
    async def tunnels_page(request: Request, me: Principal = Depends(current_user)):
        tunnel_user(me)
        rows = await remote_servers(me)
        return _TEMPLATES.TemplateResponse(request, "tunnels.html", {
            "tunnels": tunnel_rows(request, me), "servers": [r for r in rows if "error" not in r],
            "errors": [r for r in rows if "error" in r], "options": settings.options})

    @app.get("/api/tunnels")
    async def tunnels_list(request: Request, me: Principal = Depends(current_user)):
        return tunnel_rows(request, tunnel_user(me))

    @app.post("/api/tunnels/open")
    async def tunnels_open(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        tunnel_user(me)
        link = settings.link_targets.get(str(payload.get("link") or ""))
        server, role = str(payload.get("server") or ""), str(payload.get("role") or "")
        if link is None:
            return refuse(f"No link {payload.get('link')}")
        if not server or role not in ("production", "test"):
            return refuse("Choose a server and an instance (production or test)")
        try:
            found = await tunnels.open(link, server, role, me.name, me.role)
        except tunnel.TunnelError as exc:
            return refuse(str(exc))
        return {**found.to_dict(), "url": tunnel_url(request, found)}

    @app.post("/api/tunnels/{tunnel_id}/close")
    async def tunnels_close(tunnel_id: str, me: Principal = Depends(current_user)):
        tunnel_user(me)
        found = tunnels.tunnels.get(tunnel_id)
        if found is None:
            return refuse("No such tunnel (closed already?)", status.HTTP_404_NOT_FOUND)
        if found.owner != me.name and not me.can("admin"):
            return refuse("Only who opened it, or an admin, closes a tunnel", status.HTTP_403_FORBIDDEN)
        await tunnels.close(tunnel_id, f"closed by {me.name}")
        return {"ok": True}

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

    # --- pullers this instance relays ----------------------------------

    pullers_lock = asyncio.Lock()

    def puller_answer(exc: puller.PullerRefused) -> JSONResponse:
        return refuse(str(exc), exc.status)

    def puller_link(me: Principal) -> str:
        """The link a puller's request comes from: only links take part."""
        if me.kind != "link":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "A puller is used by a linked OmniaCLI only")
        return me.name

    async def puller_message(request: Request, key: str) -> dict | None:
        """The envelope (a mapping) at key of the JSON body, at most
        puller.MAX_MESSAGE bytes; None when key is missing or null."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > puller.MAX_MESSAGE:
                raise puller.PullerRefused(f"a message is at most {puller.MAX_MESSAGE >> 20} MiB", 413)
        try:
            data = json.loads(bytes(body) or b"{}")
        except ValueError:
            raise puller.PullerRefused("the body is not JSON") from None
        if not isinstance(data, dict):
            raise puller.PullerRefused("the body is not a JSON object")
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise puller.PullerRefused(f"{key} is not an envelope")
        return value

    def wait_param(request: Request) -> float:
        try:
            return float(request.query_params.get("wait", 0))
        except ValueError:
            raise puller.PullerRefused("wait is a number of seconds") from None

    @app.get("/api/pullers")
    async def puller_list(me: Principal = Depends(current_user)):
        """The pullers the calling link takes part in, and its part."""
        link = puller_link(me)
        return {"pullers": [{"name": d.name, "part": d.part(link)}
                            for d in settings.pullers.values() if d.part(link)]}

    @app.post("/api/pullers/{name}/producer")
    async def puller_producer(request: Request, name: str, me: Principal = Depends(current_user)):
        """The producer's poll: its report (if any) is kept, then the
        commands for it, waiting up to ?wait= seconds for one."""
        link = puller_link(me)
        try:
            pullers.member(name, link, "producer")
            report = await puller_message(request, "report")
            return await pullers.producer_poll(name, link, report, wait_param(request))
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.put("/api/pullers/{name}/producer/files/{file}")
    async def puller_upload(request: Request, name: str, file: str, me: Principal = Depends(current_user)):
        link = puller_link(me)
        length = request.headers.get("content-length")
        try:
            return await pullers.upload(name, link, file, request.stream(),
                                        int(length) if length and length.isdigit() else None)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/api/pullers/{name}")
    async def puller_info(name: str, me: Principal = Depends(current_user)):
        link = puller_link(me)
        try:
            definition = pullers.definition(name)
            if definition.part(link) is None:
                raise puller.PullerRefused(f"No puller {name}", 404)
            return pullers.info(name)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.post("/api/pullers/{name}/commands")
    async def puller_command(request: Request, name: str, me: Principal = Depends(current_user)):
        """A consumer's command (an envelope), queued for the producer."""
        link = puller_link(me)
        try:
            pullers.member(name, link, "consumer")
            envelope = await puller_message(request, "command")
            if envelope is None:
                raise puller.PullerRefused("command missing")
            return await pullers.consumer_command(name, link, envelope)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/api/pullers/{name}/reports")
    async def puller_reports(request: Request, name: str, since: int = 0, me: Principal = Depends(current_user)):
        """The producer's reports after ?since=, waiting up to ?wait= seconds
        for one, with what the relay knows of the puller."""
        link = puller_link(me)
        try:
            return await pullers.consumer_reports(name, link, since, wait_param(request))
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/api/pullers/{name}/files/{file}")
    async def puller_download(name: str, file: str, me: Principal = Depends(current_user)):
        link = puller_link(me)
        try:
            path = pullers.download_path(name, link, file)
        except puller.PullerRefused as exc:
            return puller_answer(exc)
        return FileResponse(path, media_type="application/octet-stream", filename=file)

    @app.delete("/api/pullers/{name}/files/{file}")
    async def puller_taken(name: str, file: str, me: Principal = Depends(current_user)):
        """The consumer has the file whole: it is deleted here."""
        link = puller_link(me)
        try:
            return pullers.taken(name, link, file)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/pullers", dependencies=[Depends(admin)])
    async def pullers_page(request: Request):
        rows = []
        for name in sorted(settings.pullers):
            info = pullers.info(name)
            info["missing"] = [p for p in [info["producer"]["link"]] + [c["link"] for c in info["consumers"]]
                               if p not in settings.links]
            rows.append(info)
        published = []
        for key, pub in settings.publications.items():
            row = pub.to_dict()
            row.pop("key", None)
            published.append({**row, "instance": key, "has_key": bool(pub.key), "status": publications.status(key),
                              "live": settings.instance(*key.split("/", 1)) is not None,
                              "link_missing": pub.link not in settings.link_targets})
        instances = [i.key for srv in settings.servers.values() for i in srv.instances.values()]
        return _TEMPLATES.TemplateResponse(request, "pullers.html", {
            "pullers": rows, "links": sorted(settings.links), "pullers_file": settings.pullers_file,
            "keep_hours": PULLER_KEEP_HOURS, "max_file_mib": PULLER_FILE_MIB,
            "publications": published, "publications_file": settings.publications_file,
            "instances": [k for k in instances if k not in settings.publications],
            "link_targets": sorted(settings.link_targets), "commands": PUBLISHED_COMMANDS,
            "connections": [{**c.to_dict(), "name": c.name, "key": "", "has_key": bool(c.key),
                             "status": consumers.conn(c.name).status,
                             "link_missing": c.link not in settings.link_targets}
                            for c in settings.connections.values()],
            "connections_file": settings.connections_file})

    @app.post("/api/pullers/save", dependencies=[Depends(admin)])
    async def pullers_save(payload: dict = Body(...)):
        """Create a puller (new: true) or change it."""
        name, new = str(payload.get("name") or "").strip(), bool(payload.get("new"))
        async with pullers_lock:
            if new and name in settings.pullers:
                return refuse(f"A puller named {name} already exists")
            if not new and name not in settings.pullers:
                return refuse(f"No puller {name}", status.HTTP_404_NOT_FOUND)
            try:
                found = parse_puller(name, {k: payload.get(k) for k in
                                            ("producer", "consumers", "keep_hours", "max_file_mib")
                                            if payload.get(k) not in (None, "")})
            except ConfigError as exc:
                return refuse(str(exc))
            unknown = [n for n in [found.producer, *found.consumers] if n not in settings.links]
            if unknown:
                return refuse(f"No incoming link {', '.join(unknown)}: create it on the Users page")
            changed = {**settings.pullers, name: found}
            try:
                await asyncio.to_thread(save_pullers, settings.pullers_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.pullers_file}: {exc}", 500)
            settings.pullers = changed
            return {"ok": True}

    @app.post("/api/pullers/delete", dependencies=[Depends(admin)])
    async def pullers_delete(payload: dict = Body(...)):
        """A puller goes, with its commands, reports and files."""
        name = str(payload.get("name") or "")
        async with pullers_lock:
            if name not in settings.pullers:
                return refuse(f"No puller {name}", status.HTTP_404_NOT_FOUND)
            changed = {k: v for k, v in settings.pullers.items() if k != name}
            try:
                await asyncio.to_thread(save_pullers, settings.pullers_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.pullers_file}: {exc}", 500)
            settings.pullers = changed
            pullers.forget(name)
            return {"ok": True}

    # --- instances of this one published on a relay's puller -------------

    @app.post("/api/publications/key", dependencies=[Depends(admin)])
    async def publication_new_key():
        """A new random key, for the publication and its consumers."""
        return {"key": new_key()}

    @app.post("/api/publications/save", dependencies=[Depends(admin)])
    async def publication_save(payload: dict = Body(...)):
        """Publish an instance (new: true) or change its publication; a blank
        key keeps the one saved, remove_key drops it."""
        key, new = str(payload.get("instance") or "").strip(), bool(payload.get("new"))
        async with pullers_lock:
            if new and key in settings.publications:
                return refuse(f"{key} is already published")
            if not new and key not in settings.publications:
                return refuse(f"{key} is not published", status.HTTP_404_NOT_FOUND)
            if not any(key in (i.key for i in srv.instances.values()) for srv in settings.servers.values()):
                return refuse(f"No instance {key}")
            data = {k: payload.get(k) for k in ("link", "puller", "key", "commands")}
            if payload.get("remove_key"):
                data["key"] = ""
            elif not str(data["key"] or "").strip() and not new:
                data["key"] = settings.publications[key].key
            try:
                pub = parse_publication(key, data)
            except ConfigError as exc:
                return refuse(str(exc))
            if pub.link not in settings.link_targets:
                return refuse(f"No link {pub.link}: add the relay on the Links page")
            changed = {**settings.publications, key: pub}
            try:
                await asyncio.to_thread(save_publications, settings.publications_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.publications_file}: {exc}", 500)
            settings.publications = changed
            publications.reload()
        risky = [a for a in ("start", "stop", "restart") if pub.exposed(a).enabled]
        warning = (f"{', '.join(risky)} exposed without a key: whoever controls the relay can send them"
                   if risky and not pub.key else "")
        return {"ok": True, "warning": warning}

    @app.post("/api/publications/delete", dependencies=[Depends(admin)])
    async def publication_delete(payload: dict = Body(...)):
        key = str(payload.get("instance") or "")
        async with pullers_lock:
            if key not in settings.publications:
                return refuse(f"{key} is not published", status.HTTP_404_NOT_FOUND)
            changed = {k: v for k, v in settings.publications.items() if k != key}
            try:
                await asyncio.to_thread(save_publications, settings.publications_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.publications_file}: {exc}", 500)
            settings.publications = changed
            publications.reload()
        return {"ok": True}

    # --- puller connections: instances operated through a relay ---------

    def a_connection(name: str):
        try:
            return consumers.conn(name)
        except consumer.ConsumerError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @app.post("/api/connections/save", dependencies=[Depends(admin)])
    async def connection_save(payload: dict = Body(...)):
        """Create a puller connection (new: true) or change it; a blank key
        keeps the one saved, remove_key drops it."""
        name, new = str(payload.get("name") or "").strip(), bool(payload.get("new"))
        async with pullers_lock:
            if new and name in settings.connections:
                return refuse(f"A puller connection named {name} already exists")
            if not new and name not in settings.connections:
                return refuse(f"No puller connection {name}", status.HTTP_404_NOT_FOUND)
            data = {k: payload.get(k) for k in ("link", "puller", "key")}
            if payload.get("remove_key"):
                data["key"] = ""
            elif not str(data["key"] or "").strip() and not new:
                data["key"] = settings.connections[name].key
            try:
                found = parse_connection(name, data)
            except ConfigError as exc:
                return refuse(str(exc))
            if found.link not in settings.link_targets:
                return refuse(f"No link {found.link}: add the relay on the Links page")
            changed = {**settings.connections, name: found}
            try:
                await asyncio.to_thread(save_connections, settings.connections_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.connections_file}: {exc}", 500)
            settings.connections = changed
            consumers.reload()
        return {"ok": True}

    @app.post("/api/connections/delete", dependencies=[Depends(admin)])
    async def connection_delete(payload: dict = Body(...)):
        """A connection goes; what it received stays on disk until purged."""
        name = str(payload.get("name") or "")
        async with pullers_lock:
            if name not in settings.connections:
                return refuse(f"No puller connection {name}", status.HTTP_404_NOT_FOUND)
            changed = {k: v for k, v in settings.connections.items() if k != name}
            try:
                await asyncio.to_thread(save_connections, settings.connections_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.connections_file}: {exc}", 500)
            settings.connections = changed
            consumers.reload()
        return {"ok": True}

    @app.get("/pullers/connections/{name}")
    async def connection_page(request: Request, name: str):
        """The instance a puller connection reaches: its state, disks, load,
        commands and jobs, as its reports told them."""
        run = a_connection(name)
        return _TEMPLATES.TemplateResponse(request, "connection.html", {
            "name": name, "connection": run.connection, "commands": PUBLISHED_COMMANDS})

    @app.get("/api/connections/{name}")
    async def connection_json(name: str):
        run = a_connection(name)
        return {"name": name, "puller": run.connection.puller, "link": run.connection.link,
                "has_key": bool(run.connection.key), "status": run.status, "relay": run.relay,
                "latest": run.latest, "disk_history": run.disk_history,
                "jobs": sorted(({k: v for k, v in j.items() if k not in ("lines", "dropped", "next")}
                                for j in run.jobs.values()), key=lambda j: j.get("created") or "", reverse=True),
                "sent": sorted(run.sent.values(), key=lambda c: c["at"], reverse=True)}

    @app.get("/api/connections/{name}/metrics")
    async def connection_metrics(name: str, since: float = 0):
        """The samples received after since: the last hour from memory, older from the files."""
        run = a_connection(name)
        if since >= time.time() - 3600:
            return {"machine": [p for p in run.machine if p["t"] > since],
                    "instance": [p for p in run.instance if p["t"] > since]}
        found = await asyncio.to_thread(run.metrics, since)
        return {"machine": [i["p"] for i in found if i["s"] == "m"],
                "instance": [i["p"] for i in found if i["s"] == "i"]}

    @app.get("/api/connections/{name}/jobs/{job_id}")
    async def connection_job(name: str, job_id: str, since: int = 0):
        try:
            return a_connection(name).job_lines(job_id, since)
        except consumer.ConsumerError as exc:
            return refuse(str(exc), status.HTTP_404_NOT_FOUND)

    @app.post("/api/connections/{name}/commands")
    async def connection_command(name: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A command to the instance there: backup (developer), start, stop and
        restart (every role, as here), stop-job (admin, as here)."""
        run = a_connection(name)
        action = str(payload.get("action") or "")
        params = payload.get("params") or {}
        needed = {"backup": "developer", "stop-job": "admin"}.get(action, "user")
        if action not in (*PUBLISHED_COMMANDS, "stop-job") or not isinstance(params, dict):
            return refuse(f"{action!r} is not a command of a puller")
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here sends commands")
        if not me.can(needed):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {needed} role is required")
        if action == "backup":
            fields = (run.latest.get("backup_fields") or [])
            params = {f["key"]: params.get(f["key"]) for f in fields if isinstance(f, dict) and f.get("key") in params}
        elif action == "stop-job":
            params = {"job": str(params.get("job") or "")}
        else:
            params = {}
        try:
            return await run.send(action, params, me.name)
        except consumer.ConsumerError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)

    # --- options of the system ------------------------------------------

    @app.get("/options", dependencies=[Depends(admin)])
    async def options_page(request: Request):
        return _TEMPLATES.TemplateResponse(request, "options.html", {
            "options": settings.options, "options_file": settings.options_file,
            "font_sizes": SHELL_FONT_SIZES, "idle_hours": TUNNEL_IDLE_HOURS})

    @app.post("/api/options/save", dependencies=[Depends(admin)])
    async def options_save(payload: dict = Body(...)):
        try:
            options = parse_options(payload)
        except ConfigError as exc:
            return refuse(str(exc))
        try:
            await asyncio.to_thread(save_options, settings.options_file, options)
        except OSError as exc:
            return refuse(f"Cannot write {settings.options_file}: {exc}", 500)
        settings.options = options
        return {"ok": True}

    # --- scheduled actions ----------------------------------------------

    schedules_lock = asyncio.Lock()

    def schedule_view(sch: schedules.Schedule) -> dict:
        try:
            upcoming = [t.isoformat(sep=" ", timespec="minutes") for t in schedules.next_runs(sch.cron)]
        except (ValueError, KeyError):
            upcoming = []
        return {"name": sch.name, "data": schedules.schedule_to_dict(sch), "steps": sch.steps,
                "user": sch.acting_user, "next": upcoming if sch.enabled else [],
                "live": scheduler.instance_of(sch) is not None,
                "runs": scheduler.history.runs(sch.name, jobs)}

    @app.get("/schedules", dependencies=[Depends(admin)])
    async def schedules_page(request: Request):
        targets = {}
        for srv in settings.servers.values():
            plugin = plugins.registry().get(srv.type)
            for inst in srv.instances.values():
                src = inst.from_link
                targets[inst.key] = {
                    "live": srv.active,
                    "type": srv.type,
                    "has_restore": bool(plugin and plugin.has_restore),
                    "backup_fields": [_field_dict(f) for f in plugin.backup_fields()] if plugin else [],
                    "restore_fields": [_field_dict(f) for f in plugin.restore_fields()] if plugin else [],
                    "from_link": src.to_dict() if src else None,
                }
        return _TEMPLATES.TemplateResponse(
            request, "schedules.html",
            {"schedules": [schedule_view(s) for s in scheduler.schedules.values()], "targets": targets,
             "links": list(settings.link_targets), "connections": list(settings.connections),
             "scripts": schedules.list_scripts(settings.scripts_dir),
             "scripts_dir": settings.scripts_dir, "schedules_file": settings.schedules_file,
             "load_error": schedules_error, "default_user": schedules.DEFAULT_USER},
        )

    @app.get("/api/schedules/next", dependencies=[Depends(admin)])
    async def schedules_next(cron: str = ""):
        """The next runs of a crontab expression, for the form."""
        try:
            expr = schedules.check_cron(cron, "cron")
        except ConfigError as exc:
            return refuse(str(exc).removeprefix("cron: "))
        return {"cron": expr, "next": [t.isoformat(sep=" ", timespec="minutes")
                                       for t in schedules.next_runs(expr, 5)]}

    @app.post("/api/schedules/save", dependencies=[Depends(admin)])
    async def schedules_save(payload: dict = Body(...)):
        nonlocal schedules_error
        original = payload.get("original") or None
        name = str(payload.get("name") or "").strip()
        data = dict(payload.get("schedule") or {})
        async with schedules_lock:
            current = scheduler.schedules
            if original is not None and original not in current:
                return refuse(f"Schedule {original} no longer exists", status.HTTP_404_NOT_FOUND)
            if name != original and name in current:
                return refuse(f"A schedule named {name} already exists")
            server, _, role = str(data.get("instance") or "").partition("/")
            srv = settings.servers.get(server)
            if srv is None or role not in srv.instances:
                return refuse(f"No instance {data.get('instance')}")
            instance = srv.instances[role]
            copy = data.get("copy")
            if copy:
                if not instance.plugin.has_restore:
                    return refuse(f"{instance.plugin.label} cannot restore a database: no copy from link")
                # the choices as the instance's plugin asks them
                try:
                    copy["backup_options"] = {f.key: _field_value(f, (copy.get("backup_options") or {}).get(f.key))
                                              for f in instance.plugin.backup_fields()}
                    copy["restore_options"] = {
                        f.key: _field_value(f, (copy.get("restore_options") or {}).get(f.key))
                        for f in instance.plugin.restore_fields()}
                except ActionRefused as exc:
                    return refuse(str(exc))
            try:
                parsed = schedules.parse_schedule(name, data)
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            updated = {}
            for key, sch in current.items():
                # an edited schedule keeps its place in the list
                updated[name if key == original else key] = parsed if key == original else sch
            updated.setdefault(name, parsed)
            try:
                await asyncio.to_thread(schedules.save_schedules, settings.schedules_file, updated)
            except OSError as exc:
                return refuse(f"Cannot write {settings.schedules_file}: {exc}", 500)
            if original and original != name:
                scheduler.history.rename(original, name)
            scheduler.schedules = updated
            schedules_error = ""
        return {"ok": True}

    @app.post("/api/schedules/delete", dependencies=[Depends(admin)])
    async def schedules_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with schedules_lock:
            if name not in scheduler.schedules:
                return refuse(f"No schedule {name}", status.HTTP_404_NOT_FOUND)
            updated = {k: v for k, v in scheduler.schedules.items() if k != name}
            try:
                await asyncio.to_thread(schedules.save_schedules, settings.schedules_file, updated)
            except OSError as exc:
                return refuse(f"Cannot write {settings.schedules_file}: {exc}", 500)
            scheduler.schedules = updated
            scheduler.history.keep_only(updated)
        return {"ok": True}

    @app.post("/api/schedules/{name}/run")
    async def schedules_run(name: str, user: Principal = Depends(admin)):
        """Run the schedule now, enabled or not."""
        sch = scheduler.schedules.get(name)
        if sch is None:
            return refuse(f"No schedule {name}", status.HTTP_404_NOT_FOUND)
        job = scheduler.fire(sch, f"manual: {user}")
        if job is None:
            return refuse(scheduler.history.data[name][0]["message"])
        return {"job": job.id}

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
            {"name": me.name, "role": WORKSPACE_ROLE if me.scoped else me.role, "workspaces": me.workspaces,
             "fixed": me.name in settings.fixed.users},
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
