# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The names of a workspace: the nginx site the root helper writes, its
certificate (Let's Encrypt or the customer's), and the workspace page that
sets them."""
import json
import shutil
import subprocess

import pytest

from omniacli import workspacehelper, workspaceops, workspaceweb
from omniacli.composehelper import HelperError
from omniacli.config import ConfigError, Workspace, parse_workspaces, server_to_dict, web_names

from .test_app import POST, client, wait_job  # noqa: F401 - the client fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture


def test_web_names():
    sites = web_names("acme.it", ["main", "shop"], "main", {"www": "main", "negozio": "shop"})
    assert [(s["instance"], s["role"], s["names"]) for s in sites] == [
        ("main", "production", ["acme.it", "main.acme.it", "www.acme.it"]),
        ("main", "test", ["test.acme.it", "main.test.acme.it", "www.test.acme.it"]),
        ("shop", "production", ["shop.acme.it", "negozio.acme.it"]),
        ("shop", "test", ["shop.test.acme.it", "negozio.test.acme.it"])]
    # no main instance: the domain alone goes nowhere
    assert [s["names"] for s in web_names("acme.it", ["shop"])] == [["shop.acme.it"], ["shop.test.acme.it"]]
    with pytest.raises(ConfigError, match="longer"):
        web_names("a" * 60 + "." + "b" * 60 + "." + "c" * 60 + "." + "d" * 60 + ".it", ["main"])


def test_parse_the_names():
    base = {"domain": "acme.it", "servers": ["acme-main", "acme-shop", "other"]}
    ws = parse_workspaces({"acme": {**base, "main": "main", "subdomains": {"WWW": "main"}, "certificate": "own"}})["acme"]
    assert (ws.instances(), ws.main, ws.subdomains, ws.certificate) == (["main", "shop"], "main", {"www": "main"}, "own")
    assert ws.to_dict()["subdomains"] == {"www": "main"}
    assert "certificate" not in parse_workspaces({"acme": base})["acme"].to_dict()
    for change, message in (({"main": "nope"}, "not an instance"),
                            ({"subdomains": {"test": "main"}}, "not a subdomain"),
                            ({"subdomains": {"a.b": "main"}}, "not a subdomain"),
                            ({"subdomains": {"shop": "main"}}, "name of an instance"),
                            ({"subdomains": {"www": "other"}}, "not an instance"),
                            ({"certificate": "self"}, "certificate")):
        with pytest.raises(ConfigError, match=message):
            parse_workspaces({"acme": {**base, **change}})
    # a domain under another workspace's: their names would meet
    with pytest.raises(ConfigError, match="is under"):
        parse_workspaces({"a": {"domain": "acme.it"}, "b": {"domain": "shop.acme.it"}})


def test_conf_text():
    sites = [{"instance": "main", "role": "production", "names": ["acme.it", "main.acme.it"],
              "http_port": 20000, "websocket_port": None},
             {"instance": "main", "role": "test", "names": ["test.acme.it"], "http_port": 20002, "websocket_port": 20003}]
    http = workspaceweb.conf_text("acme", sites, None, ipv6=False)
    assert "server_name acme.it main.acme.it test.acme.it;" in http and "return 503;" in http
    assert "listen 443" not in http and "[::]" not in http and f"root {workspaceweb.ACME_ROOT};" in http
    https = workspaceweb.conf_text("acme", sites, ("/c/fullchain.pem", "/c/privkey.pem"), ipv6=True)
    assert "return 301 https://$host$request_uri;" in https and https.count("listen [::]:443 ssl;") == 2
    production, test = https.split("# main test")
    # threaded Odoo: its websocket on the http port; with workers, on its own
    assert "proxy_pass http://127.0.0.1:20000;" in production and "20001" not in production
    assert "location /websocket {\n        proxy_pass http://127.0.0.1:20003;" in test
    assert test.count("proxy_set_header X-Forwarded-Proto $scheme;") == 2
    assert workspaceweb.workers("[options]\nworkers = 4\n") == 4 and workspaceweb.workers("[options]\n") == 0


DUMP = """nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
# configuration file /etc/nginx/nginx.conf:
http {
    # server_names_hash_bucket_size 64;
    include /etc/nginx/conf.d/*.conf;
}
# configuration file /etc/nginx/conf.d/omniacli-acme.conf:
server { server_name acme.it main.acme.it; }
# configuration file /etc/nginx/sites-enabled/default:
server {
    listen 80 default_server;
    server_name _;
}
# configuration file /etc/nginx/sites-enabled/other:
server { server_name   www.other.it other.it; # the old one
    server_name ~^(?<x>.+)\\.regex\\.it$;
}
server {server_name *.wild.it .dot.it mail.*;}
"""


def test_names_of_other_sites():
    elsewhere = workspaceweb.names_elsewhere(DUMP, "/etc/nginx/conf.d/omniacli-acme.conf")
    assert elsewhere == {"www.other.it": "/etc/nginx/sites-enabled/other", "other.it": "/etc/nginx/sites-enabled/other",
                         "*.wild.it": "/etc/nginx/sites-enabled/other", ".dot.it": "/etc/nginx/sites-enabled/other",
                         "mail.*": "/etc/nginx/sites-enabled/other"}
    assert workspaceweb.clashes(["acme.it", "other.it", "x.wild.it", "wild.it", "dot.it", "a.dot.it", "mail.acme.it"],
                                elsewhere) == [
        "other.it (other.it in /etc/nginx/sites-enabled/other)", "x.wild.it (*.wild.it in /etc/nginx/sites-enabled/other)",
        "dot.it (.dot.it in /etc/nginx/sites-enabled/other)", "a.dot.it (.dot.it in /etc/nginx/sites-enabled/other)",
        "mail.acme.it (mail.* in /etc/nginx/sites-enabled/other)"]


@pytest.fixture
def web_root(root, monkeypatch, tmp_path):
    """The root fixture with nginx and certbot answered here: an instance
    main of acme on the disk, nginx -T giving DUMP_FILES' sites."""
    for name, path in (("NGINX_SITES", "nginx"), ("LETSENCRYPT", "letsencrypt"), ("OWN_CERTS", "certs")):
        (tmp_path / path).mkdir()
        monkeypatch.setattr(workspacehelper, name, tmp_path / path)
    monkeypatch.setattr(workspaceweb, "ACME_ROOT", str(tmp_path / "acme-challenges"))
    monkeypatch.setattr(workspacehelper, "_tool", lambda name: shutil.which(name) or name)
    monkeypatch.setattr(workspacehelper, "_system", lambda config=None: {"workspaces": {"acme_email": "ops@acme.it"}})
    workspacehelper.disk("acme", str(root["storage"]), "2")
    workspacehelper.limits("acme", "0", "0")
    workspacehelper.create("acme", str(root["storage"]), "main", "odooplm-20", "{}")
    # the site of the workspace in the dump is its own: not another's
    state = {"dump": DUMP.replace("/etc/nginx/conf.d/omniacli-acme.conf", str(tmp_path / "nginx" / "omniacli-acme.conf")),
             "nginx_t": 0, "certbot": 0, "commands": []}
    real_run = subprocess.run

    def run(cmd, **kw):
        state["commands"].append(cmd)
        name = cmd[0].rsplit("/", 1)[-1]
        if name == "nginx":
            if cmd[1] == "-T":
                return subprocess.CompletedProcess(cmd, 0, state["dump"], "")
            return subprocess.CompletedProcess(cmd, state["nginx_t"], "", "nginx: [emerg] broken")
        if name == "certbot":
            if not state["certbot"]:
                lineage = tmp_path / "letsencrypt" / "omniacli-acme"
                lineage.mkdir(exist_ok=True)
                (lineage / "fullchain.pem").write_text("chain")
                (lineage / "privkey.pem").write_text("key")
            return subprocess.CompletedProcess(cmd, state["certbot"], "", "Challenge failed for domain main.acme.it")
        return real_run(cmd, **kw)
    monkeypatch.setattr(workspacehelper.subprocess, "run", run)
    return {**root, "tmp": tmp_path, "web": state, "site": tmp_path / "nginx" / "omniacli-acme.conf"}


def spec(**change) -> str:
    return json.dumps({"domain": "acme.it", "main": "main", "subdomains": {}, "certificate": "letsencrypt", **change})


def test_helper_web_letsencrypt(web_root):
    storage, state = str(web_root["storage"]), web_root["web"]
    made = workspacehelper.web("acme", storage, spec(subdomains={"www": "main"}))
    assert made["names"] == ["acme.it", "main.acme.it", "www.acme.it", "test.acme.it", "main.test.acme.it",
                             "www.test.acme.it"]
    assert made["https"] and made["certificate_error"] is None
    certbot = [c for c in state["commands"] if c[0].endswith("certbot")][0]
    assert certbot[certbot.index("--cert-name") + 1] == "omniacli-acme" and ["-m", "ops@acme.it"] == certbot[-14:-12]
    assert [certbot[i + 1] for i, a in enumerate(certbot) if a == "-d"] == made["names"]
    site = web_root["site"].read_text()
    # the ports of the instance's .env, read by the helper; threaded: websocket on http
    assert "proxy_pass http://127.0.0.1:20000;" in site and "proxy_pass http://127.0.0.1:20002;" in site
    assert "20001" not in site and "ssl_certificate " + str(web_root["tmp"] / "letsencrypt" / "omniacli-acme") in site
    assert (web_root["tmp"] / "nginx" / "omniacli.conf").read_text().endswith("server_names_hash_bucket_size 256;\n")
    assert ["systemctl", "reload", "nginx"] in web_root["calls"] or ["systemctl", "is-active", "nginx"] in web_root["calls"]


def test_helper_web_refusals_and_rollback(web_root):
    storage, state = str(web_root["storage"]), web_root["web"]
    for change, message in (({"domain": "not a domain"}, "not a domain name"), ({"main": "nope"}, "not an instance"),
                            ({"subdomains": {"test": "main"}}, "not a subdomain"),
                            ({"subdomains": {"main": "main"}}, "not a subdomain"),
                            ({"subdomains": {"www": "nope"}}, "not an instance"), ({"certificate": "x"}, "one of")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.web("acme", storage, spec(**change))
    with pytest.raises(HelperError, match="not JSON"):
        workspacehelper.web("acme", storage, "{")
    # a name another site of nginx has
    with pytest.raises(HelperError, match=r"www\.other\.it \(www\.other\.it in /etc/nginx/sites-enabled/other\)"):
        workspacehelper.web("acme", storage, json.dumps({"domain": "other.it", "main": "", "subdomains": {"www": "main"}}))
    assert not web_root["site"].exists()
    # nginx -t refuses: the site before is back
    workspacehelper.web("acme", storage, spec())
    before = web_root["site"].read_text()
    state["nginx_t"] = 1
    with pytest.raises(HelperError, match="nginx -t refused"):
        workspacehelper.web("acme", storage, spec(subdomains={"www": "main"}))
    assert web_root["site"].read_text() == before


def test_helper_web_without_certificate_and_removed(web_root):
    storage, state = str(web_root["storage"]), web_root["web"]
    state["certbot"] = 1
    made = workspacehelper.web("acme", storage, spec())
    assert not made["https"] and "Challenge failed" in made["certificate_error"]
    assert "return 503;" in web_root["site"].read_text() and "listen 443" not in web_root["site"].read_text()
    # the customer's own certificate, not there yet: no certbot
    state["commands"].clear()
    made = workspacehelper.web("acme", storage, spec(certificate="own"))
    assert "upload it" in made["certificate_error"] and not [c for c in state["commands"] if c[0].endswith("certbot")]
    # no domain: the site goes, and Let's Encrypt's certificate with it
    lineage = web_root["tmp"] / "letsencrypt" / "omniacli-acme"
    lineage.mkdir()
    made = workspacehelper.web("acme", storage, spec(domain=""))
    assert not web_root["site"].exists() and made["names"] == []
    assert ["certbot", "delete", "--cert-name", "omniacli-acme", "--non-interactive"] in web_root["calls"]


def _certificate(folder, name, days=2):
    key, cert = folder / f"{name}.key", folder / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", str(days), "-subj", "/CN=acme.it", "-addext", "subjectAltName=DNS:acme.it,DNS:*.acme.it"],
                   check=True, capture_output=True)
    return cert.read_text(), key.read_text()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_helper_own_certificate(web_root):
    storage, tmp = str(web_root["storage"]), web_root["tmp"]
    cert, key = _certificate(tmp, "good")
    _, other_key = _certificate(tmp, "other")
    made = workspacehelper.owncert("acme", storage, json.dumps({"certificate": cert, "key": key}))
    assert made["names"] == ["acme.it", "*.acme.it"] and made["not_after"]
    folder = tmp / "certs" / "acme"
    assert (folder / "fullchain.pem").read_text() == cert.strip() + "\n"
    assert oct((folder / "privkey.pem").stat().st_mode & 0o777) == "0o600" and oct(folder.stat().st_mode & 0o777) == "0o700"
    for given, message in (({"certificate": cert, "key": other_key}, "not the certificate's"),
                           ({"certificate": "x", "key": key}, "are needed"), ([1], "not JSON")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.owncert("acme", storage, json.dumps(given))
    with pytest.raises(HelperError, match="more than"):
        workspacehelper.owncert("acme", storage, " " * (workspacehelper.MAX_PEM + 1))
    # the one before stays, no new file is left
    assert (folder / "privkey.pem").read_text() == key.strip() + "\n" and sorted(p.name for p in folder.iterdir()) == [
        "fullchain.pem", "privkey.pem"]
    made = workspacehelper.web("acme", storage, spec(certificate="own"))
    assert made["https"] and f"ssl_certificate {folder / 'fullchain.pem'};" in web_root["site"].read_text()


async def test_workspace_page_sets_the_names(workspaces, client, env, monkeypatch):
    settings = env["settings"]
    published = []

    async def publish(job, ws):
        published.append((ws.name, ws.main, dict(ws.subdomains), ws.certificate))
        return {}
    monkeypatch.setattr(workspaceops, "publish", publish)
    settings.workspaces["acme"].servers = ["srv", "acme-main"]
    settings.servers["acme-main"] = settings.servers["srv"]
    settings.workspaces["acme"].storage = "/srv/omniacli"
    url = "/api/workspaces/acme/web/"
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert 'id="save-names"' in page and "main.acme.example.com" in page
    for body, message in (({"main": "nope"}, "not an instance"), ({"subdomains": {"test": "main"}}, "not a subdomain"),
                          ({"certificate": "self"}, "certificate")):
        resp = await client.post(url + "save", headers=post_as("mario"), json=body)
        assert resp.status_code == 400 and message in resp.json()["error"], body
    assert (await client.post(url + "save", headers=post_as("luca"), json={})).status_code == 403
    resp = await client.post(url + "save", headers=post_as("mario"),
                             json={"main": "main", "subdomains": {"www": "main"}, "certificate": "letsencrypt"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and published[-1] == ("acme", "main", {"www": "main"}, "letsencrypt")
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "https://www.test.acme.example.com/" in page
    # the machine's admins change the domain: the names stay, published again
    resp = await client.post("/api/workspaces/save", headers=POST, json={"name": "acme", "domain": "acme.it", "servers": ["srv", "acme-main"],
                                     "members": {"mario": "admin", "luca": "user"}})
    assert resp.status_code == 200, resp.text
    await wait_job(client, resp.json()["job"])
    assert settings.workspaces["acme"].subdomains == {"www": "main"} and published[-1][0] == "acme"
    # an instance may not be named test or as a subdomain
    for instance in ("test", "www"):
        resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                                 json={"instance": instance, "template": "odooplm-20"})
        assert resp.status_code == 400 and "another name" in resp.json()["error"]
    # the customer's certificate: only once chosen
    resp = await client.post(url + "certificate", headers=post_as("mario"), json={"certificate": "c", "key": "k"})
    assert resp.status_code == 400 and "Let's Encrypt" in resp.json()["error"]


async def test_names_follow_their_instance(workspaces, client, env):
    """A server renamed or deleted on the configuration page: the main
    instance and the subdomains follow it, or are dropped."""
    settings = env["settings"]
    server = server_to_dict(settings.servers["srv"])
    for name in ("acme-main", "acme-shop"):
        resp = await client.post("/api/servers/save", headers=POST, json={"original": None, "name": name, "server": server})
        assert resp.status_code == 200, resp.text
    settings.workspaces["acme"] = Workspace("acme", domain="acme.it", servers=["srv", "acme-main", "acme-shop"],
                                            main="main", subdomains={"www": "main", "negozio": "shop"})
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "acme-main", "name": "acme-first", "server": server})
    assert resp.status_code == 200, resp.text
    acme = settings.workspaces["acme"]
    assert (acme.main, acme.subdomains) == ("first", {"www": "first", "negozio": "shop"})
    # out of the workspace's names (no acme- prefix): dropped
    resp = await client.post("/api/servers/save", headers=POST,
                             json={"original": "acme-first", "name": "elsewhere", "server": server})
    acme = settings.workspaces["acme"]
    assert (acme.main, acme.subdomains) == ("", {"negozio": "shop"})
    assert (await client.post("/api/servers/delete", headers=POST, json={"name": "acme-shop"})).status_code == 200
    acme = settings.workspaces["acme"]
    assert acme.servers == ["srv", "elsewhere"] and acme.subdomains == {}
