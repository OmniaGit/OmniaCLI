"""Read the end of a log file and follow it like tail -F."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator

_BLOCK = 64 * 1024


def last_lines(path: Path, count: int) -> tuple[list[str], int, int]:
    """Return (last lines, size read up to, inode) of the file."""
    with open(path, "rb") as fh:
        stat = os.fstat(fh.fileno())
        end = stat.st_size
        data = b""
        pos = end
        while pos > 0 and data.count(b"\n") <= count:
            step = min(_BLOCK, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode(errors="replace").splitlines()[-count:]
    return lines, end, stat.st_ino


async def follow(path: Path, initial: int = 300, poll: float = 1.0) -> AsyncIterator[str]:
    """Yield the last `initial` lines, then every new line, across log rotation."""
    lines, pos, inode = last_lines(path, initial)
    for line in lines:
        yield line
    partial = b""
    while True:
        await asyncio.sleep(poll)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue  # rotated, the new file is not there yet
        if stat.st_ino != inode or stat.st_size < pos:
            inode, pos, partial = stat.st_ino, 0, b""
            yield "----- log file rotated or truncated -----"
        if stat.st_size == pos:
            continue
        with open(path, "rb") as fh:
            fh.seek(pos)
            data = fh.read(stat.st_size - pos)
        pos += len(data)
        data = partial + data
        *complete, partial = data.split(b"\n")
        for raw in complete:
            yield raw.decode(errors="replace")
