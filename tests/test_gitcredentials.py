# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The git credentials of the interface: kept apart (0600), given to every
git command on a server's repositories instead of the machine's, an ssh
remote reached over https; a real git checks what it reads from them."""
import os
import stat
import subprocess

import pytest

from omniacli import gitcredentials, ops
from omniacli.jobs import Job

from .test_app import POST, client  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "false"}


def creds(*entries):
    return {c.host: c for c in (gitcredentials.check(*e) for e in entries)}


def test_check_load_save(tmp_path):
    path = tmp_path / "credentials.yaml"
    assert gitcredentials.load(path) == {}
    found = {"server:srv": creds(("GitHub.com", "bot", "ghp_1")), "workspace:acme": creds(("bitbucket.org", "o", "t"))}
    gitcredentials.save(path, found)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert gitcredentials.load(path) == found and "github.com" in found["server:srv"]
    assert found["server:srv"]["github.com"].public() == {"host": "github.com", "username": "bot", "token_set": True}
    for args, message in ((("not a host!", "u", "t"), "not a host"), (("github.com", "a b", "t"), "the user"),
                          (("github.com", "u", ""), "the token")):
        with pytest.raises(gitcredentials.CredentialError, match=message):
            gitcredentials.check(*args)
    with pytest.raises(gitcredentials.CredentialError, match="not a scope"):
        gitcredentials.scope_of("server", "../x")


def test_the_server_wins_over_its_workspace():
    found = {"workspace:acme": creds(("github.com", "ws", "1"), ("gitlab.com", "ws", "2")),
             "server:srv": creds(("github.com", "srv", "3"))}
    result = gitcredentials.merged(found, "srv", "acme")
    assert (result["github.com"].username, result["gitlab.com"].username) == ("srv", "ws")
    assert gitcredentials.merged(found, "srv", None) == found["server:srv"]


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, input="")


def test_git_reads_them_and_rewrites_ssh(tmp_path):
    """A real git: the token from the store file, the machine's helpers off;
    an ssh remote of the host seen as https, the repository's config untouched."""
    store = gitcredentials.write_store(tmp_path / "store", "srv", creds(("github.com", "omnia.bot", "t:k/1")))
    assert stat.S_IMODE(store.stat().st_mode) == 0o600 and stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    options = list(gitcredentials.git_options(store, ["github.com", "git.example.com:8443"]))
    done = subprocess.run(["git", *options, "credential", "fill"], env=ENV, capture_output=True, text=True,
                          input="protocol=https\nhost=github.com\n\n")
    assert done.returncode == 0, done.stderr
    assert "username=omnia.bot\n" in done.stdout and "password=t:k/1\n" in done.stdout
    repo = tmp_path / "repo"
    assert _git("init", "-q", str(repo)).returncode == 0
    for remote in ("git@github.com:OmniaGit/x.git", "ssh://git@github.com/OmniaGit/x.git"):
        _git("remote", "remove", "origin", cwd=repo)
        _git("remote", "add", "origin", remote, cwd=repo)
        assert _git(*options, "remote", "get-url", "origin", cwd=repo).stdout.strip() == "https://github.com/OmniaGit/x.git"
        assert _git("remote", "get-url", "origin", cwd=repo).stdout.strip() == remote
    assert "t:k" not in (repo / ".git" / "config").read_text()
    # a host with a port of its own is used over https only
    assert not [o for o in options if "8443" in o and "insteadOf" in o]


def test_bind_gives_each_repository_its_servers(env, tmp_path):
    settings = env["settings"]
    settings.credentials_file = tmp_path / "credentials.yaml"
    gitcredentials.save(settings.credentials_file, {"server:srv": creds(("github.com", "bot", "tok"))})
    gitcredentials.bind(settings)
    repo = settings.servers["srv"].test.repositories[0]
    assert repo.git("pull")[:3] == ["git", "-c", "credential.helper="] and repo.git("pull")[-1] == "pull"
    store = gitcredentials.store_dir(settings.credentials_file) / "server-srv.store"
    assert store.read_text() == "https://bot:tok@github.com\n"
    # none any more: the machine's git again, the store file gone
    gitcredentials.save(settings.credentials_file, {})
    gitcredentials.bind(settings)
    assert repo.git("pull") == ["git", "pull"] and not store.exists()


async def test_the_operations_pass_them(monkeypatch, env):
    repo = env["settings"].servers["srv"].test.repositories[0]
    repo.git_options = ("-c", "x.y=1")
    ran = []

    async def run(job, cmd, **kw):
        ran.append(cmd)
        return 0

    async def capture(cmd, **kw):
        ran.append(cmd)
        return 0, {"rev-parse": "abc", "status": ""}.get(cmd[3] if cmd[:3] == ["git", "-c", "x.y=1"] else "", "")
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(ops, "capture", capture)
    inst = env["settings"].servers["srv"].test
    monkeypatch.setattr(ops, "ensure_ready", lambda *a, **k: _none())
    await ops.pull(Job(id="j", title="t", instances=[], source="s"), inst)
    pulls = [c for c in ran if "pull" in c]
    assert pulls and pulls[0][:3] == ["git", "-c", "x.y=1"]


async def _none():
    return None


async def test_the_pages(workspaces, client, env, tmp_path):
    settings = env["settings"]
    settings.credentials_file = tmp_path / "credentials.yaml"
    page = await client.get("/credentials", headers=POST)
    assert page.status_code == 200 and "Server srv" in page.text and "Workspace acme" in page.text
    resp = await client.post("/api/credentials/save", headers=POST,
                             json={"kind": "server", "name": "srv", "host": "github.com", "username": "bot", "token": "SECRET1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["credentials"] == [{"host": "github.com", "username": "bot", "token_set": True}]
    # changed without a token: the saved one is kept
    await client.post("/api/credentials/save", headers=POST,
                      json={"kind": "server", "name": "srv", "host": "github.com", "username": "bot2", "token": ""})
    saved = gitcredentials.load(settings.credentials_file)["server:srv"]["github.com"]
    assert (saved.username, saved.token) == ("bot2", "SECRET1")
    page = await client.get("/credentials", headers=POST)
    assert "SECRET1" not in page.text and "bot2" in page.text
    assert settings.servers["srv"].test.repositories[0].git_options
    for body, code in (({"kind": "server", "name": "nope", "host": "github.com", "username": "u", "token": "t"}, 404),
                       ({"kind": "server", "name": "srv", "host": "bad host", "username": "u", "token": "t"}, 400)):
        assert (await client.post("/api/credentials/save", headers=POST, json=body)).status_code == code
    # the workspace's admins set its own, never another scope's; its users and other workspaces' cannot
    url = "/api/workspaces/acme/credentials/"
    resp = await client.post(url + "save", headers=post_as("mario"),
                             json={"kind": "server", "name": "srv", "host": "gitlab.com", "username": "m", "token": "T2"})
    assert resp.status_code == 200
    found = gitcredentials.load(settings.credentials_file)
    assert "gitlab.com" in found["workspace:acme"] and "gitlab.com" not in found["server:srv"]
    assert (await client.post(url + "save", headers=post_as("luca"), json={})).status_code == 403
    assert (await client.post(url + "save", headers=post_as("eva"), json={})).status_code == 403
    assert (await client.get("/credentials", headers=basic("mario"))).status_code == 403
    page = await client.get("/workspaces/acme", headers=basic("mario"))
    assert "gitlab.com" in page.text and "T2" not in page.text
    assert (await client.post(url + "delete", headers=post_as("mario"), json={"host": "gitlab.com"})).status_code == 200
    assert (await client.post(url + "delete", headers=post_as("mario"), json={"host": "gitlab.com"})).status_code == 404
    assert (await client.post("/api/credentials/delete", headers=POST,
                              json={"kind": "server", "name": "srv", "host": "github.com"})).status_code == 200
    assert settings.servers["srv"].test.repositories[0].git_options == ()
