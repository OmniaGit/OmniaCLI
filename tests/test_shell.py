# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shell of an instance: a login shell on a pseudo-terminal joined to a
WebSocket, here and through a link."""

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
import yaml
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omniacli.app import create_app
from omniacli.config import load_options, parse_settings
from omniacli.sessions import COOKIE
from omniacli.shell import Shell

from .conftest import LINK_TOKEN, TOKEN, USER_HASH

ORIGIN = "http://testserver"


def browser(app, user, origin=ORIGIN):
    """The headers of a browser logged in as user."""
    state = app.state
    cookie = state.sessions.create(user, state.settings.users[user])
    headers = {"cookie": f"{COOKIE}={cookie}"}
    if origin:
        headers["origin"] = origin
    return headers


def run(ws, line, until, limit=10):
    """Type line, return the output up to the first one containing until."""
    ws.send_text(json.dumps({"type": "input", "data": line}))
    out, end = "", time.monotonic() + limit
    while until not in out:
        assert time.monotonic() < end, out
        out += ws.receive_bytes().decode("utf-8", "replace")
    return out


def closed(ws):
    """The close code and reason the server ends the WebSocket with."""
    for _ in range(1000):
        message = ws.receive()
        if message["type"] == "websocket.close":
            return message.get("code"), message.get("reason", "")
    raise AssertionError("not closed")


async def test_shell_runs_in_its_folder(tmp_path):
    shell = await Shell.start(tmp_path, cols=100, rows=30)
    shell.write(b"echo SIZE=$(stty size) DIR=$(pwd); exit\n")
    out = b""
    while True:
        data = await asyncio.wait_for(shell.read(), 10)
        if not data:
            break
        out += data
    await shell.close()
    assert f"SIZE=30 100 DIR={tmp_path}".encode() in out


async def test_close_hangs_up_the_session(tmp_path):
    shell = await Shell.start(tmp_path)
    shell.write(b"sleep 300 &\n")
    await asyncio.sleep(0.5)
    await shell.close()
    assert shell.process.returncode is not None


def test_shell_over_websocket(env):
    app = create_app(env["settings"])
    test = env["settings"].instance("srv", "test")
    with TestClient(app) as c:
        with c.websocket_connect("/servers/srv/test/shell/ws", headers=browser(app, "dev")) as ws:
            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            # the marker last: the typed line comes back too, with $((40+2)) unexpanded
            out = run(ws, "echo $(stty size) $(pwd) OUT=$((40+2))\n", "OUT=42")
            assert "40 120" in out and str(test.repositories[0].path) in out
            ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
            assert closed(ws) == (1000, "the shell ended")
    events = [e for e in app.state.access_log.recent() if e["event"] == "shell"]
    assert [e["detail"].split(" after")[0] for e in events][:2] == ["srv/test closed", "srv/test opened"]
    assert {e["name"] for e in events} == {"dev"}


def test_shell_refusals(env):
    app = create_app(env["settings"])
    with TestClient(app) as c:
        url = "/servers/srv/test/shell/ws"
        # no login, another site's page, an MCP token: refused at the handshake
        for headers in ({"origin": ORIGIN}, browser(app, "dev", origin="http://evil.test"),
                        browser(app, "dev", origin=None), {"authorization": f"Bearer {TOKEN}", "origin": ORIGIN}):
            with pytest.raises(WebSocketDisconnect) as refused:
                with c.websocket_connect(url, headers=headers) as ws:
                    ws.receive()
            assert refused.value.code == 1008, headers
        # a user without the developer role, an instance that is not there
        with c.websocket_connect(url, headers=browser(app, "viewer")) as ws:
            assert closed(ws) == (1008, "The developer role is required")
        with c.websocket_connect("/servers/srv/nope/shell/ws", headers=browser(app, "admin")) as ws:
            assert closed(ws) == (1008, "No instance srv/nope")


def test_shell_page_and_button(env):
    app = create_app(env["settings"])
    with TestClient(app) as c:
        page = c.get("/servers/srv", headers=browser(app, "dev")).text
        assert 'href="/servers/srv/test/shell" target="_blank"' in page
        assert "/shell" not in c.get("/servers/srv", headers=browser(app, "viewer")).text
        page = c.get("/servers/srv/test/shell", headers=browser(app, "dev"))
        assert page.status_code == 200 and 'src="/static/xterm.js"' in page.text
        assert "/servers/srv/test/shell/ws" in page.text
        assert c.get("/servers/srv/test/shell", headers=browser(app, "viewer")).status_code == 403
        js = c.get("/static/xterm.js", headers=browser(app, "viewer"))
        assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
        assert c.get("/static/xterm.js").status_code == 401
        assert c.get("/static/favicon.ico", headers=browser(app, "viewer")).status_code == 404


@pytest.fixture
def customer_server(env):
    """The customer's OmniaCLI (conftest's env, where the link omnia is a
    developer) served by uvicorn on a free port, as a link reaches it."""
    app = create_app(env["settings"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


def test_shell_through_a_link(env, tmp_path, customer_server):
    customer, url = customer_server
    conf = tmp_path / "mine"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {"cliente": {"url": url, "token": LINK_TOKEN}}}))
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH, "viewer": {"password": USER_HASH, "role": "user"}}},
        "backup_dir": str(tmp_path / "my-backups"), "jobs_dir": str(tmp_path / "my-jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)
    mine = create_app(settings)
    with TestClient(mine) as c:
        # the page there, its terminal from there, its WebSocket through here
        page = c.get("/links/cliente/servers/srv/test/shell", headers=browser(mine, "admin")).text
        assert "/links/cliente/servers/srv/test/shell/ws" in page and 'src="/links/cliente/static/xterm.js"' in page
        assert c.get("/links/cliente/static/xterm.js", headers=browser(mine, "admin")).status_code == 200
        with c.websocket_connect("/links/cliente/servers/srv/test/shell/ws", headers=browser(mine, "admin")) as ws:
            run(ws, "echo THERE=$((6*7))\n", "THERE=42")
            ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
            assert closed(ws) == (1000, "the shell ended")
        # our user's role caps the link's: a user cannot open it there
        with c.websocket_connect("/links/cliente/servers/srv/test/shell/ws", headers=browser(mine, "viewer")) as ws:
            assert closed(ws) == (1008, "The developer role is required")
        with c.websocket_connect("/links/nope/servers/srv/test/shell/ws", headers=browser(mine, "admin")) as ws:
            assert closed(ws) == (1008, "No link nope")
    for _ in range(100):
        events = [e for e in customer.state.access_log.recent() if e["event"] == "shell"]
        if any("closed" in e["detail"] for e in events):
            break
        time.sleep(0.05)
    assert events[-1]["kind"] == "link" and events[-1]["name"] == "omnia"
    assert events[-1]["detail"] == "srv/test opened, acting for admin"


async def test_shell_runs_a_command_then_stays(tmp_path):
    shell = await Shell.start(tmp_path, command="echo RAN-IN-$(pwd)")
    shell.write(b"echo STILL-HERE; exit\n")
    out = b""
    while True:
        data = await asyncio.wait_for(shell.read(), 10)
        if not data:
            break
        out += data
    await shell.close()
    assert f"RAN-IN-{tmp_path}".encode() in out and b"STILL-HERE" in out
    with pytest.raises(FileNotFoundError):
        await Shell.start(tmp_path / "nope", command="true")


def test_claude_in_its_folder(env, tmp_path, monkeypatch):
    from omniacli import shell as shell_module
    from omniacli.config import parse_servers, server_to_dict
    from omniacli.config import ConfigError

    # not the real claude: a command that says where it runs
    monkeypatch.setattr(shell_module, "CLAUDE_COMMAND", "echo CLAUDE-RAN-IN-$(pwd)")
    settings = env["settings"]
    folder = tmp_path / "claude-here"
    folder.mkdir()
    app = create_app(settings)
    with TestClient(app) as c:
        # no Claude folder: no button, no page, no shell
        assert ">Claude</a>" not in c.get("/servers/srv?view=test", headers=browser(app, "dev")).text
        assert c.get("/servers/srv/test/claude", headers=browser(app, "dev")).status_code == 404
        with c.websocket_connect("/servers/srv/test/claude/ws", headers=browser(app, "dev")) as ws:
            assert closed(ws) == (1008, "srv/test has no Claude folder")
        settings.servers["srv"].instances["test"].claude_dir = folder
        page = c.get("/servers/srv?view=test", headers=browser(app, "dev")).text
        assert 'href="/servers/srv/test/claude"' in page and ">Claude</a>" in page
        assert "runs <span class=\"mono\">echo CLAUDE-RAN" in c.get("/servers/srv/test/claude",
                                                                   headers=browser(app, "dev")).text
        assert c.get("/servers/srv/test/claude", headers=browser(app, "viewer")).status_code == 403
        with c.websocket_connect("/servers/srv/test/claude/ws", headers=browser(app, "dev")) as ws:
            out = run(ws, "echo AFTER=$((40+2))\n", "AFTER=42")
            assert f"CLAUDE-RAN-IN-{folder}" in out
    # the servers file keeps it; a relative path is refused
    data = server_to_dict(settings.servers["srv"])
    assert data["test"]["claude_dir"] == str(folder)
    assert parse_servers({"srv": data})["srv"].test.claude_dir == folder
    data["test"]["claude_dir"] = "relative/dir"
    with pytest.raises(ConfigError, match="claude_dir"):
        parse_servers({"srv": data})


def test_options_set_the_shell_text_size(env):
    """The Options page (admin) sets the text size every console (shell, log)
    opens with."""
    settings = env["settings"]
    app = create_app(settings)
    with TestClient(app) as c:
        page = c.get("/servers/srv/test/shell", headers=browser(app, "dev")).text
        assert 'data-console="shell" data-font="14"' in page
        assert 'class="console-expand"' in page and 'class="console-larger"' in page
        assert c.get("/options", headers=browser(app, "dev")).status_code == 403
        assert c.post("/api/options/save", json={"shell": {"font_size": 18}},
                      headers=browser(app, "dev")).status_code == 403
        page = c.get("/options", headers=browser(app, "admin"))
        assert page.status_code == 200 and 'value="14"' in page.text
        for bad in (7, 41, "18", 18.5, True):
            resp = c.post("/api/options/save", json={"shell": {"font_size": bad}}, headers=browser(app, "admin"))
            assert resp.status_code == 400 and "8 to 40" in resp.json()["error"], bad
        resp = c.post("/api/options/save", json={"shell": {"font_size": 18}}, headers=browser(app, "admin"))
        assert resp.status_code == 200, resp.text
        assert yaml.safe_load(settings.options_file.read_text())["shell"] == {"font_size": 18}
        assert 'data-font="18"' in c.get("/servers/srv/test/shell", headers=browser(app, "dev")).text
        # the logs too: the same widget
        log = c.get("/servers/srv/test/log", headers=browser(app, "dev")).text
        assert 'data-console="log" data-font="18"' in log and 'data-tag="ERROR"' in log
    # read back at the next start
    assert load_options(settings.options_file).shell_font_size == 18


def test_psql_on_the_instance_database(env, monkeypatch):
    """The psql page: psql with the Odoo config's connection, in the
    environment; the page ends with it."""
    settings = env["settings"]
    instance = settings.servers["srv"].instances["test"]
    instance.options.odoo_config.write_text(
        "[options]\ndb_host = dbhost\ndb_port = 5433\ndb_user = odoo\ndb_password = se cret\ndb_sslmode = False\n")
    plugin = instance.plugin
    command, found = plugin.db_shell(instance)
    assert command == "psql" and found == {
        "PGDATABASE": "DB_test", "PGAPPNAME": "omniacli psql", "PGHOST": "dbhost", "PGPORT": "5433",
        "PGUSER": "odoo", "PGPASSWORD": "se cret"}
    # unset options are left out (a local socket, the OS user)
    instance.options.odoo_config.write_text("[options]\ndb_host = False\ndb_user = odoo\n")
    assert plugin.db_shell(instance)[1] == {"PGDATABASE": "DB_test", "PGAPPNAME": "omniacli psql", "PGUSER": "odoo"}
    instance.options.odoo_config.write_text("[options]\ndb_password = se cret\ndb_user = odoo\n")
    # not the real psql: a command that shows what it got, then ends
    original = type(plugin).db_shell
    monkeypatch.setattr(type(plugin), "db_shell", lambda self, inst: (
        'echo "PSQL $PGDATABASE $PGUSER $PGPASSWORD"', original(self, inst)[1]))
    app = create_app(settings)
    with TestClient(app) as c:
        page = c.get("/servers/srv?view=test", headers=browser(app, "dev")).text
        assert 'href="/servers/srv/test/psql"' in page
        assert "/psql" not in c.get("/servers/srv?view=test", headers=browser(app, "viewer")).text
        page = c.get("/servers/srv/test/psql", headers=browser(app, "dev"))
        assert page.status_code == 200 and "and ends with it" in page.text
        assert c.get("/servers/srv/test/psql", headers=browser(app, "viewer")).status_code == 403
        with c.websocket_connect("/servers/srv/test/psql/ws", headers=browser(app, "dev")) as ws:
            out = ""
            while True:
                message = ws.receive()
                if message["type"] == "websocket.close":
                    break
                out += (message.get("bytes") or b"").decode("utf-8", "replace")
            assert "PSQL DB_test odoo se cret" in out
    # a plugin without a database shell: no button, no page
    monkeypatch.setattr(type(plugin), "db_shell", lambda self, inst: None)
    app = create_app(settings)
    with TestClient(app) as c:
        assert "/psql" not in c.get("/servers/srv?view=test", headers=browser(app, "dev")).text
        assert c.get("/servers/srv/test/psql", headers=browser(app, "dev")).status_code == 404
        with c.websocket_connect("/servers/srv/test/psql/ws", headers=browser(app, "dev")) as ws:
            assert closed(ws) == (1008, "srv/test has no database shell")
