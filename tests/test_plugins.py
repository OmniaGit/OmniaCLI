# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import ast
import base64
from pathlib import Path

import pytest
import yaml

from omniacli import ops, plugins
from omniacli.config import ConfigError, parse_servers, server_to_dict
from omniacli.jobs import Job
from omniacli.plugin_api import PLUGIN_API, ServicePlugin

from .conftest import PASSWORD
from .test_ops import head, push_change

AUTH = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()


class Dummy(ServicePlugin):
    """A type with nothing but the common commands: no backup, no process."""

    name = "dummy"
    label = "Dummy"
    api_version = PLUGIN_API

    def __init__(self):
        self.pulled = []

    def parse_options(self, data, where, restart_type):
        return {"flavour": str(data.get("flavour") or "plain")}

    def options_to_dict(self, options):
        return dict(options)

    async def checks(self, instance):
        return []

    async def after_pull(self, job, instance):
        self.pulled.append(instance.key)
        job.log("dummy after pull")


class FakeEntryPoint:
    def __init__(self, name, obj, value="pkg:Plugin"):
        self.name, self.value, self.obj, self.dist = name, value, obj, None

    def load(self):
        if isinstance(self.obj, Exception):
            raise self.obj
        return self.obj


@pytest.fixture
def with_dummy():
    """The built-in plugins and Dummy, as if a package declared it."""
    plugins.set_registry(plugins.load(entry_points=[FakeEntryPoint("dummy", Dummy)]))
    yield plugins.registry().get("dummy")
    plugins.set_registry(None)


@pytest.fixture
def without_plugins():
    plugins.set_registry(plugins.load(builtin=(), entry_points=[]))
    yield
    plugins.set_registry(None)


def job():
    return Job(id="t", title="t", instances=[], source="test")


def test_the_odoo_plugin_is_built_in():
    found = plugins.load(entry_points=[])
    assert [(i.name, i.source, i.error) for i in found.found] == [("odoo", "built-in", None)]
    assert found.get("odoo").label == "Odoo"


def _error(cls, name="x"):
    return plugins.load(builtin=(), entry_points=[FakeEntryPoint(name, cls)]).found[0].error


def test_the_contract_is_checked():
    class Half(ServicePlugin):
        name, label, api_version = "half", "Half", PLUGIN_API

        def parse_options(self, data, where, restart_type):
            return None

    assert "does not implement after_pull, checks, options_to_dict" in _error(Half, "half")
    assert "is not a subclass" in _error(object)

    class Old(Dummy):
        name, api_version = "old", 0

    assert "written for plugin API 0" in _error(Old, "old")

    class Sync(Dummy):
        name = "sync"

        def checks(self, instance):
            return []

    assert "checks must be async def" in _error(Sync, "sync")

    class Bad(Dummy):
        name = "Bad-Name"

    assert "is not lower case" in _error(Bad, "Bad-Name")
    assert "the entry point is other but the plugin's name is dummy" in _error(Dummy, "other")

    class Broken(Dummy):
        def self_test(self):
            return ["no click-odoo here"]

    assert _error(Broken, "dummy") == "no click-odoo here"
    assert _error(ImportError("No module named pkg"), "dummy") == \
        "cannot import: ImportError: No module named pkg"


def test_a_type_is_served_once():
    found = plugins.load(entry_points=[FakeEntryPoint("odoo", type("Odoo2", (Dummy,), {"name": "odoo"})),
                                       FakeEntryPoint("dummy", Dummy)])
    assert [i.error for i in found.found] == [None, "another plugin already serves the type odoo", None]
    assert sorted(found.plugins) == ["dummy", "odoo"]


def test_a_server_without_its_plugin_is_kept(without_plugins, tmp_path):
    written = {"status": "active", "production": {"database": "DB", "anything": [1, 2]}}
    servers = parse_servers({"s": written}, tmp_path)
    srv = servers["s"]
    assert srv.missing_plugin and not srv.active and srv.instances == {}
    assert srv.type == "odoo" and srv.error == "no plugin odoo is installed"
    # saved again as it was: live again once the plugin is back
    assert server_to_dict(srv) == {"status": "active", "type": "odoo", **{"production": written["production"]}}


def test_a_disabled_plugin_says_why(tmp_path):
    class Broken(Dummy):
        def self_test(self):
            return ["broken here"]

    plugins.set_registry(plugins.load(entry_points=[FakeEntryPoint("dummy", Broken)]))
    try:
        srv = parse_servers({"s": {"type": "dummy", "test": {}}}, tmp_path)["s"]
        assert srv.error == "the plugin dummy is disabled: broken here"
    finally:
        plugins.set_registry(None)


def test_process_needs_a_plugin_that_starts_it(with_dummy, tmp_path):
    data = {"type": "dummy", "test": {"restart": {"type": "process"},
                                      "repositories": [{"name": "r", "path": "/r", "branch": "b"}]}}
    with pytest.raises(ConfigError, match="Dummy cannot be started by OmniaCLI"):
        parse_servers({"s": data}, tmp_path)


def _dummy_server(env):
    """The fixture's server, as a server of type dummy."""
    data = yaml.safe_load(env["settings"].servers_file.read_text())["servers"]["srv"]
    data["type"] = "dummy"
    data["test"]["flavour"] = "spicy"
    return parse_servers({"d": data}, env["tmp"])["d"]


async def test_the_core_runs_a_type_through_its_plugin(env, with_dummy):
    srv = _dummy_server(env)
    assert srv.type == "dummy" and srv.test.options == {"flavour": "spicy"}
    assert server_to_dict(srv)["test"]["flavour"] == "spicy"
    assert "database" not in server_to_dict(srv)["test"]

    push_change(env, "odoo", "test", "a.txt")
    j = job()
    await ops.pull_restart(j, srv.test)
    assert with_dummy.pulled == ["d/test"] and "dummy after pull" in "\n".join(j.lines)
    assert "restart odoo-test" in env["calls"].read_text()

    # no backup: the merge goes on without one
    with pytest.raises(Exception, match="Dummy has no backup"):
        await ops.backup(job(), srv.production, env["settings"].backup_dir)
    j = job()
    await ops.merge_to_production(j, srv, env["settings"].backup_dir)
    assert "--- no backup: Dummy has none" in "\n".join(j.lines)
    assert with_dummy.pulled == ["d/test", "d/production"]
    assert head(srv.production.repository("odoo").path) == head(srv.test.repository("odoo").path)


def test_the_odoo_plugin_imports_only_the_plugin_api():
    """As a plugin of its own package would: from OmniaCLI only plugin_api."""
    folder = Path(__file__).parent.parent / "omniacli" / "odoo"
    for path in folder.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level >= 2:
                assert node.module == "plugin_api", f"{path.name} imports ..{node.module}"
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert not node.module.startswith("omniacli"), f"{path.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("omniacli") for a in node.names), path.name


async def test_plugins_page(env, with_dummy):
    import httpx

    from omniacli.app import create_app

    from .conftest import BasicAsSession

    env["settings"].servers["gone"] = parse_servers({"gone": {"type": "nginx", "test": {}}}, env["tmp"])["gone"]
    app = create_app(env["settings"])
    transport = httpx.ASGITransport(app=BasicAsSession(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://omniacli.test") as c:
        page = await c.get("/plugins", headers={"Authorization": AUTH})
        assert page.status_code == 200
        assert "built-in" in page.text and "Dummy" in page.text
        assert "Servers without their plugin" in page.text and "no plugin nginx is installed" in page.text
        dev = "Basic " + base64.b64encode(f"dev:{PASSWORD}".encode()).decode()
        assert (await c.get("/plugins", headers={"Authorization": dev})).status_code == 403
        # the form is Odoo's: a server it cannot edit is left as written
        edit = await c.get("/config/gone/edit", headers={"Authorization": AUTH})
        assert edit.status_code == 400 and "no plugin nginx is installed" in edit.text
        servers = (await c.get("/api/servers", headers={"Authorization": AUTH})).json()
        assert [(s["name"], s["type"]) for s in servers] == [("srv", "odoo")]
        assert servers[0]["instances"]["test"]["database"] == "DB_test"
