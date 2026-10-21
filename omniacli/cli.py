"""Command line: serve the web application, hash passwords, check a config."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from .auth import ROLES, hash_password, hash_token, new_token
from .config import ConfigError, load_settings


def _serve(args) -> int:
    import uvicorn

    os.environ["OMNIACLI_CONFIG"] = args.config
    load_settings(args.config)  # fail here, not inside uvicorn
    uvicorn.run(
        "omniacli.app:app_from_env",
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


def _new_token(args) -> int:
    token = new_token()
    print(f"Token for {args.name} (give it to the MCP client, it is not shown again):")
    print(f"  {token}")
    print("Add to auth.tokens in the main configuration:")
    print(f"  {args.name}: \"{hash_token(token)}\"")
    return 0


def _new_link_token(args) -> int:
    token = new_token()
    print(f"Token for the linked instance {args.name} (enter it in its link, it is not shown again):")
    print(f"  {token}")
    print("Add to auth.links in the main configuration:")
    print(f"  {args.name}: {{token: \"{hash_token(token)}\", role: {args.role}}}")
    return 0


def _check_config(args) -> int:
    from . import checks

    settings = load_settings(args.config)
    failed = False
    for server in settings.servers.values():
        report = asyncio.run(checks.check_server(server, settings.backup_dir))
        ok = checks.passed(report)
        print(f"{server.name} ({server.status}): {'OK' if ok else 'PROBLEMS'}")
        for check in report:
            mark = "ok  " if check.ok else ("warn" if check.level == checks.WARNING else "FAIL")
            print(f"  [{mark}] {check.target:<10} {check.name}: {check.detail}")
        failed = failed or not ok
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniacli")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("-c", "--config", default="omniacli.yaml")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.set_defaults(func=_serve)

    hashpw = sub.add_parser("hash-password", help="hash a password for auth.users")
    hashpw.set_defaults(func=_hash_password)

    token = sub.add_parser("new-token", help="create a bearer token for an MCP client (auth.tokens)")
    token.add_argument("name", help="who uses it, e.g. matteo-laptop")
    token.set_defaults(func=_new_token)

    link = sub.add_parser("new-link-token",
                          help="create a token for another OmniaCLI instance linked to this one (auth.links)")
    link.add_argument("name", help="who links in, e.g. omniasolutions")
    link.add_argument("--role", choices=ROLES, default="user",
                      help="user: monitors only; developer: acts on the servers; admin: everything")
    link.set_defaults(func=_new_link_token)

    check = sub.add_parser("check-config", help="run the Test & activate checks on every server")
    check.add_argument("-c", "--config", default="omniacli.yaml")
    check.set_defaults(func=_check_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
