"""The OmniaCI web application."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import ops
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
from .logtail import follow
from .scheduler import AutoUpdater

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
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
        yield
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

    def get_instance(server: str, role: str) -> Instance:
        instance = settings.instance(server, role)
        if instance is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{role}")
        return instance

    def started(job) -> RedirectResponse:
        return RedirectResponse(f"/jobs/{job.id}", status.HTTP_303_SEE_OTHER)

    @app.get("/")
    async def index(request: Request):
        instances = settings.all_instances()
        statuses = await asyncio.gather(
            *(
                asyncio.gather(*(ops.repository_status(r) for r in i.repositories))
                for i in instances
            )
        )
        repo_status = {i.key: s for i, s in zip(instances, statuses)}
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "servers": settings.servers.values(),
                "repo_status": repo_status,
                "auto_state": updater.state,
                "busy": {i.key: jobs.is_busy(i.key) for i in instances},
                "jobs": jobs.recent(10),
            },
        )

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
    async def action_backup(server: str, role: str, user: str = Depends(current_user)):
        instance = get_instance(server, role)
        return started(
            jobs.submit(
                f"Backup {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.backup(job, instance, settings.backup_dir),
            )
        )

    @app.post("/servers/{server}/merge")
    async def action_merge(server: str, user: str = Depends(current_user)):
        srv = settings.servers.get(server)
        if srv is None or srv.production is None or srv.test is None:
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

    @app.get("/servers/{server}/{role}/log/stream")
    async def log_stream(server: str, role: str):
        instance = get_instance(server, role)
        if not os.access(instance.log_file, os.R_OK):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Cannot read {instance.log_file}"
            )

        async def events():
            async for line in follow(instance.log_file):
                yield f"data: {line}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
                "model": {"original": original, "name": name, "server": data},
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
            try:
                parsed = parse_servers({name: payload.get("server") or {}})[name]
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            servers = {}
            for key, srv in settings.servers.items():
                # an edited server keeps its place in the list
                servers[name if key == original else key] = parsed if key == original else srv
            servers.setdefault(name, parsed)
            try:
                save_servers(settings.servers_file, servers)
            except OSError as exc:
                return refuse(f"Cannot write {settings.servers_file}: {exc}", 500)
            settings.servers = servers
            await updater.reload()
        return {"ok": True}

    @app.post("/api/servers/delete")
    async def config_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with config_lock:
            if name not in settings.servers:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            if server_busy(name):
                return refuse(f"A job is running on {name}: try again when it ends")
            servers = {k: v for k, v in settings.servers.items() if k != name}
            try:
                save_servers(settings.servers_file, servers)
            except OSError as exc:
                return refuse(f"Cannot write {settings.servers_file}: {exc}", 500)
            settings.servers = servers
            await updater.reload()
        return {"ok": True}

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniaci.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACI_CONFIG", "omniaci.yaml")))
