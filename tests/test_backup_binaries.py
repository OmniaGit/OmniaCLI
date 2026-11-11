# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup without binary columns: the dialog's choices, remembered per
instance, and what the backups without a dialog take from them."""
import json

import pytest

from omniacli import backupchoices
from omniacli.odoo import binary_dump
from omniacli.odoo import ops as odoo_ops

from .test_app import AUTH, HOST, POST, basic, client, wait_job  # noqa: F401

MB = 1 << 20
COLUMNS = {"size": 900 * MB, "columns": [
    {"schema": "public", "table": "plm_document", "column": "printout", "bytes": 400 * MB, "rows": 9,
     "not_null": False},
    {"schema": "public", "table": "ir_attachment", "column": "preview", "bytes": 30 * MB, "rows": 5,
     "not_null": True},
    {"schema": "public", "table": "res_company", "column": "logo_web", "bytes": 2 * MB, "rows": 1,
     "not_null": False},
    {"schema": "public", "table": "base_import_import", "column": "file", "bytes": 0, "rows": 0,
     "not_null": False},
]}


@pytest.fixture
def scanned(monkeypatch):
    """binary_columns answers COLUMNS; the backups the plugin makes record
    the columns they empty, and the click-odoo-backupdb ones run as before."""
    made = []

    async def binary_columns(instance):
        return json.loads(json.dumps(COLUMNS))

    real = odoo_ops.backup

    async def backup(job, instance, dest_dir, filestore=True, emptied=None):
        made.append([odoo_dump_name(c) for c in emptied] if emptied else None)
        if not emptied:
            return await real(job, instance, dest_dir, filestore)
        dest = dest_dir / f"{instance.options.database}_x{odoo_ops.NO_BINARIES}.zip"
        dest.write_text("dump\n")
        return dest

    monkeypatch.setattr(odoo_ops, "binary_columns", binary_columns)
    monkeypatch.setattr(odoo_ops, "backup", backup)
    return made


def odoo_dump_name(column):
    return f"{column['table']}.{column['column']}"


def test_ticked_rule():
    rows = [{"value": "a.x", "size": 50 * MB}, {"value": "b.y", "size": 50 * MB},
            {"value": "c.z", "size": 1 * MB}, {"value": "d.new", "size": 20 * MB}]
    remembered = {"values": {"cols": ["c.z"]}, "shown": {"cols": ["a.x", "c.z"]}}
    # a.x shown and left unticked: off; c.z ticked: on; b.y, d.new new: by size
    assert backupchoices.ticked(rows, remembered, "cols", 10) == ["b.y", "c.z", "d.new"]
    assert backupchoices.ticked(rows, {}, "cols", 30) == ["a.x", "b.y"]
    assert backupchoices.ticked(rows, {}, "cols", None) == []


def test_load_save(tmp_path):
    assert backupchoices.load(tmp_path) == {}
    backupchoices.save(tmp_path, {"no_binaries": True, "cols": ["a.x"]}, {"cols": ["a.x", "b.y"]}, "anna")
    got = backupchoices.load(tmp_path)
    assert got["values"] == {"no_binaries": True, "cols": ["a.x"]} and got["shown"] == {"cols": ["a.x", "b.y"]}
    assert got["user"] == "anna" and got["saved"]
    (tmp_path / backupchoices.FILE_NAME).write_text("not json")
    assert backupchoices.load(tmp_path) == {}


def test_dump_sql_parts():
    assert binary_dump.quote('a"b') == '"a""b"'
    assert binary_dump.table_pattern("public", "plm_document") == '"public"."plm_document"'
    assert binary_dump.copy_select(["id", "printout", "preview"], {"printout", "preview"}, {"preview"}) == \
        '"id", NULL::bytea AS "printout", \'\'::bytea AS "preview"'


async def test_dialog_lists_the_columns_and_remembers_the_choice(client, env, scanned):
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'name="no_binaries"' in page and 'data-checklist="binary_columns"' in page
    assert 'data-shown-with="no_binaries"' in page and 'name="binary_threshold_mb"' in page
    url = "/api/servers/srv/production/backup/choices"
    first = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert first["remembered"]["values"] == {} and "rows" not in first
    listed = (await client.get(url + "?key=binary_columns", headers={"Authorization": AUTH})).json()
    # the columns that hold data, the biggest first, with the database's size
    assert [r["value"] for r in listed["rows"]] == ["plm_document.printout", "ir_attachment.preview",
                                                     "res_company.logo_web"]
    assert listed["rows"][0]["size"] == 400 * MB and listed["total"] == 900 * MB
    assert "NOT NULL" in listed["rows"][1]["detail"]
    assert (await client.get(url + "?key=nope", headers={"Authorization": AUTH})).status_code == 400

    # the dialog posts the ticks, the rows shown and remember
    resp = await client.post("/servers/srv/production/backup", headers=POST, data={
        "no_binaries": "on", "binary_threshold_mb": "10", "binary_columns": "ir_attachment.preview",
        "binary_columns__shown": "plm_document.printout,ir_attachment.preview,res_company.logo_web",
        "remember": "1"})
    data = await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert data["status"] == "success", data
    assert data["title"] == "Backup srv/production (without binary columns)"
    assert data["result"]["file"].endswith("_nobinaries.zip")
    assert scanned == [["ir_attachment.preview"]]
    again = (await client.get(url, headers={"Authorization": AUTH})).json()["remembered"]
    assert again["values"]["no_binaries"] is True and again["values"]["binary_columns"] == ["ir_attachment.preview"]
    assert again["values"]["binary_threshold_mb"] == 10 and again["user"] == "admin"
    assert again["shown"]["binary_columns"] == ["plm_document.printout", "ir_attachment.preview",
                                                "res_company.logo_web"]
    # the other instance keeps its own (none)
    other = (await client.get("/api/servers/srv/test/backup/choices", headers={"Authorization": AUTH})).json()
    assert other["remembered"]["values"] == {}

    # a backup without the dialog (a script, a schedule): the last ticks
    resp = await client.post("/servers/srv/production/backup", headers=POST, data={"no_binaries": "on"})
    data = await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert data["status"] == "success" and scanned[-1] == ["ir_attachment.preview"], data
    # with the checkbox off, the dialog keeps the ticks for next time
    resp = await client.post("/servers/srv/production/backup", headers=POST, data={"remember": "1"})
    await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert scanned[-1] is None
    kept = (await client.get(url, headers={"Authorization": AUTH})).json()["remembered"]
    assert kept["values"]["no_binaries"] is False and kept["values"]["binary_columns"] == ["ir_attachment.preview"]


async def test_without_a_choice_the_threshold(client, env, scanned):
    # nobody chose: every column of at least 10 MB
    resp = await client.post("/servers/srv/production/backup", headers=POST, data={"no_binaries": "on"})
    data = await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert data["status"] == "success", data
    assert scanned == [["plm_document.printout", "ir_attachment.preview"]]
    # a threshold of its own, and a list of none: a whole backup
    resp = await client.post("/servers/srv/production/backup", headers=POST,
                             data={"no_binaries": "on", "binary_threshold_mb": "1"})
    await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert scanned[-1] == ["plm_document.printout", "ir_attachment.preview", "res_company.logo_web"]
    resp = await client.post("/servers/srv/production/backup", headers=POST,
                             data={"no_binaries": "on", "binary_columns": ""})
    data = await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    assert scanned[-1] is None and not data["result"]["file"].endswith("_nobinaries.zip")
    bad = await client.post("/servers/srv/production/backup", headers=POST,
                            data={"no_binaries": "on", "binary_columns": "a b"})
    assert bad.status_code == 400


async def test_copy_production_takes_the_last_ticks(client, env, scanned):
    from .test_copy_production import URL

    resp = await client.post("/servers/srv/production/backup", headers=POST, data={
        "no_binaries": "on", "binary_columns": "res_company.logo_web",
        "binary_columns__shown": "plm_document.printout,res_company.logo_web", "remember": "1"})
    await wait_job(client, resp.headers["location"].split("/jobs/")[1])
    resp = await client.post(URL, headers=POST, json={"backup": "", "no_binaries": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["title"] == "Copy production to test srv (new backup, binary columns emptied)", job["title"]
    # ir_attachment.preview was not shown last time: ticked by its size
    assert scanned[-1] == ["ir_attachment.preview", "res_company.logo_web"]


async def test_estimate_gives_the_weight_and_the_last_ticks(client, env, scanned, monkeypatch):
    url = "/api/servers/srv/production/backup/estimate"
    COLUMNS["filestore"] = {"path": "/x", "bytes": 50 * MB, "files": 3}
    try:
        got = (await client.get(url, headers={"Authorization": AUTH})).json()
        assert got["database"] == 900 * MB and got["files"] == 50 * MB
        assert got["files_off"] == "no_filestore" and got["list"] == "binary_columns" and got["list_on"] == "no_binaries"
        # nobody chose yet: the columns of at least 10 MB
        assert got["ticked"] == {"binary_columns": ["plm_document.printout", "ir_attachment.preview"]}
        resp = await client.post("/servers/srv/production/backup", headers=POST, data={
            "no_binaries": "on", "binary_columns": "res_company.logo_web",
            "binary_columns__shown": "plm_document.printout,ir_attachment.preview,res_company.logo_web",
            "remember": "1"})
        await wait_job(client, resp.headers["location"].split("/jobs/")[1])
        got = (await client.get(url, headers={"Authorization": AUTH})).json()
        assert got["ticked"] == {"binary_columns": ["res_company.logo_web"]} and got["remembered"]["saved"]
    finally:
        COLUMNS.pop("filestore")


async def test_room_places_each_folder_on_its_disk(client, env, monkeypatch):
    tmp = env["tmp"]

    async def restore_room(instance):
        return {"data_directory": None, "db_host": "db.example", "db_port": 5432, "database_bytes": 7 * MB,
                "filestore": {"path": str(tmp / "filestore" / "DB_test"), "bytes": 3 * MB, "files": 2},
                "temp": str(tmp)}

    monkeypatch.setattr(odoo_ops, "restore_room", restore_room)
    got = (await client.get("/api/servers/srv/test/restore/room", headers={"Authorization": AUTH})).json()
    # PostgreSQL on another machine: its disk is not checked, and said so
    assert got["unknown"] == ["database"] and any("db.example" in n for n in got["notes"])
    roles = [r for d in got["disks"] for r in d["roles"]]
    assert sorted(roles) == ["backups", "files", "temp"] and all(d["free"] > 0 for d in got["disks"])
    assert got["freed"] == {"database": 7 * MB, "files": 3 * MB}
    # a user may not read it (developer and admin may)
    assert (await client.get("/api/servers/srv/test/restore/room",
                             headers={"Authorization": basic("viewer")})).status_code == 403


async def test_room_unknown_path():
    from omniacli import restoreroom

    got = await restoreroom.room({"backups": "/", "temp": None, "database": "relative/folder"}, {}, [])
    assert [d["mount"] for d in got["disks"]] and "temp" in got["unknown"] and "files" in got["unknown"]
