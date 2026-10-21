# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Links: this instance ("mine") operating a customer's instance (the one of
conftest's env, where the link omnia is a developer), in the same process."""

import asyncio
import base64

import httpx
import pytest
import yaml

from omniacli.app import create_app
from omniacli.config import parse_settings

from .conftest import LINK_TOKEN, BasicAsSession, PASSWORD, TOKEN, USER_HASH, hash_token

HOST = "mine.test"


def basic(user):
    return "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()


class Network(httpx.AsyncBaseTransport):
    """customer.test is the customer's app; down.test refuses connections."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        if request.url.host == "down.test":
            raise httpx.ConnectError("connection refused", request=request)
        return await self.asgi.handle_async_request(request)


@pytest.fixture
async def mine(env, tmp_path):
    customer = create_app(env["settings"])
    conf = tmp_path / "mine"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "cliente": {"url": "http://customer.test", "token": LINK_TOKEN},
        "down": {"url": "https://down.test", "token": "whatever"},
    }}))
    settings = parse_settings({
        "auth": {
            "users": {"admin": USER_HASH, "dev": {"password": USER_HASH, "role": "developer"},
                      "viewer": {"password": USER_HASH, "role": "user"}},
            "tokens": {"tester": hash_token(TOKEN)},
        },
        "backup_dir": str(tmp_path / "my-backups"), "jobs_dir": str(tmp_path / "my-jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)
    network = Network(customer)
    app = create_app(settings, link_transport=network)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)), base_url=f"http://{HOST}") as c:
        c.app, c.customer, c.network = app, customer, network
        yield c


def as_user(user, post=False):
    headers = {"Authorization": basic(user), "Accept": "text/html"}
    if post:
        headers["Origin"] = f"http://{HOST}"
    return headers


async def wait_remote_job(client, job_id, user="admin"):
    for _ in range(200):
        resp = await client.get(f"/links/cliente/api/jobs/{job_id}", headers=as_user(user))
        data = resp.json()
        if data["status"] in ("success", "failed", "interrupted"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_proxy_asks_the_encoding_the_browser_accepts(mine):
    # the body is passed on as it comes: the other instance (or the nginx in
    # front of it) must not compress it for a client that cannot read it
    await mine.get("/links/cliente/", headers={**as_user("admin"), "Accept-Encoding": "gzip"})
    assert mine.network.requests[-1].headers["accept-encoding"] == "gzip"
    del mine.headers["accept-encoding"]
    await mine.get("/links/cliente/", headers=as_user("admin"))
    assert mine.network.requests[-1].headers["accept-encoding"] == "identity"


async def test_servers_page_lists_the_links_servers(mine):
    # lazy: the page comes without asking the links, its tables follow
    page = (await mine.get("/", headers=as_user("admin"))).text
    assert "cliente › srv" not in page and 'data-lazy="/dashboard/servers?q="' in page
    assert "Loading the servers of the linked instances" in page
    page = (await mine.get("/dashboard/servers", headers=as_user("admin"))).text
    assert "cliente › srv" in page and 'href="/links/cliente/servers/srv?view=both"' in page
    assert 'data-link="cliente" data-key="srv/test"' in page
    assert "Link <strong>down</strong>: cannot connect" in page
    # the filter knows the link, the database and the branch of a remote server
    for q in ("cliente", "DB_test", "addons test"):
        assert f'data-lazy="/dashboard/servers?q={q.replace(" ", "%20")}"' in (
            await mine.get(f"/?q={q}", headers=as_user("admin"))).text
        page = (await mine.get(f"/dashboard/servers?q={q}", headers=as_user("admin"))).text
        assert "cliente › srv" in page and "1 of 1 server." in page, q
    page = (await mine.get("/dashboard/servers?q=nothing", headers=as_user("admin"))).text
    assert "No servers match." in page and "down" not in page
    # tokens do not see the links
    page = (await mine.get("/", headers={"Authorization": f"Bearer {TOKEN}"})).text
    assert "cliente" not in page and "data-lazy" not in page
    assert "cliente" not in (await mine.get("/dashboard/servers", headers={"Authorization": f"Bearer {TOKEN}"})).text


async def test_proxied_pages_and_actions(mine):
    resp = await mine.get("/links/cliente", headers=as_user("admin"))
    assert resp.status_code == 307 and resp.headers["location"] == "/links/cliente/"
    page = (await mine.get("/links/cliente/servers/srv?view=both", headers=as_user("admin"))).text
    assert "Linked instance <strong>cliente</strong>" in page
    assert 'action="/links/cliente/servers/srv/test/pull"' in page
    # the link is a developer there: no merge even for an admin here
    assert "Merge test to production" not in page

    resp = await mine.post("/links/cliente/servers/srv/test/pull", headers=as_user("dev", post=True))
    assert resp.status_code == 303 and resp.headers["location"].startswith("/links/cliente/jobs/")
    data = await wait_remote_job(mine, resp.headers["location"].rsplit("/", 1)[1])
    assert data["status"] == "success" and data["source"] == "manual: link omnia (dev)"
    job = (await mine.get(resp.headers["location"], headers=as_user("dev"))).text
    assert "Linked instance" in job

    # without the Origin of this instance, this instance refuses the POST
    assert (await mine.post("/links/cliente/servers/srv/test/pull", headers=as_user("dev"))).status_code == 403


async def test_role_here_caps_the_link(mine):
    page = (await mine.get("/links/cliente/servers/srv?view=both", headers=as_user("viewer"))).text
    assert "DB_test" in page and "/pull" not in page
    resp = await mine.post("/links/cliente/servers/srv/test/pull", headers=as_user("viewer", post=True))
    assert resp.status_code == 403
    info = (await mine.get("/links/cliente/api/info", headers=as_user("viewer"))).json()
    assert info["role"] == "user" and info["caller"] == "link omnia (viewer)"


async def test_backup_download_streams_through(mine, env):
    folder = env["settings"].backup_dir / "srv" / "test"
    folder.mkdir(parents=True)
    (folder / "DB_test_1.zip").write_bytes(b"PK zip data" * 1000)
    resp = await mine.get("/links/cliente/backups/srv/test/DB_test_1.zip", headers=as_user("dev"))
    assert resp.status_code == 200 and resp.content == b"PK zip data" * 1000
    assert "DB_test_1.zip" in resp.headers["content-disposition"]
    files = (await mine.get("/links/cliente/api/servers/srv/test/backups", headers=as_user("dev"))).json()
    assert files[0]["download"] == "/links/cliente/backups/srv/test/DB_test_1.zip"
    assert (await mine.get("/links/cliente/backups/srv/test/DB_test_1.zip",
                           headers=as_user("viewer"))).status_code == 403


async def test_link_failures(mine):
    resp = await mine.get("/links/down/", headers=as_user("admin"))
    assert resp.status_code == 502 and "did not answer: cannot connect" in resp.text
    resp = await mine.get("/links/down/api/metrics", headers={"Authorization": basic("admin")})
    assert resp.status_code == 502 and resp.json()["error"].startswith("link down: cannot connect")
    assert (await mine.get("/links/nope/", headers=as_user("admin"))).status_code == 404
    # only a user who logs in here: not a token
    assert (await mine.get("/links/cliente/", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 403


async def test_links_page(mine):
    post = lambda url, body: mine.post(url, json=body, headers=as_user("admin", post=True))
    settings = mine.app.state.settings
    page = (await mine.get("/links", headers=as_user("admin"))).text
    assert "<strong><a href=\"/links/cliente/\">cliente</a></strong>" in page and LINK_TOKEN not in page
    assert "the token travels in clear" in page  # cliente is http
    assert (await mine.get("/links", headers=as_user("dev"))).status_code == 403

    data = (await post("/api/links/cliente/test", {})).json()
    assert data["ok"] and data["role"] == "developer" and data["caller"] == "link omnia (admin)"
    assert (await post("/api/links/down/test", {})).json() == {"ok": False, "error": "cannot connect (connection refused)"}

    body = {"name": "bad", "url": "http://customer.test", "token": "wrong", "new": True}
    assert (await post("/api/links/save", body)).json() == {"ok": True}
    assert (await post("/api/links/bad/test", {})).json()["error"] == "the other instance refused the token"
    resp = await mine.get("/links/bad/", headers=as_user("admin"))
    assert resp.status_code == 502 and "refused the token" in resp.text
    assert "already exists" in (await post("/api/links/save", body)).json()["error"]
    # a blank token keeps the saved one
    assert (await post("/api/links/save", {"name": "bad", "url": "http://customer.test/", "token": ""})).json()["ok"]
    assert settings.link_targets["bad"].token == "wrong" and settings.link_targets["bad"].url == "http://customer.test"
    text = settings.links_file.read_text()
    assert "wrong" in text and settings.links_file.stat().st_mode & 0o777 == 0o600
    for bad, error in (({"url": "ftp://x"}, "url must be"), ({"name": "a b"}, "a name is"),
                       ({"ca_file": "/nonexistent.pem"}, "is not a file")):
        body = {"name": "new1", "url": "https://x.example", "token": "t", "new": True, **bad}
        assert error in (await post("/api/links/save", body)).json()["error"]
    assert (await post("/api/links/delete", {"name": "bad"})).json() == {"ok": True}
    assert "bad" not in settings.link_targets and "bad" not in settings.links_file.read_text()


async def test_groups_with_linked_servers(mine):
    headers = as_user("admin", post=True)
    page = (await mine.get("/groups", headers=headers)).text
    assert 'data-member="cliente/srv"' in page and 'data-member="cliente/*"' in page
    body = {"groups": [{"name": "Customers", "members": ["cliente/*"]}]}
    assert (await mine.post("/api/groups/save", json=body, headers=headers)).json() == {"ok": True}
    page = (await mine.get("/dashboard/servers", headers=headers)).text
    assert "<h2>Customers <span" in page and "cliente › srv" in page
    assert page.index("<h2>Customers") < page.index("cliente › srv")
    # the linked instance's own page has no groups of ours
    assert "Edit groups" not in (await mine.get("/links/cliente/", headers=headers)).text
