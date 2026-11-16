# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo in Docker Compose plugin: every action through the compose helper."""
import json
import zipfile

import pytest

from omniacli import docker as plugin_module
from omniacli.config import OMNIACLI_COMMAND, ConfigError, parse_servers, server_to_dict
from omniacli.jobs import Job, JobFailed
from omniacli.plugin_api import ActionRefused

PROJECT = "/srv/odooplm-19"
HELPER = ["sudo", "-n", str(OMNIACLI_COMMAND), "compose-helper", PROJECT]


def job():
    return Job(id="t", title="t", instances=[], source="test")


def _server(**options):
    data = {"type": "docker", "test": {"restart": {"type": "compose", "service": PROJECT}, "repositories": [],
                                       "database": "DB_test", **options}}
    return parse_servers({"s": data})["s"]


def test_the_settings():
    srv = _server(http_port=19169)
    opts = srv.test.options
    assert (opts.database, opts.odoo_service, opts.db_service, opts.http_port, opts.update_modules) == (
        "DB_test", "odoo", "db", 19169, True)
    assert server_to_dict(srv)["test"] == {
        "database": "DB_test", "odoo_service": "odoo", "db_service": "db", "update_modules": True,
        "http_port": 19169, "restart": {"type": "compose", "service": PROJECT, "sudo": True},
        "auto_update": {"enabled": False, "interval_minutes": 10}, "repositories": []}
    inst = srv.test
    assert inst.log_label == "docker compose logs odoo" and inst.web_url == "http://localhost:19169"
    assert inst.plugin.describe(inst) == {"database": "DB_test", "project": PROJECT}
    command, env = inst.plugin.db_shell(inst)
    assert command == " ".join([*HELPER, "psql", "db", "DB_test"]) and env == {}
    for bad, message in (({"database": "a b"}, "not a database name"), ({"odoo_service": "x;y"}, "service name"),
                         ({"http_port": 70000}, "not a port")):
        with pytest.raises(ConfigError, match=message):
            _server(**bad)
    data = {"type": "docker", "test": {"restart": {"type": "systemd", "service": "x"}, "log_file": "/l",
                                       "repositories": [], "database": "D"}}
    with pytest.raises(ConfigError, match="restart type compose"):
        parse_servers({"s": data})


@pytest.fixture
def helper(monkeypatch):
    """The helper's commands, answered without running anything: answers
    maps an action to (exit code, output)."""
    calls, answers = [], {}

    async def capture(cmd, **kwargs):
        calls.append(cmd[len(HELPER):])
        return answers.get(cmd[len(HELPER)], (0, ""))

    async def run(job, cmd, **kwargs):
        calls.append(cmd[len(HELPER):])
        code, out = answers.get(cmd[len(HELPER)], (0, ""))
        if code:
            raise JobFailed(out)
        return 0

    class Proc:
        def __init__(self, cmd, stdin, stdout):
            self.cmd, self.returncode = cmd, 0
            self.stdout = self.stderr = _Lines([b"log line\n"])
            if stdout is not None and hasattr(stdout, "write"):
                stdout.write(answers.get("dump-zip", b""))

        async def wait(self):
            return 0

    async def create_subprocess_exec(*cmd, stdin, stdout, stderr, start_new_session):
        calls.append(list(cmd[len(HELPER):]))
        return Proc(cmd, stdin, stdout)

    monkeypatch.setattr(plugin_module, "capture", capture)
    monkeypatch.setattr(plugin_module, "run", run)
    # stop, start and restart: the core's, which run the helper's command too
    from omniacli import ops

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(plugin_module.asyncio, "create_subprocess_exec", create_subprocess_exec)
    return calls, answers


class _Lines:
    def __init__(self, lines):
        self.lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.lines:
            raise StopAsyncIteration
        return self.lines.pop(0)


def _zip(path, major="20.0"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"major_version": major}))
        zf.writestr("dump.sql", "")
    return path


async def test_checks(helper, monkeypatch, tmp_path):
    calls, answers = helper
    monkeypatch.setattr(plugin_module, "COMPOSE_TOOLS", tmp_path)
    inst = _server(http_port=19169).test
    answers["version"] = (0, "Odoo Server 20.0-20260926")
    answers["port"] = (0, "127.0.0.1:19169")
    answers["state"] = (0, 'warning\n{"Service":"odoo","State":"running"}\n{"Service":"db","State":"running"}')
    result = {c.name: c for c in await inst.plugin.checks(inst)}
    assert result["Odoo"].ok and result["HTTP port"].ok and result["Services"].ok
    assert not result["Tools"].ok and result["Tools"].level == "error"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "click-odoo-update").write_text("")
    answers["version"] = (0, "Odoo Server 17.0-20240101")
    answers["port"] = (0, "0.0.0.0:8069")
    answers["state"] = (0, '{"Service":"odoo","State":"running"}')
    result = {c.name: c for c in await inst.plugin.checks(inst)}
    assert not result["Odoo"].ok and "needs Odoo 18" in result["Odoo"].detail
    assert not result["HTTP port"].ok and not result["Services"].ok and "db" in result["Services"].detail
    assert result["Tools"].ok


async def test_after_pull_updates_the_changed_modules(helper):
    calls, answers = helper
    inst = _server().test
    await inst.plugin.after_pull(job(), inst)
    assert calls == [["update", "odoo", "DB_test"]]
    inst.options.update_modules = False
    j = job()
    await inst.plugin.after_pull(j, inst)
    assert len(calls) == 1 and "disabled" in j.lines[-1]


async def test_backup(helper, tmp_path):
    calls, answers = helper
    inst = _server().test
    _zip(tmp_path / "model.zip")
    answers["dump-zip"] = (tmp_path / "model.zip").read_bytes()
    dest = await inst.plugin.backup(job(), inst, tmp_path)
    assert calls[-1] == ["dump", "odoo", "DB_test"] and dest.name.startswith("DB_test_")
    assert zipfile.is_zipfile(dest) and not list(tmp_path.glob("*.part"))
    answers["version"] = (0, "Odoo Server 19.0-20260101")
    dest = await inst.plugin.backup(job(), inst, tmp_path, {"no_filestore": True})
    assert calls[-1] == ["dump", "odoo", "DB_test", "--no-filestore"] and dest.name.endswith("_nofilestore.zip")
    answers["version"] = (0, "Odoo Server 18.0-20260101")
    with pytest.raises(JobFailed, match="Odoo 18 cannot dump a database without its filestore"):
        await inst.plugin.backup(job(), inst, tmp_path, {"no_filestore": True})
    answers["dump-zip"] = b"not a zip"
    with pytest.raises(JobFailed, match="not a zip"):
        await inst.plugin.backup(job(), inst, tmp_path)
    assert not list(tmp_path.glob("*.part"))


async def test_restore(helper, tmp_path):
    calls, answers = helper
    inst = _server().test
    answers["version"] = (0, "Odoo Server 20.0-20260926")
    backup = _zip(tmp_path / "DB_prod_1.zip")
    await inst.plugin.restore(job(), inst, backup, {"neutralize": True})
    assert calls == [["version", "odoo"], ["stop"], ["load", "odoo", "DB_test", "--neutralize"],
                     ["update", "odoo", "DB_test"], ["start"]]
    calls.clear()
    await inst.plugin.restore(job(), inst, backup, {"neutralize": False, "load_only": True})
    assert calls == [["version", "odoo"], ["load", "odoo", "DB_test"]]
    calls.clear()
    with pytest.raises(JobFailed, match="Odoo 19, s/test runs Odoo 20"):
        await inst.plugin.restore(job(), inst, _zip(tmp_path / "old.zip", "19.0"), {})
    assert calls == [["version", "odoo"]]


async def test_the_actions(helper):
    calls, answers = helper
    inst = _server().test
    copy, update, pull = inst.plugin.actions()
    params = update.prepare(inst, {"modules": "web, plm_box", "restart": "on"})
    assert params == {"modules": ["web", "plm_box"], "restart": True}
    assert update.title_of(inst, params) == "Update web, plm_box on s/test + restart"
    await update.run(job(), inst, params)
    assert calls == [["update", "odoo", "DB_test", "--modules", "web,plm_box"], ["restart"]]
    calls.clear()
    await update.run(job(), inst, update.prepare(inst, {"modules": ""}))
    assert calls == [["update", "odoo", "DB_test"]]
    with pytest.raises(ActionRefused, match="Invalid modules: Web"):
        update.prepare(inst, {"modules": "Web"})
    calls.clear()
    await pull.run(job(), inst, {})
    assert calls == [["pull"]]


def _two_roles():
    data = {"type": "docker", **{role: {"restart": {"type": "compose", "service": f"/srv/x/{role}"},
                                        "repositories": [], "database": "main"}
                                 for role in ("production", "test")}}
    return parse_servers({"s": data})["s"]


@pytest.fixture
def copy_steps(monkeypatch, tmp_path):
    """The steps of the copy, recorded: (project, action, arguments)."""
    steps, versions = [], {"/srv/x/production": 20, "/srv/x/test": 20}

    async def helper(job, instance, action, *args, stdin=None, **kw):
        steps.append((instance.restart.service, action, *args))

    async def major(instance):
        return versions[instance.restart.service]

    async def backup(self, job, instance, dest_dir, options=None):
        steps.append((instance.restart.service, "backup", bool((options or {}).get("no_filestore"))))
        return _zip(dest_dir / "main_x.zip")

    async def stop(job, instance):
        steps.append((instance.restart.service, "stop"))

    async def start(job, instance):
        steps.append((instance.restart.service, "start"))
    monkeypatch.setattr(plugin_module, "_helper", helper)
    monkeypatch.setattr(plugin_module, "instance_major", major)
    monkeypatch.setattr(plugin_module.DockerPlugin, "backup", backup)
    monkeypatch.setattr(plugin_module, "stop", stop)
    monkeypatch.setattr(plugin_module, "start", start)

    class Host:
        def backup_folder(self, instance):
            return tmp_path / "backups" / instance.key.replace("/", "_")
    return steps, versions, Host()


async def test_copy_production_to_test(copy_steps, monkeypatch):
    steps, versions, host = copy_steps
    srv = _two_roles()
    plugin = srv.test.plugin
    # the plugin of the registry: the app of another test may have given it its host
    monkeypatch.setattr(plugin, "_host", None)
    copy = plugin.actions()[0]
    assert copy.id == "copy-production" and copy.scope == "server" and copy.applies_to(srv)
    with pytest.raises(JobFailed, match="no host"):
        await copy.run(job(), srv, {"filestore": "share"})
    assert plugin.router(host) is None
    params = copy.prepare(srv, {})
    assert params == {"filestore": "share"} and copy.title_of(srv, params) == "Copy production to test s (filestore: share)"
    await copy.run(job(), srv, params)
    p, t = "/srv/x/production", "/srv/x/test"
    assert steps == [(p, "backup", True), (t, "stop"), (t, "load", "odoo", "main", "--neutralize"),
                     (t, "link-filestore", "odoo", "main", p, "main"), (t, "update", "odoo", "main"), (t, "start")]
    steps.clear()
    await copy.run(job(), srv, copy.prepare(srv, {"filestore": "copy"}))
    assert steps[0] == (p, "backup", False) and not [s for s in steps if s[1] == "link-filestore"]
    steps.clear()
    await copy.run(job(), srv, copy.prepare(srv, {"filestore": "none"}))
    assert steps[0] == (p, "backup", True) and not [s for s in steps if s[1] == "link-filestore"]
    # Odoo 18 dumps no database without its filestore: only a copy
    versions.update({p: 18, t: 18})
    steps.clear()
    with pytest.raises(JobFailed, match="Odoo 18 cannot back up"):
        await copy.run(job(), srv, {"filestore": "share"})
    assert steps == []
    versions[t] = 20
    with pytest.raises(JobFailed, match="production runs Odoo 18, test Odoo 20"):
        await copy.run(job(), srv, {"filestore": "copy"})
    with pytest.raises(ActionRefused, match="one of share, copy, none"):
        copy.prepare(srv, {"filestore": "all"})


async def test_copy_leaves_test_stopped_when_a_step_fails(copy_steps, monkeypatch):
    steps, versions, host = copy_steps
    srv = _two_roles()
    copy = srv.test.plugin.actions()[0]
    monkeypatch.setattr(srv.test.plugin, "_host", host)

    async def helper(job, instance, action, *args, **kw):
        steps.append((instance.restart.service, action))
        if action == "link-filestore":
            raise JobFailed("not the same filesystem")
    monkeypatch.setattr(plugin_module, "_helper", helper)
    j = job()
    with pytest.raises(JobFailed, match="same filesystem"):
        await copy.run(j, srv, {"filestore": "share"})
    assert steps[-1] == ("/srv/x/test", "link-filestore") and "left stopped" in j.lines[-1]
