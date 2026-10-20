"""Load and validate the OmniaCI YAML configuration."""

from __future__ import annotations

import datetime
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROLES = ("production", "test")
RESTART_TYPES = ("systemd", "initd")
# a server name is part of URLs and of the job keys
SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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

    def command(self) -> list[str]:
        if self.type == "systemd":
            # without --no-ask-password systemctl waits on a polkit prompt
            cmd = ["systemctl", "--no-ask-password", "restart", self.service]
        else:
            cmd = [f"/etc/init.d/{self.service}", "restart"]
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
class Instance:
    server: str
    role: str
    repositories: list[Repository]
    database: str
    odoo_config: Path
    log_file: Path
    # bin directory of the venv that runs this Odoo, where click-odoo-contrib
    # is installed (its scripts must import the instance's own odoo package)
    venv_bin: Path
    restart: Restart
    update_modules: bool = True
    auto_update: AutoUpdate = field(default_factory=AutoUpdate)
    # odoo-bin for the update of chosen modules; None: look in the repositories
    odoo_bin: Path | None = None

    @property
    def key(self) -> str:
        return f"{self.server}/{self.role}"

    def repository(self, name: str) -> Repository | None:
        return next((r for r in self.repositories if r.name == name), None)


@dataclass
class Server:
    name: str
    instances: dict[str, Instance]

    @property
    def production(self) -> Instance | None:
        return self.instances.get("production")

    @property
    def test(self) -> Instance | None:
        return self.instances.get("test")


@dataclass
class Settings:
    users: dict[str, str]
    backup_dir: Path
    jobs_dir: Path
    # the servers live in their own file, which the web interface rewrites
    servers_file: Path
    servers: dict[str, Server]

    def instance(self, server: str, role: str) -> Instance | None:
        srv = self.servers.get(server)
        return srv.instances.get(role) if srv else None

    def all_instances(self) -> list[Instance]:
        return [i for s in self.servers.values() for i in s.instances.values()]


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


def _parse_instance(server: str, role: str, data: dict) -> Instance:
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
    return Instance(
        server=server,
        role=role,
        repositories=repositories,
        database=str(_require(data, "database", where)),
        odoo_config=Path(_require(data, "odoo_config", where)),
        log_file=Path(_require(data, "log_file", where)),
        venv_bin=Path(_require(data, "venv_bin", where)),
        restart=Restart(
            type=restart_type,
            service=str(_require(restart, "service", f"{where}.restart")),
            sudo=bool(restart.get("sudo", True)),
        ),
        update_modules=bool(data.get("update_modules", True)),
        auto_update=_parse_auto_update(data.get("auto_update"), f"{where}.auto_update"),
        odoo_bin=Path(data["odoo_bin"]) if data.get("odoo_bin") else None,
    )


def parse_servers(data: dict) -> dict[str, Server]:
    """Parse the mapping {server name: {production: ..., test: ...}}."""
    servers = {}
    for name, srv in (data or {}).items():
        name = str(name)
        if not SERVER_NAME.fullmatch(name):
            raise ConfigError(
                f"servers.{name}: the name may contain only letters, digits, '.', '_' and '-'"
            )
        if not isinstance(srv, dict):
            raise ConfigError(f"servers.{name}: must be a mapping")
        instances = {
            role: _parse_instance(name, role, srv[role]) for role in ROLES if srv.get(role)
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
        servers[name] = Server(name=name, instances=instances)
    return servers


def instance_to_dict(instance: Instance) -> dict:
    auto = instance.auto_update
    data = {
        "database": instance.database,
        "odoo_config": str(instance.odoo_config),
        "log_file": str(instance.log_file),
        "venv_bin": str(instance.venv_bin),
        "restart": {
            "type": instance.restart.type,
            "service": instance.restart.service,
            "sudo": instance.restart.sudo,
        },
        "update_modules": instance.update_modules,
        "auto_update": {"enabled": auto.enabled, "interval_minutes": auto.interval_minutes},
        "repositories": [
            {"name": r.name, "path": str(r.path), "remote": r.remote, "branch": r.branch}
            for r in instance.repositories
        ],
    }
    if auto.window_start is not None and auto.window_end is not None:
        data["auto_update"]["window"] = auto.window_label()
    if instance.odoo_bin is not None:
        data["odoo_bin"] = str(instance.odoo_bin)
    return data


def server_to_dict(server: Server) -> dict:
    return {role: instance_to_dict(i) for role, i in server.instances.items()}


def load_servers(path: Path) -> dict[str, Server]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_servers(data.get("servers") or {})


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


def parse_settings(data: dict, base_dir: Path = Path(".")) -> Settings:
    users = (data.get("auth") or {}).get("users") or {}
    if not users:
        raise ConfigError("auth.users: at least one user is required")
    servers_file = Path(data.get("servers_file") or "servers.yaml")
    if not servers_file.is_absolute():
        servers_file = base_dir / servers_file
    return Settings(
        users={str(k): str(v) for k, v in users.items()},
        backup_dir=Path(_require(data, "backup_dir", "root")),
        jobs_dir=Path(_require(data, "jobs_dir", "root")),
        servers_file=servers_file,
        servers=load_servers(servers_file),
    )


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    with open(path, encoding="utf-8") as fh:
        return parse_settings(yaml.safe_load(fh) or {}, base_dir=path.parent)
