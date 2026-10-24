# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Copy from link: a backup made by a linked instance ("cliente", the app of
conftest's env, where this one is the link omnia, a developer), downloaded
here and restored into a local instance, in the same process."""

import json
import zipfile

import httpx
import pytest
import yaml

from omniacli import linkcopy
from omniacli.app import create_app
from omniacli.config import ConfigError, parse_servers, parse_settings, server_to_dict

from .conftest import LINK_TOKEN, BasicAsSession, USER_HASH
from .test_copy_production import odoo_version
from .test_links import HOST, as_user

URL = "/servers/srv/test/from-link"


@pytest.fixture
async def here(env, tmp_path, monkeypatch):
    """This instance: the server srv of env again, its test copied from
    cliente › srv/production."""
    monkeypatch.setattr(linkcopy, "POLL_EVERY", 0.02)
    conf = tmp_path / "here"
    conf.mkdir()
    servers = yaml.safe_load(env["settings"].servers_file.read_text())
    servers["servers"]["srv"]["test"]["from_link"] = {"link": "cliente", "server": "srv", "role": "production"}
    servers["servers"]["srv"]["production"]["from_link"] = {"link": "cliente", "server": "srv", "role": "production"}
    (conf / "servers.yaml").write_text(yaml.safe_dump(servers))
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "cliente": {"url": "http://customer.test", "token": LINK_TOKEN}}}))
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH, "dev": {"password": USER_HASH, "role": "developer"},
                           "viewer": {"password": USER_HASH, "role": "user"}}},
        "backup_dir": str(tmp_path / "here-backups"), "jobs_dir": str(tmp_path / "here" / "jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)
    customer = create_app(env["settings"])
    app = create_app(settings, link_transport=httpx.ASGITransport(app=customer))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as c:
        c.settings, c.customer = settings, customer
        yield c


def calls(env):
    return env["calls"].read_text().splitlines() if env["calls"].exists() else []


def a_backup(env, name, major):
    """A backup zip there, with the manifest.json of Odoo major."""
    folder = env["settings"].backup_dir / "srv" / "production"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / name, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"major_version": f"{major}.0", "version": f"{major}.0"}))
        zf.writestr("dump.sql", "-- dump")
    return folder / name


async def wait(client, job_id):
    for _ in range(400):
        data = (await client.get(f"/api/jobs/{job_id}", headers=as_user("admin"))).json()
        if data["status"] in ("success", "failed", "interrupted", "stopped"):
            return data
        await __import__("asyncio").sleep(0.02)
    raise AssertionError("job did not finish")


async def test_a_new_backup_there_restored_here(here, env):
    page = (await here.get("/servers/srv?view=both", headers=as_user("admin"))).text
    assert page.count("data-from-link=") == 2 and '<dialog id="from-link-dialog">' in page
    assert "Neutralize (Odoo 16 and later)" in page

    resp = await here.post(URL, headers=as_user("dev", post=True), json={"neutralize": True})
    assert resp.status_code == 200, resp.text
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["title"] == "Copy from cliente › srv/production to srv/test (new backup)"
    done = calls(env)
    # the backup is cliente's, of its production; the rest is here, on test
    assert done[0].startswith("backup -c ") and " DB_production " in done[0]
    local = here.settings.backup_dir / "srv" / "test"
    [zip_path] = list(local.iterdir())
    assert zip_path.name.startswith("cliente_DB_production_") and zip_path.read_text() == "dump\n"
    assert done[1] == "stop odoo-test"
    assert done[2] == f"restore -c {env['tmp']}/odoo.conf --copy --force --neutralize DB_test {zip_path}"
    assert done[3].startswith("update -c ") and done[3].endswith("-d DB_test")
    assert done[4] == "start odoo-test" and len(done) == 5
    # the steps the job page draws, with the bytes of the transfer
    progress = job["result"]["progress"]
    assert [s["id"] for s in progress["steps"]] == ["backup", "download", "restore"]
    assert progress["step"] == "restore" and progress["done"] == progress["total"] == 5
    assert any("cliente: " in line and "--- backup" in line for line in job["lines"])
    # there, the backup was the link's, on behalf of dev
    there = (await here.get("/links/cliente/api/jobs", headers=as_user("admin"))).json()["jobs"]
    assert any(j["title"].startswith("Backup srv/production") and "omnia" in j["source"] and "dev" in j["source"]
               for j in there)


async def test_a_new_backup_there_without_filestore(here, env):
    page = (await here.get("/servers/srv?view=both", headers=as_user("admin"))).text
    assert 'data-backup-option="no_filestore"' in page
    resp = await here.post(URL, headers=as_user("dev", post=True), json={"neutralize": True, "no_filestore": True})
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["title"].endswith("(new backup without filestore)")
    done = calls(env)
    # asked there, as its Backup form asks it
    assert " --no-filestore DB_production " in done[0]
    [zip_path] = list((here.settings.backup_dir / "srv" / "test").iterdir())
    assert zip_path.name.startswith("cliente_DB_production_") and zip_path.name.endswith("_nofilestore.zip")


async def test_a_backup_already_there_and_its_version(here, env):
    a_backup(env, "DB_production_20261020_100000.zip", 18)
    odoo_version(env, "test", 18)
    resp = await here.post(URL, headers=as_user("admin", post=True),
                           json={"backup": "DB_production_20261020_100000.zip", "neutralize": False})
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "success", job
    done = calls(env)
    assert not any(c.startswith("backup") for c in done)
    assert "--neutralize" not in done[1] and done[1].endswith("cliente_DB_production_20261020_100000.zip")
    assert any("WARNING: not neutralized" in line for line in job["lines"])

    # a database of another Odoo: refused before the instance is touched
    env["calls"].write_text("")
    a_backup(env, "DB_production_20261021_100000.zip", 17)
    resp = await here.post(URL, headers=as_user("admin", post=True),
                           json={"backup": "DB_production_20261021_100000.zip"})
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "failed" and "a database of Odoo 17" in job["error"] and "Odoo 18" in job["error"]
    assert calls(env) == []

    # a backup there no longer: no file left here
    resp = await here.post(URL, headers=as_user("admin", post=True), json={"backup": "gone.zip"})
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "failed" and "(404)" in job["error"], job
    assert not any(p.name.endswith(".part") for p in (here.settings.backup_dir / "srv" / "test").iterdir())


async def test_who_may_and_what_is_refused(here, env):
    # a production is an admin's, with its database typed
    resp = await here.post("/servers/srv/production/from-link", headers=as_user("dev", post=True), json={})
    assert resp.status_code == 403
    page = (await here.get("/servers/srv?view=both", headers=as_user("dev"))).text
    assert page.count("data-from-link=") == 1
    resp = await here.post("/servers/srv/production/from-link", headers=as_user("admin", post=True), json={})
    assert resp.status_code == 400 and "Type DB_production" in resp.json()["error"]
    resp = await here.post("/servers/srv/production/from-link", headers=as_user("admin", post=True),
                           json={"confirm": "DB_production", "backup": "../x.zip"})
    assert resp.status_code == 400 and "not the name of a backup zip" in resp.json()["error"]
    # a user watches only
    assert (await here.post(URL, headers=as_user("viewer", post=True), json={})).status_code == 403
    assert "data-from-link=" not in (await here.get("/servers/srv", headers=as_user("viewer"))).text
    # a link that is gone
    here.settings.link_targets.clear()
    resp = await here.post(URL, headers=as_user("admin", post=True), json={})
    assert resp.status_code == 400 and "No link cliente" in resp.json()["error"]


async def test_a_server_unknown_there(here, env):
    here.settings.servers["srv"].instances["test"].from_link.server = "nope"
    resp = await here.post(URL, headers=as_user("admin", post=True), json={})
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "failed" and "cliente does not know the backup of nope/production (404)" in job["error"]
    assert calls(env) == []


async def test_the_button_disabled_says_how_to_enable_it(here):
    def button(page):
        return page.split("Copy from link</button>")[0].rsplit("<span", 1)[-1]

    # a link that is gone: shown, disabled, no dialog for it
    here.settings.link_targets.clear()
    page = (await here.get("/servers/srv?view=test", headers=as_user("admin"))).text
    assert "data-from-link=" not in page
    assert "No link cliente here: add it on the Links page" in button(page) and "disabled" in button(page)
    # no Database from link, and no link at all
    here.settings.servers["srv"].instances["test"].from_link = None
    page = (await here.get("/servers/srv?view=test", headers=as_user("admin"))).text
    assert "add a link on the Links page, then set &#39;Database from link&#39;" in button(page)
    # no Database from link, links there
    here.settings.link_targets["cliente"] = object()
    page = (await here.get("/servers/srv?view=test", headers=as_user("admin"))).text
    assert "No database from a link: set &#39;Database from link&#39; in the server&#39;s form" in button(page)
    # without the role it needs, not shown
    assert "Copy from link" not in (await here.get("/servers/srv?view=test", headers=as_user("viewer"))).text


async def test_the_form_offers_the_links(here):
    page = (await here.get("/config/srv/edit", headers=as_user("admin"))).text
    assert '"links": ["cliente"]' in page and 'data-k="from_link.link"' in page
    assert '"has_restore": true' in page and '"has_restore": false' in page


def test_from_link_in_the_servers_file(env):
    data = yaml.safe_load(env["settings"].servers_file.read_text())["servers"]
    data["srv"]["test"]["from_link"] = {"link": "cliente", "server": "srv"}
    servers = parse_servers(data)
    src = servers["srv"].test.from_link
    assert (src.link, src.server, src.role, src.label) == ("cliente", "srv", "production", "cliente › srv/production")
    assert server_to_dict(servers["srv"])["test"]["from_link"] == {"link": "cliente", "server": "srv",
                                                                  "role": "production"}
    assert servers["srv"].production.from_link is None
    assert "from_link" not in server_to_dict(servers["srv"])["production"]
    # no link: none
    data["srv"]["test"]["from_link"] = {"link": "", "server": ""}
    assert parse_servers(data)["srv"].test.from_link is None
    for bad, message in (({"link": "cliente", "server": "srv", "role": "dev"}, "role"),
                         ({"link": "cliente", "server": "a b"}, "server"),
                         ("cliente", "a mapping")):
        data["srv"]["test"]["from_link"] = bad
        with pytest.raises(ConfigError, match=message):
            parse_servers(data)
