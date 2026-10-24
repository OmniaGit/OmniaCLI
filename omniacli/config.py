# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load and validate the OmniaCLI YAML configuration."""

from __future__ import annotations

import datetime
import ipaddress
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .auth import ROLES as ACCESS_ROLES

ROLES = ("production", "test")
# an MCP token saved without a role, as before tokens had one: what such a
# token could do then (read, update_test, backup_test, download backups)
TOKEN_DEFAULT_ROLE = "developer"
# process: OmniaCLI starts the service itself (odoo-bin...), its output going
# to a log of its own
RESTART_TYPES = ("systemd", "initd", "process")
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


@dataclass
class Restart:
    type: str
    service: str
    sudo: bool = True

    def command(self, action: str = "restart") -> list[str]:
        """The restart command of a systemd or init.d service (a systemd
        service is also started and stopped: action start or stop)."""
        if self.type == "systemd":
            # without --no-ask-password systemctl waits on a polkit prompt
            cmd = ["systemctl", "--no-ask-password", action, self.service]
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
        if self.window_start is None or self.window_end is None:
            return True
        if self.window_start <= self.window_end:
            return self.window_start <= now < self.window_end
        # The window crosses midnight, e.g. 20:00-06:00
        return now >= self.window_start or now < self.window_end

    def window_label(self) -> str:
        if self.window_start is None or self.window_end is None:
            return "always"
        return f"{self.window_start:%H:%M}-{self.window_end:%H:%M}"


@dataclass
class LinkSource:
    """Where an instance's database may be copied from: an instance of a
    server of a linked OmniaCLI (links file), backed up there and restored here."""
    link: str
    server: str
    role: str

    @property
    def label(self) -> str:
        return f"{self.link} › {self.server}/{self.role}"


@dataclass
class Instance:
    server: str
    role: str
    repositories: list[Repository]
    log_file: Path
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
        parts += [*inst.plugin.search_text(inst), inst.restart.service, inst.domain or ""]
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
    window = data.get("window")
    if window:
        try:
            start, end = str(window).split("-")
        except ValueError as exc:
            raise ConfigError(f"{where}: window must be 'HH:MM-HH:MM'") from exc
        auto.window_start = _parse_time(start.strip(), where)
        auto.window_end = _parse_time(end.strip(), where)
    return auto


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
        log_file = Path(_require(data, "log_file", where))
        service = str(_require(restart, "service", f"{where}.restart"))
    run_as = str(data.get("run_as") or "").strip() or None
    if run_as is not None:
        if not USER_NAME.fullmatch(run_as):
            raise ConfigError(f"{where}.run_as: {run_as!r} is not a user name")
        if restart_type == "process":
            # OmniaCLI stops its own process with a signal, which it may not
            # send to another user's process
            raise ConfigError(f"{where}.run_as: not available with restart type process")
    from_link = _parse_from_link(data.get("from_link"), f"{where}.from_link")
    domain = _parse_domain(data.get("domain"), f"{where}.domain")
    claude_dir = str(data.get("claude_dir") or "").strip() or None
    if claude_dir is not None and not claude_dir.startswith("/"):
        raise ConfigError(f"{where}.claude_dir: {claude_dir!r} is not an absolute path")
    options = plugin.parse_options(data, where, restart_type)
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
    )


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


def _parse_from_link(data, where: str) -> LinkSource | None:
    """{link, server, role}; empty or no link: none."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping of link, server and role")
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
        src = instance.from_link
        data["from_link"] = {"link": src.link, "server": src.server, "role": src.role}
    if instance.restart.type == "process":
        # both follow from the type: the log is OmniaCLI's, there is no service
        del data["log_file"], data["restart"]["service"], data["restart"]["sudo"]
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


def _access_role(where: str, value) -> str:
    role = str(value or "")
    if role not in ACCESS_ROLES:
        raise ConfigError(f"{where}: role must be one of {', '.join(ACCESS_ROLES)}, not {role!r}")
    return role


def parse_auth(auth: dict, where: str = "auth", need_user: bool = True) -> Access:
    """users (a hash, admin, or {password, role}), tokens and links ({token,
    role}) of the main config's auth section or of the users file."""
    users, roles = {}, {}
    for name, value in (auth.get("users") or {}).items():
        name = str(name)
        if isinstance(value, dict):
            users[name] = str(value.get("password") or "")
            roles[name] = _access_role(f"{where}.users.{name}", value.get("role"))
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
    scripts_dir = Path(data.get("scripts_dir") or servers_file.parent / "scripts")
    if not scripts_dir.is_absolute():
        scripts_dir = base_dir / scripts_dir
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
