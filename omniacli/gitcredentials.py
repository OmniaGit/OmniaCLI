# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The git credentials OmniaCLI uses instead of the machine's: per host
(github.com, bitbucket.org, a GitLab...), a user and a token, set in the
interface on a server (the machine's admins) or on a workspace (its
admins). A server's win over its workspace's, host by host.

They are kept in credentials.yaml next to the servers file (0600, the
service's user only), never in a repository: every git command OmniaCLI
runs on a repository of a server that has some gets

    -c credential.helper=                       the machine's helpers off
    -c credential.helper=store --file=<file>    the server's, written here
    -c url.https://<host>/.insteadOf=git@<host>:
    -c url.https://<host>/.insteadOf=ssh://git@<host>/

so a working copy found on the machine with an ssh remote is reached over
https with the token as well, its .git/config untouched (*verified* with
git 2.30.2: the clone of an ssh remote went over https with ssh disabled,
and no token reached .git/config). A host without a credential is reached
as before, as the machine's git user.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::\d{1,5})?")
USERNAME = re.compile(r"[^\s:@/]{1,200}")
TOKEN = re.compile(r"\S{1,1000}")
SCOPE = re.compile(r"(server|workspace):([A-Za-z0-9_.-]{1,64})")


class CredentialError(ValueError):
    pass


@dataclass(frozen=True)
class Credential:
    host: str
    username: str
    token: str

    def public(self) -> dict:
        """What the pages show: never the token."""
        return {"host": self.host, "username": self.username, "token_set": bool(self.token)}


def check(host: str, username: str, token: str) -> Credential:
    host = host.strip().lower()
    if not HOST.fullmatch(host):
        raise CredentialError(f"{host!r} is not a host name (github.com, git.example.com:8443)")
    if not USERNAME.fullmatch(username or ""):
        raise CredentialError("the user is 1-200 characters, no space, : @ or /")
    if not TOKEN.fullmatch(token or ""):
        raise CredentialError("the token is 1-1000 characters, no space")
    return Credential(host, username, token)


def scope_of(kind: str, name: str) -> str:
    scope = f"{kind}:{name}"
    if not SCOPE.fullmatch(scope):
        raise CredentialError(f"{scope!r} is not a scope")
    return scope


def load(path: Path) -> dict[str, dict[str, Credential]]:
    """{scope: {host: Credential}}; no file is none."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CredentialError(f"cannot read {path}: {exc}") from None
    found: dict[str, dict[str, Credential]] = {}
    for scope, entries in (data.get("credentials") or {}).items():
        if not SCOPE.fullmatch(str(scope)):
            raise CredentialError(f"{path}: {scope!r} is not a scope")
        found[str(scope)] = {}
        for entry in entries or []:
            entry = entry if isinstance(entry, dict) else {}
            try:
                cred = check(str(entry.get("host") or ""), str(entry.get("username") or ""),
                             str(entry.get("token") or ""))
            except CredentialError as exc:
                raise CredentialError(f"{path}: {scope}: {exc}") from None
            found[str(scope)][cred.host] = cred
    return found


def save(path: Path, found: dict[str, dict[str, Credential]]) -> None:
    data = {"credentials": {scope: [{"host": c.host, "username": c.username, "token": c.token}
                                    for c in sorted(entries.values(), key=lambda c: c.host)]
                            for scope, entries in sorted(found.items()) if entries}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def merged(found: dict[str, dict[str, Credential]], server: str, workspace: str | None) -> dict[str, Credential]:
    """The credentials of a server: its workspace's, then its own over them."""
    result = dict(found.get(f"workspace:{workspace}", {})) if workspace else {}
    result.update(found.get(f"server:{server}", {}))
    return result


def store_dir(credentials_file: Path) -> Path:
    return credentials_file.parent / "credentials"


def write_store(folder: Path, server: str, creds: dict[str, Credential]) -> Path:
    """git credential-store's file of a server: one https://user:token@host per host."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / f"server-{server}.store"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for cred in sorted(creds.values(), key=lambda c: c.host):
            fh.write(f"https://{quote(cred.username, safe='')}:{quote(cred.token, safe='')}@{cred.host}\n")
    os.replace(tmp, path)
    return path


def git_options(store: Path, hosts) -> tuple[str, ...]:
    """The -c options of every git command on a repository of the server."""
    options = ["-c", "credential.helper=", "-c", f"credential.helper=store --file={shlex.quote(str(store))}"]
    for host in sorted(hosts):
        if ":" in host:
            continue  # an https port of its own: its ssh one is not known
        options += ["-c", f"url.https://{host}/.insteadOf=git@{host}:",
                    "-c", f"url.https://{host}/.insteadOf=ssh://git@{host}/"]
    return tuple(options)


def options_for(settings, server: str) -> tuple[str, ...]:
    """The git options of a server's credentials (a server of the form, not
    saved yet, included); () without any."""
    if settings.credentials_file is None:
        return ()
    try:
        creds = merged(load(settings.credentials_file), server, settings.workspace_of(server))
    except CredentialError:
        return ()
    if not creds:
        return ()
    return git_options(write_store(store_dir(settings.credentials_file), server, creds), creds)


def bind(settings) -> None:
    """Every repository of every server gets the git options of its server's
    credentials (none: the machine's git); the store files of servers that
    have none any more are removed."""
    if settings.credentials_file is None:
        return
    try:
        found = load(settings.credentials_file)
    except CredentialError:
        found = {}
    folder = store_dir(settings.credentials_file)
    kept = set()
    for name, server in settings.servers.items():
        creds = merged(found, name, settings.workspace_of(name))
        options: tuple[str, ...] = ()
        if creds:
            store = write_store(folder, name, creds)
            kept.add(store.name)
            options = git_options(store, creds)
        for inst in (server.production, server.test):
            for repo in (inst.repositories if inst is not None else []):
                repo.git_options = options
    if folder.is_dir():
        for stale in folder.glob("server-*.store"):
            if stale.name not in kept:
                stale.unlink(missing_ok=True)
