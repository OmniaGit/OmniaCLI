# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The root helper of the shell sandboxes, run through sudo by OmniaCLI's
user: on a server the Shell of a user opens as a Linux user of its own,
without privileges, whose home is a small disk of its own.

    omniacli sandbox-helper ensure <user> <size in MB>
    omniacli sandbox-helper disable <user>
    omniacli sandbox-helper enable <user>
    omniacli sandbox-helper delete <user>

<user> is the name of the OmniaCLI user (the web interface's). Its sandbox
is the Linux user omniacli-sb-<n> (OmniaCLI's names are not Linux names),
in group omniacli-sandbox, with no password: nobody logs in as it or su's
to it; OmniaCLI opens its shell with the one sudoers line

    <OmniaCLI's user> ALL=(%omniacli-sandbox) NOPASSWD: /bin/bash --login

(the commands on the shell's standard input: an argument more is refused).
From there `su -` with a password is the way to root. The table of the
names is ROOT/sandboxes.json, root's.

ensure: made at the first call, then the same; its home is ROOT/home/<n>, an
ext4 image ROOT/disks/<n>.img that takes room as it is used, mounted
nosuid,nodev by a systemd mount unit at every boot, the home 700 and its
~/.bash_profile starting the shell in the home (OmniaCLI's user cannot enter
it). A larger size grows the disk, mounted; a disk is never made smaller (a
smaller size leaves it as it is: OmniaCLI refuses it first). A disabled
sandbox is refused. It answers {"user", "home", "mb"}.

disable: the account expires (no login, no su into it) and every process of
it is killed; sudo still runs it, so OmniaCLI refuses a disabled user
itself. What was su'd to another user (root) is that user's: not killed.
enable: the expiry taken off. delete: its processes killed, the disk
unmounted and removed, the Linux user deleted. disable, enable and delete
of a user with no sandbox do nothing.

Not checked here, by design: the files of this machine that every user may
read (an Odoo configuration of mode 644, say) are readable from a sandbox
too; /tmp, /var/tmp and /dev/shm are outside its disk's limit (README,
Shell).
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import sys
from pathlib import Path

from . import workspacehelper as wh
from .composehelper import HelperError
from .config import MEMBER_NAME, SANDBOX_MB_LIMITS

ROOT = Path("/var/lib/omniacli-sandbox")
SHADOW = Path("/etc/shadow")
GROUP = "omniacli-sandbox"
PREFIX = "omniacli-sb-"
MB = 1024 * 1024
SHELL = "/bin/bash"
PROFILE = ("# written by omniacli sandbox-helper: OmniaCLI opens this shell from a folder\n"
           "# it cannot enter, so it starts in the home; then the usual profile\n"
           "cd ~ || true\n"
           "[ -f ~/.profile ] && . ~/.profile\n")


def _name(value: str) -> str:
    if not MEMBER_NAME.fullmatch(value):
        raise HelperError(f"{value!r} is not an OmniaCLI user name (1-64 letters, digits or . _ @ -)")
    return value


def _table_path() -> Path:
    return ROOT / "sandboxes.json"


def _table() -> dict[str, int]:
    try:
        data = json.loads(_table_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise HelperError(f"cannot read {_table_path()}: {exc}") from None
    return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_table(table: dict[str, int]) -> None:
    wh._write(_table_path(), json.dumps(table, indent=1, sort_keys=True) + "\n", mode=0o600)


def _folders() -> None:
    """ROOT and its homes may be crossed (711), not listed, by the sandboxes;
    the disks are root's only."""
    for folder, mode in ((ROOT, 0o711), (ROOT / "home", 0o711), (ROOT / "disks", 0o700)):
        if folder.is_symlink():
            raise HelperError(f"{folder} is a symbolic link")
        folder.mkdir(mode=mode, exist_ok=True)
        os.chown(folder, 0, 0)
        os.chmod(folder, mode)


class _Lock:
    """One helper at a time changes the table and the users."""

    def __enter__(self):
        _folders()
        self.fd = os.open(ROOT / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def _paths(number: int) -> tuple[str, Path, Path]:
    return f"{PREFIX}{number}", ROOT / "disks" / f"{number}.img", ROOT / "home" / str(number)


def _expired(user: str) -> bool:
    """The account's expiry (field 8 of the shadow file) is set."""
    try:
        lines = SHADOW.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HelperError(f"cannot read {SHADOW}: {exc}") from None
    for line in lines:
        fields = line.split(":")
        if fields[0] == user and len(fields) > 7:
            return fields[7] != ""
    return False


def _exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def _unit_text(image: Path, mount: Path, name: str) -> str:
    return ("# written by omniacli sandbox-helper: the shell sandbox of OmniaCLI user " + name + "\n"
            "[Unit]\nDescription=OmniaCLI shell sandbox " + name + "\n"
            f"[Mount]\nWhat={image}\nWhere={mount}\nType=ext4\nOptions={wh.MOUNT_OPTIONS}\n"
            "[Install]\nWantedBy=multi-user.target\n")


def _size(mb: str) -> int:
    low, high = SANDBOX_MB_LIMITS
    if not (mb.isdigit() and low <= int(mb) <= high):
        raise HelperError(f"the size is {low} to {high} MB, not {mb!r}")
    return int(mb) * MB


def _fits(image: Path, size: int) -> None:
    vfs = os.statvfs(ROOT)
    free, needed = vfs.f_bavail * vfs.f_frsize, size - wh._used(image)
    if needed > free:
        raise HelperError(f"a sandbox of {size // MB} MB needs {needed / MB:.0f} MB more on the disk of {ROOT}, "
                          f"which has {free / MB:.0f} MB free")


def _profile(home: Path, uid: int, gid: int) -> None:
    """~/.bash_profile, once: never through a link the user planted there."""
    try:
        fd = os.open(home / ".bash_profile", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchown(fh.fileno(), uid, gid)
        fh.write(PROFILE)


def ensure(name: str, mb: str) -> dict:
    name, size = _name(name), _size(mb)
    with _Lock():
        table = _table()
        if name not in table:
            table[name] = max(table.values(), default=0) + 1
            _save_table(table)
        user, image, home = _paths(table[name])
        if _exists(user) and _expired(user):
            raise HelperError(f"the sandbox of {name} is disabled")
        if not image.exists() or image.stat().st_size < size:
            _fits(image, size)
        grow = wh.image_to_size(image, size)
        wh.mount_folder(home)
        unit = wh.unit_name(home)
        text, path = _unit_text(image, home, name), wh.SYSTEMD / unit
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            wh._write(path, text)
            wh._run("systemctl", "daemon-reload")
        # mounted already: not started again (a start job of a mounted unit
        # waits for its loop device's unit, which a machine without udev, a
        # container, never has)
        wh._run("systemctl", "enable", *(() if wh.mounted(image, home) else ("--now",)), unit)
        if not wh.mounted(image, home):
            raise HelperError(f"{unit} did not mount {image} on {home}")
        wh._harden_mount(home)
        if grow:
            wh.grow_mounted(image)
        if not _exists(user):
            wh._run("getent", "group", GROUP, check=False) or wh._run("groupadd", "--system", GROUP)
            wh._run("useradd", "--system", "--no-create-home", "--home-dir", str(home), "--shell", SHELL,
                    "--gid", GROUP, "--comment", f"OmniaCLI shell sandbox of {name}", user)
        entry = pwd.getpwnam(user)
        os.chown(home, entry.pw_uid, entry.pw_gid)
        os.chmod(home, 0o700)
        _profile(home, entry.pw_uid, entry.pw_gid)
        return {"user": user, "home": str(home), "mb": image.stat().st_size // MB}


def _known(name: str) -> str | None:
    """The Linux user of name's sandbox, None: it has none."""
    number = _table().get(_name(name))
    if number is None:
        return None
    user = _paths(number)[0]
    return user if _exists(user) else None


def disable(name: str) -> dict:
    with _Lock():
        user = _known(name)
        if user:
            wh._run("usermod", "--expiredate", "1", user)
            wh._run("pkill", "-KILL", "-u", user, check=False)
        return {"user": user or "", "disabled": bool(user)}


def enable(name: str) -> dict:
    with _Lock():
        user = _known(name)
        if user:
            wh._run("usermod", "--expiredate", "", user)
        return {"user": user or "", "enabled": bool(user)}


def delete(name: str) -> dict:
    with _Lock():
        table = _table()
        number = table.get(_name(name))
        if number is None:
            return {"user": "", "deleted": False}
        user, image, home = _paths(number)
        if _exists(user):
            wh._run("pkill", "-KILL", "-u", user, check=False)
        unit = wh.unit_name(home)
        wh._run("systemctl", "disable", "--now", unit, check=False)
        (wh.SYSTEMD / unit).unlink(missing_ok=True)
        wh._run("systemctl", "daemon-reload")
        if os.path.ismount(home):
            raise HelperError(f"{home} is still mounted: its sandbox is not deleted")
        if home.exists():
            wh._run("chattr", "-i", str(home), check=False)
            home.rmdir()
        image.unlink(missing_ok=True)
        if _exists(user):
            wh._run("userdel", user)
        del table[name]
        _save_table(table)
        return {"user": user, "deleted": True}


ACTIONS = {"ensure": (ensure, 2), "disable": (disable, 1), "enable": (enable, 1), "delete": (delete, 1)}


def run(argv: list[str]) -> dict:
    if not argv or argv[0] not in ACTIONS:
        raise HelperError(f"usage: sandbox-helper {' | '.join(ACTIONS)} ...")
    action, count = ACTIONS[argv[0]]
    if len(argv) - 1 != count:
        raise HelperError(f"{argv[0]} takes {count} arguments (README, Shell)")
    if os.geteuid() != 0:
        raise HelperError("run it as root, through sudo")
    return action(*argv[1:])


def main(argv: list[str]) -> int:
    try:
        print(json.dumps(run(argv)))
    except HelperError as exc:
        print(f"sandbox-helper: {exc}", file=sys.stderr)
        return 2
    return 0
