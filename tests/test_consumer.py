# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consumer side of a puller: conftest's env OmniaCLI publishes
srv/production on a relay's puller and, through another link of the relay,
is its consumer too: srv/test copies its database from the puller. Both
links are the relay's own, with their own tokens, as two OmniaCLI would be."""

import asyncio
import base64
import datetime
import stat
import time

import httpx
import pytest
import yaml

from omniacli import consumer, publisher
from omniacli.app import create_app
from omniacli.config import (
    ConfigError,
    load_connections,
    parse_connection,
    parse_link,
    parse_link_source,
    parse_publication,
    parse_servers,
    save_connections,
    server_to_dict,
)
from omniacli.pullcrypto import new_key
from omniacli.schedules import parse_schedule, schedule_to_dict

from .conftest import PASSWORD, BasicAsSession
from .test_pullers import API_TOKEN, DEV_TOKEN, HOST, Internet, relay_settings

KEY = new_key()
HERE = "omniacli.test"


def as_user(user, post=False):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()}
    if post:
        headers["Origin"] = f"http://{HERE}"
    return headers


# --- the connection and the source --------------------------------------------

def test_parse_connection(tmp_path):
    found = parse_connection("api-prod", {"link": "vps", "puller": "api-prod", "key": KEY})
    assert (found.link, found.puller, found.key) == ("vps", "api-prod", KEY)
    for bad in ({"puller": "p"}, {"link": "vps"}, {"link": "vps", "puller": "p", "key": "x"}):
        with pytest.raises(ConfigError):
            parse_connection("c", bad)
    path = tmp_path / "connections.yaml"
    save_connections(path, {"api-prod": found})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and load_connections(path) == {"api-prod": found}


def test_a_puller_is_a_source_of_a_database(env):
    src = parse_link_source({"puller": "api-prod"}, "w")
    assert src.is_puller and src.label == "puller api-prod" and src.to_dict() == {"puller": "api-prod"}
    assert parse_link_source({"link": "c", "server": "s"}, "w").to_dict() == {"link": "c", "server": "s",
                                                                             "role": "production"}
    with pytest.raises(ConfigError):
        parse_link_source({"puller": "a b"}, "w")
    # in the servers file and in a schedule
    data = server_to_dict(env["settings"].servers["srv"])
    data["test"]["from_link"] = {"puller": "api-prod"}
    srv = parse_servers({"srv": data})["srv"]
    assert srv.test.from_link.is_puller and server_to_dict(srv)["test"]["from_link"] == {"puller": "api-prod"}
    sch = parse_schedule("n", {"instance": "srv/test", "cron": "0 2 * * *", "copy": {"puller": "api-prod"}})
    assert sch.copy.source.is_puller and schedule_to_dict(sch)["copy"]["puller"] == "api-prod"
    assert sch.steps == ["copy from puller api-prod"]


# --- one OmniaCLI, producer and consumer through the relay --------------------

@pytest.fixture
async def here(env, tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "WAIT", 1)
    monkeypatch.setattr(consumer, "WAIT", 1)
    monkeypatch.setattr(consumer, "POLL_EVERY", 0.05)
    relay_app = create_app(relay_settings(tmp_path, {"api-prod": {"producer": "api", "consumers": ["dev"]}}))
    settings = env["settings"]
    settings.link_targets = {"vps-api": parse_link("vps-api", {"url": f"https://{HOST}", "token": API_TOKEN}),
                             "vps-dev": parse_link("vps-dev", {"url": f"https://{HOST}", "token": DEV_TOKEN})}
    settings.publications = {"srv/production": parse_publication("srv/production", {
        "link": "vps-api", "puller": "api-prod", "key": KEY,
        "commands": {"backup": {"enabled": True}, "restart": {"enabled": True}}})}
    settings.connections = {"api-prod": parse_connection("api-prod", {"link": "vps-dev", "puller": "api-prod",
                                                                      "key": KEY})}
    data = server_to_dict(settings.servers["srv"])
    data["test"]["from_link"] = {"puller": "api-prod"}
    settings.servers = parse_servers({"srv": data})
    app = create_app(settings, link_transport=Internet(relay_app))
    app.state.publications.start()
    app.state.consumers.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HERE}") as client:
        client.app, client.relay_app, client.settings = app, relay_app, settings
        try:
            yield client
        finally:
            await app.state.consumers.stop()
            await app.state.publications.stop()


async def until(check, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await check()
        if found:
            return found
        await asyncio.sleep(0.1)
    raise AssertionError("not in time")


async def state(client):
    return (await client.get("/api/connections/api-prod", headers=as_user("viewer"))).json()


async def wait_job(client, job_id):
    async def done():
        data = (await client.get(f"/api/jobs/{job_id}", headers=as_user("admin"))).json()
        return data if data["status"] in ("success", "failed", "stopped", "interrupted") else None
    return await until(done, 30)


async def test_the_consumer_shows_the_instance_there(here):
    data = await until(lambda: _with_report(here))
    latest = data["latest"]
    assert latest["instance"] == "srv/production" and latest["commands"]["backup"]["enabled"]
    assert data["status"]["ok"] and data["has_key"] and data["relay"]["producer"]["seen"]
    page = await here.get("/pullers/connections/api-prod", headers={**as_user("viewer"), "Accept": "text/html"})
    assert page.status_code == 200 and "api-prod" in page.text
    # in the menu of every page
    assert '/pullers/connections/api-prod">api-prod</a>' in page.text
    assert (await here.get("/pullers/connections/nope", headers=as_user("viewer"))).status_code == 404


async def _with_report(client):
    data = await state(client)
    return data if data["latest"].get("instance") else None


async def test_the_load_is_kept_on_file_and_served(here):
    sampler = here.app.state.sampler
    now = int(time.time())
    sampler.history.setdefault("srv/production", __import__("collections").deque()).append(
        {"t": now, "cpu": 12.5, "mem": 100.0, "procs": 3})

    async def got():
        data = (await here.get("/api/connections/api-prod/metrics?since=0", headers=as_user("viewer"))).json()
        return data if any(p["cpu"] == 12.5 for p in data["instance"]) else None
    await until(got)
    run = here.app.state.consumers.conn("api-prod")
    day_file = run.dir / f"metrics-{datetime.date.today().isoformat()}.jsonl"
    assert '"cpu": 12.5' in day_file.read_text()
    # the last hour from memory
    recent = (await here.get(f"/api/connections/api-prod/metrics?since={now - 10}", headers=as_user("viewer"))).json()
    assert [p["cpu"] for p in recent["instance"]] == [12.5]
    # files older than KEEP_DAYS go
    old = run.dir / f"metrics-{(datetime.date.today() - datetime.timedelta(days=8)).isoformat()}.jsonl"
    old.write_text("")
    run.prune_metrics()
    assert not old.exists() and day_file.exists()


async def test_copy_from_the_puller_into_test(here, env):
    await until(lambda: _with_report(here))
    page = (await here.get("/servers/srv?view=test", headers=as_user("dev"))).text
    assert "puller api-prod" in page
    resp = await here.post("/servers/srv/test/from-link", headers=as_user("dev", post=True),
                           json={"backup": "", "no_filestore": True})
    assert resp.status_code == 200, resp.text
    job = await wait_job(here, resp.json()["job"])
    assert job["status"] == "success", "\n".join(job["lines"])
    assert job["title"] == "Copy from puller api-prod to srv/test (new backup without filestore)"
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["backup", "download", "decrypt", "restore"]
    assert any("api-prod: " in line and "backup" in line for line in job["lines"])
    calls = env["calls"].read_text().splitlines()
    backup = next(c for c in calls if c.startswith("backup"))
    restore = next(c for c in calls if c.startswith("restore"))
    assert "--no-filestore DB_production" in backup and "DB_test" in restore
    folder = here.settings.backup_dir / "srv" / "test"
    [local] = [p for p in folder.iterdir() if p.name.startswith("api-prod_")]
    assert local.read_bytes() == b"dump\n" and local.name in restore
    # the file went from the relay, and nothing encrypted is left here
    assert not list((here.relay_app.state.pullers.folder / "api-prod" / "files").iterdir())
    assert not [p for p in folder.iterdir() if p.name.endswith(".enc") or p.name.startswith(".")]
    # the producer's job is shown with its log
    data = await state(here)
    [remote] = [j for j in data["jobs"] if j["title"].startswith("Backup srv/production")]
    assert remote["status"] == "success" and remote["source"].startswith("puller api-prod (")
    lines = (await here.get(f"/api/connections/api-prod/jobs/{remote['id']}", headers=as_user("viewer"))).json()
    assert lines["lines"] and lines["next"] == len(lines["lines"])
    assert (await here.get(f"/api/connections/api-prod/jobs/{remote['id']}?since={lines['next']}",
                           headers=as_user("viewer"))).json()["lines"] == []


async def test_a_scheduled_copy_from_the_puller(here, env):
    await until(lambda: _with_report(here))
    resp = await here.post("/api/schedules/save", headers=as_user("admin", post=True), json={
        "original": None, "name": "nightly",
        "schedule": {"instance": "srv/test", "cron": "0 2 * * *", "copy": {"puller": "api-prod", "keep_backups": 1}}})
    assert resp.status_code == 200, resp.text
    assert yaml.safe_load(here.settings.schedules_file.read_text())["schedules"]["nightly"]["copy"]["puller"] == "api-prod"
    resp = await here.post("/api/schedules/nightly/run", headers=as_user("admin", post=True))
    job = await wait_job(here, resp.json()["job"])
    assert job["status"] == "success", "\n".join(job["lines"])
    assert any(c.startswith("restore") and "DB_test" in c for c in env["calls"].read_text().splitlines())
    remote = [j for j in (await state(here))["jobs"] if j["title"].startswith("Backup")]
    assert remote and "(" in remote[0]["source"] and "omniacli" in remote[0]["source"]


async def test_commands_from_the_page(here, env):
    await until(lambda: _with_report(here))
    send = lambda user, body: here.post("/api/connections/api-prod/commands", headers=as_user(user, post=True), json=body)
    resp = await send("viewer", {"action": "restart"})
    assert resp.status_code == 200
    cid = resp.json()["command"]

    async def answered():
        sent = {c["command"]: c for c in (await state(here))["sent"]}
        return sent[cid]["answer"]
    answer = await until(answered)
    assert answer["ok"] and answer["job"]
    resp = await send("viewer", {"action": "start"})
    cid = resp.json()["command"]
    answer = await until(answered)
    assert not answer["ok"] and "not exposed" in answer["error"]
    # the roles, as for the same actions here
    assert (await send("viewer", {"action": "backup"})).status_code == 403
    assert (await send("dev", {"action": "stop-job", "params": {"job": "x"}})).status_code == 403
    assert (await send("admin", {"action": "pull"})).status_code == 400
    resp = await here.post("/api/connections/api-prod/commands", headers={"Authorization": "Bearer mcp-test-token"},
                           json={"action": "restart"})
    assert resp.status_code == 403


async def test_a_wrong_key_fails_at_once(here):
    here.settings.connections["api-prod"] = parse_connection(
        "api-prod", {"link": "vps-dev", "puller": "api-prod", "key": new_key()})

    async def refused():
        data = await state(here)
        return data if data["status"]["ok"] is False and "a report refused" in data["status"]["error"] else None
    await until(refused)
    resp = await here.post("/servers/srv/test/from-link", headers=as_user("dev", post=True), json={"backup": ""})
    job = await wait_job(here, resp.json()["job"])
    assert job["status"] == "failed" and "is the key the one" in job["error"]


async def test_the_admin_connects_a_puller(here):
    post = as_user("admin", post=True)
    body = {"name": "other", "link": "vps-dev", "puller": "nope", "key": "", "new": True}
    assert (await here.post("/api/connections/save", headers=post, json=body)).json() == {"ok": True}
    assert load_connections(here.settings.connections_file)["other"].key == ""
    assert (await here.post("/api/connections/save", headers=post, json={**body, "link": "ghost", "new": False})
            ).status_code == 400
    page = await here.get("/pullers", headers={**as_user("admin"), "Accept": "text/html"})
    assert page.status_code == 200 and "Connected here" in page.text and KEY not in page.text

    async def failing():
        status = here.app.state.consumers.conn("other").status
        return status if status["ok"] is False else None
    status = await until(failing)
    assert "No puller nope" in status["error"]
    assert (await here.post("/api/connections/delete", headers=post, json={"name": "other"})).json() == {"ok": True}
    assert "other" not in here.settings.connections
    assert (await here.post("/api/connections/save", headers=as_user("dev", post=True), json=body)).status_code == 403


async def test_stopping_the_copy_stops_the_backup_there(here, monkeypatch):
    from omniacli import ops

    async def slow_backup(job, instance, backup_dir, options=None):
        job.log("a long backup")
        await asyncio.sleep(30)
    monkeypatch.setattr(ops, "backup", slow_backup)
    await until(lambda: _with_report(here))
    resp = await here.post("/servers/srv/test/from-link", headers=as_user("dev", post=True), json={"backup": ""})
    local = resp.json()["job"]

    async def running_there():
        jobs = (await state(here))["jobs"]
        return [j for j in jobs if j.get("status") == "running"]
    [remote] = await until(running_there)
    assert (await here.post(f"/jobs/{local}/stop", headers=as_user("admin", post=True))).status_code in (200, 303)
    assert (await wait_job(here, local))["status"] == "stopped"

    async def stopped_there():
        jobs = {j["id"]: j for j in (await state(here))["jobs"]}
        return jobs[remote["id"]] if jobs[remote["id"]].get("status") == "stopped" else None
    found = await until(stopped_there)
    assert found["error"].startswith("stopped by puller api-prod (")
