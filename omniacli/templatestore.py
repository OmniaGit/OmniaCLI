# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The templates of the workspaces' instances published for download, as
the plugins are (pluginstore.py), next to their index and with their key.

The publisher (OmniaConfigurator3's Setup/release.py, target templates)
puts on the download server, next to the plugins' index:

- the index, omniacli-templates.json: {"format": 1, "templates": [{"name",
  "label", "description", "file"}]}, with its signed manifest;
- each template's archive, omniacli-template-<name>.tar.gz
  (stacktemplates.pack), with its signed manifest, whose version is the
  template's own (template.json).

OmniaCLI believes them only when they verify with plugins.public_key of the
main configuration. Installed in two steps: the service downloads the
archive into templates/<name>/ next to the jobs, checked against its signed
manifest; then the root helper (workspace-helper template-install <name>)
copies it into a folder of root's, checks it again with the key it reads
itself from the main configuration, unpacks its three files, loads the
template (its compose rules) and puts it in INSTALLED/<name>, root's. Of a
template there and in the package (/usr/share/omniacli/templates) the
higher version is used; the instances made before keep their own files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import yaml

from . import stacktemplates
from .config import PluginSource
from .jobs import Job, JobFailed
from .pluginstore import (MAX_INDEX, SYSTEM_CONFIG, StoreError, _client, _get, _signed, copy_signed,
                          download_signed)

INDEX_NAME = "omniacli-templates.json"
INDEX_FORMAT = 1
# the templates installed from the download server: root's (sudo does not
# pass the environment on; OMNIACLI_INSTALLED_TEMPLATES is for the tests)
INSTALLED = Path(os.environ.get("OMNIACLI_INSTALLED_TEMPLATES", "/var/lib/omniacli-templates"))


def index_url(source: PluginSource) -> str:
    """Next to the plugins' index."""
    return urljoin(source.index, INDEX_NAME)


def templates_dir(jobs_dir: Path) -> Path:
    """Where the downloaded archives are kept: next to the jobs."""
    return Path(jobs_dir).parent / "templates"


def _version_of(manifest: dict, url: str) -> str:
    if not stacktemplates.version_key(manifest["version"]):
        raise StoreError(f"{url}: the signed version {manifest['version']!r} is not of numbers (2026.1.1)")
    return manifest["version"]


async def available(source: PluginSource) -> list[dict]:
    """The templates of the index, each with its signed version; StoreError
    when the index itself cannot be believed. One whose manifest does not
    verify comes with error."""
    if not source.public_key:
        raise StoreError("no plugins.public_key in the main configuration: no published template can be checked")
    url = index_url(source)
    async with _client() as client:
        manifest, _, _ = await _signed(client, source.public_key, url)
        index = await _get(client, url, MAX_INDEX)
        if len(index) != manifest["size"] or hashlib.sha256(index).hexdigest() != manifest["sha256"]:
            raise StoreError(f"{url} is not the index its manifest signs")
        try:
            data = json.loads(index)
        except ValueError:
            raise StoreError(f"{url} is not JSON") from None
        if not isinstance(data, dict) or data.get("format") != INDEX_FORMAT or not isinstance(data.get("templates"), list):
            raise StoreError(f"{url} is not an index of templates of format {INDEX_FORMAT}")
        found = []
        for entry in data["templates"]:
            entry = entry if isinstance(entry, dict) else {}
            name, file = str(entry.get("name") or ""), str(entry.get("file") or "")
            item = {"name": name, "label": str(entry.get("label") or name),
                    "description": str(entry.get("description") or ""), "file": file}
            if not stacktemplates.NAME.fullmatch(name) or file != stacktemplates.archive_name(name):
                item["error"] = "the index names it wrongly"
                found.append(item)
                continue
            item["url"] = urljoin(url, file)
            try:
                signed, _, _ = await _signed(client, source.public_key, item["url"])
                item.update(version=_version_of(signed, item["url"]), size=signed["size"], built=signed.get("built"))
            except StoreError as exc:
                item["error"] = str(exc)
            found.append(item)
    return found


async def download(job: Job, source: PluginSource, template: dict, folder: Path) -> dict:
    """The template's archive and its signed manifest into folder/<name>/;
    its verified manifest."""
    name, url = template["name"], template["url"]
    if not stacktemplates.NAME.fullmatch(name):
        raise JobFailed(f"{name!r} is not a template name")
    return await download_signed(job, source.public_key, url, folder / name, lambda m: _version_of(m, url))


# --- the root helper ----------------------------------------------------------


def _system_settings(config: Path) -> tuple[str, Path]:
    """The public key and the folder of the downloaded templates, from the main configuration."""
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
    return key, templates_dir(Path(jobs_dir))


def install(name: str, config: Path = SYSTEM_CONFIG) -> dict:
    """The template kept in templates/<name>/ into INSTALLED/<name>, once its
    copy checks with the key and loads as a template of that name and version."""
    if not stacktemplates.NAME.fullmatch(name):
        raise StoreError(f"{name!r} is not a template name")
    key, folder = _system_settings(config)
    source = folder / name
    if source.is_symlink() or not source.is_dir():
        raise StoreError(f"no template {name} in {folder}")
    INSTALLED.mkdir(mode=0o755, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="omniacli-template-"))
    new = INSTALLED / f".{name}.new"
    old = INSTALLED / f".{name}.old"
    try:
        copy, manifest = copy_signed(source / stacktemplates.archive_name(name), staging, key)
        for leftover in (new, old):
            shutil.rmtree(leftover, ignore_errors=True)
        new.mkdir(mode=0o755)
        try:
            stacktemplates.unpack(copy, new / name)
            template = stacktemplates.load_template(new / name)
        except stacktemplates.TemplateError as exc:
            raise StoreError(str(exc)) from None
        if template.version != manifest["version"]:
            raise StoreError(f"{name}: the archive holds version {template.version}, its manifest signs "
                             f"{manifest['version']}")
        target = INSTALLED / name
        if target.is_symlink():
            target.unlink()
        if target.exists():
            target.rename(old)
        (new / name).rename(target)
        shutil.rmtree(old, ignore_errors=True)
        return {"template": name, "version": template.version, "built": manifest.get("built"),
                "folder": str(target)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(new, ignore_errors=True)
