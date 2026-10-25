# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by click-odoo on a restored copy (env: its database; argument: the
Odoo major version): the password of every user becomes admin. It is
written in SQL, so no password policy refuses it.

From Odoo 12 the password column holds the pbkdf2_sha512 hash. Odoo 10 and
11 with auth_crypt (auto installed) hold it in password_crypt with an empty
password; without auth_crypt they compare the password column in clear. A
database migrated to 12 may keep a password_crypt column: the version, not
the column, decides. click-odoo commits at the end."""

import sys

from passlib.context import CryptContext

PASSWORD = "admin"

major = int(sys.argv[1])
cr = env.cr  # noqa: F821 (given by click-odoo)
context = CryptContext(["pbkdf2_sha512"])
# hash() from passlib 1.7, encrypt() before
hashed = context.hash(PASSWORD) if hasattr(context, "hash") else context.encrypt(PASSWORD)
if major >= 12:
    cr.execute("UPDATE res_users SET password = %s", (hashed,))
else:
    cr.execute("SELECT 1 FROM ir_module_module WHERE name = 'auth_crypt' AND state = 'installed'")
    if cr.fetchone():
        cr.execute("UPDATE res_users SET password = '', password_crypt = %s", (hashed,))
    else:
        cr.execute("UPDATE res_users SET password = %s", (PASSWORD,))
print("password %s set for %d users" % (PASSWORD, cr.rowcount))
