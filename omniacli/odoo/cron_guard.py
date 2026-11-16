# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by click-odoo around a module update (env: the database; arguments:
before, or after and what before printed). A cron whose XML says active=True
outside noupdate is switched on by the update of its module, and Odoo guards
a neutralized database only in ir.cron.toggle.

before prints the active crons of a neutralized database, nothing for any
other database; after switches off every other cron, also one the update
added, and prints the ones it switched off. Crons someone switched on by hand
stay on. click-odoo commits at the end."""

import sys

MARK = "OMNIACLI_ACTIVE_CRONS="

cr = env.cr  # noqa: F821 (given by click-odoo)
if sys.argv[1] == "before":
    cr.execute("SELECT 1 FROM ir_config_parameter WHERE key = 'database.is_neutralized'")
    if cr.fetchone():
        cr.execute("SELECT id FROM ir_cron WHERE active")
        print(MARK + ",".join(str(row[0]) for row in cr.fetchall()))
else:
    active = [int(found) for found in sys.argv[2].split(",") if found]
    cr.execute("""
        WITH off AS (UPDATE ir_cron SET active = false
                      WHERE active AND NOT (id = ANY(%s::int[])) RETURNING id)
        SELECT coalesce(d.module || '.' || d.name, 'id ' || off.id)
          FROM off LEFT JOIN ir_model_data d ON d.model = 'ir.cron' AND d.res_id = off.id
    """, (active,))
    for row in cr.fetchall():
        print("switched off again: " + row[0])
