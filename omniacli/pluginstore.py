# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugins published for download, installed from the Plugins page.

The publisher (a release script) puts on the download server, next to each
other:

- the index, omniacli-plugins.json: {"format": 1, "plugins": [{"name",
  "label", "description", "file"}]}, with its signed manifest
  (packagesign.py: .json, .json.sig);
- each plugin's wheel under the name the index gives it (file, without a
  version: the link does not change), with its signed manifest, whose
  version is the plugin's and whose "package" is the wheel's own file name
  (pip installs a wheel only under it).

OmniaCLI believes an index and a manifest only when they verify with the
public key of the main configuration (plugins.public_key): whoever controls
the download server can withhold a plugin, not hand out one of his own nor
change its version.

A plugin is installed in two steps. The service (as its own user) downloads
the wheel into plugins/<name>/ next to the jobs, checked against its signed
manifest. Then the root helper, `omniacli plugin-install <name>` through
sudo, copies the three files into a folder of root's own, checks them again
with the key it reads itself from the main configuration (which only root
writes), and installs the copy into the venv. The service user can then
never get into the venv (which root's postinst runs) a file the key did not
sign. Every upgrade rebuilds the venv: postinst runs `plugin-install --all`
after it, which installs again every plugin kept there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from .config import PluginSource
from .jobs import Job, JobFailed
from .packagesign import MANIFEST_SUFFIX, MAX_SMALL_FILE, SIGNATURE_SUFFIX, SigningError, verified_manifest, verify_local

INDEX_FORMAT = 1
MAX_INDEX = 1 << 20
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 120.0
INSTALL_TIMEOUT = 600
# the main configuration the root helper reads its key from: fixed, sudo
# does not pass the environment on (OMNIACLI_SYSTEM_CONFIG is for the tests)
SYSTEM_CONFIG = Path(os.environ.get("OMNIACLI_SYSTEM_CONFIG", "/etc/omniacli/omniacli.yaml"))
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
PUBLISHED_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.whl")
WHEEL = re.compile(r"(?P<dist>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)(-[A-Za-z0-9_]+)?"
                   r"-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl")


class StoreError(Exception):
    pass


def plugins_dir(jobs_dir: Path) -> Path:
    """Where the downloaded plugins are kept: next to the jobs."""
    return Path(jobs_dir).parent / "plugins"


def helper() -> Path:
    """The omniacli command of this venv, which sudo runs as root."""
    return Path(sys.executable).parent / "omniacli"


def install_command(name: str) -> list[str]:
    return ["sudo", "-n", str(helper()), "plugin-install", name]


def sudoers_line(user: str) -> str:
    return f"{user} ALL=(root) NOPASSWD: {helper()} plugin-install *"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT), follow_redirects=False)


async def _get(client: httpx.AsyncClient, url: str, limit: int) -> bytes:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise StoreError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise StoreError(f"{url}: HTTP {response.status_code}")
    if len(response.content) > limit:
        raise StoreError(f"{url}: {len(response.content)} bytes, more than a {limit}-byte file of its kind")
    return response.content


async def _signed(client: httpx.AsyncClient, public_key: str, url: str) -> tuple[dict, bytes, bytes]:
    """The verified manifest of the file at url, and its bytes and signature as published."""
    manifest = await _get(client, url + MANIFEST_SUFFIX, MAX_SMALL_FILE)
    signature = await _get(client, url + SIGNATURE_SUFFIX, MAX_SMALL_FILE)
    try:
        return verified_manifest(public_key, manifest, signature, Path(urlsplit(url).path).name), manifest, signature
    except SigningError as exc:
        raise StoreError(f"{url}: {exc}") from exc


def _wheel_of(manifest: dict, url: str) -> str:
    wheel = str(manifest.get("package") or "")
    found = WHEEL.fullmatch(wheel)
    if not found or found["version"] != manifest["version"]:
        raise StoreError(f"{url}: the signed manifest names no wheel of version {manifest['version']} "
                         f"(package {wheel!r})")
    return wheel


async def available(source: PluginSource) -> list[dict]:
    """The plugins of the index, each with its signed version; StoreError
    when the index itself cannot be believed. A plugin whose manifest does
    not verify comes with error."""
    if not source.public_key:
        raise StoreError("no plugins.public_key in the main configuration: no published plugin can be checked")
    async with _client() as client:
        manifest, _, _ = await _signed(client, source.public_key, source.index)
        index = await _get(client, source.index, MAX_INDEX)
        if len(index) != manifest["size"] or hashlib.sha256(index).hexdigest() != manifest["sha256"]:
            raise StoreError(f"{source.index} is not the index its manifest signs")
        try:
            data = json.loads(index)
        except ValueError:
            raise StoreError(f"{source.index} is not JSON") from None
        if not isinstance(data, dict) or data.get("format") != INDEX_FORMAT or not isinstance(data.get("plugins"), list):
            raise StoreError(f"{source.index} is not an index of plugins of format {INDEX_FORMAT}")
        found = []
        for entry in data["plugins"]:
            entry = entry if isinstance(entry, dict) else {}
            name, file = str(entry.get("name") or ""), str(entry.get("file") or "")
            item = {"name": name, "label": str(entry.get("label") or name),
                    "description": str(entry.get("description") or ""), "file": file}
            if not NAME.fullmatch(name) or not PUBLISHED_FILE.fullmatch(file):
                item["error"] = "the index names it wrongly"
                found.append(item)
                continue
            item["url"] = urljoin(source.index, file)
            try:
                signed, _, _ = await _signed(client, source.public_key, item["url"])
                item.update(version=signed["version"], size=signed["size"], built=signed.get("built"),
                            wheel=_wheel_of(signed, item["url"]))
            except StoreError as exc:
                item["error"] = str(exc)
            found.append(item)
    return found


async def download(job: Job, source: PluginSource, plugin: dict, folder: Path) -> dict:
    """The plugin's wheel and its signed manifest into folder/<name>/, under
    the published name; its verified manifest."""
    name, url = plugin["name"], plugin["url"]
    if not NAME.fullmatch(name):
        raise JobFailed(f"{name!r} is not a plugin name")
    return await download_signed(job, source.public_key, url, folder / name, lambda manifest: _wheel_of(manifest, url))


async def download_signed(job: Job, public_key: str, url: str, target: Path, check=None) -> dict:
    """The file at url and its signed manifest into target (the only files
    kept there: one version), under the published name, once the bytes are
    the ones signed; its verified manifest. check(manifest) may refuse it
    (StoreError) before the download."""
    target.mkdir(parents=True, exist_ok=True)
    async with _client() as client:
        try:
            manifest, manifest_bytes, signature = await _signed(client, public_key, url)
            if check is not None:
                check(manifest)
        except StoreError as exc:
            raise JobFailed(str(exc)) from None
        dest = target / Path(urlsplit(url).path).name
        part = dest.with_name(dest.name + ".part")
        job.log(f"--- download {url} ({manifest['size']} bytes), version {manifest['version']}")
        digest, size = hashlib.sha256(), 0
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise JobFailed(f"{url}: HTTP {response.status_code}")
                with open(part, "wb") as fh:
                    async for chunk in response.aiter_bytes(1 << 20):
                        size += len(chunk)
                        if size > manifest["size"]:
                            raise JobFailed(f"{url}: larger than the {manifest['size']} bytes signed")
                        fh.write(chunk)
                        digest.update(chunk)
            if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
                raise JobFailed(f"{url}: not the file its manifest signs: not installed")
            # the old files go: one version is kept
            for old in target.iterdir():
                if old != part:
                    old.unlink()
            os.replace(part, dest)
            dest.with_name(dest.name + MANIFEST_SUFFIX).write_bytes(manifest_bytes)
            dest.with_name(dest.name + SIGNATURE_SUFFIX).write_bytes(signature)
        except httpx.HTTPError as exc:
            raise JobFailed(f"{url}: {type(exc).__name__}: {exc}") from exc
        finally:
            part.unlink(missing_ok=True)
    job.log(f"{size} bytes, sha256 {manifest['sha256']} as signed")
    return manifest


def copy_signed(path: Path, staging: Path, public_key: str) -> tuple[Path, dict]:
    """The file kept at path and its manifest and signature copied into
    staging, never through a link the service user may put there meanwhile;
    the copy and its manifest, verified with the key."""
    for suffix in ("", MANIFEST_SUFFIX, SIGNATURE_SUFFIX):
        src = path.with_name(path.name + suffix)
        try:
            fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise StoreError(f"{src}: {exc.strerror}") from None
        with os.fdopen(fd, "rb") as fh, open(staging / src.name, "wb") as out:
            shutil.copyfileobj(fh, out)
    copy = staging / path.name
    try:
        return copy, verify_local(public_key, copy)
    except SigningError as exc:
        raise StoreError(f"{path}: {exc}") from None


# --- the root helper ----------------------------------------------------------


def _system_settings(config: Path) -> tuple[str, Path]:
    """The public key and the plugins folder, from the main configuration."""
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise StoreError(f"cannot read {config}: {exc}") from None
    key = str((data.get("plugins") or {}).get("public_key") or "").strip()
    if not key:
        raise StoreError(f"no plugins.public_key in {config}")
    jobs_dir = data.get("jobs_dir")
    if not jobs_dir:
        raise StoreError(f"no jobs_dir in {config}")
    return key, plugins_dir(Path(jobs_dir))


def install(name: str, config: Path = SYSTEM_CONFIG, python: str = sys.executable) -> str:
    """Install the plugin kept in plugins/<name>/ into this venv, once its
    copy checks with the key; what was installed."""
    if not NAME.fullmatch(name):
        raise StoreError(f"{name!r} is not a plugin name")
    key, folder = _system_settings(config)
    source = folder / name
    if source.is_symlink() or not source.is_dir():
        raise StoreError(f"no plugin {name} in {folder}")
    wheels = [p for p in source.iterdir() if p.suffix == ".whl" and not p.is_symlink()]
    if len(wheels) != 1:
        raise StoreError(f"{source}: {len(wheels)} wheels, not one")
    staging = Path(tempfile.mkdtemp(prefix="omniacli-plugin-"))
    try:
        copy, manifest = copy_signed(wheels[0], staging, key)
        wheel = _wheel_of(manifest, str(wheels[0]))
        named = staging / wheel
        copy.rename(named)
        result = subprocess.run([python, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                 "--no-cache-dir", "--no-deps", "--no-index", "--force-reinstall", str(named)],
                                capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
        if result.returncode:
            raise StoreError(f"pip could not install {wheel}: {(result.stderr or result.stdout).strip()[-2000:]}")
        return f"{wheel} ({name} {manifest['version']}, signed, built {manifest.get('built')})"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_all(config: Path = SYSTEM_CONFIG, python: str = sys.executable) -> list[str]:
    """Every plugin kept, installed again (postinst, after a new venv):
    the lines to print; one that fails does not stop the others."""
    try:
        jobs_dir = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("jobs_dir")
    except (OSError, yaml.YAMLError) as exc:
        return [f"no plugin installed: cannot read {config}: {exc}"]
    folder = plugins_dir(Path(jobs_dir)) if jobs_dir else None
    names = sorted(p.name for p in folder.iterdir() if NAME.fullmatch(p.name)) \
        if folder is not None and folder.is_dir() else []
    lines = []
    for name in names:
        try:
            lines.append("plugin installed: " + install(name, config, python))
        except StoreError as exc:
            lines.append(f"plugin {name} NOT installed: {exc}")
    return lines
