# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A workspace's instance created from a template, as a job: the steps that
go through the root helpers (workspacehelper.py, composehelper.py)."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

from . import config, stacktemplates, templatestore, workspacehelper
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


def storage_path(ws: Workspace) -> Path:
    """The root path the workspace's storage is bound on: what root uses, so
    what OmniaCLI reads and writes too (README, Workspaces)."""
    return Path(config.bound_root(ws.storage))


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


def _limit(ws: Workspace, key: str) -> str:
    return f"{ws.limits.get(key) or 0:g}"


async def apply_limits(job: Job, ws: Workspace) -> dict:
    """The workspace's CPU and memory: its systemd slice, written again;
    the containers running in it take the new limits."""
    cpus, memory = _limit(ws, "cpus"), _limit(ws, "memory_gb")
    job.log(f"--- the CPU and memory of workspace {ws.name}: "
            + (f"{cpus} cores" if cpus != "0" else "CPU not limited") + ", "
            + (f"{memory} GB" if memory != "0" else "memory not limited"))
    return await _helper(job, "limits", ws.name, cpus, memory)


async def create_instance(job: Job, ws: Workspace, instance: str, template: stacktemplates.Template,
                          values: dict[str, str]) -> dict:
    """The disk, the two projects, their databases, started: the server's
    data for the servers file (production and test, type docker)."""
    if not ws.storage or not ws.limits.get("disk_gb"):
        raise JobFailed(f"workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
    job.log(f"--- the disk of workspace {ws.name} ({ws.limits['disk_gb']:g} GB, in {ws.storage})")
    await _helper(job, "disk", ws.name, ws.storage, str(int(ws.limits["disk_gb"])))
    await apply_limits(job, ws)
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


class SpaceError(Exception):
    """The root helper did not tell the room of the disks."""


# what an admin does when sudo refuses the workspaces' helpers (README, Workspaces)
HELPERS_HINT = ("sudo does not allow the workspaces' helpers to OmniaCLI's user: as root, add "
                "OMNIACLI_WORKSPACES=1 to /etc/default/omniacli, then run dpkg-reconfigure omniacli "
                "(it writes /etc/sudoers.d/omniacli-workspaces and makes the default storage)")


def helpers_refused(message: str) -> bool:
    """sudo's own refusal (no line of the sudoers for the helper), not the helper's."""
    return message.startswith("sudo:") or "is not allowed to run sudo" in message


async def space() -> dict:
    """The root helper's space: each storage root's real disk and the
    workspaces' disks on it, in bytes."""
    code, out = await capture(helper("space"), timeout=HELPER_TIMEOUT)
    if code:
        raise SpaceError(out or f"workspace-helper space failed ({code})")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise SpaceError(f"workspace-helper space answered no JSON: {out}") from None


GB = workspacehelper.GB


def _limit_bytes(ws: Workspace) -> int:
    return int(float(ws.limits.get("disk_gb") or 0) * GB)


def _disks(found: dict, workspaces: dict[str, Workspace], device) -> dict[tuple[str, str], dict]:
    """The workspaces' disks on one real disk, {(root, workspace): {promised,
    used}}: a workspace's limit or its image, the larger (an image no
    workspace names any more still grows up to its size)."""
    roots = {r["root"]: r for r in found.get("roots", []) if not r.get("missing") and r.get("device") == device}
    disks: dict[tuple[str, str], dict] = {}
    for root, info in roots.items():
        for name, image in info.get("images", {}).items():
            disks[(root, name)] = {"promised": image["size"], "used": image["used"]}
    for ws in workspaces.values():
        if ws.storage in roots:
            entry = disks.setdefault((ws.storage, ws.name), {"promised": 0, "used": 0})
            entry["promised"] = max(entry["promised"], _limit_bytes(ws))
    return disks


def room(found: dict, workspaces: dict[str, Workspace], storage: str, name: str | None) -> dict | None:
    """What workspace name (None: a new one) may have on storage, in bytes:
    the room free on the real disk, what the other workspaces' disks there
    were promised and do not take yet (reserved), and so the largest disk
    without taking it from them (allowed) and the largest that fits now
    (physical, an admin's choice: README, Workspaces). None: storage unknown
    to the helper or missing."""
    info = next((r for r in found.get("roots", []) if r.get("root") == storage), None)
    if info is None or info.get("missing"):
        return None
    disks = _disks(found, workspaces, info["device"])
    own = disks.get((storage, name), {"promised": 0, "used": 0}) if name else {"promised": 0, "used": 0}
    others = {key: d for key, d in disks.items() if key != (storage, name)}
    reserved = sum(max(d["promised"] - d["used"], 0) for d in others.values())
    image = info.get("images", {}).get(name, {}) if name else {}
    return {"total": info["total"], "free": info["free"], "reserved": reserved,
            "promised": sum(d["promised"] for d in disks.values()), "used": sum(d["used"] for d in disks.values()),
            "own_used": own["used"], "own_size": image.get("size", 0),
            "allowed": max(info["free"] + own["used"] - reserved, 0), "physical": info["free"] + own["used"]}


def disk_problem(found: dict | None, workspaces: dict[str, Workspace], ws: Workspace, force: bool) -> str | None:
    """Why ws's disk limit may not be saved, or None. Without force, the disk
    may not take what the other workspaces on the same real disk were
    promised; with force, it must still fit in the room free now."""
    limit = _limit_bytes(ws)
    if not limit or not ws.storage:
        return None
    if found is None:
        return None if force else "the room of the disks cannot be read: force it to save anyway"
    r = room(found, workspaces, ws.storage, ws.name)
    if r is None:
        return None if force else f"{ws.storage} is not there yet: force it to save anyway"
    gb = lambda value: f"{value / GB:.1f} GB"  # noqa: E731
    if limit < r["own_size"]:
        return f"the disk of {ws.name} is {gb(r['own_size'])} already: a disk is not made smaller"
    if limit > r["physical"]:
        return (f"{gb(limit)} do not fit on the real disk of {ws.storage}: {gb(r['physical'])} at most "
                f"({gb(r['free'])} free" + (f", {gb(r['own_used'])} taken by {ws.name}" if r["own_used"] else "") + ")")
    if limit > r["allowed"] and not force:
        return (f"{gb(limit)} take room promised to other workspaces on the real disk of {ws.storage}: "
                f"{gb(r['allowed'])} without it ({gb(r['free'])} free, {gb(r['reserved'])} promised and not used yet); "
                f"force it to promise more than there is")
    return None


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
    return storage_path(ws) / ws.name / instance


# a customer's repository: https only, no user or token in it (they are the
# interface's git credentials), no query
REPOSITORY_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?/[A-Za-z0-9._~/-]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
CLONE_TIMEOUT = 1800


def check_repository(name: str, url: str, branch: str) -> None:
    if not workspacehelper.REPOSITORY.fullmatch(name):
        raise JobFailed(f"{name!r} is not a repository name (a-z A-Z 0-9 . _ -)")
    if not REPOSITORY_URL.fullmatch(url) or ".." in url:
        raise JobFailed(f"{url!r} is not an https:// address of a repository (no user or token in it: "
                        "those are the workspace's git credentials)")
    if not BRANCH.fullmatch(branch) or branch.startswith(("-", "/")) or ".." in branch:
        raise JobFailed(f"{branch!r} is not a branch name")


def addons_folder(ws: Workspace, instance: str, role: str) -> Path:
    return storage_path(ws) / ws.name / instance / role / "addons"


def _remove_tree(path: Path) -> None:
    """A clone of OmniaCLI's user, its read-only folders and objects included."""
    def retry(func, target, _exc):
        for folder in (os.path.dirname(target), target):
            if os.path.isdir(folder) and not os.path.islink(folder):
                os.chmod(folder, stat.S_IRWXU)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


async def clone(job: Job, ws: Workspace, instance: str, role: str, name: str, url: str, branch: str,
                git_options: tuple = ()) -> Path:
    """The customer's repository cloned, with its submodules, into the role's
    addons/ (OmniaCLI's own folder there), with the server's git credentials."""
    check_repository(name, url, branch)
    target = addons_folder(ws, instance, role) / name
    if target.exists() or target.is_symlink():
        raise JobFailed(f"{target} exists already")
    job.log(f"--- clone {url} ({branch}) into {target}")
    try:
        await run(job, ["git", *git_options, "clone", "--branch", branch, "--recurse-submodules", "--", url,
                        str(target)], timeout=CLONE_TIMEOUT)
    except BaseException:
        if target.exists():
            _remove_tree(target)
        raise
    return target


async def write_conf(job: Job, ws: Workspace, instance: str, role: str, repositories: list[str]) -> dict:
    """The role's odoo.conf again, its addons path naming these repositories."""
    job.log(f"--- odoo.conf of {role}: the addons of {', '.join(repositories) or 'no repository'}")
    return await _helper(job, "conf", ws.name, ws.storage, instance, role, json.dumps({"repositories": repositories}))


def remove_clone(path: Path) -> None:
    if path.exists():
        _remove_tree(path)
