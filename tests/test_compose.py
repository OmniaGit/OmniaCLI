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


class LoggedDummy(ComposeDummy):
    name = "logged"

    def log_service(self, instance):
        return "odoo"


@pytest.fixture
def with_logged():
    plugins.set_registry(plugins.load(entry_points=[FakeEntryPoint("logged", LoggedDummy)]))
    yield plugins.registry().get("logged")
    plugins.set_registry(None)


def test_a_compose_instance_has_no_log_file(with_logged):
    srv = _server("logged")
    assert srv.test.log_file is None and srv.test.log_label == "docker compose logs odoo"
    assert "log_file" not in server_to_dict(srv)["test"]


T1 = "2026-10-28T16:48:49.379628995Z"
T2 = "2026-10-28T16:48:50.380582551Z"
P2 = ops._micros("2026-10-28T16:48:50", "380582551")


async def test_compose_log(with_logged, monkeypatch):
    answers, asked = [], []

    async def capture(cmd, **kwargs):
        asked.append(cmd)
        return answers.pop(0)

    monkeypatch.setattr(ops, "capture", capture)
    instance = _server("logged").test
    answers.append((0, f'level=warning msg="obsolete"\n{T1} line 4\n{T2} 2026-10-28 16:48:50,380 INFO db: line 5'))
    first = await ops.compose_log(instance)
    assert asked[-1] == ["sudo", "-n", *HELPER, "logs", "odoo", "--tail", "300"]
    assert first["lines"] == ["line 4", "2026-10-28 16:48:50,380 INFO db: line 5"]
    assert first["pos"] == P2 == 1793206130380582 and first["inode"] == 0
    # --since gives again the line at that time
    answers.append((0, f"{T2} line 5\n2026-10-28T16:48:51.000001Z line 6"))
    nxt = await ops.compose_log(instance, first["pos"])
    assert asked[-1][-2:] == ["--since", "2026-10-28T16:48:50.380582Z"]
    assert nxt["lines"] == ["line 6"] and nxt["pos"] == P2 + 619419
    answers.append((0, ""))
    assert (await ops.compose_log(instance, nxt["pos"]))["pos"] == nxt["pos"]
    # nothing written yet: what comes from now on
    answers.append((0, ""))
    assert (await ops.compose_log(instance))["pos"] > P2
    answers.append((2, "compose-helper: /srv/odooplm-19 is not safe from user omniacli:\n  x is writable"))
    with pytest.raises(ops.LogUnavailable, match="x is writable"):
        await ops.compose_log(instance, P2)


async def test_no_log_service_no_log(with_compose):
    instance = _server().test
    assert instance.log_label == "no log"
    with pytest.raises(ops.LogUnavailable, match="names no service"):
        await ops.compose_log(instance)


def test_the_logs_action(tmp_path, monkeypatch):
    project = tmp_path
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    assert composehelper.commands(project, "logs", ["odoo", "--tail", "5"]) == [
        ["logs", "--no-color", "--no-log-prefix", "--timestamps", "--tail", "5", "odoo"]]
    assert composehelper.commands(project, "logs", ["db", "--since", T2])[0][-3:] == ["--since", T2, "db"]
    for args, message in ((["odoo", "--tail", "0"], "1 to 10000"), (["odoo", "--tail", "x"], "1 to 10000"),
                          (["odoo", "--since", "yesterday"], "UTC time"), (["web", "--tail", "5"], "no service"),
                          (["odoo", "--follow", "5"], "usage"), (["odoo"], "usage")):
        with pytest.raises(composehelper.HelperError, match=message):
            composehelper.commands(project, "logs", args)
    with pytest.raises(composehelper.HelperError, match="takes no arguments"):
        composehelper.commands(project, "stop", ["--timeout", "0"])


def test_the_odoo_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    monkeypatch.setattr(composehelper, "TOOLS", tmp_path / "tools")

    def one(action, *args):
        found = composehelper.commands(tmp_path, action, list(args))
        assert len(found) == 1
        return found[0]

    assert composehelper.commands(tmp_path, "pull", []) == [["pull"], ["up", "-d"]]
    assert one("version", "odoo")[:5] == ["exec", "-T", "odoo", "sh", "-c"]
    assert one("port", "odoo", "8069") == ["port", "odoo", "8069"]
    # the database reaches the shell as $1, never in the script's text
    dump = one("dump", "odoo", "DB_prod")
    assert dump[-2:] == ["sh", "DB_prod"] and "DB_prod" not in dump[5]
    assert "--no-filestore" not in dump[5] and "--no-filestore" in one("dump", "odoo", "DB_prod", "--no-filestore")[5]
    load = one("load", "odoo", "DB_test", "--neutralize")
    assert "--neutralize" in load[5] and load[-1] == "DB_test" and "cat >" in load[5]
    with pytest.raises(composehelper.HelperError, match="holds no click-odoo-update"):
        one("update", "odoo", "DB_test")
    (tmp_path / "tools" / "bin").mkdir(parents=True)
    (tmp_path / "tools" / "bin" / "click-odoo-update").write_text("")
    update = one("update", "odoo", "DB_test")
    assert update[:5] == ["run", "--rm", "--no-deps", "-T", "-v"]
    assert update[5] == f"{tmp_path / 'tools'}:{composehelper.TOOLS_MOUNT}:ro" and update[-1] == "DB_test"
    assert one("update", "odoo", "DB_test", "--modules", "web,plm_box")[-2:] == ["DB_test", "web,plm_box"]
    # psql on the caller's terminal: no -T
    assert one("psql", "db", "DB_test")[:3] == ["exec", "db", "sh"]
    for action, args, message in (
            ("dump", ["odoo", "DB; rm -rf /"], "not a database name"),
            ("dump", ["odoo", "-DB"], "not a database name"),
            ("dump", ["odoo", "DB", "--format"], "usage"),
            ("load", ["odoo", "DB", "--force"], "usage"),
            ("update", ["odoo", "DB", "--modules", "web,Base"], "not a list of module names"),
            ("update", ["odoo", "DB", "--modules", "web;x"], "not a list of module names"),
            ("update", ["odoo", "DB", "-u", "web"], "usage"),
            ("port", ["odoo", "80a"], "usage"),
            ("version", ["web"], "no service"),
            ("psql", ["db"], "usage"),
            ("pull", ["--quiet"], "takes no arguments")):
        with pytest.raises(composehelper.HelperError, match=message):
            composehelper.commands(tmp_path, action, args)


def test_the_database_connection_is_the_entrypoints(tmp_path):
    """The PG* variables from HOST, PORT, USER, PASSWORD(_FILE), with the
    official image's defaults; one set already wins."""
    import subprocess

    script = composehelper.ODOO_DB + 'echo "$PGHOST $PGPORT $PGUSER $PGPASSWORD"'
    clean = {"PATH": "/usr/bin:/bin"}
    out = lambda env: subprocess.run(["sh", "-c", script], env={**clean, **env}, capture_output=True,  # noqa: E731
                                     text=True, check=True).stdout.split()
    assert out({}) == ["db", "5432", "odoo", "odoo"]
    assert out({"HOST": "pg", "PORT": "6543", "USER": "u", "PASSWORD": "p"}) == ["pg", "6543", "u", "p"]
    assert out({"POSTGRES_USER": "pu", "POSTGRES_PASSWORD": "pp"}) == ["db", "5432", "pu", "pp"]
    assert out({"HOST": "pg", "PGHOST": "other"})[0] == "other"
    (tmp_path / "secret").write_text("from-file")
    assert out({"PASSWORD": "p", "PASSWORD_FILE": str(tmp_path / "secret")})[3] == "from-file"
