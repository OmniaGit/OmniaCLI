# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by click-odoo on an instance's database (env: its database;
argument: the days until the new expiration): the database gets a new
database.uuid and database.create_date, as Odoo's restore of a copy gives
it (ir.config_parameter.init(force=True)), and database.expiration_date
that many days from now, what Odoo Enterprise's subscription banner reads.

The same keys from Odoo 10 to 20 (mail/models/update.py writes
database.expiration_date, web_enterprise reads it). Written in SQL, as
load_sql.py does, so get_param/get_str of the version does not matter; a
running Odoo keeps the old values in its cache until it is restarted.
database.secret is left as it is: a new one would end every session.
click-odoo commits at the end."""

import datetime
import sys
import uuid

days = int(sys.argv[1])
cr = env.cr  # noqa: F821 (given by click-odoo)
now = datetime.datetime.utcnow()
# the format of fields.Datetime, which the banner deserializes
expiration = (now + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
cr.execute("SELECT value FROM ir_config_parameter WHERE key = 'database.expiration_date'")
row = cr.fetchone()
print("database.expiration_date was %s" % (row[0] if row else "not set"))
for name, value in (("database.uuid", str(uuid.uuid1())),
                    ("database.create_date", now.strftime("%Y-%m-%d %H:%M:%S")),
                    ("database.expiration_date", expiration)):
    cr.execute("UPDATE ir_config_parameter SET value = %s WHERE key = %s", (value, name))
    if not cr.rowcount:
        cr.execute("INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)", (name, value))
    print("%s = %s" % (name, value))
