# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An instance installed from a signed Debian package (packages.py)."""

import os

import httpx
import pytest

from omniacli import ops, packages
from omniacli.checks import check_server
from omniacli.config import ConfigError, instance_to_dict, parse_servers
from omniacli.jobs import Job, JobFailed
from omniacli.packagesign import (SigningError, load_private_key, manifest_of, new_private_key, sign_bytes,
                                  verified_manifest)

URL = "https://download.example.com/download/api-server.deb"


def job():
    return Job(id="t", title="t", instances=[], source="test")


@pytest.fixture
def key():
    pem, public = new_private_key(b"pass")
    return load_private_key(pem, b"pass"), public


@pytest.fixture
def dpkg(monkeypatch, tmp_path):
    """dpkg-deb, dpkg-query and apt-get that read a ".deb" written as
    "Package: <name>\\nVersion: <version>\\n" and keep the installed version
    in a file; apt-get records its arguments."""
    bin_dir = tmp_path / "dpkg-bin"
    bin_dir.mkdir()
    installed = tmp_path / "installed"
    calls = tmp_path / "apt.log"
    (bin_dir / "dpkg-deb").write_text(
        '#!/bin/sh\n# dpkg-deb -f <deb> <field>\nsed -n "s/^$3: //p" "$2"\n')
    (bin_dir / "dpkg-query").write_text(
        f'#!/bin/sh\n[ -e {installed} ] || exit 1\nprintf "install ok installed|%s" "$(cat {installed})"\n')
    (bin_dir / "apt-get").write_text(
        f'#!/bin/sh\necho "$*" >> {calls}\nfor last; do :; done\n'
        f'sed -n "s/^Version: //p" "$last" > {installed}\n')
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    monkeypatch.setitem(ops._ENV, "PATH", f"{bin_dir}{os.pathsep}{ops._ENV.get('PATH', '')}")
    monkeypatch.setattr(packages, "APT_GET", str(bin_dir / "apt-get"))
    return {"installed": installed, "calls": calls}


@pytest.fixture
def published(monkeypatch, key, tmp_path):
    """The download server: publish(name, version) puts a package there with
    its signed manifest; manifest_version, served and sign_with play a server
    that lies."""
    files = {}

    def publish(name="api-server", version="1.0", sign_with=None, served=None, manifest_version=None):
        deb = tmp_path / "api-server.deb"
        deb.write_text(f"Package: {name}\nVersion: {version}\n")
        manifest = manifest_of(deb, manifest_version or version, name)
        files[URL] = served.encode() if served is not None else deb.read_bytes()
        files[URL + ".json"] = manifest
        files[URL + ".json.sig"] = sign_bytes(sign_with or key[0], manifest)
        return files

    def handler(request):
        body = files.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    monkeypatch.setattr(packages, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return publish


@pytest.fixture
def instance(tmp_path, key, restart_calls, dpkg, fake_sudo):
    """A generic instance installed from the package only."""
    log = tmp_path / "api.log"
    log.write_text("")
    data = {"srv": {"type": "generic", "production": {
        "log_file": str(log),
        "restart": {"type": "systemd", "service": "api", "sudo": False},
        "repositories": [],
        "package": {"url": URL, "name": "api-server", "public_key": key[1]},
    }}}
    return parse_servers(data, tmp_path / "logs")["srv"].production


def test_config_round_trip(instance, tmp_path):
    pkg = instance.package
    assert pkg.deb_path == tmp_path / "packages" / "srv_production.deb"
    assert instance_to_dict(instance)["package"] == {"url": URL, "name": "api-server",
                                                     "public_key": pkg.public_key}


@pytest.mark.parametrize("package, error", [
    ({"url": "http://download.example.com/a.deb"}, "https"),
    ({"url": "https://download.example.com/a.zip"}, "https"),
    ({"url": URL, "name": "Api Server"}, "Debian package name"),
    ({"url": URL, "name": "api-server", "public_key": "abc"}, "starts with ed25519:"),
    ({"url": URL, "name": "api-server", "public_key": "ed25519:AAAA"}, "32 bytes"),
])
def test_config_refuses(tmp_path, package, error):
    data = {"srv": {"type": "generic", "production": {
        "log_file": "/x.log", "restart": {"type": "systemd", "service": "api"}, "repositories": [],
        "package": package}}}
    with pytest.raises(ConfigError, match=error):
        parse_servers(data, tmp_path / "logs")


def test_config_without_url_is_no_package(tmp_path):
    data = {"srv": {"type": "generic", "production": {
        "log_file": "/x.log", "restart": {"type": "systemd", "service": "api"}, "repositories": [],
        "package": {"url": "", "name": "", "public_key": ""}}}}
    assert parse_servers(data, tmp_path / "logs")["srv"].production.package is None


def test_signature(key, tmp_path):
    deb = tmp_path / "api-server.deb"
    deb.write_text("x")
    manifest = manifest_of(deb, "1.0", "api-server")
    signature = sign_bytes(key[0], manifest)
    data = verified_manifest(key[1], manifest, signature, "api-server.deb")
    assert (data["version"], data["size"], data["package"]) == ("1.0", 1, "api-server")
    with pytest.raises(SigningError, match="does not verify"):
        verified_manifest(key[1], manifest.replace(b'"1.0"', b'"9.0"'), signature, "api-server.deb")
    with pytest.raises(SigningError, match="does not verify"):
        verified_manifest(new_private_key(b"x")[1], manifest, signature, "api-server.deb")
    # one key signs every file: a manifest stands only for the file it names
    with pytest.raises(SigningError, match="of 'api-server.deb', not 'api-client.deb'"):
        verified_manifest(key[1], manifest, signature, "api-client.deb")


async def test_install_new_then_nothing(instance, published, dpkg):
    published(version="1.1")
    assert await ops.pending_updates(instance) == {"package api-server 1.1": 1}
    assert await packages.update(job(), instance) is True
    assert dpkg["installed"].read_text().strip() == "1.1"
    assert "--force-confdef" in dpkg["calls"].read_text()
    assert packages.installed_state(instance.package)["version"] == "1.1"
    # the download is not kept
    assert not instance.package.deb_path.exists()
    assert await ops.pending_updates(instance) == {}
    assert await packages.update(job(), instance) is False
    assert len(dpkg["calls"].read_text().splitlines()) == 1


async def test_same_version_is_not_downloaded(instance, published, dpkg):
    dpkg["installed"].write_text("1.0\n")
    files = published(version="1.0")
    del files[URL]  # read from the manifest: the .deb is never asked for
    assert await ops.pending_updates(instance) == {}
    assert await packages.update(job(), instance) is False
    assert not dpkg["calls"].exists()


async def test_downgrade_refused(instance, published, dpkg):
    dpkg["installed"].write_text("1.2\n")
    files = published(version="1.1")
    del files[URL]
    with pytest.raises(JobFailed, match="older than the installed 1.2"):
        await ops.pending_updates(instance)
    with pytest.raises(JobFailed, match="older than the installed 1.2"):
        await packages.update(job(), instance)
    assert not dpkg["calls"].exists()


async def test_other_signer_refused(instance, published, dpkg):
    other = load_private_key(new_private_key(b"x")[0], b"x")
    published(version="9.9", sign_with=other)
    with pytest.raises(JobFailed, match="does not verify"):
        await ops.pending_updates(instance)
    with pytest.raises(JobFailed, match="does not verify"):
        await packages.update(job(), instance)
    assert not dpkg["calls"].exists()


async def test_changed_file_refused(instance, published, dpkg):
    published(version="1.1", served="Package: api-server\nVersion: 6.6\n")
    with pytest.raises(JobFailed, match="not the file the manifest signs"):
        await packages.update(job(), instance)
    assert not dpkg["calls"].exists()
    assert list(instance.package.folder.iterdir()) == []


async def test_version_not_the_manifest_s_refused(instance, published, dpkg):
    published(version="1.1", manifest_version="1.2")
    with pytest.raises(JobFailed, match="is version 1.1, its manifest says 1.2"):
        await packages.update(job(), instance)
    assert not dpkg["calls"].exists()


async def test_other_package_refused(instance, published, dpkg):
    published(name="api-client", version="1.1")
    with pytest.raises(JobFailed, match="is the package api-client, not api-server"):
        await packages.update(job(), instance)
    assert not dpkg["calls"].exists()


async def test_pull_restart_installs_and_restarts(instance, published, dpkg, restart_calls):
    published(version="1.1")
    await ops.pull_restart(job(), instance, only_if_changed=True)
    assert "restart api" in restart_calls.read_text()
    restart_calls.unlink()
    await ops.pull_restart(job(), instance, only_if_changed=True)
    assert not restart_calls.exists()


def test_sudoers_line(instance):
    line = packages.sudoers_line(instance.package, "omniacli")
    assert line.startswith("omniacli ALL=(root) NOPASSWD: ")
    assert ("install -y -o Dpkg\\:\\:Options\\:\\:\\=--force-confdef -o Dpkg\\:\\:Options\\:\\:\\=--force-confold "
            f"{instance.package.deb_path}") in line


async def test_checks(instance, published, fake_sudo, tmp_path):
    published(version="1.1")
    server = parse_servers({"srv": {"type": "generic", "production": instance_to_dict(instance)}},
                           tmp_path / "logs")["srv"]
    found = {c.name: c for c in await check_server(server, tmp_path / "backups")}
    assert found["Package"].ok, found["Package"].detail
    assert found["Package folder"].ok
    assert found["Package install permission"].ok
    assert not found["Installed package"].ok and "first update installs it" in found["Installed package"].detail
