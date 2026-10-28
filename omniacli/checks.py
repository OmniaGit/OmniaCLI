# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checks "Test & activate" runs before a server goes live.

Every check looks at the real thing (the file, the database, the remote, the
service, sudo) as the user running OmniaCLI, so a server that passes can be
operated by OmniaCLI.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from . import ops, packages
from . import composehelper
from .config import Instance, Server
from .jobs import JobFailed
from .plugin_api import ERROR, WARNING, Check, system_user

GIT_TIMEOUT = 30

__all__ = ["ERROR", "WARNING", "Check", "passed", "check_server"]


def passed(checks: list[Check]) -> bool:
    """True when no check of level error failed (warnings do not block)."""
    return all(c.ok or c.level == WARNING for c in checks)


async def _instance_checks(instance: Instance) -> list[Check]:
    """The plugin's checks of the instance, then the core's: the log, the
    repositories, the service and the right to restart it."""
    target = instance.role
    try:
        checks: list[Check] = list(await instance.plugin.checks(instance))
    except Exception as exc:  # noqa: BLE001 - a plugin's bug is a failed check, not a crash
        checks = [Check(target, f"{instance.plugin.label} checks", False,
                        f"the plugin {instance.type} failed: {type(exc).__name__}: {exc}")]

    def add(name, ok, detail, level=ERROR):
        checks.append(Check(target, name, ok, detail, level))

    label = instance.plugin.label
    config = instance.plugin.process_config(instance)
    # log file
    process = instance.restart.type == "process"
    if process:
        folder = instance.log_file.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder):
                pass
            add("Log folder", True, f"{folder} writable: {label}'s output goes to {instance.log_file}")
        except OSError as exc:
            add("Log folder", False, f"{folder}: {exc}")
    else:
        readable = os.access(instance.log_file, os.R_OK)
        add("Log file", readable,
            f"{instance.log_file} readable" if readable
            else f"{instance.log_file} is not readable: the log page cannot show it", WARNING)

    # repositories
    for repo in instance.repositories:
        name = f"Repository {repo.name}"
        if not repo.path.is_dir():
            add(name, False, f"{repo.path} does not exist")
            continue
        code, branch = await ops.capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo.path)
        if code:
            add(name, False, branch.splitlines()[0] if branch else "not a git repository")
            continue
        problems = []
        if branch != repo.branch:
            problems.append(f"on branch '{branch}', expected '{repo.branch}'")
        _, changes = await ops.capture(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo.path)
        if changes and instance.keeps_local_changes:
            add(f"{name}: local changes", False,
                f"{len(changes.splitlines())} uncommitted change(s), kept: a test instance without "
                "auto update; a pull stops only if it would overwrite them, a merge to "
                "production refuses them", WARNING)
        elif changes:
            problems.append(f"{len(changes.splitlines())} uncommitted change(s)")
        code, out = await ops.capture(
            ["git", "ls-remote", "--exit-code", "--heads", repo.remote, repo.branch],
            cwd=repo.path, timeout=GIT_TIMEOUT,
        )
        if code:
            problems.append(
                f"remote {repo.remote} branch {repo.branch} not reachable"
                + (f": {out.splitlines()[-1]}" if out else ""))
        add(name, not problems,
            "; ".join(problems) if problems else
            f"{repo.path} on {repo.branch}, clean, {repo.remote}/{repo.branch} reachable")

    if instance.package is not None:
        await _package_checks(instance, add)

    # service and the right to restart it
    restart = instance.restart
    if process:
        own = ops.managed_pid(instance)
        others = ops.foreign_pids(instance, own)
        if others:
            add(f"{label} process", False,
                f"running with {config} but not started by OmniaCLI "
                f"(pid {', '.join(map(str, others))}): Restart refuses to start a second one")
        elif own is not None:
            add(f"{label} process", True, f"started by OmniaCLI, pid {own}")
        else:
            add(f"{label} process", False, "not running: Restart starts it", WARNING)
        return checks
    if restart.type == "systemd":
        code, state = await ops.capture(
            ["systemctl", "show", "-p", "LoadState", "--value", restart.service])
        add("Service", code == 0 and state == "loaded",
            f"systemd unit {restart.service}: {state or 'unknown'}")
    elif restart.type == "compose":
        # what the root helper refuses, seen as the user running OmniaCLI:
        # whoever may change the project's files is root through it
        import pwd

        found = composehelper.problems(Path(restart.service), os.getuid(),
                                       os.getgrouplist(pwd.getpwuid(os.getuid()).pw_name, os.getgid()))
        add("Service", not found,
            f"compose project {restart.service}: safe from {system_user()}" if not found else
            f"the compose-helper refuses {restart.service}: " + "; ".join(found))
        add("Docker", shutil.which("docker") is not None,
            "docker found" if shutil.which("docker") else "docker is not installed (or not in the PATH)")
    else:
        script = Path(f"/etc/init.d/{restart.service}")
        add("Service", os.access(script, os.X_OK),
            f"{script} {'exists' if os.access(script, os.X_OK) else 'not found or not executable'}")
    command = restart.command()
    if restart.sudo:
        async def refused(actions) -> list[str]:
            """The sudoers lines of the actions' commands sudo refuses:
            sudo -n -l <command> is 0 when sudoers lets this user run
            exactly it without a password."""
            lines = []
            for action in actions:
                other = restart.command(action)
                code, _ = await ops.capture(["sudo", "-n", "-l", *other[2:]])
                if code:
                    executable = shutil.which(other[2]) or other[2]
                    lines.append(f"{system_user()} ALL=(root) NOPASSWD: {' '.join([executable, *other[3:]])}")
            return lines

        if restart.type == "compose":
            # one line lets every action of the helper on this project
            code, _ = await ops.capture(["sudo", "-n", "-l", *command[2:]])
            add("Restart permission", code == 0,
                "sudo allows the compose-helper without a password" if code == 0 else
                "sudo refuses the compose-helper or asks a password; add to /etc/sudoers.d/omniacli: "
                f"{system_user()} ALL=(root) NOPASSWD: {command[2]} compose-helper {restart.service} *")
            return checks
        missing = await refused(("restart",))
        add("Restart permission", not missing,
            "sudo allows it without a password" if not missing else
            "sudo refuses it or asks a password; add to /etc/sudoers.d/omniacli: " + " and ".join(missing))
        if restart.type == "systemd":
            # Start and Stop buttons: not needed to deploy, so a warning
            missing = await refused(("start", "stop"))
            add("Start/Stop permission", not missing,
                "sudo allows them without a password" if not missing else
                "Start and Stop will fail; add to /etc/sudoers.d/omniacli: " + " and ".join(missing),
                WARNING)
    else:
        add("Restart permission", True,
            f"runs '{' '.join(command)}' without sudo: not tried, it would restart the service",
            WARNING)
    return checks


async def _package_checks(instance: Instance, add) -> None:
    """The signed manifest is published, the tools are there, the folder is
    writable, sudo allows the install."""
    package = instance.package
    try:
        manifest = await packages.published(package)
        add("Package", True, f"{package.url}: version {manifest['version']}, built {manifest.get('built')}, "
                             "manifest signed by the public key")
    except JobFailed as exc:
        add("Package", False, str(exc))
    missing = [tool for tool in ("dpkg-deb", "dpkg-query", "dpkg") if not shutil.which(tool)]
    if not os.access(packages.APT_GET, os.X_OK):
        missing.append(packages.APT_GET)
    add("Package tools", not missing,
        "dpkg-deb, dpkg-query, dpkg and apt-get found" if not missing else f"{', '.join(missing)} not found")
    try:
        package.folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=package.folder):
            pass
        add("Package folder", True, f"{package.folder} writable")
    except OSError as exc:
        add("Package folder", False, f"{package.folder}: {exc}")
    version = await packages.installed_version(package.name)
    add("Installed package", version is not None,
        f"{package.name} {version}" if version else
        f"{package.name} is not installed: the first update installs it", WARNING)
    code, _ = await ops.capture(["sudo", "-n", "-l", *packages.install_command(package)[2:]])
    add("Package install permission", code == 0,
        "sudo allows apt-get to install it without a password" if code == 0 else
        "sudo refuses it or asks a password; add to /etc/sudoers.d/omniacli: "
        + packages.sudoers_line(package, system_user()))


async def check_server(server: Server, backup_dir: Path) -> list[Check]:
    if server.missing_plugin:
        return [Check("server", "Plugin", False, server.error or f"no plugin {server.type}")]
    if server.error:
        # a draft saved incomplete: nothing to check until it is completed
        return [Check("server", "Settings", False, f"incomplete: {server.error}")]
    checks: list[Check] = []
    prod, test = server.production, server.test
    if prod and test:
        shared = {str(r.path) for r in prod.repositories} & {str(r.path) for r in test.repositories}
        checks.append(Check("server", "Separate working copies", not shared,
                            "production and test use different folders" if not shared
                            else f"production and test share {', '.join(sorted(shared))}"))
        checks.extend(prod.plugin.server_checks(prod, test))
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=backup_dir):
            pass
        checks.append(Check("server", "Backup folder", True, f"{backup_dir} writable"))
    except OSError as exc:
        checks.append(Check("server", "Backup folder", False, f"{backup_dir}: {exc}"))
    results = await asyncio.gather(*(_instance_checks(i) for i in server.instances.values()))
    for result in results:
        checks.extend(result)
    return checks
