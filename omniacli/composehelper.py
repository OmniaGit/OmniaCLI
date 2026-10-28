# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The root helper of the instances in a Docker Compose project.

    omniacli compose-helper <project folder> <action> [arguments]

OmniaCLI runs every docker compose command of an instance with restart type
compose through this helper, with sudo, one sudoers line per project:

    omniacli ALL=(root) NOPASSWD: /opt/omniacli/venv/bin/omniacli compose-helper /srv/odooplm-19 *

Whoever may change what docker compose reads in a project is root on the
machine: docker runs as root what the files say, and one compose.override.yaml
mounting / into a container is enough. So before every action the helper,
as root, refuses a project that the user who called sudo could change:

- the folder, and every folder above it up to / (a writable parent may put
  another folder in its place; a sticky one, as /tmp, only when the folder
  is that user's), must not be writable by that user;
- nor the compose files docker compose reads by itself (compose.yaml,
  compose.yml, docker-compose.yaml, docker-compose.yml, and their
  .override.yaml/.yml) and .env; none of them may be a symbolic link;
- .env may not set COMPOSE_FILE, COMPOSE_PATH_SEPARATOR or
  COMPOSE_PROJECT_DIRECTORY, which make docker compose read other files.

A file is writable by a user who owns it (an owner can change its mode), or
through the group or other write bit that applies to the user. ACLs are not
read: a project secured with chown root and chmod go-w has none that matter.

The helper takes a fixed set of actions with checked arguments, never a
command of the caller's, and runs docker with an environment of its own.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# the files docker compose reads in a project's folder by itself
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDE_FILES = ("compose.override.yaml", "compose.override.yml",
                  "docker-compose.override.yaml", "docker-compose.override.yml")
# what in .env makes docker compose read files other than the folder's
ENV_REFUSED = ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "COMPOSE_PROJECT_DIRECTORY")
# the environment docker runs with: none of the caller's
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
# what each action runs: start applies a changed .env or image (up -d makes
# again what changed), state lists every container, stopped ones too
ACTIONS = {
    "start": [["up", "-d"]],
    "stop": [["stop"]],
    "restart": [["up", "-d"], ["restart"]],
    "state": [["ps", "-a", "--format", "json"]],
}
_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class HelperError(Exception):
    pass


def caller() -> tuple[str, int, list[int]]:
    """(name, uid, groups) of the user the helper acts for: the one who
    called sudo (SUDO_USER, which sudo sets itself), else the user running it."""
    name = os.environ.get("SUDO_USER") if os.geteuid() == 0 else None
    try:
        entry = pwd.getpwnam(name) if name else pwd.getpwuid(os.getuid())
    except KeyError as exc:
        raise HelperError(f"unknown user {name!r}") from exc
    return entry.pw_name, entry.pw_uid, os.getgrouplist(entry.pw_name, entry.pw_gid)


def _writable(st: os.stat_result, uid: int, groups: list[int]) -> bool:
    if st.st_uid == uid:
        return True  # its owner may chmod it
    if st.st_gid in groups:
        return bool(st.st_mode & stat.S_IWGRP)
    return bool(st.st_mode & stat.S_IWOTH)


def _fix(path: Path) -> str:
    return f"chown root: {path}; chmod go-w {path}"


def problems(project: Path, uid: int, groups: list[int]) -> list[str]:
    """Why the project is not safe from the user (uid, groups); [] when it is.
    Root is not checked: it may do all this anyway."""
    if uid == 0:
        return []
    found = []
    if not project.is_absolute():
        return [f"{project} is not an absolute path"]
    if Path(os.path.realpath(project)) != project:
        return [f"{project} goes through a symbolic link: give its real path "
                f"{os.path.realpath(project)}"]
    try:
        chain = [project, *project.parents]
        stats = {path: path.lstat() for path in chain}
    except OSError as exc:
        return [f"{exc.filename}: {exc.strerror}"]
    if not stat.S_ISDIR(stats[project].st_mode):
        return [f"{project} is not a folder"]
    # every folder that holds one of the chain may replace it
    for child, parent in zip(chain, chain[1:]):
        st = stats[parent]
        if not _writable(st, uid, groups):
            continue
        if st.st_mode & stat.S_ISVTX and stats[child].st_uid != uid:
            continue  # sticky: only the child's owner may rename it
        found.append(f"{parent} is writable by the user: it may replace {child}; {_fix(parent)}")
    if _writable(stats[project], uid, groups):
        found.append(f"{project} is writable by the user: it may add a compose override; {_fix(project)}")
    for name in (*COMPOSE_FILES, *OVERRIDE_FILES, ".env"):
        path = project / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            found.append(f"{path}: {exc.strerror}")
            continue
        if stat.S_ISLNK(st.st_mode):
            found.append(f"{path} is a symbolic link: docker compose would read what it points to")
        elif _writable(st, uid, groups):
            found.append(f"{path} is writable by the user; {_fix(path)}")
    found += env_problems(project / ".env")
    if not any((project / name).is_file() for name in COMPOSE_FILES):
        found.append(f"{project} holds no {', '.join(COMPOSE_FILES)}")
    return found


def env_problems(env_file: Path) -> list[str]:
    try:
        text = env_file.read_text(errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"{env_file}: {exc.strerror}"]
    keys = {m.group(1) for m in map(_ENV_LINE.match, text.splitlines()) if m}
    return [f"{env_file} sets {key}: docker compose would read other files; remove it"
            for key in ENV_REFUSED if key in keys]


def compose(project: Path, *args: str) -> list[str]:
    docker = shutil.which("docker", path=ENV["PATH"])
    if docker is None:
        raise HelperError("docker is not installed")
    return [docker, "compose", "--project-directory", str(project), *args]


def run(argv: list[str]) -> int:
    """compose-helper <project> <action>: the exit code of the action."""
    if len(argv) != 2:
        raise HelperError("usage: compose-helper <project folder> <action>")
    project, action = Path(argv[0]), argv[1]
    if action not in ACTIONS:
        raise HelperError(f"unknown action {action!r}: {', '.join(ACTIONS)}")
    name, uid, groups = caller()
    found = problems(project, uid, groups)
    if found:
        raise HelperError(f"{project} is not safe from user {name}:\n  " + "\n  ".join(found))
    for args in ACTIONS[action]:
        code = subprocess.run(compose(project, *args), env=ENV, cwd=project).returncode
        if code:
            return code
    return 0


def main(argv: list[str]) -> int:
    try:
        return run(argv)
    except HelperError as exc:
        print(f"compose-helper: {exc}", file=sys.stderr)
        return 2
