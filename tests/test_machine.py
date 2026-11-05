# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The kind of machine: on a server nobody opens a shell of OmniaCLI's own
user, whatever the role, through the page, the WebSocket or a link."""

import logging

import pytest
import yaml
from starlette.testclient import TestClient

from omniacli.app import create_app
from omniacli.config import ConfigError, parse_machine, parse_settings

from .conftest import LINK_TOKEN, USER_HASH
from .test_shell import browser, closed, customer_server  # noqa: F401 (customer_server: a fixture)

REFUSED = "This machine is a server: no {} as OmniaCLI's own user"


@pytest.mark.parametrize("line, service_user, kind", [
    (None, None, "server"),
    (None, "", "server"),
    (None, "omniacli", "server"),
    (None, "devuser", "development"),
    ("server", "devuser", "server"),
    ("development", None, "development"),
])
def test_kind_of_machine(line, service_user, kind):
    assert parse_machine(line, service_user) == kind


@pytest.mark.parametrize("line", ["Server", "", "prod", True, 1])
def test_a_wrong_line_stops_the_start(line):
    with pytest.raises(ConfigError, match="machine:"):
        parse_machine(line)


def _main(tmp_path, **more):
    return parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "b"),
                           "jobs_dir": str(tmp_path / "j"), "servers_file": "servers.yaml", **more},
                          base_dir=tmp_path)


def test_the_main_configuration_decides(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIACLI_USER", raising=False)
    assert _main(tmp_path).machine == "server"
    assert _main(tmp_path, machine="development").machine == "development"
    monkeypatch.setenv("OMNIACLI_USER", "devuser")
    assert _main(tmp_path).machine == "development"
    assert _main(tmp_path, machine="server").machine == "server"
    monkeypatch.setenv("OMNIACLI_USER", "omniacli")
    assert _main(tmp_path).machine == "server"


@pytest.fixture
def server(env):
    env["settings"].machine = "server"
    return env["settings"]


@pytest.mark.parametrize("user", ["dev", "admin"])
def test_a_server_opens_no_shell_of_its_own_user(server, tmp_path, user):
    folder = tmp_path / "claude-here"
    folder.mkdir()
    server.servers["srv"].instances["test"].claude_dir = folder
    app = create_app(server)
    with TestClient(app) as c:
        page = c.get("/servers/srv?view=test", headers=browser(app, user)).text
        # the instance is shown, without the buttons
        assert "/servers/srv/test/restart" in page
        for kind in ("shell", "claude", "psql"):
            assert f'/test/{kind}"' not in page
            resp = c.get(f"/servers/srv/test/{kind}", headers=browser(app, user))
            assert resp.status_code == 403 and REFUSED.format(kind) in resp.text
            with c.websocket_connect(f"/servers/srv/test/{kind}/ws", headers=browser(app, user)) as ws:
                assert closed(ws) == (1008, REFUSED.format(kind))
    assert not [e for e in app.state.access_log.recent() if e["event"] == "shell"]


def test_a_server_keeps_psql_as_another_user(server, monkeypatch):
    instance = server.servers["srv"].instances["test"]
    instance.options.odoo_config.write_text("[options]\ndb_user = odoo\n")
    instance.run_as = "odoo"
    plugin = type(instance.plugin)
    assert instance.plugin.db_shell_as_service(instance) is False
    # not the real sudo: a command that says it ran
    monkeypatch.setattr(plugin, "db_shell", lambda self, inst: ('echo "PSQL $PGDATABASE"', {"PGDATABASE": "DB_test"}))
    app = create_app(server)
    with TestClient(app) as c:
        page = c.get("/servers/srv?view=test", headers=browser(app, "dev")).text
        assert 'href="/servers/srv/test/psql"' in page
        assert '/test/shell"' not in page and '/test/claude"' not in page
        assert c.get("/servers/srv/test/psql", headers=browser(app, "dev")).status_code == 200
        assert c.get("/servers/srv/test/shell", headers=browser(app, "dev")).status_code == 403
        out = ""
        with c.websocket_connect("/servers/srv/test/psql/ws", headers=browser(app, "dev")) as ws:
            while True:
                message = ws.receive()
                if message["type"] == "websocket.close":
                    break
                out += (message.get("bytes") or b"").decode("utf-8", "replace")
        assert "PSQL DB_test" in out
        # the other instance has no run_as: closed
        assert c.get("/servers/srv/production/psql", headers=browser(app, "dev")).status_code == 403


def test_who_runs_the_database_client():
    from omniacli.docker import DockerPlugin
    from omniacli.plugin_api import ServicePlugin

    assert DockerPlugin.db_shell_as_service(None, None) is False
    # a plugin that says nothing counts as OmniaCLI's own user: closed on a server
    assert ServicePlugin.db_shell_as_service(None, None) is True


def test_through_a_link_the_server_decides(env, tmp_path, customer_server):  # noqa: F811
    customer, url = customer_server
    env["settings"].machine = "server"
    conf = tmp_path / "mine"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {"cliente": {"url": url, "token": LINK_TOKEN}}}))
    # my own machine is free, the one the shell would run on is not
    mine = create_app(_main_at(conf, tmp_path))
    with TestClient(mine) as c:
        headers = browser(mine, "admin")
        page = c.get("/links/cliente/servers/srv?view=test", headers=headers).text
        assert "/test/restart" in page and '/test/shell"' not in page
        assert c.get("/links/cliente/servers/srv/test/shell", headers=headers).status_code == 403
        with c.websocket_connect("/links/cliente/servers/srv/test/shell/ws", headers=headers) as ws:
            code, reason = closed(ws)
            assert code in (1008, 1011) and "This machine is a server" in reason, (code, reason)
    assert not [e for e in customer.state.access_log.recent() if e["event"] == "shell"]


def _main_at(conf, tmp_path):
    return parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "my-backups"),
                           "jobs_dir": str(tmp_path / "my-jobs"), "servers_file": "servers.yaml",
                           "machine": "development"}, base_dir=conf)


@pytest.mark.parametrize("kind", ["server", "development"])
def test_the_kind_is_shown_and_logged(env, kind, caplog):
    env["settings"].machine = kind
    app = create_app(env["settings"])
    with caplog.at_level(logging.WARNING, logger="omniacli.app"):
        with TestClient(app) as c:
            page = c.get("/options", headers=browser(app, "admin")).text
    assert f'id="machine">{kind}<' in page
    assert f"this machine is a {kind}" in caplog.text
