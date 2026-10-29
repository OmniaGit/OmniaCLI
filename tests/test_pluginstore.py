# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugins published for download: the signed index, the download, the root
helper's install into a venv (a real pip, in a throwaway venv), the Plugins
page."""

import asyncio
import base64
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import pluginstore
from omniacli.app import create_app
from omniacli.config import ConfigError, PluginSource, parse_plugin_source
from omniacli.jobs import Job, JobFailed
from omniacli.packagesign import load_private_key, new_private_key, sign_file

from .conftest import PASSWORD, BasicAsSession

BASE = "https://download.test/download"
INDEX = f"{BASE}/omniacli-plugins.json"
PUBLISHED = "omniacli-plugin-demo.whl"


def make_wheel(folder: Path, version: str) -> Path:
    """A plugin's wheel, as pip wheel would make it: one module and its entry point."""
    name = f"omniacli_demo-{version}-py3-none-any.whl"
    info = f"omniacli_demo-{version}.dist-info"
    files = {
        "omniacli_demo/__init__.py": f"VERSION = {version!r}\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: omniacli-demo\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": "[omniacli.plugins]\ndemo = omniacli_demo:Plugin\n",
    }
    record = "".join(f"{path},,\n" for path in files) + f"{info}/RECORD,,\n"
    path = folder / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, text in files.items():
            zf.writestr(member, text)
        zf.writestr(f"{info}/RECORD", record)
    return path


class Site:
    """The download server: the files of folder, by name; changed: {name: bytes} served instead."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.changed = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name in self.changed:
            return httpx.Response(200, content=self.changed[name])
        path = self.folder / name
        return httpx.Response(200, content=path.read_bytes()) if path.is_file() else httpx.Response(404)


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A key, a signed wheel of version 1.0 and a signed index, on the site."""
    pem, public = new_private_key(b"pass")
    key = load_private_key(pem, b"pass")
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    def publish(version: str):
        built = make_wheel(tmp_path, version)
        target = site_dir / PUBLISHED
        target.write_bytes(built.read_bytes())
        sign_file(key, target, version, built.name)
    publish("1.0")
    index = site_dir / "omniacli-plugins.json"
    index.write_text(json.dumps({"format": 1, "plugins": [
        {"name": "demo", "label": "Demo", "description": "a test plugin", "file": PUBLISHED},
        {"name": "Bad name", "file": "x.whl"}]}))
    sign_file(key, index, "2026-10-27")
    site = Site(site_dir)
    monkeypatch.setattr(pluginstore, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(site.handler)))
    return {"public": public, "key": key, "site": site, "publish": publish, "source": PluginSource(INDEX, public)}


def _job():
    return Job(id="t", title="t", instances=["omniacli/plugins"], source="test")


def test_parse_plugin_source():
    assert parse_plugin_source(None).index.endswith("/download/omniacli-plugins.json")
    with pytest.raises(ConfigError):
        parse_plugin_source({"index": "ftp://x/y"})
    with pytest.raises(ConfigError):
        parse_plugin_source({"public_key": "ed25519:short"})


# --- the index ---------------------------------------------------------------

async def test_the_index_lists_the_signed_plugins(published):
    found = await pluginstore.available(published["source"])
    demo, bad = found
    assert (demo["name"], demo["version"], demo["wheel"]) == ("demo", "1.0", "omniacli_demo-1.0-py3-none-any.whl")
    assert demo["url"] == f"{BASE}/{PUBLISHED}" and "error" not in demo
    assert bad["error"] == "the index names it wrongly"


async def test_an_index_the_key_does_not_sign_is_refused(published):
    other = PluginSource(INDEX, new_private_key(b"x")[1])
    with pytest.raises(pluginstore.StoreError, match="does not verify"):
        await pluginstore.available(other)
    with pytest.raises(pluginstore.StoreError, match="no plugins.public_key"):
        await pluginstore.available(PluginSource(INDEX, ""))
    # the index changed on the server, its manifest not
    published["site"].changed["omniacli-plugins.json"] = b'{"format": 1, "plugins": []}'
    with pytest.raises(pluginstore.StoreError, match="not the index its manifest signs"):
        await pluginstore.available(published["source"])


async def test_a_plugin_whose_manifest_lies_is_shown_with_its_error(published):
    site = published["site"]
    # the manifest of the index put in place of the plugin's
    site.changed[PUBLISHED + ".json"] = (site.folder / "omniacli-plugins.json.json").read_bytes()
    site.changed[PUBLISHED + ".json.sig"] = (site.folder / "omniacli-plugins.json.json.sig").read_bytes()
    demo = (await pluginstore.available(published["source"]))[0]
    assert "is of 'omniacli-plugins.json'" in demo["error"]


# --- the download --------------------------------------------------------------

async def test_the_download_keeps_one_version_checked(published, tmp_path):
    folder = tmp_path / "plugins"
    demo = (await pluginstore.available(published["source"]))[0]
    manifest = await pluginstore.download(_job(), published["source"], demo, folder)
    assert manifest["version"] == "1.0"
    assert sorted(p.name for p in (folder / "demo").iterdir()) == [PUBLISHED, PUBLISHED + ".json",
                                                                   PUBLISHED + ".json.sig"]
    published["publish"]("1.1")
    demo = (await pluginstore.available(published["source"]))[0]
    await pluginstore.download(_job(), published["source"], demo, folder)
    assert json.loads((folder / "demo" / (PUBLISHED + ".json")).read_text())["version"] == "1.1"
    assert len(list((folder / "demo").iterdir())) == 3


async def test_a_download_that_is_not_the_signed_file_leaves_the_old_one(published, tmp_path):
    folder = tmp_path / "plugins"
    demo = (await pluginstore.available(published["source"]))[0]
    await pluginstore.download(_job(), published["source"], demo, folder)
    before = (folder / "demo" / PUBLISHED).read_bytes()
    published["site"].changed[PUBLISHED] = b"x" * demo["size"]
    with pytest.raises(JobFailed, match="not the file its manifest signs"):
        await pluginstore.download(_job(), published["source"], demo, folder)
    assert (folder / "demo" / PUBLISHED).read_bytes() == before
    assert not list((folder / "demo").glob("*.part"))


# --- the root helper -----------------------------------------------------------

@pytest.fixture(scope="module")
def venv(tmp_path_factory):
    """A throwaway venv with pip, the one the helper installs into."""
    path = tmp_path_factory.mktemp("venv") / "v"
    subprocess.run([sys.executable, "-m", "venv", str(path)], check=True, capture_output=True)
    return path / "bin" / "python"


def system_config(tmp_path: Path, public: str) -> Path:
    config = tmp_path / "omniacli.yaml"
    config.write_text(yaml.safe_dump({"jobs_dir": str(tmp_path / "jobs"),
                                      "plugins": {"index": INDEX, "public_key": public}}))
    return config


def entry_points(python: Path) -> str:
    return subprocess.run([str(python), "-c", "import importlib.metadata as m; "
                           "print(sorted(e.value for e in m.entry_points(group='omniacli.plugins')))"],
                          check=True, capture_output=True, text=True).stdout.strip()


async def test_the_helper_installs_only_what_the_key_signs(published, tmp_path, venv):
    config = system_config(tmp_path, published["public"])
    folder = pluginstore.plugins_dir(tmp_path / "jobs")
    demo = (await pluginstore.available(published["source"]))[0]
    await pluginstore.download(_job(), published["source"], demo, folder)
    done = pluginstore.install("demo", config, str(venv))
    assert done.startswith("omniacli_demo-1.0-py3-none-any.whl (demo 1.0, signed")
    assert entry_points(venv) == "['omniacli_demo:Plugin']"
    # a wheel changed after the download: refused, nothing installed
    wheel = folder / "demo" / PUBLISHED
    data = bytearray(wheel.read_bytes())
    data[-30] ^= 1
    wheel.write_bytes(bytes(data))
    with pytest.raises(pluginstore.StoreError, match="not the file the manifest signs"):
        pluginstore.install("demo", config, str(venv))
    # another key in the main configuration: refused
    with pytest.raises(pluginstore.StoreError, match="does not verify"):
        pluginstore.install("demo", system_config(tmp_path, new_private_key(b"y")[1]), str(venv))


def test_nothing_downloaded_nothing_said(tmp_path, venv):
    # postinst on a machine that never installed a plugin: silent, key or not
    assert pluginstore.install_all(system_config(tmp_path, ""), str(venv)) == []


def test_the_helper_refuses_names_links_and_a_config_without_key(tmp_path, venv):
    config = system_config(tmp_path, "")
    with pytest.raises(pluginstore.StoreError, match="no plugins.public_key"):
        pluginstore.install("demo", config, str(venv))
    config = system_config(tmp_path, new_private_key(b"z")[1])
    for name in ("../demo", "Demo", ""):
        with pytest.raises(pluginstore.StoreError, match="not a plugin name"):
            pluginstore.install(name, config, str(venv))
    folder = pluginstore.plugins_dir(tmp_path / "jobs")
    (folder / "elsewhere").mkdir(parents=True)
    (folder / "demo").symlink_to(folder / "elsewhere")
    with pytest.raises(pluginstore.StoreError, match="no plugin demo"):
        pluginstore.install("demo", config, str(venv))
    (folder / "real").mkdir()
    (tmp_path / "evil.whl").write_bytes(b"x")
    (folder / "real" / PUBLISHED).symlink_to(tmp_path / "evil.whl")
    with pytest.raises(pluginstore.StoreError, match="0 wheels"):
        pluginstore.install("real", config, str(venv))
    # every one tried, none stopping the others
    assert pluginstore.install_all(config, str(venv)) == [
        f"plugin demo NOT installed: no plugin demo in {folder}",
        f"plugin elsewhere NOT installed: {folder / 'elsewhere'}: 0 wheels, not one",
        f"plugin real NOT installed: {folder / 'real'}: 0 wheels, not one"]


def test_the_command_line_helper(tmp_path, monkeypatch, capsys):
    from omniacli import cli

    monkeypatch.setattr(pluginstore, "install", lambda name: f"{name} done")
    monkeypatch.setattr(pluginstore, "install_all", lambda: ["installed a", "installed b"])
    assert cli.main(["plugin-install", "demo"]) == 0 and capsys.readouterr().out == "installed demo done\n"
    assert cli.main(["plugin-install", "--all"]) == 0
    assert capsys.readouterr().out == "omniacli: installed a\nomniacli: installed b\n"
    assert cli.main(["plugin-install"]) == 2
    # no option to name another configuration: the key is root's own
    with pytest.raises(SystemExit):
        cli.main(["plugin-install", "demo", "-c", "/tmp/other.yaml"])


# --- the Plugins page ------------------------------------------------------------

def as_admin(post=False):
    headers = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
    if post:
        headers["Origin"] = "http://omniacli.test"
    return headers


async def test_install_from_the_plugins_page(env, published, tmp_path, venv, monkeypatch):
    settings = env["settings"]
    settings.plugin_source = published["source"]
    config = system_config(tmp_path, published["public"])
    # the helper, as sudo would run it: into the throwaway venv, with this config
    script = "import sys; from pathlib import Path; from omniacli import pluginstore; " \
             "print(pluginstore.install(sys.argv[1], Path(sys.argv[2]), sys.argv[3]))"
    monkeypatch.setattr(pluginstore, "install_command",
                        lambda name: [sys.executable, "-c", script, name, str(config), str(venv)])
    # the service's plugins folder is the helper's (same jobs_dir)
    monkeypatch.setattr(pluginstore, "plugins_dir", lambda jobs_dir: tmp_path / "plugins")
    monkeypatch.setattr(pluginstore, "_system_settings",
                        lambda cfg: (yaml.safe_load(cfg.read_text())["plugins"]["public_key"], tmp_path / "plugins"))
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url="http://omniacli.test") as client:
        page = await client.get("/plugins", headers={**as_admin(), "Accept": "text/html"})
        assert page.status_code == 200 and "Published for download" in page.text
        assert "plugin-install *" in page.text
        data = (await client.get("/api/plugins/available", headers=as_admin())).json()
        assert [(p["name"], p.get("version"), p["installed"]) for p in data["plugins"]][0] == ("demo", "1.0", None)
        resp = await client.post("/api/plugins/demo/install", headers=as_admin(post=True))
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job"]
        for _ in range(300):
            job = (await client.get(f"/api/jobs/{job_id}", headers=as_admin())).json()
            if job["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "success", job["lines"]
        assert job["result"] == {"plugin": "demo", "version": "1.0", "restart": True}
        assert entry_points(venv) == "['omniacli_demo:Plugin']"
        assert (await client.post("/api/plugins/nope/install", headers=as_admin(post=True))).status_code == 404
        # not run by systemd here: no restart
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        resp = await client.post("/api/omniacli/restart", headers=as_admin(post=True))
        assert resp.status_code == 400 and "not run by systemd" in resp.json()["error"]
        exits = []
        monkeypatch.setenv("INVOCATION_ID", "x")
        monkeypatch.setattr("os._exit", exits.append)
        assert (await client.post("/api/omniacli/restart", headers=as_admin(post=True))).json() == {"ok": True}
        await asyncio.sleep(1.2)
        assert exits == [75]
