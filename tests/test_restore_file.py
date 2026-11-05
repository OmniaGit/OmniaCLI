# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checks and the start of an Odoo instance whose database is missing or
empty, and the restore of a zip sent by the browser."""

import io
import zipfile

import pytest

from omniacli import checks, ops
from omniacli.jobs import JobFailed
from omniacli.odoo import checks as odoo_checks

from .test_from_link import calls, here, wait  # noqa: F401
from .test_links import as_user

UPLOAD = "/api/servers/srv/test/backups/upload"
RESTORE = "/servers/srv/test/restore"


def a_zip(*members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        for member in members:
            zf.writestr(member, "-- dump")
    return data.getvalue()


def probe_answers(instance, code, out=""):
    """The instance's python answers the database probe so."""
    python = instance.options.venv_bin / "python"
    python.unlink(missing_ok=True)
    python.write_text(f"#!/bin/sh\nprintf '%s' '{out}'\nexit {code}\n")
    python.chmod(0o755)


def database_check(report):
    return next(c for c in report if c.name == "Database")


@pytest.mark.parametrize("code, word", [(3, "does not exist"), (4, "is empty")])
async def test_no_database_warns_and_refuses_the_start(env, code, word):
    instance = env["settings"].instance("srv", "test")
    probe_answers(instance, code)
    found = database_check(await odoo_checks.instance_checks(instance))
    assert not found.ok and found.level == checks.WARNING and word in found.detail
    assert "DB_test" in found.detail and "Restore" in found.detail

    class Job:
        def log(self, line):
            pass
    for action in (ops.start, ops.restart):
        with pytest.raises(JobFailed, match=word):
            await action(Job(), instance)
    assert not env["calls"].exists()


async def test_a_failed_connection_stays_an_error_and_does_not_stop_a_start(env):
    instance = env["settings"].instance("srv", "test")
    probe_answers(instance, 1, "password authentication failed")
    found = database_check(await odoo_checks.instance_checks(instance))
    assert not found.ok and found.level == checks.ERROR
    assert found.detail == "DB_test: password authentication failed"

    class Job:
        def log(self, line):
            pass
    await ops.start(Job(), instance)
    assert "start odoo-test" in env["calls"].read_text()


def test_the_error_line_is_the_exception_not_the_caret():
    out = ('Traceback (most recent call last):\n  File "<string>", line 10, in <module>\n'
           'psycopg2.errors.UndefinedTable: relation "x" does not exist\n'
           "LINE 1: SELECT latest_version FROM x\n                                   ^\n")
    assert odoo_checks._error_line(out) == 'psycopg2.errors.UndefinedTable: relation "x" does not exist'
    out = 'psycopg2.errors.UniqueViolation: duplicate key\nDETAIL:  Key (x)=(1) already exists.\n'
    assert odoo_checks._error_line(out) == "psycopg2.errors.UniqueViolation: duplicate key"
    assert odoo_checks._error_line("no answer within 60 s") == "no answer within 60 s"
    assert odoo_checks._error_line("") == ""


async def test_upload_then_restore(here, env):
    page = (await here.get("/servers/srv?view=both", headers=as_user("admin"))).text
    assert page.count("data-restore-file=") == 2 and '<dialog id="restore-dialog">' in page

    resp = await here.post(UPLOAD, params={"name": "my backup (1).zip"}, headers=as_user("admin", post=True),
                           content=a_zip("dump.sql", "filestore/"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"file": "my_backup__1_.zip"}
    folder = here.settings.backup_dir / "srv" / "test"
    assert [p.name for p in folder.iterdir()] == ["my_backup__1_.zip"]

    resp = await here.post(RESTORE, headers=as_user("admin", post=True),
                           json={"backup": "my_backup__1_.zip", "neutralize": True})
    assert resp.status_code == 200, resp.text
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["title"] == "Restore my_backup__1_.zip into srv/test"
    assert any("names no Odoo version" in line for line in job["lines"])
    done = calls(env)
    assert done[0] == "stop odoo-test"
    assert done[1] == (f"restore -c {env['tmp']}/odoo.conf --copy --force --neutralize DB_test "
                       f"{folder / 'my_backup__1_.zip'}")
    assert done[-1] == "start odoo-test"


async def test_a_zip_that_is_no_backup_touches_nothing(here, env):
    resp = await here.post(UPLOAD, params={"name": "other.zip"}, headers=as_user("admin", post=True),
                           content=a_zip("readme.txt"))
    assert resp.status_code == 200
    resp = await here.post(RESTORE, headers=as_user("admin", post=True), json={"backup": "other.zip"})
    assert resp.status_code == 400 and "no dump.sql" in resp.json()["error"]
    assert calls(env) == []  # not stopped, nothing dropped


async def test_what_the_upload_refuses(here):
    admin = as_user("admin", post=True)
    folder = here.settings.backup_dir / "srv" / "test"
    resp = await here.post(UPLOAD, params={"name": "x.tar"}, headers=admin, content=b"x")
    assert resp.status_code == 400 and ".zip" in resp.json()["error"]
    resp = await here.post(UPLOAD, params={"name": "notzip.zip"}, headers=admin, content=b"not a zip")
    assert resp.status_code == 400 and "not a zip" in resp.json()["error"]
    assert not folder.exists() or not list(folder.iterdir())  # no part left behind
    # the folder of another instance cannot be named
    resp = await here.post(UPLOAD, params={"name": "../production/x.zip"}, headers=admin, content=a_zip("dump.sql"))
    assert resp.json() == {"file": "x.zip"} and (folder / "x.zip").is_file()
    # a backup is not overwritten
    resp = await here.post(UPLOAD, params={"name": "x.zip"}, headers=admin, content=a_zip("dump.sql"))
    assert resp.status_code == 409 and "already" in resp.json()["error"]


async def test_who_may_restore(here, env):
    for user in ("dev", "viewer"):
        headers = as_user(user, post=True)
        assert (await here.post(UPLOAD, params={"name": "x.zip"}, headers=headers, content=a_zip("dump.sql"))).status_code == 403
        assert (await here.post(RESTORE, headers=headers, json={"backup": "x.zip"})).status_code == 403
        assert "data-restore-file=" not in (await here.get("/servers/srv?view=both", headers=as_user(user))).text
    admin = as_user("admin", post=True)
    resp = await here.post(RESTORE, headers=admin, json={"backup": "../x.zip"})
    assert resp.status_code == 400 and "not the name of a backup zip" in resp.json()["error"]
    resp = await here.post(RESTORE, headers=admin, json={"backup": "absent.zip"})
    assert resp.status_code == 404
    # a production: its database typed
    await here.post("/api/servers/srv/production/backups/upload", params={"name": "p.zip"}, headers=admin,
                    content=a_zip("dump.sql"))
    resp = await here.post("/servers/srv/production/restore", headers=admin, json={"backup": "p.zip"})
    assert resp.status_code == 400 and "Type DB_production" in resp.json()["error"]
    assert calls(env) == []
