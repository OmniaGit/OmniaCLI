# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workspaces: a customer's users see the servers of their workspaces only,
with their role there, and nothing of the machine."""
import base64
import json
import re

import pytest
import yaml
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omniacli.app import WORKSPACE_ROUTES, create_app
from omniacli.auth import hash_password
from omniacli.config import (
    Access,
    ConfigError,
    Workspace,
    load_workspaces,
    parse_auth,
    parse_storage_roots,
    parse_workspaces,
    save_workspaces,
    server_to_dict,
)
from omniacli.sessions import COOKIE

from .conftest import PASSWORD
from .test_app import AUTH, HOST, POST, client, wait_job  # noqa: F401 - the client fixture

ORIGIN = f"http://{HOST}"


def basic(user: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()}


def post_as(user: str) -> dict:
    return {**basic(user), "Origin": ORIGIN}


@pytest.fixture
def workspaces(env):
    """acme holds srv (mario its admin, luca a user there); beta holds
    nothing (eva a developer there); zoe is in no workspace."""
    settings = env["settings"]
    password = hash_password(PASSWORD, iterations=1000)
    names = ("mario", "luca", "eva", "zoe")
    settings.set_web_access(Access(users={n: password for n in names}, roles={n: "workspace" for n in names}))
    settings.workspaces = {
        "acme": Workspace("acme", "ACME", "acme.example.com", servers=["srv"],
                          members={"mario": "admin", "luca": "user"}),
        "beta": Workspace("beta", members={"eva": "developer"}),
    }
    return settings


def test_parse_workspaces(tmp_path):
    found = parse_workspaces({"acme": {"label": "ACME", "domain": "ACME.example.com", "storage": "/srv/omniacli",
                                       "limits": {"disk_gb": 100, "cpus": 2.5, "instances": 3},
                                       "servers": ["srv", "srv"], "members": {"mario": "admin"}}},
                             ("/srv/omniacli",))
    acme = found["acme"]
    assert (acme.domain, acme.servers, acme.limits) == ("acme.example.com", ["srv"],
                                                        {"disk_gb": 100, "cpus": 2.5, "instances": 3})
    save_workspaces(tmp_path / "w.yaml", found)
    assert load_workspaces(tmp_path / "w.yaml", ("/srv/omniacli",)) == found
    assert load_workspaces(tmp_path / "none.yaml") == {}
    for bad, message in (
            ({"Acme": {}}, "lower case"), ({"-a": {}}, "lower case"),
            ({"a": {"domain": "not a domain"}}, "not a domain name"),
            ({"a": {"storage": "/elsewhere"}}, "not one of the storage roots"),
            ({"a": {"limits": {"disk_gb": 0}}}, "positive"), ({"a": {"limits": {"instances": 1.5}}}, "whole"),
            ({"a": {"limits": {"ram": 1}}}, "not one of"), ({"a": {"servers": ["../x"]}}, "not a server name"),
            ({"a": {"members": {"mario": "workspace"}}}, "role must be one of"),
            ({"a": {"members": {"ma rio": "user"}}}, "not a user name"),
            ({"a": {"servers": ["srv"]}, "b": {"servers": ["srv"]}}, "in two workspaces"),
            ({"a": {"domain": "x.example.com"}, "b": {"domain": "x.example.com"}}, "in two workspaces")):
        with pytest.raises(ConfigError, match=message):
            parse_workspaces(bad, ("/srv/omniacli",))
    assert parse_storage_roots({"storage": ["/srv/omniacli/", "/data"]}) == ("/srv/omniacli", "/data")
    with pytest.raises(ConfigError, match="absolute"):
        parse_storage_roots({"storage": ["srv"]})
    # a user of workspaces only; never a link or a token
    access = parse_auth({"users": {"mario": {"password": "x", "role": "workspace"}}}, need_user=False)
    assert access.roles == {"mario": "workspace"}
    with pytest.raises(ConfigError, match="role must be one of"):
        parse_auth({"links": {"x": {"token": "sha256$" + "0" * 64, "role": "workspace"}}}, need_user=False)


async def test_a_workspace_user_sees_its_servers_only(workspaces, client, env):
    # mario, admin of acme: srv, nothing of the machine
    page = (await client.get("/", headers=basic("mario"))).text
    assert "/servers/srv" in page and "This machine" not in page
    assert "/config" not in page and "/users" not in page and 'href="/workspaces/acme"' in page
    page = (await client.get("/servers/srv", headers=basic("mario"))).text
    assert "/servers/srv/test/restart" in page and "/test/shell" not in page and "/test/psql" not in page
    assert [s["name"] for s in (await client.get("/api/servers", headers=basic("mario"))).json()] == ["srv"]
    metrics = (await client.get("/api/metrics", headers=basic("mario"))).json()
    assert metrics["machine"] == [] and set(metrics["instances"]) == {"srv/production", "srv/test"}
    # eva, of beta: no srv anywhere
    assert "/servers/srv" not in (await client.get("/", headers=basic("eva"))).text
    assert (await client.get("/api/servers", headers=basic("eva"))).json() == []
    assert (await client.get("/api/metrics", headers=basic("eva"))).json()["instances"] == {}
    for url in ("/servers/srv", "/servers/srv/test/log", "/api/servers/srv/test/log",
                "/api/servers/srv/test/backups", "/x/odoo/servers/srv/test/addons"):
        resp = await client.get(url, headers=basic("eva"))
        assert resp.status_code == 403 and "workspaces" in resp.json()["detail"], url
    assert (await client.post("/servers/srv/test/restart", headers=post_as("eva"))).status_code == 403
    # zoe, in no workspace: an empty Dashboard
    assert (await client.get("/api/servers", headers=basic("zoe"))).json() == []
    # luca, a user in acme: restarts, does not pull
    assert (await client.post("/servers/srv/test/pull", headers=post_as("luca"))).status_code == 403
    resp = await client.post("/servers/srv/test/restart", headers=post_as("luca"), follow_redirects=False)
    assert resp.status_code == 303
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    await wait_job(client, job_id)
    # the job: seen in acme, not in beta
    assert job_id in [j["id"] for j in (await client.get("/api/jobs", headers=basic("mario"))).json()["jobs"]]
    assert (await client.get("/api/jobs", headers=basic("eva"))).json()["jobs"] == []
    assert job_id not in (await client.get("/jobs", headers=basic("eva"))).text
    assert (await client.get(f"/jobs/{job_id}", headers=basic("eva"))).status_code == 403
    assert (await client.get(f"/api/jobs/{job_id}", headers=basic("mario"))).status_code == 200
    # the machine's users see everything, as before
    assert (await client.get("/config", headers={"Authorization": AUTH})).status_code == 200
    assert job_id in [j["id"] for j in (await client.get("/api/jobs", headers={"Authorization": AUTH})).json()["jobs"]]


def _sample(path: str) -> str:
    values = {"server": "srv", "role": "test", "plugin": "odoo", "workspace": "acme"}
    return re.sub(r"\{(\w+)(:path)?\}", lambda m: values.get(m.group(1), "x"), path)


async def test_every_other_route_is_closed(workspaces, client, env):
    """A route not listed for the users of workspaces refuses them, mario
    being an admin there: a route added later is closed until listed."""
    checked = 0
    for route in client.app.routes:
        if not isinstance(route, APIRoute) or route.path in WORKSPACE_ROUTES:
            continue
        if route.path.startswith("/x/") and "{server}" in route.path and "{role}" in route.path:
            continue
        url = _sample(route.path)
        if "GET" in route.methods:
            resp = await client.get(url, headers=basic("mario"))
        else:
            method = sorted(route.methods)[0]
            resp = await client.request(method, url, headers=post_as("mario"), json={})
        assert resp.status_code == 403, (route.path, resp.status_code)
        assert resp.json()["detail"] == "Not open to the users of workspaces", route.path
        checked += 1
    assert checked > 60


def test_websockets_closed(env, workspaces):
    app = create_app(env["settings"])
    sockets = [r.path for r in app.routes if isinstance(r, APIWebSocketRoute)]
    assert sockets and not set(sockets) & WORKSPACE_ROUTES
    with TestClient(app) as c:
        cookie = app.state.sessions.create("mario", env["settings"].users["mario"])
        for path in sockets:
            with pytest.raises(WebSocketDisconnect) as refused:
                with c.websocket_connect(_sample(path), headers={"cookie": f"{COOKIE}={cookie}",
                                                                  "origin": ORIGIN}) as ws:
                    ws.receive()
            assert refused.value.code == 1008, path


async def test_workspace_admin_manages_its_users(workspaces, client, env):
    settings = env["settings"]
    url = "/api/workspaces/acme/members/"
    # a new user: of workspaces only, in acme
    resp = await client.post(url + "save", headers=post_as("mario"),
                             json={"name": "nina", "role": "developer", "password": "nina-pass-1", "new": True})
    assert resp.status_code == 200, resp.text
    assert settings.user_role("nina") == "workspace" and settings.workspaces["acme"].members["nina"] == "developer"
    assert yaml.safe_load(settings.users_file.read_text())["users"]["nina"]["role"] == "workspace"
    assert load_workspaces(settings.workspaces_file)["acme"].members["nina"] == "developer"
    nina = {"Authorization": "Basic " + base64.b64encode(b"nina:nina-pass-1").decode()}
    assert [s["name"] for s in (await client.get("/api/servers", headers=nina)).json()] == ["srv"]
    # its role and password; never its own role
    assert (await client.post(url + "save", headers=post_as("mario"),
                              json={"name": "nina", "role": "user", "password": "nina-pass-2"})).status_code == 200
    assert settings.workspaces["acme"].members["nina"] == "user"
    assert (await client.get("/api/servers", headers=nina)).status_code == 401
    resp = await client.post(url + "save", headers=post_as("mario"), json={"name": "mario", "role": "user"})
    assert resp.status_code == 400 and "own role" in resp.json()["error"]
    # not a user of acme; a name taken; a short password; luca is no admin there
    resp = await client.post(url + "save", headers=post_as("mario"), json={"name": "eva", "role": "admin"})
    assert resp.status_code == 404
    resp = await client.post(url + "save", headers=post_as("mario"),
                             json={"name": "eva", "role": "user", "password": "long-enough", "new": True})
    assert resp.status_code == 400 and "already exists" in resp.json()["error"]
    resp = await client.post(url + "save", headers=post_as("mario"),
                             json={"name": "ugo", "role": "user", "password": "short", "new": True})
    assert resp.status_code == 400 and "at least" in resp.json()["error"]
    assert (await client.post(url + "save", headers=post_as("luca"),
                              json={"name": "ugo", "role": "user", "password": "long-enough", "new": True})
            ).status_code == 403
    assert (await client.get("/workspaces/acme", headers=basic("luca"))).status_code == 403
    assert (await client.get("/workspaces/beta", headers=basic("mario"))).status_code == 403
    assert "nina" in (await client.get("/workspaces/acme", headers=basic("mario"))).text
    # a user of two workspaces: its password is not acme's to change, and removing it keeps it
    settings.workspaces["beta"].members["luca"] = "user"
    resp = await client.post(url + "save", headers=post_as("mario"),
                             json={"name": "luca", "role": "user", "password": "long-enough"})
    assert resp.status_code == 400 and "other workspaces" in resp.json()["error"]
    resp = await client.post(url + "delete", headers=post_as("mario"), json={"name": "luca"})
    assert resp.json() == {"ok": True, "deleted": False} and "luca" in settings.users
    # a user of acme only is deleted with it; never oneself
    resp = await client.post(url + "delete", headers=post_as("mario"), json={"name": "nina"})
    assert resp.json() == {"ok": True, "deleted": True} and "nina" not in settings.users
    resp = await client.post(url + "delete", headers=post_as("mario"), json={"name": "mario"})
    assert resp.status_code == 400
    # the machine's admin manages them too
    assert (await client.get("/workspaces/beta", headers={"Authorization": AUTH})).status_code == 200


async def test_workspaces_page_and_api(workspaces, client, env):
    settings = env["settings"]
    assert "acme.example.com" in (await client.get("/workspaces", headers={"Authorization": AUTH})).text
    assert (await client.get("/workspaces", headers=basic("mario"))).status_code == 403
    resp = await client.post("/api/workspaces/save", headers=POST,
                             json={"name": "gamma", "new": True, "label": "Gamma", "servers": ["srv"]})
    assert resp.status_code == 400 and "in two workspaces" in resp.json()["error"]
    for body, message in (({"servers": ["nope"]}, "No server nope"), ({"members": {"ghost": "user"}}, "No user ghost"),
                          ({"domain": "acme.example.com"}, "in two workspaces")):
        resp = await client.post("/api/workspaces/save", headers=POST, json={"name": "gamma", "new": True, **body})
        assert resp.status_code == 400 and message in resp.json()["error"], body
    resp = await client.post("/api/workspaces/save", headers=POST,
                             json={"name": "gamma", "new": True, "members": {"zoe": "developer"},
                                   "limits": {"disk_gb": "50"}})
    assert resp.status_code == 200, resp.text
    assert load_workspaces(settings.workspaces_file)["gamma"].limits == {"disk_gb": 50}
    resp = await client.post("/api/workspaces/save", headers=POST, json={"name": "gamma", "new": True})
    assert resp.status_code == 400 and "already exists" in resp.json()["error"]
    assert (await client.post("/api/workspaces/delete", headers=POST, json={"name": "gamma"})).json() == {"ok": True}
    assert "gamma" not in settings.workspaces
    assert (await client.post("/api/workspaces/save", headers=post_as("mario"), json={"name": "x"})).status_code == 403


async def test_cpu_and_memory_applied_when_changed(workspaces, client, env, monkeypatch):
    """The machine's admins change the CPU or memory of a workspace with
    instances: a job writes its slice again; other changes do not."""
    from omniacli import workspaceops

    settings = env["settings"]
    resp = await client.post("/api/servers/save", headers=POST, json={
        "original": None, "name": "acme-main", "server": server_to_dict(settings.servers["srv"])})
    assert resp.status_code == 200, resp.text
    applied = []

    async def apply_limits(job, ws):
        applied.append(dict(ws.limits))
        return {}
    monkeypatch.setattr(workspaceops, "apply_limits", apply_limits)
    body = {"name": "acme", "label": "ACME", "domain": "acme.example.com", "servers": ["srv", "acme-main"],
            "members": {"mario": "admin", "luca": "user"}}
    resp = await client.post("/api/workspaces/save", headers=POST,
                             json={**body, "limits": {"cpus": "2", "memory_gb": "4"}})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and applied == [{"cpus": 2, "memory_gb": 4}]
    resp = await client.post("/api/workspaces/save", headers=POST,
                             json={**body, "label": "Acme", "limits": {"cpus": "2", "memory_gb": "4"}})
    assert resp.json() == {"ok": True} and len(applied) == 1
    resp = await client.post("/api/workspaces/save", headers=POST, json={**body, "limits": {"cpus": "2"}})
    await wait_job(client, resp.json()["job"])
    assert applied[-1] == {"cpus": 2}


async def test_servers_keep_or_leave_their_workspace(workspaces, client, env):
    settings = env["settings"]
    server = server_to_dict(settings.servers["srv"])
    # renamed: still in acme
    resp = await client.post("/api/servers/save", headers=POST, json={"original": "srv", "name": "first", "server": server})
    assert resp.status_code == 200, resp.text
    assert settings.workspaces["acme"].servers == ["first"]
    # a server saved again under its name stays where it is
    resp = await client.post("/api/servers/save", headers=POST, json={"original": "first", "name": "first", "server": server})
    assert resp.status_code == 200 and settings.workspaces["acme"].servers == ["first"]
    # deleted: out of acme, so a new server of that name is in no workspace
    assert (await client.post("/api/servers/delete", headers=POST, json={"name": "first"})).status_code == 200
    assert settings.workspaces["acme"].servers == []
    # a leftover name in a workspace is not inherited by a new server
    settings.workspaces["beta"].servers.append("ghost")
    resp = await client.post("/api/servers/save", headers=POST, json={"original": None, "name": "ghost", "server": server})
    assert resp.status_code == 200 and settings.workspaces["beta"].servers == []
    assert load_workspaces(settings.workspaces_file)["beta"].servers == []


async def test_account_page_names_the_workspaces(workspaces, client, env):
    page = (await client.get("/account", headers=basic("mario"))).text
    assert "<strong>workspace</strong>" in page and "<strong>acme</strong> (admin)" in page
    assert "<strong>admin</strong>" in (await client.get("/account", headers={"Authorization": AUTH})).text


async def test_create_and_remove_an_instance(workspaces, client, env, monkeypatch, tmp_path):
    from pathlib import Path

    from omniacli import stacktemplates, workspaceops
    from omniacli.jobs import JobFailed

    settings = env["settings"]
    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odooplm-20")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    asked = []

    async def create_instance(job, ws, instance, tpl, values):
        asked.append((ws.name, instance, tpl.name, values))
        if instance == "broken":
            raise JobFailed("the helper failed")
        folder = tmp_path / "storage" / ws.name / instance
        return {"type": "docker", **{role: {
            "database": instance, "odoo_service": "odoo", "db_service": "db", "http_port": port,
            "update_modules": True, "restart": {"type": "compose", "service": str(folder / role), "sudo": True},
            "repositories": []} for role, port in (("production", 20000), ("test", 20002))}}
    removed = []

    async def remove_instance(job, ws, instance):
        removed.append((ws.name, instance))
    published = []

    async def publish(job, ws):
        published.append(list(ws.servers))
        return {}
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "remove_instance", remove_instance)
    monkeypatch.setattr(workspaceops, "publish", publish)
    url = "/api/workspaces/acme/instances/"
    # the workspace needs its storage and disk limit
    resp = await client.post(url + "create", headers=post_as("mario"), json={"instance": "main", "template": "odooplm-20"})
    assert resp.status_code == 400 and "no storage" in resp.json()["error"]
    settings.workspaces["acme"].storage = str(tmp_path / "storage")
    settings.workspaces["acme"].limits = {"disk_gb": 10, "instances": 2}
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "OdooPLM 20" in page and 'id="create-instance"' in page
    for body, message in (({"instance": "Main", "template": "odooplm-20"}, "lower case"),
                          ({"instance": "main", "template": "nope"}, "No template"),
                          ({"instance": "main", "template": "odooplm-20", "values": {"IMAGE": "evil"}}, "not one of")):
        resp = await client.post(url + "create", headers=post_as("mario"), json=body)
        assert resp.status_code == 400 and message in resp.json()["error"], body
    # a user of acme who is no admin there, a user of beta: refused
    assert (await client.post(url + "create", headers=post_as("luca"), json={})).status_code == 403
    assert (await client.post(url + "create", headers=post_as("eva"), json={})).status_code == 403
    resp = await client.post(url + "create", headers=post_as("mario"),
                             json={"instance": "main", "template": "odooplm-20", "values": {"INIT_LANG": "it_IT"}})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert asked[-1] == ("acme", "main", "odooplm-20", {"IMAGE": "ghcr.io/omniagit/odooplm:20.0",
                                                        "INIT_MODULES": "plm", "INIT_LANG": "it_IT"})
    assert settings.servers["acme-main"].active and settings.servers["acme-main"].type == "docker"
    assert settings.workspaces["acme"].servers == ["srv", "acme-main"]
    # acme has a domain: its names published with the new instance
    assert published == [["srv", "acme-main"]]
    assert "acme-main" in [s["name"] for s in (await client.get("/api/servers", headers=basic("luca"))).json()]
    assert (await client.get("/servers/acme-main", headers=basic("eva"))).status_code == 403
    # its job was seen in acme while it ran
    assert job["id"] in [j["id"] for j in (await client.get("/api/jobs", headers=basic("luca"))).json()["jobs"]]
    # the limit of instances: srv and acme-main
    resp = await client.post(url + "create", headers=post_as("mario"), json={"instance": "two", "template": "odooplm-20"})
    assert resp.status_code == 400 and "instances already" in resp.json()["error"]
    # a creation that fails leaves no server and nothing in the workspace
    settings.workspaces["acme"].limits["instances"] = 5
    resp = await client.post(url + "create", headers=post_as("mario"), json={"instance": "broken", "template": "odooplm-20"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and "acme-broken" not in settings.servers
    assert settings.workspaces["acme"].servers == ["srv", "acme-main"]
    # remove: only an instance of the workspace (srv was not made by it)
    resp = await client.post(url + "delete", headers=post_as("mario"), json={"server": "srv"})
    assert resp.status_code == 404
    resp = await client.post(url + "delete", headers=post_as("mario"), json={"server": "acme-main"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and removed == [("acme", "main")]
    assert "acme-main" not in settings.servers and settings.workspaces["acme"].servers == ["srv"]
    assert published[-1] == ["srv"]


async def test_workspaceops_create_instance(monkeypatch):
    """The job's steps: the disk, the projects, each database and start;
    the projects removed when a step fails."""
    from pathlib import Path

    from omniacli import stacktemplates, workspaceops
    from omniacli.jobs import Job, JobFailed

    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odooplm-20")
    ws = Workspace("acme", storage="/srv/omniacli", limits={"disk_gb": 20, "cpus": 2})
    made = {"database": "main", "odoo_service": "odoo", "db_service": "db",
            "roles": {"production": {"folder": "/srv/omniacli/acme/main/production", "http_port": 20000},
                      "test": {"folder": "/srv/omniacli/acme/main/test", "http_port": 20002}}}
    captured, ran = [], []

    async def capture(cmd, **kw):
        captured.append(cmd)
        return 0, json.dumps(made if cmd[4] == "create" else {"unit": "x"})

    async def run(job, cmd, check=True, **kw):
        ran.append(cmd)
        if "init" in cmd and fail_init:
            raise JobFailed("init failed")
        return 0
    monkeypatch.setattr(workspaceops, "capture", capture)
    monkeypatch.setattr(workspaceops, "run", run)
    fail_init = False
    values = {"IMAGE": "img", "INIT_MODULES": "plm", "INIT_LANG": "it_IT"}
    data = await workspaceops.create_instance(Job(id="j", title="t", instances=[], source="s"), ws, "main", template, values)
    assert [c[4:7] for c in captured] == [["disk", "acme", "/srv/omniacli"], ["limits", "acme", "2"],
                                          ["create", "acme", "/srv/omniacli"]]
    assert captured[0][-1] == "20" and captured[1][-1] == "0" and json.loads(captured[2][-1]) == values
    assert ran[0][-6:] == ["/srv/omniacli/acme/main/production", "init", "odoo", "main", "plm", "it_IT"]
    assert ran[1][-2:] == ["/srv/omniacli/acme/main/production", "start"] and ran[3][-1] == "start"
    assert data["production"]["restart"] == {"type": "compose", "service": "/srv/omniacli/acme/main/production", "sudo": True}
    assert data["test"]["http_port"] == 20002
    ran.clear()
    fail_init = True
    with pytest.raises(JobFailed):
        await workspaceops.create_instance(Job(id="k", title="t", instances=[], source="s"), ws, "main", template, values)
    assert ran[-1][-4:] == ["remove", "acme", "/srv/omniacli", "main"]
    with pytest.raises(JobFailed, match="no storage"):
        await workspaceops.create_instance(Job(id="l", title="t", instances=[], source="s"),
                                           Workspace("beta"), "main", template, values)
