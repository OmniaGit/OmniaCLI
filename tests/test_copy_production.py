# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo plugin's copy of production into the test database."""
import dataclasses
from pathlib import Path

from omniacli.config import Server
from omniacli.odoo import ops as odoo_ops

from .test_app import AUTH, HOST, POST, basic, client, wait_job  # noqa: F401

URL = "/servers/srv/x/odoo/copy-production"


def calls(env):
    return env["calls"].read_text().splitlines() if env["calls"].exists() else []


def odoo_version(env, role, major):
    """A fake odoo-bin of Odoo major in the instance's odoo repository."""
    repo = env["settings"].instance("srv", role).repository("odoo").path
    (repo / "odoo-bin").write_text("")
    (repo / "odoo").mkdir(exist_ok=True)
    (repo / "odoo" / "release.py").write_text(f"version_info = ({major}, 0, 0, FINAL, 0, '')\n")


async def test_copy_with_a_new_backup(client, env):
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'data-action="odoo.copy-production" data-url="/servers/srv/x/odoo/copy-production"' in page
    assert "<dialog id=\"copy-dialog\">" in page

    resp = await client.post(URL, headers=POST, json={"backup": ""})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["title"] == "Copy production to test srv (new backup)"
    assert job["instances"] == ["srv/production", "srv/test"]
    done = calls(env)
    assert done[0].startswith("backup -c ") and " DB_production " in done[0]
    zip_path = done[0].split()[-1]
    assert done[1] == "stop odoo-test"
    assert done[2] == f"restore -c {env['tmp']}/odoo.conf --copy --force --neutralize DB_test {zip_path}"
    assert done[3].startswith("update -c ") and done[3].endswith("-d DB_test")
    assert done[4] == "start odoo-test"
    assert len(done) == 5  # production is neither stopped nor restarted


async def test_copy_with_a_new_backup_without_filestore(client, env):
    resp = await client.post(URL, headers=POST, json={"backup": "", "no_filestore": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and job["title"].endswith("(new backup without filestore)"), job
    done = calls(env)
    assert " --no-filestore DB_production " in done[0] and done[0].endswith("_nofilestore.zip")
    assert done[2].endswith("_nofilestore.zip")


async def test_copy_of_a_backup_already_made(client, env):
    folder = env["settings"].backup_dir / "srv" / "production"
    folder.mkdir(parents=True)
    (folder / "DB_production_20261022_100000.zip").write_text("dump")
    resp = await client.post(URL, headers=POST, json={"backup": "DB_production_20261022_100000.zip"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and job["title"].endswith("(DB_production_20261022_100000.zip)"), job
    done = calls(env)
    assert not any(c.startswith("backup") for c in done)
    assert done[1].endswith(f"DB_test {folder}/DB_production_20261022_100000.zip")

    for name in ("../x.zip", "nope.zip", "a.txt"):
        resp = await client.post(URL, headers=POST, json={"backup": name})
        assert resp.status_code == 400 and "No backup" in resp.json()["error"], name


async def test_versions_and_failures(client, env):
    # production and test on different Odoo versions: nothing is touched
    odoo_version(env, "production", 17)
    odoo_version(env, "test", 18)
    job = await wait_job(client, (await client.post(URL, headers=POST, json={})).json()["job"])
    assert job["status"] == "failed" and "production runs Odoo 17, test Odoo 18" in job["error"]
    assert calls(env) == []

    # before Odoo 16: no neutralize, said in the log
    odoo_version(env, "production", 15)
    odoo_version(env, "test", 15)
    job = await wait_job(client, (await client.post(URL, headers=POST, json={})).json()["job"])
    assert job["status"] == "success", job
    assert any("WARNING: Odoo 15 cannot be neutralized" in line for line in job["lines"])
    assert "--neutralize" not in next(c for c in calls(env) if c.startswith("restore"))

    # Odoo 20: click-odoo-restoredb refuses it
    odoo_version(env, "production", 20)
    odoo_version(env, "test", 20)
    env["calls"].unlink()
    job = await wait_job(client, (await client.post(URL, headers=POST, json={})).json()["job"])
    assert job["status"] == "failed" and "Odoo 20" in job["error"] and calls(env) == []

    # a failed restore leaves test stopped
    odoo_version(env, "production", 18)
    odoo_version(env, "test", 18)
    (env["tmp"] / "restore-fails").write_text("")
    job = await wait_job(client, (await client.post(URL, headers=POST, json={})).json()["job"])
    assert job["status"] == "failed" and any("is left stopped" in line for line in job["lines"])
    assert calls(env)[-2:] == ["stop odoo-test", calls(env)[-1]] and calls(env)[-1].startswith("restore")


async def test_who_and_where(client, env):
    viewer = {"Authorization": basic("viewer"), "Origin": f"http://{HOST}"}
    assert (await client.post(URL, headers=viewer, json={})).status_code == 403
    assert "copy-production" not in (await client.get("/servers/srv", headers=viewer)).text
    dev = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    job = await wait_job(client, (await client.post(URL, headers=dev, json={})).json()["job"])
    assert job["status"] == "success" and job["source"] == "manual: dev"

    # a server without production has no such button
    settings = client.app.state.settings
    srv = settings.servers["srv"]
    settings.servers["solo"] = Server(name="solo", instances={"test": dataclasses.replace(srv.test, server="solo")})
    assert "copy-production" not in (await client.get("/servers/solo", headers={"Authorization": AUTH})).text
    assert (await client.post("/servers/solo/x/odoo/copy-production", headers=POST, json={})).status_code == 404


async def test_run_as_restore_is_staged(env, fake_sudo, monkeypatch):
    from omniacli.jobs import Job

    staging = env["tmp"] / "staging"
    staging.mkdir()
    monkeypatch.setattr(odoo_ops, "BACKUP_STAGING", staging)
    test = dataclasses.replace(env["settings"].instance("srv", "test"), run_as="odoo")
    backup = env["tmp"] / "prod.zip"
    backup.write_text("dump")
    await odoo_ops.restore(Job(id="t", title="t", instances=[], source="test"), test, backup, True)
    sudo = fake_sudo.read_text()
    assert "sudo -n -u odoo " in sudo and f"{staging}/omniacli-restore-" in sudo and "prod.zip" in sudo
    assert list(staging.iterdir()) == []  # the staging folder is removed


def shared_setup(env, monkeypatch, data_dir=True):
    """The venv's python, a staging folder, a data_dir in the Odoo config and
    a production filestore with a file and a checklist entry."""
    import sys

    (env["tmp"] / "venv" / "bin" / "python").symlink_to(sys.executable)
    staging = env["tmp"] / "staging"
    staging.mkdir()
    monkeypatch.setattr(odoo_ops, "BACKUP_STAGING", staging)
    data = env["tmp"] / "data"
    conf = env["settings"].instance("srv", "test").options.odoo_config
    conf.write_text(f"[options]\ndata_dir = {data}\n" if data_dir else "[options]\n")
    prod = data / "filestore" / "DB_production"
    (prod / "ab").mkdir(parents=True)
    (prod / "ab" / "abcdef").write_text("attachment")
    (prod / "checklist" / "ab").mkdir(parents=True)
    (prod / "checklist" / "ab" / "abcdef").write_text("")
    return data / "filestore"


async def test_copy_sharing_the_filestore(client, env, monkeypatch):
    filestore = shared_setup(env, monkeypatch)
    resp = await client.post(URL, headers=POST, json={"backup": "", "share_filestore": True, "no_filestore": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["title"].endswith("(new backup, filestore shared with production)")
    done = calls(env)
    # the backup without filestore, the restore, then the links before the update
    assert " --no-filestore DB_production " in done[0] and done[2].endswith("_nofilestore.zip")
    assert done[3].startswith("update ")
    prod, test = filestore / "DB_production", filestore / "DB_test"
    assert (test / "ab" / "abcdef").stat().st_ino == (prod / "ab" / "abcdef").stat().st_ino
    assert (test / "ab").stat().st_ino != (prod / "ab").stat().st_ino  # its own folders
    assert not (test / "checklist").exists()
    assert any("1 files linked" in line for line in job["lines"])
    assert sorted(filestore.iterdir()) == [prod, test]
    assert list((env["tmp"] / "staging").iterdir()) == []

    # test deletes its file: production keeps it
    (test / "ab" / "abcdef").unlink()
    assert (prod / "ab" / "abcdef").read_text() == "attachment"

    # with a backup already made, the choice does not apply
    folder = env["settings"].backup_dir / "srv" / "production"
    name = sorted(p.name for p in folder.iterdir())[0]
    resp = await client.post(URL, headers=POST, json={"backup": name, "share_filestore": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["title"].endswith(f"({name})") and not any("hard links" in line for line in job["lines"])


async def test_sharing_refused(client, env, monkeypatch):
    # no data_dir: refused before anything is touched
    shared_setup(env, monkeypatch, data_dir=False)
    job = await wait_job(client, (await client.post(URL, headers=POST, json={"share_filestore": True})).json()["job"])
    assert job["status"] == "failed" and "sets no data_dir" in job["error"] and calls(env) == []

    # a filestore of test still there after the restore: test is left stopped
    conf = env["settings"].instance("srv", "test").options.odoo_config
    conf.write_text(f"[options]\ndata_dir = {env['tmp'] / 'data'}\n")
    (env["tmp"] / "data" / "filestore" / "DB_test").mkdir()
    job = await wait_job(client, (await client.post(URL, headers=POST, json={"share_filestore": True})).json()["job"])
    assert job["status"] == "failed" and any("already exists" in line for line in job["lines"])
    assert any("is left stopped" in line for line in job["lines"])
    assert not any(c.startswith("update") for c in calls(env))


def test_link_script_refuses_another_filesystem(tmp_path):
    import os
    import subprocess
    import sys
    import tempfile

    shm = Path("/dev/shm")
    if not shm.is_dir() or os.stat(shm).st_dev == os.stat(tmp_path).st_dev:
        import pytest
        pytest.skip("no second filesystem")
    src = tmp_path / "src"
    (src / "ab").mkdir(parents=True)
    (src / "ab" / "x").write_text("x")
    with tempfile.TemporaryDirectory(dir=shm) as other:
        out = subprocess.run([sys.executable, odoo_ops.LINK_SCRIPT, "check", src, Path(other) / "fs" / "DB"],
                             capture_output=True, text=True)
        assert out.returncode == 1 and "not on the same filesystem" in out.stderr
        out = subprocess.run([sys.executable, odoo_ops.LINK_SCRIPT, "check", src, tmp_path / "fs" / "DB"],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert list((tmp_path / "fs").iterdir()) == []  # the probe link is removed
