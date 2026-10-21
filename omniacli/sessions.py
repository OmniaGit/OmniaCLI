# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser sessions after the login form, and the limit on failed logins.

A session is a random token in an HttpOnly cookie; the server keeps only its
SHA-256, with the user, the end of its LIFETIME and a fingerprint of the
user's password hash: a changed password or a deleted user ends every
session of that user at once. The sessions are kept in a file (mode 600), so
that a restart of OmniaCLI does not log everybody out.

Failed logins are counted per address and per user name: from the
LIMIT-th failure within WINDOW, the next try must wait, longer each time.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

COOKIE = "omniacli_session"
LIFETIME = 8 * 3600  # seconds from the login, whatever is done meanwhile

LIMIT = 5  # failed logins within WINDOW before the waits start
WINDOW = 15 * 60
FIRST_WAIT = 30  # seconds, doubled at every further failure
MAX_WAIT = 15 * 60


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._sessions = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass  # none yet, or unreadable: everybody logs in again

    def _save(self) -> None:
        """Write the live sessions; the caller holds the lock."""
        now = time.time()
        self._sessions = {k: v for k, v in self._sessions.items() if v.get("expires", 0) > now}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._sessions, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # the sessions still work until a restart

    def create(self, user: str, password_hash: str) -> str:
        """A new session of user: the token for the cookie."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[_digest(token)] = {
                "user": user, "expires": time.time() + LIFETIME, "pw": _digest(password_hash)[:32],
            }
            self._save()
        return token

    def user(self, token: str | None, users: dict[str, str]) -> str | None:
        """The user of a live session: None when it ended, its password
        changed or the user is gone."""
        if not token:
            return None
        found = self._sessions.get(_digest(token))
        if found is None or found.get("expires", 0) <= time.time():
            return None
        current = users.get(found.get("user", ""))
        if current is None or not secrets.compare_digest(_digest(current)[:32], found.get("pw", "")):
            return None
        return found["user"]

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if self._sessions.pop(_digest(token), None) is not None:
                self._save()


class LoginLimiter:
    """The failed logins of the last WINDOW, per address and per user name."""

    def __init__(self):
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque:
        times = self._failures[key]
        while times and now - times[0] > WINDOW:
            times.popleft()
        return times

    def wait(self, ip: str, user: str) -> int:
        """Seconds before the next try is allowed; 0: allowed now."""
        now = time.time()
        longest = 0.0
        with self._lock:
            for key in (f"ip:{ip}", f"user:{user.lower()}"):
                times = self._recent(key, now)
                if len(times) >= LIMIT:
                    pause = min(FIRST_WAIT * 2 ** (len(times) - LIMIT), MAX_WAIT)
                    longest = max(longest, times[-1] + pause - now)
        return max(0, int(longest + 0.999))

    def failed(self, ip: str, user: str) -> None:
        now = time.time()
        with self._lock:
            for key in (f"ip:{ip}", f"user:{user.lower()}"):
                self._recent(key, now).append(now)

    def succeeded(self, ip: str, user: str) -> None:
        with self._lock:
            self._failures.pop(f"ip:{ip}", None)
            self._failures.pop(f"user:{user.lower()}", None)
