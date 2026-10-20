"""The OmniaCI web application."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import ops
from .auth import Authenticator
from .config import Instance, Settings, load_settings
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
        if request.method == "POST" and not _same_origin(request):
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

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniaci.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACI_CONFIG", "omniaci.yaml")))
