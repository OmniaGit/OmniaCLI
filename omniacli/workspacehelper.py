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
name; config/odoo.conf; the data folders, given to the image's users;
addons/, the caller's, for the customer's repositories of that role, which
OmniaCLI clones there), and <instance>/instance.json (the template and the
database), root's. It prints what it made, as JSON. The disk must be
mounted, the instance must not exist.

conf <workspace> <storage> <instance> <role> <JSON>: the role's odoo.conf
written again from the instance's template, the admin password kept. The
JSON may hold repositories (the addons path: the image's, then
/mnt/customer-addons/<repository> of each, folders of the role's addons/),
parameters (the template's editable Odoo options, checked against its
bounds, or against bounds {name: [min, max]} the workspace's admins of the
machine set) and debug (log_level = debug); what it leaves out stays as it
was, kept in <role>/omniacli.json, root's.

remove: docker compose down -v of both projects, then the instance's folder.

web: the workspace's nginx site, /etc/nginx/conf.d/omniacli-<workspace>.conf,
from the names of <spec> ({domain, main, subdomains, certificate}: README,
Domains of a workspace) and the ports of the instances on its disk, read
here; a name another site of nginx answers on is refused; nginx -t, or the
file before is put back; then, for certificate letsencrypt, certbot
(--webroot, one certificate omniacli-<workspace> for all the names) and the
site again with https. No domain or no instance: the site and its
certificate are removed.

firewall on|apply|off: the workspaces' containers reach the internet only.
Their networks are /24s of one pool (workspaces.network of the main
configuration, 10.213.0.0/16 when not set; create gives each project its
own, SUBNET in .env); chain OMNIACLI-WS-FWD, jumped from DOCKER-USER, drops
what they send to private addresses (the LAN, other networks of the
machine) but to the pool itself (docker's own rules decide between its
networks), OMNIACLI-WS-IN, jumped first from INPUT, drops what they send to
this machine; replies pass in both (nginx reaches Odoo through docker-proxy,
the answer comes in from the container). on: the rules, and the unit
omniacli-workspaces-firewall.service that applies them at every boot before
docker; apply: the rules only (what the unit runs); off: both removed.
create puts them on first.

template-install <template>: a template downloaded from the download
server, checked with the key of the main configuration, into
/var/lib/omniacli-templates/<template> (templatestore.py); of it and the
package's, create takes the higher version.

owncert: the customer's certificate and key, {certificate, key} as JSON on
the standard input (a key is no argument: ps shows those), checked with
openssl (the key is the certificate's, not expired), kept in
/etc/omniacli/certs/<workspace>/, root's only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import yaml

from . import composehelper, stacktemplates, templatestore, workspaceweb
from .composehelper import ENV, HelperError, caller
from .config import (CERTIFICATES, DOMAIN_NAME, OMNIACLI_COMMAND, SUBDOMAIN, TEST_LABEL, WORKSPACE_NAME,
                     ConfigError, parse_storage_roots, web_names)
from .pluginstore import SYSTEM_CONFIG

# the templates root runs: the package's (build_deb.sh), root's. sudo does
# not pass the environment on (OMNIACLI_TEMPLATES is for the tests, as
# OMNIACLI_SYSTEM_CONFIG of pluginstore)
TEMPLATES = Path(os.environ.get("OMNIACLI_TEMPLATES", "/usr/share/omniacli/templates"))
SYSTEMD = Path("/etc/systemd/system")
ROLES = ("production", "test")
# what an instance was made from, root's: conf writes its odoo.conf again from it
INSTANCE_FILE = "instance.json"
# what a role was given (repositories, parameters, debug): conf's own record
ROLE_FILE = "omniacli.json"
# a customer's repository, a folder of the role's addons/
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# the ports an instance's services are published on, on 127.0.0.1
PORTS = range(20000, 30000)
MAX_DISK_GB = 65536
GB = 1024 ** 3
NGINX_SITES = Path("/etc/nginx/conf.d")
LETSENCRYPT = Path("/etc/letsencrypt/live")
OWN_CERTS = Path("/etc/omniacli/certs")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}")
# a certificate with its chain and its key, on the standard input
MAX_PEM = 256 * 1024
# the networks of the workspaces' projects: a /24 each of one pool
NETWORK_POOL = "10.213.0.0/16"
SUBNET_PREFIX = 24
FIREWALL_UNIT = "omniacli-workspaces-firewall.service"
CHAIN_FORWARD = "OMNIACLI-WS-FWD"
CHAIN_INPUT = "OMNIACLI-WS-IN"
# what a workspace's container may not reach (but the pool: docker decides)
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")


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


def _system(config: Path = SYSTEM_CONFIG) -> dict:
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HelperError(f"cannot read {config}: {exc}") from None
    return data if isinstance(data, dict) else {}


def storage_roots(config: Path = SYSTEM_CONFIG) -> tuple[str, ...]:
    data = _system(config)
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


def network_pool() -> ipaddress.IPv4Network:
    value = str((_system().get("workspaces") or {}).get("network") or NETWORK_POOL)
    try:
        pool = ipaddress.ip_network(value)
    except ValueError:
        raise HelperError(f"workspaces.network of {SYSTEM_CONFIG}: {value!r} is not a network (a.b.c.d/n)") from None
    if pool.version != 4 or not pool.is_private or pool.prefixlen > SUBNET_PREFIX:
        raise HelperError(f"workspaces.network of {SYSTEM_CONFIG}: {value} is not a private IPv4 network "
                          f"of /{SUBNET_PREFIX} or larger")
    return pool


def firewall_rules(pool: ipaddress.IPv4Network) -> dict[str, list[list[str]]]:
    """The rules of the two chains, as iptables arguments after -A <chain>."""
    src = ["-s", str(pool)]
    replies = [*src, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"]
    return {
        CHAIN_FORWARD: [replies, [*src, "-d", str(pool), "-j", "RETURN"],
                        *([*src, "-d", net, "-j", "DROP"] for net in PRIVATE)],
        CHAIN_INPUT: [replies, [*src, "-j", "DROP"]],
    }


def _iptables(*args: str, check: bool = True) -> str:
    return _run("iptables", "-w", *args, check=check)


def _jumps() -> list[tuple[str, str]]:
    return [("DOCKER-USER", CHAIN_FORWARD), ("INPUT", CHAIN_INPUT)]


def _has_jump(parent: str, chain: str) -> bool:
    return subprocess.run([_tool("iptables"), "-w", "-C", parent, "-j", chain], env=ENV,
                          capture_output=True).returncode == 0


def firewall_in_place() -> bool:
    return all(_has_jump(parent, chain) for parent, chain in _jumps())


def _host_networks() -> list[tuple[str, ipaddress.IPv4Network]]:
    found = []
    for line in _run("ip", "-4", "-o", "addr", "show").splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[2] == "inet":
            found.append((parts[1], ipaddress.ip_interface(parts[3]).network))
    return found


def firewall_apply() -> dict:
    pool = network_pool()
    # the pool is the workspaces' own: on this machine only their bridges are in it
    clash = [f"{iface} {net}" for iface, net in _host_networks()
             if net.overlaps(pool) and not (iface.startswith("br-") and net.subnet_of(pool))]
    if clash:
        raise HelperError(f"workspaces.network {pool} overlaps networks of this machine: {', '.join(clash)}")
    for chain, rules in firewall_rules(pool).items():
        _iptables("-N", chain, check=False)
        _iptables("-F", chain)
        for rule in rules:
            _iptables("-A", chain, *rule)
    # docker makes DOCKER-USER, and keeps its rules; at boot this runs first
    _iptables("-N", "DOCKER-USER", check=False)
    for parent, chain in _jumps():
        if not _has_jump(parent, chain):
            _iptables("-I", parent, "1", "-j", chain)
    return {"firewall": "on", "pool": str(pool), "chains": [CHAIN_FORWARD, CHAIN_INPUT]}


def firewall_unit_text() -> str:
    return ("# written by omniacli workspace-helper: the firewall of the workspaces' containers\n"
            "[Unit]\nDescription=OmniaCLI: the workspaces' containers reach the internet only\n"
            "Before=docker.service\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={OMNIACLI_COMMAND} workspace-helper firewall apply\n"
            "[Install]\nWantedBy=multi-user.target docker.service\n")


def firewall(mode: str) -> dict:
    if mode == "apply":
        return firewall_apply()
    unit = SYSTEMD / FIREWALL_UNIT
    if mode == "on":
        made = firewall_apply()
        _write(unit, firewall_unit_text())
        _run("systemctl", "daemon-reload")
        _run("systemctl", "enable", FIREWALL_UNIT)
        return {**made, "unit": FIREWALL_UNIT}
    if mode == "off":
        for parent, chain in _jumps():
            while _has_jump(parent, chain):
                _iptables("-D", parent, "-j", chain)
            _iptables("-F", chain, check=False)
            _iptables("-X", chain, check=False)
        if unit.exists():
            _run("systemctl", "disable", FIREWALL_UNIT, check=False)
            unit.unlink()
            _run("systemctl", "daemon-reload")
        return {"firewall": "off"}
    raise HelperError("firewall on, apply or off")


def _taken_subnets() -> list[ipaddress.IPv4Network]:
    """The networks of the workspaces' projects (.env) and of docker."""
    taken = []
    for root in storage_roots():
        for env_file in Path(root).glob("*/*/*/.env"):
            try:
                value = _env(env_file.parent).get("SUBNET", "")
                taken.append(ipaddress.ip_network(value))
            except (OSError, ValueError):
                continue
    ids = _run("docker", "network", "ls", "-q").split()
    if ids:
        out = _run("docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids)
        for value in out.split():
            try:
                taken.append(ipaddress.ip_network(value))
            except ValueError:
                continue
    return [net for net in taken if net.version == 4]


def pick_subnets(count: int) -> list[str]:
    pool = network_pool()
    taken = _taken_subnets() + [net for _, net in _host_networks()]
    found = []
    for subnet in pool.subnets(new_prefix=SUBNET_PREFIX):
        if not any(subnet.overlaps(net) for net in taken):
            found.append(str(subnet))
            taken.append(subnet)
            if len(found) == count:
                return found
    raise HelperError(f"no {count} free /{SUBNET_PREFIX} networks in {pool}")


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


def _template_in(folder: Path) -> stacktemplates.Template:
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


def template_named(name: str) -> stacktemplates.Template:
    """The package's template or the one installed from the download
    server (templatestore.py), the higher version."""
    if not stacktemplates.NAME.fullmatch(name):
        raise HelperError(f"{name!r} is not a template name")
    found = None
    for base in (TEMPLATES, templatestore.INSTALLED):
        folder = base / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        template = _template_in(folder)
        if found is None or stacktemplates.version_key(template.version) > stacktemplates.version_key(found.version):
            found = template
    if found is None:
        raise HelperError(f"no template {name} in {TEMPLATES} or {templatestore.INSTALLED}")
    return found


def template_install(name: str) -> dict:
    try:
        return templatestore.install(name)
    except templatestore.StoreError as exc:
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
    ports = pick_ports(2 * len(ROLES))
    if not firewall_in_place():
        firewall("on")
    subnets = pick_subnets(len(ROLES))
    database = instance.replace("-", "_")
    made = {"workspace": name, "instance": instance, "template": template.name, "version": template.version,
            "database": database, "odoo_service": template.odoo_service, "db_service": template.db_service,
            "values": values, "roles": {}}
    folder.mkdir(mode=0o755)
    try:
        _write(folder / INSTANCE_FILE, json.dumps({"template": template.name, "version": template.version,
                                                   "database": database}) + "\n")
        for index, role in enumerate(ROLES):
            project = folder / role
            project.mkdir(mode=0o755)
            # the customer's repositories of the role: OmniaCLI's user clones there
            addons = project / "addons"
            addons.mkdir(mode=0o755)
            os.chown(addons, caller_uid, -1)
            http_port, ws_port = ports[2 * index], ports[2 * index + 1]
            env = {**values, "PROJECT": f"{name}-{instance}-{role}", "HTTP_PORT": str(http_port),
                   "WEBSOCKET_PORT": str(ws_port), "DB_PASSWORD": stacktemplates.new_password(),
                   "SUBNET": subnets[index]}
            _write(project / "compose.yaml", template.compose)
            _write(project / ".env", stacktemplates.env_text(env), mode=0o600)
            (project / "config").mkdir(mode=0o755)
            _write(project / "config" / "odoo.conf",
                   stacktemplates.conf_text(template, database, stacktemplates.new_password(), []),
                   mode=0o644)
            for item in template.data:
                path = project / item["path"]
                path.mkdir(mode=0o755, parents=True)
                if item["owner"]:
                    os.chown(path, *_owner(item["owner"]))
            made["roles"][role] = {"folder": str(project), "http_port": http_port, "websocket_port": ws_port,
                                   "subnet": subnets[index]}
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return made


def _admin_password(conf: str) -> str | None:
    for line in conf.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "admin_passwd" and value.strip():
            return value.strip()
    return None


def role_state(project: Path) -> dict:
    """What conf gave the role last: repositories, parameters, debug."""
    try:
        data = json.loads((project / ROLE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"repositories": list(data.get("repositories") or []), "parameters": dict(data.get("parameters") or {}),
            "debug": bool(data.get("debug"))}


def conf(name: str, storage: str, instance: str, role: str, spec_json: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if role not in ROLES:
        raise HelperError(f"the role is {' or '.join(ROLES)}")
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    folder = mount / instance
    try:
        meta = json.loads((folder / INSTANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HelperError(f"{folder} holds no {INSTANCE_FILE}: not an instance made by this helper") from None
    try:
        spec = json.loads(spec_json)
    except ValueError:
        raise HelperError("the configuration is not JSON") from None
    if not isinstance(spec, dict):
        raise HelperError("the configuration is a JSON object {repositories, parameters, debug, bounds}")
    project = folder / role
    state = role_state(project)
    repos = spec.get("repositories", state["repositories"])
    if not isinstance(repos, list) or not all(isinstance(r, str) and REPOSITORY.fullmatch(r) for r in repos):
        raise HelperError("the repositories are a list of names (a-z A-Z 0-9 . _ -)")
    repos = list(dict.fromkeys(repos))
    for repo in repos:
        path = project / "addons" / repo
        if path.is_symlink() or not path.is_dir():
            raise HelperError(f"no repository {repo} in {project / 'addons'}")
    template = template_named(str(meta.get("template") or ""))
    given = spec.get("parameters", state["parameters"])
    bounds = spec.get("bounds") or {}
    if not isinstance(given, dict) or not isinstance(bounds, dict):
        raise HelperError("the parameters and the bounds are JSON objects")
    try:
        bounds = {str(k): (int(v[0]), int(v[1])) for k, v in bounds.items()}
    except (TypeError, ValueError, IndexError):
        raise HelperError("the bounds are {name: [min, max]}") from None
    # the parameters given are the whole set (the others take their default);
    # not given, those saved before that this template still has
    saved = {k: v for k, v in state["parameters"].items() if template.parameter(k) is not None}
    try:
        values = template.parameter_values(given if "parameters" in spec else saved, bounds)
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None
    debug = bool(spec.get("debug", state["debug"]))
    conf_file = project / "config" / "odoo.conf"
    try:
        password = _admin_password(conf_file.read_text(encoding="utf-8"))
    except OSError:
        password = None
    text = stacktemplates.conf_text(template, str(meta.get("database") or ""),
                                    password or stacktemplates.new_password(), repos, values, debug)
    _write(conf_file, text, mode=0o644)
    kept = {k: v for k, v in values.items() if v != template.parameter(k).default}
    _write(project / ROLE_FILE, json.dumps({"repositories": repos, "parameters": kept, "debug": debug}) + "\n")
    return {"workspace": name, "instance": instance, "role": role, "repositories": repos,
            "parameters": {k: ("" if template.parameter(k).type == "password" and v else v) for k, v in values.items()},
            "debug": debug, "template": template.name, "version": template.version}


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


def _env(project: Path) -> dict[str, str]:
    values = {}
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def instances_on_disk(mount: Path) -> list[str]:
    """The instances of a mounted workspace: <instance>/<role>/.env of every role."""
    return sorted(p.name for p in mount.iterdir()
                  if WORKSPACE_NAME.fullmatch(p.name) and not p.is_symlink() and p.is_dir()
                  and all((p / role / ".env").is_file() for role in ROLES))


def web_spec(spec_json: str, instances: list[str]) -> dict:
    """The names asked for, checked against what is on the disk."""
    try:
        spec = json.loads(spec_json)
    except ValueError:
        raise HelperError("the names are not JSON") from None
    if not isinstance(spec, dict):
        raise HelperError("the names are not a JSON object")
    domain = str(spec.get("domain") or "")
    if domain and not DOMAIN_NAME.fullmatch(domain):
        raise HelperError(f"{domain!r} is not a domain name")
    main = str(spec.get("main") or "")
    if main and main not in instances:
        raise HelperError(f"{main!r} is not an instance of the workspace")
    subdomains = spec.get("subdomains") or {}
    if not isinstance(subdomains, dict):
        raise HelperError("the subdomains are not a JSON object")
    for sub, target in subdomains.items():
        if not SUBDOMAIN.fullmatch(str(sub)) or sub == TEST_LABEL or sub in instances:
            raise HelperError(f"{sub!r} is not a subdomain the workspace may have")
        if target not in instances:
            raise HelperError(f"subdomain {sub}: {target!r} is not an instance of the workspace")
    certificate = str(spec.get("certificate") or "letsencrypt")
    if certificate not in CERTIFICATES:
        raise HelperError(f"the certificate is one of {', '.join(CERTIFICATES)}")
    return {"domain": domain, "main": main, "subdomains": {str(k): str(v) for k, v in subdomains.items()},
            "certificate": certificate}


def _sites(mount: Path, spec: dict, instances: list[str]) -> list[dict]:
    try:
        sites = web_names(spec["domain"], instances, spec["main"], spec["subdomains"])
    except ConfigError as exc:
        raise HelperError(str(exc)) from None
    for site in sites:
        project = mount / site["instance"] / site["role"]
        env = _env(project)
        try:
            conf = (project / "config" / "odoo.conf").read_text(encoding="utf-8")
        except OSError:
            conf = ""
        if not (env.get("HTTP_PORT", "").isdigit() and env.get("WEBSOCKET_PORT", "").isdigit()):
            raise HelperError(f"{project}/.env has no HTTP_PORT or WEBSOCKET_PORT")
        site["http_port"] = int(env["HTTP_PORT"])
        site["websocket_port"] = int(env["WEBSOCKET_PORT"]) if workspaceweb.workers(conf) else None
    return sites


def _tls(name: str, certificate: str) -> tuple[str, str] | None:
    folder = OWN_CERTS / name if certificate == "own" else LETSENCRYPT / f"omniacli-{name}"
    chain, key = folder / "fullchain.pem", folder / "privkey.pem"
    return (str(chain), str(key)) if chain.is_file() and key.is_file() else None


def _nginx_reload() -> str:
    if _run("systemctl", "is-active", "nginx", check=False).strip() != "active":
        return "nginx is not running: the site is written, it is read when nginx starts"
    _run("systemctl", "reload", "nginx")
    return "nginx reloaded"


def _put_site(path: Path, text: str | None) -> str:
    """The site written (None: removed), nginx -t, or the file before back."""
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    if text == before:
        return "the site is unchanged"
    if text is None:
        path.unlink()
    else:
        _write(path, text)
    test = subprocess.run([_tool("nginx"), "-t"], env=ENV, capture_output=True, text=True)
    if test.returncode:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _write(path, before)
        raise HelperError(f"nginx -t refused the site, the one before is back: {test.stderr.strip()}")
    return _nginx_reload()


def _certbot(name: str, names: list[str]) -> str | None:
    """Let's Encrypt's certificate for names: None, or why it failed."""
    email = str((_system().get("workspaces") or {}).get("acme_email") or "")
    if email and not EMAIL.fullmatch(email):
        raise HelperError(f"workspaces.acme_email of {SYSTEM_CONFIG} is not an email address")
    cmd = [_tool("certbot"), "certonly", "--webroot", "-w", workspaceweb.ACME_ROOT,
           "--cert-name", f"omniacli-{name}", "--non-interactive", "--agree-tos", "--keep-until-expiring",
           "--deploy-hook", "systemctl reload nginx",
           *(["-m", email] if email else ["--register-unsafely-without-email"])]
    for host in names:
        cmd += ["-d", host]
    done = subprocess.run(cmd, env=ENV, capture_output=True, text=True)
    return (done.stderr or done.stdout).strip() or f"certbot failed ({done.returncode})" if done.returncode else None


def _hash_bucket(dump: str) -> None:
    """The names of a workspace are longer than nginx's default (64) holds:
    omniacli.conf sets more, once, unless the machine's nginx sets it (a
    second time is an error)."""
    path = NGINX_SITES / "omniacli.conf"
    if path.exists() or re.search(r"^\s*server_names_hash_bucket_size\s", dump, re.MULTILINE):
        return
    _write(path, "# written by omniacli workspace-helper: room for the long names of the workspaces\n"
                 f"server_names_hash_bucket_size {workspaceweb.HASH_BUCKET};\n")


def web(name: str, storage: str, spec_json: str) -> dict:
    image, mount = workspace(name, storage)
    path = NGINX_SITES / f"omniacli-{name}.conf"
    instances = instances_on_disk(mount) if mounted(image, mount) else []
    spec = web_spec(spec_json, instances)
    made = {"workspace": name, "site": str(path), "names": [], "https": False, "certificate_error": None}
    if not spec["domain"] or not instances:
        made["nginx"] = _put_site(path, None) if path.exists() else "no site"
        lineage = LETSENCRYPT / f"omniacli-{name}"
        if lineage.exists():
            _run("certbot", "delete", "--cert-name", f"omniacli-{name}", "--non-interactive")
        return made
    sites = _sites(mount, spec, instances)
    names = [n for site in sites for n in site["names"]]
    made["names"] = names
    dump = subprocess.run([_tool("nginx"), "-T"], env=ENV, capture_output=True, text=True)
    if dump.returncode:
        raise HelperError(f"nginx -T: {dump.stderr.strip()}")
    taken = workspaceweb.clashes(names, workspaceweb.names_elsewhere(dump.stdout, str(path)))
    if taken:
        raise HelperError("names other sites of nginx answer on: " + ", ".join(taken))
    _hash_bucket(dump.stdout)
    acme = Path(workspaceweb.ACME_ROOT)
    acme.mkdir(mode=0o755, exist_ok=True)
    ipv6 = Path("/proc/net/if_inet6").exists()
    tls = _tls(name, spec["certificate"])
    made["nginx"] = _put_site(path, workspaceweb.conf_text(name, sites, tls, ipv6))
    if spec["certificate"] == "letsencrypt":
        made["certificate_error"] = _certbot(name, names)
        if made["certificate_error"] is None and tls is None:
            tls = _tls(name, spec["certificate"])
            made["nginx"] = _put_site(path, workspaceweb.conf_text(name, sites, tls, ipv6))
    elif tls is None:
        made["certificate_error"] = "no certificate of the customer's yet: upload it"
    made["https"] = tls is not None
    return made


def _openssl(*args: str) -> str:
    done = subprocess.run([_tool("openssl"), *args], env=ENV, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if done.returncode:
        raise HelperError(f"openssl {args[0]}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def owncert(name: str, storage: str, given: str | None = None) -> dict:
    workspace(name, storage)
    text = sys.stdin.read(MAX_PEM + 1) if given is None else given
    if len(text) > MAX_PEM:
        raise HelperError(f"more than {MAX_PEM // 1024} KB on the standard input")
    try:
        data = json.loads(text)
    except ValueError:
        raise HelperError("the standard input is not JSON {certificate, key}") from None
    if not isinstance(data, dict):
        raise HelperError("the standard input is not JSON {certificate, key}")
    chain, key = str(data.get("certificate") or ""), str(data.get("key") or "")
    if "-----BEGIN CERTIFICATE-----" not in chain or "PRIVATE KEY-----" not in key:
        raise HelperError("a certificate (PEM, with its chain) and its private key (PEM) are needed")
    OWN_CERTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = OWN_CERTS / name
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    new_chain, new_key = folder / "fullchain.pem.new", folder / "privkey.pem.new"
    try:
        _write(new_chain, chain.strip() + "\n", mode=0o600)
        _write(new_key, key.strip() + "\n", mode=0o600)
        if _openssl("x509", "-in", str(new_chain), "-noout", "-pubkey") != \
                _openssl("pkey", "-in", str(new_key), "-pubout", "-passin", "pass:"):
            raise HelperError("the key is not the certificate's")
        if subprocess.run([_tool("openssl"), "x509", "-in", str(new_chain), "-noout", "-checkend", "0"],
                          env=ENV, capture_output=True, stdin=subprocess.DEVNULL).returncode:
            raise HelperError("the certificate has expired")
        not_after = _openssl("x509", "-in", str(new_chain), "-noout", "-enddate").strip().partition("=")[2]
        alt = subprocess.run([_tool("openssl"), "x509", "-in", str(new_chain), "-noout", "-ext", "subjectAltName"],
                             env=ENV, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        os.replace(new_chain, folder / "fullchain.pem")
        os.replace(new_key, folder / "privkey.pem")
    finally:
        new_chain.unlink(missing_ok=True)
        new_key.unlink(missing_ok=True)
    return {"workspace": name, "not_after": not_after,
            "names": [part.strip()[4:] for part in alt.replace("\n", ",").split(",") if part.strip().startswith("DNS:")]}


ACTIONS = {"disk": (disk, 3), "create": (create, 5), "remove": (remove, 3), "web": (web, 3),
           "owncert": (owncert, 2), "firewall": (firewall, 1), "template-install": (template_install, 1),
           "conf": (conf, 5)}


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
