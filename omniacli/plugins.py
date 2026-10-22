# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find the plugins, check each against the contract, keep those that pass.

A plugin that fails (it cannot be imported, breaks the contract, its
self_test finds a problem, its name is taken) is kept as a failure with the
reason, shown on the Plugins page: OmniaCLI starts without it, and the
servers of its type stay in the servers file, without actions.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import re
from dataclasses import dataclass, field

from .plugin_api import ENTRY_POINT_GROUP, ServicePlugin, contract_problems, parts_problems

_logger = logging.getLogger(__name__)

# the type of a server whose mapping names none: every server before plugins
DEFAULT_TYPE = "odoo"
# the plugins that come inside the omniacli package: the generic type (git
# and a service, nothing else: what an install without plugins has) and Odoo
BUILTIN = ("omniacli.generic:GenericPlugin", "omniacli.odoo:OdooPlugin")
PLUGIN_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


@dataclass
class PluginInfo:
    """A plugin found: loaded (plugin set) or failed (error set)."""

    name: str
    target: str  # module:class
    source: str  # "built-in" or the distribution that declares it
    version: str = ""
    plugin: ServicePlugin | None = None
    error: str | None = None


@dataclass
class Registry:
    found: list[PluginInfo] = field(default_factory=list)

    @property
    def plugins(self) -> dict[str, ServicePlugin]:
        return {i.name: i.plugin for i in self.found if i.plugin is not None}

    def get(self, name: str) -> ServicePlugin | None:
        return self.plugins.get(name)

    def why_missing(self, name: str) -> str:
        """Why no plugin serves the type name."""
        failed = next((i for i in self.found if i.name == name and i.error), None)
        if failed:
            return f"the plugin {name} is disabled: {failed.error}"
        return f"no plugin {name} is installed"


def _load_target(target: str):
    module, _, attr = target.partition(":")
    obj = importlib.import_module(module)
    for part in attr.split(".") if attr else []:
        obj = getattr(obj, part)
    return obj


def _check(info: PluginInfo, cls, taken: set[str]) -> None:
    """Make the plugin of info from cls, or set info.error."""
    problems = contract_problems(cls)
    if not problems:
        name = getattr(cls, "name", "")
        if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name):
            problems.append(f"name {name!r} is not lower case letters, digits and _ (at most 32)")
        elif name != info.name:
            problems.append(f"the entry point is {info.name} but the plugin's name is {name}")
        if not isinstance(getattr(cls, "label", None), str) or not cls.label.strip():
            problems.append("label missing")
    if not problems and info.name in taken:
        problems.append(f"another plugin already serves the type {info.name}")
    if not problems:
        try:
            plugin = cls()
            problems = parts_problems(plugin) + [str(p) for p in plugin.self_test()]
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop OmniaCLI
            problems = [f"{type(exc).__name__}: {exc}"]
    if problems:
        info.error = "; ".join(problems)
        _logger.warning("Plugin %s (%s) disabled: %s", info.name, info.target, info.error)
    else:
        info.plugin = plugin
        taken.add(info.name)


def _own_version() -> str:
    try:
        return importlib.metadata.version("omniacli")
    except importlib.metadata.PackageNotFoundError:
        return ""


def load(builtin=BUILTIN, entry_points=None) -> Registry:
    """The built-in plugins, then those of the installed packages
    (entry_points: what importlib.metadata.entry_points gives, for the tests)."""
    registry, taken = Registry(), set()
    for target in builtin:
        info = PluginInfo(name=target.rpartition(":")[0].rpartition(".")[2], target=target,
                          source="built-in", version=_own_version())
        try:
            cls = _load_target(target)
        except Exception as exc:  # noqa: BLE001
            info.error = f"cannot import: {type(exc).__name__}: {exc}"
            registry.found.append(info)
            continue
        info.name = getattr(cls, "name", "") or info.name
        _check(info, cls, taken)
        registry.found.append(info)
    if entry_points is None:
        try:
            entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except TypeError:
            # Python 3.9: a dict of the groups
            entry_points = importlib.metadata.entry_points().get(ENTRY_POINT_GROUP, [])
    for ep in entry_points:
        dist = getattr(ep, "dist", None)
        info = PluginInfo(name=ep.name, target=ep.value,
                          source=dist.metadata["Name"] if dist is not None else "unknown package",
                          version=dist.version if dist is not None else "")
        try:
            cls = ep.load()
        except Exception as exc:  # noqa: BLE001
            info.error = f"cannot import: {type(exc).__name__}: {exc}"
        else:
            _check(info, cls, taken)
        registry.found.append(info)
    return registry


_registry: Registry | None = None


def registry() -> Registry:
    """The plugins of this process, found at the first call."""
    global _registry
    if _registry is None:
        _registry = load()
    return _registry


def set_registry(value: Registry | None) -> None:
    """Replace the plugins (tests); None finds them again at the next call."""
    global _registry
    _registry = value
