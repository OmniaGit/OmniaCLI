# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Links through SSH: a customer's server that shows only SSH (and Odoo)
still has its OmniaCLI reached, by an ssh -L that this OmniaCLI keeps open
for the link and restarts when it drops. Every request of the link (pages,
API, WebSockets, tunnels) then goes to the tunnel's port on 127.0.0.1.

OmniaCLI has a key of its own (an ed25519 in the ssh folder next to the
links file); the customer's server gets its public key with a line that
allows the one forward and nothing else (authorized_line), better also a
Match block of sshd_config (sshd_block). The server's own key is fetched
once (fetch_host_key), confirmed by an admin and saved with the link: ssh
accepts no other, and reads no configuration nor known_hosts of the user.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import ctypes
import hashlib
import logging
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import SSH_HOST_KEY, ConfigError, LinkTarget, check_ssh_address

_logger = logging.getLogger(__name__)

KEY_NAME = "id_ed25519"
# the client (tests put a fake one here)
SSH_COMMAND = "ssh"
# within these an ssh that runs is listening, or it is restarted
READY_SECONDS = 25.0
# the wait before a restart doubles up to the last one
RETRY_SECONDS = (5, 10, 20, 40, 60)
# up this long, the next failure starts from the first wait again
STABLE_SECONDS = 120.0
KEYSCAN_TIMEOUT = 15
# the order a host key is chosen in
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
PR_SET_PDEATHSIG = 1


class SshError(Exception):
    """What an admin is told: a key that cannot be made or fetched."""


def key_file(folder: Path) -> Path:
    return Path(folder) / KEY_NAME


def public_key(folder: Path) -> str | None:
    """OmniaCLI's public key, None until it is made."""
    try:
        return (key_file(folder).with_suffix(".pub")).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


async def ensure_key(folder: Path) -> str:
    """OmniaCLI's public key, made the first time (folder 700, key 600)."""
    found = public_key(folder)
    if found:
        return found
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    proc = await asyncio.create_subprocess_exec(
        "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"omniacli@{socket.gethostname()}",
        "-f", str(key_file(folder)), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode:
        raise SshError(f"ssh-keygen failed: {out.decode(errors='replace').strip()}")
    found = public_key(folder)
    if not found:
        raise SshError("ssh-keygen made no public key")
    return found


def fingerprint(host_key: str) -> str:
    """SHA256:… as ssh-keygen -l prints it."""
    raw = base64.b64decode(host_key.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _permit(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def authorized_line(public: str, link: LinkTarget) -> str:
    """The line of the customer's authorized_keys: the forward to the link's
    OmniaCLI only, no command, no shell, no file transfer."""
    host, port = link.remote
    return f'restrict,port-forwarding,permitopen="{_permit(host, port)}",command="/bin/false" {public}'


def sshd_block(link: LinkTarget) -> str:
    """The Match block of the customer's sshd_config for the link's user: it
    also refuses a forward from that server back to this machine (-R), which
    the line alone allows."""
    host, port = link.remote
    user = link.ssh.user if link.ssh else "omniacli-link"
    return "\n".join([
        f"Match User {user}",
        "    AllowTcpForwarding local",
        f"    PermitOpen {_permit(host, port)}",
        "    PermitListen none",
        "    ForceCommand /bin/false",
        "    PermitTTY no",
        "    X11Forwarding no",
        "    AllowAgentForwarding no",
        "    AllowStreamLocalForwarding no",
        "    PermitTunnel no",
    ])


async def fetch_host_key(host: str, port: int) -> str:
    """The SSH server's key ("<type> <base64>"), the strongest it offers, for
    an admin to compare with the one the customer reads on the server
    (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)."""
    try:
        host, port = check_ssh_address("link", host, port)
    except ConfigError as exc:
        raise SshError(str(exc)) from None
    host = host.strip("[]")
    proc = await asyncio.create_subprocess_exec(
        "ssh-keyscan", "-T", str(KEYSCAN_TIMEOUT), "-p", str(port), "-t", "ed25519,ecdsa,rsa", host,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), KEYSCAN_TIMEOUT + 5)
    except asyncio.TimeoutError:
        proc.kill()
        raise SshError(f"{host}:{port} did not answer") from None
    keys = {}
    for line in out.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            key = f"{parts[1]} {parts[2]}"
            if SSH_HOST_KEY.fullmatch(key):
                keys[parts[1]] = key
    for kind in KEY_TYPES:
        if kind in keys:
            return keys[kind]
    reason = [line for line in err.decode(errors="replace").splitlines() if line and not line.startswith("#")]
    raise SshError(f"no SSH server answered on {host}:{port}" + (f": {reason[-1]}" if reason else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ssh_command(link: LinkTarget, key: Path, known_hosts: Path, port: int, ssh: str = "ssh") -> list[str]:
    """ssh -N -L for the link: no configuration, no agent, no known_hosts of
    the user, only the saved key of the server, and an exit when the forward
    cannot be made or the server stops answering."""
    route = link.ssh
    host, remote_port = link.remote
    target = f"[{host}]" if ":" in host else host
    options = {
        "IdentitiesOnly": "yes", "IdentityAgent": "none", "BatchMode": "yes",
        "StrictHostKeyChecking": "yes", "UserKnownHostsFile": str(known_hosts),
        "GlobalKnownHostsFile": "/dev/null", "HostKeyAlias": alias(link), "CheckHostIP": "no",
        "UpdateHostKeys": "no", "ExitOnForwardFailure": "yes", "ServerAliveInterval": "15",
        "ServerAliveCountMax": "3", "ConnectTimeout": "15", "ClearAllForwardings": "no",
    }
    cmd = [ssh, "-N", "-F", "/dev/null", "-i", str(key)]
    for name, value in options.items():
        cmd += ["-o", f"{name}={value}"]
    return cmd + ["-L", f"127.0.0.1:{port}:{target}:{remote_port}", "-p", str(route.port), "-l", route.user,
                  route.host]


def alias(link: LinkTarget) -> str:
    return f"omniacli-link-{link.name}"


def _die_with_parent() -> None:
    """In the child: SIGTERM when OmniaCLI dies, even killed (the service
    stops OmniaCLI's process only, KillMode=process)."""
    with contextlib.suppress(Exception):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


async def _listening(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


@dataclass
class _Tunnel:
    link: LinkTarget
    port: int
    state: str = "starting"
    detail: str = ""
    since: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    errors: collections.deque = field(default_factory=lambda: collections.deque(maxlen=5))

    def key(self) -> tuple:
        return self.link.ssh, self.link.url


class SshTunnels:
    """The ssh -L of every link that has ssh, kept open while OmniaCLI runs."""

    def __init__(self, folder: Path, ssh: str | None = None):
        self.folder = Path(folder)
        self.ssh = ssh or SSH_COMMAND
        self.tunnels: dict[str, _Tunnel] = {}

    def sync(self, links: dict[str, LinkTarget]) -> None:
        """Start, keep or stop tunnels for the links as they are now, and
        give each link its tunnel's port. In the event loop."""
        for name in list(self.tunnels):
            link = links.get(name)
            tunnel = self.tunnels[name]
            if link is None or link.ssh is None or (link.ssh, link.url) != tunnel.key():
                self._stop(name)
        for name, link in links.items():
            if link.ssh is None:
                continue
            tunnel = self.tunnels.get(name)
            if tunnel is None:
                tunnel = self.tunnels[name] = _Tunnel(link, free_port())
                tunnel.task = asyncio.get_running_loop().create_task(self._keep(tunnel))
            tunnel.link = link
            link.local_port = tunnel.port

    def status(self, name: str) -> dict | None:
        tunnel = self.tunnels.get(name)
        if tunnel is None:
            return None
        return {"state": tunnel.state, "detail": tunnel.detail, "port": tunnel.port, "since": tunnel.since}

    def _stop(self, name: str) -> None:
        tunnel = self.tunnels.pop(name)
        if tunnel.task is not None:
            tunnel.task.cancel()

    async def close(self) -> None:
        tasks = [t.task for t in self.tunnels.values() if t.task is not None]
        for name in list(self.tunnels):
            self._stop(name)
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _set(self, tunnel: _Tunnel, state: str, detail: str = "") -> None:
        if tunnel.state != state:
            tunnel.since = time.time()
            _logger.info("ssh tunnel of link %s: %s %s", tunnel.link.name, state, detail)
        tunnel.state, tunnel.detail = state, detail

    async def _keep(self, tunnel: _Tunnel) -> None:
        failures = 0
        try:
            while True:
                started = time.monotonic()
                await self._run_once(tunnel)
                if time.monotonic() - started >= STABLE_SECONDS:
                    failures = 0
                wait = RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)]
                failures += 1
                if tunnel.state != "down":
                    self._set(tunnel, "down", "ssh ended")
                tunnel.detail = f"{tunnel.detail}; again in {wait} s"
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            await self._end(tunnel)
            raise

    async def _run_once(self, tunnel: _Tunnel) -> None:
        link = tunnel.link
        known_hosts = self.folder / f"known_hosts_{link.name}"
        key = key_file(self.folder)
        if not key.exists():
            self._set(tunnel, "down", "OmniaCLI has no SSH key yet: open the Links page to make it")
            return
        known_hosts.write_text(f"{alias(link)} {link.ssh.host_key}\n", encoding="utf-8")
        if not await _free(tunnel.port):
            tunnel.port = free_port()
            link.local_port = tunnel.port
        self._set(tunnel, "starting")
        tunnel.errors.clear()
        try:
            tunnel.process = await asyncio.create_subprocess_exec(
                *ssh_command(link, key, known_hosts, tunnel.port, self.ssh),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, preexec_fn=_die_with_parent)
        except OSError as exc:
            self._set(tunnel, "down", f"ssh did not start: {exc}")
            return
        reader = asyncio.get_running_loop().create_task(self._read_errors(tunnel))
        try:
            deadline = time.monotonic() + READY_SECONDS
            while tunnel.process.returncode is None and time.monotonic() < deadline:
                if await _listening(tunnel.port):
                    self._set(tunnel, "up", f"127.0.0.1:{tunnel.port} → {link.ssh.host}")
                    break
                await asyncio.sleep(0.3)
            else:
                if tunnel.process.returncode is None:
                    self._set(tunnel, "down", f"no forward within {READY_SECONDS:.0f} s")
                    await self._end(tunnel)
            code = await tunnel.process.wait()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), 2)
            reason = tunnel.errors[-1] if tunnel.errors else f"ssh ended with {code}"
            if tunnel.state != "down" or not tunnel.detail.startswith("no forward"):
                self._set(tunnel, "down", reason)
        finally:
            reader.cancel()

    async def _read_errors(self, tunnel: _Tunnel) -> None:
        stream = tunnel.process.stderr
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").strip()
            if text:
                tunnel.errors.append(text[:300])

    async def _end(self, tunnel: _Tunnel) -> None:
        process = tunnel.process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def _free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


__all__ = ["SshError", "SshTunnels", "authorized_line", "ensure_key", "fetch_host_key", "fingerprint",
           "public_key", "ssh_command", "sshd_block"]
