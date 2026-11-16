# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The customer's repositories of a workspace's instance: https only, cloned
by OmniaCLI into the role's addons/ with the workspace's git credentials,
named in that role's odoo.conf, the role restarted; removed the same way."""
import os
import stat

import pytest

from omniacli import gitcredentials, ops, workspaceops
from omniacli.config import Workspace, parse_servers
from omniacli.jobs import Job, JobFailed

from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

URL = "https://github.com/acme/odoo-custom.git"


def test_what_a_repository_may_be():
    workspaceops.check_repository("custom", URL, "17.0")
    for name, url, branch, message in (
            ("../x", URL, "main", "not a repository name"),
            ("custom", "http://github.com/acme/x.git", "main", "not an https"),
            ("custom", "https://bot:tok@github.com/acme/x.git", "main", "not an https"),
            ("custom", "git@github.com:acme/x.git", "main", "not an https"),
            ("custom", "https://github.com/acme/../x", "main", "not an https"),
            ("custom", "https://github.com/acme/x.git?u=1", "main", "not an https"),
            ("custom", URL, "--upload-pack=x", "not a branch"),
            ("custom", URL, "a..b", "not a branch")):
        with pytest.raises(JobFailed, match=message):
            workspaceops.check_repository(name, url, branch)


def _job():
    return Job(id="j", title="t", instances=[], source="s")


async def test_clone(tmp_path, monkeypatch):
    ws = Workspace("acme", storage=str(tmp_path))
    addons = workspaceops.addons_folder(ws, "main", "test")
    addons.mkdir(parents=True)
    ran = []

    async def run(job, cmd, **kw):
        ran.append(cmd)
        target = cmd[-1]
        os.makedirs(os.path.join(target, ".git", "objects"))
        # git keeps its objects read only
        open(os.path.join(target, ".git", "objects", "o"), "w").close()
        os.chmod(os.path.join(target, ".git", "objects", "o"), stat.S_IRUSR)
        os.chmod(os.path.join(target, ".git", "objects"), stat.S_IRUSR | stat.S_IXUSR)
        if fail:
            raise JobFailed("clone failed")
        return 0
    monkeypatch.setattr(workspaceops, "run", run)
    fail = False
    path = await workspaceops.clone(_job(), ws, "main", "test", "custom", URL, "17.0", ("-c", "a.b=1"))
    assert path == addons / "custom"
    assert ran[0] == ["git", "-c", "a.b=1", "clone", "--branch", "17.0", "--recurse-submodules", "--", URL, str(path)]
    with pytest.raises(JobFailed, match="exists already"):
        await workspaceops.clone(_job(), ws, "main", "test", "custom", URL, "17.0")
    # a clone that fails leaves nothing, its read-only objects included
    fail = True
    with pytest.raises(JobFailed, match="clone failed"):
        await workspaceops.clone(_job(), ws, "main", "test", "other", URL, "17.0")
    assert not (addons / "other").exists()
    workspaceops.remove_clone(path)
    assert not path.exists()


@pytest.fixture
def instance(workspaces, env, tmp_path, monkeypatch):
    """acme-main, an instance of acme on tmp_path, its roles compose projects;
    the clone, the helper's conf and the restart recorded."""
    settings = env["settings"]
    ws = settings.workspaces["acme"]
    ws.storage = str(tmp_path / "storage")
    ws.servers.append("acme-main")
    folder = tmp_path / "storage" / "acme" / "main"
    data = {"type": "docker", "status": "active", **{role: {
        "database": "main", "http_port": port, "repositories": [],
        "restart": {"type": "compose", "service": str(folder / role), "sudo": True}}
        for role, port in (("production", 20000), ("test", 20002))}}
    settings.servers["acme-main"] = parse_servers({"acme-main": data}, settings.logs_dir)["acme-main"]
    settings.credentials_file = tmp_path / "credentials.yaml"
    gitcredentials.save(settings.credentials_file, {"workspace:acme": {"github.com": gitcredentials.check(
        "github.com", "bot", "tok")}})
    calls = {"clone": [], "conf": [], "restart": [], "fail_conf": False}

    async def clone(job, ws, instance, role, name, url, branch, git_options=()):
        calls["clone"].append((instance, role, name, url, branch, bool(git_options)))
        path = workspaceops.addons_folder(ws, instance, role) / name
        path.mkdir(parents=True)
        return path

    async def write_conf(job, ws, instance, role, repositories):
        calls["conf"].append((role, list(repositories)))
        if calls["fail_conf"] and repositories:
            raise JobFailed("the helper refused")
        return {}

    async def restart(job, inst):
        calls["restart"].append(inst.key)
    monkeypatch.setattr(workspaceops, "clone", clone)
    monkeypatch.setattr(workspaceops, "write_conf", write_conf)
    monkeypatch.setattr(ops, "restart", restart)
    return {"calls": calls, "folder": folder, "settings": settings}


async def test_add_and_remove(instance, client):
    calls, settings = instance["calls"], instance["settings"]
    url = "/api/workspaces/acme/repositories/"
    body = {"server": "acme-main", "url": URL, "production_branch": "17.0", "test_branch": "17.0-dev", "name": "custom"}
    resp = await client.post(url + "add", headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    # cloned into each role on its branch, with the workspace's credentials; both odoo.conf; both restarted
    assert calls["clone"] == [("main", "production", "custom", URL, "17.0", True),
                              ("main", "test", "custom", URL, "17.0-dev", True)]
    assert calls["conf"] == [("production", ["custom"]), ("test", ["custom"])]
    assert calls["restart"] == ["acme-main/production", "acme-main/test"]
    server = settings.servers["acme-main"]
    assert [(r.name, r.branch, r.path) for r in server.test.repositories] == [
        ("custom", "17.0-dev", instance["folder"] / "test" / "addons" / "custom")]
    assert [(r.name, r.branch) for r in server.production.repositories] == [("custom", "17.0")]
    # pulled and merged later with the credentials too
    assert server.test.repositories[0].git_options[:3] == ("-c", "credential.helper=", "-c")
    resp = await client.post(url + "add", headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and "already" in resp.json()["error"]
    page = (await client.get("/workspaces/acme", headers=post_as("mario"))).text
    assert "production 17.0, test 17.0-dev" in page
    resp = await client.post(url + "remove", headers=post_as("mario"), json={"server": "acme-main", "name": "custom"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert calls["conf"][-2:] == [("production", []), ("test", [])]
    server = settings.servers["acme-main"]
    assert server.test.repositories == [] and server.production.repositories == []
    for role in ("production", "test"):
        assert not (instance["folder"] / role / "addons" / "custom").exists()


async def test_a_conf_refused_leaves_nothing(instance, client):
    calls, settings = instance["calls"], instance["settings"]
    calls["fail_conf"] = True
    resp = await client.post("/api/workspaces/acme/repositories/add", headers=post_as("mario"),
                             json={"server": "acme-main", "url": URL, "production_branch": "17.0",
                                   "test_branch": "17.0", "name": "custom"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed"
    server = settings.servers["acme-main"]
    assert server.production.repositories == [] and server.test.repositories == []
    assert calls["conf"][-2:] == [("production", []), ("test", [])] and calls["restart"] == []
    for role in ("production", "test"):
        assert not (instance["folder"] / role / "addons" / "custom").exists()


async def test_who_and_what_is_refused(instance, client):
    url = "/api/workspaces/acme/repositories/add"
    good = {"server": "acme-main", "url": URL, "production_branch": "17.0", "test_branch": "17.0", "name": "custom"}
    assert (await client.post(url, headers=post_as("luca"), json=good)).status_code == 403
    assert (await client.post(url, headers=post_as("eva"), json=good)).status_code == 403
    for change, code, message in (({"server": "srv"}, 404, "No instance srv"),
                                  ({"test_branch": "--x"}, 400, "not a branch"),
                                  ({"url": "https://bot:tok@github.com/a/b.git"}, 400, "no user or token"),
                                  ({"name": "a/b"}, 400, "not a repository name")):
        resp = await client.post(url, headers=post_as("mario"), json={**good, **change})
        assert resp.status_code == code and message in resp.json()["error"], change
    resp = await client.post("/api/workspaces/acme/repositories/remove", headers=post_as("mario"),
                             json={"server": "acme-main", "name": "nope"})
    assert resp.status_code == 404
