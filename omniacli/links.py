# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Links to other OmniaCLI instances: this one serves their pages under
/links/<name>/ and lists their servers, acting with the link token they gave
(see auth.links on their side)."""

from __future__ import annotations

import asyncio
import ssl

import httpx

from .config import LinkTarget

# a link answers within these, or it is shown as unreachable
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
LIST_TIMEOUT = 5.0


class LinkClients:
    """One HTTP client per link, made when first used and dropped when the
    link changes. transport replaces the network (tests)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self._clients: dict[str, tuple[LinkTarget, httpx.AsyncClient]] = {}

    def client(self, link: LinkTarget) -> httpx.AsyncClient:
        known = self._clients.get(link.name)
        if known and known[0] == link:
            return known[1]
        if known:
            asyncio.get_running_loop().create_task(known[1].aclose())
        verify: ssl.SSLContext | bool = True
        if link.ca_file:
            verify = ssl.create_default_context(cafile=link.ca_file)
        client = httpx.AsyncClient(
            base_url=link.url,
            transport=self.transport,
            verify=verify,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            follow_redirects=False,
        )
        self._clients[link.name] = (link, client)
        return client

    async def close(self) -> None:
        for _, client in self._clients.values():
            await client.aclose()
        self._clients = {}


def link_headers(link: LinkTarget, user: str, role: str) -> dict[str, str]:
    """What the other instance needs: the link token, the path its pages are
    served under here, who acts and the most that user may do here."""
    return {
        "Authorization": f"Bearer {link.token}",
        "X-OmniaCLI-Prefix": f"/links/{link.name}",
        "X-OmniaCLI-User": user,
        "X-OmniaCLI-Role-Cap": role,
    }


def websocket_url(link: LinkTarget, path: str) -> str:
    """The other instance's ws(s):// URL of path."""
    scheme, rest = link.url.split("://", 1)
    return ("wss" if scheme == "https" else "ws") + "://" + rest + "/" + path.lstrip("/")


async def proxy_websocket(websocket, link: LinkTarget, path: str, headers: dict[str, str]) -> None:
    """Join an accepted WebSocket of a browser to the other instance's
    WebSocket at path, frame by frame, until one side closes. Raises
    OSError or websockets' InvalidHandshake when the other side refuses."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    ssl_context = None
    if link.url.startswith("https://"):
        ssl_context = ssl.create_default_context(cafile=link.ca_file or None)
    async with connect(websocket_url(link, path), additional_headers=headers, ssl=ssl_context,
                       open_timeout=CONNECT_TIMEOUT, max_size=None) as remote:

        async def to_remote():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await remote.send(message["bytes"])
                elif message.get("text") is not None:
                    await remote.send(message["text"])

        async def to_browser():
            try:
                async for message in remote:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            except ConnectionClosed:
                pass
            # the other side closed: the same code and reason to the browser
            code, reason = remote.close_code or 1000, remote.close_reason or ""
            await websocket.close(code if code not in (1005, 1006) else 1011, reason)

        tasks = [asyncio.ensure_future(to_remote()), asyncio.ensure_future(to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def describe_error(exc: Exception) -> str:
    from websockets.exceptions import InvalidStatus

    if isinstance(exc, InvalidStatus):
        # a WebSocket the other side answered with plain HTTP: 404 when a
        # proxy drops the Upgrade header, or the route is not there yet
        code = exc.response.status_code
        if code == 401:
            return "the other instance refused the token: ask for a new one"
        if code == 404:
            return "WebSocket refused (404): a proxy there that does not pass WebSockets on, or an older OmniaCLI"
        return f"WebSocket refused ({code})"
    if isinstance(exc, httpx.TimeoutException):
        return "no answer in time"
    if isinstance(exc, httpx.ConnectError):
        return f"cannot connect ({exc})"
    return f"{type(exc).__name__}: {exc}"
