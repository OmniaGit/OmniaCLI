"""The actions run on an instance: git pull, module update, restart, backup, merge."""

from __future__ import annotations

import asyncio
import configparser
import datetime
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

from .config import Instance, Repository, Server
from .jobs import Job, JobFailed

# No git command may stop on a credential or passphrase prompt: there is no
# terminal (start_new_session) and git is told not to ask.
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
# Odoo can log very long lines (tracebacks with data, SQL)
_LINE_LIMIT = 4 * 1024 * 1024
RESTART_TIMEOUT = 300
# an Odoo OmniaCLI starts itself: seconds it must stay up to count as started,
# and seconds it has to stop on SIGTERM before SIGKILL
START_WAIT = 5
STOP_TIMEOUT = 60
# pip may build packages from source (psycopg2, lxml...)
PIP_TIMEOUT = 1800
# what OmniaCLI runs in an instance's venv: click-odoo-update, click-odoo-backupdb
CLI_PACKAGE = "click-odoo-contrib"


async def run(
    job: Job,
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
    env: dict | None = None,
) -> int:
    """Run a command, streaming its output into the job log."""
    job.log(f"$ {shlex.join(str(c) for c in cmd)}" + (f"   (in {cwd})" if cwd else ""))
    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            cwd=cwd,
            env=env or _ENV,
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


async def capture(
    cmd: list[str], cwd: Path | None = None, timeout: float | None = None,
    env: dict | None = None,
) -> tuple[int, str]:
    """Run a command quietly and return (exit code, stripped output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            cwd=cwd,
            env=env or _ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"no answer within {timeout:.0f} s"
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


async def ensure_ready(job: Job, repo: Repository, keep_changes: bool = False) -> None:
    """Fail unless the working copy is on its branch with no local changes
    (keep_changes: local changes are kept, git pull --ff-only refuses to
    overwrite them)."""
    branch = await git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != repo.branch:
        raise JobFailed(
            f"[{repo.name}] {repo.path} is on branch '{branch}', expected '{repo.branch}'"
        )
    changes = await git(repo, "status", "--porcelain", "--untracked-files=no")
    if changes:
        job.log(changes)
        if keep_changes:
            job.log(f"[{repo.name}] uncommitted changes kept (test instance without auto update)")
            return
        raise JobFailed(f"[{repo.name}] {repo.path} has uncommitted changes")


async def pull(job: Job, instance: Instance) -> bool:
    """Fast-forward every repository of the instance. Return True if any moved."""
    changed = False
    for repo in instance.repositories:
        job.log(f"--- [{repo.name}] pull {repo.remote}/{repo.branch}")
        await ensure_ready(job, repo, keep_changes=instance.keeps_local_changes)
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
        env=odoo_env(instance),
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


def odoo_env(instance: Instance) -> dict:
    """The environment for click-odoo: it imports odoo, which a venv that runs
    Odoo from its sources (odoo-bin) does not have; the folder of odoo-bin
    makes it importable (checked 2026-10-21, Odoo 20 and click-odoo 1.8)."""
    odoo_bin = find_odoo_bin(instance)
    if odoo_bin is None or not odoo_bin.is_file():
        return _ENV
    paths = [str(odoo_bin.parent), _ENV.get("PYTHONPATH", "")]
    return {**_ENV, "PYTHONPATH": os.pathsep.join(p for p in paths if p)}


def find_odoo_bin(instance: Instance) -> Path | None:
    return locate_odoo_bin(instance.odoo_bin, [r.path for r in instance.repositories])


def locate_odoo_bin(odoo_bin: Path | None, folders: list[Path]) -> Path | None:
    """odoo_bin when set, otherwise the odoo-bin of the first folder having one."""
    if odoo_bin is not None:
        return odoo_bin
    for folder in folders:
        if (folder / "odoo-bin").is_file():
            return folder / "odoo-bin"
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
    if instance.restart.type == "process":
        await restart_process(job, instance)
        return
    job.log(f"--- restart {instance.restart.type} service {instance.restart.service}")
    await run(job, instance.restart.command(), timeout=RESTART_TIMEOUT)


# The Odoo processes OmniaCLI started, to reap them when they end; one started
# before OmniaCLI was restarted is found again from its pid file.
_children: dict[str, subprocess.Popen] = {}


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _config_pids(instance: Instance) -> list[int]:
    from .metrics import processes_by_config  # metrics imports ops

    return processes_by_config().get(os.path.realpath(instance.odoo_config), [])


def managed_pid(instance: Instance) -> int | None:
    """The Odoo OmniaCLI started for the instance, if it still runs."""
    child = _children.get(instance.key)
    if child is not None and child.poll() is not None:
        _children.pop(instance.key, None)
    try:
        pid = int(instance.pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_text().split("\0")
    except OSError:
        return None
    # the pid may have been given to another program since
    if not _alive(pid) or str(instance.odoo_config) not in argv:
        return None
    return pid


def foreign_pids(instance: Instance, own: int | None) -> list[int]:
    """Processes started with the instance's Odoo config that OmniaCLI did not
    start: its own run in the process group it created."""
    return [p for p in _config_pids(instance) if own is None or _pgid(p) != own]


async def stop_process(job: Job, instance: Instance, pid: int) -> None:
    """SIGTERM to the process group (Odoo and its workers), SIGKILL after
    STOP_TIMEOUT seconds."""
    job.log(f"stopping Odoo, pid {pid}")
    for sig, wait in ((signal.SIGTERM, STOP_TIMEOUT), (signal.SIGKILL, 10)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        for _ in range(wait * 2):
            if not any(_alive(p) for p in _config_pids(instance) if _pgid(p) == pid):
                break
            await asyncio.sleep(0.5)
        else:
            job.log(f"still running after {wait} s" + (", sending SIGKILL" if sig == signal.SIGTERM else ""))
            continue
        break
    else:
        raise JobFailed(f"Odoo, pid {pid}, does not stop")
    instance.pid_file.unlink(missing_ok=True)
    child = _children.pop(instance.key, None)
    if child is not None:
        child.poll()  # reaps it
    job.log("stopped")


async def start_process(job: Job, instance: Instance) -> int:
    """Start odoo-bin in its own session, so that it outlives OmniaCLI, with
    its output appended to the instance's log."""
    odoo_bin = find_odoo_bin(instance)
    if odoo_bin is None or not odoo_bin.is_file():
        raise JobFailed("odoo-bin not found: set odoo_bin in the instance configuration")
    cmd = [str(instance.venv_bin / "python"), str(odoo_bin), "-c", str(instance.odoo_config),
           # the log goes to stdout, so to the instance's log, not to the logfile of the config
           logfile_to_stdout(odoo_major(odoo_bin))]
    job.log(f"$ {shlex.join(cmd)}   (output to {instance.log_file})")
    instance.log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(instance.log_file, "ab") as out:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out.write(f"--- {stamp} started by OmniaCLI: {shlex.join(cmd)}\n".encode())
            out.flush()
            child = subprocess.Popen(cmd, cwd=odoo_bin.parent, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise JobFailed(f"cannot start Odoo: {exc}") from exc
    _children[instance.key] = child
    instance.pid_file.write_text(f"{child.pid}\n")
    for _ in range(START_WAIT * 2):
        await asyncio.sleep(0.5)
        if child.poll() is not None:
            instance.pid_file.unlink(missing_ok=True)
            _children.pop(instance.key, None)
            raise JobFailed(f"Odoo exited with code {child.returncode} at once: see its log "
                            f"{instance.log_file}")
    job.log(f"Odoo started, pid {child.pid}")
    return child.pid


async def stop(job: Job, instance: Instance) -> None:
    """Stop the Odoo OmniaCLI started; a service is not stopped from here."""
    if instance.restart.type != "process":
        raise JobFailed("Stop is only for an Odoo that OmniaCLI starts itself (restart type process)")
    job.log(f"--- stop Odoo started by OmniaCLI ({instance.odoo_config})")
    own = managed_pid(instance)
    if own is None:
        others = foreign_pids(instance, None)
        job.log("not running" + (
            f"; pid {', '.join(map(str, others))} run with this config but OmniaCLI did "
            "not start them: not touched" if others else ""))
        return
    await stop_process(job, instance, own)


async def restart_process(job: Job, instance: Instance) -> None:
    job.log(f"--- restart Odoo started by OmniaCLI ({instance.odoo_config})")
    own = managed_pid(instance)
    others = foreign_pids(instance, own)
    if others:
        raise JobFailed(
            f"Odoo is already running with {instance.odoo_config} but OmniaCLI did not start "
            f"it (pid {', '.join(map(str, others))}): stop it first, OmniaCLI stops only its own")
    if own is not None:
        await stop_process(job, instance, own)
    await start_process(job, instance)


def requirement_files(odoo_bin: Path | None, folders: list[Path]) -> list[Path]:
    """Odoo's requirements.txt (next to odoo-bin), then each folder's."""
    found = []
    odoo_bin = locate_odoo_bin(odoo_bin, folders)
    candidates = [odoo_bin.parent] if odoo_bin is not None else []
    for folder in candidates + folders:
        path = folder / "requirements.txt"
        if path.is_file() and path not in found:
            found.append(path)
    return found


async def pip_install(job: Job, venv_bin: Path, args: list[str]) -> None:
    await run(
        job,
        [venv_bin / "python", "-m", "pip", "install", "--disable-pip-version-check", *args],
        timeout=PIP_TIMEOUT,
    )


async def install_requirements(job: Job, venv_bin: Path, odoo_bin: Path | None,
                               folders: list[Path]) -> None:
    """pip install, in the venv, the requirements of Odoo and of every
    repository folder, in one resolution."""
    files = requirement_files(odoo_bin, folders)
    if not files:
        raise JobFailed("no requirements.txt next to odoo-bin nor in the repositories")
    job.log("--- requirements: " + ", ".join(str(f) for f in files))
    await pip_install(job, venv_bin, [arg for f in files for arg in ("-r", str(f))])


async def install_cli(job: Job, venv_bin: Path) -> None:
    """pip install the scripts OmniaCLI runs in the venv."""
    job.log(f"--- {CLI_PACKAGE} in {venv_bin}")
    await pip_install(job, venv_bin, [CLI_PACKAGE])


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
        env=odoo_env(instance),
    )
    return dest


async def pull_restart(job: Job, instance: Instance, only_if_changed: bool = False) -> None:
    changed = await pull(job, instance)
    if only_if_changed and not changed:
        job.log("Nothing new to deploy")
        return
    await update_modules(job, instance)
    if only_if_changed and instance.restart.type == "process" and managed_pid(instance) is None:
        # the auto update: an Odoo someone stopped stays stopped
        job.log("Odoo is stopped: not started again")
        return
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
            # what runs on test must be a commit, or production would get
            # something else than what was tested
            if await git(test_repo, "status", "--porcelain", "--untracked-files=no"):
                raise JobFailed(f"[{test_repo.name}] the test copy {test_repo.path} has uncommitted "
                                "changes: commit and push them, or discard them, before the merge")
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
                    f"Merge tested {test_repo.branch} ({tested[:10]}) into {repo.branch} [OmniaCLI]",
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
