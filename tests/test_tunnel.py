# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tunnels: a local port that reaches an instance's web interface through a link."""

import hashlib
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn
import yaml
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omniacli import tunnel
from omniacli.app import create_app
from omniacli.auth import hash_token
from omniacli.config import ConfigError, parse_options, parse_settings

from .conftest import LINK_TOKEN, TOKEN, USER_HASH
from .test_shell import browser

BIG = bytes(range(256)) * 20480  # 5 MiB


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_odoo(name: str):
    """An HTTP server that answers who it is and what it was asked; /big
    gives 5 MiB, a POST its body's sha256."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: bytes):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(BIG if self.path == "/big" else f"{name} {self.path} host={self.headers['Host']}".encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self._send(f"{name} {hashlib.sha256(body).hexdigest()}".encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def serve(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture
def customer(env):
    """The customer's OmniaCLI: its test instance is an "Odoo" with workers,
    its web port and its evented port two fake servers."""
    web, evented = fake_odoo("WEB"), fake_odoo("EVENTED")
    settings = env["settings"]
    settings.servers["srv"].instances["test"].options.odoo_config.write_text(
        f"[options]\nhttp_port = {web.server_address[1]}\nworkers = 2\n"
        f"gevent_port = {evented.server_address[1]}\n")
    app = create_app(settings)
    server, thread, url = serve(app)
    yield app, url
    server.should_exit = True
    thread.join(10)
    web.shutdown()
    evented.shutdown()


def my_app(tmp_path, url, ports):
    conf = tmp_path / "mine"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {"cliente": {"url": url, "token": LINK_TOKEN}}}))
    (conf / "options.yaml").write_text(yaml.safe_dump({"tunnel": {"ports": ports}}))
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH, "viewer": {"password": USER_HASH, "role": "user"},
                           "other": {"password": USER_HASH, "role": "user"},
                           "dev": {"password": USER_HASH, "role": "developer"}},
                 "tokens": {"tester": hash_token(TOKEN)}},
        "backup_dir": str(tmp_path / "my-backups"), "jobs_dir": str(tmp_path / "my-jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)
    return create_app(settings)


def get(url: str, data: bytes | None = None) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as resp:
        return resp.read()


def test_request_path():
    assert tunnel.request_path(b"GET /web?debug=1 HTTP/1.1\r\nHost: x\r\n") == "/web"
    assert tunnel.request_path(b"GET /websocket HTTP/1.1\r\n") == "/websocket"
    assert tunnel.request_path(b"\x16\x03\x01 not http\r\n") == "/"


def test_odoo_tunnel_address(env):
    instance = env["settings"].servers["srv"].instances["test"]
    plugin, conf = instance.plugin, instance.options.odoo_config
    conf.write_text("[options]\nhttp_port = 8100\n")
    assert plugin.tunnel_address(instance, "/web") == ("127.0.0.1", 8100)
    # no workers: the bus is served on the web port
    assert plugin.tunnel_address(instance, "/websocket") == ("127.0.0.1", 8100)
    conf.write_text("[options]\nhttp_port = 8100\nworkers = 3\nhttp_interface = 10.0.0.5\n")
    assert plugin.tunnel_address(instance, "/websocket") == ("10.0.0.5", 8072)
    assert plugin.tunnel_address(instance, "/websocket/peek_notifications") == ("10.0.0.5", 8072)
    assert plugin.tunnel_address(instance, "/web/webclient") == ("10.0.0.5", 8100)
    # before Odoo 16: longpolling_port, /longpolling
    conf.write_text("[options]\nxmlrpc_port = 8100\nworkers = 3\nlongpolling_port = 8200\nhttp_interface = 0.0.0.0\n")
    assert plugin.tunnel_address(instance, "/longpolling/poll") == ("127.0.0.1", 8200)
    assert plugin.tunnel_address(instance, "/websocketx") == ("127.0.0.1", 8100)


def test_tunnel_options():
    options = parse_options({})
    assert (options.tunnel_idle_hours, options.tunnel_bind, options.tunnel_ports) == (4, "127.0.0.1", "18000-18099")
    options = parse_options({"tunnel": {"idle_hours": 8, "bind": "0.0.0.0", "ports": "19000"}})
    assert (options.tunnel_idle_hours, options.tunnel_bind, options.tunnel_ports) == (8, "0.0.0.0", "19000")
    for bad, words in (({"idle_hours": 0}, "1 to 720"), ({"bind": "localhost"}, "not an IP"),
                       ({"ports": "80-90"}, "1024 to 65535"), ({"ports": "19000-18000"}, "1024 to 65535"),
                       ({"ports": "a"}, "range")):
        with pytest.raises(ConfigError, match=words):
            parse_options({"tunnel": bad})


def test_tunnel_through_a_link(env, tmp_path, customer):
    customer_app, url = customer
    port = free_port()
    mine = my_app(tmp_path, url, str(port))
    with TestClient(mine) as c:
        page = c.get("/tunnels", headers=browser(mine, "viewer")).text
        assert 'data-server="srv"' in page and 'data-roles="production test"' in page
        # the menu names the page to developers and administrators only
        assert 'href="/tunnels"' not in page
        assert 'href="/tunnels"' in c.get("/tunnels", headers=browser(mine, "dev")).text
        # a user may open one (the link's role capped to user is enough there)
        resp = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                      headers=browser(mine, "viewer"))
        assert resp.status_code == 200, resp.text
        opened = resp.json()
        assert opened["port"] == port and opened["url"] == f"http://127.0.0.1:{port}/"
        # opened again: the same tunnel
        again = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                       headers=browser(mine, "admin")).json()
        assert again["id"] == opened["id"]
        base = opened["url"].rstrip("/")
        # the web port, the Host the browser used; the bus to the evented port
        assert get(base + "/web/login") == f"WEB /web/login host=127.0.0.1:{port}".encode()
        assert get(base + "/websocket") == f"EVENTED /websocket host=127.0.0.1:{port}".encode()
        # big answers and bodies, unchanged
        assert get(base + "/big") == BIG
        assert get(base + "/upload", data=BIG) == f"WEB {hashlib.sha256(BIG).hexdigest()}".encode()
        # several connections at once
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(get(f"{base}/page{i}"))) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results) == sorted(f"WEB /page{i} host=127.0.0.1:{port}".encode() for i in range(8))
        listed = c.get("/api/tunnels", headers=browser(mine, "other")).json()
        assert [t["id"] for t in listed] == [opened["id"]] and listed[0]["can_close"] is False
        assert "/tunnels" in c.get("/tunnels", headers=browser(mine, "other")).text
        # only who opened it, or an admin, closes it
        resp = c.post(f"/api/tunnels/{opened['id']}/close", headers=browser(mine, "other"))
        assert resp.status_code == 403
        resp = c.post(f"/api/tunnels/{opened['id']}/close", headers=browser(mine, "viewer"))
        assert resp.status_code == 200
        with pytest.raises(urllib.error.URLError):
            get(base + "/web")
        assert c.post(f"/api/tunnels/{opened['id']}/close", headers=browser(mine, "viewer")).status_code == 404
        # what cannot be reached is said when opening
        for body, words in (({"link": "cliente", "server": "nope", "role": "test"}, "No instance nope/test"),
                            ({"link": "nope", "server": "srv", "role": "test"}, "No link nope"),
                            ({"link": "cliente", "server": "srv", "role": "x"}, "production or test")):
            resp = c.post("/api/tunnels/open", json=body, headers=browser(mine, "admin"))
            assert resp.status_code == 400 and words in resp.json()["error"], resp.text
        # an MCP token does not open tunnels here
        resp = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                      headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code in (403, 405)
    # the customer logged who opened it, through which link (both apps of
    # this test write the same access log: told apart by kind)
    events = [e for e in customer_app.state.access_log.recent() if e["event"] == "tunnel"]
    there = [e for e in events if e["kind"] == "link"]
    assert there and there[0]["name"] == "omnia" and there[0]["detail"] == "srv/test opened, acting for viewer"
    here = [e["detail"] for e in events if e["kind"] == "user" and e["name"] == "viewer"]
    assert any("closed: closed by viewer" in d for d in here) and any("opened on 127.0.0.1:" in d for d in here)


def test_tunnel_closes_when_idle_and_at_stop(env, tmp_path, customer):
    _, url = customer
    port = free_port()
    mine = my_app(tmp_path, url, str(port))
    with TestClient(mine) as c:
        opened = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                        headers=browser(mine, "admin")).json()
        manager = mine.state.tunnels
        found = manager.tunnels[opened["id"]]
        get(opened["url"] + "web")
        assert time.time() - found.last_active < 5
        # traffic within the hours: kept; past them: closed
        c.portal.call(manager.close_idle)
        assert opened["id"] in manager.tunnels
        found.last_active -= 4 * 3600
        c.portal.call(manager.close_idle)
        assert manager.tunnels == {}
        with pytest.raises(urllib.error.URLError):
            get(opened["url"] + "web")
        # the port is free again; OmniaCLI stopping closes what is open
        reopened = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                          headers=browser(mine, "admin")).json()
        assert reopened["port"] == port
    assert mine.state.tunnels.tunnels == {}
    with pytest.raises(urllib.error.URLError):
        get(reopened["url"] + "web")
    details = [e["detail"] for e in mine.state.access_log.recent() if e["event"] == "tunnel"]
    assert any("no traffic for 4 h" in d for d in details) and any("OmniaCLI stopped" in d for d in details)


def test_tunnel_refusals(env, tmp_path, customer):
    customer_app, url = customer
    # the far side: no MCP token, no draft or unknown instance
    with TestClient(customer_app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/servers/srv/test/tunnel/ws?probe=1") as ws:
                ws.receive()
        with pytest.raises(WebSocketDisconnect) as refused:
            with c.websocket_connect("/servers/srv/test/tunnel/ws?probe=1",
                                     headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                ws.receive()
        assert refused.value.code == 1008
    # no free port in the range: said
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    mine = my_app(tmp_path, url, str(busy.getsockname()[1]))
    with TestClient(mine) as c:
        resp = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                      headers=browser(mine, "admin"))
        assert resp.status_code == 400 and "no free port" in resp.json()["error"]
    busy.close()
    # an OmniaCLI without tunnels (here: any web server answering 404)
    old = fake_odoo("OLD")
    mine = my_app(tmp_path / "old", f"http://127.0.0.1:{old.server_address[1]}", str(free_port()))
    with TestClient(mine) as c:
        resp = c.post("/api/tunnels/open", json={"link": "cliente", "server": "srv", "role": "test"},
                      headers=browser(mine, "admin"))
        assert resp.status_code == 400, resp.text
    old.shutdown()
