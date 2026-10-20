import asyncio
import base64
import datetime

import httpx
import pytest

from omniaci.app import create_app
from omniaci.logtail import read_since

from .conftest import PASSWORD, clone, commit, sh

AUTH = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
HOST = "omniaci.test"


@pytest.fixture
async def client(env):
    app = create_app(env["settings"])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as c:
            c.app = app
            yield c


async def wait_job(client, job_id):
    for _ in range(200):
        data = (await client.get(f"/api/jobs/{job_id}", headers={"Authorization": AUTH})).json()
        if data["status"] in ("success", "failed", "interrupted"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_requires_auth(client):
    assert (await client.get("/")).status_code == 401
    bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
    assert (await client.get("/", headers={"Authorization": bad})).status_code == 401
    resp = await client.get("/", headers={"Authorization": AUTH})
    assert resp.status_code == 200 and 'href="/servers/srv?view=both"' in resp.text
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
    path = env["settings"].instance("srv", "test").repository("odoo").path
    (path / "base.txt").write_text("dirty\n")
    resp = await client.post(
        "/servers/srv/test/pull-restart", headers={"Authorization": AUTH, "Origin": f"http://{HOST}"}
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
    import omniaci.logtail as logtail

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
    from omniaci.config import load_servers, server_to_dict

    server = server_to_dict(settings.servers["srv"])
    server["test"]["auto_update"] = {"enabled": True, "interval_minutes": 10, "window": "00:00-00:01"}

    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": None, "name": "srv2", "server": server})
    assert resp.status_code == 200 and resp.json()["status"] == "draft", resp.text
    assert list(load_servers(settings.servers_file)) == ["srv", "srv2"]
    # a draft is not live: not on the dashboard, no actions, no auto update
    index = (await client.get("/", headers={"Authorization": AUTH})).text
    assert "/servers/srv2" not in index and "1 draft server" in index
    assert (await client.post("/servers/srv2/test/pull", headers=POST)).status_code == 404
    assert (await client.get("/servers/srv2", headers={"Authorization": AUTH})).status_code == 404
    assert client.app.state.updater._tasks == []
    # active through the checks (here made to pass)
    import omniaci.checks as checks

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
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "srv", "name": "srv", "server": bad})
    assert resp.status_code == 400 and "missing" in resp.json()["error"]
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
    test.odoo_config.write_text(
        f"[options]\naddons_path = {odoo_repo / 'addons'},\n    {tmp / 'extra'}\n"
    )
    (test.venv_bin / "python").symlink_to(sys.executable)
    return test


async def test_list_addons(env):
    from omniaci import ops

    test = make_addons(env)
    addons = ops.list_addons(test)
    assert [(a["name"], a["in_repository"]) for a in addons] == [("in_repo", True), ("outside", False)]
    assert ops.odoo_major(ops.find_odoo_bin(test)) == 18


async def test_update_popup_actions(client, env):
    make_addons(env)
    test = env["settings"].instance("srv", "test")
    test.update_modules = False  # the explicit update runs anyway
    data = (await client.get("/api/servers/srv/test/addons", headers={"Authorization": AUTH})).json()
    assert [a["name"] for a in data["addons"]] == ["in_repo", "outside"]
    assert data["odoo_bin"].endswith("odoo-bin")

    url = "/api/servers/srv/test/update"
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
    assert 'class="open-update"' in page.text and "/servers/srv/test/restart" in page.text
    assert "/servers/srv/production/restart" not in page.text


async def test_failed_checks_keep_live_server_unchanged(client, env):
    """Editing a live server with activate=True and failing checks saves nothing."""
    from omniaci.config import server_to_dict

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
