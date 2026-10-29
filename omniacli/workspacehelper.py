# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The root helper of the workspaces: their disks and their instances.

    omniacli workspace-helper disk <workspace> <storage> <size in GB>
    omniacli workspace-helper create <workspace> <storage> <instance> <template> <values as JSON>
    omniacli workspace-helper remove <workspace> <storage> <instance>

OmniaCLI runs it with sudo (sudoers: README, Workspaces). It takes no path
and no command of the caller's: a workspace and an instance are names
(a-z 0-9 -), the storage one of the folders the main configuration
(/etc/omniacli/omniacli.yaml, which only root writes) lists in
workspaces.storage, the template one of TEMPLATES (the package's, root's).

disk: the workspace's virtual disk, <storage>/.disks/<workspace>.img, an
ext4 image that takes room as it is used, mounted on <storage>/<workspace>
by a systemd mount unit (at every boot), docker started after it (a drop-in
of docker.service); the empty folder beneath is immutable, so nothing is
written there while the disk is not mounted. Again with a larger size, it
grows the disk, mounted; a disk is never made smaller.

create: an instance of the workspace from a template, production and test,
each a compose project <storage>/<workspace>/<instance>/<role> of root's
(compose.yaml of the template, .env of its checked values and of what this
helper makes: ports on 127.0.0.1, the database password, the project's
name; config/odoo.conf; the data folders, given to the image's users), and
<instance>/addons, the caller's, for the customer's repositories. It prints
what it made, as JSON. The disk must be mounted, the instance must not exist.

remove: docker compose down -v of both projects, then the instance's folder.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import yaml

from . import composehelper, stacktemplates
from .composehelper import ENV, HelperError, caller
from .config import WORKSPACE_NAME, parse_storage_roots
from .pluginstore import SYSTEM_CONFIG

# the templates root runs: the package's (build_deb.sh), root's. sudo does
# not pass the environment on (OMNIACLI_TEMPLATES is for the tests, as
# OMNIACLI_SYSTEM_CONFIG of pluginstore)
TEMPLATES = Path(os.environ.get("OMNIACLI_TEMPLATES", "/usr/share/omniacli/templates"))
SYSTEMD = Path("/etc/systemd/system")
ROLES = ("production", "test")
# the ports an instance's services are published on, on 127.0.0.1
PORTS = range(20000, 30000)
MAX_DISK_GB = 65536
GB = 1024 ** 3


def _tool(name: str) -> str:
    found = shutil.which(name, path=ENV["PATH"])
    if found is None:
        raise HelperError(f"{name} is not installed")
    return found


def _run(*cmd: str, check: bool = True) -> str:
    done = subprocess.run([_tool(cmd[0]), *cmd[1:]], env=ENV, capture_output=True, text=True)
    if check and done.returncode:
        raise HelperError(f"{' '.join(cmd)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def storage_roots(config: Path = SYSTEM_CONFIG) -> tuple[str, ...]:
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HelperError(f"cannot read {config}: {exc}") from None
    try:
        return parse_storage_roots(data.get("workspaces"))
    except Exception as exc:  # noqa: BLE001 - ConfigError, reported as the helper's
        raise HelperError(str(exc)) from None


def _name(kind: str, value: str) -> str:
    if not WORKSPACE_NAME.fullmatch(value):
        raise HelperError(f"{value!r} is not a {kind} name (a-z 0-9 -)")
    return value


def _safe_folder(folder: Path, what: str) -> None:
    """folder and every one above it out of the caller's reach."""
    _, uid, groups = caller()
    if uid == 0:
        return
    if Path(os.path.realpath(folder)) != folder:
        raise HelperError(f"{what} {folder} goes through a symbolic link")
    for path in (folder, *folder.parents):
        st = path.lstat()
        if composehelper._writable(st, uid, groups) and not (st.st_mode & stat.S_ISVTX and path != folder):
            raise HelperError(f"{what} {folder}: {path} is writable by the caller")


def workspace(name: str, storage: str) -> tuple[Path, Path]:
    """(image, mount point) of a workspace in one of the storage roots."""
    _name("workspace", name)
    if storage not in storage_roots():
        raise HelperError(f"{storage} is not one of workspaces.storage of {SYSTEM_CONFIG}")
    root = Path(storage)
    if not root.is_dir():
        raise HelperError(f"{storage} is not a folder")
    _safe_folder(root, "the storage")
    return root / ".disks" / f"{name}.img", root / name


def loop_of(image: Path) -> str | None:
    """The loop device image is attached to, None: none."""
    out = _run("losetup", "--noheadings", "--output", "NAME", "--associated", str(image), check=False)
    return out.split()[0] if out.split() else None


def mounted(image: Path, mount: Path) -> bool:
    """mount is the image's file system (not the folder beneath it)."""
    if not os.path.ismount(mount):
        return False
    source = _run("findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(mount), check=False).strip()
    loop = loop_of(image)
    return bool(loop) and source == loop


def unit_name(mount: Path) -> str:
    return _run("systemd-escape", "--path", "--suffix=mount", str(mount)).strip()


def unit_text(image: Path, mount: Path, name: str) -> str:
    return ("# written by omniacli workspace-helper: the virtual disk of workspace " + name + "\n"
            "[Unit]\nDescription=OmniaCLI workspace " + name + "\nBefore=docker.service\n"
            f"[Mount]\nWhat={image}\nWhere={mount}\nType=ext4\nOptions=loop\n"
            "[Install]\nWantedBy=multi-user.target\n")


def _write(path: Path, text: str, mode: int = 0o644, uid: int = 0, gid: int = 0) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def disk(name: str, storage: str, size_gb: str) -> dict:
    image, mount = workspace(name, storage)
    if not (size_gb.isdigit() and 1 <= int(size_gb) <= MAX_DISK_GB):
        raise HelperError(f"the size is 1 to {MAX_DISK_GB} GB, not {size_gb!r}")
    size = int(size_gb) * GB
    image.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(image.parent, 0o700)
    grow = False
    if image.is_symlink():
        raise HelperError(f"{image} is a symbolic link")
    if not image.exists():
        fd = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.truncate(image, size)
        _run("mkfs.ext4", "-q", "-F", str(image))
    elif image.stat().st_size < size:
        os.truncate(image, size)
        grow = True
    elif image.stat().st_size > size:
        raise HelperError(f"the disk of {name} is {image.stat().st_size // GB} GB: a disk is not made smaller")
    if not mount.exists():
        mount.mkdir(mode=0o755)
    if not os.path.ismount(mount):
        if any(mount.iterdir()):
            raise HelperError(f"{mount} is not empty: a disk is mounted on an empty folder only")
        # nothing is written beneath the disk while it is not mounted
        _run("chattr", "+i", str(mount))
    unit = unit_name(mount)
    _write(SYSTEMD / unit, unit_text(image, mount, name))
    dropin = SYSTEMD / "docker.service.d"
    dropin.mkdir(mode=0o755, exist_ok=True)
    _write(dropin / f"omniacli-{name}.conf",
           f"# written by omniacli workspace-helper: docker after the disk of workspace {name}\n"
           f"[Unit]\nAfter={unit}\n")
    _run("systemctl", "daemon-reload")
    _run("systemctl", "enable", "--now", unit)
    if not mounted(image, mount):
        raise HelperError(f"{unit} did not mount {image} on {mount}")
    if grow:
        loop = loop_of(image)
        _run("losetup", "--set-capacity", loop)
        _run("resize2fs", loop)
    os.chown(mount, 0, 0)
    os.chmod(mount, 0o755)
    return {"workspace": name, "image": str(image), "mount": str(mount), "unit": unit, "size_gb": int(size_gb)}


def _taken_ports() -> set[int]:
    """The ports the instances of every workspace were given (their .env)."""
    taken = set()
    for root in storage_roots():
        for env_file in Path(root).glob("*/*/*/.env"):
            try:
                text = env_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                key, _, value = line.partition("=")
                if key in ("HTTP_PORT", "WEBSOCKET_PORT") and value.isdigit():
                    taken.add(int(value))
    return taken


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_ports(count: int) -> list[int]:
    taken, found = _taken_ports(), []
    for port in PORTS:
        if port not in taken and _free(port):
            found.append(port)
            if len(found) == count:
                return found
    raise HelperError(f"no {count} free ports in {PORTS.start}-{PORTS.stop - 1}")


def template_named(name: str) -> stacktemplates.Template:
    if not stacktemplates.NAME.fullmatch(name):
        raise HelperError(f"{name!r} is not a template name")
    folder = TEMPLATES / name
    if not folder.is_dir():
        raise HelperError(f"no template {name} in {TEMPLATES}")
    _safe_folder(folder, "the template")
    _, uid, groups = caller()
    for file_name in stacktemplates.FILES:
        st = (folder / file_name).lstat()
        if stat.S_ISLNK(st.st_mode) or (uid != 0 and composehelper._writable(st, uid, groups)):
            raise HelperError(f"the template's {file_name} is a link or writable by the caller")
    try:
        return stacktemplates.load_template(folder)
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None


def _owner(spec: str) -> tuple[int, int]:
    uid, _, gid = spec.partition(":")
    return int(uid), int(gid)


def create(name: str, storage: str, instance: str, template_name: str, values_json: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted: workspace-helper disk first")
    folder = mount / instance
    if folder.exists() or folder.is_symlink():
        raise HelperError(f"{folder} exists already")
    template = template_named(template_name)
    try:
        given = json.loads(values_json)
    except ValueError:
        raise HelperError("the values are not JSON") from None
    if not isinstance(given, dict):
        raise HelperError("the values are not a JSON object")
    try:
        values = template.values({str(k): str(v) for k, v in given.items()})
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None
    _, caller_uid, _ = caller()
    owners = [_owner(d["owner"]) for d in template.data if d["owner"]]
    # the Odoo configuration: root's, readable by the group of the image's user
    conf_gid = owners[0][1] if owners else 0
    ports = pick_ports(2 * len(ROLES))
    database = instance.replace("-", "_")
    made = {"workspace": name, "instance": instance, "template": template.name, "version": template.version,
            "database": database, "odoo_service": template.odoo_service, "db_service": template.db_service,
            "values": values, "roles": {}}
    folder.mkdir(mode=0o755)
    try:
        addons = folder / "addons"
        addons.mkdir(mode=0o755)
        os.chown(addons, caller_uid, -1)
        for index, role in enumerate(ROLES):
            project = folder / role
            project.mkdir(mode=0o755)
            http_port, ws_port = ports[2 * index], ports[2 * index + 1]
            env = {**values, "PROJECT": f"{name}-{instance}-{role}", "HTTP_PORT": str(http_port),
                   "WEBSOCKET_PORT": str(ws_port), "DB_PASSWORD": stacktemplates.new_password()}
            _write(project / "compose.yaml", template.compose)
            _write(project / ".env", stacktemplates.env_text(env), mode=0o600)
            (project / "config").mkdir(mode=0o755)
            _write(project / "config" / "odoo.conf",
                   stacktemplates.conf_text(template, database, stacktemplates.new_password(), []),
                   mode=0o640, gid=conf_gid)
            for item in template.data:
                path = project / item["path"]
                path.mkdir(mode=0o755, parents=True)
                if item["owner"]:
                    os.chown(path, *_owner(item["owner"]))
            made["roles"][role] = {"folder": str(project), "http_port": http_port, "websocket_port": ws_port}
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return made


def remove(name: str, storage: str, instance: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    folder = mount / instance
    if folder.is_symlink() or not folder.is_dir():
        raise HelperError(f"no instance {instance} in workspace {name}")
    for role in ROLES:
        project = folder / role
        if any((project / f).is_file() for f in composehelper.COMPOSE_FILES):
            code = subprocess.run(composehelper.compose(project, "down", "-v", "--remove-orphans"),
                                  env=ENV, cwd=project).returncode
            if code:
                raise HelperError(f"docker compose down of {project} failed ({code}): nothing removed")
    shutil.rmtree(folder)
    return {"workspace": name, "instance": instance, "removed": str(folder)}


ACTIONS = {"disk": (disk, 3), "create": (create, 5), "remove": (remove, 3)}


def run(argv: list[str]) -> dict:
    if not argv or argv[0] not in ACTIONS:
        raise HelperError(f"usage: workspace-helper {' | '.join(ACTIONS)} ...")
    action, count = ACTIONS[argv[0]]
    if len(argv) - 1 != count:
        raise HelperError(f"{argv[0]} takes {count} arguments (workspace-helper --help: README, Workspaces)")
    if os.geteuid() != 0:
        raise HelperError("run it as root, through sudo")
    return action(*argv[1:])


def main(argv: list[str]) -> int:
    try:
        print(json.dumps(run(argv)))
    except HelperError as exc:
        print(f"workspace-helper: {exc}", file=sys.stderr)
        return 2
    return 0
