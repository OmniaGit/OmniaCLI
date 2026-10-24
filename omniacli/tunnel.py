# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tunnels: a port of this machine that reaches the web interface of an
instance of a linked OmniaCLI, for a browser that cannot reach it directly.

Every TCP connection to the port travels in a WebSocket of its own through
the link (/servers/<server>/<role>/tunnel/ws there). The other side reads
the first line of the connection (an HTTP request line), asks the
instance's plugin where that path goes (Odoo: /websocket to its gevent port
when it runs workers) and joins the two, byte by byte. The web interface is
served at the root of its own address, as it expects (Odoo's pages call
root paths: they cannot live under /links/<name>/)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import ssl
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import LinkTarget
from .links import CONNECT_TIMEOUT, websocket_url

_logger = logging.getLogger(__name__)

# the request line must come within these bytes
FIRST_LINE_LIMIT = 16384
CHUNK = 65536
# how often idle tunnels are looked for
WATCH_SECONDS = 60


class TunnelError(Exception):
    """A tunnel that cannot be opened: the reason, for the page."""


def tunnel_path(server: str, role: str) -> str:
    return f"servers/{server}/{role}/tunnel/ws"


def request_path(first: bytes) -> str:
    """The path of an HTTP request line ("GET /web?x=1 HTTP/1.1" -> /web);
    "/" when it is not one."""
    parts = first.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) >= 2 and parts[1].startswith(b"/"):
        return parts[1].split(b"?", 1)[0].decode("latin-1")
    return "/"


# --- the side of the instance -------------------------------------------

async def serve_far(websocket, target: Callable[[str], tuple[str, int] | None],
                    connect: Callable[[str, int], Awaitable[tuple]] = asyncio.open_connection) -> None:
    """One connection of a tunnel, on an accepted WebSocket: wait for its
    request line, connect to target(path) and join the two until one ends."""
    buffered = b""
    while b"\r\n" not in buffered:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        buffered += message.get("bytes") or (message.get("text") or "").encode()
        if b"\r\n" not in buffered and len(buffered) > FIRST_LINE_LIMIT:
            await websocket.close(1009, "no request line")
            return
    address = target(request_path(buffered))
    if address is None:
        await websocket.close(1008, "the instance has no web port")
        return
    try:
        reader, writer = await asyncio.wait_for(connect(*address), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        await websocket.close(1011, f"cannot connect to {address[0]}:{address[1]}: {exc}"[:120])
        return
    writer.write(buffered)

    async def to_instance():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes") or (message.get("text") or "").encode()
            writer.write(data)
            await writer.drain()

    async def to_tunnel():
        while True:
            data = await reader.read(CHUNK)
            if not data:
                return
            await websocket.send_bytes(data)

    await _join(to_instance(), to_tunnel())
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    with contextlib.suppress(Exception):
        await websocket.close(1000)


async def _join(*coroutines) -> None:
    """Run the two directions until either ends; cancel the other."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                _logger.debug("tunnel: %r", result)


# --- this side: the local port ------------------------------------------

@dataclass
class Tunnel:
    id: str
    link: str
    server: str
    role: str
    # who opened it and with which role here (the link's role is capped by it)
    owner: str
    owner_role: str
    bind: str
    port: int
    opened: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    connections: int = 0
    listener: asyncio.AbstractServer | None = None
    tasks: set = field(default_factory=set)

    def to_dict(self) -> dict:
        return {"id": self.id, "link": self.link, "server": self.server, "role": self.role,
                "owner": self.owner, "bind": self.bind, "port": self.port, "opened": self.opened,
                "last_active": self.last_active, "connections": self.connections}


def parse_ports(text: str) -> range:
    """"18000-18099" -> range(18000, 18100)."""
    low, _, high = str(text).partition("-")
    low, high = int(low), int(high or low)
    if not 1024 <= low <= high <= 65535:
        raise ValueError(f"ports {text}: from 1024 to 65535, low-high")
    return range(low, high + 1)


class TunnelManager:
    """The open tunnels of this instance. options() gives the current
    Options (bind address, port range, idle hours); headers(link, user,
    role) the link's headers; record(user, detail) writes the access log."""

    def __init__(self, options: Callable, headers: Callable, record: Callable[[str, str], None]):
        self.options = options
        self.headers = headers
        self.record = record
        self.tunnels: dict[str, Tunnel] = {}
        self._watch: asyncio.Task | None = None

    def start(self) -> None:
        self._watch = asyncio.ensure_future(self._watch_idle())

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        for tunnel_id in list(self.tunnels):
            await self.close(tunnel_id, "OmniaCLI stopped")

    async def _connect(self, link: LinkTarget, tunnel: Tunnel, probe: bool = False):
        from websockets.asyncio.client import connect

        ssl_context = None
        if link.url.startswith("https://"):
            ssl_context = ssl.create_default_context(cafile=link.ca_file or None)
        path = tunnel_path(tunnel.server, tunnel.role) + ("?probe=1" if probe else "")
        return await connect(websocket_url(link, path), ssl=ssl_context, open_timeout=CONNECT_TIMEOUT,
                             additional_headers=self.headers(link, tunnel.owner, tunnel.owner_role),
                             max_size=None, compression=None)

    async def open(self, link: LinkTarget, server: str, role: str, owner: str, owner_role: str) -> Tunnel:
        """Ask the other side whether the instance can be reached, then
        listen on the first free port of the range. TunnelError: why not."""
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        for tunnel in self.tunnels.values():
            if (tunnel.link, tunnel.server, tunnel.role) == (link.name, server, role):
                return tunnel
        options = self.options()
        tunnel = Tunnel(secrets.token_urlsafe(6), link.name, server, role, owner, owner_role,
                        options.tunnel_bind, 0)
        try:
            ws = await self._connect(link, tunnel, probe=True)
            try:
                await ws.recv()
            except ConnectionClosed:
                pass
            if ws.close_code != 1000:
                raise TunnelError(ws.close_reason or f"refused ({ws.close_code})")
        except InvalidStatus as exc:
            status = exc.response.status_code
            raise TunnelError(
                f"{link.name} answered {status}: an OmniaCLI without tunnels (update it)"
                if status == 404 else f"{link.name} refused the tunnel ({status})") from None
        except (OSError, asyncio.TimeoutError) as exc:
            raise TunnelError(f"{link.name} cannot be reached: {exc}") from None
        used = {t.port for t in self.tunnels.values()}
        for port in parse_ports(options.tunnel_ports):
            if port in used:
                continue
            try:
                tunnel.listener = await asyncio.start_server(
                    lambda r, w, t=tunnel, lk=link: self._accepted(lk, t, r, w), tunnel.bind, port)
            except OSError:
                continue
            tunnel.port = port
            break
        else:
            raise TunnelError(f"no free port in {options.tunnel_ports} on {tunnel.bind}")
        self.tunnels[tunnel.id] = tunnel
        self.record(owner, f"{link.name}/{server}/{role} opened on {tunnel.bind}:{tunnel.port}")
        return tunnel

    def _accepted(self, link: LinkTarget, tunnel: Tunnel, reader, writer):
        task = asyncio.ensure_future(self._relay(link, tunnel, reader, writer))
        tunnel.tasks.add(task)
        task.add_done_callback(tunnel.tasks.discard)

    async def _relay(self, link: LinkTarget, tunnel: Tunnel, reader, writer) -> None:
        """One connection to the local port, through its own WebSocket."""
        from websockets.exceptions import ConnectionClosed

        tunnel.connections += 1
        tunnel.last_active = time.time()
        try:
            try:
                ws = await self._connect(link, tunnel)
            except Exception as exc:  # noqa: BLE001 - the browser sees the connection closed
                _logger.info("tunnel %s: %s", tunnel.id, exc)
                return

            async def to_link():
                while True:
                    data = await reader.read(CHUNK)
                    if not data:
                        return
                    tunnel.last_active = time.time()
                    await ws.send(data)

            async def to_browser():
                with contextlib.suppress(ConnectionClosed):
                    async for message in ws:
                        tunnel.last_active = time.time()
                        writer.write(message if isinstance(message, bytes) else message.encode())
                        await writer.drain()

            await _join(to_link(), to_browser())
            with contextlib.suppress(Exception):
                await ws.close()
        finally:
            tunnel.connections -= 1
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self, tunnel_id: str, why: str) -> bool:
        tunnel = self.tunnels.pop(tunnel_id, None)
        if tunnel is None:
            return False
        if tunnel.listener:
            tunnel.listener.close()
        for task in list(tunnel.tasks):
            task.cancel()
        await asyncio.gather(*tunnel.tasks, return_exceptions=True)
        self.record(tunnel.owner, f"{tunnel.link}/{tunnel.server}/{tunnel.role} on port {tunnel.port} closed: {why}")
        return True

    async def close_idle(self) -> None:
        limit = self.options().tunnel_idle_hours * 3600
        now = time.time()
        for tunnel in list(self.tunnels.values()):
            if now - tunnel.last_active >= limit:
                await self.close(tunnel.id, f"no traffic for {self.options().tunnel_idle_hours} h")

    async def _watch_idle(self) -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            try:
                await self.close_idle()
            except Exception:  # noqa: BLE001 - the watch goes on
                _logger.exception("tunnel idle watch")
