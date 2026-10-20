"""The actions run on an instance: git pull, module update, restart, backup, merge."""

from __future__ import annotations

import asyncio
import datetime
import os
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


async def update_modules(job: Job, instance: Instance) -> None:
    if not instance.update_modules:
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
