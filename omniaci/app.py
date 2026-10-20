"""The OmniaCI web application."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import checks, ops
from .auth import Authenticator
from .config import (
    ConfigError,
    Instance,
    Settings,
    load_settings,
    parse_servers,
    save_servers,
    server_to_dict,
)
from .jobs import JobManager
from .logtail import read_since
from .metrics import Sampler
from .scheduler import AutoUpdater

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# The OmniaSolutions "O" (Client2019 Setup/dist/images/omnia.ico, 355x280,
# centred on a transparent square); served without login, like any favicon.
_STATIC = Path(__file__).parent / "static"
_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")
_ICONS = {"/favicon.ico": ("favicon.ico", "image/x-icon"),
          "/icon-180.png": ("icon-180.png", "image/png")}
_basic = HTTPBasic(realm="OmniaCI")


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts: the browser sends Basic credentials anyway."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlsplit(source).netloc == request.headers.get("host")


def create_app(settings: Settings) -> FastAPI:
    authenticator = Authenticator(settings.users)
    jobs = JobManager(settings.jobs_dir)
    updater = AutoUpdater(settings, jobs)
    sampler = Sampler(settings)

    def current_user(
        request: Request, credentials: HTTPBasicCredentials = Depends(_basic)
    ) -> str:
        if not authenticator.check(credentials.username, credentials.password):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="OmniaCI"'},
            )
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")
        return credentials.username

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        updater.start()
        sampler.start()
        yield
        await sampler.stop()
        await updater.stop()

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

    def get_instance(server: str, role: str) -> Instance:
        instance = settings.instance(server, role)
        if instance is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{role}")
        return instance

    def started(job) -> RedirectResponse:
        return RedirectResponse(f"/jobs/{job.id}", status.HTTP_303_SEE_OTHER)

    def active_server(name: str):
        srv = settings.servers.get(name)
        if srv is None or not srv.active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No active server {name}")
        return srv

    @app.get("/")
    async def index(request: Request):
        instances = settings.all_instances()
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "servers": settings.active_servers(),
                "drafts": [s for s in settings.servers.values() if not s.active],
                "busy": {i.key: jobs.is_busy(i.key) for i in instances},
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
                "jobs": [j for j in jobs.recent(200) if any(k.startswith(f"{server}/") for k in j.instances)][:10],
            },
        )

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
    async def action_pull(server: str, role: str, user: str = Depends(current_user)):
        instance = get_instance(server, role)
        return started(
            jobs.submit(
                f"Git pull {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.pull(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/pull-restart")
    async def action_pull_restart(server: str, role: str, user: str = Depends(current_user)):
        instance = get_instance(server, role)
        return started(
            jobs.submit(
                f"Git pull and restart {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.pull_restart(job, instance),
            )
        )

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
        server: str, role: str, payload: dict = Body(...), user: str = Depends(current_user)
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
    async def action_restart(server: str, role: str, user: str = Depends(current_user)):
        instance = get_instance(server, role)
        return started(
            jobs.submit(
                f"Restart {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.restart(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/backup")
    async def action_backup(
        server: str, role: str, download: bool = False, user: str = Depends(current_user)
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
        return RedirectResponse(
            f"/jobs/{job.id}" + ("?download=1" if download else ""), status.HTTP_303_SEE_OTHER
        )

    def backup_folder(instance: Instance) -> Path:
        return settings.backup_dir / instance.server / instance.role

    @app.get("/api/servers/{server}/{role}/backups")
    async def backup_list(server: str, role: str):
        instance = get_instance(server, role)
        folder = backup_folder(instance)
        files = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if folder.is_dir() else []
        return [
            {
                "file": p.name,
                "size": p.stat().st_size,
                "time": int(p.stat().st_mtime),
                "download": f"/backups/{server}/{role}/{p.name}",
            }
            for p in files
        ]

    @app.get("/backups/{server}/{role}/{name}")
    async def backup_download(server: str, role: str, name: str):
        instance = get_instance(server, role)
        folder = backup_folder(instance).resolve()
        path = (folder / name).resolve()
        # only a zip directly in this instance's backup folder
        if not _BACKUP_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such backup")
        return FileResponse(path, media_type="application/zip", filename=name)

    @app.post("/servers/{server}/merge")
    async def action_merge(server: str, user: str = Depends(current_user)):
        srv = settings.servers.get(server)
        if srv is None or not srv.active or srv.production is None or srv.test is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No server {server} with production and test"
            )
        return started(
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
    async def job_list(request: Request):
        return _TEMPLATES.TemplateResponse(request, "jobs.html", {"jobs": jobs.recent(200)})

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
                },
            },
        )

    def configured(name: str):
        srv = settings.servers.get(name)
        if srv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No server {name}")
        return srv

    @app.get("/config")
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

    @app.get("/config/new")
    async def config_new(request: Request):
        return server_form(request, "New server", None, "", {})

    @app.get("/config/{name}/edit")
    async def config_edit(request: Request, name: str):
        srv = configured(name)
        return server_form(request, f"Edit {name}", name, name, server_to_dict(srv))

    @app.get("/config/{name}/clone")
    async def config_clone(request: Request, name: str):
        srv = configured(name)
        new_name = f"{name}_copy"
        while new_name in settings.servers:
            new_name += "_copy"
        return server_form(request, f"Clone of {name}", None, new_name, server_to_dict(srv))

    def refuse(message: str, code: int = status.HTTP_400_BAD_REQUEST):
        return JSONResponse({"error": message}, status_code=code)

    @app.post("/api/servers/save")
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
                parsed = parse_servers({name: server_data})[name]
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

    @app.post("/api/servers/{name}/test")
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

    @app.post("/api/servers/{name}/deactivate")
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

    @app.post("/api/servers/delete")
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

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniaci.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACI_CONFIG", "omniaci.yaml")))
