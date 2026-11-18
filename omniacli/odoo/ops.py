# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What OmniaCLI runs on an Odoo instance: module update, backup, odoo-bin, pip."""

from __future__ import annotations

import ast
import asyncio
import configparser
import contextlib
import datetime
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from ..plugin_api import Instance, Job, JobFailed, as_user, base_env, capture, run

# pip may build packages from source (psycopg2, lxml...)
PIP_TIMEOUT = 1800
# a backup made or restored as an instance's run_as user passes through a
# folder here that only that user can enter: it cannot reach the backup folder
# (on disk: /tmp may be in memory)
BACKUP_STAGING = Path("/var/tmp")
# the folder (700, beside the zip) where a restore without run_as unpacks
# the backup: the backup listing shows only the zips
RESTORE_TEMP = ".restore-"
# what OmniaCLI runs in an instance's venv: click-odoo-update, click-odoo-backupdb
CLI_PACKAGE = "click-odoo-contrib"
# what the venv's python runs to share a filestore through hard links
LINK_SCRIPT = Path(__file__).with_name("link_filestore.py")
# what click-odoo runs to set every user's password to admin
PASSWORDS_SCRIPT = Path(__file__).with_name("reset_passwords.py")
# what the venv's python runs to load a plain SQL dump (the database dropped
# and made again, the filestore left as it is)
LOAD_SQL_SCRIPT = Path(__file__).with_name("load_sql.py")
SQL_ERRORS_MARK = "OMNIACLI_SQL_ERRORS="
# what click-odoo runs around a module update: a neutralized database keeps
# its crons off
CRON_GUARD_SCRIPT = Path(__file__).with_name("cron_guard.py")
CRON_GUARD_MARK = "OMNIACLI_ACTIVE_CRONS="
# what the venv's python runs to find the binary columns of the database
# and to back it up with some of them emptied
BINARY_SCRIPT = Path(__file__).with_name("binary_dump.py")
BINARY_MARK = "OMNIACLI_BINARY_COLUMNS="
ROOM_MARK = "OMNIACLI_RESTORE_ROOM="
MODULES_MARK = "OMNIACLI_MODULE_VERSIONS="
# the hosts of a PostgreSQL on this machine (empty: its socket)
LOCAL_HOSTS = (None, "", "localhost", "127.0.0.1", "::1")


@contextlib.contextmanager
def _staged_script(script: Path, prefix: str):
    """A copy of script any user may read, in a folder only OmniaCLI writes to."""
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=BACKUP_STAGING))
    try:
        staging.chmod(0o755)
        copy = staging / script.name
        shutil.copyfile(script, copy)
        copy.chmod(0o644)
        yield copy
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def _binary_dump(instance: Instance, mode: str, mark: str, input: bytes | None = None,
                       timeout: float = 600) -> dict:
    """binary_dump.py scan, room or modules on the instance's database, as
    the user the instance runs as (input: its standard input): the JSON it
    prints after mark."""
    opts = instance.options
    env = odoo_env(instance)
    with _staged_script(BINARY_SCRIPT, "omniacli-binaries-") as script:
        cmd = [opts.venv_bin / "python", script, mode, opts.odoo_config, opts.database]
        code, out = await capture(as_user(instance.run_as, cmd, env), env=env, timeout=timeout,
                                  input=input)
    found = [line for line in out.splitlines() if line.startswith(mark)]
    if code or not found:
        raise JobFailed(f"{opts.database} cannot be read ({mode}): {out[-2000:]}")
    try:
        data = json.loads(found[-1][len(mark):])
    except ValueError as exc:
        raise JobFailed(f"{opts.database} ({mode}): {exc}") from None
    if not isinstance(data, dict):
        raise JobFailed(f"{opts.database} ({mode}): not a mapping")
    return data


async def restore_room(instance: Instance) -> dict:
    """Where a restore into the instance writes and what it drops first
    (binary_dump.py room), with PostgreSQL's data folder found by
    pg_lsclusters when PostgreSQL does not tell it (not a superuser) and it
    runs on this machine; data_directory stays None when it is not found."""
    found = await _binary_dump(instance, "room", ROOM_MARK)
    if not found.get("data_directory") and (found.get("db_host") in LOCAL_HOSTS
                                            or str(found.get("db_host")).startswith("/")):
        found["data_directory"] = await _cluster_folder(int(found.get("db_port") or 5432))
    return found


async def _cluster_folder(port: int) -> str | None:
    """The data folder of the PostgreSQL cluster on port (Debian's
    pg_lsclusters, which any user may run); None without it."""
    if not shutil.which("pg_lsclusters"):
        return None
    code, out = await capture(["pg_lsclusters", "--no-header"], timeout=30)
    if code:
        return None
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[2] == str(port):
            return fields[5]
    return None


async def binary_columns(instance: Instance) -> dict:
    """The size on disk of the instance's database and every bytea column
    of it with the bytes it holds (binary_dump.py scan, as the user the
    instance runs as): {size, filestore: {path, bytes, files}, columns:
    [{schema, table, column, bytes, rows, not_null}]}."""
    data = await _binary_dump(instance, "scan", BINARY_MARK)
    if not isinstance(data.get("columns"), list):
        raise JobFailed(f"the binary columns of {instance.options.database}: no list of columns")
    return data


async def _cron_guard(instance: Instance, *args: str) -> tuple[int, str]:
    """click-odoo runs cron_guard.py as the user the instance runs as."""
    opts = instance.options
    env = odoo_env(instance)
    # a copy of the script any user may read, in a folder only OmniaCLI writes to
    staging = Path(tempfile.mkdtemp(prefix="omniacli-crons-", dir=BACKUP_STAGING))
    try:
        staging.chmod(0o755)
        script = staging / CRON_GUARD_SCRIPT.name
        shutil.copyfile(CRON_GUARD_SCRIPT, script)
        script.chmod(0o644)
        cmd = [opts.venv_bin / "click-odoo", "-c", opts.odoo_config, "-d", opts.database, script, *args]
        return await capture(as_user(instance.run_as, cmd, env), env=env)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@contextlib.asynccontextmanager
async def crons_stay_off(job: Job, instance: Instance):
    """What runs inside updates modules: on a neutralized database, the crons
    that are off stay off after it, even when it fails half way. An update
    never stops for this: without the list it goes on, with a warning."""
    code, out = await _cron_guard(instance, "before")
    if code:
        job.log(f"WARNING: the active crons of {instance.options.database} are not known ({code}): "
                f"if it is neutralized, the update may switch some on. {out}")
    found = [line for line in out.splitlines() if line.startswith(CRON_GUARD_MARK)]
    try:
        yield
    finally:
        if found:
            code, out = await _cron_guard(instance, "after", found[-1][len(CRON_GUARD_MARK):])
            if code:
                raise JobFailed(f"the update may have switched on crons of the neutralized database "
                                f"{instance.options.database}, and they could not be switched off ({code}): {out}")
            for line in out.splitlines():
                if line.startswith("switched off again: "):
                    job.log(f"Neutralized database: cron {line}")


async def update_modules(job: Job, instance: Instance, force: bool = False) -> None:
    """Update the changed modules; `force` ignores the instance's
    update_modules setting (the user asked for it explicitly)."""
    opts = instance.options
    if not opts.update_modules and not force:
        job.log("Module update disabled for this instance")
        return
    job.log("--- update changed modules")
    env = odoo_env(instance)
    async with crons_stay_off(job, instance):
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


def psql_path() -> str:
    """The psql of the psql page, by its full path: the one a sudoers line
    names for an instance with run_as."""
    return shutil.which("psql") or "/usr/bin/psql"


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


def manifest_versions(addons: list[dict]) -> dict[str, str | None]:
    """{module: the version its manifest gives (None: none, Odoo's 1.0)} of
    the addons list_addons found; a manifest that cannot be read is left out."""
    found: dict[str, str | None] = {}
    for addon in addons:
        for name in _MANIFESTS:
            path = Path(addon["path"]) / name
            if not path.is_file():
                continue
            try:
                manifest = ast.literal_eval(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError, SyntaxError, MemoryError, RecursionError):
                break
            if isinstance(manifest, dict):
                version = manifest.get("version")
                found[addon["name"]] = str(version) if version is not None else None
            break
    return found


async def module_versions(instance: Instance) -> dict[str, dict]:
    """{module: {state, installed, available, newer}} of the addons the
    update dialog lists and of the database's modules (binary_dump.py
    modules): the version installed in the database, the one on disk the
    update brings, and whether that one is newer."""
    manifests = await asyncio.to_thread(lambda: manifest_versions(list_addons(instance)))
    return await _binary_dump(instance, "modules", MODULES_MARK,
                              input=json.dumps(manifests).encode(), timeout=120)


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
    async with crons_stay_off(job, instance):
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


# the name of a backup made without the filestore ends so
NO_FILESTORE = "_nofilestore"
# and of one made with binary columns emptied
NO_BINARIES = "_nobinaries"

# What the venv's python runs as run_as to carry a backup in and out of its
# private folder: the file holds a whole database, so no other user of the
# machine may read it, not even knowing its path (which ps shows).
_MAKE_FOLDER = "import sys, tempfile; print(tempfile.mkdtemp(prefix=sys.argv[2], dir=sys.argv[1]))"
_REMOVE_FOLDER = "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)"
_FILE_FROM_STDIN = """import os, shutil, sys
with os.fdopen(os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
    shutil.copyfileobj(sys.stdin.buffer, out, 1 << 20)
"""
_FILE_TO_STDOUT = """import shutil, sys
with open(sys.argv[1], "rb") as source:
    shutil.copyfileobj(source, sys.stdout.buffer, 1 << 20)
"""


def _python_as(instance: Instance, code: str, *args) -> list:
    """The venv's python running code as run_as, isolated from its environment."""
    return as_user(instance.run_as, [instance.options.venv_bin / "python", "-I", "-c", code, *args])


@contextlib.asynccontextmanager
async def _private_folder(instance: Instance, prefix: str):
    """A folder in BACKUP_STAGING made by run_as (mode 700), removed at the end."""
    code, out = await capture(_python_as(instance, _MAKE_FOLDER, BACKUP_STAGING, prefix))
    lines = out.splitlines()
    if code or not lines:
        raise JobFailed(f"cannot make a private folder in {BACKUP_STAGING} as {instance.run_as}: {out}")
    folder = Path(lines[-1])
    try:
        yield folder
    finally:
        await capture(_python_as(instance, _REMOVE_FOLDER, folder))


async def backup(job: Job, instance: Instance, dest_dir: Path, filestore: bool = True,
                 emptied: list[dict] | None = None) -> Path:
    """click-odoo-backupdb of the database, and of its filestore unless
    filestore is false, into a zip of dest_dir. With emptied (columns of
    binary_columns), binary_dump.py makes the same zip with those columns
    left empty."""
    opts = instance.options
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / (f"{opts.database}_{stamp}{'' if filestore else NO_FILESTORE}"
                       f"{NO_BINARIES if emptied else ''}.zip")
    job.log(f"--- backup database {opts.database}" + ("" if filestore else " without its filestore")
            + (f", {len(emptied)} binary columns emptied" if emptied else "") + f" to {dest}")
    env = odoo_env(instance)
    with contextlib.ExitStack() as stack:
        if emptied:
            script = stack.enter_context(_staged_script(BINARY_SCRIPT, "omniacli-binaries-"))
            columns = json.dumps([[c["schema"], c["table"], c["column"]] for c in emptied])

            def command(target):
                return [opts.venv_bin / "python", script, "dump", opts.odoo_config, opts.database, target,
                        columns, "1" if filestore else "0"]
        else:
            def command(target):
                return [opts.venv_bin / "click-odoo-backupdb", "-c", opts.odoo_config,
                        *([] if filestore else ["--no-filestore"]), opts.database, target]
        return await _backup_to(job, instance, dest, command, env)


async def _backup_to(job: Job, instance: Instance, dest: Path, command, env: dict) -> Path:
    """command(target) writes the backup at target: dest itself, or with
    run_as a file of its private folder carried to dest."""
    if instance.run_as is None:
        await run(job, command(dest), env=env)
        return dest
    async with _private_folder(instance, "omniacli-backup-") as folder:
        staged = folder / dest.name
        await run(job, as_user(instance.run_as, command(staged), env), env=env)
        # through <file>.part: a cut copy leaves nothing
        part = dest.with_name(dest.name + ".part")
        try:
            with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
                await run(job, _python_as(instance, _FILE_TO_STDOUT, staged), stdout=out)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
    return dest


def backup_major(backup_zip: Path) -> int | None:
    """The major Odoo version of the database in a backup zip, from the
    manifest.json click-odoo-backupdb (and Odoo's own backup) writes in it;
    None when it has none."""
    try:
        with zipfile.ZipFile(backup_zip) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if not isinstance(manifest, dict):
        return None
    match = re.match(r"(?:saas~)?(\d+)", str(manifest.get("major_version") or manifest.get("version") or ""))
    return int(match.group(1)) if match else None


def backup_problem(backup_zip: Path) -> str | None:
    """Why Odoo cannot restore this file, told before its restore drops the
    database (Odoo reads only dump.sql and filestore/ of a zip)."""
    try:
        with zipfile.ZipFile(backup_zip) as zf:
            found = "dump.sql" in zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        return f"{backup_zip.name} is not a zip that can be read: {exc}"
    return None if found else f"{backup_zip.name} is not an Odoo backup: it has no dump.sql"


async def restore(job: Job, instance: Instance, backup_zip: Path, neutralize: bool) -> None:
    """click-odoo-restoredb of a backup zip into the instance's database: the
    database and its filestore are dropped first (--force), the copy gets a
    new dbuuid (--copy), and with neutralize Odoo disables its crons and
    outgoing mails (--neutralize, Odoo 16 and later).

    Odoo unpacks the zip (dump.sql, filestore) in a folder of tempfile
    first (Odoo 10 to 19): TMPDIR puts it on the disk of the backups, not
    in /tmp (the system's disk, or memory). Without run_as a folder 700
    beside the zip (restore_temp), with run_as the private folder of the
    staged zip; removed at the end, failed or not."""
    opts = instance.options
    cmd = [opts.venv_bin / "click-odoo-restoredb", "-c", opts.odoo_config, "--copy", "--force"]
    if neutralize:
        cmd.append("--neutralize")
    env = odoo_env(instance)
    job.log(f"--- restore {backup_zip.name} into database {opts.database}"
            + (" (neutralized)" if neutralize else ""))
    if instance.run_as is None:
        with tempfile.TemporaryDirectory(prefix=RESTORE_TEMP, dir=backup_zip.parent) as unpacked:
            job.log(f"the backup is unpacked in {unpacked} first")
            await run(job, [*cmd, opts.database, backup_zip], env={**env, "TMPDIR": unpacked})
        return
    # run_as cannot read the backup folder: a copy in its private folder,
    # unpacked there too
    async with _private_folder(instance, "omniacli-restore-") as folder:
        staged = folder / backup_zip.name
        with open(backup_zip, "rb") as source:
            await run(job, _python_as(instance, _FILE_FROM_STDIN, staged), stdin=source)
        env = {**env, "TMPDIR": str(folder)}
        job.log(f"the backup is unpacked in {folder} first")
        await run(job, as_user(instance.run_as, [*cmd, opts.database, staged], env), env=env)


async def load_sql(job: Job, instance: Instance, dump: Path, neutralize: bool, base_url: str | None) -> int:
    """load_sql.py: the plain SQL dump replaces the instance's database
    (dropped and made again by Odoo), the filestore is left as it is; with
    neutralize Odoo's neutralize_database runs; then a new database.uuid and
    database.secret and web.base.url = base_url (None: Odoo's default). The
    dump reaches the script on its standard input, opened here: run_as
    needs no access to the backup folder and no copy is made. The number
    of the SQL errors psql went past (each is in the log)."""
    opts = instance.options
    env = odoo_env(instance)
    job.log(f"--- load the SQL dump {dump.name} into database {opts.database}"
            + (" (neutralized)" if neutralize else "") + ": the filestore is not touched")
    lines: list[str] = []

    class Seen:
        """The job, keeping the lines the script prints too."""
        def log(self, line: str) -> None:
            lines.append(line)
            job.log(line)

    with _staged_script(LOAD_SQL_SCRIPT, "omniacli-load-sql-") as script:
        cmd = [opts.venv_bin / "python", script, opts.odoo_config, opts.database, "1" if neutralize else "0",
               base_url or "", str(dump.stat().st_size)]
        with open(dump, "rb") as source:
            # no timeout, as the restore of a zip: a big dump takes its time
            await run(Seen(), as_user(instance.run_as, cmd, env), env=env, stdin=source)
    found = [line for line in lines if line.startswith(SQL_ERRORS_MARK)]
    return int(found[-1][len(SQL_ERRORS_MARK):]) if found else 0



async def reset_passwords(job: Job, instance: Instance, major: int) -> None:
    """click-odoo runs reset_passwords.py on the instance's database, as the
    user the instance runs as: every user's password becomes admin."""
    opts = instance.options
    job.log(f"--- every user's password in database {opts.database} set to admin")
    env = odoo_env(instance)
    # a copy of the script any user may read, in a folder only OmniaCLI writes to
    staging = Path(tempfile.mkdtemp(prefix="omniacli-passwords-", dir=BACKUP_STAGING))
    try:
        staging.chmod(0o755)
        script = staging / PASSWORDS_SCRIPT.name
        shutil.copyfile(PASSWORDS_SCRIPT, script)
        script.chmod(0o644)
        cmd = [opts.venv_bin / "click-odoo", "-c", opts.odoo_config, "-d", opts.database, script, str(major)]
        await run(job, as_user(instance.run_as, cmd, env), env=env)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def share_filestore(job: Job, instance: Instance, src: Path, dst: Path, check: bool = False) -> None:
    """Hard link the filestore src (production's) into dst, the filestore of
    instance (test), with the venv's python and as the user instance runs as
    (see link_filestore.py); with check, only prove it can be done."""
    job.log(f"--- {'check: ' if check else ''}filestore {dst} shared with {src} (hard links)")
    # a copy of the script any user may read, in a folder only OmniaCLI writes to
    staging = Path(tempfile.mkdtemp(prefix="omniacli-link-", dir=BACKUP_STAGING))
    try:
        staging.chmod(0o755)
        script = staging / LINK_SCRIPT.name
        shutil.copyfile(LINK_SCRIPT, script)
        script.chmod(0o644)
        cmd = [instance.options.venv_bin / "python", script, "check" if check else "link", src, dst]
        await run(job, as_user(instance.run_as, cmd))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
