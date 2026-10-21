"""Links to other OmniaCI instances: this one serves their pages under
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
        "X-OmniaCI-Prefix": f"/links/{link.name}",
        "X-OmniaCI-User": user,
        "X-OmniaCI-Role-Cap": role,
    }


def describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "no answer in time"
    if isinstance(exc, httpx.ConnectError):
        return f"cannot connect ({exc})"
    return f"{type(exc).__name__}: {exc}"
