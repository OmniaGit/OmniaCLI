# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restore of a plain SQL dump (pg_dump --format=plain) sent by the
browser: the upload, the list, the restore job of the Odoo plugin, and
load_sql.py's own steps (its head, the \\restrict lines, psql's errors)."""

import io
import sys
from types import SimpleNamespace

import pytest

from omniacli import sqldump
from omniacli.odoo import load_sql

from .test_from_link import calls, here, wait  # noqa: F401
from .test_links import as_user

UPLOAD = "/api/servers/srv/test/backups/upload"
RESTORE = "/servers/srv/test/restore"
BACKUPS = "/api/servers/srv/test/backups"
KEY = b"mDTKMggqhCxskXcLNUhrQVYMcc8O5IXbjoSD"
DUMP = (b"--\n-- PostgreSQL database dump\n--\n\n\\restrict " + KEY + b"\n\n"
        b"-- Dumped from database version 16.15\n\nSET statement_timeout = 0;\n"
        b"CREATE TABLE t (x integer);\n\\unrestrict " + KEY + b"\n\n")


def fake_python(env, errors=0):
    """The venv's python: load_sql.py records its arguments and its standard
    input, prints the count of SQL errors; anything else (the database
    probe of a start) fails as a connection that cannot be made."""
    venv = env["settings"].instance("srv", "test").options.venv_bin
    python = venv / "python"
    python.write_text(
        "#!/bin/sh\n"
        'case "$1" in *load_sql.py) ;; *) echo "connection refused"; exit 1 ;; esac\n'
        f'shift; echo "load_sql.py $*" >> {env["tmp"] / "calls.log"}\n'
        f'cat > {env["tmp"] / "loaded.sql"}\n'
        f'echo "OMNIACLI_SQL_ERRORS={errors}"\n')
    python.chmod(0o755)


def odoo_18(env):
    """An odoo-bin of Odoo 18 in the test instance's first repository."""
    folder = env["settings"].instance("srv", "test").repositories[0].path
    (folder / "odoo-bin").write_text("#!/bin/sh\n")
    (folder / "odoo").mkdir()
    (folder / "odoo" / "release.py").write_text("version_info = (18, 0, 0, 'final', 0, '')\n")


async def test_upload_then_restore_a_sql_dump(here, env):
    fake_python(env, errors=2)
    odoo_18(env)
    admin = as_user("admin", post=True)
    page = (await here.get("/servers/srv?view=both", headers=as_user("admin"))).text
    assert "\"sql\": true" in page and "backups?dumps=1" in page

    resp = await here.post(UPLOAD, params={"name": "17.11.2026.bck"}, headers=admin, content=DUMP)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"file": "17.11.2026.bck.sql"}
    folder = here.settings.backup_dir / "srv" / "test"
    assert (folder / "17.11.2026.bck.sql").read_bytes() == DUMP
    assert (folder / "17.11.2026.bck.sql").stat().st_mode & 0o777 == 0o600
    # the restore dialog lists it, a link's copy (zips only) does not
    listed = (await here.get(BACKUPS, params={"dumps": 1}, headers=as_user("admin"))).json()
    assert [f["file"] for f in listed] == ["17.11.2026.bck.sql"]
    assert (await here.get(BACKUPS, headers=as_user("admin"))).json() == []
    download = await here.get(listed[0]["download"], headers=as_user("admin"))
    assert download.status_code == 200 and download.content == DUMP

    resp = await here.post(RESTORE, headers=admin, json={"backup": "17.11.2026.bck.sql", "neutralize": True,
                                                         "reset_passwords": True})
    assert resp.status_code == 200, resp.text
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    done = calls(env)
    assert done[0] == "stop odoo-test"
    assert done[1] == f"load_sql.py {env['tmp']}/odoo.conf DB_test 1  {len(DUMP)}"
    assert not any(line.startswith("restore ") for line in done)  # no click-odoo-restoredb
    assert done[2] == f"click-odoo -c {env['tmp']}/odoo.conf -d DB_test reset_passwords.py 18"
    assert done[-1] == "start odoo-test"
    # the dump reached the script whole, on its standard input
    assert (env["tmp"] / "loaded.sql").read_bytes() == DUMP
    lines = "\n".join(job["lines"])
    assert "is a SQL dump: it names no Odoo version" in lines
    assert "the filestore is not touched" in lines
    assert "psql went past 2 SQL error(s)" in lines


async def test_what_the_sql_upload_refuses(here, env):
    admin = as_user("admin", post=True)
    folder = here.settings.backup_dir / "srv" / "test"
    for content, said in [(b"PGDMP\x01\x0e\x00", "custom format"),
                          (b"--\n-- PostgreSQL database cluster dump\n--\n", "not a zip nor a plain SQL dump"),
                          (b"SELECT 1;\n-- PostgreSQL database dump\n", "not a zip nor a plain SQL dump"),
                          (b"", "not a zip nor a plain SQL dump")]:
        resp = await here.post(UPLOAD, params={"name": "x.bck"}, headers=admin, content=content)
        assert resp.status_code == 400 and said in resp.json()["error"], (content, resp.text)
        assert resp.json()["error"].startswith("x.bck ")
    assert not folder.exists() or not list(folder.iterdir())  # nothing kept, no part left
    # a dump is not overwritten
    assert (await here.post(UPLOAD, params={"name": "d.sql"}, headers=admin, content=DUMP)).status_code == 200
    resp = await here.post(UPLOAD, params={"name": "d.sql"}, headers=admin, content=DUMP)
    assert resp.status_code == 409
    # a file of the backups that is no dump, though named .sql: refused before anything is touched
    (folder / "bad.sql").write_bytes(b"SELECT 1;\n")
    resp = await here.post(RESTORE, headers=admin, json={"backup": "bad.sql"})
    assert resp.status_code == 400 and "not a zip nor a plain SQL dump" in resp.json()["error"]
    assert calls(env) == []


async def test_a_plugin_without_restores_sql_takes_zips_only(here, env, monkeypatch):
    instance = here.settings.instance("srv", "test")
    monkeypatch.setattr(type(instance.plugin), "restores_sql", False)
    admin = as_user("admin", post=True)
    resp = await here.post(UPLOAD, params={"name": "17.11.2026.bck"}, headers=admin, content=DUMP)
    assert resp.status_code == 400 and resp.json()["error"] == "The file must be a .zip"
    folder = here.settings.backup_dir / "srv" / "test"
    folder.mkdir(parents=True)
    (folder / "d.sql").write_bytes(DUMP)
    resp = await here.post(RESTORE, headers=admin, json={"backup": "d.sql"})
    assert resp.status_code == 400 and "does not restore a SQL dump" in resp.json()["error"]
    assert calls(env) == []


def test_the_name_a_dump_is_kept_under():
    assert sqldump.stored_name("17.11.2026.bck") == "17.11.2026.bck.sql"
    assert sqldump.stored_name("db.sql") == "db.sql"
    assert sqldump.RESTORE_NAME.fullmatch("a.zip") and sqldump.RESTORE_NAME.fullmatch("a.sql")
    assert not sqldump.RESTORE_NAME.fullmatch(".sql") and not sqldump.RESTORE_NAME.fullmatch("../a.sql")


def test_load_sql_reads_the_head_and_its_restrict_key():
    head, key = load_sql.read_head(io.BytesIO(DUMP))
    assert key == KEY
    assert b"".join(head).startswith(b"--\n-- PostgreSQL database dump\n")
    assert not any(line.startswith(b"\\restrict") for line in head)
    head, key = load_sql.read_head(io.BytesIO(b"--\n-- PostgreSQL database dump\n--\nSET x = 1;\n"))
    assert key is None and head[-1] == b"SET x = 1;\n"
    for content, said in [(b"PGDMP\x01", "custom format"), (b"--\n-- PostgreSQL database cluster dump\n",
                                                             "not a plain SQL dump"), (b"", "not a plain SQL dump")]:
        with pytest.raises(SystemExit, match=said):
            load_sql.read_head(io.BytesIO(content))


def fake_odoo(tmp_path, psql_exit=0):
    """What load() takes of odoo: a psql that copies its standard input,
    tells one SQL error and ends with psql_exit."""
    psql = tmp_path / "psql"
    psql.write_text(f"#!/bin/sh\necho \"$*\" > {tmp_path / 'args'}\ncat > {tmp_path / 'fed.sql'}\n"
                    "echo 'psql:<stdin>:9: ERROR:  cannot change data type of view column' >&2\n"
                    f"exit {psql_exit}\n")
    psql.chmod(0o755)
    misc = SimpleNamespace(find_pg_tool=lambda name: str(psql), exec_pg_environ=lambda: {"PATH": "/usr/bin:/bin"})
    return SimpleNamespace(tools=SimpleNamespace(misc=misc))


def test_load_sql_feeds_psql_without_the_restrict_lines_and_counts_its_errors(tmp_path, capfd):
    source = io.BytesIO(DUMP)
    head, key = load_sql.read_head(source)
    assert load_sql.load(fake_odoo(tmp_path), "DB", source, head, key, len(DUMP)) == 1
    fed = (tmp_path / "fed.sql").read_bytes()
    assert fed == DUMP.replace(b"\\restrict " + KEY + b"\n", b"").replace(b"\\unrestrict " + KEY + b"\n", b"")
    # no ON_ERROR_STOP: psql goes past an error, as Odoo's restore does
    assert (tmp_path / "args").read_text().split() == ["-X", "-q", "--dbname=DB", "-f", "-"]
    out, err = capfd.readouterr()
    assert "ERROR:  cannot change data type" in err and "OMNIACLI_SQL_ERRORS=1" in out


def test_load_sql_fails_when_psql_does(tmp_path):
    source = io.BytesIO(DUMP)
    head, key = load_sql.read_head(source)
    with pytest.raises(SystemExit, match="exit code 2"):
        load_sql.load(fake_odoo(tmp_path, psql_exit=2), "DB", source, head, key, 0)


@pytest.mark.skipif(sys.version_info < (3, 8), reason="compiled as Python 2.7 below")
def test_load_sql_is_python_2_7_too():
    """Odoo 10 runs on Python 2.7: no f-string, no syntax of Python 3 only
    (python2.7 compiles it when the machine has one)."""
    import shutil
    import subprocess

    python2 = shutil.which("python2.7") or shutil.which("python2")
    if python2 is None:
        pytest.skip("no python2.7 on this machine")
    # compile() only: py_compile would leave a .pyc beside the file
    subprocess.run([python2, "-c", "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')",
                    load_sql.__file__], check=True)
