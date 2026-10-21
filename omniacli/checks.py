"""The checks "Test & activate" runs before a server goes live.

Every check looks at the real thing (the file, the database, the remote, the
service, sudo) as the user running OmniaCLI, so a server that passes can be
operated by OmniaCLI.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import ops
from .config import Instance, Server

ERROR, WARNING = "error", "warning"
GIT_TIMEOUT = 30
PYTHON_TIMEOUT = 60

# Connects to the instance's database with the settings of its Odoo config,
# run by the instance's own python (which has psycopg2, an Odoo requirement).
_DB_PROBE = r"""
import configparser, sys
conf, dbname = sys.argv[1], sys.argv[2]
parser = configparser.ConfigParser(interpolation=None)
parser.read(conf)
opt = lambda key: (parser.get("options", key, fallback="") or "").strip()
value = lambda key: None if opt(key) in ("", "False", "None") else opt(key)
import psycopg2
cn = psycopg2.connect(dbname=dbname, host=value("db_host"), port=value("db_port"),
                      user=value("db_user"), password=value("db_password"),
                      connect_timeout=10)
cr = cn.cursor()
cr.execute("SELECT latest_version FROM ir_module_module WHERE name = 'base'")
row = cr.fetchone()
print("base module %s" % (row[0] if row else "missing"))
"""


@dataclass
class Check:
    target: str
    name: str
    ok: bool
    detail: str
    level: str = ERROR

    def to_dict(self) -> dict:
        return asdict(self)


def passed(checks: list[Check]) -> bool:
    """True when no check of level error failed (warnings do not block)."""
    return all(c.ok or c.level == WARNING for c in checks)


async def _instance_checks(instance: Instance) -> list[Check]:
    target = instance.role
    checks: list[Check] = []

    def add(name, ok, detail, level=ERROR):
        checks.append(Check(target, name, ok, detail, level))

    # Odoo config and addons path
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(instance.odoo_config, encoding="utf-8") as fh:
            parser.read_file(fh)
        add("Odoo config", True, f"{instance.odoo_config} readable")
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        add("Odoo config", False, f"{instance.odoo_config}: {exc}")
    raw = parser.get("options", "addons_path", fallback="") if parser.has_section("options") else ""
    missing = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip() and not Path(p.strip()).is_dir()]
    if raw:
        add("Addons path", not missing,
            "every folder exists" if not missing else f"missing: {', '.join(missing)}", WARNING)

    # log file
    process = instance.restart.type == "process"
    if process:
        folder = instance.log_file.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder):
                pass
            add("Log folder", True, f"{folder} writable: Odoo's output goes to {instance.log_file}")
        except OSError as exc:
            add("Log folder", False, f"{folder}: {exc}")
    else:
        readable = os.access(instance.log_file, os.R_OK)
        add("Log file", readable,
            f"{instance.log_file} readable" if readable
            else f"{instance.log_file} is not readable: the log page cannot show it", WARNING)

    # click-odoo scripts and the instance's python
    for script in ("click-odoo-update", "click-odoo-backupdb"):
        path = instance.venv_bin / script
        ok = os.access(path, os.X_OK)
        add(script, ok, str(path) if ok else
            f"{path} not found: pip install click-odoo-contrib in that venv")
    code, out = await ops.capture(
        [instance.venv_bin / "python", "-c",
         "import click_odoo, odoo; print('Odoo', odoo.release.version)"],
        timeout=PYTHON_TIMEOUT,
        env=ops.odoo_env(instance),
    )
    add("Odoo importable by click-odoo", code == 0,
        out.splitlines()[-1] if out else f"exit code {code}")

    # database
    code, out = await ops.capture(
        [instance.venv_bin / "python", "-c", _DB_PROBE, instance.odoo_config, instance.database],
        timeout=PYTHON_TIMEOUT,
    )
    add("Database", code == 0,
        f"{instance.database}: {out.splitlines()[-1] if out else ''}" if code == 0
        else f"{instance.database}: {out.splitlines()[-1] if out else f'exit code {code}'}")

    # odoo-bin, only needed to update chosen modules
    odoo_bin = ops.find_odoo_bin(instance)
    if odoo_bin is not None and odoo_bin.is_file():
        major = ops.odoo_major(odoo_bin)
        add("odoo-bin", True, f"{odoo_bin}" + (f" (Odoo {major})" if major else ""), WARNING)
    elif process:
        add("odoo-bin", False, "not found: OmniaCLI cannot start Odoo; set odoo_bin")
    else:
        add("odoo-bin", False,
            "not found: the update of chosen modules will not work; set odoo_bin", WARNING)

    # repositories
    for repo in instance.repositories:
        name = f"Repository {repo.name}"
        if not repo.path.is_dir():
            add(name, False, f"{repo.path} does not exist")
            continue
        code, branch = await ops.capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo.path)
        if code:
            add(name, False, branch.splitlines()[0] if branch else "not a git repository")
            continue
        problems = []
        if branch != repo.branch:
            problems.append(f"on branch '{branch}', expected '{repo.branch}'")
        _, changes = await ops.capture(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo.path)
        if changes and instance.keeps_local_changes:
            add(f"{name}: local changes", False,
                f"{len(changes.splitlines())} uncommitted change(s), kept: a test instance without "
                "auto update; a pull stops only if it would overwrite them, a merge to "
                "production refuses them", WARNING)
        elif changes:
            problems.append(f"{len(changes.splitlines())} uncommitted change(s)")
        code, out = await ops.capture(
            ["git", "ls-remote", "--exit-code", "--heads", repo.remote, repo.branch],
            cwd=repo.path, timeout=GIT_TIMEOUT,
        )
        if code:
            problems.append(
                f"remote {repo.remote} branch {repo.branch} not reachable"
                + (f": {out.splitlines()[-1]}" if out else ""))
        add(name, not problems,
            "; ".join(problems) if problems else
            f"{repo.path} on {repo.branch}, clean, {repo.remote}/{repo.branch} reachable")

    # service and the right to restart it
    restart = instance.restart
    if process:
        own = ops.managed_pid(instance)
        others = ops.foreign_pids(instance, own)
        if others:
            add("Odoo process", False,
                f"running with {instance.odoo_config} but not started by OmniaCLI "
                f"(pid {', '.join(map(str, others))}): Restart refuses to start a second one")
        elif own is not None:
            add("Odoo process", True, f"started by OmniaCLI, pid {own}")
        else:
            add("Odoo process", False, "not running: Restart starts it", WARNING)
        return checks
    if restart.type == "systemd":
        code, state = await ops.capture(
            ["systemctl", "show", "-p", "LoadState", "--value", restart.service])
        add("Service", code == 0 and state == "loaded",
            f"systemd unit {restart.service}: {state or 'unknown'}")
    else:
        script = Path(f"/etc/init.d/{restart.service}")
        add("Service", os.access(script, os.X_OK),
            f"{script} {'exists' if os.access(script, os.X_OK) else 'not found or not executable'}")
    command = restart.command()
    if restart.sudo:
        # sudo -n -l <command>: 0 when sudoers lets this user run exactly it
        # without a password
        code, _ = await ops.capture(["sudo", "-n", "-l", *command[2:]])
        executable = shutil.which(command[2]) or command[2]
        add("Restart permission", code == 0,
            "sudo allows it without a password" if code == 0 else
            "sudo refuses it or asks a password; add to /etc/sudoers.d/omniacli: "
            f"{_user()} ALL=(root) NOPASSWD: {' '.join([executable, *command[3:]])}")
    else:
        add("Restart permission", True,
            f"runs '{' '.join(command)}' without sudo: not tried, it would restart the service",
            WARNING)
    return checks


def _user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER", "omniacli")


async def check_server(server: Server, backup_dir: Path) -> list[Check]:
    if server.error:
        # a draft saved incomplete: nothing to check until it is completed
        return [Check("server", "Settings", False, f"incomplete: {server.error}")]
    checks: list[Check] = []
    prod, test = server.production, server.test
    if prod and test:
        shared = {str(r.path) for r in prod.repositories} & {str(r.path) for r in test.repositories}
        checks.append(Check("server", "Separate working copies", not shared,
                            "production and test use different folders" if not shared
                            else f"production and test share {', '.join(sorted(shared))}"))
        same_db = prod.database == test.database
        checks.append(Check("server", "Separate databases", not same_db,
                            f"production {prod.database}, test {test.database}" if not same_db
                            else f"production and test both use {prod.database}"))
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=backup_dir):
            pass
        checks.append(Check("server", "Backup folder", True, f"{backup_dir} writable"))
    except OSError as exc:
        checks.append(Check("server", "Backup folder", False, f"{backup_dir}: {exc}"))
    results = await asyncio.gather(*(_instance_checks(i) for i in server.instances.values()))
    for result in results:
        checks.extend(result)
    return checks
