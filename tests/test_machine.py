# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The kind of machine: on a server nobody opens a shell of OmniaCLI's own
user, whatever the role, through the page, the WebSocket or a link; its
Shell opens in the user's sandbox."""

import getpass
import logging

import pytest
import yaml
from starlette.testclient import TestClient

from omniacli.app import create_app
from omniacli.checks import WARNING, check_server, writable_by
from omniacli.config import ConfigError, Restart, parse_machine, parse_settings

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
        # the instance is shown, without the buttons; the Shell is the sandbox's
        assert "/servers/srv/test/restart" in page and 'href="/servers/srv/test/shell"' in page
        assert "A shell of your sandbox on this server" in page
        for kind in ("claude", "psql"):
            assert f'/test/{kind}"' not in page
            resp = c.get(f"/servers/srv/test/{kind}", headers=browser(app, user))
            assert resp.status_code == 403 and REFUSED.format(kind) in resp.text
            with c.websocket_connect(f"/servers/srv/test/{kind}/ws", headers=browser(app, user)) as ws:
                assert closed(ws) == (1008, REFUSED.format(kind))
            # no file dropped on a terminal that does not open
            resp = c.post(f"/servers/srv/test/{kind}/upload?name=a.txt", content=b"x", headers=browser(app, user))
            assert resp.status_code == 403 and REFUSED.format(kind) in resp.text
    assert not [e for e in app.state.access_log.recent() if e["event"] == "shell"]
    assert not (tmp_path / "drops").exists()


@pytest.fixture
def sandbox(monkeypatch):
    """The root helper and sudo faked: ensure answers a sandbox user, and the
    shell started as that user is an ordinary one, which records it."""
    from omniacli import sandboxops, shell as shell_module

    calls, real_start = {"ensure": [], "as_user": []}, shell_module.Shell.start.__func__

    async def ensure(user, mb):
        calls["ensure"].append((user, mb))
        if calls.get("fail"):
            raise sandboxops.SandboxError(calls["fail"])
        return {"user": "omniacli-sb-1", "home": "/var/lib/omniacli-sandbox/home/1", "mb": mb}

    async def start(cls, cwd, cols=80, rows=24, command=None, env=None, stay=True, as_user=""):
        calls["as_user"].append(as_user)
        return await real_start(cls, cwd, cols, rows, command, env, stay)
    monkeypatch.setattr(sandboxops, "ensure", ensure)
    monkeypatch.setattr(shell_module.Shell, "start", classmethod(start))
    return calls


def test_the_shell_of_a_server_opens_in_the_users_sandbox(server, tmp_path, sandbox):
    from .test_shell import run

    app = create_app(server)
    with TestClient(app) as c:
        page = c.get("/servers/srv/test/shell", headers=browser(app, "dev"))
        assert page.status_code == 200 and "sandbox of dev" in page.text and "a disk of 100 MB" in page.text
        assert "Files cannot be dropped on the shell of a sandbox" in page.text and "{upload:" not in page.text
        assert c.get("/servers/srv/test/shell", headers=browser(app, "viewer")).status_code == 403
        with c.websocket_connect("/servers/srv/test/shell/ws", headers=browser(app, "dev")) as ws:
            assert "READY" in run(ws, "echo READY\n", "READY")
        assert sandbox["ensure"] == [("dev", 100)] and sandbox["as_user"] == ["omniacli-sb-1"]
        opened = [e["detail"] for e in app.state.access_log.recent() if e["event"] == "shell"]
        assert any("in the sandbox omniacli-sb-1 of dev (100 MB) opened" in d for d in opened), opened
        # no file dropped: OmniaCLI's user cannot write in the sandbox's home
        resp = c.post("/servers/srv/test/shell/upload?name=a.txt", content=b"x", headers=browser(app, "dev"))
        assert resp.status_code == 403 and "Files cannot be dropped" in resp.text
        # a helper that fails: said to the page, no shell
        sandbox["fail"] = "a sandbox of 100 MB needs 100 MB more"
        with c.websocket_connect("/servers/srv/test/shell/ws", headers=browser(app, "dev")) as ws:
            assert closed(ws) == (1011, "The sandbox of dev: a sandbox of 100 MB needs 100 MB more")
        assert sandbox["as_user"] == ["omniacli-sb-1"]


def test_a_disabled_user_opens_no_sandbox(server, sandbox):
    from omniacli.config import Access

    server.set_web_access(Access(users={"anna": USER_HASH}, roles={"anna": "developer"}))
    app = create_app(server)
    with TestClient(app) as c:
        headers = browser(app, "anna")
        server.set_web_access(Access(users={"anna": USER_HASH}, roles={"anna": "developer"}, disabled={"anna"}))
        # its session ends; and the sandbox is refused by itself too (sudo runs an expired account)
        assert c.get("/servers/srv/test/shell", headers=headers).status_code == 401
    assert sandbox["ensure"] == []


async def test_a_sandbox_shell_runs_its_one_sudoers_command(monkeypatch):
    """The command is exactly what the sudoers line allows, from /."""
    from omniacli import shell as shell_module

    seen = {}

    async def spawn(*args, **kwargs):
        seen.update(args=args, cwd=kwargs["cwd"], env=kwargs["env"])
        raise OSError("not started in the test")
    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError):
        await shell_module.Shell.start(None, as_user="omniacli-sb-3")
    assert seen["args"] == ("setsid", "-c", "sudo", "-n", "-H", "-u", "omniacli-sb-3", "/bin/bash", "--login")
    assert seen["cwd"] == "/" and "HOME" not in seen["env"]


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
        assert '/test/claude"' not in page
        assert c.get("/servers/srv/test/psql", headers=browser(app, "dev")).status_code == 200
        assert c.get("/servers/srv/test/claude", headers=browser(app, "dev")).status_code == 403
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
        assert "/test/restart" in page and '/test/claude"' not in page
        # its Shell is a sandbox of a user there, and this link acts for none
        resp = c.get("/links/cliente/servers/srv/test/shell", headers=headers)
        assert resp.status_code == 403 and "this link acts for none" in resp.text
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


def test_writable_by(tmp_path):
    me = getpass.getuser()
    folder = tmp_path / "repo"
    folder.mkdir()
    folder.chmod(0o755)
    assert writable_by(folder, me)
    folder.chmod(0o555)
    assert not writable_by(folder, me)
    folder.chmod(0o755)
    assert not writable_by(tmp_path / "nope", me)
    assert not writable_by(folder, "no-such-user-here")
    assert writable_by(folder, "root")


async def _names(settings, machine):
    report = await check_server(settings.servers["srv"], settings.backup_dir, machine)
    return {(c.target, c.name): c for c in report}


async def test_a_server_warns_of_what_still_reaches_its_user(env, tmp_path):
    settings = env["settings"]
    test = settings.servers["srv"].instances["test"]
    test.claude_dir = tmp_path
    found = await _names(settings, "server")
    assert "set Run as" in found["test", "Isolation"].detail
    assert "no effect" in found["test", "Claude folder"].detail
    assert all(not c.ok and c.level == WARNING for k, c in found.items() if k[1] in ("Isolation", "Claude folder"))
    # nothing of this on a development machine
    found = await _names(settings, "development")
    assert not [k for k in found if k[1].startswith("Isolation") or k[1] == "Claude folder"]


async def test_a_repository_the_run_as_user_may_write(env):
    settings = env["settings"]
    test = settings.servers["srv"].instances["test"]
    test.run_as = getpass.getuser()
    repo = test.repositories[0]
    found = await _names(settings, "server")
    warning = found["test", f"Isolation of {repo.name}"]
    assert not warning.ok and warning.level == WARNING
    assert f"chown -R {getpass.getuser()}:{test.run_as} {repo.path}" in warning.detail
    assert "/hooks" in warning.detail and "/config" in warning.detail
    assert ("test", "Isolation") not in found  # Run as is set
    # closed as the warning says: nothing left to say
    for path in [repo.path, repo.path / ".git", repo.path / ".git" / "hooks", repo.path / ".git" / "config"]:
        path.chmod(path.stat().st_mode & ~0o222)
    try:
        found = await _names(settings, "server")
        assert ("test", f"Isolation of {repo.name}") not in found
    finally:
        for path in [repo.path, repo.path / ".git", repo.path / ".git" / "hooks", repo.path / ".git" / "config"]:
            path.chmod(path.stat().st_mode | 0o200)


def test_a_process_instance_says_so(env):
    test = env["settings"].servers["srv"].instances["test"]
    test.restart = Restart("process", "")
    assert "Odoo itself runs as" in test.plugin.own_user_code(test)
    test.restart = Restart("systemd", "odoo")
    test.run_as = "odoo"
    assert test.plugin.own_user_code(test) is None
