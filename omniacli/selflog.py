# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OmniaCLI's own log, in a file: omniacli.log in the logs folder, for the
Log page of the admins. What goes to the journal still goes there.

Run as the package's service, OmniaCLI logs to the journal only, which its
system user may not read (journalctl needs group systemd-journal or adm):
the file is what that page can always read. Past MAX_BYTES it moves to
omniacli.log.1 (the previous one is dropped), as the access log does.

The lines are written as Odoo writes them (date, pid, level), so the log
viewer of the instances shows them with their levels and its errors filter.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import weakref
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
FORMAT = "%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s"
# uvicorn's own loggers do not pass their records up to the root
LOGGERS = ("", "uvicorn", "uvicorn.access")


class _Once(logging.Filter):
    """A record once, though the handler is on several loggers of its way
    (uvicorn.access passes its records up when uvicorn did not set it up)."""

    def __init__(self):
        super().__init__()
        self.seen: weakref.WeakSet = weakref.WeakSet()

    def filter(self, record: logging.LogRecord) -> bool:
        if record in self.seen:
            return False
        self.seen.add(record)
        return True


def attach(path: Path) -> list[tuple[logging.Logger, logging.Handler]]:
    """Write the log to path too; detach() undoes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=1,
                                                   encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.setLevel(logging.INFO)
    handler.addFilter(_Once())
    added = []
    root = logging.getLogger()
    if not root.handlers:
        # without a handler of its own the root logger prints warnings on
        # stderr (logging.lastResort); one added here would end that
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setLevel(logging.WARNING)
        root.addHandler(stderr)
        added.append((root, stderr))
    for name in LOGGERS:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        added.append((logger, handler))
    # OmniaCLI's own information (tunnels, links) also when nothing else set
    # a level (the root logger's is WARNING)
    own = logging.getLogger("omniacli")
    if own.level == logging.NOTSET:
        own.setLevel(logging.INFO)
    return added


def detach(added: list[tuple[logging.Logger, logging.Handler]]) -> None:
    for logger, handler in added:
        logger.removeHandler(handler)
    for handler in {h for _, h in added}:
        handler.close()
