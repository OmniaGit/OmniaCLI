# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who logged in, when and from where: the access log.

A user's login and logout are those of the login form. A link or an MCP token
sends its token with every request, so its login is its first request after
SESSION_GAP without requests from the same address. A wrong password, a
login refused for too many failures and an unknown token are logged every
time. Every call to /mcp is logged, with its JSON-RPC method and tool.

One JSON object per line in access.jsonl, next to the instances' logs; past
MAX_BYTES the file moves to access.jsonl.1 (the previous one is dropped).
"""

from __future__ import annotations

import datetime
import json
import threading
import time
from collections import deque
from pathlib import Path

SESSION_GAP = 30 * 60  # seconds
MAX_BYTES = 5 * 1024 * 1024


class AccessLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seen: dict[tuple, float] = {}

    def record(self, event: str, kind: str, name: str, ip: str = "", detail: str = "") -> None:
        """event: login, logout, failed or mcp; kind: user, link, token or mcp."""
        entry = {
            "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event, "kind": kind, "name": name, "ip": ip, "detail": detail,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass  # the log must never stop a request

    def seen(self, kind: str, name: str, ip: str = "", detail: str = "") -> None:
        """A request that passed the login: logged when it starts a session."""
        key = (kind, name, ip, detail)
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            self._seen[key] = now
            if len(self._seen) > 10000:  # forget the sessions long ended
                self._seen = {k: t for k, t in self._seen.items() if now - t < SESSION_GAP}
        if last is None or now - last >= SESSION_GAP:
            self.record("login", kind, name, ip, detail)

    def recent(self, limit: int = 1000) -> list[dict]:
        """The last entries, the newest first."""
        lines: deque[str] = deque(maxlen=limit)
        for path in (self.path.with_name(self.path.name + ".1"), self.path):
            try:
                with open(path, encoding="utf-8") as fh:
                    lines.extend(fh)
            except OSError:
                continue
        entries = []
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries
