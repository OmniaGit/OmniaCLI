# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shell of the server page: a login shell of the user running OmniaCLI
on a pseudo-terminal, joined to a browser's WebSocket.

The browser sends text frames of JSON: {"type": "input", "data": "..."} for
what is typed, {"type": "resize", "cols": n, "rows": n} when the terminal
changes size. The shell's output goes to the browser as binary frames. The
shell ends when the WebSocket closes, and the WebSocket closes when the
shell exits.

A file dropped on the terminal is uploaded into the drops folder of the
instance, in the home of the user running OmniaCLI (DROPS_DIR/<server>/
<role>), and the terminal types its path, as a terminal does with a file
dropped on it: Claude reads it from there. Files older than DROP_DAYS go at
the next upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pwd
import shlex
import signal
import re
import struct
import tempfile
import termios
import time
from collections.abc import AsyncIterator
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect

_logger = logging.getLogger(__name__)

# a shell for a system user whose own is nologin
FALLBACK_SHELLS = ("/bin/bash", "/bin/sh")
# the WebSocket's close code when the shell exits by itself
SHELL_EXITED = 1000
READ_SIZE = 65536
# what the shell gets of OmniaCLI's environment (the rest is its own)
_KEPT_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
# what the Claude button runs, found by the login shell's PATH (Claude
# Code's installer puts it in ~/.local/bin)
CLAUDE_COMMAND = "claude"
# the files dropped on a terminal: where, how long they are kept, how big
DROPS_DIR = ".omniacli-drops"
DROP_DAYS = 7
DROP_MAX = 100 << 20


class DropRefused(ValueError):
    """A dropped file that is not saved (too big, no name), said to the page."""


def drops_folder(server: str, role: str) -> Path:
    """The folder of the files dropped on the terminals of an instance."""
    return Path(login_shell()[1]) / DROPS_DIR / server / role


def drop_name(name: str) -> str:
    """The name a dropped file is saved with: its own, without folders,
    leading dots and characters a shell would need quoted twice."""
    name = re.sub(r"[^\w.+-]+", "_", Path(name.replace("\\", "/")).name).lstrip(".")
    return name[:200] or "file"


def prune_drops(folder: Path, days: int = DROP_DAYS) -> None:
    """Remove the files of folder older than days."""
    limit = time.time() - days * 86400
    for path in folder.iterdir() if folder.is_dir() else ():
        with contextlib.suppress(OSError):
            if path.is_file() and path.stat().st_mtime < limit:
                path.unlink()


async def save_drop(folder: Path, name: str, chunks: AsyncIterator[bytes], limit: int = DROP_MAX) -> Path:
    """Write a dropped file into folder, whole or not at all (a .part file
    first), with a name of its own there (name-1.ext, name-2.ext... when
    taken); more than limit bytes is refused."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, part_name = tempfile.mkstemp(dir=folder, prefix=".", suffix=".part")
    part = Path(part_name)
    try:
        size = 0
        with os.fdopen(handle, "wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise DropRefused(f"{name}: more than {limit >> 20} MiB")
                out.write(chunk)
        stem, suffix = Path(name).stem, Path(name).suffix
        for n in range(1000):
            dest = folder / (name if not n else f"{stem}-{n}{suffix}")
            try:
                # a hard link never replaces a file there: taken, the next name
                os.link(part, dest)
                return dest
            except FileExistsError:
                continue
        raise DropRefused(f"{name}: too many files with this name")
    finally:
        part.unlink(missing_ok=True)


def login_shell() -> tuple[str, str]:
    """(shell, home) of the user running OmniaCLI: its own shell, or bash
    when that is nologin or false (a system user's)."""
    entry = pwd.getpwuid(os.getuid())
    shell = entry.pw_shell
    if not shell or Path(shell).name in ("nologin", "false") or not os.access(shell, os.X_OK):
        shell = next(s for s in FALLBACK_SHELLS if os.access(s, os.X_OK))
    return shell, entry.pw_dir


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(1, min(int(cols), 1000)), max(1, min(int(rows), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Shell:
    """A login shell on a pseudo-terminal, in its own session (setsid -c
    makes the terminal its controlling one: job control works)."""

    def __init__(self, master: int, process: asyncio.subprocess.Process):
        self.master = master
        self.process = process

    @classmethod
    async def start(cls, cwd: Path | None, cols: int = 80, rows: int = 24, command: str | None = None,
                    env: dict | None = None, stay: bool = True) -> "Shell":
        """command: run first by the login shell (its profile sets PATH),
        which is then left interactive (stay) or ends with it; a cwd given
        must exist. env: more variables for the shell and the command."""
        shell, home = login_shell()
        user = pwd.getpwuid(os.getuid()).pw_name
        extra = env or {}
        env = {k: os.environ[k] for k in _KEPT_ENV if k in os.environ}
        env.update({"HOME": home, "USER": user, "LOGNAME": user, "SHELL": shell, "TERM": "xterm-256color"})
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.setdefault("LANG", "C.UTF-8")
        env.update(extra)
        if command and cwd is not None and not cwd.is_dir():
            raise FileNotFoundError(f"no folder {cwd}")
        folder = cwd if cwd is not None and cwd.is_dir() else Path(home)
        if not folder.is_dir():
            folder = Path("/")
        master, slave = os.openpty()
        try:
            _set_size(master, cols, rows)
            args = [shell, "-l"]
            if command:
                args += ["-c", f"{command}; exec {shlex.quote(shell)} -l" if stay else f"exec {command}"]
            process = await asyncio.create_subprocess_exec(
                "setsid", "-c", *args, stdin=slave, stdout=slave, stderr=slave,
                cwd=str(folder), env=env)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        return cls(master, process)

    def resize(self, cols: int, rows: int) -> None:
        _set_size(self.master, cols, rows)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.master, view):]
            except BlockingIOError:
                # the shell does not read: what it has not taken is dropped
                return

    async def read(self) -> bytes:
        """The next output; b"" once the shell and everything on its terminal ended."""
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        loop.add_reader(self.master, lambda: ready.done() or ready.set_result(None))
        try:
            await ready
        finally:
            loop.remove_reader(self.master)
        try:
            return os.read(self.master, READ_SIZE)
        except OSError:
            # EIO: no process has the terminal open any more
            return b""

    async def close(self) -> None:
        """Hang up the shell's session (SIGHUP, as a closed terminal does),
        SIGKILL what is left after a few seconds."""
        # setsid ran in the child itself (it is no group leader): the shell's
        # pid is its session and process group
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGHUP)
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        with contextlib.suppress(OSError):
            os.close(self.master)


async def serve(websocket: WebSocket, cwd: Path | None, command: str | None = None,
                env: dict | None = None, stay: bool = True) -> None:
    """Join an accepted WebSocket to a new shell until one of them ends."""
    shell = await Shell.start(cwd, command=command, env=env, stay=stay)

    async def output():
        while True:
            data = await shell.read()
            if not data:
                return
            await websocket.send_bytes(data)

    async def typed():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            try:
                event = json.loads(message.get("text") or "")
                if event.get("type") == "input":
                    shell.write(str(event.get("data", "")).encode("utf-8"))
                elif event.get("type") == "resize":
                    shell.resize(event["cols"], event["rows"])
            except (ValueError, KeyError, TypeError, AttributeError, OSError):
                continue

    tasks = [asyncio.ensure_future(output()), asyncio.ensure_future(typed())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            # a browser gone while output is sent ends it too
            if task.exception() is not None:
                _logger.debug("shell: %r", task.exception())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await shell.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(SHELL_EXITED, "the shell ended")
