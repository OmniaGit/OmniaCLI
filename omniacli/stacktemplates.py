# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The templates a workspace's instances are created from (docs/workspaces.md).

A template is a folder:

    template.json   what it is and what it asks: name, label, version, the
                    Odoo and database services, the addons path of its image,
                    where the customer's addons are mounted, the data folders
                    (and the user of the image that owns each), the fields
    compose.yaml    the project, with only ${...} variables: the fields' and
                    those OmniaCLI makes (GENERATED)
    odoo.conf.in    the Odoo configuration, with {{...}} placeholders

What a template's compose.yaml may hold is a closed list (check_compose):
root runs it, so a template, signed or not, may not open the machine. Every
value of a field is checked against the field (its choices or pattern), and
reaches .env only, never compose.yaml.

Published (templatestore.py), a template is an archive of its three files,
omniacli-template-<name>.tar.gz, made by pack (the release script runs
`omniacli template-pack`) and read back by unpack, which takes those three
regular files and nothing else.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import secrets
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FILES = ("template.json", "compose.yaml", "odoo.conf.in")
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
FIELD_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# the variables OmniaCLI gives every instance (in .env and odoo.conf)
GENERATED = ("PROJECT", "HTTP_PORT", "WEBSOCKET_PORT", "DB_PASSWORD", "SUBNET")
CONF_PLACEHOLDERS = ("ADDONS_PATH", "DB_NAME", "ADMIN_PASSWD")
# a value of a field: one line, no quote that .env would read
VALUE = re.compile(r"[^\n\r\"'`$\\]{0,200}")
# what a template's compose.yaml may hold, and nothing else
TOP_KEYS = {"name", "services", "networks"}
# the project's one network: a /24 of the workspaces' pool, which the
# firewall knows (workspacehelper.firewall); required, nothing else
NETWORKS = {"default": {"ipam": {"config": [{"subnet": "${SUBNET}"}]}}}
SERVICE_KEYS = {"image", "restart", "depends_on", "ports", "environment", "volumes", "healthcheck",
                "logging", "command", "user", "working_dir", "labels", "stop_grace_period"}
# a port: on this machine only, its number from .env
PORT = re.compile(r"127\.0\.0\.1:\$\{[A-Z][A-Z0-9_]*\}:\d{1,5}")
# a mount: a folder of the instance (./ of its role, ../ of the instance)
MOUNT = re.compile(r"(\.\.?/[A-Za-z0-9._/-]+):(/[A-Za-z0-9._/-]+)(:ro)?")
VARIABLE = re.compile(r"\$\{([^}]*)\}")
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class TemplateError(Exception):
    pass


@dataclass
class Field:
    name: str
    label: str
    default: str = ""
    choices: list[str] = field(default_factory=list)
    pattern: str = ""

    def check(self, value: str) -> str:
        value = str(value if value is not None else self.default).strip()
        if not VALUE.fullmatch(value):
            raise TemplateError(f"{self.label}: {value!r} has characters that are not allowed")
        if self.choices and value not in self.choices:
            raise TemplateError(f"{self.label}: {value!r} is not one of {', '.join(map(repr, self.choices))}")
        if self.pattern and not re.fullmatch(self.pattern, value):
            raise TemplateError(f"{self.label}: {value!r} is not valid")
        return value


@dataclass
class Template:
    folder: Path
    name: str
    label: str
    version: str
    odoo_service: str
    db_service: str
    addons_path: list[str]
    customer_addons: str
    data: list[dict]
    fields: list[Field]
    compose: str
    conf: str

    def values(self, given: dict) -> dict[str, str]:
        """The fields' values, checked; a field not given takes its default."""
        unknown = set(given) - {f.name for f in self.fields}
        if unknown:
            raise TemplateError(f"{self.name} has no field {', '.join(sorted(unknown))}")
        return {f.name: f.check(given.get(f.name)) for f in self.fields}

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "version": self.version,
                "fields": [{"name": f.name, "label": f.label, "default": f.default, "choices": f.choices,
                            "pattern": f.pattern} for f in self.fields]}


def load_template(folder: Path) -> Template:
    folder = Path(folder)
    for name in FILES:
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise TemplateError(f"{folder} holds no {name}")
    try:
        meta = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TemplateError(f"{folder}/template.json: {exc}") from None
    if not isinstance(meta, dict):
        raise TemplateError(f"{folder}/template.json is not an object")
    name = str(meta.get("name") or "")
    if not NAME.fullmatch(name) or name != folder.name:
        raise TemplateError(f"{folder}: the template's name {name!r} must be its folder's, a-z 0-9 . -")
    fields = []
    for item in meta.get("fields") or []:
        item = item if isinstance(item, dict) else {}
        key = str(item.get("name") or "")
        if not FIELD_NAME.fullmatch(key) or key in GENERATED:
            raise TemplateError(f"{name}: {key!r} is not a field name (A-Z 0-9 _, not {', '.join(GENERATED)})")
        pattern = str(item.get("pattern") or "")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise TemplateError(f"{name}: the pattern of {key}: {exc}") from None
        fields.append(Field(key, str(item.get("label") or key), str(item.get("default") or ""),
                            [str(c) for c in item.get("choices") or []], pattern))
    data = []
    for item in meta.get("data") or []:
        item = item if isinstance(item, dict) else {}
        path = str(item.get("path") or "")
        owner = str(item.get("owner") or "")
        if not re.fullmatch(r"[a-z0-9_-]+(/[a-z0-9_-]+)*", path):
            raise TemplateError(f"{name}: data path {path!r} is not a folder of the instance")
        if owner and not re.fullmatch(r"\d{1,9}:\d{1,9}", owner):
            raise TemplateError(f"{name}: data owner {owner!r} is not uid:gid")
        data.append({"path": path, "owner": owner})
    template = Template(
        folder=folder, name=name, label=str(meta.get("label") or name), version=str(meta.get("version") or ""),
        odoo_service=str(meta.get("odoo_service") or "odoo"), db_service=str(meta.get("db_service") or "db"),
        addons_path=[str(p) for p in meta.get("addons_path") or []],
        customer_addons=str(meta.get("customer_addons") or ""), data=data, fields=fields,
        compose=(folder / "compose.yaml").read_text(encoding="utf-8"),
        conf=(folder / "odoo.conf.in").read_text(encoding="utf-8"))
    found = check_compose(template)
    if found:
        raise TemplateError(f"{name}: its compose.yaml is refused:\n  " + "\n  ".join(found))
    return template


def check_compose(template: Template) -> list[str]:
    """Why the template's compose.yaml may not be run as root; [] when it may."""
    found = []
    known = {*GENERATED, *(f.name for f in template.fields)}
    for name in VARIABLE.findall(template.compose):
        if name not in known:
            found.append(f"${{{name}}} is neither a field nor one OmniaCLI makes")
    try:
        data = yaml.safe_load(template.compose)
    except yaml.YAMLError as exc:
        return [f"not YAML: {exc}"]
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return ["no services"]
    found += [f"{key}: not allowed (only {', '.join(sorted(TOP_KEYS))})" for key in data if key not in TOP_KEYS]
    if data.get("networks") != NETWORKS:
        found.append("networks: must be exactly default with ipam.config [subnet: ${SUBNET}]")
    for svc_name, svc in data["services"].items():
        where = f"services.{svc_name}"
        if not isinstance(svc, dict):
            found.append(f"{where}: not a mapping")
            continue
        found += [f"{where}.{key}: not allowed" for key in svc if key not in SERVICE_KEYS]
        for port in svc.get("ports") or []:
            if not (isinstance(port, str) and PORT.fullmatch(port)):
                found.append(f"{where}.ports: {port!r} is not 127.0.0.1:${{VARIABLE}}:<port>")
        for mount in svc.get("volumes") or []:
            match = MOUNT.fullmatch(mount) if isinstance(mount, str) else None
            if match is None:
                found.append(f"{where}.volumes: {mount!r} is not ./<folder>:<path>[:ro] or ../<folder>:<path>[:ro]")
                continue
            source = match.group(1)
            parts = source.split("/")
            if ".." in parts[1:] or (parts[0] == ".." and len(parts) < 2):
                found.append(f"{where}.volumes: {source} leaves the instance's folder")
            if (source.startswith("../") or source.rstrip("/") == "./config") and not match.group(3):
                found.append(f"{where}.volumes: {source} must be mounted read only (:ro)")
        logging = svc.get("logging") or {}
        options = logging.get("options") or {} if isinstance(logging, dict) else {}
        if not (isinstance(logging, dict) and logging.get("driver") == "json-file"
                and options.get("max-size") and options.get("max-file")):
            found.append(f"{where}.logging: json-file with max-size and max-file is required")
    if template.odoo_service not in data["services"] or template.db_service not in data["services"]:
        found.append(f"services {template.odoo_service} and {template.db_service} are required")
    for name in PLACEHOLDER.findall(template.conf):
        if name not in CONF_PLACEHOLDERS:
            found.append(f"odoo.conf.in: {{{{{name}}}}} is not one of {', '.join(CONF_PLACEHOLDERS)}")
    return found


def env_text(values: dict[str, str]) -> str:
    """.env: KEY=value per line, the values already checked."""
    for key, value in values.items():
        if not FIELD_NAME.fullmatch(key) or not VALUE.fullmatch(value):
            raise TemplateError(f"{key}: not a value for .env")
    return "".join(f"{key}={value}\n" for key, value in values.items())


def conf_text(template: Template, database: str, admin_password: str, customer_repos: list[str]) -> str:
    """The Odoo configuration: the image's addons, then the customer's repositories."""
    addons = [*template.addons_path,
              *(f"{template.customer_addons}/{repo}" for repo in customer_repos if template.customer_addons)]
    values = {"ADDONS_PATH": ",".join(addons), "DB_NAME": database, "ADMIN_PASSWD": admin_password}
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template.conf)


def new_password() -> str:
    return secrets.token_urlsafe(24)


def version_key(version: str) -> tuple:
    """2026.1.10 after 2026.1.9; a version not of numbers comes first."""
    parts = str(version).split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else ()


def available(folders: list[Path]) -> dict[str, Template]:
    """The templates of folders: of two with one name (the package's and a
    downloaded one) the higher version, the first folder's when equal; a
    folder that is not a valid template is left out."""
    found: dict[str, Template] = {}
    for base in folders:
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                template = load_template(folder)
            except TemplateError:
                continue
            if folder.name not in found or version_key(template.version) > version_key(found[folder.name].version):
                found[folder.name] = template
    return found


# --- published: an archive of the three files ------------------------------

ARCHIVE_PREFIX = "omniacli-template-"
ARCHIVE_SUFFIX = ".tar.gz"
MAX_FILE = 256 * 1024


def archive_name(name: str) -> str:
    return f"{ARCHIVE_PREFIX}{name}{ARCHIVE_SUFFIX}"


def pack(folder: Path, dest_dir: Path) -> dict:
    """The archive of the template in folder, into dest_dir, once it checks;
    the same bytes for the same files (no time, no owner). Its entry of the
    index: name, label, description, version, file."""
    template = load_template(folder)
    meta = json.loads((Path(folder) / "template.json").read_text(encoding="utf-8"))
    path = Path(dest_dir) / archive_name(template.name)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(FILES):
            data = (Path(folder) / name).read_bytes()
            if len(data) > MAX_FILE:
                raise TemplateError(f"{folder}/{name}: more than {MAX_FILE} bytes")
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
        out.write(buffer.getvalue())
    return {"name": template.name, "label": template.label, "description": str(meta.get("description") or ""),
            "version": template.version, "file": path.name}


def unpack(archive: Path, dest: Path) -> None:
    """The three files of a template's archive into the folder dest (made
    here); anything else in the archive refuses it."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if sorted(m.name for m in members) != sorted(FILES):
                raise TemplateError(f"{archive.name} holds {', '.join(sorted(m.name for m in members))}, "
                                    f"not {', '.join(sorted(FILES))}")
            dest.mkdir(mode=0o755)
            for member in members:
                if not member.isreg() or member.size > MAX_FILE:
                    raise TemplateError(f"{archive.name}: {member.name} is not a file of at most {MAX_FILE} bytes")
                data = tar.extractfile(member).read(MAX_FILE + 1)
                (dest / member.name).write_bytes(data)
                (dest / member.name).chmod(0o644)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise TemplateError(f"{archive.name}: not a template's archive: {exc}") from None
