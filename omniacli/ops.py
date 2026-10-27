# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The actions run on an instance: git pull, restart, start, stop, backup, merge.

What only a type of server knows (Odoo's module update, its backup, the
command of its process) its plugin does: instance.plugin."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import os
import shlex
import signal
import subprocess
from pathlib import Path

from . import packages
from .config import Instance, Repository, Server
from .jobs import Job, JobFailed

# The environment of every program OmniaCLI runs. No git command may stop on
# a credential or passphrase prompt: there is no terminal (start_new_session)
# and git is told not to ask. OmniaCLI's own Python settings are left out:
# the programs of an instance's venv (Odoo, click-odoo, pip) must import that
# venv's packages, not OmniaCLI's (an IDE puts its venv in PYTHONPATH: Odoo
# then loaded OmniaCLI's cryptography with its own pyOpenSSL and failed on
# 'GEN_EMAIL', found 2026-10-21).
_OWN_PYTHON = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
_ENV = {**{k: v for k, v in os.environ.items() if k not in _OWN_PYTHON},
        "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
# a service can log very long lines (Odoo: tracebacks with data, SQL)
_LINE_LIMIT = 4 * 1024 * 1024
RESTART_TIMEOUT = 300
# a process OmniaCLI starts itself: seconds it must stay up to count as
# started, and seconds it has to stop on SIGTERM before SIGKILL
START_WAIT = 5
STOP_TIMEOUT = 60
# a command past its timeout gets SIGTERM (sudo passes it on to the command,
# SIGKILL it cannot), then SIGKILL after these seconds
KILL_WAIT = 10


def as_user(run_as: str | None, cmd: list, env: dict | None = None) -> list:
    """cmd as the system user run_as, through sudo -n; sudo resets the
    environment, so a PYTHONPATH of env goes on the command line (sudoers
    needs SETENV for it)."""
    if run_as is None:
        return cmd
    extra = [f"PYTHONPATH={env['PYTHONPATH']}"] if env and env.get("PYTHONPATH") else []
    return ["sudo", "-n", "-u", run_as, *extra, *cmd]


# where a command of another user runs when its caller names no folder: that
# user may not enter OmniaCLI's own (/var/lib/omniacli, mode 750), and a
# program that looks in its cwd (ezdxf for ./ezdxf.ini) then fails; "/" is
# what systemd gives a service without WorkingDirectory
OTHER_USER_CWD = Path("/")


def _cwd(cmd: list, cwd: Path | None) -> Path | None:
    """cwd, or OTHER_USER_CWD for a command of as_user without one."""
    if cwd is None and len(cmd) > 2 and Path(str(cmd[0])).name == "sudo" and "-u" in [str(c) for c in cmd[1:3]]:
        return OTHER_USER_CWD
    return cwd


def _program(cmd: list) -> str:
    """The name of the program a command runs, past sudo and its options."""
    args = [str(c) for c in cmd]
    if args and Path(args[0]).name == "sudo":
        args = args[1:]
        while args and (args[0].startswith("-") or "=" in args[0]):
            args = args[2:] if args[0] == "-u" else args[1:]
    return Path(args[0]).name if args else "sudo"


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """sig to the command's process group (started in a session of its own:
    its children too, pip's builds, Odoo's workers); to the command alone
    when the group cannot be signalled."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, SIGKILL after KILL_WAIT. A command of another user (sudo -u)
    gets the SIGTERM through sudo, which passes it on; the SIGKILL reaches
    only sudo: a program of that user that ignores SIGTERM keeps running."""
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_WAIT)
    except asyncio.TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        await proc.wait()


async def run(
    job: Job,
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
    env: dict | None = None,
) -> int:
    """Run a command, streaming its output into the job log."""
    shown, cwd = cwd, _cwd(cmd, cwd)
    job.log(f"$ {shlex.join(str(c) for c in cmd)}" + (f"   (in {shown})" if shown else ""))
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
        await _terminate(proc)
        raise JobFailed(f"{_program(cmd)} did not finish within {timeout:.0f} s")
    except asyncio.CancelledError:
        # the job was stopped: so is its program
        job.log(f"--- stopping {_program(cmd)} (pid {proc.pid})")
        await _terminate(proc)
        raise
    if check and returncode:
        raise JobFailed(f"{_program(cmd)} exited with code {returncode}")
    return returncode


async def capture(
    cmd: list[str], cwd: Path | None = None, timeout: float | None = None,
    env: dict | None = None,
) -> tuple[int, str]:
    """Run a command quietly and return (exit code, stripped output)."""
    cwd = _cwd(cmd, cwd)
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
        await _terminate(proc)
        return 124, f"no answer within {timeout:.0f} s"
    except asyncio.CancelledError:
        await _terminate(proc)
        raise
    return proc.returncode, out.decode(errors="replace").strip()


async def git(repo: Repository, *args: str) -> str:
    code, out = await capture(["git", *args], cwd=repo.path)
    if code:
        raise JobFailed(f"[{repo.name}] git {' '.join(args)} failed: {out}")
    return out


async def remote_branches(path: Path, remote: str, timeout: float = 30) -> dict:
    """The branches a working copy's remote has (the ones a pull can follow)
    and the branch checked out; when the remote cannot be reached, the
    remote-tracking branches of the last fetch, with a note."""
    if not path.is_dir():
        return {"error": f"{path} does not exist"}
    code, current = await capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if code:
        return {"error": current.splitlines()[0] if current else "not a git repository"}
    code, out = await capture(["git", "ls-remote", "--heads", remote], cwd=path, timeout=timeout)
    if code == 0:
        branches = [line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line]
        return {"branches": sorted(branches), "current": current, "note": None}
    reason = out.splitlines()[-1] if out else f"exit code {code}"
    code, out = await capture(
        ["git", "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{remote}"], cwd=path)
    if code:
        return {"error": f"remote {remote} not reachable: {reason}"}
    branches = [b for b in out.splitlines() if b and b != "HEAD"]
    return {"branches": sorted(branches), "current": current,
            "note": f"remote {remote} not reachable ({reason}): branches of the last fetch"}


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


async def update_submodules(job: Job, repo: Repository, check: bool = True) -> None:
    """Check out the submodules at the commits the repository records: a pull,
    a merge or a reset moves only their pointers, and a submodule left behind
    counts as an uncommitted change. Local changes in a submodule make git
    refuse, and they are kept."""
    if not (repo.path / ".gitmodules").is_file():
        return
    try:
        await run(job, ["git", "submodule", "update", "--init", "--recursive"],
                  cwd=repo.path, check=check)
    except JobFailed as exc:
        raise JobFailed(f"[{repo.name}] {exc}: its submodules are not on the commits "
                        "it records") from exc


async def pull(job: Job, instance: Instance) -> bool:
    """Fast-forward every repository of the instance and its submodules,
    then install its package if a new one is published (packages.py).
    Return True if any repository moved or the package was installed."""
    changed = False
    for repo in instance.repositories:
        job.log(f"--- [{repo.name}] pull {repo.remote}/{repo.branch}")
        await ensure_ready(job, repo, keep_changes=instance.keeps_local_changes)
        before = await git(repo, "rev-parse", "HEAD")
        await run(job, ["git", "pull", "--ff-only", repo.remote, repo.branch], cwd=repo.path)
        await update_submodules(job, repo)
        after = await git(repo, "rev-parse", "HEAD")
        if before != after:
            changed = True
            job.log(await git(repo, "log", "--oneline", f"{before}..{after}"))
        else:
            job.log(f"[{repo.name}] already up to date")
    if instance.package is not None and await packages.update(job, instance):
        changed = True
    return changed


async def pending_updates(instance: Instance) -> dict[str, int]:
    """Fetch every repository; return {repository: commits not yet pulled},
    and {"package <name>": 1} when a new package is published."""
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
    pending.update(await packages.pending(instance))
    return pending


async def restart(job: Job, instance: Instance) -> None:
    if instance.restart.type == "process":
        await restart_process(job, instance)
        return
    job.log(f"--- restart {instance.restart.type} service {instance.restart.service}")
    await run(job, instance.restart.command(), timeout=RESTART_TIMEOUT)


async def service_state(instance: Instance) -> str | None:
    """ActiveState of a systemd instance's unit (active, inactive, failed,
    activating...); None when it is not systemd or the unit is not found."""
    if instance.restart.type != "systemd":
        return None
    code, out = await capture(["systemctl", "show", "-p", "LoadState", "-p", "ActiveState",
                               instance.restart.service])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if code or props.get("LoadState") != "loaded":
        return None
    return props.get("ActiveState") or None


def _label(instance: Instance) -> str:
    return instance.plugin.label


def _config(instance: Instance) -> Path | None:
    """The config file the instance's processes are started with (plugin)."""
    return instance.plugin.process_config(instance)


async def start(job: Job, instance: Instance) -> None:
    """Start the systemd service, or the process OmniaCLI starts itself."""
    if instance.restart.type == "process":
        job.log(f"--- start {_label(instance)} started by OmniaCLI ({_config(instance)})")
        if managed_pid(instance) is not None:
            job.log("already running")
            return
        await restart_process(job, instance)
        return
    if instance.restart.type != "systemd":
        raise JobFailed(f"Start is for a systemd service or an {_label(instance)} OmniaCLI starts itself")
    job.log(f"--- start systemd service {instance.restart.service}")
    await run(job, instance.restart.command("start"), timeout=RESTART_TIMEOUT)


# The processes OmniaCLI started, to reap them when they end; one started
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

    config = _config(instance)
    return processes_by_config().get(os.path.realpath(config), []) if config is not None else []


def managed_pid(instance: Instance) -> int | None:
    """The process OmniaCLI started for the instance, if it still runs."""
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
    config = _config(instance)
    if not _alive(pid) or config is None or str(config) not in argv:
        return None
    return pid


def foreign_pids(instance: Instance, own: int | None) -> list[int]:
    """Processes started with the instance's config that OmniaCLI did not
    start: its own run in the process group it created."""
    return [p for p in _config_pids(instance) if own is None or _pgid(p) != own]


async def stop_process(job: Job, instance: Instance, pid: int) -> None:
    """SIGTERM to the process group (Odoo and its workers), SIGKILL after
    STOP_TIMEOUT seconds."""
    job.log(f"stopping {_label(instance)}, pid {pid}")
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
        raise JobFailed(f"{_label(instance)}, pid {pid}, does not stop")
    instance.pid_file.unlink(missing_ok=True)
    child = _children.pop(instance.key, None)
    if child is not None:
        child.poll()  # reaps it
    job.log("stopped")


async def start_process(job: Job, instance: Instance) -> int:
    """Start the plugin's command in its own session, so that it outlives
    OmniaCLI, with its output appended to the instance's log."""
    cmd, cwd = instance.plugin.process_command(instance)
    cmd = [str(c) for c in cmd]
    job.log(f"$ {shlex.join(cmd)}   (output to {instance.log_file})")
    instance.log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(instance.log_file, "ab") as out:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out.write(f"--- {stamp} started by OmniaCLI: {shlex.join(cmd)}\n".encode())
            out.flush()
            child = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, env=_ENV,
                                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise JobFailed(f"cannot start {_label(instance)}: {exc}") from exc
    _children[instance.key] = child
    instance.pid_file.write_text(f"{child.pid}\n")
    for _ in range(START_WAIT * 2):
        await asyncio.sleep(0.5)
        if child.poll() is not None:
            instance.pid_file.unlink(missing_ok=True)
            _children.pop(instance.key, None)
            raise JobFailed(f"{_label(instance)} exited with code {child.returncode} at once: see its log "
                            f"{instance.log_file}")
    job.log(f"{_label(instance)} started, pid {child.pid}")
    return child.pid


async def stop(job: Job, instance: Instance) -> None:
    """Stop the systemd service, or the process OmniaCLI started (one it did
    not start is not touched); an init.d service is not stopped from here."""
    if instance.restart.type == "systemd":
        job.log(f"--- stop systemd service {instance.restart.service}")
        await run(job, instance.restart.command("stop"), timeout=RESTART_TIMEOUT)
        return
    if instance.restart.type != "process":
        raise JobFailed(f"Stop is for a systemd service or an {_label(instance)} OmniaCLI starts itself")
    job.log(f"--- stop {_label(instance)} started by OmniaCLI ({_config(instance)})")
    own = managed_pid(instance)
    if own is None:
        others = foreign_pids(instance, None)
        job.log("not running" + (
            f"; pid {', '.join(map(str, others))} run with this config but OmniaCLI did "
            "not start them: not touched" if others else ""))
        return
    await stop_process(job, instance, own)


async def restart_process(job: Job, instance: Instance) -> None:
    job.log(f"--- restart {_label(instance)} started by OmniaCLI ({_config(instance)})")
    own = managed_pid(instance)
    others = foreign_pids(instance, own)
    if others:
        raise JobFailed(
            f"{_label(instance)} is already running with {_config(instance)} but OmniaCLI did not start "
            f"it (pid {', '.join(map(str, others))}): stop it first, OmniaCLI stops only its own")
    if own is not None:
        await stop_process(job, instance, own)
    await start_process(job, instance)


async def backup(job: Job, instance: Instance, backup_dir: Path, options: dict | None = None) -> Path:
    """The plugin's backup, into <backup_dir>/<server>/<role>; options: the
    values of its backup_fields (none: its defaults)."""
    if not instance.plugin.has_backup:
        raise JobFailed(f"{_label(instance)} has no backup")
    dest_dir = backup_dir / instance.server / instance.role
    dest_dir.mkdir(parents=True, exist_ok=True)
    return await instance.plugin.backup(job, instance, dest_dir, options)


async def pull_restart(job: Job, instance: Instance, only_if_changed: bool = False) -> None:
    changed = await pull(job, instance)
    if only_if_changed and not changed:
        job.log("Nothing new to deploy")
        return
    await instance.plugin.after_pull(job, instance)
    if only_if_changed and await _stopped(instance):
        # the auto update: a service someone stopped stays stopped
        job.log(f"{_label(instance)} is stopped: not started again")
        return
    await restart(job, instance)


async def _stopped(instance: Instance) -> bool:
    if instance.restart.type == "process":
        return managed_pid(instance) is None
    return await service_state(instance) in ("inactive", "failed")


async def merge_to_production(job: Job, server: Server, backup_dir: Path) -> None:
    """Back up production, merge into it the commits deployed on test, push,
    do what follows a pull (Odoo: update the changed modules) and restart
    production. A type without a backup is merged without one.

    What is merged is the commit checked out on the test server, not the tip
    of its remote branch: production gets exactly what was tested.
    """
    prod, test = server.production, server.test
    if prod is None or test is None:
        raise JobFailed(f"server {server.name} needs both a production and a test instance")

    if prod.plugin.has_backup:
        await backup(job, prod, backup_dir)
    else:
        job.log(f"--- no backup: {_label(prod)} has none")

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
            await update_submodules(job, repo)
    except JobFailed:
        for repo, before in reversed(merged):
            job.log(f"--- [{repo.name}] roll back to {before[:10]}")
            # the working copy was clean (ensure_ready), so nothing local is lost
            await run(job, ["git", "reset", "--hard", before], cwd=repo.path, check=False)
            await update_submodules(job, repo, check=False)
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

    await prod.plugin.after_pull(job, prod)
    await restart(job, prod)
