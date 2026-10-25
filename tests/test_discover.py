# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import os
import stat

import pytest

from omniacli.odoo import discover
from omniacli.plugin_api import Discovered, system_user

from .conftest import PASSWORD, commit, sh
from .test_app import AUTH, HOST, client  # noqa: F401 - the fixture

ORIGIN = {"Authorization": AUTH, "Origin": f"http://{HOST}"}


def test_script_index():
    assert discover.script_index(["/v/bin/python", "odoo-bin", "-c", "x"]) == 1
    assert discover.script_index(["python3", "-u", "-X", "dev", "/opt/odoo/odoo-bin"]) == 4
    assert discover.script_index(["/usr/bin/python3", "/usr/bin/odoo", "--db_host", "db"]) == 1
    assert discover.script_index(["/opt/odoo/odoo-bin", "-c", "x"]) == 0
    assert discover.script_index(["/usr/bin/python3", "/opt/app/main.py"]) is None
    assert discover.script_index(["/usr/sbin/cron", "-f"]) is None
    assert discover.script_index([]) is None


def test_parse_args():
    args = discover.parse_args(["-c", "/etc/odoo.conf", "--http-port=8079", "-d", "db1",
                                "--gevent-port", "8078", "--logfile=/var/log/o.log", "--workers=2"])
    assert args == {"command": "server", "config": "/etc/odoo.conf", "http_port": "8079", "database": "db1",
                    "gevent_port": "8078", "logfile": "/var/log/o.log", "workers": "2"}
    assert discover.parse_args(["-c/etc/a.conf"])["config"] == "/etc/a.conf"
    assert discover.parse_args(["--config=/etc/b.conf", "--xmlrpc-port", "8069"]) == {
        "command": "server", "config": "/etc/b.conf", "http_port": "8069"}
    # Odoo's gevent server, a child of the main process
    assert discover.parse_args(["gevent", "-c", "/etc/a.conf"])["command"] == "gevent"


def test_unit_of():
    assert discover.unit_of("0::/system.slice/odoo-prod.service\n") == ("odoo-prod.service", None)
    assert discover.unit_of("12:pids:/x\n1:name=systemd:/system.slice/odoo.service\n") == ("odoo.service", None)
    assert discover.unit_of("0::/user.slice/user-1000.slice/session-2.scope\n") == (None, None)
    unit, why = discover.unit_of("0::/system.slice/docker-db5daa91.scope\n")
    assert unit is None and "container" in why
    unit, why = discover.unit_of("0::/user.slice/user-1000.slice/user@1000.service/app.slice/odoo.service\n")
    assert unit is None and "user unit" in why


def fake_proc(base, processes):
    """processes: {pid: (ppid, argv, cgroup)}; the uid is this user's."""
    proc = base / "proc"
    for pid, (ppid, argv, cgroup) in processes.items():
        folder = proc / str(pid)
        folder.mkdir(parents=True)
        (folder / "cmdline").write_text("\0".join(argv) + "\0")
        (folder / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\nUid:\t{os.getuid()}\t{os.getuid()}\t0\t0\n")
        (folder / "cgroup").write_text(cgroup)
    (proc / "self").mkdir()
    return proc


def test_processes_top_only(tmp_path):
    odoo = ["/v/bin/python", "odoo-bin", "-c", "/etc/a.conf"]
    proc = fake_proc(tmp_path, {
        100: (1, odoo, "0::/system.slice/odoo-prod.service\n"),
        101: (100, odoo, "0::/system.slice/odoo-prod.service\n"),            # a worker
        102: (100, ["/v/bin/python", "odoo-bin", "gevent", "-c", "/etc/a.conf"],
              "0::/system.slice/odoo-prod.service\n"),                       # its gevent server
        200: (1, ["/usr/bin/python3", "/usr/bin/odoo", "--db_host", "db"], "0::/system.slice/docker-1.scope\n"),
        300: (1, ["/usr/sbin/cron", "-f"], "0::/system.slice/cron.service\n"),
    })
    found = {p.pid: p for p in discover.processes(proc)}
    assert sorted(found) == [100, 200]
    assert found[100].unit == "odoo-prod.service" and found[100].skip is None
    assert found[100].user == system_user()
    assert found[200].skip and "container" in found[200].skip


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """An Odoo checkout (git, odoo-bin, release.py of Odoo 18), an addons
    repository, a venv, a config, and a systemctl that knows odoo-prod.service
    (main pid 100) and odoo-old.service (stopped)."""
    tmp_path = tmp_path / "machine"
    odoo = tmp_path / "odoo"
    (odoo / "odoo").mkdir(parents=True)
    sh("git", "init", "-q", "-b", "18.0", str(odoo))
    (odoo / "odoo" / "release.py").write_text("version_info = (18, 0, 0, FINAL, 0, '')\n")
    commit(odoo, "odoo-bin", "#!/usr/bin/env python3\n", "odoo")
    addons = tmp_path / "addons"
    addons.mkdir()
    sh("git", "init", "-q", "-b", "18.0", str(addons))
    commit(addons, "README", "addons\n", "addons")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python").write_text("")
    conf = tmp_path / "odoo.conf"
    conf.write_text(f"[options]\ndb_name = PROD\nlogfile = {tmp_path}/odoo.log\n"
                    f"addons_path = {addons}\nhttp_port = 8069\n")
    old_conf = tmp_path / "old.conf"
    old_conf.write_text(f"[options]\ndb_name = OLD\nlogfile = {tmp_path}/old.log\naddons_path = {addons}\n")

    argv = f"{venv}/bin/python {odoo}/odoo-bin -c {old_conf}"
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f"""#!/bin/sh
case "$1" in
list-unit-files) printf 'odoo-prod.service enabled enabled\\nodoo-old.service disabled enabled\\ngetty@.service enabled enabled\\ncron.service enabled enabled\\n' ;;
show) sed "s#\\$WORKDIR#$(cat {tmp_path}/workdir 2>/dev/null)#" <<'EOF'
Id=odoo-prod.service
ExecStart={{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}
User=
WorkingDirectory=$WORKDIR
MainPID=100
ActiveState=active

Id=odoo-old.service
ExecStart={{ path={venv}/bin/python ; argv[]={argv} ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}
User=
WorkingDirectory=
MainPID=0
ActiveState=inactive

Id=cron.service
ExecStart={{ path=/usr/sbin/cron ; argv[]=/usr/sbin/cron -f ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}
User=
WorkingDirectory=
MainPID=1295
ActiveState=active
EOF
;;
*) exit 1 ;;
esac
""")
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    import omniacli.ops as ops

    monkeypatch.setattr(ops, "_ENV", {**ops._ENV, "PATH": f"{bin_dir}:{ops._ENV.get('PATH', '')}"})
    proc = fake_proc(tmp_path, {
        # started from odoo's folder, odoo-bin relative
        100: (1, [f"{venv}/bin/python", "odoo-bin", "-c", str(conf), "--http-port=8079", "--logfile",
                  f"{tmp_path}/cmd.log"], "0::/system.slice/odoo-prod.service\n"),
        # by hand, from a shell of OmniaCLI's own service
        110: (1, [f"{venv}/bin/python", f"{odoo}/odoo-bin", "-c", str(tmp_path / "dev.conf"), "-d", "DEV"],
              "0::/system.slice/omniacli.service\n"),
    })
    (tmp_path / "dev.conf").write_text(f"[options]\naddons_path = {addons}\n")
    (proc / "100" / "cwd").symlink_to(odoo)
    return {"tmp": tmp_path, "proc": proc, "odoo": odoo, "addons": addons, "venv": venv, "conf": conf}


async def test_discover(machine):
    found = {d.label: d for d in await discover.discover([], machine["proc"])}
    assert sorted(found) == ["process 100, unit odoo-prod.service", "process 110", "unit odoo-old.service"]

    prod = found["process 100, unit odoo-prod.service"]
    inst = prod.instance
    assert prod.name == "PROD" and prod.skip is None and prod.known is None
    assert inst["database"] == "PROD" and inst["odoo_config"] == str(machine["conf"])
    assert inst["venv_bin"] == str(machine["venv"] / "bin")
    assert inst["restart"] == {"type": "systemd", "service": "odoo-prod.service", "sudo": True}
    # the command line's logfile wins over the config's
    assert inst["log_file"] == str(machine["tmp"] / "cmd.log")
    # odoo-bin, relative, found in the Odoo repository, which the addons_path did not name
    assert [r["path"] for r in inst["repositories"]] == [str(machine["odoo"]), str(machine["addons"])]
    assert all(r["branch"] == "18.0" for r in inst["repositories"])
    assert "odoo_bin" not in inst and "run_as" not in inst
    facts = dict(prod.facts)
    assert facts["odoo-bin"] == f"{machine['odoo']}/odoo-bin (Odoo 18)"
    assert facts["HTTP port"] == "8079 (command line)"

    dev = found["process 110"]
    assert dev.instance["restart"] == {"type": "process"} and dev.instance["database"] == "DEV"
    assert any("not its own" in n for n in dev.notes)

    old = found["unit odoo-old.service"]
    assert ("State", "inactive") in old.facts
    assert old.instance["database"] == "OLD" and old.instance["restart"]["service"] == "odoo-old.service"
    # the unit has no User=: root
    assert old.instance["run_as"] == "root" and any("as root" in n for n in old.notes)


async def test_discover_relative_odoo_bin_not_found(machine):
    """odoo-bin relative, its working directory not readable, and no
    repository of the addons_path holds it: a note says so."""
    (machine["proc"] / "100" / "cwd").unlink()
    prod = next(d for d in await discover.discover([], machine["proc"]) if d.label.startswith("process 100"))
    assert any("relative and in no repository" in n for n in prod.notes)
    assert [r["path"] for r in prod.instance["repositories"]] == [str(machine["addons"])]


async def test_discover_odoo_bin_from_unit_workdir(machine):
    """/proc does not give the working directory: the unit's WorkingDirectory does."""
    (machine["proc"] / "100" / "cwd").unlink()
    (machine["tmp"] / "workdir").write_text(str(machine["odoo"]))
    prod = next(d for d in await discover.discover([], machine["proc"]) if d.label.startswith("process 100"))
    assert dict(prod.facts)["odoo-bin"] == f"{machine['odoo']}/odoo-bin (Odoo 18)"
    assert [r["path"] for r in prod.instance["repositories"]] == [str(machine["odoo"]), str(machine["addons"])]


async def test_discover_marks_known(machine, env):
    srv = env["settings"].servers["srv"]
    known = list(srv.instances.values())
    for inst in known:
        inst.options.odoo_config = machine["conf"]
    found = {d.label: d for d in await discover.discover(known, machine["proc"])}
    assert found["process 100, unit odoo-prod.service"].known in ("srv/production", "srv/test")
    assert found["process 110"].known is None


async def test_discover_unreadable_config(machine):
    machine["conf"].chmod(0)
    try:
        if os.access(machine["conf"], os.R_OK):
            pytest.skip("running as root: every file is readable")
        prod = next(d for d in await discover.discover([], machine["proc"]) if d.label.startswith("process 100"))
        assert any("Permission denied" in n for n in prod.notes)
    finally:
        machine["conf"].chmod(0o644)


def item(name, role, **extra):
    instance = {"database": "D", "odoo_config": "/etc/x.conf", "venv_bin": "/v/bin", "log_file": "/var/log/x.log",
                "restart": {"type": "systemd", "service": "odoo-x", "sudo": True}, "repositories": []}
    return {"name": name, "role": role, "type": "odoo", "instance": {**instance, **extra}}


async def test_discover_api(client, env, monkeypatch):  # noqa: F811
    async def fake(known):
        assert {i.key for i in known} == {"srv/production", "srv/test"}
        return [Discovered(key="/etc/x.conf", label="process 1", name="X", instance=item("X", "test")["instance"],
                           facts=[("State", "running")], notes=["a note"])]
    monkeypatch.setattr(discover, "discover", fake)
    resp = await client.get("/api/servers/discover", headers={"Authorization": AUTH})
    data = resp.json()
    assert resp.status_code == 200 and data["names"] == ["srv"]
    assert data["found"][0]["type"] == "odoo" and data["found"][0]["type_label"] == "Odoo"
    assert data["found"][0]["notes"] == ["a note"]
    page = await client.get("/config/discover", headers={"Authorization": AUTH})
    assert page.status_code == 200 and "Create drafts" in page.text
    assert 'href="/config/discover"' in (await client.get("/config", headers={"Authorization": AUTH})).text

    url = "/api/servers/discover"
    for body, error in (
        ({"items": [item("srv", "production")]}, "already exists"),
        ({"items": [item("a b", "production")]}, "server name"),
        ({"items": [item("n", "staging")]}, "role"),
        ({"items": [item("n", "test"), item("n", "test")]}, "two instances"),
        ({"items": []}, "at least one"),
    ):
        resp = await client.post(url, json=body, headers=ORIGIN)
        assert resp.status_code == 400 and error in resp.json()["error"], (body, resp.json())
    resp = await client.post(url, json={"items": [item("new", "production"), item("new", "test", database="T"),
                                                  item("other", "production", venv_bin="")]}, headers=ORIGIN)
    assert resp.status_code == 200, resp.json()
    created = {c["name"]: c["error"] for c in resp.json()["created"]}
    assert created["new"] is None and "venv_bin" in created["other"]
    servers = env["settings"].servers
    assert servers["new"].status == "draft" and set(servers["new"].instances) == {"production", "test"}
    assert servers["new"].instances["test"].options.database == "T"
    assert servers["other"].status == "draft" and servers["other"].error
    listing = (await client.get("/config", headers={"Authorization": AUTH})).text
    assert "<strong>new</strong>" in listing and "<strong>other</strong>" in listing
    # only an admin
    developer = {"Authorization": "Basic " + base64.b64encode(f"dev:{PASSWORD}".encode()).decode()}
    assert (await client.get(url, headers=developer)).status_code == 403
