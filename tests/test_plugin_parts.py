# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a plugin adds besides its type: fields of the server form, buttons
of the server page, routes, templates and static files, its part of the MCP
dev_setup; and the generic type."""
import ast
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import APIRouter, Depends, Request

from omniacli import plugins
from omniacli.app import create_app
from omniacli.config import parse_servers
from omniacli.plugin_api import Action, ActionRefused, Field, FormButton

from .conftest import BasicAsSession
from .test_app import AUTH, HOST, POST, basic, wait_job
from .test_plugins import Dummy, FakeEntryPoint, _error

HERE = Path(__file__).parent
TEMPLATES = None  # set by the fixture: the folder of the plugin's templates
STATIC = None


class Rich(Dummy):
    """Dummy with fields, actions, a router, templates and static files."""

    name = "rich"
    label = "Rich"

    def __init__(self):
        super().__init__()
        self.ran = []

    def templates_dir(self):
        return TEMPLATES

    def static_dir(self):
        return STATIC

    def fields(self):
        return [Field("flavour", "Flavour", required=True,
                      buttons=(FormButton("Taste", "form/taste", only_with_value=False),)),
                Field("port", "Port", "number", restart_types=("process",))]

    def actions(self):
        async def record(job, target, params):
            self.ran.append((getattr(target, "key", None) or target.name, params))
            job.log(f"ran with {params}")

        def check(target, params):
            if params["count"] > 3:
                raise ActionRefused("at most 3")
            return params

        return [
            Action("wave", "Wave", record, group="service", role="user", confirm="Wave?"),
            Action("brew", "Brew", record, group="kitchen",
                   params=(Field("count", "Count", "number", required=True, default=1),
                           Field("hot", "Hot", "checkbox"),
                           Field("cup", "Cup", "select", choices=(("s", "Small"), ("l", "Large")))),
                   prepare=check, title=lambda target, params: f"Brew {params['count']} on {target.key}"),
            Action("stir", "Stir", record, group="kitchen", dialog="stir_dialog.html"),
            Action("feast", "Feast", record, scope="server", role="admin"),
        ]

    def router(self, host):
        router = APIRouter()

        @router.get("/page/{server}/{role}")
        async def page(request: Request, server: str, role: str):
            inst = host.instance(server, role)
            return host.render(request, "page.html", {"inst": inst})

        @router.post("/form/taste", dependencies=[Depends(host.needs("admin"))])
        async def taste(request: Request):
            job = host.submit(request, "Taste", ["form/test"], lambda job: self._noop(job))
            return {"job": job.id, "fill": {"flavour": "sweet"}}

        return router

    async def _noop(self, job):
        job.log("tasted")

    async def dev_setup(self, instance, repositories):
        for repo in repositories:
            repo["tasted"] = True
        return {"flavour": instance.options["flavour"]}

    mcp_help = "Servers of type rich: they are tasty."


@pytest.fixture
def rich(tmp_path):
    global TEMPLATES, STATIC
    TEMPLATES, STATIC = tmp_path / "rich-templates", tmp_path / "rich-static"
    TEMPLATES.mkdir()
    STATIC.mkdir()
    (TEMPLATES / "stir_dialog.html").write_text("<dialog id=stir>stir for {{ prefix }}</dialog>")
    (TEMPLATES / "page.html").write_text(
        '{% extends "base.html" %}{% block content %}<h1>Rich page of {{ inst.key }}</h1>{% endblock %}')
    (STATIC / "rich.css").write_text("body { color: red }")
    (tmp_path / "secret.txt").write_text("not served")
    plugins.set_registry(plugins.load(entry_points=[FakeEntryPoint("rich", Rich)]))
    yield plugins.registry().get("rich")
    plugins.set_registry(None)


def _rich_server(env):
    """The fixture's server, as a server of type rich."""
    data = yaml.safe_load(env["settings"].servers_file.read_text())["servers"]["srv"]
    data["type"] = "rich"
    for role in ("production", "test"):
        data[role]["flavour"] = "spicy"
    return parse_servers({"d": data}, env["tmp"])["d"]


def _instance_dict(inst):
    from omniacli.config import instance_to_dict

    return instance_to_dict(inst)


@pytest.fixture
async def rich_client(env, rich):
    env["settings"].servers["d"] = _rich_server(env)
    app = create_app(env["settings"])
    transport = httpx.ASGITransport(app=BasicAsSession(app))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as c:
        c.app = app
        yield c


def test_the_parts_are_checked():
    class BadField(Dummy):
        name = "badfield"

        def fields(self):
            return [Field("restart", "Restart"), Field("x", "X", "colour"), Field("y", "Y"), Field("y", "Y")]

    error = _error(BadField, "badfield")
    assert "field restart is a key of the core" in error
    assert "field x: kind 'colour' is not one of" in error and "field y given twice" in error

    class BadActions(Dummy):
        name = "badactions"

        def actions(self):
            def sync(job, target, params):
                return None

            async def ok(job, target, params):
                return None

            return [Action("Bad Id", "B", ok), Action("sync", "S", sync), Action("r", "R", ok, role="boss"),
                    Action("s", "S", ok, scope="world"), Action("d", "D", ok, dialog="none.html")]

    error = _error(BadActions, "badactions")
    assert "action id 'Bad Id' is not unique" in error and "action sync: run must be async def" in error
    assert "action r: role 'boss'" in error and "action s: scope must be" in error
    assert "action d: no template none.html" in error


async def test_actions_on_the_server_page(rich_client, env, rich):
    c = rich_client
    page = (await c.get("/servers/d?view=both", headers={"Authorization": AUTH})).text
    # a plain button with its question, the core's dialog of params, the plugin's dialog
    assert 'action="/servers/d/test/x/rich/wave"' in page and "return confirm(\"Wave?\")" in page
    assert 'action="/servers/d/test/x/rich/brew"' in page and 'name="count"' in page and "Large" in page
    assert 'data-action="rich.stir" data-url="/servers/d/test/x/rich/stir" data-key="d/test"' in page
    assert page.count("<dialog id=stir>stir for </dialog>") == 1  # drawn once for both instances
    assert 'action="/servers/d/x/rich/feast"' in page
    # no backup for a type without one; its settings by its fields
    assert '/servers/d/test/backup"' not in page and "/servers/d/test/backup?download" not in page
    assert "<dt>Flavour</dt>" in page and "spicy" in page and "<dt>Port</dt>" not in page

    viewer = (await c.get("/servers/d?view=both", headers={"Authorization": basic("viewer")})).text
    assert "/x/rich/wave" in viewer and "/x/rich/brew" not in viewer and "/x/rich/feast" not in viewer

    # a form post goes to the job's page, or back to next
    resp = await c.post("/servers/d/test/x/rich/brew", headers=POST, data={"count": "2", "hot": "on", "cup": "l"})
    assert resp.status_code == 303
    job = await wait_job(c, resp.headers["location"].rsplit("/", 1)[1])
    assert job["status"] == "success" and job["title"] == "Brew 2 on d/test" and job["source"] == "manual: admin"
    assert rich.ran[-1] == ("d/test", {"count": 2, "hot": True, "cup": "l"})
    resp = await c.post("/servers/d/test/x/rich/wave", headers={"Authorization": basic("viewer"),
                                                                 "Origin": f"http://{HOST}"},
                        data={"next": "/servers/d"})
    assert resp.status_code == 303 and resp.headers["location"] == "/servers/d"
    # JSON from a dialog of the plugin's own: the job's id
    resp = await c.post("/servers/d/production/x/rich/stir", headers=POST, json={"spoon": "wood"})
    job = await wait_job(c, resp.json()["job"])
    assert job["title"] == "Stir d/production" and rich.ran[-1] == ("d/production", {"spoon": "wood"})
    # JSON without a body: no parameters, answered as a form
    resp = await c.post("/servers/d/x/rich/feast", headers={**POST, "Content-Type": "application/json"})
    assert resp.status_code == 303 and "/jobs/" in resp.headers["location"]
    resp = await c.post("/servers/d/x/rich/feast", headers=POST)
    job = await wait_job(c, resp.headers["location"].rsplit("/", 1)[1])
    assert job["instances"] == ["d/production", "d/test"] and rich.ran[-1] == ("d", {})

    # refused: bad params, a role too low, an action or a type that is not the server's
    for data, message in (({"count": "9"}, "at most 3"), ({"count": "x"}, "is not a whole number"),
                          ({"count": ""}, "a number is required"), ({"count": "1", "cup": "xl"}, "not one of")):
        resp = await c.post("/servers/d/test/x/rich/brew", headers=POST, data=data)
        assert resp.status_code == 400 and message in resp.json()["error"], data
    dev = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    assert (await c.post("/servers/d/x/rich/feast", headers=dev)).status_code == 403
    viewer = {"Authorization": basic("viewer"), "Origin": f"http://{HOST}"}
    assert (await c.post("/servers/d/test/x/rich/brew", headers=viewer, data={"count": "1"})).status_code == 403
    assert (await c.post("/servers/d/test/x/rich/nope", headers=POST)).status_code == 404
    assert (await c.post("/servers/srv/test/x/rich/wave", headers=POST)).status_code == 404
    assert (await c.post("/servers/d/test/x/odoo/update", headers=POST, json={"mode": "changed"})).status_code == 404


async def test_routes_templates_and_static(rich_client, env):
    c = rich_client
    page = await c.get("/x/rich/page/d/test", headers={"Authorization": AUTH})
    assert page.status_code == 200 and "Rich page of d/test" in page.text and "<html" in page.text
    # host.instance: a server of the plugin's type only
    assert (await c.get("/x/rich/page/srv/test", headers={"Authorization": AUTH})).status_code == 404
    css = await c.get("/x/rich/static/rich.css", headers={"Authorization": AUTH})
    assert css.status_code == 200 and "color: red" in css.text
    for path in ("../secret.txt", "%2e%2e/secret.txt", "nope.css"):
        assert (await c.get(f"/x/rich/static/{path}", headers={"Authorization": AUTH})).status_code == 404, path
    assert (await c.get("/x/odoo/static/x.css", headers={"Authorization": AUTH})).status_code == 404
    # the core's login, then the route's own role
    c.cookies.clear()
    assert (await c.get("/x/rich/static/rich.css")).status_code == 401
    assert (await c.get("/x/rich/page/d/test")).status_code == 401
    dev = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    assert (await c.post("/x/rich/form/taste", headers=dev, json={})).status_code == 403
    resp = await c.post("/x/rich/form/taste", headers=POST, json={})
    job = await wait_job(c, resp.json()["job"])
    assert job["source"] == "manual: admin" and job["lines"][-1].endswith(" tasted")


async def test_the_form_of_a_plugin(rich_client, env):
    c = rich_client
    page = await c.get("/config/d/edit", headers={"Authorization": AUTH})
    assert page.status_code == 200
    model = json.loads(page.text.split("const MODEL = ", 1)[1].split(";\n", 1)[0])
    assert model["server"]["type"] == "rich" and model["server"]["test"]["flavour"] == "spicy"
    assert sorted(model["types"]) == ["generic", "odoo", "rich"]
    flavour, port = model["types"]["rich"]["fields"]
    assert flavour["required"] and flavour["buttons"][0]["route"] == "form/taste"
    assert port["restart_types"] == ["process"] and not model["types"]["rich"]["has_process"]
    assert model["types"]["odoo"]["has_process"] and model["default_type"] == "odoo"

    # saved by the form as it sends it
    data = {"type": "rich", "test": {**model["server"]["test"], "flavour": "mild"}}
    resp = await c.post("/api/servers/save", headers=POST, json={"original": "d", "name": "d", "server": data})
    assert resp.status_code == 200, resp.text
    assert env["settings"].servers["d"].test.options == {"flavour": "mild"}


async def test_the_generic_type(rich_client, env):
    c = rich_client
    srv = env["settings"].servers["srv"]
    test = _instance_dict(srv.test)
    generic = {k: v for k, v in test.items() if k in ("repositories", "restart", "log_file")}
    resp = await c.post("/api/servers/save", headers=POST,
                        json={"original": None, "name": "g", "server": {"type": "generic", "test": generic}})
    assert resp.status_code == 200, resp.text
    saved = env["settings"].servers["g"]
    assert saved.type == "generic" and saved.error is None and saved.test.options == {}
    # live (Test & activate needs a real systemd unit)
    saved.status = "active"
    page = (await c.get("/servers/g", headers={"Authorization": AUTH})).text
    assert "/servers/g/test/pull" in page and '/servers/g/test/backup"' not in page and "Generic service" in page
    resp = await c.post("/servers/g/test/pull-restart", headers=POST)
    job = await wait_job(c, resp.headers["location"].rsplit("/", 1)[1])
    assert job["status"] == "success", job
    # no command of its own: OmniaCLI cannot start it
    bad = {"type": "generic", "test": {**generic, "restart": {"type": "process"}}}
    resp = await c.post("/api/servers/save", headers=POST, json={"original": None, "name": "g2", "server": bad})
    assert resp.status_code == 200 and env["settings"].servers["g2"].error


async def test_a_router_that_fails_keeps_the_plugin(env):
    class NoRoutes(Rich):
        def router(self, host):
            raise RuntimeError("no fastapi here")

    try:
        global TEMPLATES
        TEMPLATES = env["tmp"]
        (TEMPLATES / "stir_dialog.html").write_text("")
        plugins.set_registry(plugins.load(entry_points=[FakeEntryPoint("rich", NoRoutes)]))
        app = create_app(env["settings"])
        transport = httpx.ASGITransport(app=BasicAsSession(app))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as c:
            page = (await c.get("/plugins", headers={"Authorization": AUTH})).text
            assert "Its routes are not served: RuntimeError: no fastapi here" in page
            assert (await c.get("/", headers={"Authorization": AUTH})).status_code == 200
    finally:
        plugins.set_registry(None)


def test_the_core_does_not_import_the_odoo_plugin():
    """Odoo is reached only through the plugin contract (plugins.BUILTIN names it)."""
    for path in (HERE.parent / "omniacli").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                module = ("." * node.level) + (node.module or "")
                assert not module.startswith((".odoo", "omniacli.odoo")), f"{path.name} imports {module}"


async def test_mcp_dev_setup_of_a_plugin(env, rich):
    pytest.importorskip("mcp")
    from .test_mcp import call
    import asyncio

    env["settings"].servers["d"] = _rich_server(env)
    app = create_app(env["settings"])
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()
    task = asyncio.create_task(lifespan())
    await started.wait()
    try:
        transport = httpx.ASGITransport(app=BasicAsSession(app))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as c:
            setup = await call(c, "dev_setup", server_name="d")
            assert setup["type"] == "rich" and setup["flavour"] == "spicy"
            assert [r["tasted"] for r in setup["repositories"]] == [True, True]
            assert "has_odoo_bin" not in setup["repositories"][0] and "backups" in setup
            from .test_mcp import rpc

            init = await rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "t", "version": "1"}})
            text = init.json()["result"]["instructions"]
            assert "they are tasty" in text and "Servers of type odoo" in text
    finally:
        stop.set()
        await task
