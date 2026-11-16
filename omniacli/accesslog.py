# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who logged in, when and from where, and what they did: the access log.

A user's login and logout are those of the login form. A link or an MCP token
sends its token with every request, so its login is its first request after
SESSION_GAP without requests from the same address. Every call to /mcp is
logged, with its JSON-RPC method and tool, and so is every action of whoever
passed the login (app.py: what it asked and the answer), every shell, tunnel
and download of a backup.

Wrong passwords and tokens need no login, so they can never push out what the
others wrote: they have a file of their own, and from one address at most
BURST_ADDRESS of them a minute are written (BURST_TOTAL from all addresses),
the rest counted in one entry at the end of that minute.

One JSON object per line in access.jsonl and access-failed.jsonl, next to the
instances' logs; past MAX_BYTES a file moves to <file>.1, and so on up to
<file>.KEEP_FILES (the oldest is dropped).
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from pathlib import Path

SESSION_GAP = 30 * 60  # seconds
MAX_BYTES = 5 * 1024 * 1024
KEEP_FILES = 10

BURST_WINDOW = 60  # seconds
BURST_ADDRESS = 10
BURST_TOTAL = 300


def _rotate(path: Path) -> None:
    for number in range(KEEP_FILES - 1, 0, -1):
        older = path.with_name(f"{path.name}.{number}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{number + 1}"))
    path.replace(path.with_name(path.name + ".1"))


class _Burst:
    """How many failures one address (or all of them) sent in this minute."""

    def __init__(self, now: float):
        self.start, self.written, self.dropped = now, 0, 0


class AccessLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.failed_path = self.path.with_name(f"{self.path.stem}-failed{self.path.suffix}")
        self._lock = threading.Lock()
        self._seen: dict[tuple, float] = {}
        self._bursts: dict[str, _Burst] = {}
        self._total = _Burst(0.0)

    def _write(self, path: Path, event: str, kind: str, name: str, ip: str, detail: str) -> None:
        """One line; the caller holds the lock."""
        entry = {
            "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "ns": time.time_ns(),
            "event": event, "kind": kind, "name": name, "ip": ip, "detail": detail,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                _rotate(path)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the log must never stop a request

    def _close_bursts(self, now: float) -> None:
        """The minutes that ended: what they did not write, counted. The
        caller holds the lock."""
        for ip, burst in list(self._bursts.items()):
            if now - burst.start >= BURST_WINDOW:
                if burst.dropped:
                    self._write(self.failed_path, "failed", "burst", "", ip,
                                f"{burst.dropped} more failed attempts in {BURST_WINDOW} s, not listed")
                del self._bursts[ip]

    def _allowed(self, ip: str, now: float) -> bool:
        """Whether a failure from ip is written; the caller holds the lock."""
        self._close_bursts(now)
        if now - self._total.start >= BURST_WINDOW:
            self._total = _Burst(now)
        burst = self._bursts.setdefault(ip, _Burst(now))
        if burst.written >= BURST_ADDRESS or self._total.written >= BURST_TOTAL:
            burst.dropped += 1
            return False
        burst.written += 1
        self._total.written += 1
        return True

    def record(self, event: str, kind: str, name: str, ip: str = "", detail: str = "") -> None:
        """event: login, logout, failed, mcp, shell, tunnel, action or
        download; kind: user, link, token or mcp."""
        with self._lock:
            if event != "failed":
                self._write(self.path, event, kind, name, ip, detail)
            elif self._allowed(ip, time.monotonic()):
                self._write(self.failed_path, event, kind, name, ip, detail)

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

    def _lines(self, base: Path, limit: int) -> list[str]:
        """The last limit lines of base and its older files, the oldest first."""
        found: list[str] = []
        for path in [base, *(base.with_name(f"{base.name}.{n}") for n in range(1, KEEP_FILES + 1))]:
            try:
                with open(path, encoding="utf-8") as fh:
                    found = fh.readlines() + found
            except OSError:
                continue
            if len(found) >= limit:
                break
        return found[-limit:]

    def recent(self, limit: int = 1000) -> list[dict]:
        """The last entries, the newest first."""
        with self._lock:
            self._close_bursts(time.monotonic())
        entries = []
        for base in (self.path, self.failed_path):
            for line in self._lines(base, limit):
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        # entries older than "ns" keep the order of their file
        entries.sort(key=lambda e: e.get("ns", 0))
        return entries[-limit:][::-1]
