# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The templates of the workspaces' instances, and the root helper that
makes their disks and folders (its system commands answered here)."""
import json
import os
import shutil
from pathlib import Path

import pytest

from omniacli import composehelper, stacktemplates, workspacehelper
from omniacli.composehelper import HelperError

REPO_TEMPLATES = Path(__file__).parent.parent / "templates"


def copy_template(tmp_path, compose=None) -> Path:
    folder = tmp_path / "templates" / "odooplm-20"
    shutil.copytree(REPO_TEMPLATES / "odooplm-20", folder)
    if compose is not None:
        (folder / "compose.yaml").write_text(compose)
    return folder


def test_the_template_of_the_repository():
    template = stacktemplates.load_template(REPO_TEMPLATES / "odooplm-20")
    assert (template.odoo_service, template.db_service) == ("odoo", "db")
    assert template.values({}) == {"IMAGE": "ghcr.io/omniagit/odooplm:20.0", "INIT_MODULES": "plm", "INIT_LANG": ""}
    assert template.values({"INIT_MODULES": "plm,plm_web_3d", "INIT_LANG": "it_IT"})["INIT_LANG"] == "it_IT"
    for bad in ({"IMAGE": "evil/image"}, {"INIT_MODULES": "plm;rm"}, {"INIT_LANG": "xx"}, {"OTHER": "1"},
                {"INIT_MODULES": "a\nb"}):
        with pytest.raises(stacktemplates.TemplateError):
            template.values(bad)
    conf = stacktemplates.conf_text(template, "acme_main", "pw", ["custom", "more"])
    assert "addons_path = /mnt/odooplm-addons,/mnt/customer-addons/custom,/mnt/customer-addons/more" in conf
    assert "dbfilter = ^acme_main$" in conf and "list_db = False" in conf and "http_interface = 0.0.0.0" in conf
    assert stacktemplates.env_text({"A": "1", "B": "x y"}) == "A=1\nB=x y\n"
    with pytest.raises(stacktemplates.TemplateError):
        stacktemplates.env_text({"A": "x\nB=2"})


@pytest.mark.parametrize("change, message", [
    (("    cgroup_parent: ${SLICE}\n    depends_on:", "    cgroup_parent: ${SLICE}\n    privileged: true\n    depends_on:"),
     "privileged: not allowed"),
    (("    cgroup_parent: ${SLICE}\n    depends_on:", "    depends_on:"), "cgroup_parent: must be"),
    (("    cgroup_parent: ${SLICE}\n    environment:", "    cgroup_parent: system.slice\n    environment:"),
     "cgroup_parent: must be"),
    (('"127.0.0.1:${HTTP_PORT}:8069"', '"0.0.0.0:${HTTP_PORT}:8069"'), "is not 127.0.0.1"),
    (("./data/filestore:/var/lib/odoo", "/:/host"), "is not ./<folder>"),
    (("./data/filestore:/var/lib/odoo", "../../other:/var/lib/odoo"), "leaves the instance"),
    (("./config:/etc/odoo:ro", "./config:/etc/odoo"), "read only"),
    (("./addons:/mnt/customer-addons:ro", "./addons:/mnt/customer-addons"), "read only"),
    (("./addons:/mnt/customer-addons:ro", "../shared:/mnt/customer-addons"), "read only"),
    (("image: ${IMAGE}", "image: ${SECRET}"), "neither a field"),
    (("name: ${PROJECT}", "name: ${PROJECT}\nvolumes: {}"), "volumes: not allowed"),
    (("        max-file: \"5\"\n  db:", "  db:"), "logging"),
    (("    environment:\n      HOST: db", "    network_mode: host\n    environment:\n      HOST: db"), "network_mode"),
])
def test_compose_rules(tmp_path, change, message):
    text = (REPO_TEMPLATES / "odooplm-20" / "compose.yaml").read_text()
    assert change[0] in text, change[0]
    folder = copy_template(tmp_path, text.replace(change[0], change[1], 1))
    with pytest.raises(stacktemplates.TemplateError, match=message):
        stacktemplates.load_template(folder)


def test_template_must_be_named_as_its_folder(tmp_path):
    folder = copy_template(tmp_path)
    renamed = folder.with_name("other")
    folder.rename(renamed)
    with pytest.raises(stacktemplates.TemplateError, match="folder"):
        stacktemplates.load_template(renamed)
    assert stacktemplates.available([tmp_path / "templates", tmp_path / "none"]) == {}


def test_compose_helper_init(monkeypatch, tmp_path):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    assert composehelper.commands(tmp_path, "init", ["odoo", "acme", "plm,base", "it_IT"]) == [
        ["run", "--rm", "-T", "odoo", "odoo", "-d", "acme", "-i", "plm,base", "--stop-after-init",
         "--without-demo=all", "--no-http", "--load-language", "it_IT"]]
    assert composehelper.commands(tmp_path, "init", ["odoo", "acme", "base"])[0][-1] == "--no-http"
    for args in (["odoo", "acme"], ["odoo", "acme", "plm;x"], ["odoo", "acme", "base", "italian"],
                 ["nope", "acme", "base"], ["odoo", "a b", "base"]):
        with pytest.raises(HelperError):
            composehelper.commands(tmp_path, "init", args)


@pytest.fixture
def root(monkeypatch, tmp_path):
    """The helper as root, its system commands recorded: the storage root
    tmp_path/storage, the templates a copy of the repository's, the disk
    mounted once systemctl enables it."""
    storage = tmp_path / "storage"
    storage.mkdir()
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    copy_template(tmp_path)
    monkeypatch.setattr(workspacehelper, "TEMPLATES", tmp_path / "templates")
    monkeypatch.setattr(workspacehelper, "SYSTEMD", systemd)
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config=None: (str(storage),))
    # files of the test's own: the template's checks see a caller who could
    # not change them (the refusal: test_the_template_is_refused...)
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("omniacli", 0, [0]))
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *args: None)
    monkeypatch.setattr(workspacehelper, "pick_ports", lambda count: list(range(20000, 20000 + count)))
    calls, state = [], {"mounted": False, "jumps": set(), "ip": "", "networks": ""}

    def run(*cmd, check=True):
        calls.append(list(cmd))
        if cmd[0] == "systemd-escape":
            return "srv-acme.mount\n"
        if cmd[:3] == ("systemctl", "enable", "--now"):
            state["mounted"] = True
        if cmd[:3] == ("iptables", "-w", "-I"):
            state["jumps"].add((cmd[3], cmd[-1]))
        if cmd[:3] == ("iptables", "-w", "-D"):
            state["jumps"].discard((cmd[3], cmd[-1]))
        if cmd[:2] == ("ip", "-4"):
            return state["ip"]
        if cmd[:3] == ("docker", "network", "ls"):
            return "abc\n" if state["networks"] else ""
        if cmd[:3] == ("docker", "network", "inspect"):
            return state["networks"]
        return ""
    # the main configuration of the test: no workspaces.network (the default pool)
    monkeypatch.setattr(workspacehelper, "_system", lambda config=None: {})
    monkeypatch.setattr(workspacehelper, "_has_jump", lambda parent, chain: (parent, chain) in state["jumps"])
    monkeypatch.setattr(workspacehelper, "_run", run)
    monkeypatch.setattr(workspacehelper, "mounted", lambda image, mount: state["mounted"])
    monkeypatch.setattr(workspacehelper, "loop_of", lambda image: "/dev/loop9")
    # the storage is the test's, writable by it: as root nothing is refused
    monkeypatch.setattr(workspacehelper, "_safe_folder", lambda folder, what: None)
    return {"storage": storage, "systemd": systemd, "calls": calls, "state": state}


def test_disk(root):
    made = workspacehelper.disk("acme", str(root["storage"]), "2")
    image = root["storage"] / ".disks" / "acme.img"
    assert made["unit"] == "srv-acme.mount" and image.stat().st_size == 2 * 1024 ** 3
    assert ["mkfs.ext4", "-q", "-F", str(image)] in root["calls"]
    assert ["chattr", "+i", str(root["storage"] / "acme")] in root["calls"]
    unit = (root["systemd"] / "srv-acme.mount").read_text()
    assert f"What={image}" in unit and "Options=loop" in unit and "Before=docker.service" in unit
    assert (root["systemd"] / "docker.service.d" / "omniacli-acme.conf").read_text().endswith("After=srv-acme.mount\n")
    assert ["systemctl", "enable", "--now", "srv-acme.mount"] in root["calls"]
    # larger: grown, mounted; smaller: refused
    root["calls"].clear()
    workspacehelper.disk("acme", str(root["storage"]), "3")
    assert image.stat().st_size == 3 * 1024 ** 3
    assert ["mkfs.ext4", "-q", "-F", str(image)] not in root["calls"]
    assert ["resize2fs", "/dev/loop9"] in root["calls"] and ["losetup", "--set-capacity", "/dev/loop9"] in root["calls"]
    with pytest.raises(HelperError, match="not made smaller"):
        workspacehelper.disk("acme", str(root["storage"]), "1")
    for args, message in ((("Acme", str(root["storage"]), "2"), "not a workspace name"),
                          (("acme", "/elsewhere", "2"), "not one of workspaces.storage"),
                          (("acme", str(root["storage"]), "0"), "size"),
                          (("acme", str(root["storage"]), "2x"), "size")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.disk(*args)


def test_create_and_remove(root, monkeypatch):
    storage = root["storage"]
    with pytest.raises(HelperError, match="not mounted"):
        workspacehelper.create("acme", str(storage), "main", "odooplm-20", "{}")
    workspacehelper.disk("acme", str(storage), "2")
    with pytest.raises(HelperError, match="limits first"):
        workspacehelper.create("acme", str(storage), "main", "odooplm-20", "{}")
    workspacehelper.limits("acme", "2", "4")
    made = workspacehelper.create("acme", str(storage), "main", "odooplm-20", json.dumps({"INIT_LANG": "it_IT"}))
    assert made["database"] == "main" and made["values"]["INIT_LANG"] == "it_IT"
    assert made["roles"]["production"]["http_port"] == 20000 and made["roles"]["test"]["websocket_port"] == 20003
    project = storage / "acme" / "main" / "production"
    env = (project / ".env").read_text()
    assert "PROJECT=acme-main-production\n" in env and "HTTP_PORT=20000\n" in env and "IMAGE=ghcr.io/omniagit/odooplm:20.0\n" in env
    assert oct((project / ".env").stat().st_mode & 0o777) == "0o600"
    assert (project / "compose.yaml").read_text() == (REPO_TEMPLATES / "odooplm-20" / "compose.yaml").read_text()
    assert "dbfilter = ^main$" in (project / "config" / "odoo.conf").read_text()
    # root's, readable by everyone: the image's user is no user of this machine
    assert oct((project / "config" / "odoo.conf").stat().st_mode & 0o777) == "0o644"
    assert (project / "data" / "filestore").is_dir() and (project / "data" / "pgdata").is_dir()
    # the customer's repositories: a folder per role; what the instance was made from
    assert (project / "addons").is_dir() and (storage / "acme" / "main" / "test" / "addons").is_dir()
    assert json.loads((storage / "acme" / "main" / "instance.json").read_text()) == {
        "template": "odooplm-20", "version": made["version"], "database": "main"}
    # every container in the workspace's slice
    assert "SLICE=omniacli_acme.slice\n" in env
    # each role its network of the pool, the firewall on before
    assert "SUBNET=10.213.0.0/24\n" in env and made["roles"]["test"]["subnet"] == "10.213.1.0/24"
    assert root["state"]["jumps"] == {("DOCKER-USER", "OMNIACLI-WS-FWD"), ("INPUT", "OMNIACLI-WS-IN")}
    assert (root["systemd"] / workspacehelper.FIREWALL_UNIT).is_file()
    # the two roles have their own password
    test_env = (storage / "acme" / "main" / "test" / ".env").read_text()
    password = [line for line in env.splitlines() if line.startswith("DB_PASSWORD=")][0]
    assert password not in test_env
    for args, message in ((("acme", str(storage), "main", "odooplm-20", "{}"), "exists already"),
                          (("acme", str(storage), "Main", "odooplm-20", "{}"), "not a instance name"),
                          (("acme", str(storage), "other", "nope", "{}"), "no template"),
                          (("acme", str(storage), "other", "../x", "{}"), "not a template name"),
                          (("acme", str(storage), "other", "odooplm-20", '{"IMAGE": "evil"}'), "not one of"),
                          (("acme", str(storage), "other", "odooplm-20", "[1]"), "not a JSON object")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.create(*args)
    assert not (storage / "acme" / "other").exists()
    # remove: down -v of both projects, then the folder
    downs = []
    monkeypatch.setattr(workspacehelper.subprocess, "run",
                        lambda cmd, **kw: downs.append(cmd) or type("Done", (), {"returncode": 0})())
    monkeypatch.setattr(composehelper.shutil, "which", lambda name, path=None: "/usr/bin/docker")
    workspacehelper.remove("acme", str(storage), "main")
    assert [c[-3:] for c in downs] == [["down", "-v", "--remove-orphans"]] * 2
    assert not (storage / "acme" / "main").exists()


def test_the_template_is_refused_when_the_caller_may_change_it(root, monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(workspacehelper, "TEMPLATES", tmp_path / "templates")
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("omniacli", os.getuid(), [os.getgid()]))
    with pytest.raises(HelperError, match="writable by the caller"):
        workspacehelper.template_named("odooplm-20")


def test_limits(root):
    made = workspacehelper.limits("acme-shop", "1.5", "4")
    assert made == {"workspace": "acme-shop", "slice": "omniacli_acme_shop.slice", "cpus": 1.5, "memory_gb": 4}
    unit = (root["systemd"] / "omniacli_acme_shop.slice").read_text()
    assert "[Slice]\nCPUQuota=150%\nMemoryMax=4294967296\nMemorySwapMax=0\n" in unit
    assert root["calls"][-1] == ["systemctl", "daemon-reload"]
    # 0: no limit, the unit written again
    workspacehelper.limits("acme-shop", "0", "0")
    unit = (root["systemd"] / "omniacli_acme_shop.slice").read_text()
    assert "[Slice]\nCPUQuota=\nMemoryMax=infinity\nMemorySwapMax=infinity\n" in unit
    for args, message in ((("Acme", "1", "1"), "not a workspace name"), (("acme", "-1", "1"), "CPU is 0"),
                          (("acme", "1", "x"), "memory is 0"), (("acme", "nan", "1"), "CPU is 0"),
                          (("acme", "1", "inf"), "memory is 0")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.limits(*args)


def test_run_needs_root(monkeypatch):
    with pytest.raises(HelperError, match="usage"):
        workspacehelper.run(["format"])
    with pytest.raises(HelperError, match="takes 3 arguments"):
        workspacehelper.run(["disk", "acme"])
    monkeypatch.setattr(workspacehelper.os, "geteuid", lambda: 1000)
    with pytest.raises(HelperError, match="as root"):
        workspacehelper.run(["disk", "acme", "/srv", "1"])


def test_compose_needs_the_network_of_the_pool(tmp_path):
    text = (REPO_TEMPLATES / "odooplm-20" / "compose.yaml").read_text()
    for compose in (text.replace("networks:\n  default:\n    ipam:\n      config:\n        - subnet: ${SUBNET}\n", ""),
                    text.replace("- subnet: ${SUBNET}", "- subnet: 10.0.0.0/24"),
                    text.replace("networks:\n  default:", "networks:\n  lan:\n    external: true\n  default:")):
        folder = copy_template(tmp_path / str(len(compose)), compose)
        with pytest.raises(stacktemplates.TemplateError, match="networks: must be exactly"):
            stacktemplates.load_template(folder)


def test_firewall_rules():
    rules = workspacehelper.firewall_rules(workspacehelper.ipaddress.ip_network("10.213.0.0/16"))
    forward, inbound = rules["OMNIACLI-WS-FWD"], rules["OMNIACLI-WS-IN"]
    replies = ["-s", "10.213.0.0/16", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"]
    # replies first; the pool to itself left to docker; then every private range dropped
    assert forward[0] == replies and forward[1] == ["-s", "10.213.0.0/16", "-d", "10.213.0.0/16", "-j", "RETURN"]
    assert [r[3] for r in forward[2:]] == list(workspacehelper.PRIVATE) and {r[-1] for r in forward[2:]} == {"DROP"}
    assert inbound == [replies, ["-s", "10.213.0.0/16", "-j", "DROP"]]


def test_firewall_on_and_off(root, monkeypatch):
    calls, state = root["calls"], root["state"]
    made = workspacehelper.firewall("on")
    assert made["pool"] == "10.213.0.0/16"
    assert ["iptables", "-w", "-A", "OMNIACLI-WS-IN", "-s", "10.213.0.0/16", "-j", "DROP"] in calls
    assert ["iptables", "-w", "-I", "INPUT", "1", "-j", "OMNIACLI-WS-IN"] in calls
    unit = (root["systemd"] / workspacehelper.FIREWALL_UNIT).read_text()
    assert "Before=docker.service" in unit and "workspace-helper firewall apply" in unit
    assert "WantedBy=multi-user.target docker.service" in unit
    # again: the chains flushed and filled, the jumps not doubled
    calls.clear()
    workspacehelper.firewall("apply")
    assert ["iptables", "-w", "-F", "OMNIACLI-WS-FWD"] in calls and not [c for c in calls if c[2] == "-I"]
    workspacehelper.firewall("off")
    assert state["jumps"] == set() and not (root["systemd"] / workspacehelper.FIREWALL_UNIT).exists()
    assert ["iptables", "-w", "-X", "OMNIACLI-WS-IN"] in calls
    with pytest.raises(HelperError, match="on, apply or off"):
        workspacehelper.firewall("maybe")


@pytest.mark.parametrize("pool, ip, message", [
    ("8.8.0.0/16", "", "not a private IPv4"), ("10.213.0.0/25", "", "not a private IPv4"),
    ("nope", "", "not a network"),
    ("10.0.0.0/8", "3: lxcbr0    inet 10.0.3.1/24 brd 10.0.3.255 scope global lxcbr0\n", "overlaps networks of this machine"),
])
def test_firewall_pool_refused(root, monkeypatch, pool, ip, message):
    monkeypatch.setattr(workspacehelper, "_system", lambda config=None: {"workspaces": {"network": pool}})
    root["state"]["ip"] = ip
    with pytest.raises(HelperError, match=message):
        workspacehelper.firewall("on")


def test_firewall_pool_holds_its_own_bridges(root):
    root["state"]["ip"] = "9: br-1234    inet 10.213.0.1/24 brd 10.213.0.255 scope global br-1234\n"
    assert workspacehelper.firewall("apply")["firewall"] == "on"


def test_subnets_skip_what_is_taken(root):
    root["state"]["networks"] = "172.17.0.0/16 10.213.0.0/24 \n10.213.2.0/23 \n"
    root["state"]["ip"] = "9: br-1234    inet 10.213.1.1/24 brd 10.213.1.255 scope global br-1234\n"
    assert workspacehelper.pick_subnets(2) == ["10.213.4.0/24", "10.213.5.0/24"]


def _conf(storage, role, **spec):
    return workspacehelper.conf("acme", str(storage), "main", role, json.dumps(spec))


def test_conf_names_the_repositories(root):
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    workspacehelper.create("acme", str(storage), "main", "odooplm-20", "{}")
    conf_file = storage / "acme" / "main" / "test" / "config" / "odoo.conf"
    password = [l for l in conf_file.read_text().splitlines() if l.startswith("admin_passwd")][0]
    with pytest.raises(HelperError, match="no repository custom"):
        _conf(storage, "test", repositories=["custom"])
    (storage / "acme" / "main" / "test" / "addons" / "custom").mkdir()
    (storage / "acme" / "main" / "test" / "addons" / "oca-web").mkdir()
    made = _conf(storage, "test", repositories=["custom", "oca-web"])
    text = conf_file.read_text()
    assert made["repositories"] == ["custom", "oca-web"]
    assert "addons_path = /mnt/odooplm-addons,/mnt/customer-addons/custom,/mnt/customer-addons/oca-web" in text
    # the admin password is kept; production is untouched
    assert password in text
    assert "customer-addons" not in (storage / "acme" / "main" / "production" / "config" / "odoo.conf").read_text()
    _conf(storage, "test", repositories=[])
    assert "addons_path = /mnt/odooplm-addons\n" in conf_file.read_text()
    for args, message in ((("acme", str(storage), "main", "stage", "{}"), "the role is"),
                          (("acme", str(storage), "main", "test", '{"repositories": ["../x"]}'), "list of names"),
                          (("acme", str(storage), "main", "test", "{"), "not JSON"),
                          (("acme", str(storage), "main", "test", "[]"), "JSON object"),
                          (("acme", str(storage), "nope", "test", "{}"), "no instance.json")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.conf(*args)


def test_conf_sets_the_parameters(root):
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    workspacehelper.create("acme", str(storage), "main", "odooplm-20", "{}")
    project = storage / "acme" / "main" / "test"
    (project / "addons" / "custom").mkdir()
    _conf(storage, "test", repositories=["custom"])
    made = _conf(storage, "test", parameters={"workers": 2, "limit_memory_hard": 3072, "smtp_password": "s3cret",
                                              "smtp_ssl": "on"}, debug=True)
    text = (project / "config" / "odoo.conf").read_text()
    assert "workers = 2\n" in text and "limit_memory_hard = 3221225472\n" in text and "smtp_ssl = True\n" in text
    assert "smtp_password = s3cret\n" in text and text.endswith("log_level = debug\n")
    # the repositories given before stay; the password is never given back
    assert "/mnt/customer-addons/custom" in text and made["parameters"]["smtp_password"] == ""
    state = json.loads((project / "omniacli.json").read_text())
    assert state["parameters"] == {"workers": 2, "limit_memory_hard": 3072, "smtp_password": "s3cret", "smtp_ssl": True}
    # a later change of the repositories keeps the parameters and the debug switch
    _conf(storage, "test", repositories=[])
    text = (project / "config" / "odoo.conf").read_text()
    assert "workers = 2\n" in text and "log_level = debug" in text
    _conf(storage, "test", parameters={}, debug=False)
    text = (project / "config" / "odoo.conf").read_text()
    assert "workers = 0\n" in text and "log_level" not in text and "smtp_password" not in text
    # the template's bounds, or the workspace's
    for parameters, message in (({"workers": 9}, "between 0 and 8"), ({"limit_time_cpu": "x"}, "whole number"),
                                ({"email_from": "a\nadmin_passwd = x"}, "one line"), ({"data_dir": "/"}, "no editable")):
        with pytest.raises(HelperError, match=message):
            _conf(storage, "test", parameters=parameters)
    _conf(storage, "test", parameters={"workers": 12}, bounds={"workers": [0, 16]})
    assert "workers = 12\n" in (project / "config" / "odoo.conf").read_text()
