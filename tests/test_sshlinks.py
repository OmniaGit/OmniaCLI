# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Links through SSH: the link's settings, the ssh command, the lines for
the customer's server, and the tunnel kept open (a fake ssh that forwards
for real to the customer's OmniaCLI)."""

import base64
import html
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from omniacli import sshlinks
from omniacli.app import create_app
from omniacli.config import ConfigError, parse_link, parse_settings

from .conftest import LINK_TOKEN, USER_HASH
from .test_shell import browser
from .test_tunnel import customer  # noqa: F401 (a fixture)

# an ed25519 key as a server's: its type, then 32 bytes
HOST_KEY = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()
SSH = {"host": "customer.example", "user": "omniacli-link", "host_key": HOST_KEY}

# a fake ssh: -L 127.0.0.1:<port>:<host>:<port> forwarded for real, its
# arguments written down, the exit code of FAKE_SSH_FAIL when that is set
FAKE_SSH = '''#!{python}
import asyncio, json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as fh:
    fh.write(json.dumps({{"pid": os.getpid(), "args": args}}) + "\\n")
if os.path.exists(os.environ["FAKE_SSH_LOG"] + ".fail"):
    print("Permission denied (publickey).", file=sys.stderr)
    sys.exit(255)
bind, lport, host, rport = args[args.index("-L") + 1].rsplit(":", 3)

async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()

async def handle(reader, writer):
    far_reader, far_writer = await asyncio.open_connection(host, int(rport))
    await asyncio.gather(pipe(reader, far_writer), pipe(far_reader, writer), return_exceptions=True)

async def main():
    server = await asyncio.start_server(handle, bind, int(lport))
    async with server:
        await server.serve_forever()

asyncio.run(main())
'''


def test_a_link_through_ssh():
    link = parse_link("c", {"url": "http://127.0.0.1:8090/", "token": "t", "ssh": {**SSH, "port": 2222}})
    assert link.ssh.port == 2222 and link.remote == ("127.0.0.1", 8090)
    assert link.to_dict()["ssh"] == {**SSH, "port": 2222}
    # the requests go to the tunnel's port here
    link.local_port = 18123
    assert link.address == "http://127.0.0.1:18123"
    assert parse_link("c", {"url": "https://x.example", "token": "t"}).address == "https://x.example"
    # a known_hosts line is taken without its host and comment
    assert parse_link("c", {"url": "http://127.0.0.1:8090", "token": "t",
                            "ssh": {**SSH, "host_key": f"{HOST_KEY} root@srv"}}).ssh.host_key == HOST_KEY


@pytest.mark.parametrize("change, error", [
    ({"url": "https://127.0.0.1:8090"}, "through SSH the url is http://"),
    ({"ssh": {**SSH, "host": "-oProxyCommand=touch /tmp/x"}}, "is not a host name"),
    ({"ssh": {**SSH, "host": "a b"}}, "is not a host name"),
    ({"ssh": {**SSH, "user": "-l"}}, "is not a user name"),
    ({"ssh": {**SSH, "port": 70000}}, "is not a port"),
    ({"ssh": {**SSH, "host_key": ""}}, "the server's key is missing"),
    ({"ssh": {**SSH, "host_key": "ssh-dss AAAA"}}, "the server's key is missing"),
    ({"ssh": {**SSH, "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDd5"}}, "not a key of type ssh-ed25519"),
    ({"ssh": {**SSH, "host_key": "ssh-rsa " + HOST_KEY.split()[1]}}, "not a key of type ssh-rsa"),
    ({"ssh": "yes"}, "must be a mapping"),
])
def test_what_a_link_through_ssh_refuses(change, error):
    with pytest.raises(ConfigError, match=error):
        parse_link("c", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": SSH, **change})


def test_the_ssh_command():
    link = parse_link("c", {"url": "http://[::1]:8090", "token": "t", "ssh": SSH})
    cmd = sshlinks.ssh_command(link, Path("/k/id"), Path("/k/known"), 18001, "ssh")
    options = {cmd[i + 1] for i, a in enumerate(cmd) if a == "-o"}
    # nothing of the user's: no configuration, agent or known_hosts, and only the saved key
    assert cmd[:6] == ["ssh", "-N", "-F", "/dev/null", "-i", "/k/id"]
    assert {"StrictHostKeyChecking=yes", "UserKnownHostsFile=/k/known", "GlobalKnownHostsFile=/dev/null",
            "HostKeyAlias=omniacli-link-c", "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
            "ExitOnForwardFailure=yes"} <= options
    assert cmd[-7:] == ["-L", "127.0.0.1:18001:[::1]:8090", "-p", "22", "-l", "omniacli-link", "customer.example"]


def test_the_lines_for_the_customers_server():
    link = parse_link("c", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": SSH})
    assert sshlinks.authorized_line("ssh-ed25519 AAAA omniacli@here", link) == (
        'restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/bin/false" ssh-ed25519 AAAA omniacli@here')
    block = sshlinks.sshd_block(link)
    assert block.splitlines()[0] == "Match User omniacli-link"
    for line in ("AllowTcpForwarding local", "PermitOpen 127.0.0.1:8090", "PermitListen none",
                 "ForceCommand /bin/false", "PermitTTY no"):
        assert f"    {line}" in block.splitlines()


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="no ssh-keygen")
async def test_the_key_and_the_fingerprint(tmp_path):
    folder = tmp_path / "ssh"
    public = await sshlinks.ensure_key(folder)
    assert public.startswith("ssh-ed25519 ") and await sshlinks.ensure_key(folder) == public
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert oct(sshlinks.key_file(folder).stat().st_mode & 0o777) == "0o600"
    printed = subprocess.run(["ssh-keygen", "-lf", str(folder / "id_ed25519.pub")], capture_output=True,
                             text=True, check=True).stdout.split()[1]
    assert sshlinks.fingerprint(public) == printed


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    script = tmp_path / "fake-ssh"
    script.write_text(FAKE_SSH.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "fake-ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setattr(sshlinks, "SSH_COMMAND", str(script))
    monkeypatch.setattr(sshlinks, "RETRY_SECONDS", (0.3,))
    return log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _until(check, limit=15.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        found = check()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError("not in time")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is not alive
    return "zombie" not in Path(f"/proc/{pid}/status").read_text().lower()


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="no ssh-keygen")
def test_a_link_goes_through_its_tunnel(env, tmp_path, customer, fake_ssh):  # noqa: F811
    _, url = customer
    conf = tmp_path / "mine"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {}}))
    mine = create_app(parse_settings({
        "auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "b"),
        "jobs_dir": str(tmp_path / "j"), "servers_file": "servers.yaml"}, base_dir=conf))
    with TestClient(mine) as c:
        headers = browser(mine, "admin")
        # no key yet: the page offers to make it
        assert 'id="make-key"' in c.get("/links", headers=headers).text
        public = c.post("/api/links/ssh/key", headers=headers).json()["public_key"]
        assert (conf / "ssh" / "id_ed25519").exists()
        # the link: the customer's OmniaCLI as its SSH server would reach it
        body = {"name": "cliente", "url": url, "token": LINK_TOKEN, "new": True,
                "ssh": {**SSH, "host": "127.0.0.1", "port": 2222}}
        assert c.post("/api/links/save", json=body, headers=headers).json() == {"ok": True}
        tunnels = mine.state.ssh_tunnels
        _until(lambda: (tunnels.status("cliente") or {}).get("state") == "up")
        link = mine.state.settings.link_targets["cliente"]
        assert link.address == f"http://127.0.0.1:{tunnels.status('cliente')['port']}"
        call = _calls(fake_ssh)[-1]
        assert call["args"][-1] == "127.0.0.1" and ["-p", "2222"] == call["args"][-5:-3]
        assert (conf / "ssh" / "known_hosts_cliente").read_text() == f"omniacli-link-cliente {HOST_KEY}\n"
        # through it: the link's test, its pages, its servers on the Dashboard
        test = c.post("/api/links/cliente/test", headers=headers).json()
        assert test["ok"] and test["app"] == "OmniaCLI", test
        assert c.get("/links/cliente/servers/srv", headers=headers).status_code == 200
        page = html.unescape(c.get("/links", headers=headers).text)
        assert f'permitopen="127.0.0.1:{url.rsplit(":", 1)[1]}",command="/bin/false" {public}' in page
        assert "Match User omniacli-link" in page and ">up<" in page
        # ssh dropped: down, then again, on the same port
        first = call["pid"]
        os.kill(first, signal.SIGKILL)
        _until(lambda: len({x["pid"] for x in _calls(fake_ssh)}) == 2
               and tunnels.status("cliente")["state"] == "up")
        assert c.post("/api/links/cliente/test", headers=headers).json()["ok"]
        # refused by the server: down, with ssh's own reason
        (fake_ssh.parent / (fake_ssh.name + ".fail")).write_text("")
        os.kill(_calls(fake_ssh)[-1]["pid"], signal.SIGKILL)
        _until(lambda: "Permission denied" in tunnels.status("cliente")["detail"])
        assert tunnels.status("cliente")["state"] == "down"
        refused = c.post("/api/links/cliente/test", headers=headers).json()
        assert not refused["ok"] and "SSH tunnel down: Permission denied" in refused["error"]
        (fake_ssh.parent / (fake_ssh.name + ".fail")).unlink()
        _until(lambda: tunnels.status("cliente")["state"] == "up")
        running = _calls(fake_ssh)[-1]["pid"]
        # the link without SSH again: its ssh ends
        body = {"name": "cliente", "url": url, "token": "", "ssh": None}
        assert c.post("/api/links/save", json=body, headers=headers).json() == {"ok": True}
        _until(lambda: not _alive(running))
        assert tunnels.status("cliente") is None
        assert mine.state.settings.link_targets["cliente"].address == url
        # and with it once more: OmniaCLI stopping ends it
        body["ssh"] = {**SSH, "host": "127.0.0.1"}
        assert c.post("/api/links/save", json=body, headers=headers).json() == {"ok": True}
        _until(lambda: (tunnels.status("cliente") or {}).get("state") == "up")
        last = _calls(fake_ssh)[-1]["pid"]
    _until(lambda: not _alive(last))
    # saved as given: the host key, no port of this machine
    saved = yaml.safe_load((conf / "links.yaml").read_text())["links"]["cliente"]
    assert saved["ssh"] == {**SSH, "host": "127.0.0.1", "port": 22} and "local_port" not in saved


def test_the_host_key_route_refuses_options(env):
    app = create_app(env["settings"])
    with TestClient(app) as c:
        resp = c.post("/api/links/ssh/host-key", json={"host": "-oProxyCommand=x", "port": 22},
                      headers=browser(app, "admin"))
        assert resp.status_code == 400 and "is not a host name" in resp.json()["error"]
        assert c.post("/api/links/ssh/host-key", json={"host": "127.0.0.1"},
                      headers=browser(app, "dev")).status_code == 403


@pytest.mark.skipif(not shutil.which("ssh-keyscan"), reason="no ssh-keyscan")
async def test_fetch_host_key_checks_only_the_address():
    # nothing answers on port 1 here: the address was accepted, the server is missing
    with pytest.raises(sshlinks.SshError, match="no SSH server answered on 127.0.0.1:1"):
        await sshlinks.fetch_host_key("127.0.0.1", 1)

