"""The actions run on an instance: git pull, module update, restart, backup, merge."""

from __future__ import annotations

import asyncio
import configparser
import datetime
import os
import re
import shlex
from pathlib import Path

from .config import Instance, Repository, Server
from .jobs import Job, JobFailed

# No git command may stop on a credential or passphrase prompt: there is no
# terminal (start_new_session) and git is told not to ask.
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
# Odoo can log very long lines (tracebacks with data, SQL)
_LINE_LIMIT = 4 * 1024 * 1024
RESTART_TIMEOUT = 300


async def run(
    job: Job,
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> int:
    """Run a command, streaming its output into the job log."""
    job.log(f"$ {shlex.join(str(c) for c in cmd)}" + (f"   (in {cwd})" if cwd else ""))
    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            cwd=cwd,
            env=_ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=_LINE_LIMIT,
        )
    except OSError as exc:
        raise JobFailed(f"cannot run {cmd[0]}: {exc}") from exc
    assert proc.stdout is not None

    async def stream() -> int:
        async for raw in proc.stdout:
            job.log(raw.decode(errors="replace").rstrip("\n"))
        return await proc.wait()

    try:
        returncode = await asyncio.wait_for(stream(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise JobFailed(f"{Path(str(cmd[0])).name} did not finish within {timeout:.0f} s")
    if check and returncode:
        raise JobFailed(f"{Path(str(cmd[0])).name} exited with code {returncode}")
    return returncode


async def capture(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run a command quietly and return (exit code, stripped output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            cwd=cwd,
            env=_ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, str(exc)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def git(repo: Repository, *args: str) -> str:
    code, out = await capture(["git", *args], cwd=repo.path)
    if code:
        raise JobFailed(f"[{repo.name}] git {' '.join(args)} failed: {out}")
    return out


async def repository_status(repo: Repository) -> dict:
    """Branch and last commit of a working copy, for the dashboard."""
    if not repo.path.is_dir():
        return {"error": f"{repo.path} does not exist"}
    code, out = await capture(
        ["git", "log", "-1", "--format=%h%x00%cd%x00%s", "--date=format:%Y-%m-%d %H:%M"],
        cwd=repo.path,
    )
    if code:
        return {"error": out}
    sha, date, subject = (out.split("\0") + ["", ""])[:3]
    _, branch = await capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo.path)
    return {"sha": sha, "date": date, "subject": subject, "branch": branch}


async def ensure_ready(job: Job, repo: Repository) -> None:
    """Fail unless the working copy is on its branch with no local changes."""
    branch = await git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != repo.branch:
        raise JobFailed(
            f"[{repo.name}] {repo.path} is on branch '{branch}', expected '{repo.branch}'"
        )
    changes = await git(repo, "status", "--porcelain", "--untracked-files=no")
    if changes:
        job.log(changes)
        raise JobFailed(f"[{repo.name}] {repo.path} has uncommitted changes")


async def pull(job: Job, instance: Instance) -> bool:
    """Fast-forward every repository of the instance. Return True if any moved."""
    changed = False
    for repo in instance.repositories:
        job.log(f"--- [{repo.name}] pull {repo.remote}/{repo.branch}")
        await ensure_ready(job, repo)
        before = await git(repo, "rev-parse", "HEAD")
        await run(job, ["git", "pull", "--ff-only", repo.remote, repo.branch], cwd=repo.path)
        after = await git(repo, "rev-parse", "HEAD")
        if before != after:
            changed = True
            job.log(await git(repo, "log", "--oneline", f"{before}..{after}"))
        else:
            job.log(f"[{repo.name}] already up to date")
    return changed


async def pending_updates(instance: Instance) -> dict[str, int]:
    """Fetch every repository; return {repository: commits not yet pulled}."""
    pending = {}
    for repo in instance.repositories:
        code, out = await capture(["git", "fetch", repo.remote, repo.branch], cwd=repo.path)
        if code:
            raise JobFailed(f"[{repo.name}] git fetch failed: {out}")
        code, out = await capture(["git", "rev-list", "--count", "HEAD..FETCH_HEAD"], cwd=repo.path)
        if code:
            raise JobFailed(f"[{repo.name}] git rev-list failed: {out}")
        if int(out):
            pending[repo.name] = int(out)
    return pending


async def update_modules(job: Job, instance: Instance, force: bool = False) -> None:
    """Update the changed modules; `force` ignores the instance's
    update_modules setting (the user asked for it explicitly)."""
    if not instance.update_modules and not force:
        job.log("Module update disabled for this instance")
        return
    job.log("--- update changed modules")
    await run(
        job,
        [
            instance.venv_bin / "click-odoo-update",
            "-c",
            instance.odoo_config,
            "-d",
            instance.database,
        ],
    )


ADDON_NAME = re.compile(r"[A-Za-z0-9_]+")
_MANIFESTS = ("__manifest__.py", "__openerp__.py")
_VERSION_INFO = re.compile(r"^version_info\s*=\s*\(\s*['\"]?(?:saas~)?(\d+)", re.MULTILINE)


def odoo_major(odoo_bin: Path) -> int | None:
    """The major version from odoo/release.py next to odoo-bin."""
    try:
        text = (odoo_bin.parent / "odoo" / "release.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_INFO.search(text)
    return int(match.group(1)) if match else None


def logfile_to_stdout(major: int | None) -> str:
    """The --logfile option that sends the Odoo log to the job.

    Checked on 2026-10-20 with a config that has a logfile: an empty value logs
    to stdout from Odoo 14 (14, 15, 16, 19, 20) but is ignored by 10, 12 and
    13; /dev/stdout works on all of them, but from 12 Odoo also prints a
    misleading "couldn't create the logfile directory" error.
    """
    return "--logfile=" if major is not None and major >= 14 else "--logfile=/dev/stdout"


def find_odoo_bin(instance: Instance) -> Path | None:
    if instance.odoo_bin is not None:
        return instance.odoo_bin
    for repo in instance.repositories:
        if (repo.path / "odoo-bin").is_file():
            return repo.path / "odoo-bin"
    return None


def addons_paths(instance: Instance) -> list[Path]:
    """The addons_path of the instance's Odoo config, then Odoo's own addons."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(instance.odoo_config, encoding="utf-8")
        raw = parser.get("options", "addons_path", fallback="")
    except (configparser.Error, OSError, UnicodeDecodeError):
        raw = ""
    paths = [Path(p.strip()) for p in raw.replace("\n", ",").split(",") if p.strip()]
    odoo_bin = find_odoo_bin(instance)
    if odoo_bin is not None:
        paths += [odoo_bin.parent / "addons", odoo_bin.parent / "odoo" / "addons"]
    if not raw:
        # no readable addons_path: the repositories and their first level
        for repo in instance.repositories:
            paths += [repo.path] + sorted(p for p in repo.path.iterdir() if p.is_dir())
    return paths


def list_addons(instance: Instance) -> list[dict]:
    """Every addon Odoo would see, the first of a name winning as in Odoo,
    those inside the instance's repositories first."""
    repos = [r.path.resolve() for r in instance.repositories]
    found: dict[str, dict] = {}
    for base in addons_paths(instance):
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for path in children:
            if path.name in found or not ADDON_NAME.fullmatch(path.name):
                continue
            if any((path / m).is_file() for m in _MANIFESTS):
                resolved = path.resolve()
                found[path.name] = {
                    "name": path.name,
                    "path": str(path),
                    "in_repository": any(r == resolved or r in resolved.parents for r in repos),
                }
    return sorted(found.values(), key=lambda a: (not a["in_repository"], a["name"]))


async def update_chosen_modules(job: Job, instance: Instance, modules: list[str]) -> None:
    """odoo-bin -u <modules> --stop-after-init. --workers=0: with workers,
    Odoo opens the HTTP port even when it stops after init, and the running
    instance holds it (checked in Odoo 10, 19 and 20 server.py)."""
    bad = [m for m in modules if not ADDON_NAME.fullmatch(m)]
    if not modules or bad:
        raise JobFailed(f"invalid module list: {', '.join(bad) or 'empty'}")
    odoo_bin = find_odoo_bin(instance)
    if odoo_bin is None or not odoo_bin.is_file():
        raise JobFailed(
            "odoo-bin not found: set odoo_bin in the instance configuration"
        )
    job.log(f"--- update modules {', '.join(modules)}")
    first_line = len(job.lines)
    await run(
        job,
        [
            instance.venv_bin / "python",
            odoo_bin,
            "-c",
            instance.odoo_config,
            "-d",
            instance.database,
            "-u",
            ",".join(modules),
            "--stop-after-init",
            "--workers=0",
            # log to the job, not to the instance's log file
            logfile_to_stdout(odoo_major(odoo_bin)),
        ],
    )
    # Odoo only warns about a module it does not know and exits 0 (same
    # message in Odoo 10 to 20)
    for line in job.lines[first_line:]:
        if "invalid module names, ignored: " in line:
            ignored = line.split("invalid module names, ignored: ", 1)[1].strip()
            raise JobFailed(f"Odoo does not know the modules {ignored}: nothing updated for them")


async def restart(job: Job, instance: Instance) -> None:
    job.log(f"--- restart {instance.restart.type} service {instance.restart.service}")
    await run(job, instance.restart.command(), timeout=RESTART_TIMEOUT)


async def backup(job: Job, instance: Instance, backup_dir: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = backup_dir / instance.server / instance.role
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{instance.database}_{stamp}.zip"
    job.log(f"--- backup database {instance.database} to {dest}")
    await run(
        job,
        [
            instance.venv_bin / "click-odoo-backupdb",
            "-c",
            instance.odoo_config,
            instance.database,
            dest,
        ],
    )
    return dest


async def pull_restart(job: Job, instance: Instance, only_if_changed: bool = False) -> None:
    changed = await pull(job, instance)
    if only_if_changed and not changed:
        job.log("Nothing new to deploy")
        return
    await update_modules(job, instance)
    await restart(job, instance)


async def merge_to_production(job: Job, server: Server, backup_dir: Path) -> None:
    """Back up production, merge into it the commits deployed on test, push,
    update the changed modules and restart production.

    What is merged is the commit checked out on the test server, not the tip
    of its remote branch: production gets exactly what was tested.
    """
    prod, test = server.production, server.test
    if prod is None or test is None:
        raise JobFailed(f"server {server.name} needs both a production and a test instance")

    await backup(job, prod, backup_dir)

    merged: list[tuple[Repository, str]] = []  # (production repository, HEAD before)
    try:
        for test_repo in test.repositories:
            repo = prod.repository(test_repo.name)
            job.log(f"--- [{repo.name}] merge test into {repo.branch}")
            await ensure_ready(job, repo)
            tested = await git(test_repo, "rev-parse", "HEAD")
            before = await git(repo, "rev-parse", "HEAD")
            merged.append((repo, before))
            await run(job, ["git", "pull", "--ff-only", repo.remote, repo.branch], cwd=repo.path)
            await run(job, ["git", "fetch", repo.remote], cwd=repo.path)
            code, _ = await capture(["git", "cat-file", "-e", f"{tested}^{{commit}}"], cwd=repo.path)
            if code:
                raise JobFailed(
                    f"[{repo.name}] the tested commit {tested[:10]} is not on "
                    f"{repo.remote}: push the test branch first"
                )
            code = await run(
                job,
                [
                    "git",
                    "merge",
                    "--no-edit",
                    "-m",
                    f"Merge tested {test_repo.branch} ({tested[:10]}) into {repo.branch} [OmniaCI]",
                    tested,
                ],
                cwd=repo.path,
                check=False,
            )
            if code:
                await run(job, ["git", "merge", "--abort"], cwd=repo.path, check=False)
                raise JobFailed(
                    f"[{repo.name}] merge conflict: resolve it by hand, nothing was pushed"
                )
    except JobFailed:
        for repo, before in reversed(merged):
            job.log(f"--- [{repo.name}] roll back to {before[:10]}")
            # the working copy was clean (ensure_ready), so nothing local is lost
            await run(job, ["git", "reset", "--hard", before], cwd=repo.path, check=False)
        raise

    pushed = []
    for repo, _ in merged:
        job.log(f"--- [{repo.name}] push {repo.branch}")
        try:
            await run(job, ["git", "push", repo.remote, f"HEAD:{repo.branch}"], cwd=repo.path)
        except JobFailed as exc:
            raise JobFailed(
                f"{exc}; already pushed: {', '.join(pushed) or 'none'}. "
                "Production was merged locally but not restarted"
            ) from exc
        pushed.append(repo.name)

    await update_modules(job, prod)
    await restart(job, prod)
