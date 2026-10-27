# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An instance installed from a published Debian package (instance.package).

A check reads url.json and url.json.sig (packagesign.py): a manifest whose
signature does not verify with the instance's public key, or that is the
manifest of another file, is refused. Its version, compared with the
installed one before anything is downloaded: the same means nothing new, an
older one is refused (a server that offers an old signed package cannot
downgrade), a newer one is downloaded to the instance's fixed path, checked
against the manifest (sha256, size) and against itself (its Package is the
instance's name, its Version the manifest's), and installed with apt-get,
keeping the configuration files changed here (--force-confdef
--force-confold: without them dpkg stops on its question, having no
terminal).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import ops
from .config import Instance, Package
from .jobs import Job, JobFailed
from .packagesign import MANIFEST_SUFFIX, MAX_SMALL_FILE, SIGNATURE_SUFFIX, SigningError, verified_manifest

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 120.0
INSTALL_TIMEOUT = 900
APT_GET = "/usr/bin/apt-get"


def install_command(package: Package) -> list[str]:
    """The command sudoers must allow, exactly (sudoers_line)."""
    return ["sudo", "-n", APT_GET, "install", "-y",
            "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
            str(package.deb_path)]


def sudoers_line(package: Package, user: str) -> str:
    """install_command for /etc/sudoers.d: ':' and '=' of the arguments escaped."""
    args = " ".join(a.replace(":", "\\:").replace("=", "\\=") for a in install_command(package)[3:])
    return f"{user} ALL=(root) NOPASSWD: {APT_GET} {args}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                             follow_redirects=False)


async def _small(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise JobFailed(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise JobFailed(f"{url}: HTTP {response.status_code}")
    if len(response.content) > MAX_SMALL_FILE:
        raise JobFailed(f"{url}: {len(response.content)} bytes, not a manifest or a signature")
    return response.content


async def published(package: Package) -> dict:
    """The signed manifest of the published .deb: version, sha256, size..."""
    async with _client() as client:
        manifest = await _small(client, package.url + MANIFEST_SUFFIX)
        signature = await _small(client, package.url + SIGNATURE_SUFFIX)
    try:
        return verified_manifest(package.public_key, manifest, signature,
                                 Path(urlsplit(package.url).path).name)
    except SigningError as exc:
        raise JobFailed(f"{package.url}: {exc}") from exc


def installed_state(package: Package) -> dict:
    """{version, sha256, installed} of the package OmniaCLI installed last; {} if none."""
    try:
        data = json.loads(package.state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(package: Package, manifest: dict) -> None:
    package.folder.mkdir(parents=True, exist_ok=True)
    tmp = package.state_path.with_name(package.state_path.name + ".tmp")
    tmp.write_text(json.dumps({"version": manifest["version"], "sha256": manifest["sha256"],
                               "url": package.url,
                               "installed": datetime.datetime.now().isoformat(timespec="seconds")},
                              indent=2), encoding="utf-8")
    os.replace(tmp, package.state_path)


async def installed_version(name: str) -> str | None:
    code, out = await ops.capture(["dpkg-query", "-W", "-f=${Status}|${Version}", name])
    if code or "|" not in out:
        return None
    status, version = out.split("|", 1)
    return version if status.endswith(" installed") else None


async def _older(version: str, than: str) -> bool:
    code, _ = await ops.capture(["dpkg", "--compare-versions", version, "lt", than])
    return code == 0


async def _compare(package: Package) -> tuple[dict, str | None, bool]:
    """(the signed manifest, the installed version, True when the published
    one is to be installed); JobFailed when it is older than the installed."""
    manifest = await published(package)
    current = await installed_version(package.name)
    if current == manifest["version"]:
        return manifest, current, False
    if current and await _older(manifest["version"], current):
        raise JobFailed(f"{package.url} is version {manifest['version']}, older than the installed "
                        f"{current}: a downgrade is not installed")
    return manifest, current, True


async def pending(instance: Instance) -> dict[str, int]:
    """{"package <name> <version>": 1} when a newer version is published, else {}."""
    package = instance.package
    if package is None:
        return {}
    manifest, _, new = await _compare(package)
    return {f"package {package.name} {manifest['version']}": 1} if new else {}


async def _download(job: Job, package: Package, manifest: dict) -> None:
    """The .deb to package.deb_path, through .part: a cut download or one
    that is not the file the manifest signs leaves nothing there."""
    package.folder.mkdir(parents=True, exist_ok=True)
    part = package.deb_path.with_name(package.deb_path.name + ".part")
    digest, size = hashlib.sha256(), 0
    job.log(f"download {package.url} ({manifest['size']} bytes)")
    try:
        async with _client() as client, client.stream("GET", package.url) as response:
            if response.status_code != 200:
                raise JobFailed(f"{package.url}: HTTP {response.status_code}")
            with open(part, "wb") as fh:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > manifest["size"]:
                        raise JobFailed(f"{package.url}: larger than the {manifest['size']} bytes signed")
                    fh.write(chunk)
                    digest.update(chunk)
        if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
            raise JobFailed(f"{package.url}: {size} bytes, sha256 {digest.hexdigest()}: not the file the "
                            f"manifest signs ({manifest['size']} bytes, {manifest['sha256']}): not installed")
        part.chmod(0o644)
        os.replace(part, package.deb_path)
    except httpx.HTTPError as exc:
        raise JobFailed(f"{package.url}: {type(exc).__name__}: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)
    job.log(f"{size} bytes, sha256 {manifest['sha256']} as signed")


async def _field(deb: Path, name: str) -> str:
    code, out = await ops.capture(["dpkg-deb", "-f", deb, name])
    if code or not out:
        raise JobFailed(f"dpkg-deb cannot read {name} of {deb}: {out or 'empty'}")
    return out


async def update(job: Job, instance: Instance) -> bool:
    """Install the published package if it is newer; True when installed."""
    package = instance.package
    if package is None:
        return False
    job.log(f"--- [package {package.name}] check {package.url}")
    manifest, current, new = await _compare(package)
    job.log(f"[package {package.name}] published {manifest['version']} (signed, built {manifest.get('built')}), "
            f"installed {current or 'none'}")
    if not new:
        job.log(f"[package {package.name}] already installed")
        return False
    await _download(job, package, manifest)
    try:
        name = await _field(package.deb_path, "Package")
        if name != package.name:
            raise JobFailed(f"{package.url} is the package {name}, not {package.name}: not installed")
        version = await _field(package.deb_path, "Version")
        if version != manifest["version"]:
            raise JobFailed(f"{package.url} is version {version}, its manifest says {manifest['version']}: "
                            "not installed")
        await ops.run(job, install_command(package), timeout=INSTALL_TIMEOUT)
        now = await installed_version(package.name)
        if now != version:
            raise JobFailed(f"after apt-get {package.name} is {now or 'not installed'}, not {version}")
        _record(package, manifest)
        job.log(f"[package {package.name}] {version} installed")
        return True
    finally:
        package.deb_path.unlink(missing_ok=True)
