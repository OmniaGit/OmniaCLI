# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A plain SQL dump (pg_dump --format=plain) to restore: what the upload,
the backup list and the restore of a plugin with restores_sql accept
besides a backup zip. It is told by its head, not by its name: a dump
uploaded as 17.11.2026.bck is kept as 17.11.2026.bck.sql."""

from __future__ import annotations

import re
from pathlib import Path

SUFFIX = ".sql"
# a file of the backups that a restore takes: a zip or a SQL dump
RESTORE_NAME = re.compile(r"[A-Za-z0-9._-]+\.(zip|sql)")
# the line that opens every dump of pg_dump (pg_dumpall writes "-- PostgreSQL
# database cluster dump", and a dump of every database is refused)
TITLE = b"-- PostgreSQL database dump"
CUSTOM_MAGIC = b"PGDMP"
HEAD_BYTES = 64 << 10


def is_dump_name(name: str) -> bool:
    return name.endswith(SUFFIX)


def stored_name(name: str) -> str:
    """The name a SQL dump is kept under in the backups."""
    return name if is_dump_name(name) else name + SUFFIX


def problem(path: Path, name: str | None = None) -> str | None:
    """Why the file is not a plain SQL dump of one database (None: it is),
    told by its head: the comment lines pg_dump writes first. name: the
    one to tell (default: the file's)."""
    name = name or path.name
    try:
        with open(path, "rb") as source:
            head = source.read(HEAD_BYTES)
    except OSError as exc:
        return f"{name} cannot be read: {exc}"
    if head.startswith(CUSTOM_MAGIC):
        return (f"{name} is a pg_dump of the custom format (pg_restore's): a plain SQL dump "
                "(pg_dump --format=plain) is asked")
    for line in head.splitlines():
        if line.rstrip(b"\r") == TITLE:
            return None
        if line.strip() and not line.startswith(b"--"):
            break
    return (f"{name} is not a zip nor a plain SQL dump of pg_dump (its head has no line "
            f"\"{TITLE.decode()}\")")
