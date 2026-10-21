import os

import pytest

from omniacli import ops
from omniacli.jobs import Job, JobFailed

from .conftest import clone, commit, sh


def job():
    return Job(id="t", title="t", instances=[], source="test")


def head(path):
    return sh("git", "rev-parse", "HEAD", cwd=path)


def remote_head(remote, branch):
    return sh("git", "rev-parse", branch, cwd=remote)


def push_change(env, name, branch, filename, content="x\n"):
    """Simulate a developer pushing a commit to the remote."""
    work = clone(env["remotes"][name], env["tmp"] / f"dev_{name}_{branch}_{filename}", branch)
    sha = commit(work, filename, content, f"change {filename}")
    sh("git", "push", "-q", "origin", f"HEAD:{branch}", cwd=work)
    return sha


async def test_pull_and_restart(env):
    test = env["settings"].instance("srv", "test")
    sha = push_change(env, "odoo", "test", "a.txt")
    assert await ops.pending_updates(test) == {"odoo": 1}
    j = job()
    await ops.pull_restart(j, test, only_if_changed=True)
    assert head(test.repository("odoo").path) == sha
    calls = env["calls"].read_text()
    assert "update -c" in calls and "-d DB_test" in calls and "restart odoo-test" in calls
    assert await ops.pending_updates(test) == {}


async def test_pull_restart_nothing_new(env):
    test = env["settings"].instance("srv", "test")
    await ops.pull_restart(job(), test, only_if_changed=True)
    assert not env["calls"].exists()


async def test_pull_refuses_wrong_branch_and_dirty(env):
    test = env["settings"].instance("srv", "test")
    path = test.repository("odoo").path
    (path / "base.txt").write_text("local edit\n")
    # a test instance with auto update, like production, refuses local changes
    test.auto_update.enabled = True
    with pytest.raises(JobFailed, match="uncommitted"):
        await ops.pull(job(), test)
    production = env["settings"].instance("srv", "production")
    (production.repository("odoo").path / "base.txt").write_text("local edit\n")
    with pytest.raises(JobFailed, match="uncommitted"):
        await ops.pull(job(), production)
    sh("git", "checkout", "-q", "--", "base.txt", cwd=path)
    sh("git", "checkout", "-q", "-b", "other", cwd=path)
    with pytest.raises(JobFailed, match="expected 'test'"):
        await ops.pull(job(), test)


async def test_pull_keeps_local_changes_on_a_test_instance(env):
    test = env["settings"].instance("srv", "test")
    path = test.repository("odoo").path
    (path / "base.txt").write_text("local edit\n")
    sha = push_change(env, "odoo", "test", "other.txt")
    j = job()
    assert await ops.pull(j, test)
    assert head(path) == sha and (path / "base.txt").read_text() == "local edit\n"
    assert any("uncommitted changes kept" in line for line in j.lines)
    # a pulled commit that changes the edited file: git refuses, the edit stays
    push_change(env, "odoo", "test", "base.txt", "from the remote\n")
    with pytest.raises(JobFailed, match="git exited with code"):
        await ops.pull(job(), test)
    assert (path / "base.txt").read_text() == "local edit\n"


async def test_merge_refuses_local_changes_on_test(env):
    settings = env["settings"]
    server = settings.servers["srv"]
    (server.test.repository("odoo").path / "base.txt").write_text("not committed\n")
    before = head(server.production.repository("odoo").path)
    with pytest.raises(JobFailed, match="the test copy .* has uncommitted changes"):
        await ops.merge_to_production(job(), server, settings.backup_dir)
    assert head(server.production.repository("odoo").path) == before


async def test_backup(env):
    prod = env["settings"].instance("srv", "production")
    dest = await ops.backup(job(), prod, env["settings"].backup_dir)
    assert dest.parent == env["settings"].backup_dir / "srv" / "production"
    assert dest.name.startswith("DB_production_") and dest.read_text() == "dump\n"


async def test_merge_to_production(env):
    settings = env["settings"]
    server = settings.servers["srv"]
    test = server.test
    push_change(env, "odoo", "test", "feature.txt")
    await ops.pull(job(), test)
    tested = head(test.repository("odoo").path)
    # a commit pushed to test after the test server pulled must NOT be merged
    untested = push_change(env, "odoo", "test", "untested.txt")

    await ops.merge_to_production(job(), server, settings.backup_dir)

    prod_odoo = server.production.repository("odoo").path
    assert remote_head(env["remotes"]["odoo"], "main") == head(prod_odoo)
    sh("git", "merge-base", "--is-ancestor", tested, "HEAD", cwd=prod_odoo)
    assert not (prod_odoo / "untested.txt").exists()
    assert untested != tested
    calls = env["calls"].read_text().splitlines()
    assert calls[0].startswith("backup ")  # backup comes before anything else
    assert calls[-1] == "restart odoo-production"


async def test_merge_conflict_rolls_back_everything(env):
    settings = env["settings"]
    server = settings.servers["srv"]
    # "odoo" merges cleanly, "addons" conflicts: odoo must be rolled back
    push_change(env, "odoo", "test", "feature.txt")
    push_change(env, "addons", "test", "base.txt", "test side\n")
    push_change(env, "addons", "main", "base.txt", "main side\n")
    await ops.pull(job(), server.test)
    prod_odoo = server.production.repository("odoo").path
    before = head(prod_odoo)
    main_before = {n: remote_head(r, "main") for n, r in env["remotes"].items()}

    j = job()
    with pytest.raises(JobFailed, match="merge conflict"):
        await ops.merge_to_production(j, server, settings.backup_dir)

    assert head(prod_odoo) == before
    assert {n: remote_head(r, "main") for n, r in env["remotes"].items()} == main_before
    prod_addons = server.production.repository("addons").path
    assert sh("git", "status", "--porcelain", cwd=prod_addons) == ""
    assert "restart" not in env["calls"].read_text()


async def test_merge_refuses_unpushed_tested_commit(env):
    settings = env["settings"]
    server = settings.servers["srv"]
    commit(server.test.repository("odoo").path, "local.txt", "x\n", "local only")
    with pytest.raises(JobFailed, match="push the test branch first"):
        await ops.merge_to_production(job(), server, settings.backup_dir)


def run_as(instance, user="odoo"):
    import dataclasses

    return dataclasses.replace(instance, run_as=user)


async def test_run_as_update(env, fake_sudo):
    test = env["settings"].instance("srv", "test")
    odoo_bin = env["tmp"] / "odoo-src" / "odoo-bin"
    odoo_bin.parent.mkdir()
    odoo_bin.write_text("")
    test = run_as(test)
    test.odoo_bin = odoo_bin
    await ops.update_modules(job(), test)
    sudo = fake_sudo.read_text()
    # PYTHONPATH reaches the command through sudo's command line
    assert f"sudo -n -u odoo PYTHONPATH={odoo_bin.parent}" in sudo
    assert f"{test.venv_bin}/click-odoo-update -c" in sudo
    assert "update -c" in env["calls"].read_text()


async def test_run_as_backup_is_staged(env, fake_sudo, monkeypatch):
    staging = env["tmp"] / "staging"
    staging.mkdir()
    monkeypatch.setattr(ops, "BACKUP_STAGING", staging)
    prod = run_as(env["settings"].instance("srv", "production"))
    dest = await ops.backup(job(), prod, env["settings"].backup_dir)
    assert dest.parent == env["settings"].backup_dir / "srv" / "production" and dest.read_text() == "dump\n"
    sudo = fake_sudo.read_text()
    assert "sudo -n -u odoo " in sudo and f"{staging}/omniacli-backup-" in sudo
    assert list(staging.iterdir()) == []  # the staging folder is removed


async def test_run_as_pip(env, fake_sudo):
    venv_bin = env["settings"].instance("srv", "test").venv_bin
    (venv_bin / "python").write_text('#!/bin/sh\necho "python $*"\n')
    (venv_bin / "python").chmod(0o755)
    j = job()
    await ops.install_cli(j, venv_bin, "odoo")
    assert f"sudo -n -u odoo {venv_bin}/python -m pip install --disable-pip-version-check click-odoo-contrib" \
        in fake_sudo.read_text()
    assert any(line.endswith(" python -m pip install --disable-pip-version-check click-odoo-contrib")
               for line in j.lines)


async def test_run_timeout_kills_what_ignores_sigterm(monkeypatch):
    monkeypatch.setattr(ops, "KILL_WAIT", 0.2)
    with pytest.raises(JobFailed, match="did not finish"):
        await ops.run(job(), ["sh", "-c", 'trap "" TERM; exec sleep 30'], timeout=0.2)


async def test_failure_names_the_program_past_sudo(fake_sudo):
    assert ops._program(["sudo", "-n", "-u", "odoo", "PYTHONPATH=/o", "/v/bin/click-odoo-update", "-c"]) \
        == "click-odoo-update"
    assert ops._program(["sudo", "-n", "systemctl", "restart", "x"]) == "systemctl"
    assert ops._program(["/v/bin/python", "-m", "pip"]) == "python"
    with pytest.raises(JobFailed, match="^false exited with code 1$"):
        await ops.run(job(), ops.as_user("odoo", ["false"]))


async def test_run_timeout():
    j = job()
    with pytest.raises(JobFailed, match="did not finish"):
        await ops.run(j, ["sleep", "5"], timeout=0.2)


FAKE_ODOO = """import os, sys, time
print("odoo up", " ".join(sys.argv[1:]), flush=True)
if os.fork() == 0:  # a worker, in the same process group
    print("worker up", flush=True)
time.sleep(120)
"""


def process_instance(tmp_path):
    import sys

    from omniacli.config import parse_servers

    venv = tmp_path / "pvenv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    odoo = tmp_path / "podoo"
    odoo.mkdir()
    (odoo / "odoo-bin").write_text(FAKE_ODOO)
    conf = tmp_path / "process.conf"
    conf.write_text("[options]\n")
    data = {"s": {"test": {
        "database": "DB", "odoo_config": str(conf), "venv_bin": str(venv),
        "odoo_bin": str(odoo / "odoo-bin"), "restart": {"type": "process"},
        "repositories": [{"name": "odoo", "path": str(odoo), "branch": "x"}],
    }}}
    return parse_servers(data, tmp_path / "logs")["s"].test


def group_alive(instance, pid):
    return [p for p in ops.foreign_pids(instance, None) if ops._pgid(p) == pid and ops._alive(p)]


@pytest.fixture(autouse=True)
def quick_start(monkeypatch):
    monkeypatch.setattr(ops, "START_WAIT", 1)


async def test_process_start_restart_stop(tmp_path):
    import asyncio
    import subprocess
    import sys

    instance = process_instance(tmp_path)
    assert instance.log_file == tmp_path / "logs" / "s_test.log"
    assert ops.managed_pid(instance) is None
    j = job()
    await ops.restart(j, instance)
    first = ops.managed_pid(instance)
    assert first and len(group_alive(instance, first)) == 2  # Odoo and its worker
    await ops.restart(job(), instance)
    second = ops.managed_pid(instance)
    assert second and second != first and not group_alive(instance, first)
    log = instance.log_file.read_text()
    assert log.count("started by OmniaCLI") == 2 and log.count("worker up") == 2
    assert f"-c {instance.odoo_config}" in log

    # found again from the pid file, as after a restart of OmniaCLI
    ops._children.clear()
    assert ops.managed_pid(instance) == second

    # an Odoo with the same config that OmniaCLI did not start is never touched
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                              "-c", str(instance.odoo_config)])
    try:
        await asyncio.sleep(0.2)
        with pytest.raises(JobFailed, match=f"did not start it \\(pid {other.pid}\\)"):
            await ops.restart(job(), instance)
        assert other.poll() is None and ops.managed_pid(instance) == second
    finally:
        other.kill()
        other.wait()

    await ops.stop_process(job(), instance, second)
    assert ops.managed_pid(instance) is None and not group_alive(instance, second)
    assert not instance.pid_file.exists()


async def test_process_exits_at_once(tmp_path):
    instance = process_instance(tmp_path)
    instance.odoo_bin.write_text("import sys; print('bad config'); sys.exit(3)\n")
    with pytest.raises(JobFailed, match="exited with code 3"):
        await ops.restart(job(), instance)
    assert "bad config" in instance.log_file.read_text() and not instance.pid_file.exists()


def test_process_type_config(tmp_path):
    from omniacli.config import ConfigError, parse_servers, server_to_dict

    instance = process_instance(tmp_path)
    assert instance.restart.service == ""
    data = server_to_dict(parse_servers({"s": {"test": {
        "database": "DB", "odoo_config": "/c", "venv_bin": "/v", "restart": {"type": "process"},
        "repositories": [{"name": "odoo", "path": "/o", "branch": "x"}]}}}, tmp_path)["s"])
    assert "log_file" not in data["test"] and data["test"]["restart"] == {"type": "process"}
    with pytest.raises(ConfigError, match="logs folder"):
        parse_servers({"s": data})


def test_odoo_env(tmp_path):
    instance = process_instance(tmp_path)
    assert ops.odoo_env(instance)["PYTHONPATH"].split(os.pathsep)[0] == str(instance.odoo_bin.parent)
    instance.odoo_bin.unlink()
    assert ops.odoo_env(instance) is ops._ENV


async def test_process_stop(tmp_path, monkeypatch):
    instance = process_instance(tmp_path)
    j = job()
    await ops.stop(j, instance)  # not running: nothing to do
    assert "not running" in j.lines[-1]
    await ops.restart(job(), instance)
    pid = ops.managed_pid(instance)
    await ops.stop(job(), instance)
    assert ops.managed_pid(instance) is None and not group_alive(instance, pid)

    # the auto update leaves a stopped Odoo stopped, a manual pull + restart starts it
    async def pulled(job, instance):
        return True

    async def no_update(job, instance, force=False):
        pass
    monkeypatch.setattr(ops, "pull", pulled)
    monkeypatch.setattr(ops, "update_modules", no_update)
    j = job()
    await ops.pull_restart(j, instance, only_if_changed=True)
    assert "not started again" in j.lines[-1] and ops.managed_pid(instance) is None
    await ops.pull_restart(job(), instance)
    pid = ops.managed_pid(instance)
    assert pid
    await ops.stop(job(), instance)


async def test_stop_only_process(env):
    with pytest.raises(JobFailed, match="restart type process"):
        await ops.stop(job(), env["settings"].instance("srv", "test"))
