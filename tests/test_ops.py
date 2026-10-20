import pytest

from omniaci import ops
from omniaci.jobs import Job, JobFailed

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
    with pytest.raises(JobFailed, match="uncommitted"):
        await ops.pull(job(), test)
    sh("git", "checkout", "-q", "--", "base.txt", cwd=path)
    sh("git", "checkout", "-q", "-b", "other", cwd=path)
    with pytest.raises(JobFailed, match="expected 'test'"):
        await ops.pull(job(), test)


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


async def test_run_timeout():
    j = job()
    with pytest.raises(JobFailed, match="did not finish"):
        await ops.run(j, ["sleep", "5"], timeout=0.2)
