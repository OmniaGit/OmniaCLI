# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The workspaces' templates published for download: the archive the
release script publishes, the signed index, the download, the root helper's
install, the higher version used, the Workspaces page."""

import asyncio
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import pluginstore, stacktemplates, templatestore, workspacehelper, workspaceops
from omniacli.app import create_app
from omniacli.config import PluginSource
from omniacli.jobs import Job, JobFailed
from omniacli.packagesign import load_private_key, new_private_key, sign_file

from .conftest import BasicAsSession
from .test_pluginstore import INDEX, Site, as_admin

REPO_TEMPLATES = Path(__file__).parent.parent / "templates"
ARCHIVE = "omniacli-template-odooplm-20.tar.gz"


def template_copy(folder: Path, version: str) -> Path:
    """The repository's odooplm-20 at version, in folder/odooplm-20."""
    target = folder / "odooplm-20"
    shutil.copytree(REPO_TEMPLATES / "odooplm-20", target)
    meta = json.loads((target / "template.json").read_text())
    (target / "template.json").write_text(json.dumps({**meta, "version": version}))
    return target


def test_pack_is_checked_and_reproducible(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    entry = stacktemplates.pack(REPO_TEMPLATES / "odooplm-20", tmp_path / "a")
    stacktemplates.pack(REPO_TEMPLATES / "odooplm-20", tmp_path / "b")
    assert entry["file"] == ARCHIVE and entry["name"] == "odooplm-20" and entry["version"]
    assert (tmp_path / "a" / ARCHIVE).read_bytes() == (tmp_path / "b" / ARCHIVE).read_bytes()
    with tarfile.open(tmp_path / "a" / ARCHIVE) as tar:
        assert sorted(m.name for m in tar) == sorted(stacktemplates.FILES)
        assert {(m.mtime, m.uid, m.mode) for m in tar} == {(0, 0, 0o644)}
    # a template the helper would refuse is not packed
    bad = template_copy(tmp_path / "bad", "2026.1.1")
    (bad / "compose.yaml").write_text((bad / "compose.yaml").read_text().replace("127.0.0.1:", "0.0.0.0:"))
    with pytest.raises(stacktemplates.TemplateError, match="127.0.0.1"):
        stacktemplates.pack(bad, tmp_path)


def _archive(path: Path, members: list[tarfile.TarInfo], data: dict) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info in members:
            tar.addfile(info, io.BytesIO(data.get(info.name, b"")) if info.isreg() else None)
    return path


def test_unpack_takes_the_three_files_only(tmp_path):
    stacktemplates.pack(REPO_TEMPLATES / "odooplm-20", tmp_path)
    stacktemplates.unpack(tmp_path / ARCHIVE, tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(stacktemplates.FILES)

    def reg(name, size=1):
        info = tarfile.TarInfo(name)
        info.size = size
        return info
    link = tarfile.TarInfo("compose.yaml")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
    for i, members in enumerate((
            [reg("template.json"), reg("compose.yaml"), reg("odoo.conf.in"), reg("../evil")],
            [reg("template.json"), link, reg("odoo.conf.in")],
            [reg("template.json"), reg("compose.yaml")])):
        archive = _archive(tmp_path / f"bad{i}.tar.gz", members, {m.name: b"x" for m in members})
        with pytest.raises(stacktemplates.TemplateError):
            stacktemplates.unpack(archive, tmp_path / f"bad{i}")
    (tmp_path / "junk.tar.gz").write_bytes(b"not an archive")
    with pytest.raises(stacktemplates.TemplateError, match="not a template's archive"):
        stacktemplates.unpack(tmp_path / "junk.tar.gz", tmp_path / "junk")


def test_the_higher_version_is_used(tmp_path):
    assert stacktemplates.version_key("2026.1.10") > stacktemplates.version_key("2026.1.9")
    assert stacktemplates.version_key("dev") == ()
    template_copy(tmp_path / "package", "2026.1.2")
    template_copy(tmp_path / "installed", "2026.1.10")
    assert stacktemplates.available([tmp_path / "package", tmp_path / "installed"])["odooplm-20"].version == "2026.1.10"
    template_copy(tmp_path / "old", "2026.1.1")
    assert stacktemplates.available([tmp_path / "package", tmp_path / "old"])["odooplm-20"].version == "2026.1.2"


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A key, the signed archive of odooplm-20 2026.1.5 and a signed index, on the site."""
    pem, public = new_private_key(b"pass")
    key = load_private_key(pem, b"pass")
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    def publish(version: str, signed_version: str | None = None):
        folder = template_copy(tmp_path / f"src-{version}", version)
        stacktemplates.pack(folder, site_dir)
        sign_file(key, site_dir / ARCHIVE, signed_version or version)
    publish("2026.1.5")
    index = site_dir / templatestore.INDEX_NAME
    index.write_text(json.dumps({"format": 1, "templates": [
        {"name": "odooplm-20", "label": "OdooPLM 20", "description": "Odoo 20", "file": ARCHIVE},
        {"name": "other", "file": "wrong.tar.gz"}]}))
    sign_file(key, index, "20261030")
    site = Site(site_dir)
    client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(site.handler))  # noqa: E731
    monkeypatch.setattr(pluginstore, "_client", client)
    monkeypatch.setattr(templatestore, "_client", client)
    installed = tmp_path / "installed"
    monkeypatch.setattr(templatestore, "INSTALLED", installed)
    config = tmp_path / "omniacli.yaml"
    config.write_text(yaml.safe_dump({"jobs_dir": str(tmp_path / "jobs"), "plugins": {"index": INDEX, "public_key": public}}))
    return {"public": public, "site": site, "publish": publish, "source": PluginSource(INDEX, public),
            "config": config, "installed": installed, "kept": tmp_path / "templates"}


def _job():
    return Job(id="t", title="t", instances=[], source="test")


async def test_the_index_lists_the_signed_templates(published):
    found = await templatestore.available(published["source"])
    assert templatestore.index_url(published["source"]).endswith("/download/omniacli-templates.json")
    assert (found[0]["name"], found[0]["version"], found[0]["url"].endswith(ARCHIVE)) == ("odooplm-20", "2026.1.5", True)
    assert found[1]["error"] == "the index names it wrongly"
    with pytest.raises(pluginstore.StoreError, match="no plugins.public_key"):
        await templatestore.available(PluginSource(INDEX, ""))
    published["site"].changed[templatestore.INDEX_NAME] = b'{"format": 1, "templates": []}'
    with pytest.raises(pluginstore.StoreError, match="not the index its manifest signs"):
        await templatestore.available(published["source"])


async def test_download_and_install(published):
    entry = (await templatestore.available(published["source"]))[0]
    manifest = await templatestore.download(_job(), published["source"], entry, published["kept"])
    assert manifest["version"] == "2026.1.5" and (published["kept"] / "odooplm-20" / ARCHIVE).is_file()
    made = templatestore.install("odooplm-20", published["config"])
    target = published["installed"] / "odooplm-20"
    assert made["version"] == "2026.1.5" and made["folder"] == str(target)
    assert stacktemplates.load_template(target).version == "2026.1.5"
    assert sorted(p.name for p in published["installed"].iterdir()) == ["odooplm-20"]
    # a newer one replaces it
    published["publish"]("2026.1.6")
    entry = (await templatestore.available(published["source"]))[0]
    await templatestore.download(_job(), published["source"], entry, published["kept"])
    assert templatestore.install("odooplm-20", published["config"])["version"] == "2026.1.6"
    assert sorted(p.name for p in published["installed"].iterdir()) == ["odooplm-20"]


async def test_install_refuses_what_the_key_does_not_sign(published):
    entry = (await templatestore.available(published["source"]))[0]
    await templatestore.download(_job(), published["source"], entry, published["kept"])
    templatestore.install("odooplm-20", published["config"])
    kept = published["kept"] / "odooplm-20" / ARCHIVE
    # changed after the download: the root helper's check refuses it, the installed one stays
    kept.write_bytes(kept.read_bytes() + b"x")
    with pytest.raises(pluginstore.StoreError, match="not the file"):
        templatestore.install("odooplm-20", published["config"])
    assert stacktemplates.load_template(published["installed"] / "odooplm-20").version == "2026.1.5"
    # an archive whose template.json says another version than the one signed
    published["publish"]("2026.1.7", signed_version="2026.1.8")
    entry = (await templatestore.available(published["source"]))[0]
    await templatestore.download(_job(), published["source"], entry, published["kept"])
    with pytest.raises(pluginstore.StoreError, match="holds version 2026.1.7, its manifest signs 2026.1.8"):
        templatestore.install("odooplm-20", published["config"])
    for name, message in (("../x", "not a template name"), ("missing", "no template missing")):
        with pytest.raises(pluginstore.StoreError, match=message):
            templatestore.install(name, published["config"])
    # a version not of numbers is not downloaded
    published["publish"]("dev")
    entry = (await templatestore.available(published["source"]))[0]
    assert "not of numbers" in entry["error"]


def test_the_helper_uses_the_higher_version(published, tmp_path, monkeypatch):
    monkeypatch.setattr(workspacehelper, "TEMPLATES", tmp_path / "package")
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("omniacli", 0, [0]))
    template_copy(tmp_path / "package", "2026.1.2")
    assert workspacehelper.template_named("odooplm-20").version == "2026.1.2"
    template_copy(published["installed"], "2026.1.9")
    assert workspacehelper.template_named("odooplm-20").version == "2026.1.9"
    monkeypatch.setattr(workspacehelper, "TEMPLATES", tmp_path / "none")
    assert workspaceops.templates()["odooplm-20"].version == "2026.1.9"
    with pytest.raises(workspacehelper.HelperError, match="no template nope"):
        workspacehelper.template_named("nope")


def test_template_pack_command(tmp_path, capsys):
    from omniacli.cli import main

    assert main(["template-pack", str(REPO_TEMPLATES / "odooplm-20"), str(tmp_path)]) == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["file"] == ARCHIVE and (tmp_path / ARCHIVE).is_file()
    assert main(["template-pack", str(tmp_path), str(tmp_path)]) == 1


async def test_install_from_the_workspaces_page(env, published, tmp_path, monkeypatch):
    settings = env["settings"]
    settings.plugin_source = published["source"]
    monkeypatch.setattr(workspacehelper, "TEMPLATES", REPO_TEMPLATES)
    monkeypatch.setattr(templatestore, "templates_dir", lambda jobs_dir: published["kept"])
    # the root helper, as sudo would run it: this config, the test's installed folder
    script = ("import sys; from pathlib import Path; from omniacli import templatestore; "
              "templatestore.INSTALLED = Path(sys.argv[3]); print(templatestore.install(sys.argv[1], Path(sys.argv[2])))")
    monkeypatch.setattr(workspaceops, "helper", lambda *args: [sys.executable, "-c", script, args[1],
                                                                str(published["config"]), str(published["installed"])])
    monkeypatch.setattr(templatestore, "_system_settings", lambda cfg: (published["public"], published["kept"]))
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url="http://omniacli.test") as client:
        page = await client.get("/workspaces", headers={**as_admin(), "Accept": "text/html"})
        assert page.status_code == 200 and "look-templates" in page.text and "omniacli-templates.json" in page.text
        data = (await client.get("/api/templates/available", headers=as_admin())).json()
        used = stacktemplates.load_template(REPO_TEMPLATES / "odooplm-20").version
        assert (data["templates"][0]["version"], data["templates"][0]["used"]) == ("2026.1.5", used)
        resp = await client.post("/api/templates/odooplm-20/install", headers=as_admin(post=True))
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job"]
        for _ in range(300):
            job = (await client.get(f"/api/jobs/{job_id}", headers=as_admin())).json()
            if job["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "success", job["lines"]
        assert job["result"] == {"template": "odooplm-20", "version": "2026.1.5"}
        assert (await client.post("/api/templates/nope/install", headers=as_admin(post=True))).status_code == 404
        assert (await client.post("/api/templates/other/install", headers=as_admin(post=True))).status_code == 400
