import asyncio
import base64
import datetime

import httpx
import pytest

from omniaci.app import create_app
from omniaci.logtail import follow

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
    assert resp.status_code == 200 and "srv" in resp.text and "DB_production" in resp.text


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


async def test_follow_handles_rotation(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("a\nb\nc\n")
    gen = follow(log, initial=2, poll=0.01)
    assert [await gen.__anext__() for _ in range(2)] == ["b", "c"]
    with open(log, "a") as fh:
        fh.write("d\npart")
    assert await gen.__anext__() == "d"
    with open(log, "a") as fh:
        fh.write("ial\n")
    assert await gen.__anext__() == "partial"
    log.rename(tmp_path / "x.log.1")
    log.write_text("new\n")
    assert "rotated" in await gen.__anext__()
    assert await gen.__anext__() == "new"
    await gen.aclose()


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
    assert resp.status_code == 200, resp.text
    assert list(load_servers(settings.servers_file)) == ["srv", "srv2"]
    assert "srv2" in (await client.get("/", headers={"Authorization": AUTH})).text
    assert len(client.app.state.updater._tasks) == 1  # the clone's auto update runs

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
    # "first" got the same configuration, auto update included
    assert len(client.app.state.updater._tasks) == 1


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
    page = await client.get("/", headers={"Authorization": AUTH})
    assert 'class="open-update"' in page.text and "/servers/srv/test/restart" in page.text
