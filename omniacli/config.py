# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load and validate the OmniaCLI YAML configuration."""

from __future__ import annotations

import datetime
import ipaddress
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .auth import ROLES as ACCESS_ROLES
from .auth import WORKSPACE_ROLE

ROLES = ("production", "test")
# the roles a user may have on the machine: the access roles, or none but
# those of its workspaces
USER_ROLES = (*ACCESS_ROLES, WORKSPACE_ROLE)
# an MCP token saved without a role, as before tokens had one: what such a
# token could do then (read, update_test, backup_test, download backups)
TOKEN_DEFAULT_ROLE = "developer"
# process: OmniaCLI starts the service itself (odoo-bin...), its output going
# to a log of its own; compose: a Docker Compose project, the service being
# its folder (only for a plugin that runs in one: has_compose)
RESTART_TYPES = ("systemd", "initd", "process", "compose")
# the omniacli command of this venv: a compose project's commands go
# through its root helper, compose-helper (composehelper.py)
OMNIACLI_COMMAND = Path(sys.executable).parent / "omniacli"
# a server name is part of URLs and of the job keys
SERVER_STATUSES = ("active", "draft")
SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# the system user an instance's venv commands run as (sudo -u)
USER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\$?")


class ConfigError(Exception):
    pass


@dataclass
class Repository:
    name: str
    path: Path
    branch: str
    remote: str = "origin"
    # the -c options of the server's git credentials (gitcredentials.bind),
    # given to every git command on it; not saved
    git_options: tuple = field(default=(), compare=False, repr=False)

    def git(self, *args: str) -> list[str]:
        """A git command on this repository, with its server's credentials."""
        return ["git", *self.git_options, *args]


@dataclass
class Restart:
    type: str
    service: str
    sudo: bool = True

    def command(self, action: str = "restart", *args: str) -> list[str]:
        """The restart command of a systemd or init.d service (a systemd
        service is also started and stopped: action start or stop), or of a
        compose project, through the root helper (also actions state and
        logs, whose arguments follow)."""
        if self.type == "systemd":
            # without --no-ask-password systemctl waits on a polkit prompt
            cmd = ["systemctl", "--no-ask-password", action, self.service]
        elif self.type == "compose":
            cmd = [str(OMNIACLI_COMMAND), "compose-helper", self.service, action, *args]
        else:
            cmd = [f"/etc/init.d/{self.service}", action]
        return (["sudo", "-n"] + cmd) if self.sudo else cmd


@dataclass
class AutoUpdate:
    enabled: bool = False
    interval_minutes: int = 10
    window_start: datetime.time | None = None
    window_end: datetime.time | None = None

    def in_window(self, now: datetime.time) -> bool:
        return in_window(self.window_start, self.window_end, now)

    def window_label(self) -> str:
        return window_label(self.window_start, self.window_end)


def in_window(start: datetime.time | None, end: datetime.time | None, now: datetime.time) -> bool:
    """Whether now is in start-end (no window: always)."""
    if start is None or end is None:
        return True
    if start <= end:
        return start <= now < end
    # The window crosses midnight, e.g. 20:00-06:00
    return now >= start or now < end


def window_label(start: datetime.time | None, end: datetime.time | None) -> str:
    if start is None or end is None:
        return "always"
    return f"{start:%H:%M}-{end:%H:%M}"


@dataclass
class LinkSource:
    """Where an instance's database may be copied from: an instance of a
    server of a linked OmniaCLI (links file), backed up there and restored
    here; or (kind puller) the instance a puller connection reaches, link
    then naming the connection, server and role empty."""
    link: str
    server: str
    role: str
    kind: str = "link"

    @property
    def is_puller(self) -> bool:
        return self.kind == "puller"

    @property
    def label(self) -> str:
        if self.is_puller:
            return f"puller {self.link}"
        return f"{self.link} › {self.server}/{self.role}"

    def to_dict(self) -> dict:
        if self.is_puller:
            return {"puller": self.link}
        return {"link": self.link, "server": self.server, "role": self.role}


@dataclass
class Package:
    """A Debian package an instance is installed from (packages.py): url
    publishes the .deb, url.json its manifest (version, sha256, size) and
    url.json.sig the Ed25519 signature of that manifest, made with the
    private key of public_key. folder holds the downloaded file at a fixed
    path, the one sudoers allows to install."""
    url: str
    name: str
    public_key: str
    folder: Path
    server: str
    role: str

    @property
    def deb_path(self) -> Path:
        return self.folder / f"{self.server}_{self.role}.deb"

    @property
    def state_path(self) -> Path:
        """What was installed last: its checksum and version."""
        return self.folder / f"{self.server}_{self.role}.json"


@dataclass
class Instance:
    server: str
    role: str
    repositories: list[Repository]
    # None with restart type compose: the log is docker compose's
    # (plugin.log_service)
    log_file: Path | None
    restart: Restart
    # the plugin of the server's type (plugin_api.ServicePlugin), and its
    # settings of this instance (what its parse_options returned)
    plugin: Any = field(repr=False, compare=False)
    options: Any
    auto_update: AutoUpdate = field(default_factory=AutoUpdate)
    # the system user the type's programs run as, through sudo -n -u (for
    # Odoo the user of its service: its database login, filestore and venv);
    # None: as the user running OmniaCLI. git always runs as that user.
    run_as: str | None = None
    from_link: LinkSource | None = None
    # the base URL users open the instance at (its domain name); None: this
    # computer's localhost at the port the plugin knows
    domain: str | None = None
    # the folder the Claude button of the server page opens a shell in, to
    # run claude there; None: no button
    claude_dir: Path | None = None
    # installed from a published Debian package, besides or instead of git
    package: Package | None = None

    @property
    def type(self) -> str:
        return self.plugin.name

    @property
    def web_url(self) -> str | None:
        """Where the instance's web interface is opened: its domain, else
        http://localhost:<port>; None when neither is known."""
        if self.domain:
            return self.domain
        port = self.plugin.web_port(self)
        return f"http://localhost:{port}" if port else None

    @property
    def keeps_local_changes(self) -> bool:
        """A test instance without auto update may have uncommitted changes:
        someone works on it, and nothing pulls behind their back."""
        return self.role == "test" and not self.auto_update.enabled

    @property
    def key(self) -> str:
        return f"{self.server}/{self.role}"

    @property
    def log_label(self) -> str:
        """Where its log is read from, as the pages show it."""
        if self.log_file is not None:
            return str(self.log_file)
        service = self.plugin.log_service(self)
        return f"docker compose logs {service}" if service else "no log"

    @property
    def pid_file(self) -> Path:
        """Of an instance OmniaCLI starts itself, next to its log."""
        return self.log_file.with_suffix(".pid")

    def repository(self, name: str) -> Repository | None:
        return next((r for r in self.repositories if r.name == name), None)


@dataclass
class Server:
    name: str
    instances: dict[str, Instance]
    # a draft is saved but not live: no dashboard, no actions, no auto update
    # until "Test & activate" passes its checks
    status: str = "active"
    # a draft may be saved incomplete, and a server's plugin may be missing:
    # then it has no instances, data holds the roles as written and error
    # says what is wrong
    data: dict | None = None
    error: str | None = None
    # the plugin that serves it (plugins.DEFAULT_TYPE when the file names none)
    type: str = "odoo"
    # its plugin is not installed or is disabled (error says which)
    missing_plugin: bool = False

    @property
    def active(self) -> bool:
        """Live: active, and its plugin is there (a server whose plugin is
        missing keeps its status, and is live again with the plugin)."""
        return self.status == "active" and not self.missing_plugin

    @property
    def production(self) -> Instance | None:
        return self.instances.get("production")

    @property
    def test(self) -> Instance | None:
        return self.instances.get("test")


def server_text(server: Server) -> str:
    """What the Dashboard filters on: the name, and of each instance the
    service, repository names and branches, and the words its plugin gives
    (Odoo: the database)."""
    parts = [server.name]
    for inst in server.instances.values():
        parts += [*inst.plugin.search_text(inst), inst.restart.service, inst.domain or "",
                  inst.package.name if inst.package else ""]
        parts += [f"{r.name} {r.branch}" for r in inst.repositories]
    return " ".join(parts)


def matches(text: str, query: str) -> bool:
    """True when text contains every word of query, case insensitive."""
    text = text.lower()
    return all(w in text for w in query.lower().split())


def filter_servers(servers: list[Server], query: str = "") -> list[Server]:
    """The servers whose server_text contains every word of query."""
    return [s for s in servers if matches(server_text(s), query)]


@dataclass
class LinkAccess:
    """A linked OmniaCLI instance allowed in: its token's hash and its role."""

    token: str
    role: str


@dataclass
class Access:
    """The credentials of one source: the main config or the users file."""

    users: dict[str, str] = field(default_factory=dict)  # {name: password hash}
    roles: dict[str, str] = field(default_factory=dict)  # {user name: role}
    tokens: dict[str, str] = field(default_factory=dict)  # MCP clients: {name: token hash}
    links: dict[str, LinkAccess] = field(default_factory=dict)
    token_roles: dict[str, str] = field(default_factory=dict)  # {MCP token name: role}


@dataclass
class Settings:
    # {name: password hash}; their roles in user_roles (admin when missing).
    # users, user_roles, tokens and links join the main config's (fixed)
    # and the users file's (edited from the web interface): see set_web_access
    users: dict[str, str]
    backup_dir: Path
    jobs_dir: Path
    # the servers live in their own file, which the web interface rewrites
    servers_file: Path
    servers: dict[str, Server]
    # MCP clients: {name: "sha256$<hex>"} (omniacli new-token)
    tokens: dict[str, str] = field(default_factory=dict)
    user_roles: dict[str, str] = field(default_factory=dict)
    # linked instances: {name: LinkAccess} (omniacli new-link-token)
    links: dict[str, LinkAccess] = field(default_factory=dict)
    users_file: Path | None = None
    # the other OmniaCLI instances this one operates (Links page)
    links_file: Path | None = None
    link_targets: dict[str, LinkTarget] = field(default_factory=dict)
    # how the Dashboard groups this instance's and the links' servers
    groups_file: Path | None = None
    groups: list[ServerGroup] = field(default_factory=list)
    # the scheduled actions (schedules.py) and the scripts they may run
    schedules_file: Path | None = None
    scripts_dir: Path | None = None
    # the options of the system (Options page)
    options_file: Path | None = None
    options: Options = field(default_factory=lambda: Options())
    # the pullers this instance relays (pullers.yaml, Pullers page)
    pullers_file: Path | None = None
    pullers: dict[str, PullerDef] = field(default_factory=dict)
    # the instances of this one published on a puller of a linked relay
    # (publications.yaml, readable by its owner only: it holds the keys)
    publications_file: Path | None = None
    publications: dict[str, Publication] = field(default_factory=dict)
    # the pullers of linked relays this one is a consumer of (connections.yaml,
    # readable by its owner only: it holds the keys)
    connections_file: Path | None = None
    connections: dict[str, PullerConnection] = field(default_factory=dict)
    # the plugins published for download (main configuration, plugins:)
    plugin_source: PluginSource = field(default_factory=lambda: PluginSource())
    # the customers' workspaces (workspaces.yaml, Workspaces page), and the
    # folders they may live in (main configuration, workspaces.storage)
    workspaces_file: Path | None = None
    # the git credentials of servers and workspaces (gitcredentials.py)
    credentials_file: Path | None = None
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    storage_roots: tuple[str, ...] = ()
    token_roles: dict[str, str] = field(default_factory=dict)
    fixed: Access = field(default_factory=Access)
    web: Access = field(default_factory=Access)

    def set_web_access(self, web: Access) -> None:
        """Make web current: the fixed credentials win a name clash."""
        self.web = web
        self.users = {**web.users, **self.fixed.users}
        self.user_roles = {**web.roles, **self.fixed.roles}
        self.tokens = {**web.tokens, **self.fixed.tokens}
        self.links = {**web.links, **self.fixed.links}
        self.token_roles = {**web.token_roles, **self.fixed.token_roles}

    def user_role(self, name: str) -> str:
        return self.user_roles.get(name, "admin")

    def token_role(self, name: str) -> str:
        return self.token_roles.get(name, TOKEN_DEFAULT_ROLE)

    @property
    def logs_dir(self) -> Path:
        return logs_dir_of(self.jobs_dir)

    def active_servers(self) -> list[Server]:
        return [s for s in self.servers.values() if s.active]

    def instance(self, server: str, role: str, drafts: bool = False) -> Instance | None:
        """An instance of an active server, or also of a draft (drafts: for an
        admin's backup and its download; a draft saved incomplete has none)."""
        srv = self.servers.get(server)
        return srv.instances.get(role) if srv and (srv.active or drafts) else None

    def all_instances(self) -> list[Instance]:
        """The instances of the active servers."""
        return [i for s in self.active_servers() for i in s.instances.values()]

    def workspace_of(self, server: str) -> str | None:
        """The workspace server is in, None: none."""
        return next((ws.name for ws in self.workspaces.values() if server in ws.servers), None)

    def memberships(self, user: str) -> tuple[tuple[str, str], ...]:
        """((workspace, role), ...) of user, by workspace name."""
        return tuple((ws.name, ws.members[user]) for ws in sorted(self.workspaces.values(), key=lambda w: w.name)
                     if user in ws.members)


def _require(data: dict, key: str, where: str):
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: missing '{key}'")
    return data[key]


def _parse_time(value, where: str) -> datetime.time:
    try:
        return datetime.datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{where}: invalid time '{value}', expected HH:MM") from exc


def _parse_auto_update(data: dict | None, where: str) -> AutoUpdate:
    data = data or {}
    auto = AutoUpdate(
        enabled=bool(data.get("enabled", False)),
        interval_minutes=int(data.get("interval_minutes", 10)),
    )
    if auto.interval_minutes < 1:
        raise ConfigError(f"{where}: interval_minutes must be >= 1")
    auto.window_start, auto.window_end = _parse_window(data.get("window"), where)
    return auto


def _parse_window(window, where: str) -> tuple[datetime.time | None, datetime.time | None]:
    """'HH:MM-HH:MM' (it may cross midnight); empty: none."""
    if not window:
        return None, None
    try:
        start, end = str(window).split("-")
    except ValueError as exc:
        raise ConfigError(f"{where}: window must be 'HH:MM-HH:MM'") from exc
    return _parse_time(start.strip(), where), _parse_time(end.strip(), where)


def _parse_instance(server: str, role: str, data: dict, logs_dir: Path | None, plugin) -> Instance:
    where = f"servers.{server}.{role}"
    repositories = []
    for idx, repo in enumerate(_require(data, "repositories", where)):
        rwhere = f"{where}.repositories[{idx}]"
        repositories.append(
            Repository(
                name=str(_require(repo, "name", rwhere)),
                path=Path(_require(repo, "path", rwhere)),
                branch=str(_require(repo, "branch", rwhere)),
                remote=str(repo.get("remote", "origin")),
            )
        )
    names = [r.name for r in repositories]
    if len(names) != len(set(names)):
        raise ConfigError(f"{where}: repository names must be unique")
    restart = _require(data, "restart", where)
    restart_type = _require(restart, "type", f"{where}.restart")
    if restart_type not in RESTART_TYPES:
        raise ConfigError(
            f"{where}.restart: type must be one of {', '.join(RESTART_TYPES)}"
        )
    if restart_type == "compose" and not plugin.has_compose:
        raise ConfigError(f"{where}.restart: {plugin.label} does not run in Docker Compose "
                          "(type compose)")
    if restart_type == "process":
        if not plugin.has_process:
            raise ConfigError(f"{where}.restart: {plugin.label} cannot be started by OmniaCLI "
                              "(type process)")
        if logs_dir is None:
            raise ConfigError(f"{where}.restart: type process needs the logs folder")
        # the output of the process OmniaCLI starts
        log_file = logs_dir / f"{server}_{role}.log"
        service = ""
    else:
        # compose: the log is docker compose's, read through the helper
        log_file = None if restart_type == "compose" else Path(_require(data, "log_file", where))
        service = str(_require(restart, "service", f"{where}.restart"))
        if restart_type == "compose" and not service.startswith("/"):
            raise ConfigError(f"{where}.restart.service: {service!r} is not the absolute path "
                              "of the compose project's folder")
    run_as = str(data.get("run_as") or "").strip() or None
    if run_as is not None:
        if not USER_NAME.fullmatch(run_as):
            raise ConfigError(f"{where}.run_as: {run_as!r} is not a user name")
        if restart_type == "process":
            # OmniaCLI stops its own process with a signal, which it may not
            # send to another user's process
            raise ConfigError(f"{where}.run_as: not available with restart type process")
    from_link = parse_link_source(data.get("from_link"), f"{where}.from_link")
    domain = _parse_domain(data.get("domain"), f"{where}.domain")
    claude_dir = str(data.get("claude_dir") or "").strip() or None
    if claude_dir is not None and not claude_dir.startswith("/"):
        raise ConfigError(f"{where}.claude_dir: {claude_dir!r} is not an absolute path")
    options = plugin.parse_options(data, where, restart_type)
    package = _parse_package(data.get("package"), f"{where}.package", server, role, logs_dir)
    return Instance(
        server=server,
        role=role,
        repositories=repositories,
        log_file=log_file,
        restart=Restart(
            type=restart_type,
            service=service,
            sudo=bool(restart.get("sudo", True)),
        ),
        plugin=plugin,
        options=options,
        auto_update=_parse_auto_update(data.get("auto_update"), f"{where}.auto_update"),
        run_as=run_as,
        from_link=from_link,
        domain=domain,
        claude_dir=Path(claude_dir) if claude_dir else None,
        package=package,
    )


# a Debian package name (Debian Policy 5.6.1)
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")


def _parse_package(data, where: str, server: str, role: str, logs_dir: Path | None) -> Package | None:
    """{url, name, public_key}; empty or no url: none. The url is https (http
    only to this computer); the downloads go next to the logs folder."""
    from .packagesign import SigningError, parse_public_key

    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping of url, name and public_key")
    url = str(data.get("url") or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local)
            or not parts.hostname or parts.query or parts.fragment or parts.username
            or not parts.path.endswith(".deb")):
        raise ConfigError(f"{where}.url: {url!r} is not an https:// URL of a .deb")
    name = str(data.get("name") or "").strip()
    if not PACKAGE_NAME.fullmatch(name):
        raise ConfigError(f"{where}.name: {name!r} is not a Debian package name (api-server)")
    public_key = str(data.get("public_key") or "").strip()
    try:
        parse_public_key(public_key)
    except SigningError as exc:
        raise ConfigError(f"{where}.public_key: {exc}") from None
    if logs_dir is None:
        raise ConfigError(f"{where}: a package needs OmniaCLI's data folder")
    return Package(url=url, name=name, public_key=public_key, folder=logs_dir.parent / "packages",
                   server=server, role=role)


def _parse_domain(value, where: str) -> str | None:
    """A domain name (erp.example.com[:port], opened with https) or a whole
    http(s) URL, as the base URL without the trailing /; empty: none."""
    value = str(value or "").strip().rstrip("/")
    if not value:
        return None
    url = value if "://" in value else f"https://{value}"
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise ConfigError(f"{where}: {value!r} has no valid port") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment
            or parts.username or parts.password or any(c.isspace() for c in url)):
        raise ConfigError(f"{where}: {value!r} is not a domain name (erp.example.com) "
                          "or an http(s)://host[:port][/path] URL")
    return url


def parse_link_source(data, where: str) -> LinkSource | None:
    """{link, server, role}, or {puller} (a puller connection); empty or no
    link: none."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping of link, server and role, or of puller")
    connection = str(data.get("puller") or "").strip()
    if connection:
        if not SERVER_NAME.fullmatch(connection):
            raise ConfigError(f"{where}.puller: {connection!r} is not a name of letters, digits and . _ -")
        return LinkSource(link=connection, server="", role="", kind="puller")
    link = str(data.get("link") or "").strip()
    if not link:
        return None
    server = str(data.get("server") or "").strip()
    role = str(data.get("role") or "production").strip()
    for key, value in (("link", link), ("server", server)):
        if not SERVER_NAME.fullmatch(value):
            raise ConfigError(f"{where}.{key}: {value!r} is not a name of letters, digits and . _ -")
    if role not in ROLES:
        raise ConfigError(f"{where}.role: one of {', '.join(ROLES)}")
    return LinkSource(link=link, server=server, role=role)


def parse_servers(data: dict, logs_dir: Path | None = None) -> dict[str, Server]:
    """Parse the mapping {server name: {production: ..., test: ...}}.

    logs_dir: where the instances of restart type process log."""
    servers = {}
    for name, srv in (data or {}).items():
        name = str(name)
        if not SERVER_NAME.fullmatch(name):
            raise ConfigError(
                f"servers.{name}: the name may contain only letters, digits, '.', '_' and '-'"
            )
        if not isinstance(srv, dict):
            raise ConfigError(f"servers.{name}: must be a mapping")
        status = str(srv.get("status") or "active")
        if status not in SERVER_STATUSES:
            raise ConfigError(f"servers.{name}.status must be one of {', '.join(SERVER_STATUSES)}")
        from .plugins import DEFAULT_TYPE, registry  # the plugins import this module

        kind = str(srv.get("type") or DEFAULT_TYPE)
        written = {role: srv[role] for role in ROLES if srv.get(role)}
        plugin = registry().get(kind)
        if plugin is None:
            # kept as written, without actions, until its plugin is there
            servers[name] = Server(name=name, instances={}, status=status, data=written,
                                   error=registry().why_missing(kind), type=kind, missing_plugin=True)
            continue
        try:
            servers[name] = _parse_server(name, srv, status, logs_dir, plugin)
        except (ConfigError, TypeError, ValueError, AttributeError) as exc:
            if status != "draft":
                raise
            servers[name] = Server(name=name, instances={}, status=status, data=written,
                                   error=str(exc), type=kind)
    return servers


def _parse_server(name: str, srv: dict, status: str, logs_dir: Path | None, plugin) -> Server:
    instances = {
        role: _parse_instance(name, role, srv[role], logs_dir, plugin) for role in ROLES if srv.get(role)
    }
    if not instances:
        raise ConfigError(f"servers.{name}: define 'production' and/or 'test'")
    prod, test = instances.get("production"), instances.get("test")
    if prod and test:
        # merge to production pairs repositories by name
        missing = [r.name for r in test.repositories if not prod.repository(r.name)]
        if missing:
            raise ConfigError(
                f"servers.{name}: test repositories without a production "
                f"repository of the same name: {', '.join(missing)}"
            )
    return Server(name=name, instances=instances, status=status, type=plugin.name)


def instance_to_dict(instance: Instance) -> dict:
    auto = instance.auto_update
    data = {
        **instance.plugin.options_to_dict(instance.options),
        "log_file": str(instance.log_file),
        "restart": {
            "type": instance.restart.type,
            "service": instance.restart.service,
            "sudo": instance.restart.sudo,
        },
        "auto_update": {"enabled": auto.enabled, "interval_minutes": auto.interval_minutes},
        "repositories": [
            {"name": r.name, "path": str(r.path), "remote": r.remote, "branch": r.branch}
            for r in instance.repositories
        ],
    }
    if auto.window_start is not None and auto.window_end is not None:
        data["auto_update"]["window"] = auto.window_label()
    if instance.run_as is not None:
        data["run_as"] = instance.run_as
    if instance.domain is not None:
        data["domain"] = instance.domain
    if instance.claude_dir is not None:
        data["claude_dir"] = str(instance.claude_dir)
    if instance.from_link is not None:
        data["from_link"] = instance.from_link.to_dict()
    if instance.package is not None:
        pkg = instance.package
        data["package"] = {"url": pkg.url, "name": pkg.name, "public_key": pkg.public_key}
    if instance.restart.type == "process":
        # both follow from the type: the log is OmniaCLI's, there is no service
        del data["log_file"], data["restart"]["service"], data["restart"]["sudo"]
    if instance.log_file is None:
        del data["log_file"]
    return data


def server_to_dict(server: Server) -> dict:
    data = {"status": server.status, "type": server.type}
    if server.data is not None:
        data.update(server.data)
    data.update({role: instance_to_dict(i) for role, i in server.instances.items()})
    return data


def load_servers(path: Path, logs_dir: Path | None = None) -> dict[str, Server]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_servers(data.get("servers") or {}, logs_dir)


def save_servers(path: Path, servers: dict[str, Server]) -> None:
    """Write the servers file atomically, keeping the previous one as .bak."""
    text = yaml.safe_dump(
        {"servers": {name: server_to_dict(srv) for name, srv in servers.items()}},
        sort_keys=False,
        allow_unicode=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


_TOKEN_HASH = re.compile(r"sha256\$[0-9a-f]{64}")


def _access_role(where: str, value, roles: tuple[str, ...] = ACCESS_ROLES) -> str:
    role = str(value or "")
    if role not in roles:
        raise ConfigError(f"{where}: role must be one of {', '.join(roles)}, not {role!r}")
    return role


def parse_auth(auth: dict, where: str = "auth", need_user: bool = True) -> Access:
    """users (a hash, admin, or {password, role}), tokens and links ({token,
    role}) of the main config's auth section or of the users file."""
    users, roles = {}, {}
    for name, value in (auth.get("users") or {}).items():
        name = str(name)
        if isinstance(value, dict):
            users[name] = str(value.get("password") or "")
            roles[name] = _access_role(f"{where}.users.{name}", value.get("role"), USER_ROLES)
        else:
            # the format before roles: such a user is an admin
            users[name], roles[name] = str(value), "admin"
        if not users[name]:
            raise ConfigError(f"{where}.users.{name}: password hash missing (omniacli hash-password)")
    if need_user and not users:
        raise ConfigError(f"{where}.users: at least one user is required")
    tokens, token_roles = {}, {}
    for name, value in (auth.get("tokens") or {}).items():
        name = str(name)
        if isinstance(value, dict):
            tokens[name] = str(value.get("token") or "")
            token_roles[name] = _access_role(f"{where}.tokens.{name}", value.get("role") or TOKEN_DEFAULT_ROLE)
        else:
            # the format before roles: what such a token could do then
            tokens[name], token_roles[name] = str(value), TOKEN_DEFAULT_ROLE
        if not _TOKEN_HASH.fullmatch(tokens[name]):
            raise ConfigError(f"{where}.tokens: {name} is not a token hash (omniacli new-token)")
    links = {}
    for name, value in (auth.get("links") or {}).items():
        name, value = str(name), value if isinstance(value, dict) else {}
        token = str(value.get("token") or "")
        if not _TOKEN_HASH.fullmatch(token):
            raise ConfigError(f"{where}.links.{name}: token is not a token hash (omniacli new-link-token)")
        links[name] = LinkAccess(token, _access_role(f"{where}.links.{name}", value.get("role")))
    return Access(users, roles, tokens, links, token_roles)


def load_access(path: Path) -> Access:
    """The users file; none yet is no credentials."""
    if not path.exists():
        return Access()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_auth(data, where=path.name, need_user=False)


def save_access(path: Path, access: Access) -> None:
    """Write the users file atomically, readable by its owner only."""
    data = {
        "users": {n: {"password": h, "role": access.roles.get(n, "admin")} for n, h in access.users.items()},
        "links": {n: {"token": link.token, "role": link.role} for n, link in access.links.items()},
        "tokens": {n: {"token": h, "role": access.token_roles.get(n, TOKEN_DEFAULT_ROLE)}
                   for n, h in access.tokens.items()},
    }
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


@dataclass
class LinkTarget:
    name: str
    url: str  # the other instance's base URL, https://… (http only through a tunnel)
    token: str  # in clear: it is sent with every request
    ca_file: str = ""  # a CA bundle for a certificate of the customer's own CA

    def to_dict(self) -> dict:
        data = {"url": self.url, "token": self.token}
        if self.ca_file:
            data["ca_file"] = self.ca_file
        return data


def parse_link(name: str, data: dict) -> LinkTarget:
    if not SERVER_NAME.fullmatch(name):
        raise ConfigError(f"links.{name}: a name is letters, digits and . _ - (it is part of URLs)")
    url = str(data.get("url") or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError(f"links.{name}: url must be http(s)://host[:port][/path]")
    token = str(data.get("token") or "").strip()
    if not token:
        raise ConfigError(f"links.{name}: token missing (the other instance creates it on its Users page)")
    ca_file = str(data.get("ca_file") or "").strip()
    if ca_file and not Path(ca_file).is_file():
        raise ConfigError(f"links.{name}: ca_file {ca_file} is not a file")
    return LinkTarget(name, url, token, ca_file)


def load_links(path: Path) -> dict[str, LinkTarget]:
    """The links file; none yet is no links."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    links = data.get("links") or {}
    if not isinstance(links, dict):
        raise ConfigError(f"{path}: links must be a mapping")
    return {str(name): parse_link(str(name), value or {}) for name, value in links.items()}


def save_links(path: Path, links: dict[str, LinkTarget]) -> None:
    """Write the links file atomically, readable by its owner only: it holds the tokens."""
    text = yaml.safe_dump({"links": {n: link.to_dict() for n, link in links.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


_MEMBER_PART = r"(?:\*|[A-Za-z0-9][A-Za-z0-9._-]*)"
_GROUP_MEMBER = re.compile(rf"{_MEMBER_PART}(?:/{_MEMBER_PART})?")


@dataclass
class ServerGroup:
    """A heading of the Dashboard. A member is a server of this instance
    (srv, or * for all of them) or of a link (customer/srv, customer/*)."""

    name: str
    members: list[str] = field(default_factory=list)

    def contains(self, link: str, server: str) -> bool:
        for member in self.members:
            where, _, name = member.rpartition("/")
            if where in ("*", link) if link else not where:
                if name in ("*", server):
                    return True
        return False


def parse_groups(data) -> list[ServerGroup]:
    if not isinstance(data, list):
        raise ConfigError("groups must be a list of {name, members}")
    groups, seen = [], set()
    for item in data:
        item = item if isinstance(item, dict) else {}
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 64:
            raise ConfigError("a group needs a name of at most 64 characters")
        if name in seen:
            raise ConfigError(f"two groups are named {name}")
        seen.add(name)
        members = [str(m).strip() for m in item.get("members") or [] if str(m).strip()]
        bad = [m for m in members if not _GROUP_MEMBER.fullmatch(m)]
        if bad:
            raise ConfigError(f"group {name}: {', '.join(bad)} is not a server, link/server, link/* or *")
        groups.append(ServerGroup(name, list(dict.fromkeys(members))))
    return groups


def load_groups(path: Path) -> list[ServerGroup]:
    """The groups file; none yet is no groups."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_groups(data.get("groups") or [])
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_groups(path: Path, groups: list[ServerGroup]) -> None:
    text = yaml.safe_dump({"groups": [{"name": g.name, "members": g.members} for g in groups]},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# a workspace's name is part of URLs, of folders and of systemd unit names
WORKSPACE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
# the domain production answers on (test: test.<domain>)
DOMAIN_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
# the users of a workspace: names as the Users page makes them
MEMBER_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# what a workspace may use; a limit not set is no limit
WORKSPACE_LIMITS = ("disk_gb", "cpus", "memory_gb", "instances")
# a subdomain of a workspace's domain (and of test.<domain>): one label
SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# the label of the test names, never an instance's or a subdomain's
TEST_LABEL = "test"
# where the certificate of a workspace's names comes from
CERTIFICATES = ("letsencrypt", "own")


@dataclass
class Workspace:
    """A customer's closed part of the machine: its servers, and the users
    who see them, each with its role there. Set by the users of the machine
    (classes of users: README, Workspaces)."""

    name: str
    label: str = ""
    domain: str = ""
    # one of the storage roots of the main configuration (workspaces.storage)
    storage: str = ""
    limits: dict[str, float] = field(default_factory=dict)
    servers: list[str] = field(default_factory=list)
    members: dict[str, str] = field(default_factory=dict)  # {user name: role}
    # the names (README, Domains of a workspace): the instance on <domain>
    # and test.<domain>, the subdomains {<sub>: instance}, the certificate
    main: str = ""
    subdomains: dict[str, str] = field(default_factory=dict)
    certificate: str = "letsencrypt"

    def instances(self) -> list[str]:
        """The instances of the workspace: its servers <name>-<instance>."""
        prefix = f"{self.name}-"
        return [s[len(prefix):] for s in self.servers if s.startswith(prefix) and len(s) > len(prefix)]

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"label": self.label}
        if self.domain:
            data["domain"] = self.domain
        if self.storage:
            data["storage"] = self.storage
        if self.limits:
            data["limits"] = dict(self.limits)
        data["servers"] = list(self.servers)
        data["members"] = dict(self.members)
        if self.main:
            data["main"] = self.main
        if self.subdomains:
            data["subdomains"] = dict(self.subdomains)
        if self.certificate != "letsencrypt":
            data["certificate"] = self.certificate
        return data


def parse_workspace(name: str, data: dict, storage_roots: tuple[str, ...] = ()) -> Workspace:
    where = f"workspaces.{name}"
    if not WORKSPACE_NAME.fullmatch(name):
        raise ConfigError(f"{where}: a name is 1-32 lower case letters, digits and -, "
                          "not starting or ending with -")
    data = data if isinstance(data, dict) else {}
    label = str(data.get("label") or "").strip()
    if len(label) > 100:
        raise ConfigError(f"{where}.label: at most 100 characters")
    domain = str(data.get("domain") or "").strip().lower()
    if domain and not DOMAIN_NAME.fullmatch(domain):
        raise ConfigError(f"{where}.domain: {domain!r} is not a domain name")
    storage = str(data.get("storage") or "").strip()
    if storage and storage_roots and storage not in storage_roots:
        raise ConfigError(f"{where}.storage: {storage} is not one of the storage roots "
                          f"({', '.join(storage_roots)})")
    limits = {}
    for key, value in (data.get("limits") or {}).items():
        if key not in WORKSPACE_LIMITS:
            raise ConfigError(f"{where}.limits: {key} is not one of {', '.join(WORKSPACE_LIMITS)}")
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}.limits.{key}: {value!r} is not a number") from None
        if number <= 0 or (key == "instances" and not number.is_integer()):
            raise ConfigError(f"{where}.limits.{key}: {value!r} is not a positive "
                              + ("whole number" if key == "instances" else "number"))
        limits[key] = int(number) if number.is_integer() else number
    servers = [str(s).strip() for s in data.get("servers") or [] if str(s).strip()]
    bad = [s for s in servers if not SERVER_NAME.fullmatch(s)]
    if bad:
        raise ConfigError(f"{where}.servers: {', '.join(bad)} is not a server name")
    members = {}
    for user, role in (data.get("members") or {}).items():
        user = str(user)
        if not MEMBER_NAME.fullmatch(user):
            raise ConfigError(f"{where}.members: {user!r} is not a user name")
        members[user] = _access_role(f"{where}.members.{user}", role)
    ws = Workspace(name, label, domain, storage, limits, list(dict.fromkeys(servers)), members)
    instances = ws.instances()
    main = str(data.get("main") or "").strip()
    if main and main not in instances:
        raise ConfigError(f"{where}.main: {main} is not an instance of the workspace")
    subdomains = {}
    for sub, instance in (data.get("subdomains") or {}).items():
        sub, instance = str(sub).strip().lower(), str(instance or "").strip()
        if not SUBDOMAIN.fullmatch(sub) or sub == TEST_LABEL:
            raise ConfigError(f"{where}.subdomains: {sub!r} is not a subdomain (one label a-z 0-9 -, not {TEST_LABEL})")
        if sub in instances:
            raise ConfigError(f"{where}.subdomains: {sub} is the name of an instance already")
        if instance not in instances:
            raise ConfigError(f"{where}.subdomains.{sub}: {instance!r} is not an instance of the workspace")
        subdomains[sub] = instance
    certificate = str(data.get("certificate") or "letsencrypt")
    if certificate not in CERTIFICATES:
        raise ConfigError(f"{where}.certificate: one of {', '.join(CERTIFICATES)}, not {certificate!r}")
    return replace(ws, main=main, subdomains=subdomains, certificate=certificate)


def parse_workspaces(data, storage_roots: tuple[str, ...] = ()) -> dict[str, Workspace]:
    if not isinstance(data, dict):
        raise ConfigError("workspaces must be a mapping {name: workspace}")
    found = {str(name): parse_workspace(str(name), value, storage_roots) for name, value in data.items()}
    # a server is in one workspace at most, and so is a domain
    owner: dict[str, str] = {}
    for ws in found.values():
        for srv in ws.servers:
            if srv in owner:
                raise ConfigError(f"server {srv} is in two workspaces: {owner[srv]} and {ws.name}")
            owner[srv] = ws.name
    domains: dict[str, str] = {}
    for ws in found.values():
        if ws.domain and ws.domain in domains:
            raise ConfigError(f"domain {ws.domain} is in two workspaces: {domains[ws.domain]} and {ws.name}")
        domains[ws.domain] = ws.name
    # every name of a workspace is under its domain: no domain under another's
    for domain, ws_name in domains.items():
        for other, other_ws in domains.items():
            if domain and other and domain != other and domain.endswith("." + other):
                raise ConfigError(f"domain {domain} of workspace {ws_name} is under {other} of workspace {other_ws}")
    return found


def web_names(domain: str, instances, main: str = "", subdomains: dict[str, str] | None = None) -> list[dict]:
    """The names each instance and role of a workspace answers on:
    [{instance, role, names}], production first. The main instance takes
    <domain> and test.<domain>; every instance <instance>.<domain> and
    <instance>.test.<domain>; a subdomain <sub>.<domain> and <sub>.test.<domain>."""
    found = []
    for instance in instances:
        subs = [sub for sub, target in (subdomains or {}).items() if target == instance]
        for role, base in (("production", domain), ("test", f"{TEST_LABEL}.{domain}")):
            names = ([base] if instance == main else []) + [f"{label}.{base}" for label in (instance, *subs)]
            long = [n for n in names if len(n) > 253]
            if long:
                raise ConfigError(f"{long[0]} is longer than a domain name may be (253)")
            found.append({"instance": instance, "role": role, "names": names})
    return found


def load_workspaces(path: Path, storage_roots: tuple[str, ...] = ()) -> dict[str, Workspace]:
    """The workspaces file; none yet is no workspaces."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_workspaces(data.get("workspaces") or {}, storage_roots)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_workspaces(path: Path, workspaces: dict[str, Workspace]) -> None:
    text = yaml.safe_dump({"workspaces": {n: ws.to_dict() for n, ws in workspaces.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def parse_storage_roots(data) -> tuple[str, ...]:
    """workspaces.storage of the main configuration: the folders a workspace
    may live in (absolute, real paths: no symbolic links)."""
    data = data if isinstance(data, dict) else {}
    roots = []
    for value in data.get("storage") or []:
        root = str(value).strip().rstrip("/")
        # in a sudoers line too (packaging: postinst): no space, no wildcard
        if not re.fullmatch(r"(/[A-Za-z0-9._-]+)+", root) or "/../" in root + "/" or "/./" in root + "/":
            raise ConfigError(f"workspaces.storage: {value!r} is not an absolute path of letters, digits and . _ -")
        roots.append(root)
    return tuple(dict.fromkeys(roots))


# the text size of the shell pages, in pixels
SHELL_FONT_SIZE = 14
SHELL_FONT_SIZES = (8, 40)
# tunnels: closed after this many hours without traffic (TUNNEL_IDLE_HOURS
# the limits); the local address and ports they listen on
TUNNEL_IDLE = 4
TUNNEL_IDLE_HOURS = (1, 720)
TUNNEL_BIND = "127.0.0.1"
TUNNEL_PORTS = "18000-18099"


@dataclass
class Options:
    """The options of the system, set on the Options page (admin)."""

    shell_font_size: int = SHELL_FONT_SIZE
    tunnel_idle_hours: int = TUNNEL_IDLE
    tunnel_bind: str = TUNNEL_BIND
    tunnel_ports: str = TUNNEL_PORTS


def _whole(value, where: str, limits: tuple[int, int]) -> int:
    low, high = limits
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where} must be a whole number from {low} to {high}")
    return value


def parse_options(data) -> Options:
    data = data if isinstance(data, dict) else {}
    shell = data.get("shell") or {}
    tunnel = data.get("tunnel") or {}
    if not isinstance(shell, dict) or not isinstance(tunnel, dict):
        raise ConfigError("shell and tunnel must be mappings")
    size = _whole(shell.get("font_size", SHELL_FONT_SIZE), "shell.font_size", SHELL_FONT_SIZES)
    idle = _whole(tunnel.get("idle_hours", TUNNEL_IDLE), "tunnel.idle_hours", TUNNEL_IDLE_HOURS)
    bind = str(tunnel.get("bind", TUNNEL_BIND)).strip()
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise ConfigError(f"tunnel.bind: {bind!r} is not an IP address (127.0.0.1, 0.0.0.0...)") from None
    ports = str(tunnel.get("ports", TUNNEL_PORTS)).strip()
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", ports)
    low, high = (int(match[1]), int(match[2] or match[1])) if match else (0, 0)
    if not 1024 <= low <= high <= 65535:
        raise ConfigError(f"tunnel.ports: {ports!r} must be a port or a range low-high, from 1024 to 65535")
    return Options(shell_font_size=size, tunnel_idle_hours=idle, tunnel_bind=bind, tunnel_ports=ports)


def load_options(path: Path) -> Options:
    """The options file; none yet is the defaults."""
    if not path.exists():
        return Options()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_options(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_options(path: Path, options: Options) -> None:
    text = yaml.safe_dump({"shell": {"font_size": options.shell_font_size},
                           "tunnel": {"idle_hours": options.tunnel_idle_hours, "bind": options.tunnel_bind,
                                      "ports": options.tunnel_ports}}, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# the limits of a puller: hours a file and a report are kept, MiB of a file
PULLER_KEEP_HOURS = 24
PULLER_KEEP_LIMITS = (1, 720)
PULLER_FILE_MIB = 10240
PULLER_FILE_LIMITS = (1, 1 << 20)
_LINK_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")


@dataclass
class PullerDef:
    """A puller this instance relays (puller.py): the link (Users page) of
    the producer, the OmniaCLI whose instance it serves, and those of its
    consumers. The link names are those of auth.links."""

    name: str
    producer: str
    consumers: list[str] = field(default_factory=list)
    keep_hours: int = PULLER_KEEP_HOURS
    max_file_mib: int = PULLER_FILE_MIB

    def part(self, link: str) -> str | None:
        """producer, consumer or None: what the link is in this puller."""
        if link == self.producer:
            return "producer"
        return "consumer" if link in self.consumers else None

    def to_dict(self) -> dict:
        return {"producer": self.producer, "consumers": list(self.consumers),
                "keep_hours": self.keep_hours, "max_file_mib": self.max_file_mib}


def parse_puller(name: str, data) -> PullerDef:
    where = f"pullers.{name}"
    if not SERVER_NAME.fullmatch(name) or len(name) > 64:
        raise ConfigError(f"{where}: a name is letters, digits and . _ - (it is part of URLs)")
    data = data if isinstance(data, dict) else {}
    producer = str(data.get("producer") or "").strip()
    if not _LINK_NAME.fullmatch(producer):
        raise ConfigError(f"{where}.producer: the name of a link of the Users page")
    consumers = data.get("consumers") or []
    if not isinstance(consumers, list):
        raise ConfigError(f"{where}.consumers: a list of names of links of the Users page")
    consumers = list(dict.fromkeys(str(c).strip() for c in consumers if str(c).strip()))
    bad = [c for c in consumers if not _LINK_NAME.fullmatch(c)]
    if bad:
        raise ConfigError(f"{where}.consumers: {', '.join(bad)} is not a link name")
    if not consumers:
        raise ConfigError(f"{where}.consumers: at least one")
    if producer in consumers:
        raise ConfigError(f"{where}: {producer} cannot be the producer and a consumer")
    keep = _whole(data.get("keep_hours", PULLER_KEEP_HOURS), f"{where}.keep_hours", PULLER_KEEP_LIMITS)
    size = _whole(data.get("max_file_mib", PULLER_FILE_MIB), f"{where}.max_file_mib", PULLER_FILE_LIMITS)
    return PullerDef(name, producer, consumers, keep, size)


def load_pullers(path: Path) -> dict[str, PullerDef]:
    """The pullers file; none yet is no pullers."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    pullers = data.get("pullers") or {}
    if not isinstance(pullers, dict):
        raise ConfigError(f"{path}: pullers must be a mapping")
    try:
        return {str(name): parse_puller(str(name), value) for name, value in pullers.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_pullers(path: Path, pullers: dict[str, PullerDef]) -> None:
    text = yaml.safe_dump({"pullers": {n: p.to_dict() for n, p in pullers.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# what a publication may let its consumers do, each off unless enabled
PUBLISHED_COMMANDS = ("backup", "start", "stop", "restart")


@dataclass
class Exposed:
    """A command a publication lets the consumers send: enabled, and the
    hours it is taken in (AutoUpdate's window: none is always)."""

    enabled: bool = False
    window_start: datetime.time | None = None
    window_end: datetime.time | None = None

    def in_window(self, now: datetime.time) -> bool:
        return in_window(self.window_start, self.window_end, now)

    def window_label(self) -> str:
        return window_label(self.window_start, self.window_end)


@dataclass
class Publication:
    """An instance of this OmniaCLI published as the producer of a puller of
    a relay this one links to (publisher.py). key: the puller's key (text
    opk1-...), empty for none."""

    instance: str  # server/role
    link: str
    puller: str
    key: str = ""
    commands: dict[str, Exposed] = field(default_factory=dict)

    def exposed(self, action: str) -> Exposed:
        return self.commands.get(action) or Exposed()

    def to_dict(self) -> dict:
        data = {"link": self.link, "puller": self.puller}
        if self.key:
            data["key"] = self.key
        data["commands"] = {}
        for action in PUBLISHED_COMMANDS:
            rule = self.exposed(action)
            entry = {"enabled": rule.enabled}
            if rule.window_start is not None and rule.window_end is not None:
                entry["window"] = rule.window_label()
            data["commands"][action] = entry
        return data


def parse_publication(instance: str, data) -> Publication:
    where = f"publications.{instance}"
    server, _, role = instance.partition("/")
    if not SERVER_NAME.fullmatch(server) or role not in ROLES:
        raise ConfigError(f"{where}: an instance is server/role (production or test)")
    data = data if isinstance(data, dict) else {}
    link, name = str(data.get("link") or "").strip(), str(data.get("puller") or "").strip()
    for key, value in (("link", link), ("puller", name)):
        if not SERVER_NAME.fullmatch(value):
            raise ConfigError(f"{where}.{key}: {value!r} is not a name of letters, digits and . _ -")
    key = str(data.get("key") or "").strip()
    if key:
        from .pullcrypto import PullerCryptoError, parse_key  # it imports cryptography

        try:
            parse_key(key)
        except PullerCryptoError as exc:
            raise ConfigError(f"{where}.key: {exc}") from None
    commands = data.get("commands") or {}
    if not isinstance(commands, dict):
        raise ConfigError(f"{where}.commands: a mapping of {', '.join(PUBLISHED_COMMANDS)}")
    unknown = [str(c) for c in commands if c not in PUBLISHED_COMMANDS]
    if unknown:
        raise ConfigError(f"{where}.commands: {', '.join(unknown)} is not one of {', '.join(PUBLISHED_COMMANDS)}")
    exposed = {}
    for action in PUBLISHED_COMMANDS:
        entry = commands.get(action) or {}
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}.commands.{action}: a mapping of enabled and window")
        start, end = _parse_window(entry.get("window"), f"{where}.commands.{action}")
        exposed[action] = Exposed(bool(entry.get("enabled", False)), start, end)
    return Publication(instance, link, name, key, exposed)


def load_publications(path: Path) -> dict[str, Publication]:
    """The publications file; none yet is no publications."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    found = data.get("publications") or {}
    if not isinstance(found, dict):
        raise ConfigError(f"{path}: publications must be a mapping")
    try:
        return {str(k): parse_publication(str(k), v) for k, v in found.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_publications(path: Path, publications: dict[str, Publication]) -> None:
    """Write the publications file atomically, readable by its owner only: it holds the keys."""
    text = yaml.safe_dump({"publications": {k: p.to_dict() for k, p in publications.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


@dataclass
class PullerConnection:
    """This OmniaCLI as a consumer of a puller of a relay it links to
    (consumer.py): the instance the puller's producer publishes, operated from
    here. key: the puller's key (text opk1-...), empty for none."""

    name: str
    link: str
    puller: str
    key: str = ""

    def to_dict(self) -> dict:
        data = {"link": self.link, "puller": self.puller}
        if self.key:
            data["key"] = self.key
        return data


def parse_connection(name: str, data) -> PullerConnection:
    where = f"connections.{name}"
    if not SERVER_NAME.fullmatch(name) or len(name) > 64:
        raise ConfigError(f"{where}: a name is letters, digits and . _ - (it is part of URLs)")
    data = data if isinstance(data, dict) else {}
    link, puller = str(data.get("link") or "").strip(), str(data.get("puller") or "").strip()
    for key, value in (("link", link), ("puller", puller)):
        if not SERVER_NAME.fullmatch(value):
            raise ConfigError(f"{where}.{key}: {value!r} is not a name of letters, digits and . _ -")
    key = str(data.get("key") or "").strip()
    if key:
        from .pullcrypto import PullerCryptoError, parse_key  # it imports cryptography

        try:
            parse_key(key)
        except PullerCryptoError as exc:
            raise ConfigError(f"{where}.key: {exc}") from None
    return PullerConnection(name, link, puller, key)


def load_connections(path: Path) -> dict[str, PullerConnection]:
    """The connections file; none yet is no connections."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    found = data.get("connections") or {}
    if not isinstance(found, dict):
        raise ConfigError(f"{path}: connections must be a mapping")
    try:
        return {str(k): parse_connection(str(k), v) for k, v in found.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_connections(path: Path, connections: dict[str, PullerConnection]) -> None:
    """Write the connections file atomically, readable by its owner only: it holds the keys."""
    text = yaml.safe_dump({"connections": {k: c.to_dict() for k, c in connections.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# where the plugins published for download are listed (pluginstore.py)
DEFAULT_PLUGIN_INDEX = "https://www.omniasolutions.website/download/omniacli-plugins.json"


@dataclass
class PluginSource:
    """The index of the plugins published for download, and the public key
    their manifests are signed with (empty: none can be installed). Set in
    the main configuration, which only root writes: the root helper that
    installs a plugin reads the key from there (pluginstore.py)."""

    index: str = DEFAULT_PLUGIN_INDEX
    public_key: str = ""


def parse_plugin_source(data) -> PluginSource:
    data = data if isinstance(data, dict) else {}
    index = str(data.get("index") or DEFAULT_PLUGIN_INDEX).strip()
    parts = urlsplit(index)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"plugins.index: {index!r} is not an http(s) URL")
    key = str(data.get("public_key") or "").strip()
    if key:
        from .packagesign import SigningError, parse_public_key  # it imports cryptography

        try:
            parse_public_key(key)
        except SigningError as exc:
            raise ConfigError(f"plugins.public_key: {exc}") from None
    return PluginSource(index, key)


def parse_settings(data: dict, base_dir: Path = Path(".")) -> Settings:
    fixed = parse_auth(data.get("auth") or {})
    servers_file = Path(data.get("servers_file") or "servers.yaml")
    if not servers_file.is_absolute():
        servers_file = base_dir / servers_file
    # next to the servers file by default: a folder the service may write
    users_file = Path(data.get("users_file") or servers_file.parent / "users.yaml")
    if not users_file.is_absolute():
        users_file = base_dir / users_file
    links_file = Path(data.get("links_file") or servers_file.parent / "links.yaml")
    if not links_file.is_absolute():
        links_file = base_dir / links_file
    groups_file = Path(data.get("groups_file") or servers_file.parent / "groups.yaml")
    if not groups_file.is_absolute():
        groups_file = base_dir / groups_file
    schedules_file = Path(data.get("schedules_file") or servers_file.parent / "schedules.yaml")
    if not schedules_file.is_absolute():
        schedules_file = base_dir / schedules_file
    options_file = Path(data.get("options_file") or servers_file.parent / "options.yaml")
    if not options_file.is_absolute():
        options_file = base_dir / options_file
    pullers_file = Path(data.get("pullers_file") or servers_file.parent / "pullers.yaml")
    if not pullers_file.is_absolute():
        pullers_file = base_dir / pullers_file
    publications_file = Path(data.get("publications_file") or servers_file.parent / "publications.yaml")
    if not publications_file.is_absolute():
        publications_file = base_dir / publications_file
    connections_file = Path(data.get("connections_file") or servers_file.parent / "connections.yaml")
    if not connections_file.is_absolute():
        connections_file = base_dir / connections_file
    scripts_dir = Path(data.get("scripts_dir") or servers_file.parent / "scripts")
    if not scripts_dir.is_absolute():
        scripts_dir = base_dir / scripts_dir
    workspaces_file = Path(data.get("workspaces_file") or servers_file.parent / "workspaces.yaml")
    if not workspaces_file.is_absolute():
        workspaces_file = base_dir / workspaces_file
    storage_roots = parse_storage_roots(data.get("workspaces"))
    jobs_dir = Path(_require(data, "jobs_dir", "root"))
    settings = Settings(
        users={},
        backup_dir=Path(_require(data, "backup_dir", "root")),
        jobs_dir=jobs_dir,
        servers_file=servers_file,
        servers=load_servers(servers_file, logs_dir_of(jobs_dir)),
        users_file=users_file,
        links_file=links_file,
        link_targets=load_links(links_file),
        groups_file=groups_file,
        groups=load_groups(groups_file),
        schedules_file=schedules_file,
        scripts_dir=scripts_dir,
        options_file=options_file,
        options=load_options(options_file),
        pullers_file=pullers_file,
        pullers=load_pullers(pullers_file),
        publications_file=publications_file,
        publications=load_publications(publications_file),
        connections_file=connections_file,
        connections=load_connections(connections_file),
        plugin_source=parse_plugin_source(data.get("plugins")),
        workspaces_file=workspaces_file,
        workspaces=load_workspaces(workspaces_file, storage_roots),
        credentials_file=servers_file.parent / "credentials.yaml",
        storage_roots=storage_roots,
        fixed=fixed,
    )
    settings.set_web_access(load_access(users_file))
    return settings


def logs_dir_of(jobs_dir: Path) -> Path:
    """The logs of the instances OmniaCLI starts itself: next to the jobs."""
    return jobs_dir.parent / "logs"


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    return parse_settings(data, base_dir=path.parent)
