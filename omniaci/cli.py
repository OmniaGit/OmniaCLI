"""Command line: serve the web application, hash passwords, check a config."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from .auth import hash_password
from .config import ConfigError, load_settings


def _serve(args) -> int:
    import uvicorn

    os.environ["OMNIACI_CONFIG"] = args.config
    load_settings(args.config)  # fail here, not inside uvicorn
    uvicorn.run(
        "omniaci.app:app_from_env",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
    )
    return 0


def _hash_password(args) -> int:
    first = getpass.getpass("Password: ")
    if not first or first != getpass.getpass("Repeat: "):
        print("Passwords are empty or do not match", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


async def _check_instance(instance) -> list[str]:
    from . import ops

    problems = []
    for path, what in (
        (instance.odoo_config, "Odoo config"),
        (instance.log_file, "log file"),
    ):
        if not os.access(path, os.R_OK):
            problems.append(f"{what} {path} is not readable")
    for script in ("click-odoo-update", "click-odoo-backupdb"):
        if not os.access(instance.venv_bin / script, os.X_OK):
            problems.append(
                f"{instance.venv_bin / script} not found: "
                f"pip install click-odoo-contrib in that venv"
            )
    for repo in instance.repositories:
        status = await ops.repository_status(repo)
        if "error" in status:
            problems.append(f"repository {repo.name}: {status['error']}")
        elif status["branch"] != repo.branch:
            problems.append(
                f"repository {repo.name} is on '{status['branch']}', expected '{repo.branch}'"
            )
    return problems


def _check_config(args) -> int:
    settings = load_settings(args.config)
    failed = False
    for instance in settings.all_instances():
        problems = asyncio.run(_check_instance(instance))
        print(f"{instance.key}: {'OK' if not problems else 'PROBLEMS'}")
        for problem in problems:
            print(f"  - {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniaci")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("-c", "--config", default="omniaci.yaml")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.set_defaults(func=_serve)

    hashpw = sub.add_parser("hash-password", help="hash a password for auth.users")
    hashpw.set_defaults(func=_hash_password)

    check = sub.add_parser("check-config", help="validate the config and the paths it names")
    check.add_argument("-c", "--config", default="omniaci.yaml")
    check.set_defaults(func=_check_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
