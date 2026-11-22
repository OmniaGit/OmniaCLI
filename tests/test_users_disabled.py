# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Users that are disabled (no login, their shells closed, their sandbox kept)
and the size of each user's shell sandbox."""
import asyncio

import pytest
import yaml

from omniacli.config import (SANDBOX_MB, Access, ConfigError, load_access, load_options, parse_auth,
                             parse_options, save_access)
from omniacli.shell import LiveShells

from .conftest import PASSWORD
from .test_app import HOST, access, auth_of, client, form_login  # noqa: F401 (client is a fixture)

HASH = "pbkdf2_sha256$1$00$00"


def test_the_users_file_keeps_disabled_and_sandbox(tmp_path):
    access_ = parse_auth({"users": {
        "anna": {"password": HASH, "role": "developer", "disabled": True, "sandbox_mb": 250},
        "luca": {"password": HASH, "role": "user"},
        "old": HASH,
    }})
    assert access_.disabled == {"anna"} and access_.sandbox_mb == {"anna": 250}
    path = tmp_path / "users.yaml"
    save_access(path, access_)
    users = yaml.safe_load(path.read_text())["users"]
    # what is not the default is written, nothing else: a file of before stays as it was
    assert users["anna"] == {"password": HASH, "role": "developer", "disabled": True, "sandbox_mb": 250}
    assert users["luca"] == {"password": HASH, "role": "user"}
    assert load_access(path) == access_


@pytest.mark.parametrize("record, error", [
    ({"disabled": "yes"}, "disabled must be true or false"),
    ({"sandbox_mb": 49}, "sandbox_mb must be a whole number from 50 to 102400"),
    ({"sandbox_mb": "100"}, "sandbox_mb must be a whole number"),
    ({"sandbox_mb": True}, "sandbox_mb must be a whole number"),
])
def test_wrong_values_are_refused(record, error):
    with pytest.raises(ConfigError, match=error):
        parse_auth({"users": {"anna": {"password": HASH, "role": "user", **record}}})


def test_the_main_config_wins_for_its_users():
    from omniacli.config import Settings

    settings = Settings(users={}, backup_dir=None, jobs_dir=None, servers_file=None, servers={},
                        fixed=Access(users={"root": HASH}, roles={"root": "admin"}, sandbox_mb={"root": 300}))
    settings.set_web_access(Access(users={"root": HASH, "anna": HASH}, disabled={"root", "anna"},
                                   sandbox_mb={"root": 900}))
    assert settings.disabled == {"anna"} and settings.user_sandbox_mb("root") == 300
    assert settings.user_sandbox_mb("anna") == SANDBOX_MB


def test_the_default_size_is_an_option(tmp_path):
    assert parse_options({}).sandbox_default_mb == SANDBOX_MB
    assert parse_options({"sandbox": {"default_mb": 500}}).sandbox_default_mb == 500
    for bad in (49, 102401, "100", 1.5, True):
        with pytest.raises(ConfigError, match="sandbox.default_mb must be a whole number from 50 to 102400"):
            parse_options({"sandbox": {"default_mb": bad}})
    with pytest.raises(ConfigError, match="must be mappings"):
        parse_options({"sandbox": 100})


async def test_live_shells_close_by_owner():
    live = LiveShells()
    first, why_first = live.open("anna")
    second, _ = live.open("anna")
    other, _ = live.open("luca")
    assert live.count("anna") == 2
    assert live.close("anna", "the user is disabled") == 2
    assert first.is_set() and second.is_set() and not other.is_set()
    assert why_first == ["the user is disabled"]
    live.ended("anna", first)
    live.ended("anna", second)
    assert live.count("anna") == 0 and live.close("nobody", "x") == 0


async def new_user(client, name, role="developer", **more):
    resp = await access(client, "users/save", {"name": name, "role": role, "password": f"{name}-pass-1",
                                               "new": True, **more})
    assert resp.status_code == 200, resp.text
    return {"Authorization": auth_of(name, f"{name}-pass-1"), "Origin": f"http://{HOST}"}


async def test_a_disabled_user_cannot_log_in_and_its_shells_close(client):
    settings = client.app.state.settings
    anna = await new_user(client, "anna")
    assert (await client.get("/api/info", headers=anna)).status_code == 200
    stop, why = client.app.state.live_shells.open("anna")

    resp = await access(client, "users/save", {"name": "anna", "role": "developer", "disabled": True})
    assert resp.status_code == 200, resp.text
    assert settings.user_disabled("anna") and stop.is_set() and why == ["the user is disabled"]
    assert yaml.safe_load(settings.users_file.read_text())["users"]["anna"]["disabled"] is True
    # its session ends at once, and the login form refuses it, with the right password
    assert (await client.get("/api/info", headers=anna)).status_code == 401
    client.cookies.clear()
    resp = await form_login(client, "anna", "anna-pass-1")
    assert resp.status_code == 403 and "This user is disabled." in resp.text
    log = client.app.state.access_log.recent(5)
    assert any(e["event"] == "failed" and "disabled" in e["detail"] for e in log)
    # the page shows it
    page = (await client.get("/users", headers={"Authorization": auth_of("admin")})).text
    assert 'data-text="anna developer disabled"' in page
    row = page[page.index('data-user="anna"'):]
    row = row[:row.index("</tr>")]
    assert 'aria-label="anna is active"' in row and 'aria-label="anna is active" checked' not in row

    # enabled again, everything comes back
    resp = await access(client, "users/save", {"name": "anna", "role": "developer", "disabled": False})
    assert resp.status_code == 200, resp.text
    assert (await client.get("/api/info", headers=anna)).status_code == 200
    assert "disabled" not in yaml.safe_load(settings.users_file.read_text())["users"]["anna"]


async def test_who_cannot_be_disabled(client):
    settings = client.app.state.settings
    await new_user(client, "boss", role="admin")
    me = dict(user="boss", password="boss-pass-1")
    resp = await access(client, "users/save", {"name": "boss", "role": "admin", "disabled": True}, **me)
    assert resp.json()["error"] == "You cannot disable yourself"
    # the only admin who can log in: not disabled (a disabled admin counts for nothing)
    settings.fixed.roles["admin"] = "user"
    settings.set_web_access(settings.web)
    data = (await access(client, "links/save", {"name": "ops", "role": "admin", "new": True}, **me)).json()
    link = {"Authorization": f"Bearer {data['token']}"}
    resp = await client.post("/api/access/users/save", json={"name": "boss", "role": "admin", "disabled": True},
                             headers=link)
    assert resp.json()["error"] == "boss is the last admin"


async def test_the_sandbox_size_of_a_user(client):
    settings = client.app.state.settings
    settings.options = parse_options({"sandbox": {"default_mb": 120}})
    # a new user takes the default of the Options page, then it is its own
    await new_user(client, "anna")
    assert settings.web.sandbox_mb["anna"] == 120
    settings.options = parse_options({"sandbox": {"default_mb": 80}})
    assert settings.user_sandbox_mb("anna") == 120
    await new_user(client, "luca", sandbox_mb=300)
    assert settings.user_sandbox_mb("luca") == 300
    # it grows, it is never made smaller
    resp = await access(client, "users/save", {"name": "anna", "role": "developer", "sandbox_mb": 200})
    assert resp.status_code == 200 and settings.user_sandbox_mb("anna") == 200
    resp = await access(client, "users/save", {"name": "anna", "role": "developer", "sandbox_mb": 150})
    assert resp.status_code == 400
    assert resp.json()["error"] == "The sandbox of anna is 200 MB: a disk is not made smaller"
    for bad in (49, 102401, "100", True):
        resp = await access(client, "users/save", {"name": "anna", "role": "developer", "sandbox_mb": bad})
        assert resp.status_code == 400 and "50 to 102400 MB" in resp.json()["error"], bad
    # a user of the main config keeps the default until its file says otherwise
    assert settings.user_sandbox_mb("viewer") == 80
    page = (await client.get("/users", headers={"Authorization": auth_of("admin")})).text
    assert 'data-sandbox="200"' in page and "is deleted too, with its disk" in page


async def test_deleting_a_user_closes_its_shells(client):
    settings = client.app.state.settings
    await new_user(client, "anna", sandbox_mb=300)
    stop, why = client.app.state.live_shells.open("anna")
    assert (await access(client, "users/delete", {"name": "anna"})).status_code == 200
    assert stop.is_set() and why == ["the user is deleted"]
    assert "anna" not in settings.sandbox_mb and "anna" not in settings.users


async def test_the_options_page_sets_the_default(client):
    settings = client.app.state.settings
    admin = {"Authorization": auth_of("admin"), "Origin": f"http://{HOST}"}
    page = (await client.get("/options", headers=admin)).text
    assert 'id="sandbox-default-mb"' in page and f'value="{SANDBOX_MB}"' in page
    resp = await client.post("/api/options/save", json={"sandbox": {"default_mb": 250}}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert load_options(settings.options_file).sandbox_default_mb == 250
    resp = await client.post("/api/options/save", json={"sandbox": {"default_mb": 10}}, headers=admin)
    assert resp.status_code == 400
    await asyncio.sleep(0)
    assert settings.options.sandbox_default_mb == 250
    assert PASSWORD


def test_an_open_shell_closes_when_its_user_is_disabled(env):
    """The real WebSocket of a shell (a development machine: as OmniaCLI's
    own user) ends at once when an admin disables its user."""
    from starlette.testclient import TestClient

    from omniacli.app import create_app

    from .test_shell import browser, closed, run

    settings = env["settings"]
    settings.set_web_access(Access(users={"anna": HASH}, roles={"anna": "developer"}))
    app = create_app(settings)
    with TestClient(app) as c:
        with c.websocket_connect("/servers/srv/test/shell/ws", headers=browser(app, "anna")) as ws:
            assert "READY" in run(ws, "echo READY\n", "READY")
            assert app.state.live_shells.count("anna") == 1
            resp = c.post("/api/access/users/save", json={"name": "anna", "role": "developer", "disabled": True},
                          headers=browser(app, "admin"))
            assert resp.status_code == 200, resp.text
            assert closed(ws) == (1000, "the user is disabled")
        assert app.state.live_shells.count("anna") == 0
