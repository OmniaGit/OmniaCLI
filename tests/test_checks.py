# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import sys

from omniacli import checks


def by_name(report, target, name):
    return next(c for c in report if c.target == target and c.name == name)


async def test_real_checks_on_the_test_env(env):
    """The checks against the real git repositories of the fixture, a venv
    whose python has no click_odoo, no psycopg2, and no such systemd unit."""
    settings = env["settings"]
    server = settings.servers["srv"]
    for inst in server.instances.values():
        (inst.options.venv_bin / "python").unlink(missing_ok=True)
        (inst.options.venv_bin / "python").symlink_to(sys.executable)
        inst.options.odoo_config.write_text("[options]\ndb_name = x\n")
    report = await checks.check_server(server, settings.backup_dir)

    assert by_name(report, "server", "Separate working copies").ok
    assert by_name(report, "server", "Separate databases").ok
    assert by_name(report, "server", "Backup folder").ok
    assert by_name(report, "test", "Odoo config").ok
    assert by_name(report, "test", "Log file").ok
    assert by_name(report, "test", "click-odoo-update").ok
    assert not by_name(report, "test", "Odoo importable by click-odoo").ok
    assert not by_name(report, "test", "Database").ok
    repo = by_name(report, "test", "Repository odoo")
    assert repo.ok, repo.detail
    service = by_name(report, "test", "Service")
    assert not service.ok and "odoo-test" in service.detail
    permission = by_name(report, "test", "Restart permission")
    assert permission.level == checks.WARNING  # sudo: false in the fixture
    assert not checks.passed(report)


async def test_run_as_checks(env, fake_sudo):
    import dataclasses
    import getpass

    settings = env["settings"]
    test = settings.instance("srv", "test")
    (test.options.venv_bin / "python").unlink(missing_ok=True)
    (test.options.venv_bin / "python").symlink_to(sys.executable)
    test.options.odoo_config.write_text("[options]\ndb_name = x\n")
    report = await checks._instance_checks(dataclasses.replace(test, run_as=getpass.getuser()))
    assert by_name(report, "test", "Run as").ok
    sudo = fake_sudo.read_text()
    assert f"sudo -n -l -u {getpass.getuser()} {test.options.venv_bin}/python" in sudo
    # the probes run as that user too
    assert f"sudo -n -u {getpass.getuser()} {test.options.venv_bin}/python -c import click_odoo" in sudo
    report = await checks._instance_checks(dataclasses.replace(test, run_as="no-such-user-x"))
    assert by_name(report, "test", "Run as").detail == "no system user no-such-user-x"


async def test_repository_problems_are_reported(env):
    from .conftest import sh

    settings = env["settings"]
    production = settings.servers["srv"].production
    path = production.repository("odoo").path
    (path / "base.txt").write_text("dirty\n")
    sh("git", "remote", "set-url", "origin", str(env["tmp"] / "nowhere.git"), cwd=path)
    report = await checks.check_server(settings.servers["srv"], settings.backup_dir)
    repo = by_name(report, "production", "Repository odoo")
    assert not repo.ok and "uncommitted" in repo.detail and "not reachable" in repo.detail


async def test_local_changes_on_a_test_instance_without_auto_update(env):
    settings = env["settings"]
    test = settings.servers["srv"].test
    (test.repository("odoo").path / "base.txt").write_text("dirty\n")
    report = await checks.check_server(settings.servers["srv"], settings.backup_dir)
    assert by_name(report, "test", "Repository odoo").ok
    kept = by_name(report, "test", "Repository odoo: local changes")
    assert not kept.ok and kept.level == checks.WARNING and "1 uncommitted change(s), kept" in kept.detail
    # with auto update on, nothing may pull over someone's work: an error again
    test.auto_update.enabled = True
    report = await checks.check_server(settings.servers["srv"], settings.backup_dir)
    repo = by_name(report, "test", "Repository odoo")
    assert not repo.ok and "uncommitted" in repo.detail and repo.level == checks.ERROR


def test_passed_ignores_warnings():
    assert checks.passed([checks.Check("x", "a", False, "", checks.WARNING)])
    assert not checks.passed([checks.Check("x", "a", False, "")])


async def test_process_type_checks(tmp_path):
    from omniacli import checks

    from .test_ops import process_instance

    instance = process_instance(tmp_path)
    result = {c.name: c for c in await checks._instance_checks(instance)}
    assert result["Log folder"].ok and result["odoo-bin"].ok
    assert not result["Odoo process"].ok and result["Odoo process"].level == checks.WARNING
    assert "Service" not in result and "Restart permission" not in result and "Log file" not in result
    instance.options.odoo_bin.unlink()
    result = {c.name: c for c in await checks._instance_checks(instance)}
    assert not result["odoo-bin"].ok and result["odoo-bin"].level == checks.ERROR
