# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find the Odoo instances of this machine, for a machine where OmniaCLI is
new: the odoo-bin processes running, and the systemd units that start one
(stopped ones too).

Checked on 2026-10-25 on a Debian 11 workstation: /proc/<pid>/cmdline is
readable by every user and gives the venv's python, odoo-bin (often relative:
"odoo-bin", started from its folder), -c and the options given on the command
line, which win over the config; /proc/<pid>/exe and cwd may not be readable
even for the same user, so a relative odoo-bin is looked for in the
repositories; /proc/<pid>/cgroup names the unit the process runs in, which is
not its own when it was started from a shell of another service (an Odoo
started from a shell of OmniaCLI showed omniacli.service): the unit counts
only when its MainPID is that process. An Odoo in a Docker container shows
/usr/bin/odoo without -c, in a docker-<id>.scope cgroup. Odoo starts its
gevent server as a child of itself ([python, odoo-bin, "gevent", ...], Odoo
18 service/server.py) and its workers are forks: only the top process counts.
"""

from __future__ import annotations

import os
import pwd
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..plugin_api import SERVER_NAME, ConfigError, Discovered, Instance, capture, system_user
from . import odooconf
from .ops import locate_odoo_bin, odoo_major

PROC = Path("/proc")
# the scripts that start Odoo: odoo-bin from git, odoo of the Debian package,
# openerp-server and odoo.py of the old versions
SCRIPTS = ("odoo-bin", "odoo", "openerp-server", "odoo.py")
CONTAINER = re.compile(r"docker|containerd|libpod|kubepods|/lxc")
# the options of odoo-bin read here (Odoo 18 tools/config.py; xmlrpc-port and
# longpolling-port before Odoo 11 and 16) -> key
OPTIONS = {
    "-c": "config", "--config": "config",
    "-d": "database", "--database": "database",
    "-p": "http_port", "--http-port": "http_port", "--xmlrpc-port": "http_port",
    "--gevent-port": "gevent_port", "--longpolling-port": "gevent_port",
    "--logfile": "logfile",
    "--addons-path": "addons_path",
    "--workers": "workers",
    "--db-filter": "dbfilter",
}
SHORT = {name for name in OPTIONS if not name.startswith("--")}
_EXEC_ARGV = re.compile(r"argv\[\]=(.*?) ;")


@dataclass
class Started:
    """One Odoo: a process, a unit, or both."""
    argv: list[str]
    pid: int | None = None
    user: str | None = None
    cwd: str | None = None
    # the systemd unit it runs as (its own), and that unit's state
    unit: str | None = None
    state: str = "running"
    notes: list[str] = field(default_factory=list)
    skip: str | None = None


def script_index(argv: list[str]) -> int | None:
    """Where the Odoo script is in argv: after a python and its options, or
    first; None when argv does not start Odoo."""
    if not argv:
        return None
    first = Path(argv[0]).name
    if first in SCRIPTS:
        return 0
    if not first.startswith("python"):
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        # python's own options; -m, -c and -X take a value
        i += 2 if argv[i] in ("-m", "-c", "-X", "-W") else 1
    if i < len(argv) and Path(argv[i]).name in SCRIPTS:
        return i
    return None


def parse_args(args: list[str]) -> dict:
    """The options of odoo-bin that tell where an instance is (the args
    after the script); "command" is its subcommand (server when none)."""
    found: dict = {"command": "server"}
    if args and not args[0].startswith("-"):
        found["command"] = args[0]
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg:
            name, value = arg.split("=", 1)
            if name in OPTIONS:
                found[OPTIONS[name]] = value
        elif arg in OPTIONS and i + 1 < len(args):
            found[OPTIONS[arg]] = args[i + 1]
            i += 1
        elif arg[:2] in SHORT and len(arg) > 2:
            # -c/etc/odoo.conf
            found[OPTIONS[arg[:2]]] = arg[2:]
        i += 1
    return found


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _argv(proc: Path, pid: str) -> list[str]:
    raw = _read(proc / pid / "cmdline")
    return raw.rstrip("\0").split("\0") if raw else []


def _status(proc: Path, pid: str) -> dict:
    found = {}
    for line in (_read(proc / pid / "status") or "").splitlines():
        key, _, value = line.partition(":")
        found[key] = value.split()
    return found


def _user(uid: str) -> str:
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        return uid


def unit_of(cgroup: str) -> tuple[str | None, str | None]:
    """(unit, why it is not usable) from /proc/<pid>/cgroup: the .service the
    process runs in (cgroup v2 0::, or v1 name=systemd)."""
    path = None
    for line in cgroup.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[0] == "0" and parts[1] == "" or parts[1] == "name=systemd"):
            path = parts[2]
    if not path:
        return None, None
    if CONTAINER.search(path):
        return None, "in a container: not an instance of this machine"
    unit = next((p for p in reversed(path.split("/")) if p.endswith(".service")), None)
    if unit and "/user@" in path:
        return None, f"in the systemd user unit {unit}: not supported yet"
    return unit, None


def processes(proc: Path = PROC) -> list[Started]:
    """The Odoo processes running: the top one of each (not its workers nor
    its gevent server)."""
    found, argvs, parents = [], {}, {}
    try:
        entries = [e.name for e in proc.iterdir() if e.name.isdigit()]
    except OSError:
        return []
    for pid in entries:
        argv = _argv(proc, pid)
        if script_index(argv) is not None:
            argvs[pid] = argv
            parents[pid] = (_status(proc, pid).get("PPid") or [""])[0]
    for pid, argv in argvs.items():
        if parents[pid] in argvs:
            continue
        status = _status(proc, pid)
        uid = (status.get("Uid") or [""])[0]
        try:
            cwd = os.readlink(proc / pid / "cwd")
        except OSError:
            cwd = None
        item = Started(argv=argv, pid=int(pid), user=_user(uid) if uid else None, cwd=cwd)
        item.unit, item.skip = unit_of(_read(proc / pid / "cgroup") or "")
        found.append(item)
    return found


def _show_blocks(text: str) -> list[dict]:
    blocks, current = [], {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
            current = {}
            continue
        key, _, value = line.partition("=")
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


async def units() -> list[dict]:
    """Every systemd service unit with Id, ExecStart's argv, User,
    WorkingDirectory, MainPID and ActiveState; empty without systemd."""
    code, out = await capture(["systemctl", "list-unit-files", "--type=service", "--no-legend",
                               "--plain", "--no-pager"], timeout=30)
    if code:
        return []
    names = [line.split()[0] for line in out.splitlines() if line.split()]
    # templates (name@.service) are not units of their own
    names = [n for n in names if n.endswith(".service") and not n.endswith("@.service")]
    if not names:
        return []
    code, out = await capture(["systemctl", "show", "-p", "Id", "-p", "ExecStart", "-p", "User",
                               "-p", "WorkingDirectory", "-p", "MainPID", "-p", "ActiveState",
                               "--", *names], timeout=60)
    if code:
        return []
    found = []
    for block in _show_blocks(out):
        match = _EXEC_ARGV.search(block.get("ExecStart", ""))
        block["argv"] = match.group(1).split() if match else []
        found.append(block)
    return found


def _realpath(path: str | None) -> str | None:
    return os.path.realpath(path) if path else None


def merge(running: list[Started], unit_list: list[dict]) -> list[Started]:
    """The processes, each given its unit when it is that unit's main
    process, then the units starting Odoo that run no process found."""
    by_name = {u.get("Id"): u for u in unit_list}
    used = set()
    for item in running:
        if item.unit is None or item.skip:
            continue
        unit = by_name.get(item.unit)
        if unit is not None and unit.get("MainPID") == str(item.pid):
            used.add(item.unit)
            # where a relative odoo-bin or -c is, when /proc does not tell
            item.cwd = item.cwd or unit.get("WorkingDirectory") or None
        else:
            item.notes.append(f"runs inside the unit {item.unit}, which is not its own (started by hand "
                              "from a shell of that service?): set its service by hand")
            item.unit = None
    found = list(running)
    for unit in unit_list:
        name = unit.get("Id")
        if name in used or script_index(unit.get("argv") or []) is None:
            continue
        if unit.get("ActiveState") == "active":
            # its process was not found as an Odoo (a wrapper script...)
            state = "running"
        else:
            state = unit.get("ActiveState") or "inactive"
        found.append(Started(argv=unit["argv"], user=unit.get("User") or "root",
                             cwd=unit.get("WorkingDirectory") or None, unit=name, state=state))
    return found


def _name(text: str) -> str:
    """A server name from a database or a unit name."""
    text = re.sub(r"\.service$", "", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return text if SERVER_NAME.fullmatch(text or "") else "odoo"


async def describe(item: Started, known: list[Instance]) -> Discovered:
    """What OmniaCLI would save for one Odoo found, and what it could not find."""
    notes = list(item.notes)
    at = script_index(item.argv) or 0
    args = parse_args(item.argv[at + 1:])
    label = f"process {item.pid}" if item.pid else f"unit {item.unit}"
    if item.pid and item.unit:
        label += f", unit {item.unit}"
    facts = [("Command", " ".join(item.argv)), ("User", item.user or "?"),
             ("State", item.state)]
    if item.skip:
        return Discovered(key=label, label=label, name="odoo", instance={}, facts=facts,
                          notes=notes, skip=item.skip)
    if any("$" in a for a in item.argv):
        notes.append("the command uses variables (an EnvironmentFile?): what they hold is not read here")
    config = args.get("config")
    if not config:
        home = None
        try:
            home = pwd.getpwnam(item.user).pw_dir if item.user else None
        except KeyError:
            pass
        config = os.path.join(home, ".odoorc") if home else None
        notes.append(f"no -c: Odoo reads {config or '~/.odoorc of its user'}")
    elif not os.path.isabs(config):
        config = os.path.join(item.cwd, config) if item.cwd else None
        if config is None:
            notes.append(f"-c {args['config']}: relative, and its working directory cannot be read")

    # odoo-bin: absolute, against the working directory, or in a repository
    script = item.argv[at]
    odoo_bin = None
    if os.path.isabs(script):
        odoo_bin = Path(script)
    elif item.cwd:
        odoo_bin = Path(item.cwd) / script

    found = {"database": None, "log_file": None, "repositories": [], "notes": []}
    if config:
        try:
            found = await odooconf.read_odoo_config(config)
        except ConfigError as exc:
            notes.append(f"{exc}" + (f": the user running OmniaCLI ({system_user()}) must read it "
                                     f"(e.g. sudo adduser {system_user()} {item.user})"
                                     if "Permission denied" in str(exc) and item.user else ""))
    notes.extend(n for n in found["notes"] if not n.startswith("no logfile"))
    repositories = list(found["repositories"])
    if args.get("addons_path"):
        # the command line's addons_path wins over the config's
        repositories = await odooconf.addons_repositories(args["addons_path"], notes)
    if odoo_bin is None or not odoo_bin.is_file():
        if odoo_bin is not None:
            notes.append(f"{odoo_bin} not found")
        odoo_bin = locate_odoo_bin(None, [Path(r["path"]) for r in repositories])
        if odoo_bin is None:
            notes.append(f"{script}: relative and in no repository of the addons_path: set odoo-bin by hand")
    if odoo_bin is not None and odoo_bin.is_file():
        major = odoo_major(odoo_bin)
        facts.append(("odoo-bin", f"{odoo_bin}" + (f" (Odoo {major})" if major else
                                                    " (no odoo/release.py next to it: version unknown)")))
        if not any(odoo_bin.parent == Path(r["path"]) for r in repositories):
            # the Odoo repository itself, when the addons_path names only its addons
            repo = await odooconf.repository_of(odoo_bin.parent, notes)
            if repo is not None and not any(r["path"] == repo["path"] for r in repositories):
                repositories.insert(0, repo)
            elif repo is None:
                notes.append(f"{odoo_bin} is in no git repository (installed by a package?): "
                             "OmniaCLI updates only what is in git")

    # the python: the venv's bin folder when it is a venv
    python = item.argv[0] if at else None
    venv_bin = None
    if python and os.path.isabs(python):
        venv_bin = Path(python).parent
        if not (venv_bin.parent / "pyvenv.cfg").is_file():
            notes.append(f"{python} is not in a venv: click-odoo-contrib must be installed for it")
    elif at:
        notes.append(f"python {python}: no path, set the venv's bin folder by hand")
    else:
        notes.append(f"{script} started without a python on the command line: set the venv's bin folder by hand")

    database = found["database"]
    if args.get("database"):
        names = [n for n in args["database"].split(",") if n]
        database = names[0] if len(names) == 1 else None
        if len(names) != 1:
            notes.append(f"-d {args['database']}: choose the database")
    log_file = args.get("logfile") or found["log_file"]
    if not log_file:
        notes.append("no logfile: Odoo logs to its standard output (the journal of its unit): "
                     "OmniaCLI needs a log file, set logfile in the Odoo config")

    me = system_user()
    run_as = item.user if item.user and item.user != me else None
    if run_as == "root":
        notes.append("Odoo runs as root: run_as root lets OmniaCLI run its venv as root")
    if item.unit:
        restart = {"type": "systemd", "service": item.unit, "sudo": True}
    elif not run_as:
        restart = {"type": "process"}
        notes.append("started by hand: OmniaCLI may start it itself (restart type process) once it is "
                     "stopped, or set its systemd service")
    else:
        restart = {"type": "systemd", "service": "", "sudo": True}
        notes.append(f"started by hand as {run_as}: set its systemd service")

    instance = {"database": database or "", "odoo_config": config or "",
                "venv_bin": str(venv_bin) if venv_bin else "", "update_modules": True,
                "restart": restart, "repositories": repositories}
    if odoo_bin is not None and odoo_bin.is_file() and not any(
            (Path(r["path"]) / "odoo-bin") == odoo_bin for r in repositories):
        instance["odoo_bin"] = str(odoo_bin)
    if restart["type"] != "process":
        instance["log_file"] = log_file or ""
    elif args.get("http_port"):
        instance["http_port"] = args["http_port"]
    if run_as:
        instance["run_as"] = run_as
    for key, label_ in (("database", "Database"), ("http_port", "HTTP port"),
                        ("gevent_port", "Gevent port")):
        if key == "database" and database:
            facts.append((label_, database))
        elif args.get(key):
            facts.append((label_, f"{args[key]} (command line)"))
    if config:
        facts.append(("Odoo config", config))
    if venv_bin:
        facts.append(("Venv bin", str(venv_bin)))

    real = _realpath(config)
    known_as = next((i.key for i in known
                     if _realpath(str(getattr(i.options, "odoo_config", "") or "")) == real), None) if real else None
    if args["command"] != "server":
        notes.append(f"odoo-bin {args['command']}: not the server")
    return Discovered(key=real or label, label=label, name=_name(database or item.unit or "odoo"),
                      instance=instance, facts=facts, notes=notes, known=known_as,
                      skip=None if args["command"] == "server" else f"odoo-bin {args['command']}")


async def discover(known: list[Instance], proc: Path = PROC) -> list[Discovered]:
    """Every Odoo found, once per config file (a process and a unit that
    is not its own may both name it)."""
    result, keys = [], set()
    for item in merge(processes(proc), await units()):
        found = await describe(item, known)
        if found.key in keys:
            continue
        keys.add(found.key)
        result.append(found)
    return result
