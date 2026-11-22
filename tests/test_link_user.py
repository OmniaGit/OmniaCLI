# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An incoming link bound to a user of this machine: it acts with at most
that user's role, and on a server its Shell opens in that user's sandbox."""
import pytest
import yaml
from starlette.testclient import TestClient

from omniacli.app import create_app
from omniacli.config import Access, ConfigError, LinkAccess, load_access, parse_auth, save_access

from .conftest import USER_HASH
from .test_app import AUTH, access, client  # noqa: F401 (client is a fixture)
from .test_machine import sandbox, server  # noqa: F401 (fixtures)
from .test_shell import closed, run

TOKEN_HASH = "sha256$" + "0" * 64


def test_the_users_file_keeps_the_user_of_a_link(tmp_path):
    access_ = parse_auth({"users": {"anna": {"password": USER_HASH, "role": "developer"}},
                          "links": {"ops": {"token": TOKEN_HASH, "role": "admin", "user": "anna"},
                                    "old": {"token": TOKEN_HASH, "role": "user"}}})
    assert access_.links["ops"] == LinkAccess(TOKEN_HASH, "admin", "anna") and access_.links["old"].user == ""
    path = tmp_path / "users.yaml"
    save_access(path, access_)
    links = yaml.safe_load(path.read_text())["links"]
    assert links["ops"]["user"] == "anna" and "user" not in links["old"]
    assert load_access(path) == access_
    with pytest.raises(ConfigError, match="is not a user name"):
        parse_auth({"links": {"ops": {"token": TOKEN_HASH, "role": "user", "user": "a b"}}}, need_user=False)


async def test_a_link_acts_with_at_most_its_users_role(client):
    settings = client.app.state.settings
    assert (await access(client, "users/save", {"name": "anna", "role": "developer", "password": "anna-pass-1",
                                                "new": True})).status_code == 200
    data = (await access(client, "links/save", {"name": "ops", "role": "admin", "user": "anna", "new": True})).json()
    link = {"Authorization": f"Bearer {data['token']}"}
    assert settings.links["ops"].user == "anna"
    assert (await client.get("/api/info", headers=link)).json()["role"] == "developer"
    # its user disabled: the link as it was (no sandbox), its own role
    await access(client, "users/save", {"name": "anna", "role": "developer", "disabled": True})
    assert (await client.get("/api/info", headers=link)).json()["role"] == "admin"
    await access(client, "users/save", {"name": "anna", "role": "user", "disabled": False})
    assert (await client.get("/api/info", headers=link)).json()["role"] == "user"
    # unbound: its own role again
    assert (await access(client, "links/save", {"name": "ops", "role": "admin", "user": ""})).status_code == 200
    assert (await client.get("/api/info", headers=link)).json()["role"] == "admin"


async def test_who_a_link_may_act_as(client):
    settings = client.app.state.settings
    resp = await access(client, "links/save", {"name": "ops", "role": "user", "user": "nobody", "new": True})
    assert resp.status_code == 400 and "No user nobody here" in resp.json()["error"]
    settings.set_web_access(Access(users={"mario": USER_HASH}, roles={"mario": "workspace"}))
    resp = await access(client, "links/save", {"name": "ops", "role": "user", "user": "mario", "new": True})
    assert resp.status_code == 400 and "No user mario here" in resp.json()["error"]
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert 'aria-label="User the new link acts as"' in page and "<option>mario</option>" not in page


async def test_deleting_a_user_unbinds_its_links(client):
    settings = client.app.state.settings
    await access(client, "users/save", {"name": "anna", "role": "developer", "password": "anna-pass-1", "new": True})
    await access(client, "links/save", {"name": "ops", "role": "developer", "user": "anna", "new": True})
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert 'data-links="ops"' in page and "act for nobody then" in page
    assert (await access(client, "users/delete", {"name": "anna"})).status_code == 200
    assert settings.links["ops"].user == "" and settings.links["ops"].role == "developer"
    assert "user" not in yaml.safe_load(settings.users_file.read_text())["links"]["ops"]


def test_through_its_link_the_sandbox_is_its_users(server, sandbox):  # noqa: F811
    """On a server the link's Shell opens in the sandbox of the user it acts
    as, and closes when that user is disabled; a link acting for nobody
    opens none."""
    import hashlib

    token = "link-of-anna"
    server.set_web_access(Access(users={"anna": USER_HASH}, roles={"anna": "developer"},
                                 links={"ops": LinkAccess("sha256$" + hashlib.sha256(token.encode()).hexdigest(),
                                                          "admin", "anna")}))
    app = create_app(server)
    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {token}"}
        assert c.get("/servers/srv/test/shell", headers=headers).status_code == 200
        with c.websocket_connect("/servers/srv/test/shell/ws", headers=headers) as ws:
            assert "READY" in run(ws, "echo READY\n", "READY")
            assert app.state.live_shells.count("anna") == 1
            # its user disabled from the Users page: the link's shell closes too
            from .test_shell import browser
            resp = c.post("/api/access/users/save", json={"name": "anna", "role": "developer", "disabled": True},
                          headers=browser(app, "admin"))
            assert resp.status_code == 200, resp.text
            assert closed(ws) == (1000, "the user is disabled")
        assert sandbox["ensure"] == [("anna", 100)]
        # now it acts for nobody: no Shell
        resp = c.get("/servers/srv/test/shell", headers=headers)
        assert resp.status_code == 403 and "this link acts for none" in resp.text


def test_new_link_token_names_its_user(capsys):
    from omniacli.cli import main

    assert main(["new-link-token", "ops", "--role", "developer", "--user", "anna"]) == 0
    assert 'role: developer, user: anna}' in capsys.readouterr().out
