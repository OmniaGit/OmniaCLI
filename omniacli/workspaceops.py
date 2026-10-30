# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A workspace's instance created from a template, as a job: the steps that
go through the root helpers (workspacehelper.py, composehelper.py)."""

from __future__ import annotations

import json
from pathlib import Path

from . import stacktemplates, templatestore, workspacehelper
from .config import OMNIACLI_COMMAND, Workspace
from .jobs import Job, JobFailed
from .ops import capture, run

# the disk of a workspace may take a while to be made (mkfs), a database
# minutes to be created (the modules installed)
HELPER_TIMEOUT = 300
INIT_TIMEOUT = 1800


def helper(*args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "workspace-helper", *args]


def compose(project: str, *args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "compose-helper", project, *args]


def templates() -> dict[str, stacktemplates.Template]:
    """The templates the root helper creates instances from."""
    return stacktemplates.available([workspacehelper.TEMPLATES, templatestore.INSTALLED])


def server_name(ws: Workspace, instance: str) -> str:
    return f"{ws.name}-{instance}"


async def _helper(job: Job, *args: str) -> dict:
    job.log("$ " + " ".join(helper(*args)))
    code, out = await capture(helper(*args), timeout=HELPER_TIMEOUT)
    if code:
        raise JobFailed(out or f"workspace-helper {args[0]} failed ({code})")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise JobFailed(f"workspace-helper {args[0]} answered no JSON: {out}") from None


async def create_instance(job: Job, ws: Workspace, instance: str, template: stacktemplates.Template,
                          values: dict[str, str]) -> dict:
    """The disk, the two projects, their databases, started: the server's
    data for the servers file (production and test, type docker)."""
    if not ws.storage or not ws.limits.get("disk_gb"):
        raise JobFailed(f"workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
    job.log(f"--- the disk of workspace {ws.name} ({ws.limits['disk_gb']:g} GB, in {ws.storage})")
    await _helper(job, "disk", ws.name, ws.storage, str(int(ws.limits["disk_gb"])))
    job.log(f"--- instance {instance} from template {template.name} {template.version}")
    made = await _helper(job, "create", ws.name, ws.storage, instance, template.name, json.dumps(values))
    try:
        modules = values.get("INIT_MODULES") or "base"
        language = values.get("INIT_LANG") or ""
        for role, info in made["roles"].items():
            job.log(f"--- {role}: database {made['database']} with {modules}")
            await run(job, compose(info["folder"], "init", made["odoo_service"], made["database"], modules,
                                   *([language] if language else [])), timeout=INIT_TIMEOUT)
            await run(job, compose(info["folder"], "start"), timeout=HELPER_TIMEOUT)
    except BaseException:
        job.log(f"--- the creation failed: instance {instance} removed")
        await run(job, helper("remove", ws.name, ws.storage, instance), check=False, timeout=HELPER_TIMEOUT)
        raise
    job.log("The new databases have the user admin with password admin: change it at the first login.")
    return {
        "type": "docker",
        **{role: {"database": made["database"], "odoo_service": made["odoo_service"],
                  "db_service": made["db_service"], "http_port": info["http_port"], "update_modules": True,
                  "restart": {"type": "compose", "service": info["folder"], "sudo": True}, "repositories": []}
           for role, info in made["roles"].items()},
    }


async def remove_instance(job: Job, ws: Workspace, instance: str) -> None:
    await _helper(job, "remove", ws.name, ws.storage, instance)


def web_spec(ws: Workspace) -> str:
    """The names of the workspace for the root helper (it reads the ports)."""
    return json.dumps({"domain": ws.domain, "main": ws.main, "subdomains": ws.subdomains,
                       "certificate": ws.certificate})


async def publish(job: Job, ws: Workspace) -> dict:
    """The workspace's nginx site and its certificate: a certificate that
    fails leaves the site on http (port 80 answers 503 until then)."""
    if not ws.storage:
        job.log(f"--- workspace {ws.name} has no storage: no site")
        return {}
    job.log(f"--- the names of workspace {ws.name}" + (f" under {ws.domain}" if ws.domain else ": none (no domain)"))
    made = await _helper(job, "web", ws.name, ws.storage, web_spec(ws))
    for name in made.get("names", []):
        job.log(("https://" if made.get("https") else "http://") + name)
    job.log(f"nginx: {made.get('nginx')}")
    if made.get("certificate_error"):
        job.log(f"The certificate is not there yet: {made['certificate_error']}")
    return made


async def own_certificate(ws: Workspace, certificate: str, key: str) -> tuple[bool, str]:
    """The customer's certificate to the root helper, on its standard input."""
    if not ws.storage:
        return False, f"workspace {ws.name} has no storage"
    code, out = await capture(helper("owncert", ws.name, ws.storage), timeout=HELPER_TIMEOUT,
                              input=json.dumps({"certificate": certificate, "key": key}).encode())
    return code == 0, out


def instance_folder(ws: Workspace, instance: str) -> Path:
    return Path(ws.storage) / ws.name / instance
