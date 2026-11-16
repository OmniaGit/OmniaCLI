# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The storages' binds: each storage of workspaces.storage (name: folder) is
bound on the root path of its name by its unit, at every boot from the
folder named then, its identity kept. The system commands are faked; a bind
is a mounted path and the folder bound there."""
from pathlib import Path

import pytest

from omniacli import config, workspacehelper
from omniacli.composehelper import HelperError

from .conftest import REAL_BIND_STORAGE, REAL_BOUND_ROOT


def test_bound_root():
    # the storage's name, not its folder: a new folder (another disk) keeps it
    assert REAL_BOUND_ROOT("disk2") == config.STORAGE_BASE + "/disk2"


@pytest.fixture
def binds(monkeypatch, tmp_path):
    """The real bind_storage with the system's commands faked: the start of a
    storage's unit runs its storage-check, mount --bind mounts, umount
    unmounts; a folder bound has the identity of the folder bound there."""
    source = tmp_path / "disk" / "mine" / "workspaces"
    source.mkdir(parents=True)
    base = tmp_path / "base"
    base.mkdir()
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    roots = {"mine": str(source)}
    monkeypatch.setattr(config, "bound_root", lambda storage: str(base / storage))
    monkeypatch.setattr(workspacehelper, "bind_storage", REAL_BIND_STORAGE)
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config_path=None: dict(roots))
    monkeypatch.setattr(workspacehelper, "SYSTEMD", systemd)
    monkeypatch.setattr(workspacehelper, "STORAGE_IDS", tmp_path / "ids")
    monkeypatch.setattr(workspacehelper, "_safe_folder", lambda folder, what: None)
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("omniacli", 1000, [1000]))
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *args: None)
    # the test's folders are the test user's: taken as root's alone unless a test says otherwise
    owner = {"problem": None}
    monkeypatch.setattr(workspacehelper, "_root_only", lambda path: owner["problem"])
    mounted, calls = {}, []
    real_identity = workspacehelper._identity
    monkeypatch.setattr(workspacehelper, "_identity", lambda path: real_identity(mounted.get(str(path), path)))

    def run(*cmd, check=True):
        calls.append(list(cmd))
        if cmd[0] == "systemd-escape":
            return Path(cmd[-1]).name + ".mount\n"
        if cmd[:2] == ("systemctl", "start") and cmd[2].startswith("omniacli-storage-"):
            try:
                workspacehelper.storage_check(cmd[2][len("omniacli-storage-"):-len(".service")])
            except HelperError as exc:
                return f"failed: {exc}"
        if cmd[:2] == ("systemctl", "stop") and cmd[2].startswith("omniacli-storage-"):
            mounted.pop(str(base / cmd[2][len("omniacli-storage-"):-len(".service")]), None)
        if cmd[:2] == ("mount", "--bind"):
            mounted[cmd[3]] = cmd[2]
        if cmd[0] == "umount":
            mounted.pop(cmd[1], None)
        if cmd[0] == "findmnt":
            return "\n".join(sorted(mounted))
        return ""
    monkeypatch.setattr(workspacehelper, "_run", run)
    monkeypatch.setattr(workspacehelper, "_is_mount", lambda path: str(path) in mounted)
    return {"source": source, "target": base / "mine", "systemd": systemd, "calls": calls, "roots": roots,
            "mounted": mounted, "owner": owner, "ids": tmp_path / "ids", "tmp": tmp_path}


def test_a_storage_is_bound_and_checked(binds):
    source, target = binds["source"], binds["target"]
    assert workspacehelper.bind_storage("mine") == target and binds["mounted"][str(target)] == str(source)
    unit = workspacehelper.check_unit_name(target)
    assert unit == "omniacli-storage-mine.service"
    # its unit binds it at every boot, from the folder named then, before docker
    text = (binds["systemd"] / unit).read_text()
    assert "workspace-helper storage-check mine\n" in text and "Before=docker.service" in text
    assert f"ExecStop=-/bin/umount {target}\n" in text and str(source) not in text
    assert ["mount", "-o", "remount,bind,nosuid,nodev", str(target)] in binds["calls"]
    assert ["chattr", "+i", str(target)] in binds["calls"]
    assert ["systemctl", "enable", unit] in binds["calls"]
    # its identity and its folder kept
    kept = (binds["ids"] / "mine.id").read_text().split("\n")
    assert kept[0] == workspacehelper._identity(source) and kept[1] == str(source)
    # bound already: taken as it is
    binds["calls"].clear()
    assert workspacehelper.bind_storage("mine") == target and not any(c[0] == "systemctl" for c in binds["calls"])
    assert workspacehelper.storage_check("mine")["folder"] == str(source)
    # a workspace's disk waits for its storage's unit
    image, mount = workspacehelper.workspace("acme", "mine")
    assert image == target / ".disks" / "acme.img" and mount == target / "acme"
    assert f"Requires={unit}\nAfter={unit}\n" in workspacehelper.unit_text(image, mount, "acme")


def test_a_new_folder_of_a_storage(binds):
    """A new disk: root names another folder for the storage; the name stays,
    so do its root path and all that uses it."""
    target = binds["target"]
    workspacehelper.bind_storage("mine")
    new = binds["tmp"] / "newdisk" / "omniacli"
    new.mkdir(parents=True)
    binds["roots"]["mine"] = str(new)
    # a workspace's disk mounted on it: not while it runs
    binds["mounted"][f"{target}/acme"] = "image"
    with pytest.raises(HelperError, match="main configuration changed.*stop them, or restart"):
        workspacehelper.bind_storage("mine")
    del binds["mounted"][f"{target}/acme"]
    # then (or at the next boot) bound from the new folder, and that one kept
    assert workspacehelper.bind_storage("mine") == target and binds["mounted"][str(target)] == str(new)
    assert (binds["ids"] / "mine.id").read_text().split("\n")[1] == str(new)
    # at a boot: the unit binds the folder named then
    binds["mounted"].clear()
    assert workspacehelper.storage_check("mine")["folder"] == str(new)


def test_another_folder_at_the_same_path_is_refused(binds):
    """At a boot the path counts again: another folder there (moved or
    replaced under it) is not bound, and no action takes it."""
    source, target = binds["source"], binds["target"]
    workspacehelper.bind_storage("mine")
    (binds["ids"] / "mine.id").write_text(f"1 2\n{source}\n")  # the folder set up was another
    with pytest.raises(HelperError, match="not the folder set up"):
        workspacehelper.bind_storage("mine")
    with pytest.raises(HelperError, match="storage-reset"):
        workspacehelper.storage_check("mine")
    assert ["umount", str(target)] in binds["calls"] and str(target) not in binds["mounted"]
    # its unit (a boot) does not bind it
    with pytest.raises(HelperError, match="not the folder set up"):
        workspacehelper.storage_check("mine")
    assert str(target) not in binds["mounted"]
    # nor an action
    with pytest.raises(HelperError, match="not the folder set up|did not bind"):
        workspacehelper.workspace("acme", "mine")
    assert str(target) not in binds["mounted"]


def test_a_storage_others_may_write_is_refused(binds):
    binds["owner"]["problem"] = "x is not root's alone (owner uid 1000, mode 755)"
    with pytest.raises(HelperError, match="root's alone.*chown root"):
        workspacehelper.bind_storage("mine")
    assert str(binds["target"]) not in binds["mounted"]
    assert not (binds["ids"] / "mine.id").exists()


def test_what_is_refused(binds, tmp_path):
    with pytest.raises(HelperError, match="not a storage of workspaces.storage"):
        workspacehelper.bind_storage("elsewhere")
    with pytest.raises(HelperError, match="not a storage of workspaces.storage"):
        workspacehelper.storage_check("elsewhere")
    link = tmp_path / "link"
    link.symlink_to(binds["source"])
    binds["roots"]["mine"] = str(link)
    with pytest.raises(HelperError, match="symbolic link"):
        workspacehelper.bind_storage("mine")


def test_storage_reset(binds, monkeypatch):
    source, target = binds["source"], binds["target"]
    workspacehelper.bind_storage("mine")
    id_file = binds["ids"] / "mine.id"
    id_file.write_text(f"1 2\n{source}\n")
    # through sudo: never
    with pytest.raises(HelperError, match="by root by hand"):
        workspacehelper.storage_reset("mine")
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("root", 0, [0]))
    binds["mounted"][f"{target}/acme"] = "image"
    with pytest.raises(HelperError, match="workspace disks are mounted"):
        workspacehelper.storage_reset("mine")
    del binds["mounted"][f"{target}/acme"]
    assert workspacehelper.storage_reset("mine")["bound"] == str(target)
    assert id_file.read_text().split("\n")[0] == workspacehelper._identity(source)


def test_storage_setup(binds, tmp_path):
    binds["roots"]["later"] = str(tmp_path / "later")
    systemd = binds["systemd"]
    # the binds of before (a .mount of a storage's root path) and a storage no longer listed: taken away;
    # a workspace's disk, and what the helper did not write, are kept
    old = workspacehelper.config.STORAGE_BASE
    (systemd / "old-bind.mount").write_text("# written by omniacli workspace-helper: a storage of the workspaces, "
                                           f"bound on a root path\n[Mount]\nWhere={old}/srv-a-12345678\n")
    (systemd / "omniacli-storage-gone.service").write_text("# written by omniacli workspace-helper: a storage\n")
    (systemd / "disk.mount").write_text("# written by omniacli workspace-helper: the virtual disk of workspace "
                                        f"acme\n[Mount]\nWhere={binds['target']}/acme\n")
    (systemd / "omniacli-storage-other.service").write_text("# someone else's\n")
    # the disk of a workspace on a storage no longer listed: taken away, unless it is mounted
    for unit, ws in (("old-disk.mount", "w1"), ("busy-disk.mount", "w2")):
        (systemd / unit).write_text("# written by omniacli workspace-helper: the virtual disk of workspace "
                                    f"{ws}\n[Mount]\nWhere={old}/srv-b-87654321/{ws}\n")
    binds["mounted"][f"{old}/srv-b-87654321/w2"] = "image"
    found = workspacehelper.storage_setup()["storages"]
    assert found["mine"] == f"{binds['source']} bound on {binds['target']}"
    assert found["later"] == f"{tmp_path / 'later'}: not a folder yet"
    assert found["old-bind.mount"] == "removed" and found["omniacli-storage-gone.service"] == "removed"
    assert not (systemd / "old-bind.mount").exists() and not (systemd / "omniacli-storage-gone.service").exists()
    assert (systemd / "disk.mount").exists() and (systemd / "omniacli-storage-other.service").exists()
    assert found["old-disk.mount"] == "removed" and not (systemd / "old-disk.mount").exists()
    assert found["busy-disk.mount"].startswith("kept:") and (systemd / "busy-disk.mount").exists()
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
