# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The relay side of a puller: its definition, the commands, the reports and
the files, and three OmniaCLI in one process (the relay, the producer and
the consumer, each linking to the relay with a token of its own)."""

import asyncio
import base64
import os
import time

import httpx
import pytest
import yaml

from omniacli import puller
from omniacli.app import create_app
from omniacli.config import ConfigError, load_pullers, parse_puller, parse_settings, save_pullers
from omniacli.links import LinkClients, link_headers
from omniacli.pullcrypto import COMMAND, REPORT, Command, Sealer, decrypt_file, encrypt_file, new_key, parse_key

from .conftest import PASSWORD, TOKEN, USER_HASH, BasicAsSession, hash_token

API_TOKEN, DEV_TOKEN, OTHER_TOKEN = "api-link-token", "dev-link-token", "other-link-token"
HOST = "relay.test"


def relay_settings(tmp_path, pullers=None):
    conf = tmp_path / "relay"
    conf.mkdir(exist_ok=True)
    if pullers is not None:
        (conf / "pullers.yaml").write_text(yaml.safe_dump({"pullers": pullers}))
    return parse_settings({
        "auth": {
            "users": {"admin": USER_HASH, "viewer": {"password": USER_HASH, "role": "user"}},
            "tokens": {"tester": {"token": hash_token(TOKEN), "role": "admin"}},
            "links": {name: {"token": hash_token(token), "role": "user"}
                      for name, token in (("api", API_TOKEN), ("dev", DEV_TOKEN), ("other", OTHER_TOKEN))},
        },
        "backup_dir": str(tmp_path / "relay-backups"), "jobs_dir": str(tmp_path / "relay-jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)


PULLERS = {"api-prod": {"producer": "api", "consumers": ["dev"]}}


@pytest.fixture
async def relay(tmp_path):
    app = create_app(relay_settings(tmp_path, PULLERS))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as client:
        client.app = app
        yield client


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


API, DEV, OTHER = bearer(API_TOKEN), bearer(DEV_TOKEN), bearer(OTHER_TOKEN)
CLEAR = Sealer(None, "api-prod")


def as_user(user, post=False):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()}
    if post:
        headers["Origin"] = f"http://{HOST}"
    return headers


# --- the definition -----------------------------------------------------------

def test_parse_puller():
    found = parse_puller("api-prod", {"producer": "api", "consumers": ["dev", "dev", " other "]})
    assert (found.producer, found.consumers, found.keep_hours, found.max_file_mib) == \
        ("api", ["dev", "other"], 24, 10240)
    assert found.part("api") == "producer" and found.part("other") == "consumer" and found.part("x") is None


@pytest.mark.parametrize("name, data", [
    ("bad name", {"producer": "api", "consumers": ["dev"]}),
    ("p", {"consumers": ["dev"]}),
    ("p", {"producer": "api"}),
    ("p", {"producer": "api", "consumers": "dev"}),
    ("p", {"producer": "api", "consumers": ["api"]}),
    ("p", {"producer": "api", "consumers": ["a b"]}),
    ("p", {"producer": "api", "consumers": ["dev"], "keep_hours": 0}),
    ("p", {"producer": "api", "consumers": ["dev"], "keep_hours": 721}),
    ("p", {"producer": "api", "consumers": ["dev"], "max_file_mib": "10"}),
])
def test_a_bad_puller_is_refused(name, data):
    with pytest.raises(ConfigError):
        parse_puller(name, data)


def test_pullers_file_round_trips(tmp_path):
    path = tmp_path / "pullers.yaml"
    assert load_pullers(path) == {}
    pullers = {"a": parse_puller("a", {"producer": "api", "consumers": ["dev"], "keep_hours": 2})}
    save_pullers(path, pullers)
    assert load_pullers(path) == pullers
    path.write_text("pullers: {a: {producer: api}}\n")
    with pytest.raises(ConfigError, match="pullers.yaml"):
        load_pullers(path)


def test_settings_find_the_pullers_next_to_the_servers(tmp_path):
    settings = relay_settings(tmp_path, PULLERS)
    assert settings.pullers_file == tmp_path / "relay" / "pullers.yaml"
    assert list(settings.pullers) == ["api-prod"]


# --- who may use a puller -----------------------------------------------------

async def test_only_the_links_of_a_puller_use_it(relay):
    assert (await relay.get("/api/pullers", headers=API)).json() == \
        {"pullers": [{"name": "api-prod", "part": "producer"}]}
    assert (await relay.get("/api/pullers", headers=DEV)).json() == \
        {"pullers": [{"name": "api-prod", "part": "consumer"}]}
    assert (await relay.get("/api/pullers", headers=OTHER)).json() == {"pullers": []}
    # a link outside the puller does not learn it exists
    for method, path in (("GET", "/api/pullers/api-prod"), ("POST", "/api/pullers/api-prod/producer"),
                         ("POST", "/api/pullers/api-prod/commands"), ("GET", "/api/pullers/api-prod/reports"),
                         ("GET", "/api/pullers/api-prod/files/a.zip"), ("PUT", "/api/pullers/api-prod/producer/files/a")):
        resp = await relay.request(method, path, headers=OTHER, json={})
        assert resp.status_code == 404, (method, path)
        assert resp.json() == {"error": "No puller api-prod"}
    # each in its own part only
    resp = await relay.post("/api/pullers/api-prod/commands", headers=API, json={"command": CLEAR.seal(COMMAND, {})})
    assert resp.status_code == 403 and "producer" in resp.json()["error"]
    resp = await relay.post("/api/pullers/api-prod/producer", headers=DEV, json={})
    assert resp.status_code == 403
    # not a user, not a token, not a wrong link token
    assert (await relay.get("/api/pullers", headers=as_user("admin"))).status_code == 403
    assert (await relay.get("/api/pullers", headers=bearer(TOKEN))).status_code == 403
    assert (await relay.get("/api/pullers", headers=bearer("wrong"))).status_code == 401


# --- commands -----------------------------------------------------------------

async def test_a_command_reaches_the_producer_waiting_for_it(relay):
    envelope = CLEAR.seal(COMMAND, Command("backup", "dev: anna").to_dict())
    started = time.monotonic()
    poll = asyncio.ensure_future(relay.post("/api/pullers/api-prod/producer?wait=10", headers=API, json={}))
    await asyncio.sleep(0.2)
    assert not poll.done()
    resp = await relay.post("/api/pullers/api-prod/commands", headers=DEV, json={"command": envelope})
    assert resp.status_code == 200
    qid = resp.json()["qid"]
    answer = (await poll).json()
    assert time.monotonic() - started < 5
    assert answer["seq"] is None
    assert [(c["qid"], c["by"], c["envelope"]) for c in answer["commands"]] == [(qid, "dev", envelope)]
    # taken: gone from the relay
    again = await relay.post("/api/pullers/api-prod/producer?wait=0", headers=API, json={})
    assert again.json()["commands"] == []
    assert (await relay.get("/api/pullers/api-prod", headers=DEV)).json()["pending"] == []


async def test_a_poll_without_commands_waits_then_answers_empty(relay):
    started = time.monotonic()
    resp = await relay.post("/api/pullers/api-prod/producer?wait=0.5", headers=API, json={})
    assert resp.status_code == 200 and resp.json()["commands"] == []
    assert 0.4 < time.monotonic() - started < 3
    # never longer than WAIT, whatever is asked
    assert (await relay.post("/api/pullers/api-prod/producer?wait=x", headers=API, json={})).status_code == 400


async def test_a_command_not_taken_expires(relay):
    resp = await relay.post("/api/pullers/api-prod/commands", headers=DEV, json={"command": CLEAR.seal(COMMAND, {})})
    qid = resp.json()["qid"]
    assert resp.json()["expires"] == pytest.approx(time.time() + puller.PICKUP, abs=5)
    hub = relay.app.state.pullers
    hub.state("api-prod").commands[0].at -= puller.PICKUP + 1
    hub.clean()
    info = (await relay.get("/api/pullers/api-prod", headers=DEV)).json()
    assert info["pending"] == [] and [e["qid"] for e in info["expired"]] == [qid]
    assert (await relay.post("/api/pullers/api-prod/producer", headers=API, json={})).json()["commands"] == []


async def test_commands_waiting_are_limited(relay):
    for _ in range(puller.MAX_PENDING):
        assert (await relay.post("/api/pullers/api-prod/commands", headers=DEV,
                                 json={"command": {"v": 1}})).status_code == 200
    resp = await relay.post("/api/pullers/api-prod/commands", headers=DEV, json={"command": {"v": 1}})
    assert resp.status_code == 429 and "does not take them" in resp.json()["error"]


@pytest.mark.parametrize("body, status", [(b"not json", 400), (b"[]", 400), (b'{"command": 1}', 400),
                                          (b"{}", 400), (b'{"command": "' + b"x" * puller.MAX_MESSAGE + b'"}', 413)])
async def test_a_bad_message_is_refused(relay, body, status):
    resp = await relay.post("/api/pullers/api-prod/commands", headers={**DEV, "Content-Type": "application/json"},
                            content=body)
    assert resp.status_code == status


# --- reports ------------------------------------------------------------------

async def test_reports_are_read_from_the_last_one_read(relay):
    for i in range(3):
        resp = await relay.post("/api/pullers/api-prod/producer", headers=API,
                                json={"report": CLEAR.seal(REPORT, {"i": i})})
        assert resp.json()["seq"] == i + 1
    data = (await relay.get("/api/pullers/api-prod/reports?since=0", headers=DEV)).json()
    assert [CLEAR.open(REPORT, r["envelope"]) for r in data["reports"]] == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert (data["next"], data["first"], data["last"], data["reset"]) == (3, 1, 3, False)
    assert data["producer"]["link"] == "api" and data["producer"]["seen"]
    # a consumer waiting is woken by the next report
    wait = asyncio.ensure_future(relay.get("/api/pullers/api-prod/reports?since=3&wait=10", headers=DEV))
    await asyncio.sleep(0.2)
    assert not wait.done()
    await relay.post("/api/pullers/api-prod/producer", headers=API, json={"report": CLEAR.seal(REPORT, {"i": 3})})
    data = (await wait).json()
    assert [r["seq"] for r in data["reports"]] == [4] and data["next"] == 4
    # a since beyond the last one: the reports began again, from the oldest
    data = (await relay.get("/api/pullers/api-prod/reports?since=99", headers=DEV)).json()
    assert data["reset"] and [r["seq"] for r in data["reports"]] == [1, 2, 3, 4]


async def test_old_reports_go_and_the_consumer_sees_the_gap(relay):
    for i in range(3):
        await relay.post("/api/pullers/api-prod/producer", headers=API, json={"report": {"v": 1, "i": i}})
    hub = relay.app.state.pullers
    state = hub.state("api-prod")
    state.reports[0].at -= 25 * 3600
    hub.clean()
    data = (await relay.get("/api/pullers/api-prod/reports?since=0", headers=DEV)).json()
    assert [r["seq"] for r in data["reports"]] == [2, 3] and data["first"] == 2


async def test_the_state_survives_a_restart(tmp_path):
    app = create_app(relay_settings(tmp_path, PULLERS))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        for i in range(2):
            await client.post("/api/pullers/api-prod/producer", headers=API, json={"report": {"v": 1, "i": i}})
        await client.post("/api/pullers/api-prod/commands", headers=DEV, json={"command": {"v": 1}})
    await app.state.pullers.stop()
    app = create_app(relay_settings(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}") as client:
        data = (await client.get("/api/pullers/api-prod/reports", headers=DEV)).json()
        assert [r["envelope"]["i"] for r in data["reports"]] == [0, 1]
        assert data["consumers"][0]["seen"] and len(data["pending"]) == 1
        resp = await client.post("/api/pullers/api-prod/producer", headers=API, json={"report": {"v": 1}})
        assert resp.json()["seq"] == 3 and len(resp.json()["commands"]) == 1
        resp = await client.post("/api/pullers/api-prod/commands", headers=DEV, json={"command": {"v": 1}})
        assert resp.json()["qid"] == 2


# --- files --------------------------------------------------------------------

async def test_a_file_goes_up_comes_down_and_goes(relay):
    data = os.urandom(3 * puller.CHUNK + 5)
    resp = await relay.put("/api/pullers/api-prod/producer/files/db_1.zip", headers=API, content=data)
    assert resp.json() == {"file": "db_1.zip", "size": len(data)}
    info = (await relay.get("/api/pullers/api-prod", headers=DEV)).json()
    assert [(f["name"], f["size"]) for f in info["files"]] == [("db_1.zip", len(data))]
    # only a consumer downloads it, and deletes it
    assert (await relay.get("/api/pullers/api-prod/files/db_1.zip", headers=API)).status_code == 403
    resp = await relay.get("/api/pullers/api-prod/files/db_1.zip", headers=DEV)
    assert resp.status_code == 200 and resp.content == data
    assert (await relay.delete("/api/pullers/api-prod/files/db_1.zip", headers=DEV)).json() == {"ok": True}
    assert (await relay.get("/api/pullers/api-prod/files/db_1.zip", headers=DEV)).status_code == 404
    assert not any((relay.app.state.pullers.folder / "api-prod" / "files").iterdir())


async def test_a_file_over_the_limit_leaves_nothing(tmp_path):
    app = create_app(relay_settings(tmp_path, {"p": {"producer": "api", "consumers": ["dev"], "max_file_mib": 1}}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}") as client:
        big = b"x" * ((1 << 20) + 1)
        resp = await client.put("/api/pullers/p/producer/files/a.zip", headers=API, content=big)
        assert resp.status_code == 413
        # without a length too: counted as it comes
        async def chunks():
            yield big[:1000]
            yield big[1000:]
        resp = await client.put("/api/pullers/p/producer/files/a.zip", headers=API, content=chunks())
        assert resp.status_code == 413
        assert list((app.state.pullers.folder / "p" / "files").iterdir()) == []
        resp = await client.put("/api/pullers/p/producer/files/a.zip", headers=API, content=b"x" * (1 << 20))
        assert resp.status_code == 200


@pytest.mark.parametrize("name", [".hidden", "a b", "a" * 129, "-x"])
async def test_a_bad_file_name_is_refused(relay, name):
    resp = await relay.put(f"/api/pullers/api-prod/producer/files/{name}", headers=API, content=b"x")
    assert resp.status_code == 400


async def test_files_kept_are_limited_and_old_ones_go(relay):
    for i in range(puller.MAX_FILES):
        assert (await relay.put(f"/api/pullers/api-prod/producer/files/{i}.zip", headers=API,
                                content=b"x")).status_code == 200
    resp = await relay.put("/api/pullers/api-prod/producer/files/more.zip", headers=API, content=b"x")
    assert resp.status_code == 429
    files = relay.app.state.pullers.folder / "api-prod" / "files"
    old = time.time() - 25 * 3600
    os.utime(files / "0.zip", (old, old))
    (files / "cut.zip.part").write_bytes(b"x")
    os.utime(files / "cut.zip.part", (old, old))
    relay.app.state.pullers.clean()
    assert sorted(p.name for p in files.iterdir()) == ["1.zip", "2.zip", "3.zip", "4.zip"]


# --- the Pullers page ---------------------------------------------------------

async def test_the_admin_defines_the_pullers(relay):
    page = await relay.get("/pullers", headers={**as_user("admin"), "Accept": "text/html"})
    assert page.status_code == 200 and "api-prod" in page.text
    assert (await relay.get("/pullers", headers=as_user("viewer"))).status_code == 403
    save = lambda body: relay.post("/api/pullers/save", headers=as_user("admin", post=True), json=body)
    resp = await save({"name": "p2", "producer": "other", "consumers": ["dev", "ghost"], "new": True})
    assert resp.status_code == 400 and "ghost" in resp.json()["error"]
    resp = await save({"name": "api-prod", "producer": "other", "consumers": ["dev"], "new": True})
    assert "already exists" in resp.json()["error"]
    assert (await save({"name": "p2", "producer": "other", "consumers": ["dev"], "new": True,
                        "keep_hours": 2})).json() == {"ok": True}
    settings = relay.app.state.settings
    assert load_pullers(settings.pullers_file)["p2"].keep_hours == 2
    assert (await relay.get("/api/pullers", headers=OTHER)).json()["pullers"] == [{"name": "p2", "part": "producer"}]
    # deleted with its state
    await relay.put("/api/pullers/p2/producer/files/a.zip", headers=OTHER, content=b"x")
    folder = relay.app.state.pullers.folder / "p2"
    assert folder.is_dir()
    resp = await relay.post("/api/pullers/delete", headers=as_user("admin", post=True), json={"name": "p2"})
    assert resp.json() == {"ok": True} and not folder.exists() and "p2" not in settings.pullers
    assert (await relay.get("/api/pullers/p2", headers=OTHER)).status_code == 404


async def test_a_puller_deleted_frees_its_waiting_producer(relay):
    poll = asyncio.ensure_future(relay.post("/api/pullers/api-prod/producer?wait=10", headers=API, json={}))
    await asyncio.sleep(0.2)
    await relay.post("/api/pullers/delete", headers=as_user("admin", post=True), json={"name": "api-prod"})
    resp = await asyncio.wait_for(poll, 5)
    assert resp.status_code == 404


# --- three OmniaCLI: the relay, the producer and the consumer -----------------

class Internet(httpx.AsyncBaseTransport):
    """relay.test is the relay's app; nothing else is reachable."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request):
        if request.url.host != HOST:
            raise httpx.ConnectError("unreachable", request=request)
        return await self.asgi.handle_async_request(request)


def end_settings(tmp_path, name, token):
    """An OmniaCLI (the producer's or the consumer's) whose link vps goes to the relay."""
    conf = tmp_path / name
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {"vps": {"url": f"https://{HOST}", "token": token}}}))
    return parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / f"{name}-backups"),
                           "jobs_dir": str(tmp_path / f"{name}-jobs"), "servers_file": "servers.yaml"}, base_dir=conf)


async def test_three_instances_through_the_relay_with_a_key(tmp_path):
    relay_app = create_app(relay_settings(tmp_path, PULLERS))
    internet = Internet(relay_app)
    api_settings, dev_settings = end_settings(tmp_path, "api", API_TOKEN), end_settings(tmp_path, "dev", DEV_TOKEN)
    # the two ends are OmniaCLI too: their links reach the relay through the "internet" only
    create_app(api_settings, link_transport=internet), create_app(dev_settings, link_transport=internet)
    api_clients, dev_clients = LinkClients(internet), LinkClients(internet)
    api_link, dev_link = api_settings.link_targets["vps"], dev_settings.link_targets["vps"]
    api_http, dev_http = api_clients.client(api_link), dev_clients.client(dev_link)
    api_headers, dev_headers = link_headers(api_link, "puller", "user"), link_headers(dev_link, "anna", "admin")
    key = parse_key(new_key())
    api_seal, dev_seal = Sealer(key, "api-prod"), Sealer(key, "api-prod")
    try:
        # the consumer asks a backup; the producer, waiting, gets it and opens it
        poll = asyncio.ensure_future(api_http.post("/api/pullers/api-prod/producer", params={"wait": 10},
                                                   headers=api_headers, json={}))
        await asyncio.sleep(0.2)
        cmd = Command("backup", "dev: anna", {"no_filestore": True})
        resp = await dev_http.post("/api/pullers/api-prod/commands", headers=dev_headers,
                                   json={"command": dev_seal.seal(COMMAND, cmd.to_dict())})
        assert resp.status_code == 200, resp.text
        taken = (await poll).json()["commands"]
        assert Command.from_dict(api_seal.open(COMMAND, taken[0]["envelope"])) == cmd
        # the relay keeps what it cannot read
        stored = (relay_app.state.pullers.folder / "api-prod" / "state.json")
        assert "no_filestore" not in stored.read_text()
        # the producer encrypts and uploads its backup, and reports it
        plain = tmp_path / "db.zip"
        plain.write_bytes(os.urandom(2 * puller.CHUNK + 11))
        sealed = tmp_path / "db.zip.enc"
        encrypt_file(key, "api-prod", plain, sealed)
        with open(sealed, "rb") as fh:
            resp = await api_http.put("/api/pullers/api-prod/producer/files/db.zip.enc", headers=api_headers,
                                      content=fh.read())
        assert resp.status_code == 200, resp.text
        report = {"job": cmd.id, "status": "success", "file": "db.zip.enc"}
        resp = await api_http.post("/api/pullers/api-prod/producer", params={"wait": 0}, headers=api_headers,
                                   json={"report": api_seal.seal(REPORT, report)})
        assert resp.json()["seq"] == 1
        assert "db.zip.enc" not in (relay_app.state.pullers.folder / "api-prod" / "reports.jsonl").read_text()
        # the consumer reads the report, downloads, decrypts, and the file goes from the relay
        data = (await dev_http.get("/api/pullers/api-prod/reports", params={"since": 0},
                                   headers=dev_headers)).json()
        assert dev_seal.open(REPORT, data["reports"][0]["envelope"]) == report
        got = tmp_path / "got.enc"
        got.write_bytes((await dev_http.get("/api/pullers/api-prod/files/db.zip.enc", headers=dev_headers)).content)
        decrypt_file(key, "api-prod", got, tmp_path / "got.zip")
        assert (tmp_path / "got.zip").read_bytes() == plain.read_bytes()
        assert (await dev_http.delete("/api/pullers/api-prod/files/db.zip.enc", headers=dev_headers)).status_code == 200
        assert (await dev_http.get("/api/pullers/api-prod", headers=dev_headers)).json()["files"] == []
        # a report the relay made up is refused by the consumer
        fake = {"v": 1, "enc": None, "p": {"status": "success", "file": "evil.zip"}}
        relay_app.state.pullers.state("api-prod").add_report(fake, time.time())
        data = (await dev_http.get("/api/pullers/api-prod/reports", params={"since": 1},
                                   headers=dev_headers)).json()
        with pytest.raises(Exception, match="in clear"):
            dev_seal.open(REPORT, data["reports"][0]["envelope"])
    finally:
        await api_clients.close()
        await dev_clients.close()


async def test_a_consumer_user_reads_the_puller_through_the_link_but_cannot_delete(tmp_path):
    relay_app = create_app(relay_settings(tmp_path, PULLERS))
    dev_settings = end_settings(tmp_path, "dev", DEV_TOKEN)
    dev_app = create_app(dev_settings, link_transport=Internet(relay_app))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(dev_app)),
                                 base_url="http://dev.test") as client:
        headers = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
        resp = await client.get("/links/vps/api/pullers/api-prod", headers=headers)
        assert resp.status_code == 200 and resp.json()["consumers"][0]["link"] == "dev"
        # the proxy of a link passes GET, HEAD and POST only
        resp = await client.delete("/links/vps/api/pullers/api-prod/files/a.zip", headers=headers)
        assert resp.status_code == 405
