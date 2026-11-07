# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The storages' binds: each folder of workspaces.storage is bound on a root
path by systemd, its identity kept and checked at every boot. The system
commands are recorded; a bind is a set of mounted paths."""
from pathlib import Path

import pytest

from omniacli import config, workspacehelper
from omniacli.composehelper import HelperError

from .conftest import REAL_BIND_STORAGE, REAL_BOUND_ROOT


def test_bound_root():
    one = REAL_BOUND_ROOT("/media/disk2/omniacli")
    assert one.startswith(config.STORAGE_BASE + "/media-disk2-omniacli-") and one == REAL_BOUND_ROOT("/media/disk2/omniacli")
    # paths that would make the same name still differ, and only sudoers' and systemd's letters
    assert REAL_BOUND_ROOT("/a-b") != REAL_BOUND_ROOT("/a/b")
    assert all(c.isalnum() or c in "._-/" for c in REAL_BOUND_ROOT("/srv/omniacli/default_workspace"))


@pytest.fixture
def binds(monkeypatch, tmp_path):
    """The real bind_storage with the system's commands faked: systemctl start
    of a bind unit mounts it, umount unmounts it."""
    source = tmp_path / "disk" / "mine" / "workspaces"
    source.mkdir(parents=True)
    base = tmp_path / "base"
    base.mkdir()
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    monkeypatch.setattr(config, "bound_root", lambda storage: str(base / Path(REAL_BOUND_ROOT(storage)).name))
    monkeypatch.setattr(workspacehelper, "bind_storage", REAL_BIND_STORAGE)
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config_path=None: (str(source),))
    monkeypatch.setattr(workspacehelper, "SYSTEMD", systemd)
    monkeypatch.setattr(workspacehelper, "STORAGE_IDS", tmp_path / "ids")
    monkeypatch.setattr(workspacehelper, "_safe_folder", lambda folder, what: None)
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("omniacli", 1000, [1000]))
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *args: None)
    # the test's folders are the test user's: taken as root's alone unless a test says otherwise
    owner = {"problem": None}
    monkeypatch.setattr(workspacehelper, "_root_only", lambda path: owner["problem"])
    mounted, calls = set(), []

    def run(*cmd, check=True):
        calls.append(list(cmd))
        if cmd[0] == "systemd-escape":
            return Path(cmd[-1]).name + ".mount\n"
        if cmd[:2] == ("systemctl", "start") and cmd[2].endswith(".mount"):
            mounted.add(str(base / cmd[2][: -len(".mount")]))
        if cmd[:2] == ("systemctl", "stop") and cmd[2].endswith(".mount"):
            mounted.discard(str(base / cmd[2][: -len(".mount")]))
        if cmd[0] == "umount":
            mounted.discard(cmd[1])
        if cmd[0] == "findmnt":
            return "\n".join(sorted(mounted))
        return ""
    monkeypatch.setattr(workspacehelper, "_run", run)
    monkeypatch.setattr(workspacehelper, "_is_mount", lambda path: str(path) in mounted)
    return {"source": source, "target": workspacehelper.bound(str(source)), "systemd": systemd, "calls": calls,
            "mounted": mounted, "owner": owner, "ids": tmp_path / "ids"}


def test_a_storage_is_bound_and_checked(binds):
    source, target = str(binds["source"]), binds["target"]
    assert workspacehelper.bind_storage(source) == target and str(target) in binds["mounted"]
    bind_unit, check_unit = f"{target.name}.mount", workspacehelper.check_unit_name(target)
    text = (binds["systemd"] / bind_unit).read_text()
    assert f"What={source}\n" in text and f"Where={target}\n" in text and "Options=bind,nosuid,nodev\n" in text
    assert "Before=docker.service" in text
    check = (binds["systemd"] / check_unit).read_text()
    assert f"Requires={bind_unit}\nAfter={bind_unit}\n" in check and "Before=docker.service" in check
    assert f"workspace-helper storage-check {source}\n" in check
    assert ["chattr", "+i", str(target)] in binds["calls"]
    assert ["systemctl", "enable", bind_unit, check_unit] in binds["calls"]
    assert ["systemctl", "start", check_unit] in binds["calls"]
    assert (binds["ids"] / f"{target.name}.id").read_text().strip() == workspacehelper._identity(target)
    # bound already: taken as it is
    binds["calls"].clear()
    assert workspacehelper.bind_storage(source) == target and not any(c[0] == "systemctl" for c in binds["calls"])
    assert workspacehelper.storage_check(source)["bound"] == str(target)
    # a workspace's disk waits for its storage's check
    image, mount = workspacehelper.workspace("acme", source)
    assert image == target / ".disks" / "acme.img" and mount == target / "acme"
    assert f"Requires={check_unit}\nAfter={check_unit}\n" in workspacehelper.unit_text(image, mount, "acme")


def test_another_folder_bound_is_unbound(binds):
    """At a boot the path counts again: another folder there is unbound by
    the check, and no action takes it."""
    source, target = str(binds["source"]), binds["target"]
    workspacehelper.bind_storage(source)
    (binds["ids"] / f"{target.name}.id").write_text("1 2\n")  # the folder set up was another
    with pytest.raises(HelperError, match="not the folder set up"):
        workspacehelper.bind_storage(source)
    with pytest.raises(HelperError, match="storage-reset"):
        workspacehelper.storage_check(source)
    assert ["umount", str(target)] in binds["calls"] and str(target) not in binds["mounted"]
    # an action stops the bind's unit before binding again (systemd may still take it for active)
    binds["calls"].clear()
    with pytest.raises(HelperError):
        workspacehelper.bind_storage(source)
    starts = [c for c in binds["calls"] if c[:2] in (["systemctl", "stop"], ["systemctl", "start"])]
    assert starts[:2] == [["systemctl", "stop", f"{target.name}.mount"], ["systemctl", "start", f"{target.name}.mount"]]
    # bound again by an action: refused, unbound
    with pytest.raises(HelperError, match="not the folder set up"):
        workspacehelper.workspace("acme", source)
    assert str(target) not in binds["mounted"]


def test_a_storage_others_may_write_is_refused(binds):
    binds["owner"]["problem"] = "x is not root's alone (owner uid 1000, mode 755)"
    with pytest.raises(HelperError, match="root's alone.*chown root"):
        workspacehelper.bind_storage(str(binds["source"]))
    assert str(binds["target"]) not in binds["mounted"]
    assert not (binds["ids"] / f"{binds['target'].name}.id").exists()


def test_what_is_refused(binds, tmp_path, monkeypatch):
    with pytest.raises(HelperError, match="not one of workspaces.storage"):
        workspacehelper.bind_storage("/elsewhere")
    with pytest.raises(HelperError, match="not one of workspaces.storage"):
        workspacehelper.storage_check("/elsewhere")
    link = tmp_path / "link"
    link.symlink_to(binds["source"])
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config_path=None: (str(link),))
    with pytest.raises(HelperError, match="symbolic link"):
        workspacehelper.bind_storage(str(link))


def test_storage_reset(binds, monkeypatch):
    source, target = str(binds["source"]), binds["target"]
    workspacehelper.bind_storage(source)
    id_file = binds["ids"] / f"{target.name}.id"
    id_file.write_text("1 2\n")
    # through sudo: never
    with pytest.raises(HelperError, match="by root by hand"):
        workspacehelper.storage_reset(source)
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("root", 0, [0]))
    binds["mounted"].add(f"{target}/acme")
    with pytest.raises(HelperError, match="workspace disks are mounted"):
        workspacehelper.storage_reset(source)
    binds["mounted"].discard(f"{target}/acme")
    assert workspacehelper.storage_reset(source)["bound"] == str(target)
    assert id_file.read_text().strip() == workspacehelper._identity(target)


def test_storage_setup(binds, tmp_path, monkeypatch):
    missing = str(tmp_path / "later")
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config_path=None: (str(binds["source"]), missing))
    found = workspacehelper.storage_setup()["storages"]
    assert found == {str(binds["source"]): str(binds["target"]), missing: "not a folder yet"}
    assert {"storage-setup", "storage-check", "storage-reset"} <= set(workspacehelper.ACTIONS)


def test_a_bind_of_the_same_file_system_is_a_mount(monkeypatch, tmp_path):
    """os.path.ismount misses it (same device): the kernel's list sees it."""
    info = tmp_path / "mountinfo"
    info.write_text("22 1 259:1 / / rw - ext4 /dev/x rw\n"
                    "90 22 259:1 /srv/a /var/lib/omniacli-storage/srv-a-1 rw - ext4 /dev/x rw\n"
                    "91 22 259:1 /srv/b /var/lib/omniacli-storage/with\\040space rw - ext4 /dev/x rw\n")
    monkeypatch.setattr(workspacehelper, "MOUNTINFO", info)
    assert workspacehelper._is_mount(Path("/var/lib/omniacli-storage/srv-a-1"))
    assert workspacehelper._is_mount(Path("/var/lib/omniacli-storage/with space"))
    assert not workspacehelper._is_mount(Path("/var/lib/omniacli-storage/srv-b-2"))
