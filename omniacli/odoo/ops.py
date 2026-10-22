# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What OmniaCLI runs on an Odoo instance: module update, backup, odoo-bin, pip."""

from __future__ import annotations

import configparser
import datetime
import re
import shutil
import tempfile
from pathlib import Path

from ..plugin_api import Instance, Job, JobFailed, as_user, base_env, run

# pip may build packages from source (psycopg2, lxml...)
PIP_TIMEOUT = 1800
# a backup made as an instance's run_as user is written here first: that
# user cannot write in the backup folder (on disk: /tmp may be in memory)
BACKUP_STAGING = Path("/var/tmp")
# what OmniaCLI runs in an instance's venv: click-odoo-update, click-odoo-backupdb
CLI_PACKAGE = "click-odoo-contrib"


async def update_modules(job: Job, instance: Instance, force: bool = False) -> None:
    """Update the changed modules; `force` ignores the instance's
    update_modules setting (the user asked for it explicitly)."""
    opts = instance.options
    if not opts.update_modules and not force:
        job.log("Module update disabled for this instance")
        return
    job.log("--- update changed modules")
    env = odoo_env(instance)
    await run(
        job,
        as_user(instance.run_as, [
            opts.venv_bin / "click-odoo-update",
            "-c",
            opts.odoo_config,
            "-d",
            opts.database,
        ], env),
        env=env,
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
        return base_env()
    return {**base_env(), "PYTHONPATH": str(odoo_bin.parent)}


def find_odoo_bin(instance: Instance) -> Path | None:
    return locate_odoo_bin(instance.options.odoo_bin, [r.path for r in instance.repositories])


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
        parser.read(instance.options.odoo_config, encoding="utf-8")
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
    opts = instance.options
    job.log(f"--- update modules {', '.join(modules)}")
    first_line = len(job.lines)
    await run(
        job,
        as_user(instance.run_as, [
            opts.venv_bin / "python",
            odoo_bin,
            "-c",
            opts.odoo_config,
            "-d",
            opts.database,
            "-u",
            ",".join(modules),
            "--stop-after-init",
            "--workers=0",
            # log to the job, not to the instance's log file
            logfile_to_stdout(odoo_major(odoo_bin)),
        ]),
    )
    # Odoo only warns about a module it does not know and exits 0 (same
    # message in Odoo 10 to 20)
    for line in job.lines[first_line:]:
        if "invalid module names, ignored: " in line:
            ignored = line.split("invalid module names, ignored: ", 1)[1].strip()
            raise JobFailed(f"Odoo does not know the modules {ignored}: nothing updated for them")


def process_command(instance: Instance) -> tuple[list[str], Path]:
    """odoo-bin as OmniaCLI starts it (restart type process), in its folder."""
    odoo_bin = find_odoo_bin(instance)
    if odoo_bin is None or not odoo_bin.is_file():
        raise JobFailed("odoo-bin not found: set odoo_bin in the instance configuration")
    opts = instance.options
    major = odoo_major(odoo_bin)
    cmd = [str(opts.venv_bin / "python"), str(odoo_bin), "-c", str(opts.odoo_config),
           # the log goes to stdout, so to the instance's log, not to the logfile of the config
           logfile_to_stdout(major)]
    if opts.http_port is not None:
        # wins over http_port of the config; before Odoo 11 the option was --xmlrpc-port
        flag = "--xmlrpc-port" if major is not None and major < 11 else "--http-port"
        cmd.append(f"{flag}={opts.http_port}")
    return cmd, odoo_bin.parent


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


async def pip_install(job: Job, venv_bin: Path, args: list[str], run_as: str | None = None) -> None:
    await run(
        job,
        as_user(run_as, [venv_bin / "python", "-m", "pip", "install", "--disable-pip-version-check", *args]),
        timeout=PIP_TIMEOUT,
    )


async def install_requirements(job: Job, venv_bin: Path, odoo_bin: Path | None,
                               folders: list[Path], run_as: str | None = None) -> None:
    """pip install, in the venv, the requirements of Odoo and of every
    repository folder, in one resolution."""
    files = requirement_files(odoo_bin, folders)
    if not files:
        raise JobFailed("no requirements.txt next to odoo-bin nor in the repositories")
    job.log("--- requirements: " + ", ".join(str(f) for f in files))
    await pip_install(job, venv_bin, [arg for f in files for arg in ("-r", str(f))], run_as)


async def install_cli(job: Job, venv_bin: Path, run_as: str | None = None) -> None:
    """pip install the scripts OmniaCLI runs in the venv."""
    job.log(f"--- {CLI_PACKAGE} in {venv_bin}" + (f" as {run_as}" if run_as else ""))
    await pip_install(job, venv_bin, [CLI_PACKAGE], run_as)


async def backup(job: Job, instance: Instance, dest_dir: Path) -> Path:
    """click-odoo-backupdb of the database and filestore into a zip of dest_dir."""
    opts = instance.options
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{opts.database}_{stamp}.zip"
    job.log(f"--- backup database {opts.database} to {dest}")
    env = odoo_env(instance)
    if instance.run_as is None:
        await run(job, [opts.venv_bin / "click-odoo-backupdb", "-c", opts.odoo_config,
                        opts.database, dest], env=env)
        return dest
    # run_as may create the file here (write and search) but not list the folder
    staging = Path(tempfile.mkdtemp(prefix="omniacli-backup-", dir=BACKUP_STAGING))
    try:
        staging.chmod(0o733)
        staged = staging / dest.name
        await run(job, as_user(instance.run_as, [
            opts.venv_bin / "click-odoo-backupdb", "-c", opts.odoo_config,
            opts.database, staged], env), env=env)
        shutil.move(staged, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return dest
