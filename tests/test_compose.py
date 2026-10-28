# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restart type compose: a Docker Compose project, for a type that runs in one."""
import json

import pytest

from omniacli import checks, ops, plugins
from omniacli.config import ConfigError, parse_servers, server_to_dict
from omniacli.jobs import Job, JobFailed

from .test_plugins import Dummy, FakeEntryPoint

PROJECT = "/srv/odooplm-19"
BASE = ["docker", "compose", "--project-directory", PROJECT]


class ComposeDummy(Dummy):
    name = "composed"
    label = "Composed"

    @property
    def has_compose(self):
        return True


@pytest.fixture
def with_compose():
    plugins.set_registry(plugins.load(entry_points=[FakeEntryPoint("composed", ComposeDummy)]))
    yield plugins.registry().get("composed")
    plugins.set_registry(None)


def job():
    return Job(id="t", title="t", instances=[], source="test")


def _server(kind="composed", service=PROJECT, sudo=True, log_file="/var/log/x.log"):
    data = {"type": kind, "test": {"log_file": log_file,
                                   "restart": {"type": "compose", "service": service, "sudo": sudo},
                                   "repositories": []}}
    if kind == "odoo":
        data["test"].update(database="db", odoo_config="/c", venv_bin="/b")
    return parse_servers({"s": data})["s"]


def test_only_a_type_that_runs_in_compose(with_compose):
    with pytest.raises(ConfigError, match="does not run in Docker Compose"):
        _server("odoo")
    with pytest.raises(ConfigError, match="absolute path"):
        _server(service="srv/odooplm-19")
    srv = _server()
    assert srv.test.restart.type == "compose" and srv.test.restart.service == PROJECT
    saved = server_to_dict(srv)["test"]
    assert saved["restart"] == {"type": "compose", "service": PROJECT, "sudo": True}


def test_the_commands(with_compose):
    restart = _server().test.restart
    assert restart.command() == ["sudo", "-n", *BASE, "restart"]
    assert restart.command("start") == ["sudo", "-n", *BASE, "up", "-d"]
    assert restart.command("stop") == ["sudo", "-n", *BASE, "stop"]
    assert restart.command("state") == ["sudo", "-n", *BASE, "ps", "-a", "--format", "json"]
    assert _server(sudo=False).test.restart.command("stop") == [*BASE, "stop"]


def _ps(*states):
    return "\n".join(json.dumps({"Service": f"s{i}", "State": s}) for i, s in enumerate(states))


def test_compose_state():
    assert ops.compose_state(_ps("running", "running")) == "active"
    assert ops.compose_state(_ps("running", "exited")) == "partial"
    assert ops.compose_state(_ps("exited", "created")) == "inactive"
    assert ops.compose_state("") == "inactive"
    # before Compose 2.21: one array
    assert ops.compose_state(json.dumps([{"State": "running"}])) == "active"
    assert ops.compose_state("not json") is None


async def test_restart_start_stop(with_compose, monkeypatch):
    calls = []

    async def run(job, cmd, **kwargs):
        calls.append(cmd[4 + 2:])
        return 0

    monkeypatch.setattr(ops, "run", run)
    instance = _server().test
    await ops.restart(job(), instance)
    assert calls == [["up", "-d"], ["restart"]]
    calls.clear()
    await ops.start(job(), instance)
    await ops.stop(job(), instance)
    assert calls == [["up", "-d"], ["stop"]]


async def test_service_state(with_compose, monkeypatch):
    answer = (0, _ps("running", "exited"))

    async def capture(cmd, **kwargs):
        assert cmd == ["sudo", "-n", *BASE, "ps", "-a", "--format", "json"]
        return answer

    monkeypatch.setattr(ops, "capture", capture)
    instance = _server().test
    assert await ops.service_state(instance) == "partial"
    answer = (1, "sudo: a password is required")
    assert await ops.service_state(instance) is None


async def test_an_inactive_project_stays_stopped_for_the_auto_update(with_compose, monkeypatch):
    async def capture(cmd, **kwargs):
        return 0, _ps("exited")

    monkeypatch.setattr(ops, "capture", capture)
    assert await ops._stopped(_server().test)


async def test_checks(with_compose, monkeypatch, tmp_path):
    allowed = set()

    async def capture(cmd, **kwargs):
        assert cmd[:3] == ["sudo", "-n", "-l"]
        return (0 if tuple(cmd[3:]) in allowed else 1), ""

    monkeypatch.setattr(ops, "capture", capture)
    instance = _server(service=str(tmp_path)).test
    result = {c.name: c for c in await checks._instance_checks(instance)}
    assert not result["Service"].ok and "compose.yaml" in result["Service"].detail
    restart = result["Restart permission"]
    assert not restart.ok and restart.level == checks.ERROR
    assert f"--project-directory {tmp_path} up -d" in restart.detail
    assert f"--project-directory {tmp_path} restart" in restart.detail
    start_stop = result["Start/Stop permission"]
    assert not start_stop.ok and start_stop.level == checks.WARNING
    assert f"{tmp_path} stop" in start_stop.detail and f"{tmp_path} ps -a --format json" in start_stop.detail

    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    for action in ("start", "restart", "stop", "state"):
        allowed.add(tuple(instance.restart.command(action)[2:]))
    result = {c.name: c for c in await checks._instance_checks(instance)}
    assert result["Service"].ok and "docker-compose.yml" in result["Service"].detail
    assert result["Restart permission"].ok and result["Start/Stop permission"].ok

