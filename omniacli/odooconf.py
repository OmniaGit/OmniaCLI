"""Read an instance's settings from its Odoo config file, for the server form.

From the config: the database (db_name, or an exact ^name$ dbfilter), the log
file and the git repositories that hold the addons_path folders, with the
branch each one is on and the remote that branch tracks.
"""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

from . import ops
from .config import ConfigError

_EXACT_DBFILTER = re.compile(r"\^([A-Za-z0-9_.\-]+)\$")


def _option(parser: configparser.ConfigParser, key: str) -> str:
    value = (parser.get("options", key, fallback="") or "").strip()
    # Odoo writes unset options as False or None
    return "" if value in ("False", "None") else value


async def _repository(top: Path, notes: list[str]) -> dict:
    _, branch = await ops.capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=top)
    if branch == "HEAD":
        notes.append(f"{top}: detached HEAD, set the branch by hand")
        branch = ""
    remote = ""
    if branch:
        _, remote = await ops.capture(["git", "config", "--get", f"branch.{branch}.remote"], cwd=top)
    if not remote:
        _, remotes = await ops.capture(["git", "remote"], cwd=top)
        names = remotes.split()
        remote = "origin" if "origin" in names else (names[0] if names else "")
        if not remote:
            notes.append(f"{top}: no git remote")
    return {"name": top.name, "path": str(top), "remote": remote or "origin", "branch": branch}


async def read_odoo_config(path: str | Path) -> dict:
    """{database, log_file, repositories, notes}; database and log_file are
    None when the config does not give them, notes says why."""
    path = Path(path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except configparser.MissingSectionHeaderError:
        parser.clear()
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {str(exc).splitlines()[0]}") from exc
    if not parser.has_section("options"):
        raise ConfigError(f"{path}: no [options] section, not an Odoo config")
    notes: list[str] = []

    database = None
    names = [n.strip() for n in _option(parser, "db_name").split(",") if n.strip()]
    if len(names) == 1:
        database = names[0]
    elif names:
        notes.append(f"db_name lists several databases ({', '.join(names)}): choose one")
    else:
        match = _EXACT_DBFILTER.fullmatch(_option(parser, "dbfilter"))
        if match:
            database = match.group(1)
        else:
            notes.append("no db_name and no ^name$ dbfilter: set the database by hand")

    log_file = _option(parser, "logfile") or None
    if log_file is None:
        notes.append("no logfile: Odoo logs to its standard output, set the log file by hand")

    repositories: list[dict] = []
    tops: set[str] = set()
    for raw in _option(parser, "addons_path").replace("\n", ",").split(","):
        folder = os.path.expandvars(os.path.expanduser(raw.strip()))
        if not folder:
            continue
        if not os.path.isabs(folder):
            notes.append(f"addons_path {folder}: relative, Odoo resolves it against "
                         "its working directory; skipped")
            continue
        if not os.path.isdir(folder):
            notes.append(f"addons_path {folder}: no such folder")
            continue
        code, top = await ops.capture(["git", "rev-parse", "--show-toplevel"], cwd=Path(folder))
        if code:
            notes.append(f"addons_path {folder}: not inside a git repository")
            continue
        if top not in tops:
            tops.add(top)
            repositories.append(await _repository(Path(top), notes))
    if not repositories:
        notes.append("no git repository found in addons_path")
    elif not any((Path(r["path"]) / "odoo-bin").is_file() for r in repositories):
        notes.append("no repository holds odoo-bin: add the Odoo repository or set odoo-bin")
    return {"database": database, "log_file": log_file,
            "repositories": repositories, "notes": notes}
