# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Renew expiration of the Odoo plugin: a new database.uuid and expiration
date by click-odoo (renew_expiration.py), then a restart if asked; on a
production an admin's only. The script's own SQL on a fake cursor."""

import datetime
import runpy
import sys
import uuid

from omniacli.odoo import ops as odoo_ops

from .test_app import HOST, client, wait_job  # noqa: F401
from .test_links import basic


def headers(user):
    return {"Authorization": basic(user), "Origin": f"http://{HOST}"}


async def test_a_developer_renews_test_and_it_restarts(client, env):
    page = (await client.get("/servers/srv?view=both", headers=headers("dev"))).text
    assert "/servers/srv/test/x/odoo/renew-expiration" in page
    assert "renew-expiration-production" not in page
    resp = await client.post("/servers/srv/test/x/odoo/renew-expiration", headers=headers("dev"),
                             data={"days": "45", "restart": "on"})
    assert resp.status_code == 303, resp.text
    job = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert job["status"] == "success", job["lines"]
    assert job["title"] == "Renew expiration srv/test (45 days)"
    done = env["calls"].read_text().splitlines()
    assert done == [f"click-odoo -c {env['tmp']}/odoo.conf -d DB_test renew_expiration.py 45",
                    "restart odoo-test"]


async def test_without_restart_and_the_days_refused(client, env):
    resp = await client.post("/servers/srv/test/x/odoo/renew-expiration", headers=headers("dev"),
                             data={"days": "30"})
    job = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert job["status"] == "success", job["lines"]
    assert env["calls"].read_text().splitlines() == [
        f"click-odoo -c {env['tmp']}/odoo.conf -d DB_test renew_expiration.py 30"]
    for days in ("0", "-3", "4000", "x"):
        resp = await client.post("/servers/srv/test/x/odoo/renew-expiration", headers=headers("dev"),
                                 data={"days": days})
        assert resp.status_code == 400, days


async def test_production_is_an_admins_with_a_warning(client, env):
    url = "/servers/srv/production/x/odoo/renew-expiration-production"
    assert (await client.post(url, headers=headers("dev"), data={"days": "30"})).status_code == 403
    # the test action does not reach production
    assert (await client.post("/servers/srv/production/x/odoo/renew-expiration", headers=headers("admin"),
                              data={"days": "30"})).status_code == 404
    page = (await client.get("/servers/srv?view=both", headers=headers("admin"))).text
    assert url in page and "detaches it from its Odoo subscription" in page
    resp = await client.post(url, headers=headers("admin"), data={"days": "30", "restart": "on"})
    job = await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    assert job["status"] == "success", job["lines"]
    assert env["calls"].read_text().splitlines() == [
        f"click-odoo -c {env['tmp']}/odoo.conf -d DB_production renew_expiration.py 30",
        "restart odoo-production"]


class FakeCursor:
    """ir_config_parameter as a dict, through the three statements the script runs."""

    def __init__(self, params):
        self.params, self.rowcount, self.row = params, 0, None

    def execute(self, sql, args=()):
        if sql.startswith("SELECT value"):
            value = self.params.get("database.expiration_date")
            self.row = (value,) if value else None
        elif sql.startswith("UPDATE"):
            value, key = args
            self.rowcount = int(key in self.params)
            if self.rowcount:
                self.params[key] = value
        elif sql.startswith("INSERT"):
            key, value = args
            self.params[key] = value
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.row


def test_the_script_writes_uuid_create_date_and_expiration(monkeypatch, capsys):
    old_uuid = str(uuid.uuid1())
    params = {"database.uuid": old_uuid, "database.secret": "s", "database.create_date": "2020-01-01 00:00:00"}
    cr = FakeCursor(params)
    monkeypatch.setattr(sys, "argv", ["renew_expiration.py", "10"])
    before = datetime.datetime.utcnow()
    runpy.run_path(str(odoo_ops.RENEW_SCRIPT), init_globals={"env": type("Env", (), {"cr": cr})})
    assert params["database.uuid"] != old_uuid and uuid.UUID(params["database.uuid"]).version == 1
    assert params["database.secret"] == "s"
    expiration = datetime.datetime.strptime(params["database.expiration_date"], "%Y-%m-%d %H:%M:%S")
    assert datetime.timedelta(days=10) - datetime.timedelta(seconds=2) \
        <= expiration - before <= datetime.timedelta(days=10, seconds=2)
    assert params["database.create_date"][:10] == before.strftime("%Y-%m-%d")
    out = capsys.readouterr().out
    assert "database.expiration_date was not set" in out and "database.uuid = " in out
