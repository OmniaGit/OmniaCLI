# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nginx site of a workspace, from validated names and ports only (the
root helper writes it: workspacehelper.web), and the names other sites of
the machine's nginx already answer on (nginx -T), which a workspace may not
take."""

from __future__ import annotations

import re

# the challenges of Let's Encrypt (certbot --webroot), root's
ACME_ROOT = "/var/lib/omniacli-acme"
# an upload to Odoo (an attachment, a database restore is not done here)
MAX_BODY = "200m"
# Odoo's longest requests (a report, an import) before nginx gives up
TIMEOUT = "720s"
# server_names_hash_bucket_size: names up to about 250 characters
HASH_BUCKET = 256

_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$proxy_add_x_forwarded_for"),
    ("X-Forwarded-Proto", "$scheme"),
    ("X-Real-IP", "$remote_addr"),
)


def _headers(indent: str) -> list[str]:
    return [f"{indent}proxy_set_header {name} {value};" for name, value in _HEADERS]


def conf_text(workspace: str, sites: list[dict], tls: tuple[str, str] | None, ipv6: bool) -> str:
    """sites: [{instance, role, names, http_port, websocket_port}]. Port 80
    answers the challenges and sends the rest to https; without a
    certificate yet (tls None) it answers 503, and there is no 443."""
    names = [n for site in sites for n in site["names"]]
    out = [f"# written by omniacli workspace-helper: the names of workspace {workspace}.",
           "# Changed here, it is written again at the workspace's next change.", ""]
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    out += ["server {", *listen80, f"    server_name {' '.join(names)};",
            "    location /.well-known/acme-challenge/ {", f"        root {ACME_ROOT};", "    }",
            "    location / {",
            "        return 301 https://$host$request_uri;" if tls else "        return 503;",
            "    }", "}"]
    if tls is None:
        return "\n".join(out) + "\n"
    listen443 = ["    listen 443 ssl;"] + (["    listen [::]:443 ssl;"] if ipv6 else [])
    for site in sites:
        # Odoo threaded (workers = 0) answers its websocket on its http port
        ws_port = site["websocket_port"] or site["http_port"]
        out += ["", f"# {site['instance']} {site['role']}", "server {", *listen443,
                f"    server_name {' '.join(site['names'])};",
                f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
                f"    client_max_body_size {MAX_BODY};",
                f"    proxy_read_timeout {TIMEOUT};", f"    proxy_send_timeout {TIMEOUT};",
                "    location /websocket {",
                f"        proxy_pass http://127.0.0.1:{ws_port};",
                "        proxy_http_version 1.1;",
                # a location's proxy_set_header replaces the server's: all again
                *_headers("        "),
                "        proxy_set_header Upgrade $http_upgrade;",
                '        proxy_set_header Connection "upgrade";',
                "    }",
                "    location / {",
                f"        proxy_pass http://127.0.0.1:{site['http_port']};",
                *_headers("        "),
                "        proxy_redirect off;",
                "    }", "}"]
    return "\n".join(out) + "\n"


_FILE = re.compile(r"^# configuration file (.+):$")
_SERVER_NAME = re.compile(r"(?:^|[;{}\s])server_name\s+([^;]*);")


def names_elsewhere(dump: str, own_file: str) -> dict[str, str]:
    """The server names of nginx -T's dump, outside own_file: {name: file}.
    Regular expressions (~...) are left out: they cannot be compared."""
    found: dict[str, str] = {}
    current = ""
    chunks: dict[str, list[str]] = {}
    for line in dump.splitlines():
        match = _FILE.match(line)
        if match:
            current = match.group(1)
            continue
        chunks.setdefault(current, []).append(line.split("#", 1)[0])
    for path, lines in chunks.items():
        if path == own_file:
            continue
        for match in _SERVER_NAME.finditer("\n".join(lines)):
            for name in match.group(1).split():
                name = name.lower().strip("\"'")
                if name and name != "_" and not name.startswith("~"):
                    found.setdefault(name, path)
    return found


def clashes(names: list[str], elsewhere: dict[str, str]) -> list[str]:
    """The names another site takes: the same name, or a wildcard of it
    (*.acme.it, .acme.it, www.*)."""
    found = []
    for name in names:
        for other, path in elsewhere.items():
            if other.startswith("*."):
                hit = name.endswith(other[1:])
            elif other.startswith("."):
                hit = name == other[1:] or name.endswith(other)
            elif other.endswith(".*"):
                hit = name.startswith(other[:-1])
            else:
                hit = name == other
            if hit:
                found.append(f"{name} ({other} in {path})")
    return found


def workers(conf: str) -> int:
    """workers of an odoo.conf, 0 when not set."""
    for line in conf.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "workers" and value.strip().isdigit():
            return int(value.strip())
    return 0
