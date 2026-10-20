"""Read a log file in steps, like tail -F, for a page that polls it.

The page asks again every few seconds with the position it reached: no
connection stays open (an open stream per log filled the browser's six
connections per server and blocked the other pages).
"""

from __future__ import annotations

import os
from pathlib import Path

_BLOCK = 64 * 1024
MAX_READ = 512 * 1024  # bytes returned by one call; the page asks again for more


def last_lines(path: Path, count: int) -> tuple[list[str], int, int]:
    """(last complete lines, position after them, inode) of the file.

    A last line still being written is left for the next read."""
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
    cut = data.rfind(b"\n") + 1
    complete = data[:cut]
    lines = complete.decode(errors="replace").splitlines()[-count:]
    return lines, end - (len(data) - cut), stat.st_ino


def read_since(path: Path, pos: int | None = None, inode: int | None = None,
               initial: int = 300) -> dict:
    """The lines written after `pos`, or the last `initial` lines when pos is None.

    Returns {lines, pos, inode, rotated, more}: pass pos and inode back on the
    next call. rotated: the file was replaced or truncated and is read from
    its start. more: there is more to read right away.
    """
    if pos is None:
        lines, pos, inode = last_lines(path, initial)
        return {"lines": lines, "pos": pos, "inode": inode, "rotated": False, "more": False}
    with open(path, "rb") as fh:
        stat = os.fstat(fh.fileno())
        rotated = stat.st_ino != inode or stat.st_size < pos
        if rotated:
            pos = 0
        fh.seek(pos)
        data = fh.read(min(stat.st_size - pos, MAX_READ))
    cut = data.rfind(b"\n") + 1
    if cut == 0 and len(data) == MAX_READ:
        cut = len(data)  # a single line longer than MAX_READ: give it in pieces
    lines = data[:cut].decode(errors="replace").splitlines()
    pos += cut
    return {
        "lines": lines,
        "pos": pos,
        "inode": stat.st_ino,
        "rotated": rotated,
        "more": pos < stat.st_size and cut > 0,
    }
