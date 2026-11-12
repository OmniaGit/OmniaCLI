# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backups are OmniaCLI's own: a zip holds a whole database, so no other
user of the machine may read it, wherever backup_dir is (the package's
/var/lib/omniacli/backups is 750, a backup_dir set elsewhere may not be).

The folders of an instance's backups (<backup_dir>/<server>/<role> and its
server's folder) are 700, every zip written there 600: made, downloaded
from a link or a relay, uploaded. A folder or file of another user (made by
hand) is left as it is: OmniaCLI may not change it."""

from __future__ import annotations

import os
from pathlib import Path

FOLDER_MODE = 0o700
FILE_MODE = 0o600


def _restrict(path: Path, mode: int) -> None:
    try:
        if path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
    except OSError:
        pass


def backup_folder(folder: Path) -> Path:
    """folder (an instance's backups) and its server's folder, made when
    missing, 700."""
    for level in (folder.parent, folder):
        if not level.is_dir():
            level.mkdir(parents=True, mode=FOLDER_MODE, exist_ok=True)
        _restrict(level, FOLDER_MODE)
    return folder


def new_file(path: Path):
    """path opened to write ("wb"), created 600 (emptied when it is there)."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE), "wb")


def backup_file(path: Path) -> Path:
    """path (a zip just written by a tool with its own mode) made 600."""
    _restrict(path, FILE_MODE)
    return path
