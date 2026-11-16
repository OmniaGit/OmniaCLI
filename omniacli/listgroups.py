# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The groups of the lists that offer them (the list widget's grouping:
templates/_list.html): the Dashboard's servers and its linked instances.

Each list has its own tree of groups, in order, in one file (groups.yaml):

    lists:
      servers:
        - name: Customers
          members: [acme-*, beta]
          groups:
            - name: Test machines
              members: ["*-test"]
      links:
        - name: Abroad
          members: [cliente]

A member is the name of an item of the list (a server, a link) or a pattern
of it (* any characters, ? one). An item goes under the first group that
has it, itself or in a subgroup, then under the first of its subgroups that
has it, and so on; one no group has goes under "Others".

The file of before (groups: [{name, members}], the servers of this instance
and of the links together) is read as the groups of both lists: a member
without a link (srv, *) the servers', a link's every server (cliente/*) its
link; a single server of a link no longer has a list here, and is dropped.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LISTS = ("servers", "links")
MAX_DEPTH = 4
_MEMBER = re.compile(r"[A-Za-z0-9*?][A-Za-z0-9._*?-]*")
_OLD_MEMBER = re.compile(r"(?:\*|[A-Za-z0-9][A-Za-z0-9._-]*)(?:/(?:\*|[A-Za-z0-9][A-Za-z0-9._-]*))?")


class GroupsError(ValueError):
    pass


@dataclass
class ListGroup:
    name: str
    members: list[str] = field(default_factory=list)
    groups: list["ListGroup"] = field(default_factory=list)

    def own(self, key: str) -> bool:
        return any(fnmatch.fnmatchcase(key, m) for m in self.members)

    def contains(self, key: str) -> bool:
        """Whether key is a member here or in a subgroup."""
        return self.own(key) or any(g.contains(key) for g in self.groups)

    def to_dict(self) -> dict:
        data = {"name": self.name, "members": list(self.members)}
        if self.groups:
            data["groups"] = [g.to_dict() for g in self.groups]
        return data


def parse_tree(data, where: str = "groups", depth: int = 1) -> list[ListGroup]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise GroupsError(f"{where} must be a list of {{name, members, groups}}")
    if depth > MAX_DEPTH:
        raise GroupsError(f"{where}: groups go {MAX_DEPTH} levels deep at most")
    groups, seen = [], set()
    for item in data:
        item = item if isinstance(item, dict) else {}
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 64:
            raise GroupsError(f"{where}: a group needs a name of at most 64 characters")
        if name in seen:
            raise GroupsError(f"{where}: two groups are named {name}")
        seen.add(name)
        members = [str(m).strip() for m in item.get("members") or [] if str(m).strip()]
        bad = [m for m in members if not _MEMBER.fullmatch(m)]
        if bad:
            raise GroupsError(f"group {name}: {', '.join(bad)} is not a name or a pattern (* and ?) of one")
        groups.append(ListGroup(name, list(dict.fromkeys(members)),
                                parse_tree(item.get("groups"), f"group {name}", depth + 1)))
    return groups


def parse_lists(data) -> dict[str, list[ListGroup]]:
    """{list: its groups} of a groups file's content."""
    data = data if isinstance(data, dict) else {}
    if "lists" in data:
        lists = data["lists"] if isinstance(data["lists"], dict) else {}
        unknown = sorted(set(lists) - set(LISTS))
        if unknown:
            raise GroupsError(f"lists: {', '.join(unknown)} is not one of {', '.join(LISTS)}")
        return {name: parse_tree(lists.get(name), name) for name in LISTS}
    return _from_old(data.get("groups") or [])


def _from_old(data) -> dict[str, list[ListGroup]]:
    if not isinstance(data, list):
        raise GroupsError("groups must be a list of {name, members}")
    servers, links = [], []
    for item in data:
        item = item if isinstance(item, dict) else {}
        name = str(item.get("name") or "").strip()
        members = [str(m).strip() for m in item.get("members") or [] if _OLD_MEMBER.fullmatch(str(m).strip())]
        own = [m for m in members if "/" not in m]
        whole_links = [m.split("/")[0] for m in members if m.endswith("/*") and not m.startswith("*/")]
        if name and own:
            servers.append({"name": name, "members": own})
        if name and whole_links:
            links.append({"name": name, "members": whole_links})
    return {"servers": parse_tree(servers, "servers"), "links": parse_tree(links, "links")}


def load(path: Path) -> dict[str, list[ListGroup]]:
    """The groups file; none yet is no groups."""
    if not path.exists():
        return {name: [] for name in LISTS}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GroupsError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_lists(data)
    except GroupsError as exc:
        raise GroupsError(f"{path}: {exc}") from exc


def save(path: Path, lists: dict[str, list[ListGroup]]) -> None:
    text = yaml.safe_dump({"lists": {name: [g.to_dict() for g in lists.get(name, [])] for name in LISTS}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sections(items: list, groups: list[ListGroup], key) -> list[dict]:
    """items arranged by groups for the list widget: [{name, items, groups,
    count}], in the groups' order, those with no item left out; then
    "Others", the items no group has (no name when there are no groups).
    key(item): the name the members match."""

    def place(found: list, tree: list[ListGroup]) -> tuple[list[dict], list]:
        out, rest = [], list(found)
        for group in tree:
            mine = [i for i in rest if group.contains(key(i))]
            rest = [i for i in rest if not group.contains(key(i))]
            sub, own = place(mine, group.groups)
            if mine:
                out.append({"name": group.name, "items": own, "groups": sub, "count": len(mine)})
        return out, rest

    placed, rest = place(items, groups)
    if rest or not placed:
        placed.append({"name": "Others" if placed else "", "items": rest, "groups": [], "count": len(rest)})
    return placed
