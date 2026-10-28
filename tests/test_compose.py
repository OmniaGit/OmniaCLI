# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restart type compose: a Docker Compose project, for a type that runs in one,
driven through the root helper compose-helper."""
import json
import os
import stat

import pytest

from omniacli import checks, composehelper, ops, plugins
from omniacli.config import OMNIACLI_COMMAND, ConfigError, parse_servers, server_to_dict
from omniacli.jobs import Job

from .test_plugins import Dummy, FakeEntryPoint

PROJECT = "/srv/odooplm-19"
HELPER = [str(OMNIACLI_COMMAND), "compose-helper", PROJECT]
# a user who owns nothing here and is in no group of these files
STRANGER = (54321, [54321])


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


def test_every_command_goes_through_the_helper(with_compose):
    restart = _server().test.restart
    for action in ("restart", "start", "stop", "state"):
        assert restart.command(action) == ["sudo", "-n", *HELPER, action]
    assert _server(sudo=False).test.restart.command("stop") == [*HELPER, "stop"]


def _ps(*states):
    return "\n".join(json.dumps({"Service": f"s{i}", "State": s}) for i, s in enumerate(states))


def test_compose_state():
    assert ops.compose_state(_ps("running", "running")) == "active"
    assert ops.compose_state(_ps("running", "exited")) == "partial"
    assert ops.compose_state(_ps("exited", "created")) == "inactive"
    assert ops.compose_state("") == "inactive"
    # before Compose 2.21: one array
    assert ops.compose_state(json.dumps([{"State": "running"}])) == "active"
    # a warning of docker compose, mixed with the output
    assert ops.compose_state('time="..." level=warning msg="obsolete"\n' + _ps("running")) == "active"
    assert ops.compose_state("{not json") is None


async def test_restart_start_stop(with_compose, monkeypatch):
    calls = []

    async def run(job, cmd, **kwargs):
        calls.append(cmd[-1])
        return 0

    monkeypatch.setattr(ops, "run", run)
    instance = _server().test
    await ops.restart(job(), instance)
    await ops.start(job(), instance)
    await ops.stop(job(), instance)
    assert calls == ["restart", "start", "stop"]


async def test_service_state(with_compose, monkeypatch):
    answer = (0, _ps("running", "exited"))

    async def capture(cmd, **kwargs):
        assert cmd == ["sudo", "-n", *HELPER, "state"]
        return answer

    monkeypatch.setattr(ops, "capture", capture)
    instance = _server().test
    assert await ops.service_state(instance) == "partial"
    answer = (2, "compose-helper: /srv/odooplm-19 is not safe from user omniacli")
    assert await ops.service_state(instance) is None


async def test_an_inactive_project_stays_stopped_for_the_auto_update(with_compose, monkeypatch):
    async def capture(cmd, **kwargs):
        return 0, _ps("exited")

    monkeypatch.setattr(ops, "capture", capture)
    assert await ops._stopped(_server().test)


def _project(tmp_path):
    project = tmp_path / "stack"
    project.mkdir()
    (project / "compose.yaml").write_text("services: {}\n")
    for path in (tmp_path, project, project / "compose.yaml"):
        path.chmod(path.stat().st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
    return project


def _inside(found, tmp_path):
    """The problems of the project, not of the folders above tmp_path (the
    test machine's own)."""
    return [p for p in found if not any(p.startswith(f"{a} ") for a in tmp_path.parents)]


def test_a_project_safe_from_the_user(tmp_path):
    project = _project(tmp_path)
    assert _inside(composehelper.problems(project, *STRANGER), tmp_path) == []
    # root is not checked
    assert composehelper.problems(project, 0, [0]) == []


def test_what_makes_a_project_unsafe(tmp_path):
    project = _project(tmp_path)
    uid, groups = STRANGER

    def found():
        return "\n".join(_inside(composehelper.problems(project, uid, groups), tmp_path))

    (project / "compose.override.yaml").write_text("services: {}\n")
    (project / "compose.override.yaml").chmod(0o666)
    assert "compose.override.yaml is writable by the user" in found()
    (project / "compose.override.yaml").unlink()
    project.chmod(0o777)
    assert "may add a compose override" in found()
    project.chmod(0o755)
    tmp_path.chmod(0o777)
    assert f"{tmp_path} is writable by the user: it may replace {project}" in found()
    # sticky, as /tmp: only the project's owner may rename it
    tmp_path.chmod(0o1777)
    assert found() == ""
    tmp_path.chmod(0o755)
    # the group of the file, when the user is in it
    (project / "compose.yaml").chmod(0o664)
    assert found() == ""
    groups = [*groups, (project / "compose.yaml").stat().st_gid]
    assert "compose.yaml is writable by the user" in found()
    (project / "compose.yaml").chmod(0o644)
    # its owner may change the mode of a file
    uid = (project / "compose.yaml").stat().st_uid
    assert "compose.yaml is writable by the user" in found()
    uid = STRANGER[0]
    (project / ".env").write_text("TAG=19.0\nexport COMPOSE_FILE=/home/me/evil.yaml\n")
    (project / ".env").chmod(0o644)
    assert ".env sets COMPOSE_FILE" in found()
    (project / ".env").unlink()
    os.symlink(project / "compose.yaml", project / "docker-compose.yml")
    assert "docker-compose.yml is a symbolic link" in found()
    (project / "docker-compose.yml").unlink()
    (project / "compose.yaml").unlink()
    assert "holds no compose.yaml" in found()


def test_a_project_through_a_link_is_refused(tmp_path):
    project = _project(tmp_path)
    os.symlink(project, tmp_path / "link")
    assert "symbolic link" in composehelper.problems(tmp_path / "link", *STRANGER)[0]
    assert "not an absolute path" in composehelper.problems(type(tmp_path)("stack"), *STRANGER)[0]


def test_the_helper_refuses_before_running_anything(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    ran = []
    monkeypatch.setattr(composehelper.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(composehelper, "caller", lambda: ("omniacli", *STRANGER))
    monkeypatch.setattr(composehelper, "problems", lambda project, uid, groups: ["x is writable"])
    assert composehelper.main([str(project), "start"]) == 2
    assert "not safe from user omniacli" in capsys.readouterr().err and ran == []
    assert composehelper.main([str(project), "rm"]) == 2
    assert "unknown action" in capsys.readouterr().err


def test_the_helper_runs_the_actions(tmp_path, monkeypatch):
    project = _project(tmp_path)
    ran = []

    class Done:
        returncode = 0

    def run(cmd, env, cwd):
        ran.append(cmd[4:])
        assert cmd[1:4] == ["compose", "--project-directory", str(project)]
        assert env == composehelper.ENV
        return Done()

    monkeypatch.setattr(composehelper.subprocess, "run", run)
    monkeypatch.setattr(composehelper.shutil, "which", lambda name, path: "/usr/bin/docker")
    monkeypatch.setattr(composehelper, "caller", lambda: ("omniacli", *STRANGER))
    monkeypatch.setattr(composehelper, "problems", lambda project, uid, groups: [])
    for action in ("restart", "start", "stop", "state"):
        assert composehelper.main([str(project), action]) == 0
    assert ran == [["up", "-d"], ["restart"], ["up", "-d"], ["stop"], ["ps", "-a", "--format", "json"]]


async def test_checks(with_compose, monkeypatch, tmp_path):
    allowed = set()

    async def capture(cmd, **kwargs):
        assert cmd[:3] == ["sudo", "-n", "-l"]
        return (0 if tuple(cmd[3:]) in allowed else 1), ""

    monkeypatch.setattr(ops, "capture", capture)
    instance = _server(service=str(tmp_path)).test
    result = {c.name: c for c in await checks._instance_checks(instance)}
    assert not result["Service"].ok and "the compose-helper refuses" in result["Service"].detail
    restart = result["Restart permission"]
    assert not restart.ok and restart.level == checks.ERROR
    assert restart.detail.endswith(f"NOPASSWD: {OMNIACLI_COMMAND} compose-helper {tmp_path} *")
    assert "Start/Stop permission" not in result

    allowed.add(tuple(instance.restart.command("restart")[2:]))
    monkeypatch.setattr(checks.composehelper, "problems", lambda project, uid, groups: [])
    result = {c.name: c for c in await checks._instance_checks(instance)}
    assert result["Service"].ok and result["Restart permission"].ok
