# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shell sandboxes of the users on a server, through their root helper
(sandboxhelper.py): made at a user's first shell, disabled, enabled again
and deleted with it."""

from __future__ import annotations

import json

from .config import OMNIACLI_COMMAND
from .ops import capture
from .workspaceops import helpers_refused

# making a disk takes a mkfs
TIMEOUT = 120
# what an admin does when sudo refuses the helper (README, Shell)
HINT = ("sudo does not allow the shell sandboxes' helper to OmniaCLI's user: as root, run "
        "dpkg-reconfigure omniacli (it writes /etc/sudoers.d/omniacli-sandbox)")


class SandboxError(Exception):
    """The helper failed; refused: sudo itself refused it (no sudoers line)."""

    def __init__(self, message: str, refused: bool = False):
        super().__init__(message)
        self.refused = refused


def helper(*args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "sandbox-helper", *args]


async def _call(*args: str) -> dict:
    code, out = await capture(helper(*args), timeout=TIMEOUT)
    if code:
        message = (out or "").strip() or f"sandbox-helper {args[0]} failed ({code})"
        if helpers_refused(message):
            raise SandboxError(f"{message} ({HINT})", refused=True)
        raise SandboxError(message.removeprefix("sandbox-helper: "))
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise SandboxError(f"sandbox-helper {args[0]} answered no JSON: {out}") from None


async def ensure(user: str, mb: int) -> dict:
    """{"user", "home", "mb"} of user's sandbox, made or grown to mb."""
    return await _call("ensure", user, str(mb))


async def disable(user: str) -> dict:
    return await _call("disable", user)


async def enable(user: str) -> dict:
    return await _call("enable", user)


async def delete(user: str) -> dict:
    return await _call("delete", user)
