# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shell of the server page: a login shell of the user running OmniaCLI
on a pseudo-terminal, joined to a browser's WebSocket.

The browser sends text frames of JSON: {"type": "input", "data": "..."} for
what is typed, {"type": "resize", "cols": n, "rows": n} when the terminal
changes size. The shell's output goes to the browser as binary frames. The
shell ends when the WebSocket closes, and the WebSocket closes when the
shell exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pwd
import signal
import struct
import termios
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
    async def start(cls, cwd: Path | None, cols: int = 80, rows: int = 24) -> "Shell":
        shell, home = login_shell()
        user = pwd.getpwuid(os.getuid()).pw_name
        env = {k: os.environ[k] for k in _KEPT_ENV if k in os.environ}
        env.update({"HOME": home, "USER": user, "LOGNAME": user, "SHELL": shell, "TERM": "xterm-256color"})
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.setdefault("LANG", "C.UTF-8")
        folder = cwd if cwd is not None and cwd.is_dir() else Path(home)
        if not folder.is_dir():
            folder = Path("/")
        master, slave = os.openpty()
        try:
            _set_size(master, cols, rows)
            process = await asyncio.create_subprocess_exec(
                "setsid", "-c", shell, "-l", stdin=slave, stdout=slave, stderr=slave,
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


async def serve(websocket: WebSocket, cwd: Path | None) -> None:
    """Join an accepted WebSocket to a new shell until one of them ends."""
    shell = await Shell.start(cwd)

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
