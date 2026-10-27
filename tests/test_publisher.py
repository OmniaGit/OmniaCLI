# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The producer side of a puller: an instance of conftest's env published on
the puller of a relay, both OmniaCLI in this process. The consumer is played
here with the relay's routes and the puller's key, as its side does not
exist yet."""

import asyncio
import datetime
import os
import stat
import time

import httpx
import pytest
import yaml

from omniacli import publisher
from omniacli.app import create_app
from omniacli.config import (
    ConfigError,
    Restart,
    load_publications,
    parse_link,
    parse_publication,
    save_publications,
)
from omniacli.pullcrypto import COMMAND, REPORT, Command, Sealer, decrypt_file, new_key, parse_key

from .conftest import PASSWORD, BasicAsSession
from .test_pullers import DEV, HOST, API_TOKEN, Internet, relay_settings

KEY_TEXT = new_key()
KEY = parse_key(KEY_TEXT)


# --- the publication ---------------------------------------------------------

def test_parse_publication():
    pub = parse_publication("srv/production", {"link": "vps", "puller": "api-prod", "key": KEY_TEXT, "commands": {
        "backup": {"enabled": True, "window": "22:00-06:00"}, "stop": {"enabled": True}}})
    assert pub.exposed("backup").enabled and pub.exposed("backup").window_label() == "22:00-06:00"
    assert pub.exposed("backup").in_window(datetime.time(23, 0)) and not pub.exposed("backup").in_window(datetime.time(12, 0))
    assert pub.exposed("stop").window_label() == "always" and not pub.exposed("restart").enabled
    assert parse_publication("srv/production", pub.to_dict()) == pub


@pytest.mark.parametrize("instance, data", [
    ("srv", {"link": "vps", "puller": "p"}),
    ("srv/staging", {"link": "vps", "puller": "p"}),
    ("srv/test", {"puller": "p"}),
    ("srv/test", {"link": "vps", "puller": "a b"}),
    ("srv/test", {"link": "vps", "puller": "p", "key": "opk1-short"}),
    ("srv/test", {"link": "vps", "puller": "p", "commands": {"pull": {"enabled": True}}}),
    ("srv/test", {"link": "vps", "puller": "p", "commands": {"stop": {"window": "8-9"}}}),
])
def test_a_bad_publication_is_refused(instance, data):
    with pytest.raises(ConfigError):
        parse_publication(instance, data)


def test_the_publications_file_is_for_its_owner_only(tmp_path):
    path = tmp_path / "publications.yaml"
    pubs = {"srv/test": parse_publication("srv/test", {"link": "vps", "puller": "p", "key": KEY_TEXT})}
    save_publications(path, pubs)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_publications(path) == pubs


# --- the producer and the relay ------------------------------------------------

WATCHED = ("restart", "stop")


@pytest.fixture
async def published(env, tmp_path, monkeypatch):
    """The relay (with puller api-prod: producer api, consumer dev) and env's
    OmniaCLI publishing srv/production on it, with the key; a report every
    second, not every 25."""
    monkeypatch.setattr(publisher, "WAIT", 1)
    relay_app = create_app(relay_settings(tmp_path, {"api-prod": {"producer": "api", "consumers": ["dev"]}}))
    settings = env["settings"]
    settings.link_targets = {"vps": parse_link("vps", {"url": f"https://{HOST}", "token": API_TOKEN})}
    later = (datetime.datetime.now() + datetime.timedelta(hours=2)).strftime("%H:%M")
    much_later = (datetime.datetime.now() + datetime.timedelta(hours=3)).strftime("%H:%M")
    settings.publications = {"srv/production": parse_publication("srv/production", {
        "link": "vps", "puller": "api-prod", "key": KEY_TEXT,
        "commands": {"backup": {"enabled": True}, "restart": {"enabled": True},
                     "stop": {"enabled": True, "window": f"{later}-{much_later}"}}})}
    api_app = create_app(settings, link_transport=Internet(relay_app))
    relay = httpx.AsyncClient(transport=httpx.ASGITransport(app=relay_app), base_url=f"http://{HOST}")
    api_app.state.publications.start()
    consumer = Consumer(relay)
    try:
        yield consumer, api_app, relay_app
    finally:
        await api_app.state.publications.stop()
        await relay.aclose()


class Consumer:
    """The consumer's side, by hand: sealed commands, reports read in order."""

    def __init__(self, relay):
        self.relay = relay
        self.sealer = Sealer(KEY, "api-prod")
        self.since = 0
        self.reports = []

    async def send(self, cmd: Command, envelope=None) -> int:
        resp = await self.relay.post("/api/pullers/api-prod/commands", headers=DEV,
                                     json={"command": envelope or self.sealer.seal(COMMAND, cmd.to_dict())})
        assert resp.status_code == 200, resp.text
        return resp.json()["qid"]

    async def until(self, found, seconds=20):
        """Read the reports until found(report) is true for one; that report."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            data = (await self.relay.get("/api/pullers/api-prod/reports",
                                         params={"since": self.since, "wait": 1}, headers=DEV)).json()
            for item in data["reports"]:
                report = self.sealer.open(REPORT, item["envelope"])
                self.reports.append(report)
                self.since = item["seq"]
                if found(report):
                    return report
        raise AssertionError("no such report")

    async def answer(self, qid):
        report = await self.until(lambda r: any(a["qid"] == qid for a in r["answers"]))
        return next(a for a in report["answers"] if a["qid"] == qid)

    async def job_end(self, job_id):
        """The job's status as reported at its end, and all its lines in order."""
        lines = []

        def ended(report):
            for job in report["jobs"]:
                if job["id"] == job_id:
                    assert job["lines_from"] == len(lines)
                    lines.extend(job["lines"])
                    if job["status"] in ("success", "failed", "stopped", "interrupted"):
                        return True
            return False
        # the reports already read count too
        for report in self.reports:
            if ended(report):
                break
        else:
            await self.until(ended)
        last = [j for r in self.reports for j in r["jobs"] if j["id"] == job_id][-1]
        return last, lines


async def test_the_producer_reports_its_state(published):
    consumer, api_app, relay_app = published
    report = await consumer.until(lambda r: True)
    assert report["instance"] == "srv/production" and report["type"] == "odoo" and report["encrypted"]
    assert report["state"] in ("running", "stopped", "unknown") and report["busy"] is False
    assert report["commands"]["backup"] == {"enabled": True, "window": "always"}
    assert report["commands"]["start"]["enabled"] is False
    assert any(f["key"] == "no_filestore" for f in report["backup_fields"])
    assert isinstance(report["disks"], list) and report["disks"]
    assert "disk_history" in report and set(report["metrics"]) >= {"machine", "instance", "cores"}
    # nothing readable on the relay
    text = (relay_app.state.pullers.folder / "api-prod" / "reports.jsonl").read_text()
    assert "srv/production" not in text and "odoo" not in text


async def test_samples_are_sent_once_and_none_lost(published):
    consumer, api_app, _ = published
    sampler = api_app.state.sampler
    sampler.machine.append({"t": int(time.time()) + 1000, "cpu": 1.0, "rx": 0, "tx": 0})
    report = await consumer.until(lambda r: any(p["t"] == int(time.time()) + 1000 or p["cpu"] == 1.0
                                                for p in r["metrics"]["machine"]))
    sampler.machine.append({"t": int(time.time()) + 2000, "cpu": 2.0, "rx": 0, "tx": 0})
    report = await consumer.until(lambda r: bool(r["metrics"]["machine"]))
    assert [p["cpu"] for p in report["metrics"]["machine"]] == [2.0]
    # the disk history went with the first report only
    assert sum("disk_history" in r for r in consumer.reports) == 1


async def test_a_backup_reaches_the_consumer_encrypted(published, env, tmp_path):
    consumer, api_app, relay_app = published
    cmd = Command("backup", "dev: anna", {"no_filestore": True})
    answer = await consumer.answer(await consumer.send(cmd))
    assert answer["ok"] and answer["command"] == cmd.id and answer["action"] == "backup"
    # the poll that took the command ended well (a poll's end is known only then)
    status = api_app.state.publications.status("srv/production")
    assert status["ok"] is True and status["commands"] == 1
    job, lines = await consumer.job_end(answer["job"])
    assert job["status"] == "success", lines
    assert job["source"] == "puller api-prod (dev: anna)"
    assert job["result"]["encrypted"] and job["result"]["file"].endswith("_nofilestore.zip.enc")
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["backup", "encrypt", "upload"]
    assert any("upload" in line for line in lines)
    # the file is on the relay, encrypted; the consumer decrypts it
    name = job["result"]["file"]
    resp = await consumer.relay.get(f"/api/pullers/api-prod/files/{name}", headers=DEV)
    assert resp.status_code == 200 and b"dump" not in resp.content
    (tmp_path / "got.enc").write_bytes(resp.content)
    decrypt_file(KEY, "api-prod", tmp_path / "got.enc", tmp_path / "got.zip")
    local = env["settings"].backup_dir / "srv" / "production" / job["result"]["backup"]
    assert (tmp_path / "got.zip").read_bytes() == local.read_bytes()
    # nothing encrypted is left behind on the producer
    assert not any((api_app.state.publications.folder / "uploads").iterdir())


async def test_commands_not_exposed_or_out_of_hours_are_refused(published, env):
    consumer, _, _ = published
    answer = await consumer.answer(await consumer.send(Command("start", "dev: anna")))
    assert not answer["ok"] and "not exposed" in answer["error"]
    answer = await consumer.answer(await consumer.send(Command("stop", "dev: anna")))
    assert not answer["ok"] and "only, it is" in answer["error"]
    answer = await consumer.answer(await consumer.send(Command("pull", "dev: anna")))
    assert not answer["ok"] and "not a command" in answer["error"]
    # nothing ran
    assert "stop" not in (env["calls"].read_text() if env["calls"].exists() else "")


async def test_replays_and_clear_commands_are_refused(published):
    consumer, _, _ = published
    cmd = Command("restart", "dev: anna")
    envelope = consumer.sealer.seal(COMMAND, cmd.to_dict())
    first = await consumer.answer(await consumer.send(cmd, envelope))
    assert first["ok"]
    again = await consumer.answer(await consumer.send(cmd, envelope))
    assert not again["ok"] and "already received" in again["error"]
    old = Command("restart", "dev: anna", created=time.time() - 3600)
    answer = await consumer.answer(await consumer.send(old))
    assert not answer["ok"] and "too old" in answer["error"]
    clear = Sealer(None, "api-prod").seal(COMMAND, Command("restart", "relay").to_dict())
    answer = await consumer.answer(await consumer.send(None, clear))
    assert not answer["ok"] and answer["command"] is None and "in clear" in answer["error"]


async def test_a_restart_runs_and_a_long_job_is_stopped(published, env, monkeypatch):
    consumer, api_app, _ = published
    answer = await consumer.answer(await consumer.send(Command("restart", "dev: anna")))
    job, _ = await consumer.job_end(answer["job"])
    assert job["status"] == "success" and "restart odoo-production" in env["calls"].read_text()
    # a restart that hangs: stopped on the consumer's request
    monkeypatch.setattr(Restart, "command", lambda self, action="restart": ["sleep", "30"])
    answer = await consumer.answer(await consumer.send(Command("restart", "dev: anna")))
    await consumer.until(lambda r: any(j["id"] == answer["job"] and j["status"] == "running" for j in r["jobs"]))
    stop = await consumer.answer(await consumer.send(Command("stop-job", "dev: anna", {"job": answer["job"]})))
    assert stop["ok"]
    job, _ = await consumer.job_end(answer["job"])
    assert job["status"] == "stopped" and job["error"] == "stopped by puller api-prod (dev: anna)"
    # only the jobs of the puller
    other = api_app.state.jobs.submit("x", ["srv/test"], "manual", lambda job: asyncio.sleep(5))
    refused = await consumer.answer(await consumer.send(Command("stop-job", "dev: anna", {"job": other.id})))
    assert not refused["ok"] and "not started through puller" in refused["error"]
    api_app.state.jobs.stop(other.id, "test")


async def test_the_seen_commands_survive_a_restart_of_the_producer(published, env):
    consumer, api_app, relay_app = published
    cmd = Command("restart", "dev: anna")
    envelope = consumer.sealer.seal(COMMAND, cmd.to_dict())
    assert (await consumer.answer(await consumer.send(cmd, envelope)))["ok"]
    await api_app.state.publications.stop()
    again = create_app(env["settings"], link_transport=Internet(relay_app))
    again.state.publications.start()
    try:
        answer = await consumer.answer(await consumer.send(cmd, envelope))
        assert not answer["ok"] and "already received" in answer["error"]
    finally:
        await again.state.publications.stop()


async def test_a_relay_that_cannot_be_reached_is_shown(env, tmp_path):
    settings = env["settings"]
    settings.link_targets = {"vps": parse_link("vps", {"url": "https://nowhere.test", "token": "t"})}
    settings.publications = {"srv/production": parse_publication("srv/production", {"link": "vps", "puller": "p"})}
    app = create_app(settings, link_transport=Internet(create_app(relay_settings(tmp_path, {}))))
    app.state.publications.start()
    try:
        for _ in range(100):
            status = app.state.publications.status("srv/production")
            if status.get("ok") is False:
                break
            await asyncio.sleep(0.05)
        assert status["ok"] is False and "cannot connect" in status["error"]
    finally:
        await app.state.publications.stop()


# --- the Pullers page, publications -------------------------------------------

async def test_the_admin_publishes_an_instance(env):
    settings = env["settings"]
    settings.link_targets = {"vps": parse_link("vps", {"url": f"https://{HOST}", "token": "t"})}
    app = create_app(settings, link_transport=Internet(create_app(relay_settings(env["tmp"], {}))))
    import base64

    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
    post = {**auth, "Origin": "http://omniacli.test"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url="http://omniacli.test") as client:
        key = (await client.post("/api/publications/key", headers=post)).json()["key"]
        assert key.startswith("opk1-")
        body = {"instance": "srv/production", "link": "vps", "puller": "api-prod", "key": "", "new": True,
                "commands": {"backup": {"enabled": True, "window": ""}, "stop": {"enabled": True, "window": "22:00-23:00"}}}
        resp = await client.post("/api/publications/save", headers=post, json=body)
        assert resp.json()["ok"] and "stop exposed without a key" in resp.json()["warning"]
        assert (await client.post("/api/publications/save", headers=post, json=body)).status_code == 400
        # a blank key keeps the one saved; remove_key drops it
        body.update(new=False, key=key)
        assert (await client.post("/api/publications/save", headers=post, json=body)).json()["warning"] == ""
        body["key"] = ""
        await client.post("/api/publications/save", headers=post, json=body)
        assert load_publications(settings.publications_file)["srv/production"].key == key
        await client.post("/api/publications/save", headers=post, json={**body, "remove_key": True})
        assert load_publications(settings.publications_file)["srv/production"].key == ""
        for bad in ({**body, "instance": "nope/production"}, {**body, "link": "ghost"}):
            assert (await client.post("/api/publications/save", headers=post, json=bad)).status_code in (400, 404)
        page = await client.get("/pullers", headers={**auth, "Accept": "text/html"})
        assert page.status_code == 200 and "srv/production" in page.text and key not in page.text
        assert (await client.post("/api/publications/delete", headers=post,
                                  json={"instance": "srv/production"})).json() == {"ok": True}
        assert settings.publications == {}
        await app.state.publications.stop()
