# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The root helper of the shell sandboxes, its system commands recorded (the
real commands were tried as root: README, Shell)."""
import json
import os
import types

import pytest

from omniacli import sandboxhelper, workspacehelper
from omniacli.composehelper import HelperError

MB = 1024 * 1024


@pytest.fixture
def root(monkeypatch, tmp_path):
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    shadow = tmp_path / "shadow"
    shadow.write_text("root:*:19000:0:99999:7:::\n")
    monkeypatch.setattr(sandboxhelper, "ROOT", tmp_path / "sandbox")
    monkeypatch.setattr(sandboxhelper, "SHADOW", shadow)
    monkeypatch.setattr(workspacehelper, "SYSTEMD", systemd)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    users, calls, state = {}, [], {"mounted": set()}

    def run(*cmd, check=True):
        calls.append(list(cmd))
        if cmd[0] == "systemd-escape":
            return cmd[-1].strip("/").replace("/", "-") + ".mount\n"
        if cmd[:3] == ("systemctl", "enable", "--now"):
            state["mounted"].add(cmd[3])
        if cmd[:3] == ("systemctl", "disable", "--now"):
            state["mounted"].discard(cmd[3])
        if cmd[0] == "useradd":
            users[cmd[-1]] = types.SimpleNamespace(pw_uid=900 + len(users), pw_gid=990)
        if cmd[0] == "userdel":
            users.pop(cmd[-1])
        if cmd[0] == "usermod":
            lines = [line for line in shadow.read_text().splitlines() if not line.startswith(cmd[-1] + ":")]
            lines.append(f"{cmd[-1]}:!:19000:0:99999:7::{cmd[2]}:")
            shadow.write_text("\n".join(lines) + "\n")
        if cmd[:2] == ("findmnt", "--noheadings"):
            return "rw,nosuid,nodev,relatime\n"
        return ""
    monkeypatch.setattr(workspacehelper, "_run", run)
    monkeypatch.setattr(workspacehelper, "mounted", lambda image, mount: workspacehelper.unit_name(mount) in state["mounted"])
    monkeypatch.setattr(workspacehelper, "loop_of", lambda image: "/dev/loop7")
    # a home is a mount point while its unit is enabled
    monkeypatch.setattr(os.path, "ismount", lambda path: str(path).strip("/").replace("/", "-") + ".mount"
                        in state["mounted"])
    monkeypatch.setattr(sandboxhelper, "_exists", lambda user: user in users)
    monkeypatch.setattr(sandboxhelper.pwd, "getpwnam", lambda user: users[user])
    return {"calls": calls, "users": users, "systemd": systemd, "shadow": shadow, "root": tmp_path / "sandbox"}


def test_ensure_makes_the_sandbox_once_then_grows_it(root):
    made = sandboxhelper.ensure("anna@example", "100")
    home = root["root"] / "home" / "1"
    image = root["root"] / "disks" / "1.img"
    assert made == {"user": "omniacli-sb-1", "home": str(home), "mb": 100}
    assert image.stat().st_size == 100 * MB and image.stat().st_mode & 0o777 == 0o600
    assert ["mkfs.ext4", "-q", "-F", str(image)] in root["calls"]
    assert ["chattr", "+i", str(home)] in root["calls"]
    unit = (root["systemd"] / workspacehelper.unit_name(home)).read_text()
    assert f"What={image}" in unit and "Options=loop,nosuid,nodev\n" in unit and "docker" not in unit
    useradd = next(c for c in root["calls"] if c[0] == "useradd")
    assert useradd[-1] == "omniacli-sb-1" and "--system" in useradd and ["--gid", "omniacli-sandbox"] == \
        useradd[useradd.index("--gid"):useradd.index("--gid") + 2] and "/bin/bash" in useradd
    assert (home / ".bash_profile").read_text().startswith("# written by omniacli sandbox-helper")
    assert oct(home.stat().st_mode & 0o777) == oct(0o700)
    assert json.loads((root["root"] / "sandboxes.json").read_text()) == {"anna@example": 1}
    assert oct(root["root"].stat().st_mode & 0o777) == oct(0o711)
    assert oct((root["root"] / "disks").stat().st_mode & 0o777) == oct(0o700)

    # again: the same user, nothing made again
    root["calls"].clear()
    assert sandboxhelper.ensure("anna@example", "100")["user"] == "omniacli-sb-1"
    assert not any(c[0] in ("mkfs.ext4", "useradd", "resize2fs") for c in root["calls"])
    # larger: grown while mounted; smaller: left as it is
    assert sandboxhelper.ensure("anna@example", "250")["mb"] == 250
    assert ["losetup", "--set-capacity", "/dev/loop7"] in root["calls"] and ["resize2fs", "/dev/loop7"] in root["calls"]
    assert sandboxhelper.ensure("anna@example", "100")["mb"] == 250
    # another user, another number
    assert sandboxhelper.ensure("luca", "100")["user"] == "omniacli-sb-2"


def test_a_profile_planted_by_the_user_is_kept(root):
    home = root["root"] / "home" / "1"
    sandboxhelper.ensure("anna", "100")
    (home / ".bash_profile").unlink()
    (home / ".bash_profile").symlink_to(root["shadow"])
    sandboxhelper.ensure("anna", "100")
    # never written through the link
    assert root["shadow"].read_text().startswith("root:")


def test_disable_enable_delete(root):
    sandboxhelper.ensure("anna", "100")
    assert sandboxhelper.disable("anna") == {"user": "omniacli-sb-1", "disabled": True}
    assert ["usermod", "--expiredate", "1", "omniacli-sb-1"] in root["calls"]
    assert ["pkill", "-KILL", "-u", "omniacli-sb-1"] in root["calls"]
    with pytest.raises(HelperError, match="the sandbox of anna is disabled"):
        sandboxhelper.ensure("anna", "100")
    assert sandboxhelper.enable("anna") == {"user": "omniacli-sb-1", "enabled": True}
    assert sandboxhelper.ensure("anna", "100")["user"] == "omniacli-sb-1"

    home, image = root["root"] / "home" / "1", root["root"] / "disks" / "1.img"
    unit = workspacehelper.unit_name(home)
    # the test's folder is no mount point: what the unmount left behind goes
    for leftover in home.iterdir():
        leftover.unlink()
    assert sandboxhelper.delete("anna") == {"user": "omniacli-sb-1", "deleted": True}
    assert ["systemctl", "disable", "--now", unit] in root["calls"] and ["userdel", "omniacli-sb-1"] in root["calls"]
    assert not image.exists() and not home.exists() and not (root["systemd"] / unit).exists()
    assert json.loads((root["root"] / "sandboxes.json").read_text()) == {}
    # nothing to do for a user with no sandbox
    assert sandboxhelper.delete("anna") == {"user": "", "deleted": False}
    assert sandboxhelper.disable("nobody") == {"user": "", "disabled": False}
    assert sandboxhelper.enable("nobody") == {"user": "", "enabled": False}


def test_a_sandbox_must_fit(root, monkeypatch):
    class Room:
        f_frsize, f_bavail = 1, 60 * MB
    monkeypatch.setattr(sandboxhelper.os, "statvfs", lambda path: Room)
    with pytest.raises(HelperError, match="a sandbox of 100 MB needs 100 MB more .* 60 MB free"):
        sandboxhelper.ensure("anna", "100")


@pytest.mark.parametrize("args, message", [
    (["ensure", "anna x", "100"], "not an OmniaCLI user name"),
    (["ensure", "anna", "49"], "the size is 50 to 102400 MB"),
    (["ensure", "anna", "1e3"], "the size is"),
    (["ensure", "anna"], "takes 2 arguments"),
    (["format", "anna"], "usage: sandbox-helper"),
    (["delete", "../x"], "not an OmniaCLI user name"),
])
def test_wrong_arguments(root, monkeypatch, args, message):
    monkeypatch.setattr(sandboxhelper.os, "geteuid", lambda: 0)
    with pytest.raises(HelperError, match=message):
        sandboxhelper.run(args)


def test_only_as_root(root):
    if os.geteuid() == 0:
        pytest.skip("run as root")
    assert sandboxhelper.main(["ensure", "anna", "100"]) == 2
