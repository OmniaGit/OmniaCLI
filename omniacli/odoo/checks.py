# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checks of an Odoo instance for Test & activate: its config, click-odoo,
the database, odoo-bin and the run_as user (the core checks the log, the
repositories and the service)."""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from ..plugin_api import ERROR, WARNING, Check, Instance, as_user, capture, system_user
from . import ops

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


async def instance_checks(instance: Instance) -> list[Check]:
    target = instance.role
    opts = instance.options
    checks: list[Check] = []

    def add(name, ok, detail, level=ERROR):
        checks.append(Check(target, name, ok, detail, level))

    # Odoo config and addons path
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(opts.odoo_config, encoding="utf-8") as fh:
            parser.read_file(fh)
        add("Odoo config", True, f"{opts.odoo_config} readable")
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        add("Odoo config", False, f"{opts.odoo_config}: {exc}")
    raw = parser.get("options", "addons_path", fallback="") if parser.has_section("options") else ""
    missing = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip() and not Path(p.strip()).is_dir()]
    if raw:
        add("Addons path", not missing,
            "every folder exists" if not missing else f"missing: {', '.join(missing)}", WARNING)

    # click-odoo scripts and the instance's python
    for script in ("click-odoo-update", "click-odoo-backupdb"):
        path = opts.venv_bin / script
        ok = os.access(path, os.X_OK)
        add(script, ok, str(path) if ok else
            f"{path} not found: pip install click-odoo-contrib in that venv")
    if instance.run_as is not None:
        checks.extend(await _run_as_checks(instance))
    env = ops.odoo_env(instance)
    code, out = await capture(
        as_user(instance.run_as, [opts.venv_bin / "python", "-c",
                                  "import click_odoo, odoo; print('Odoo', odoo.release.version)"], env),
        timeout=PYTHON_TIMEOUT,
        env=env,
    )
    add("Odoo importable by click-odoo", code == 0,
        out.splitlines()[-1] if out else f"exit code {code}")

    # database
    code, out = await capture(
        as_user(instance.run_as,
                [opts.venv_bin / "python", "-c", _DB_PROBE, opts.odoo_config, opts.database]),
        timeout=PYTHON_TIMEOUT,
    )
    add("Database", code == 0,
        f"{opts.database}: {out.splitlines()[-1] if out else ''}" if code == 0
        else f"{opts.database}: {out.splitlines()[-1] if out else f'exit code {code}'}")

    # odoo-bin, only needed to update chosen modules
    odoo_bin = ops.find_odoo_bin(instance)
    if odoo_bin is not None and odoo_bin.is_file():
        major = ops.odoo_major(odoo_bin)
        add("odoo-bin", True, f"{odoo_bin}" + (f" (Odoo {major})" if major else ""), WARNING)
    elif instance.restart.type == "process":
        add("odoo-bin", False, "not found: OmniaCLI cannot start Odoo; set odoo_bin")
    else:
        add("odoo-bin", False,
            "not found: the update of chosen modules will not work; set odoo_bin", WARNING)
    return checks


async def _run_as_checks(instance: Instance) -> list[Check]:
    """The user exists, and sudo lets this one run the venv's programs and
    psql as it, setting variables (SETENV), without a password."""
    import pwd

    try:
        pwd.getpwnam(instance.run_as)
    except KeyError:
        return [Check(instance.role, "Run as", False, f"no system user {instance.run_as}")]
    venv_bin = instance.options.venv_bin
    python = venv_bin / "python"
    code, _ = await capture(["sudo", "-n", "-l", "-u", instance.run_as, python])
    line = f"{system_user()} ALL=({instance.run_as}) NOPASSWD:SETENV: {venv_bin}/"
    checks = [Check(instance.role, "Run as", code == 0,
                    f"sudo runs the venv's programs as {instance.run_as}" if code == 0 else
                    f"sudo refuses to run {python} as {instance.run_as} or asks a password; "
                    f"add to /etc/sudoers.d/omniacli: {line}")]
    # the psql page runs psql as that user too (a warning: only that page needs it)
    psql = ops.psql_path()
    code, _ = await capture(["sudo", "-n", "-l", "-u", instance.run_as, psql])
    line = f"{system_user()} ALL=({instance.run_as}) NOPASSWD:SETENV: {psql}"
    checks.append(Check(instance.role, "psql", code == 0,
                        f"sudo runs {psql} as {instance.run_as}" if code == 0 else
                        f"the psql page will not connect: add to /etc/sudoers.d/omniacli: {line}", WARNING))
    return checks


def server_checks(production: Instance, test: Instance) -> list[Check]:
    same_db = production.options.database == test.options.database
    return [Check("server", "Separate databases", not same_db,
                  f"production {production.options.database}, test {test.options.database}" if not same_db
                  else f"production and test both use {production.options.database}")]
