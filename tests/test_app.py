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
