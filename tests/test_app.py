# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import base64
import dataclasses
import datetime
import re

import httpx
import pytest

from omniacli.app import create_app
from omniacli.odoo import ops as odoo_ops
from omniacli.logtail import read_since

from .conftest import BasicAsSession, LINK_TOKEN, LINK_VIEW_TOKEN, PASSWORD, TOKEN, clone, commit, sh

AUTH = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
HOST = "omniacli.test"


@pytest.fixture
async def client(env):
    app = create_app(env["settings"])
    # the lifespan in one task, as uvicorn runs it: the MCP session manager's
    # anyio cancel scope must be left in the task that entered it
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()
    task = asyncio.create_task(lifespan())
    # a lifespan that fails on startup ends the task: raise its error, do not hang
    await asyncio.wait({task, asyncio.ensure_future(started.wait())},
                       return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    try:
        transport = httpx.ASGITransport(app=BasicAsSession(app))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as c:
            c.app = app
            yield c
    finally:
        stop.set()
        await task


async def wait_job(client, job_id):
    for _ in range(200):
        data = (await client.get(f"/api/jobs/{job_id}", headers={"Authorization": AUTH})).json()
        if data["status"] in ("success", "failed", "interrupted", "stopped"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_requires_auth(client):
    assert (await client.get("/")).status_code == 401
    bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
    assert (await client.get("/", headers={"Authorization": bad})).status_code == 401
    resp = await client.get("/", headers={"Authorization": AUTH})
    assert resp.status_code == 200 and 'href="/servers/srv"><strong>srv</strong>' in resp.text
    resp = await client.get("/servers/srv?view=both", headers={"Authorization": AUTH})
    assert resp.status_code == 200 and "DB_production" in resp.text and "DB_test" in resp.text


async def test_post_needs_same_origin(client):
    url = "/servers/srv/test/pull"
    resp = await client.post(url, headers={"Authorization": AUTH})
    assert resp.status_code == 403
    resp = await client.post(url, headers={"Authorization": AUTH, "Origin": "http://evil.example"})
    assert resp.status_code == 403
    resp = await client.post(url, headers={"Authorization": AUTH, "Origin": f"http://{HOST}"})
    assert resp.status_code == 303
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    data = await wait_job(client, job_id)
    assert data["status"] == "success" and data["source"] == "manual: admin"
    page = await client.get(f"/jobs/{job_id}", headers={"Authorization": AUTH})
    assert page.status_code == 200


async def test_failed_job_shows_error(client, env):
    path = env["settings"].instance("srv", "production").repository("odoo").path
    (path / "base.txt").write_text("dirty\n")
    resp = await client.post(
        "/servers/srv/production/pull-restart", headers={"Authorization": AUTH, "Origin": f"http://{HOST}"}
    )
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "failed" and "uncommitted" in data["error"]
    listing = await client.get("/jobs", headers={"Authorization": AUTH})
    assert "uncommitted" in listing.text


async def test_unknown_instance(client):
    resp = await client.post(
        "/servers/nope/test/pull", headers={"Authorization": AUTH, "Origin": f"http://{HOST}"}
    )
    assert resp.status_code == 404


async def test_auto_updater(client, env):
    updater = client.app.state.updater
    test = env["settings"].instance("srv", "test")
    test.auto_update.window_start = datetime.time(8, 0)
    test.auto_update.window_end = datetime.time(9, 0)
    await updater.check(test, now=datetime.time(12, 0))
    assert "outside window" in updater.state[test.key]["message"]

    await updater.check(test, now=datetime.time(8, 30))
    assert updater.state[test.key]["message"] == "up to date"

    work = clone(env["remotes"]["addons"], env["tmp"] / "dev_auto", "test")
    commit(work, "new.txt", "x\n", "new")
    sh("git", "push", "-q", "origin", "HEAD:test", cwd=work)
    await updater.check(test, now=datetime.time(8, 30))
    message = updater.state[test.key]["message"]
    assert "addons +1" in message
    data = await wait_job(client, message.rsplit(" ", 1)[1])
    assert data["status"] == "success" and data["source"] == "auto"
    assert "restart odoo-test" in env["calls"].read_text()


def test_read_since_handles_partial_lines_and_rotation(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("a\nb\nc\npart")
    first = read_since(log, initial=2)
    assert first["lines"] == ["b", "c"]  # the line being written waits
    with open(log, "a") as fh:
        fh.write("ial\nd\n")
    nxt = read_since(log, first["pos"], first["inode"])
    assert nxt["lines"] == ["partial", "d"] and not nxt["rotated"]
    assert read_since(log, nxt["pos"], nxt["inode"])["lines"] == []
    log.rename(tmp_path / "x.log.1")
    log.write_text("new\n")
    rotated = read_since(log, nxt["pos"], nxt["inode"])
    assert rotated["rotated"] and rotated["lines"] == ["new"]


def test_read_since_in_pieces(tmp_path, monkeypatch):
    import omniacli.logtail as logtail

    monkeypatch.setattr(logtail, "MAX_READ", 10)
    log = tmp_path / "x.log"
    log.write_text("")
    start = read_since(log)
    log.write_text("1234\n5678\nabcd\n")
    got, pos, inode = [], start["pos"], start["inode"]
    while True:
        data = read_since(log, pos, inode)
        got += data["lines"]; pos = data["pos"]
        if not data["more"]:
            break
    assert got == ["1234", "5678", "abcd"]


async def test_log_api(client, env):
    url = "/api/servers/srv/test/log"
    first = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert first["lines"][-1] == "line 9"
    log = env["settings"].instance("srv", "test").log_file
    with open(log, "a") as fh:
        fh.write("line 10\n")
    nxt = await client.get(f"{url}?pos={first['pos']}&inode={first['inode']}", headers={"Authorization": AUTH})
    assert nxt.json()["lines"] == ["line 10"]
    log.unlink()
    assert (await client.get(url, headers={"Authorization": AUTH})).status_code == 404


POST = {"Authorization": AUTH, "Origin": f"http://{HOST}"}


async def test_config_pages(client):
    for url in ("/config", "/config/new", "/config/srv/edit", "/config/srv/clone"):
        resp = await client.get(url, headers={"Authorization": AUTH})
        assert resp.status_code == 200, url
    clone = await client.get("/config/srv/clone", headers={"Authorization": AUTH})
    assert '"srv_copy"' in clone.text and '"original": null' in clone.text
    assert (await client.get("/config/nope/edit", headers={"Authorization": AUTH})).status_code == 404


async def test_config_clone_rename_delete(client, env):
    settings = env["settings"]
    from omniacli.config import load_servers, server_to_dict

    server = server_to_dict(settings.servers["srv"])
    server["test"]["auto_update"] = {"enabled": True, "interval_minutes": 10, "window": "00:00-00:01"}

    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": None, "name": "srv2", "server": server})
    assert resp.status_code == 200 and resp.json()["status"] == "draft", resp.text
    assert list(load_servers(settings.servers_file)) == ["srv", "srv2"]
    # a draft is not live: not on the dashboard, no actions, no auto update
    index = (await client.get("/", headers={"Authorization": AUTH})).text
    assert "/servers/srv2" not in index
    assert (await client.post("/servers/srv2/test/pull", headers=POST)).status_code == 404
    assert (await client.get("/servers/srv2", headers={"Authorization": AUTH})).status_code == 404
    assert client.app.state.updater._tasks == []
    # active through the checks (here made to pass)
    import omniacli.checks as checks

    async def all_ok(server, backup_dir):
        return [checks.Check("server", "fake", True, "ok")]
    original_check, checks.check_server = checks.check_server, all_ok
    try:
        resp = await client.post("/api/servers/srv2/test", headers=POST)
    finally:
        checks.check_server = original_check
    assert resp.json()["ok"] and resp.json()["status"] == "active"
    assert load_servers(settings.servers_file)["srv2"].active
    assert len(client.app.state.updater._tasks) == 1  # its auto update runs now
    assert "/servers/srv2" in (await client.get("/", headers={"Authorization": AUTH})).text

    # a name already taken is refused
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": None, "name": "srv", "server": server})
    assert resp.status_code == 400 and "already exists" in resp.json()["error"]

    # rename keeps the position in the list
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "srv", "name": "first", "server": server})
    assert resp.status_code == 200
    assert list(settings.servers) == ["first", "srv2"]
    assert settings.servers_file.with_name("servers.yaml.bak").exists()

    resp = await client.post("/api/servers/delete", headers=POST, json={"name": "srv2"})
    assert resp.status_code == 200 and list(load_servers(settings.servers_file)) == ["first"]
    # "first" got the same configuration but was saved as a draft
    assert len(client.app.state.updater._tasks) == 0


async def test_config_invalid_and_busy(client, env):
    settings = env["settings"]
    before = settings.servers_file.read_text()
    bad = {"test": {"database": "x"}}
    # a live server is not replaced by settings that cannot pass the checks
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "srv", "name": "srv", "server": bad, "activate": True})
    assert resp.status_code == 400 and "incomplete" in resp.json()["checks"][0]["detail"]
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": None, "name": "bad/name", "server": bad})
    assert resp.status_code == 400
    assert settings.servers_file.read_text() == before

    lock = client.app.state.jobs.lock("srv/test")
    await lock.acquire()
    try:
        resp = await client.post("/api/servers/delete", headers=POST, json={"name": "srv"})
        assert resp.status_code == 400 and "job is running" in resp.json()["error"]
    finally:
        lock.release()
    resp = await client.post("/api/servers/delete", headers={"Authorization": AUTH},
                             json={"name": "srv"})
    assert resp.status_code == 403  # no Origin: cross-site protection
    assert "srv" in settings.servers


def make_addons(env):
    """A fake odoo-bin in the test odoo repository and an addons_path with a
    module inside the repositories, one outside, and a duplicate name."""
    import sys

    test = env["settings"].instance("srv", "test")
    tmp = env["tmp"]
    odoo_repo = test.repository("odoo").path
    (odoo_repo / "odoo-bin").write_text(
        "import sys\n"
        f"open({str(env['calls'])!r}, 'a').write('odoo-bin ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('odoo log line')\n"
    )
    (odoo_repo / "odoo").mkdir()
    (odoo_repo / "odoo" / "release.py").write_text("version_info = (18, 0, 0, FINAL, 0, '')\n")
    for base, name in ((odoo_repo / "addons", "in_repo"), (tmp / "extra", "outside"),
                       (tmp / "extra", "in_repo"), (tmp / "extra", "not_a_module")):
        (base / name).mkdir(parents=True, exist_ok=True)
        if name != "not_a_module":
            (base / name / "__manifest__.py").write_text("{}")
    test.options.odoo_config.write_text(
        f"[options]\naddons_path = {odoo_repo / 'addons'},\n    {tmp / 'extra'}\n"
    )
    (test.options.venv_bin / "python").symlink_to(sys.executable)
    return test


async def test_list_addons(env):
    test = make_addons(env)
    addons = odoo_ops.list_addons(test)
    assert [(a["name"], a["in_repository"]) for a in addons] == [("in_repo", True), ("outside", False)]
    assert odoo_ops.odoo_major(odoo_ops.find_odoo_bin(test)) == 18


async def test_update_popup_actions(client, env):
    make_addons(env)
    test = env["settings"].instance("srv", "test")
    test.options.update_modules = False  # the explicit update runs anyway
    data = (await client.get("/x/odoo/servers/srv/test/addons", headers={"Authorization": AUTH})).json()
    assert [a["name"] for a in data["addons"]] == ["in_repo", "outside"]
    assert data["odoo_bin"].endswith("odoo-bin")

    url = "/servers/srv/test/x/odoo/update"
    for body in ({"mode": "chosen", "modules": []}, {"mode": "chosen", "modules": ["bad;rm"]},
                 {"mode": "other"}):
        assert (await client.post(url, headers=POST, json=body)).status_code == 400

    resp = await client.post(url, headers=POST,
                             json={"mode": "chosen", "modules": ["in_repo", "base"], "restart": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job
    assert any("odoo log line" in line for line in job["lines"])
    calls = env["calls"].read_text().splitlines()
    assert calls[0].startswith("odoo-bin -c ") and "-d DB_test -u in_repo,base --stop-after-init --workers=0 --logfile=" in calls[0]
    assert calls[1] == "restart odoo-test"

    # a module Odoo does not know: it only warns, the job must fail
    odoo_bin = env["settings"].instance("srv", "test").repository("odoo").path / "odoo-bin"
    odoo_bin.write_text(odoo_bin.read_text() + "print('WARNING db odoo.modules.loading: invalid module names, ignored: nope')\n")
    resp = await client.post(url, headers=POST, json={"mode": "chosen", "modules": ["nope"], "restart": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and "nope" in job["error"], job
    assert env["calls"].read_text().count("restart") == 1  # no restart after the failure

    resp = await client.post(url, headers=POST, json={"mode": "changed"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and job["title"] == "Update changed modules on srv/test"
    assert env["calls"].read_text().splitlines()[-1].startswith("update -c")


async def test_restart_action(client, env):
    resp = await client.post("/servers/srv/test/restart", headers=POST)
    assert resp.status_code == 303
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "success"
    assert env["calls"].read_text() == "restart odoo-test\n"
    page = await client.get("/servers/srv?view=test", headers={"Authorization": AUTH})
    assert 'data-action="odoo.update"' in page.text and 'data-url="/servers/srv/test/x/odoo/update"' in page.text
    assert "/servers/srv/test/restart" in page.text
    assert "/servers/srv/production/restart" not in page.text


async def test_failed_checks_keep_live_server_unchanged(client, env):
    """Editing a live server with activate=True and failing checks saves nothing."""
    from omniacli.config import server_to_dict

    settings = env["settings"]
    before = settings.servers_file.read_text()
    server = server_to_dict(settings.servers["srv"])
    server["test"]["database"] = "OTHER"
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "srv", "name": "srv", "server": server, "activate": True})
    assert resp.status_code == 400 and "still live" in resp.json()["error"]
    assert any(not c["ok"] for c in resp.json()["checks"])
    assert settings.servers_file.read_text() == before and settings.servers["srv"].active
    # saved as draft on purpose: it goes offline
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "srv", "name": "srv", "server": server})
    assert resp.json()["status"] == "draft" and not settings.servers["srv"].active


async def test_metrics_api(client, env):
    sampler = client.app.state.sampler
    await sampler.sample()
    await asyncio.sleep(0.2)
    await sampler.sample()
    data = (await client.get("/api/metrics?server=srv", headers={"Authorization": AUTH})).json()
    assert set(data["instances"]) == {"srv/production", "srv/test"} and data["cores"] >= 1
    point = data["instances"]["srv/test"]["points"][-1]
    assert set(point) == {"t", "cpu", "mem", "procs"}
    machine = data["machine"][-1]
    assert set(machine) == {"t", "cpu", "rx", "tx"} and 0 <= machine["cpu"] <= 100
    only = (await client.get("/api/metrics?machine_only=1", headers={"Authorization": AUTH})).json()
    assert only["instances"] == {} and only["machine"]
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    assert 'class="panel machine-panel" data-link=""' in page


def test_machine_counters(tmp_path):
    from omniacli.metrics import machine_counters

    proc, net = tmp_path / "proc", tmp_path / "net"
    (proc / "net").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 5 50 800 40 3 2 0 7 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n")
    (proc / "net" / "dev").write_text(
        "Inter-|   Receive |  Transmit\n face |bytes packets|bytes packets\n"
        "    lo: 1000 1 0 0 0 0 0 0 1000 1 0 0 0 0 0 0\n"
        "  eth0: 200 2 0 0 0 0 0 0 300 3 0 0 0 0 0 0\n"
        "veth1: 50 1 0 0 0 0 0 0 60 1 0 0 0 0 0 0\n"
    )
    for name in ("lo", "eth0", "veth1"):
        (net / name).mkdir(parents=True)
    (net / "eth0" / "device").mkdir()
    # busy: all but idle and iowait (guest is already in user); only eth0 has a device
    assert machine_counters(proc, net) == (160, 1000, 200, 300)
    (net / "eth0" / "device").rmdir()  # a container: every interface but lo
    assert machine_counters(proc, net) == (160, 1000, 250, 360)


def test_disk_mounts(tmp_path):
    from omniacli.disks import _holder, mounts

    table = tmp_path / "mounts"
    table.write_text(
        "/dev/sda1 / ext4 rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "/dev/loop3 /snap/core/1 squashfs ro 0 0\n"
        "/dev/sdb1 /media/My\\040Disk ext4 rw 0 0\n"
        "/dev/sdb1 /srv/bound ext4 rw 0 0\n"
        "tmpfs /tmp tmpfs rw 0 0\n"
    )
    found = mounts(table)
    assert found == [("/dev/sda1", "/", "ext4"), ("/dev/sdb1", "/srv/bound", "ext4")]
    points = ["/", "/media/My Disk", "/srv/bound"]
    assert _holder("/media/My Disk/backups/x.zip", points) == "/media/My Disk"
    assert _holder("/media/My Diskette/x", points) == "/"
    assert _holder("/srv/bound", points) == "/srv/bound"


async def test_disks_api(client, env, monkeypatch):
    import os

    from omniacli import disks

    # the host's own mounts vary (a container's root is an overlay, left out)
    monkeypatch.setattr(disks, "mounts", lambda: [("/dev/root", "/", "ext4"),
                                                   ("/dev/jobs", str(env["settings"].jobs_dir), "ext4")])
    found = (await client.get("/api/disks", headers={"Authorization": AUTH})).json()["disks"]
    assert [d["mount"] for d in found] == ["/", str(env["settings"].jobs_dir)]
    root = found[0]
    st = os.statvfs("/")
    assert root["total"] == st.f_blocks * st.f_frsize and root["level"] in ("ok", "warn", "error")
    assert "backups" in root["holds"] and "srv/test log" in root["holds"]
    assert found[1]["holds"] == ["OmniaCLI jobs"]
    page = await client.get("/", headers={"Authorization": AUTH})
    assert 'class="disk-list"' in page.text and "/api/disks" in page.text


LOGIN_HEADERS = {"Origin": f"http://{HOST}", "Content-Type": "application/x-www-form-urlencoded"}


async def form_login(client, user, password, next_path="/"):
    from urllib.parse import urlencode

    return await client.post("/login", headers=LOGIN_HEADERS,
                             content=urlencode({"username": user, "password": password, "next": next_path}))


async def test_access_log(client, env):
    log = client.app.state.access_log
    assert log.path == env["settings"].logs_dir / "access.jsonl"
    assert (await client.get("/")).status_code == 401  # no session: not a failure
    assert log.recent() == []
    assert (await form_login(client, "admin", "wrong")).status_code == 401
    resp = await form_login(client, "admin", PASSWORD, "/jobs")
    assert resp.status_code == 303 and resp.headers["location"] == "/jobs"
    for _ in range(3):  # pages of the session: no more logins
        assert (await client.get("/jobs")).status_code == 200
    entries = log.recent()
    assert [(e["event"], e["kind"], e["name"]) for e in entries] == [
        ("login", "user", "admin"), ("failed", "user", "admin")]
    assert entries[0]["time"][:4].isdigit() and entries[1]["detail"] == "login form"
    page = await client.get("/access")
    assert page.status_code == 200 and "Failed login" in page.text
    assert "Failed login" not in (await client.get("/access?q=nothing-matches")).text


async def test_login_form_session_and_logout(client, env):
    from omniacli.sessions import COOKIE

    # a browser without a session goes to the form, and back after it
    resp = await client.get("/jobs?server=srv", headers={"Accept": "text/html"})
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/jobs%3Fserver%3Dsrv"
    page = await client.get("/login?next=/jobs", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'name="next" value="/jobs"' in page.text
    assert "/login-background.jpg" in page.text
    image = await client.get("/login-background.jpg")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    # never another site after the login, never a login from another site
    resp = await form_login(client, "admin", PASSWORD, "//evil.example/x")
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=28800" in cookie
    assert (await client.get("/jobs")).status_code == 200
    client.cookies.clear()
    from urllib.parse import urlencode
    body = urlencode({"username": "admin", "password": PASSWORD})
    resp = await client.post("/login", content=body, headers={**LOGIN_HEADERS, "Origin": "http://evil.example"})
    assert resp.status_code == 403
    # every page answers with the anti-framing headers
    assert resp.headers["x-frame-options"] == "DENY"
    assert (await client.get("/favicon.ico")).headers["content-security-policy"] == "frame-ancestors 'none'"

    # Basic is gone: credentials in the header open nothing
    import base64 as b64
    raw = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=raw, base_url=f"http://{HOST}") as plain:
        basic_admin = "Basic " + b64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        assert (await plain.get("/jobs", headers={"Authorization": basic_admin})).status_code == 401

    # logout ends the session on the server too
    await form_login(client, "admin", PASSWORD)
    token = client.cookies[COOKIE]
    resp = await client.post("/logout", headers={"Origin": f"http://{HOST}"})
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    client.cookies.set(COOKIE, token)
    assert (await client.get("/jobs")).status_code == 401
    assert [e["event"] for e in client.app.state.access_log.recent()][:2] == ["logout", "login"]


async def test_failed_logins_wait(client, monkeypatch):
    for _ in range(5):
        assert (await form_login(client, "dev", "wrong")).status_code == 401
    # the right password must wait too: no check is made meanwhile
    resp = await form_login(client, "dev", PASSWORD)
    assert resp.status_code == 429 and "Too many failed logins" in resp.text
    # another user from the same address waits as well
    assert (await form_login(client, "admin", PASSWORD)).status_code == 429
    import time
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 31)
    assert (await form_login(client, "dev", PASSWORD)).status_code == 303


async def test_session_ends(client, env, monkeypatch):
    import time

    from omniacli.sessions import COOKIE, SessionStore

    await form_login(client, "admin", PASSWORD)
    assert (await client.get("/jobs")).status_code == 200
    # kept across a restart of OmniaCLI
    again = SessionStore(client.app.state.sessions.path)
    assert again.user(client.cookies[COOKIE], env["settings"].users) == "admin"
    # 8 hours after the login
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 8 * 3600 + 1)
    assert (await client.get("/jobs")).status_code == 401
    monkeypatch.setattr(time, "time", real)
    # a new password ends the user's sessions
    assert (await client.get("/jobs")).status_code == 200
    env["settings"].users["admin"] = "pbkdf2_sha256$1$00$00"
    assert (await client.get("/jobs")).status_code == 401


def test_http_port(tmp_path):
    from omniacli.odoo.odooconf import http_port

    conf = tmp_path / "o.conf"
    conf.write_text("[options]\nhttp_port = 8123\n")
    assert http_port(conf) == 8123
    conf.write_text("[options]\nxmlrpc_port = 8070\n")  # before Odoo 11
    assert http_port(conf) == 8070
    conf.write_text("[options]\nhttp_port = False\n")
    assert http_port(conf) == 8069
    assert http_port(tmp_path / "missing.conf") is None


async def test_dashboard_state_buttons_and_odoo_link(client, env):
    test = env["settings"].instance("srv", "test")
    test.options.odoo_config.write_text("[options]\nhttp_port = 8123\n")
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    # systemd units the test machine does not have: state unknown, every button
    assert 'class="run-state unknown"' in page
    for action in ("start", "stop", "restart"):
        assert f'action="/servers/srv/test/{action}"' in page, action
    assert 'href="http://localhost:8123" target="_blank"' in page
    servers = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
    assert servers[0]["instances"]["test"]["state"] == "unknown"
    assert servers[0]["instances"]["test"]["port"] == 8123



async def test_web_link_follows_the_domain(client, env):
    test = env["settings"].instance("srv", "test")
    test.options.odoo_config.write_text("[options]\nhttp_port = 8123\n")
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'class="web-link mono" href="http://localhost:8123"' in page
    assert 'class="button view-test" href="http://localhost:8123" target="_blank"' in (
        await client.get("/", headers={"Authorization": AUTH})).text
    test.domain = "https://test.example.com"
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'class="web-link mono" href="https://test.example.com"' in page
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    # the button of the server's row opens the instance
    assert 'class="button view-test" href="https://test.example.com" target="_blank"' in page
    servers = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
    assert servers[0]["instances"]["test"]["url"] == "https://test.example.com"

async def test_help_for_developers_and_admins(client):
    for user in ("admin", "dev"):
        page = await client.get("/help", headers={"Authorization": basic(user)})
        assert page.status_code == 200 and "Create link token" in page.text, user
        assert f"http://{HOST}/mcp" in page.text and 'href="/help"' in page.text
    assert (await client.get("/help", headers={"Authorization": basic("viewer")})).status_code == 403
    assert 'href="/help"' not in (await client.get("/", headers={"Authorization": basic("viewer")})).text


async def test_dashboard_follows_jobs(client, env):
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    # the runners are there for every instance, hidden while it has no job
    assert 'data-busy="srv/test" hidden' in page and 'id="toasts"' in page
    resp = await client.post("/servers/srv/test/pull", headers=POST)
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    await wait_job(client, job_id)
    data = (await client.get("/api/jobs?limit=5", headers={"Authorization": AUTH})).json()
    job = data["jobs"][0]
    assert job["id"] == job_id and job["status"] == "success" and job["servers"] == ["srv"]
    assert set(job) == {"id", "title", "status", "instances", "servers", "source", "created", "finished", "error"}
    assert data["busy"] == []
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    assert f'<tr data-job="{job_id}">' in page


async def test_start_stop_go_back_to_the_page(client, env):
    headers = {**POST, "Content-Type": "application/x-www-form-urlencoded"}
    resp = await client.post("/servers/srv/test/stop", headers=headers, content="next=%2Fservers%2Fsrv%3Fview%3Dtest")
    assert resp.status_code == 303 and resp.headers["location"] == "/servers/srv?view=test"
    data = (await client.get("/api/jobs?limit=1", headers={"Authorization": AUTH})).json()
    assert data["jobs"][0]["title"] == "Stop srv/test"
    await wait_job(client, data["jobs"][0]["id"])
    # never another site; without next, the job's page as before
    resp = await client.post("/servers/srv/test/start", headers=headers, content="next=%2F%2Fevil.example")
    assert resp.headers["location"] == "/"
    await wait_job(client, (await client.get("/api/jobs?limit=1", headers={"Authorization": AUTH})).json()["jobs"][0]["id"])
    resp = await client.post("/servers/srv/test/restart", headers=POST)
    assert resp.headers["location"].startswith("/jobs/")
    page = (await client.get("/servers/srv?view=test", headers={"Authorization": AUTH})).text
    assert 'name="next" value="/servers/srv?view=test"' in page


async def test_favicon_without_login(client):
    for path, kind in (("/favicon.ico", "image/x-icon"), ("/icon-180.png", "image/png")):
        resp = await client.get(path)
        assert resp.status_code == 200 and resp.headers["content-type"] == kind and resp.content
    page = await client.get("/", headers={"Authorization": AUTH})
    assert '<link rel="icon" href="/favicon.ico"' in page.text
    assert (await client.get("/")).status_code == 401  # everything else still needs it


async def test_backup_and_download(client, env):
    resp = await client.post("/servers/srv/production/backup?download=1", headers=POST)
    assert resp.status_code == 303 and resp.headers["location"].endswith("?download=1")
    job_id = resp.headers["location"].split("/jobs/")[1].split("?")[0]
    data = await wait_job(client, job_id)
    assert data["status"] == "success" and data["title"].endswith("+ download")
    result = data["result"]
    assert result["download"] == f"/backups/srv/production/{result['file']}" and result["size"] > 0
    file = await client.get(result["download"], headers={"Authorization": AUTH})
    assert file.status_code == 200 and file.content == b"dump\n"
    assert f'filename="{result["file"]}"' in file.headers["content-disposition"]
    listing = (await client.get("/api/servers/srv/production/backups", headers={"Authorization": AUTH})).json()
    assert [f["file"] for f in listing] == [result["file"]]
    # login needed, and nothing outside the instance's backup folder
    assert (await client.get(result["download"])).status_code == 401
    for bad in ("/backups/srv/test/" + result["file"], "/backups/srv/production/..%2F..%2Fjobs",
                "/backups/srv/production/x.txt"):
        assert (await client.get(bad, headers={"Authorization": AUTH})).status_code == 404, bad


async def test_backup_without_filestore(client, env):
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'name="no_filestore"' in page and 'formaction="/servers/srv/production/backup?download=1"' in page
    resp = await client.post("/servers/srv/production/backup", headers=POST, data={"no_filestore": "on"})
    data = await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert data["status"] == "success" and data["title"] == "Backup srv/production (without filestore)", data
    assert data["result"]["file"].endswith("_nofilestore.zip")
    [call] = [c for c in env["calls"].read_text().splitlines() if c.startswith("backup")]
    assert " --no-filestore DB_production " in call
    # no choice posted: the filestore too, as before
    resp = await client.post("/servers/srv/production/backup", headers=POST)
    data = await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert data["title"] == "Backup srv/production" and not data["result"]["file"].endswith("_nofilestore.zip")
    assert "--no-filestore" not in env["calls"].read_text().splitlines()[-1]


async def test_breadcrumbs(client, env):
    import re

    def crumbs(html):
        nav = re.search(r'<nav class="crumbs".*?</nav>', html, re.S).group(0)
        return re.findall(r'<(?:a href="([^"]*)"|span aria-current="page")>([^<]*)<', nav)

    get = lambda url: client.get(url, headers={"Authorization": AUTH})
    assert crumbs((await get("/servers/srv?view=test")).text) == [("/", "Dashboard"), ("/servers/srv", "srv"), ("", "Test")]
    assert crumbs((await get("/servers/srv/production/log")).text)[-2:] == [
        ("/servers/srv?view=production", "Production"), ("", "Log")]
    assert crumbs((await get("/config/srv/edit")).text) == [("/config", "Servers"), ("", "Edit srv")]
    resp = await client.post("/servers/srv/test/pull", headers=POST)
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    await wait_job(client, job_id)
    assert crumbs((await get(f"/jobs/{job_id}")).text) == [
        ("/", "Dashboard"), ("/servers/srv", "srv"), ("/servers/srv?view=test", "Test"), ("", "Git pull srv/test")]
    # a server set to draft (or deleted) has no page: the crumb goes to its jobs
    env["settings"].servers["srv"].status = "draft"
    assert crumbs((await get(f"/jobs/{job_id}")).text) == [
        ("/jobs", "Jobs"), ("/jobs?server=srv", "srv"), ("", "Git pull srv/test")]
    assert 'class="crumbs"' not in (await get("/")).text  # top level pages have none


async def test_read_odoo_config(client, env):
    tmp = env["tmp"]
    odoo, addons = tmp / "production" / "odoo", tmp / "production" / "addons"
    (addons / "sub").mkdir()
    conf = tmp / "read.conf"
    conf.write_text(f"[options]\naddons_path = {addons}/sub,\n    {odoo},\n    {addons},\n"
                    f"    {tmp}/missing\ndb_name = False\ndbfilter = ^PROD$\nlogfile = /var/log/x.log\n")
    url = "/x/odoo/form/config"
    resp = await client.post(url, headers=POST, json={"values": {"odoo_config": str(conf)}})
    data = resp.json()
    assert resp.status_code == 200, data
    assert data["fill"] == {"database": "PROD", "log_file": "/var/log/x.log"}
    assert data["repositories"] == [
        {"name": "addons", "path": str(addons), "remote": "origin", "branch": "main"},
        {"name": "odoo", "path": str(odoo), "remote": "origin", "branch": "main"},
    ]
    assert any("missing: no such folder" in n for n in data["notes"])
    assert any("odoo-bin" in n for n in data["notes"])

    conf.write_text("[options]\ndb_name = A,B\n")
    data = (await client.post(url, headers=POST, json={"values": {"odoo_config": str(conf)}})).json()
    assert data["fill"] == {} and data["repositories"] == []
    assert any("several databases" in n for n in data["notes"])

    for path, error in ((tmp / "none.conf", "cannot read"), (env["tmp"] / "odoo.log", "no [options]")):
        resp = await client.post(url, headers=POST, json={"values": {"odoo_config": str(path)}})
        assert resp.status_code == 400 and error in resp.json()["error"]
    resp = await client.post(url, headers=POST, json={"values": {}})
    assert resp.status_code == 400 and "Set the Odoo config file first" in resp.json()["error"]
    dev = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    assert (await client.post(url, headers=dev, json={"values": {"odoo_config": str(conf)}})).status_code == 403
    # the routes of a plugin need a login like every page
    client.cookies.clear()
    assert (await client.get("/x/odoo/servers/srv/test/addons")).status_code == 401


async def test_incomplete_draft(client, env):
    settings = env["settings"]
    from omniacli.config import load_servers

    draft = {"production": {"database": "DB", "odoo_config": "/etc/odoo.conf",
                            "repositories": [{"name": "odoo", "remote": "origin", "branch": "20.0"}]}}
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": None, "name": "V20", "server": draft})
    assert resp.status_code == 200 and resp.json()["status"] == "draft", resp.text
    # kept as entered, also after a restart
    saved = load_servers(settings.servers_file)["V20"]
    assert saved.instances == {} and saved.data == draft and "missing 'path'" in saved.error
    page = (await client.get("/config", headers={"Authorization": AUTH})).text
    assert "Incomplete: servers.V20.production" in page
    edit = (await client.get("/config/V20/edit", headers={"Authorization": AUTH})).text
    assert '"odoo_config": "/etc/odoo.conf"' in edit
    # not live and cannot be activated until complete
    assert (await client.get("/servers/V20", headers={"Authorization": AUTH})).status_code == 404
    data = (await client.post("/api/servers/V20/test", headers=POST)).json()
    assert not data["ok"] and data["status"] == "draft" and "incomplete" in data["checks"][0]["detail"]
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "V20", "name": "V20", "server": draft, "activate": True})
    assert resp.status_code == 200 and resp.json()["status"] == "draft"
    assert not resp.json()["checks"][0]["ok"]
    # completed, it parses again
    draft["production"]["repositories"][0]["path"] = "/opt/odoo"
    draft["production"].update(log_file="/l", venv_bin="/v", restart={"type": "systemd", "service": "o"})
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "V20", "name": "V20", "server": draft})
    srv = settings.servers["V20"]
    assert resp.status_code == 200 and srv.error is None and srv.production.options.database == "DB"


async def test_stop_button(client, env):
    from .test_ops import process_instance

    get = lambda url: client.get(url, headers={"Authorization": AUTH})
    # a systemd service whose state is unknown (no such unit here): both
    page = (await get("/servers/srv?view=test")).text
    assert "/servers/srv/test/start" in page and "/servers/srv/test/stop" in page
    assert "service state unknown" in page
    resp = await client.post("/servers/srv/test/stop", headers=POST)
    assert (await wait_job(client, resp.headers["location"].rsplit("/", 1)[1]))["status"] == "success"
    assert "stop odoo-test" in env["calls"].read_text()
    srv = env["settings"].servers["srv"]
    instance = process_instance(env["tmp"])
    instance.server, instance.role = "srv", "test"
    srv.instances["test"] = instance
    page = (await get("/servers/srv?view=test")).text
    assert ">Start</button>" in page and "/stop" not in page and "stopped" in page
    from omniacli import ops
    await ops.restart_process(ops.Job(id="t", title="t", instances=[], source="t"), instance)
    try:
        page = (await get("/servers/srv?view=test")).text
        assert ">Restart</button>" in page and "/servers/srv/test/stop" in page
        resp = await client.post("/servers/srv/test/stop", headers=POST)
        data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
        assert data["status"] == "success" and ops.managed_pid(instance) is None
    finally:
        pid = ops.managed_pid(instance)
        if pid:
            await ops.stop_process(ops.Job(id="t", title="t", instances=[], source="t"), instance, pid)


async def test_jobs_server_column_and_filter(client, env):
    get = lambda url: client.get(url, headers={"Authorization": AUTH})
    ids = []
    for role in ("test", "production"):
        resp = await client.post(f"/servers/srv/{role}/pull", headers=POST)
        ids.append(resp.headers["location"].rsplit("/", 1)[1])
        await wait_job(client, ids[-1])
    jobs = client.app.state.jobs
    other = jobs.submit("Git pull other/test", ["other/test"], "manual: bob", lambda job: asyncio.sleep(0))
    await wait_job(client, other.id)

    page = (await get("/jobs")).text
    assert "<th>Server</th>" in page and 'href="/jobs?server=srv"' in page and "3 jobs" in page
    assert '<option value="other"' in page  # a server only the jobs know
    page = (await get("/jobs?server=srv")).text
    assert "2 jobs" in page and other.id not in page and all(i in page for i in ids)
    page = (await get("/jobs?q=BOB")).text
    assert "1 job." in page and other.id in page and ids[0] not in page
    page = (await get("/jobs?server=srv&q=production")).text
    assert "1 job." in page and ids[1] in page and ids[0] not in page
    assert "No jobs match." in (await get("/jobs?server=nope")).text
    assert 'href="/jobs?server=srv"' in (await get("/servers/srv")).text


async def test_servers_filter(client, env):
    get = lambda url: client.get(url, headers={"Authorization": AUTH})
    # one server: no filter to show
    assert 'name="q"' not in (await get("/")).text
    settings = client.app.state.settings
    srv = settings.servers["srv"]
    settings.servers["other"] = dataclasses.replace(srv, name="other", instances={
        role: dataclasses.replace(inst, server="other",
                                  options=dataclasses.replace(inst.options, database=f"OTHER_{role}"))
        for role, inst in srv.instances.items()
    })

    page = (await get("/")).text
    assert 'name="q"' in page and 'href="/servers/other"' in page and 'href="/servers/srv"' in page
    page = (await get("/?q=other_TEST")).text  # a database, any case
    assert "1 of 2 servers." in page and 'href="/servers/other"' in page
    assert 'href="/servers/srv"' not in page and 'href="/">Clear' in page
    page = (await get("/?q=addons+test")).text  # every word: a repository and a branch
    assert "2 of 2 servers." in page
    assert "No servers match." in (await get("/?q=srv+OTHER_")).text



def basic(user):
    return "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()


async def test_user_role_watches_and_restarts(client, env):
    get = lambda url: client.get(url, headers={"Authorization": basic("viewer")})
    post = lambda url: client.post(url, headers={"Authorization": basic("viewer"), "Origin": f"http://{HOST}"})
    page = (await get("/servers/srv?view=both")).text
    assert "/servers/srv/test/log" in page and "/pull" not in page and "Merge test to production" not in page
    # settings, paths and repositories: developers and admins only
    assert "Settings and repositories" not in page and "odoo.conf" not in page
    assert "Settings and repositories" in (await client.get("/servers/srv", headers={"Authorization": basic("dev")})).text
    assert "Backup" not in page and "/config\">Servers<" not in page and "/x/odoo/pip-cli" not in page
    assert 'data-action="odoo.update"' not in page
    for action in ("restart", "start", "stop"):
        for role in ("production", "test"):
            assert f"/servers/srv/{role}/{action}" in page, (role, action)
        resp = await post(f"/servers/srv/production/{action}")
        assert resp.status_code == 303, action
        data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
        assert data["status"] == "success" and data["source"] == "manual: viewer"
        assert f"{action} odoo-production" in env["calls"].read_text()
    assert (await get("/jobs")).status_code == 200
    assert (await get("/api/servers/srv/test/log")).status_code == 200
    assert (await get("/api/servers/srv/test/backups")).status_code == 200
    for url in ("/config", "/config/new", "/config/srv/edit", "/backups/srv/test/x.zip"):
        assert (await get(url)).status_code == 403, url
    for url in ("/servers/srv/test/pull", "/servers/srv/test/backup", "/servers/srv/test/x/odoo/pip-cli",
                "/servers/srv/test/x/odoo/update",
                "/servers/srv/merge", "/api/servers/srv/test", "/api/servers/srv/deactivate"):
        assert (await post(url)).status_code == 403, url


async def test_developer_role_acts_but_does_not_configure(client, env):
    headers = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    page = (await client.get("/servers/srv?view=both", headers=headers)).text
    assert "/servers/srv/test/pull" in page and "Merge test to production" not in page
    assert "/config\">Servers<" not in page
    resp = await client.post("/servers/srv/test/pull", headers=headers)
    assert resp.status_code == 303
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "success" and data["source"] == "manual: dev"
    # a missing backup, but past the role check
    assert (await client.get("/backups/srv/test/x.zip", headers=headers)).status_code == 404
    assert (await client.post("/servers/srv/merge", headers=headers)).status_code == 403
    assert (await client.get("/config", headers=headers)).status_code == 403
    resp = await client.post("/api/servers/save", headers=headers, json={"name": "x", "server": {}})
    assert resp.status_code == 403


async def test_admin_sees_everything(client):
    page = (await client.get("/servers/srv?view=both", headers={"Authorization": AUTH})).text
    assert "Merge test to production" in page and "/config\">Servers<" in page and "/servers/srv/test/pull" in page


async def test_link_serves_prefixed_pages_and_acts(client, env):
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/cliente",
            "X-OmniaCLI-User": "matteo"}
    page = (await client.get("/servers/srv?view=both", headers=link)).text
    assert 'action="/links/cliente/servers/srv/test/pull"' in page
    assert 'href="/links/cliente/jobs?server=srv"' in page and 'data-log="/links/cliente/api/' in page
    assert 'href="/servers/' not in page and 'action="/servers/' not in page
    # no Origin: no browser sends the link token by itself
    resp = await client.post("/servers/srv/test/pull", headers=link)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/links/cliente/jobs/")
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["source"] == "manual: link omnia (matteo)"
    # no page links outside the prefix (the icons are the same on every
    # instance, and the linked instance bar links back to the linking one)
    unprefixed = re.compile(r'(?:href|action|data-url|data-log)="/(?!links/cliente/|favicon|icon-180|")'
                            r'|fetch\(\s*["`]/(?!links/cliente/)')
    for url in ("/", "/jobs", f"/jobs/{data['id']}", "/servers/srv/test/log", "/servers/srv?view=test"):
        page = (await client.get(url, headers=link)).text
        assert not unprefixed.findall(page), (url, unprefixed.findall(page))
        assert page.count('href="/"') == 1, url  # the bar's link back only
    resp = await client.get("/api/servers/srv/test/backups", headers=link)
    assert resp.status_code == 200
    # the link's role is developer
    assert (await client.post("/servers/srv/merge", headers=link)).status_code == 403
    # a prefix or user that is not a plain path or name is ignored
    bad = {**link, "X-OmniaCLI-Prefix": "//evil.example/x\"onload=\"", "X-OmniaCLI-User": "a b<c>"}
    page = (await client.get("/servers/srv?view=both", headers=bad)).text
    assert 'action="/servers/srv/test/pull"' in page and "evil" not in page
    resp = await client.post("/servers/srv/test/pull", headers=bad)
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["source"] == "manual: link omnia"


async def test_link_with_user_role_and_bad_token(client):
    watcher = {"Authorization": f"Bearer {LINK_VIEW_TOKEN}"}
    assert (await client.get("/", headers=watcher)).status_code == 200
    assert (await client.post("/servers/srv/test/pull", headers=watcher)).status_code == 403
    assert (await client.get("/", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_mcp_token_still_only_reads(client):
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    # a token reads what its role allows: without a role, developer
    assert (await client.get("/config", headers=bearer)).status_code == 403
    assert (await client.get("/jobs", headers=bearer)).status_code == 200
    resp = await client.post("/servers/srv/test/pull", headers=bearer)
    assert resp.status_code == 403 and resp.json()["detail"] == "A token may only read"
    # a prefix is a link's only
    page = (await client.get("/", headers={**bearer, "X-OmniaCLI-Prefix": "/links/x"})).text
    assert "/links/x" not in page


async def test_info_and_server_list(client):
    for headers, role in (({"Authorization": AUTH}, "admin"),
                          ({"Authorization": f"Bearer {LINK_VIEW_TOKEN}"}, "user")):
        data = (await client.get("/api/info", headers=headers)).json()
        assert data["app"] == "OmniaCLI" and data["role"] == role and data["version"]
    servers = (await client.get("/api/servers", headers={"Authorization": f"Bearer {LINK_VIEW_TOKEN}"})).json()
    assert [s["name"] for s in servers] == ["srv"]
    test = servers[0]["instances"]["test"]
    assert test["database"] == "DB_test" and test["service"] == "odoo-test" and test["busy"] is False
    assert {"name": "odoo", "branch": "test"} in test["repositories"]


def auth_of(user, password=PASSWORD):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def access(client, path, body, user="admin", password=PASSWORD):
    return await client.post(f"/api/access/{path}", json=body,
                             headers={"Authorization": auth_of(user, password), "Origin": f"http://{HOST}"})


async def test_users_page_and_web_user(client, env):
    settings = client.app.state.settings
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert ">Users</a>" in page and "<strong>viewer</strong>" in page and "main config" in page
    resp = await access(client, "users/save", {"name": "anna", "role": "developer", "password": "anna-pass-1", "new": True})
    assert resp.status_code == 200, resp.text
    # the file holds the hash, readable by its owner only
    text = settings.users_file.read_text()
    assert "anna-pass-1" not in text and "pbkdf2_sha256$" in text
    assert settings.users_file.stat().st_mode & 0o777 == 0o600
    anna = {"Authorization": auth_of("anna", "anna-pass-1"), "Origin": f"http://{HOST}"}
    assert (await client.get("/api/info", headers=anna)).json()["role"] == "developer"
    resp = await client.post("/servers/srv/test/pull", headers=anna)
    assert resp.status_code == 303
    await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])

    # a new password works at once, the old one stops (it was verified before)
    resp = await access(client, "users/save", {"name": "anna", "role": "user", "password": "anna-pass-2"})
    assert resp.status_code == 200, resp.text
    assert (await client.get("/", headers=anna)).status_code == 401
    anna["Authorization"] = auth_of("anna", "anna-pass-2")
    assert (await client.get("/api/info", headers=anna)).json()["role"] == "user"
    assert (await client.post("/servers/srv/test/pull", headers=anna)).status_code == 403
    # role only: the password stays
    assert (await access(client, "users/save", {"name": "anna", "role": "developer"})).status_code == 200
    assert (await client.get("/", headers=anna)).status_code == 200

    assert (await access(client, "users/delete", {"name": "anna"})).status_code == 200
    assert (await client.get("/", headers=anna)).status_code == 401
    assert "anna" not in settings.users_file.read_text()


@pytest.mark.parametrize("body, error", [
    ({"name": "x y", "role": "user", "password": "12345678", "new": True}, "A name is"),
    ({"name": "bob", "role": "boss", "password": "12345678", "new": True}, "The role must be one of"),
    ({"name": "bob", "role": "user", "password": "short", "new": True}, "at least 8 characters"),
    ({"name": "viewer", "role": "user", "password": "12345678", "new": True}, "set in the main configuration"),
    ({"name": "viewer", "role": "admin"}, "set in the main configuration"),
    ({"name": "nobody", "role": "user"}, "No user nobody"),
])
async def test_user_save_refused(client, body, error):
    resp = await access(client, "users/save", body)
    assert resp.status_code in (400, 404) and error in resp.json()["error"], resp.text


async def test_access_needs_admin_and_main_config_stays(client):
    assert (await client.get("/users", headers={"Authorization": basic("dev")})).status_code == 403
    body = {"name": "bob", "role": "admin", "password": "12345678", "new": True}
    assert (await access(client, "users/save", body, user="dev")).status_code == 403
    assert "set in the main configuration" in (await access(client, "users/delete", {"name": "viewer"})).json()["error"]
    assert "set in the main configuration" in (await access(client, "links/delete", {"name": "omnia"})).json()["error"]
    assert "set in the main configuration" in (await access(client, "tokens/delete", {"name": "tester"})).json()["error"]


async def test_own_role_self_delete_and_last_admin(client):
    settings = client.app.state.settings
    body = {"name": "boss", "role": "admin", "password": "boss-pass-1", "new": True}
    assert (await access(client, "users/save", body)).status_code == 200
    me = dict(user="boss", password="boss-pass-1")
    resp = await access(client, "users/save", {"name": "boss", "role": "user"}, **me)
    assert resp.json()["error"] == "You cannot change your own role"
    assert (await access(client, "users/delete", {"name": "boss"}, **me)).json()["error"] == "You cannot delete yourself"
    # boss the only admin left: not even an admin link may demote or delete it
    settings.fixed.roles["admin"] = "user"
    settings.set_web_access(settings.web)
    data = (await access(client, "links/save", {"name": "ops", "role": "admin", "new": True}, **me)).json()
    link = {"Authorization": f"Bearer {data['token']}"}
    resp = await client.post("/api/access/users/save", json={"name": "boss", "role": "user"}, headers=link)
    assert resp.json()["error"] == "boss is the last admin"
    resp = await client.post("/api/access/users/delete", json={"name": "boss"}, headers=link)
    assert resp.json()["error"] == "boss is the last admin"


async def test_link_and_mcp_tokens_from_the_web(client):
    data = (await access(client, "links/save", {"name": "partner", "role": "user", "new": True})).json()
    link = {"Authorization": f"Bearer {data['token']}"}
    assert (await client.get("/api/info", headers=link)).json() == {
        "app": "OmniaCLI", "version": (await client.get("/api/info", headers=link)).json()["version"],
        "caller": "link partner", "role": "user"}
    assert (await access(client, "links/save", {"name": "partner", "role": "developer"})).json()["token"] is None
    assert (await client.get("/api/info", headers=link)).json()["role"] == "developer"
    assert "already exists" in (await access(client, "links/save", {"name": "partner", "role": "user", "new": True})).json()["error"]
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert data["token"] not in page and "<strong>partner</strong>" in page
    assert (await access(client, "links/delete", {"name": "partner"})).status_code == 200
    assert (await client.get("/", headers=link)).status_code == 401

    data = (await access(client, "tokens/save", {"name": "laptop", "role": "user", "new": True})).json()
    bearer = {"Authorization": f"Bearer {data['token']}"}
    assert (await client.get("/api/info", headers=bearer)).json()["caller"] == "token laptop"
    assert (await client.get("/api/info", headers=bearer)).json()["role"] == "user"
    assert (await access(client, "tokens/save", {"name": "laptop", "role": "admin"})).json()["token"] is None
    assert (await client.get("/api/info", headers=bearer)).json()["role"] == "admin"
    saved = client.app.state.settings.users_file.read_text()
    assert "laptop:" in saved and "role: admin" in saved and data["token"] not in saved
    assert (await client.post("/servers/srv/test/pull", headers=bearer)).status_code == 403
    assert (await access(client, "tokens/delete", {"name": "laptop"})).status_code == 200
    assert (await client.get("/", headers=bearer)).status_code == 401
    assert (await access(client, "nothing/delete", {"name": "x"})).status_code == 404


async def test_own_password(client):
    page = (await client.get("/account", headers={"Authorization": basic("viewer")})).text
    assert "set in the main configuration" in page and 'href="/account"' in page
    resp = await client.post("/api/account/password", json={"current": PASSWORD, "password": "12345678"},
                             headers={"Authorization": basic("viewer"), "Origin": f"http://{HOST}"})
    assert "main configuration" in resp.json()["error"]
    assert (await client.get("/account", headers={"Authorization": f"Bearer {LINK_TOKEN}"})).status_code == 404

    body = {"name": "luca", "role": "user", "password": "luca-pass-1", "new": True}
    assert (await access(client, "users/save", body)).status_code == 200
    luca = {"Authorization": auth_of("luca", "luca-pass-1"), "Origin": f"http://{HOST}"}
    assert "Change password" in (await client.get("/account", headers=luca)).text
    resp = await client.post("/api/account/password", json={"current": "wrong", "password": "luca-pass-2"}, headers=luca)
    assert resp.json()["error"] == "The current password is wrong"
    resp = await client.post("/api/account/password", json={"current": "luca-pass-1", "password": "short"}, headers=luca)
    assert "at least 8" in resp.json()["error"]
    resp = await client.post("/api/account/password", json={"current": "luca-pass-1", "password": "luca-pass-2"}, headers=luca)
    assert resp.status_code == 200, resp.text
    # the new password ended luca's sessions, and this browser got a new one
    from omniacli.sessions import COOKIE
    assert resp.headers["set-cookie"].startswith(f"{COOKIE}=")
    assert (await client.get("/")).status_code == 200
    client.cookies.clear()
    assert (await client.get("/", headers=luca)).status_code == 401
    assert (await client.get("/", headers={"Authorization": auth_of("luca", "luca-pass-2")})).status_code == 200


async def test_pip_buttons(client, env):
    settings = client.app.state.settings
    inst = settings.instance("srv", "test")
    calls = env["tmp"] / "pip.log"
    python = inst.options.venv_bin / "python"
    python.write_text(f'#!/bin/sh\necho "python $*" >> {calls}\n')
    python.chmod(0o755)
    headers = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    page = (await client.get("/servers/srv?view=test", headers=headers)).text
    assert "/servers/srv/test/x/odoo/pip-requirements" in page and "/servers/srv/test/x/odoo/pip-cli" in page
    assert "pip-requirements" not in (await client.get("/servers/srv?view=test",
                                                        headers={"Authorization": basic("viewer")})).text

    # no requirements.txt anywhere: the job says so
    resp = await client.post("/servers/srv/test/x/odoo/pip-requirements", headers=headers)
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "failed" and "no requirements.txt" in data["error"]

    odoo, addons = inst.repository("odoo").path, inst.repository("addons").path
    (odoo / "odoo-bin").write_text("")
    (odoo / "requirements.txt").write_text("lxml\n")
    (addons / "requirements.txt").write_text("requests\n")
    resp = await client.post("/servers/srv/test/x/odoo/pip-requirements", headers=headers)
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "success" and data["source"] == "manual: dev", data
    # Odoo's first, once even though odoo-bin is in a repository too
    assert calls.read_text().splitlines() == [
        f"python -m pip install --disable-pip-version-check -r {odoo}/requirements.txt -r {addons}/requirements.txt"]

    resp = await client.post("/servers/srv/test/x/odoo/pip-cli", headers=headers)
    data = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "success" and data["title"] == "pip install click-odoo-contrib srv/test"
    assert calls.read_text().splitlines()[-1] == "python -m pip install --disable-pip-version-check click-odoo-contrib"
    for url in ("/servers/srv/test/x/odoo/pip-requirements", "/servers/srv/test/x/odoo/pip-cli"):
        resp = await client.post(url, headers={"Authorization": basic("viewer"), "Origin": f"http://{HOST}"})
        assert resp.status_code == 403


async def test_pip_buttons_in_the_form(client, env, tmp_path):
    # a venv not yet in any server: the form's values, saved or not
    venv = tmp_path / "newvenv" / "bin"
    venv.mkdir(parents=True)
    calls = tmp_path / "formpip.log"
    (venv / "python").write_text(f'#!/bin/sh\necho "python $*" >> {calls}\n')
    (venv / "python").chmod(0o755)
    odoo = env["settings"].instance("srv", "test").repository("odoo").path
    (odoo / "requirements.txt").write_text("lxml\n")
    headers = {"Authorization": AUTH, "Origin": f"http://{HOST}"}
    page = (await client.get("/config/srv/edit", headers=headers)).text
    assert '"route": "form/pip-requirements"' in page and '"route": "form/pip-cli"' in page

    body = {"name": "newsrv", "role": "test", "values": {
        "venv_bin": str(venv), "odoo_bin": "",
        "repositories": [{"path": p} for p in (str(odoo), "", "relative/path", str(tmp_path / "missing"))]}}
    resp = await client.post("/x/odoo/form/pip-requirements", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = await wait_job(client, resp.json()["job"])
    assert data["status"] == "success" and data["instances"] == ["newsrv/test"], data
    assert calls.read_text().splitlines() == [
        f"python -m pip install --disable-pip-version-check -r {odoo}/requirements.txt"]
    resp = await client.post("/x/odoo/form/pip-cli", json={**body, "name": ""}, headers=headers)
    data = await wait_job(client, resp.json()["job"])
    assert data["status"] == "success" and data["title"] == "pip install click-odoo-contrib form/test"
    assert calls.read_text().splitlines()[-1].endswith("install --disable-pip-version-check click-odoo-contrib")

    bad = {**body, "values": {**body["values"], "venv_bin": str(tmp_path)}}
    resp = await client.post("/x/odoo/form/pip-cli", json=bad, headers=headers)
    assert resp.status_code == 400 and "No python in" in resp.json()["error"]
    bad = {**body, "values": {**body["values"], "run_as": "no such user!"}}
    resp = await client.post("/x/odoo/form/pip-cli", json=bad, headers=headers)
    assert resp.status_code == 400 and "is not a user name" in resp.json()["error"]
    resp = await client.post("/x/odoo/form/pip-requirements", json=body,
                             headers={"Authorization": basic("dev"), "Origin": f"http://{HOST}"})
    assert resp.status_code == 403


async def test_servers_page_groups(client, env):
    settings = client.app.state.settings
    headers = {"Authorization": AUTH, "Origin": f"http://{HOST}"}
    page = (await client.get("/", headers=headers)).text
    assert 'class="group"' not in page and 'href="/groups">Edit groups' in page
    assert "Edit groups" not in (await client.get("/", headers={"Authorization": basic("dev")})).text
    page = (await client.get("/groups", headers=headers)).text
    assert 'data-member="srv"' in page
    assert (await client.get("/groups", headers={"Authorization": basic("dev")})).status_code == 403

    body = {"groups": [{"name": "Main servers", "members": ["srv"]}, {"name": "Empty", "members": ["nope"]}]}
    assert (await client.post("/api/groups/save", json=body, headers=headers)).json() == {"ok": True}
    assert "Main servers" in settings.groups_file.read_text()
    page = (await client.get("/", headers=headers)).text
    assert "<h2>Main servers <span" in page and "Empty" not in page and "Other servers" not in page
    # the filter finds a server by its group too
    assert "1 of 1 server." in (await client.get("/?q=main", headers=headers)).text
    resp = await client.post("/api/groups/save", json={"groups": [{"name": "x", "members": ["a b"]}]},
                             headers=headers)
    assert resp.status_code == 400 and "is not a server" in resp.json()["error"]
    resp = await client.post("/api/groups/save", json=body,
                             headers={"Authorization": basic("dev"), "Origin": f"http://{HOST}"})
    assert resp.status_code == 403
