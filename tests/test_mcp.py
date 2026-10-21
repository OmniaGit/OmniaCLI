import json

import pytest

pytest.importorskip("mcp")  # the SDK needs Python 3.10

from omniaci.mcp_server import _errors_only, _public_url  # noqa: E402

from .conftest import TOKEN, sh  # noqa: E402
from .test_app import AUTH, HOST, POST, client, wait_job  # noqa: E402,F401

BEARER = {"Authorization": f"Bearer {TOKEN}"}
HEADERS = {**BEARER, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def rpc(client, method, params=None, headers=HEADERS):
    resp = await client.post("/mcp", headers=headers, content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
    return resp


async def call(client, tool, **arguments):
    resp = await rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    if "structuredContent" in result:
        content = result["structuredContent"]
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(result["content"][0]["text"])


async def test_token_required(client):
    assert (await rpc(client, "tools/list", headers={**HEADERS, "Authorization": AUTH})).status_code == 401
    assert (await rpc(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer nope"})).status_code == 401
    resp = await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "1"}})
    assert resp.status_code == 200 and resp.json()["result"]["serverInfo"]["name"] == "OmniaCI"
    tools = {t["name"] for t in (await rpc(client, "tools/list")).json()["result"]["tools"]}
    assert tools == {"list_servers", "server_status", "pending_updates", "read_log", "list_jobs",
                     "get_job", "update_test", "backup_test", "dev_setup"}
    # the token reads pages but never acts on them
    assert (await client.get("/jobs", headers=BEARER)).status_code == 200
    assert (await client.post("/servers/srv/test/pull", headers={**BEARER, "Origin": f"http://{HOST}"})).status_code == 403


async def test_monitor_and_update_test(client, env):
    servers = await call(client, "list_servers")
    assert servers[0]["name"] == "srv" and servers[0]["instances"]["test"]["database"] == "DB_test"
    status = await call(client, "server_status", server_name="srv")
    assert status["test"]["repositories"]["odoo"]["branch"] == "test" and not status["test"]["job_running"]
    log = await call(client, "read_log", server_name="srv", role="test", lines=3)
    assert log["lines"] == ["line 7", "line 8", "line 9"]

    job = await call(client, "update_test", server_name="srv", wait_seconds=30)
    assert job["status"] == "success" and job["source"] == "mcp: tester", job
    assert "restart odoo-test" in env["calls"].read_text()
    assert "restart odoo-production" not in env["calls"].read_text()
    assert (await call(client, "get_job", job_id=job["id"], tail=5))["id"] == job["id"]
    assert [j["id"] for j in await call(client, "list_jobs", server_name="srv", query="tester")] == [job["id"]]
    # production has no action, an unknown server is an error
    assert "No active server nope" in (await call(client, "update_test", server_name="nope"))["error"]


async def test_dev_setup_and_backup(client, env):
    tmp = env["tmp"]
    test = env["settings"].instance("srv", "test")
    odoo = test.repository("odoo").path
    (odoo / "sub").mkdir()
    test.odoo_config.write_text(
        f"[options]\naddons_path = {odoo}/sub,{test.repository('addons').path}, {tmp}/elsewhere\n"
        "admin_passwd = SECRET\ndb_password = SECRET\ndb_host = db.local\nworkers = 4\n")
    sh("git", "remote", "set-url", "origin", "https://user:SECRET@git.example.com/odoo.git", cwd=odoo)
    setup = await call(client, "dev_setup", server_name="srv")
    assert "SECRET" not in json.dumps(setup)
    repo = setup["repositories"][0]
    assert repo["url"] == "https://git.example.com/odoo.git" and repo["branch"] == "test"
    assert repo["commit"] == sh("git", "rev-parse", "HEAD", cwd=odoo)
    assert setup["addons_path"][:2] == [{"repository": "odoo", "subpath": "sub"},
                                        {"repository": "addons", "subpath": "."}]
    assert "outside_repositories" in setup["addons_path"][2]
    assert setup["odoo_conf_options"] == {"workers": "4"}

    job = await call(client, "backup_test", server_name="srv", wait_seconds=30)
    assert job["status"] == "success", job
    resp = await client.get(job["result"]["download"], headers=BEARER)
    assert resp.status_code == 200 and resp.content == b"dump\n"
    setup = await call(client, "dev_setup", server_name="srv")
    assert setup["backups"][0]["download"] == job["result"]["download"]


def test_helpers():
    assert _public_url("https://u:p@host:8443/r.git") == "https://host:8443/r.git"
    assert _public_url("git@github.com:org/r.git") == "git@github.com:org/r.git"
    lines = ["2026-10-21 05:00:00,1 9 INFO db x: ok", "2026-10-21 05:00:01,1 9 ERROR db x: bad",
             "Traceback (most recent call last):", "2026-10-21 05:00:02,1 9 INFO db x: ok again"]
    assert _errors_only(lines) == lines[1:3]
