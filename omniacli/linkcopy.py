# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A backup made by a linked OmniaCLI, brought here: the backup is started
there (or one already there is chosen), its job followed, and the zip
downloaded into a local folder, through the link's token and with the role of
whoever acts here (at most).

It uses what every OmniaCLI with links answers: POST /servers/<s>/<r>/backup
(a 303 to its job), GET /api/jobs/<id> (status, log, result.file) and GET
/backups/<s>/<r>/<file>."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

import httpx

from .config import LinkSource, LinkTarget
from .jobs import Job, JobFailed
from .links import describe_error

POLL_EVERY = 2.0  # seconds between two looks at the job there
CHUNK = 1 << 20
BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")
_FINISHED = ("success", "failed", "stopped", "interrupted")


def _said(resp: httpx.Response) -> str:
    """What the answer says (read): an OmniaCLI's detail or error, else the
    start of its text (a proxy's page)."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("detail") or data.get("error"), str):
        return data.get("detail") or data.get("error")
    text = " ".join(resp.text.split())
    return text[:200] + ("..." if len(text) > 200 else "")


def _refused(link: LinkTarget, resp: httpx.Response, what: str) -> JobFailed:
    said = _said(resp)
    said = f": {said}" if said else ""
    if resp.status_code == 401:
        return JobFailed(f"{link.name} refused the link's token: ask it for a new one{said}")
    if resp.status_code == 403:
        return JobFailed(f"{link.name} refused {what}: the developer role is needed there, for the link "
                         f"and for you{said}")
    if resp.status_code == 404:
        return JobFailed(f"{link.name} does not know {what} (404){said}")
    return JobFailed(f"{link.name} answered HTTP {resp.status_code} to {what}{said}")


async def _remote_backup(job: Job, client: httpx.AsyncClient, link: LinkTarget, headers: dict,
                         src: LinkSource, options: dict) -> str:
    """Back up src there with the choices of options (posted as its Backup
    form posts them: a checkbox off is left out), follow its job; the name of
    the zip made."""
    where = f"{src.server}/{src.role}"
    form = {k: "on" if v is True else str(v) for k, v in options.items() if v not in (False, None, "")}
    job.log(f"--- backup of {where} on {link.name}" + (f" ({', '.join(form)})" if form else ""))
    try:
        resp = await client.post(f"/servers/{where}/backup", headers=headers, data=form or None)
    except httpx.HTTPError as exc:
        raise JobFailed(f"{link.name}: {describe_error(exc)}") from exc
    if resp.status_code != 303:
        raise _refused(link, resp, f"the backup of {where}")
    remote_id = resp.headers.get("location", "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    seen = 0
    try:
        while True:
            try:
                resp = await client.get(f"/api/jobs/{remote_id}", params={"since": seen}, headers=headers)
            except httpx.HTTPError as exc:
                raise JobFailed(f"{link.name}: {describe_error(exc)} (its job {remote_id} goes on)") from exc
            if resp.status_code != 200:
                raise _refused(link, resp, f"the job {remote_id}")
            data = resp.json()
            for line in data.get("lines", []):
                job.log(f"  {link.name}: {line}")
            seen = data.get("next", seen)
            if data.get("status") in _FINISHED:
                break
            await asyncio.sleep(POLL_EVERY)
    except asyncio.CancelledError:
        job.log(f"--- the backup on {link.name} (its job {remote_id}) is not stopped there")
        raise
    if data["status"] != "success":
        raise JobFailed(f"the backup on {link.name} {data['status']}: {data.get('error') or 'no reason given'}")
    name = str((data.get("result") or {}).get("file") or "")
    if not BACKUP_NAME.fullmatch(name):
        raise JobFailed(f"the backup on {link.name} names no zip (job {remote_id})")
    return name


async def _download(job: Job, client: httpx.AsyncClient, link: LinkTarget, headers: dict,
                    src: LinkSource, name: str, dest: Path, progress: dict) -> None:
    """The zip there into dest, through dest.part: dest is whole or absent.
    progress gets done and total (bytes) as it goes: the job page draws it."""
    part = dest.with_name(dest.name + ".part")
    job.log(f"--- download {name} from {link.name} to {dest}")
    started, size = time.monotonic(), 0
    try:
        async with client.stream("GET", f"/backups/{src.server}/{src.role}/{name}", headers=headers) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise _refused(link, resp, f"the backup {name} of {src.server}/{src.role}")
            length = int(resp.headers.get("content-length") or 0)
            # since: the page's first look gets a speed
            progress.update(done=0, total=length, since=time.time())
            free = shutil.disk_usage(dest.parent).free
            if length and length > free:
                raise JobFailed(f"{name} is {length >> 20} MiB, {dest.parent} has {free >> 20} MiB free")
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(CHUNK):
                    fh.write(chunk)
                    size += len(chunk)
                    progress["done"] = size
        if length and size != length:
            raise JobFailed(f"{name}: {size} of {length} bytes downloaded")
        part.replace(dest)
    except httpx.HTTPError as exc:
        raise JobFailed(f"{link.name}: {describe_error(exc)} while downloading {name}") from exc
    finally:
        part.unlink(missing_ok=True)
    job.log(f"{size >> 20} MiB in {time.monotonic() - started:.0f} s")


async def fetch_backup(job: Job, client: httpx.AsyncClient, link: LinkTarget, headers: dict,
                       src: LinkSource, backup: str, folder: Path, progress: dict | None = None,
                       options: dict | None = None) -> Path:
    """A new backup of src there (backup empty), with the choices of options,
    or its backup named backup, downloaded into folder as <link>_<name>; the
    local file. progress, when given, follows the steps: step "backup", then
    "download" with its bytes."""
    progress = {} if progress is None else progress
    progress["step"] = "backup"
    name = backup or await _remote_backup(job, client, link, headers, src, options or {})
    if not BACKUP_NAME.fullmatch(name):
        raise JobFailed(f"{name!r} is not the name of a backup zip")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{link.name}_{name}"
    progress["step"] = "download"
    await _download(job, client, link, headers, src, name, dest, progress)
    return dest
