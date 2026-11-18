# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by the venv's python of an Odoo instance (as the user it runs as),
with the folder of odoo-bin on PYTHONPATH: a plain SQL dump (pg_dump
--format=plain) read on the standard input replaces the instance's
database. The filestore is not touched.

    load_sql.py <odoo config> <database> <neutralize 0|1> <web.base.url> <bytes>

1. The head of the dump is read first: what is no plain dump of one
   database is refused before the database is dropped.
2. The database is dropped (its other connections ended first) and made
   again by Odoo's _create_empty_database, as Odoo's own restore does.
3. psql loads the dump and goes on past an SQL error, as Odoo's restore
   (restore_db) and a plain "psql < dump" do: a dump of a newer PostgreSQL
   may fail on a statement (checked 2026-11-17: a dump of PostgreSQL 16 into
   12 fails on CREATE OR REPLACE VIEW of a report view, which Odoo makes
   again at the update of its module), and stopping there would leave out
   the foreign keys that come after it. Every error line goes to the
   standard error and their count is printed (ERRORS_MARK); the load fails
   only when psql does (exit code not 0). The \\restrict / \\unrestrict
   lines that pg_dump writes since its August 2025 releases are left out:
   an older psql takes them for an unknown command.
4. With neutralize (Odoo 16 and later), Odoo's neutralize_database runs:
   the function odoo-bin neutralize and Odoo's restore run.
5. What Odoo's restore of a copy does (ir.config_parameter.init(force=True),
   Odoo 10 to 20): a new database.secret, database.uuid and
   database.create_date; and web.base.url, given (empty: Odoo's default,
   http://localhost:<http_port>).

<bytes>: the size of the dump, for the progress lines (0: not known).

Odoo 10 runs on Python 2.7: this file is Python 2.7 and 3. It uses only
what Odoo 10 to 20 have: tools.config.parse_config, sql_db.db_connect and
connection_info_for, tools.misc.find_pg_tool and exec_pg_environ,
_create_empty_database (service/db.py, modules/db.py from Odoo 20) and
modules.neutralize (Odoo 16 and later).
"""

from __future__ import print_function

import datetime
import errno
import os
import subprocess
import sys
import threading
import uuid

# the line that opens every dump of pg_dump (not pg_dumpall's)
DUMP_TITLE = b"-- PostgreSQL database dump"
# the magic of pg_dump's custom format
CUSTOM_MAGIC = b"PGDMP"
# the lines of the head read before the database is touched
HEAD_LINES = 200
PROGRESS_EVERY = 256 << 20
ERRORS_MARK = "OMNIACLI_SQL_ERRORS="


def quote(name):
    """A SQL identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


def read_head(source):
    """The first lines of the dump and the key of its \\restrict line (None:
    none). Refused (SystemExit) when it is no plain dump of one database."""
    head = []
    for _ in range(HEAD_LINES):
        line = source.readline()
        if not line:
            break
        head.append(line)
        if line.strip() and not line.startswith(b"--") and not line.startswith(b"\\restrict "):
            break
    first = head[0] if head else b""
    if first.startswith(CUSTOM_MAGIC):
        raise SystemExit("the file is a pg_dump of the custom format (pg_restore's): a plain SQL dump "
                         "(pg_dump --format=plain) is asked")
    if not any(line.rstrip(b"\r\n") == DUMP_TITLE for line in head):
        raise SystemExit("the file is not a plain SQL dump of pg_dump: its head has no line %r"
                         % DUMP_TITLE.decode())
    key = None
    kept = []
    for line in head:
        if key is None and line.startswith(b"\\restrict "):
            key = line[len(b"\\restrict "):].strip()
            continue
        kept.append(line)
    return kept, key


def drop_and_create(odoo, database):
    """The database dropped (its other connections ended first), then made
    again by Odoo: the template, encoding and extensions of its version."""
    import psycopg2

    _, info = odoo.sql_db.connection_info_for("postgres")
    cnx = psycopg2.connect(**info)
    try:
        cnx.autocommit = True
        cr = cnx.cursor()
        cr.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   "WHERE datname = %s AND pid <> pg_backend_pid()", (database,))
        cr.execute("DROP DATABASE IF EXISTS " + quote(database))
    finally:
        cnx.close()
    try:
        from odoo.service.db import _create_empty_database
    except ImportError:
        # Odoo 20
        from odoo.modules.db import _create_empty_database
    _create_empty_database(database)
    odoo.sql_db.close_db(database)


def relay_errors(stream, found):
    """psql's standard error, line by line, to ours; found[0]: the lines
    that tell an SQL error."""
    out = getattr(sys.stderr, "buffer", sys.stderr)
    for line in iter(stream.readline, b""):
        if b"ERROR:" in line:
            found[0] += 1
        out.write(line)
        out.flush()
    stream.close()


def load(odoo, database, source, head, key, total):
    """psql loads the dump into the database; the \\unrestrict line of key is
    left out. The number of its SQL errors; SystemExit when it fails."""
    psql = odoo.tools.misc.find_pg_tool("psql")
    devnull = open(os.devnull, "wb")
    proc = subprocess.Popen([psql, "-X", "-q", "--dbname=" + database, "-f", "-"],
                            stdin=subprocess.PIPE, stdout=devnull, stderr=subprocess.PIPE,
                            env=odoo.tools.misc.exec_pg_environ())
    errors = [0]
    relay = threading.Thread(target=relay_errors, args=(proc.stderr, errors))
    relay.start()
    closing = b"\\unrestrict " + key if key else None
    sent = 0
    shown = PROGRESS_EVERY
    try:
        try:
            for line in head:
                proc.stdin.write(line)
                sent += len(line)
            for line in iter(source.readline, b""):
                sent += len(line)
                if closing is not None and line.startswith(b"\\unrestrict ") and line.strip() == closing:
                    continue
                proc.stdin.write(line)
                if sent >= shown:
                    shown += PROGRESS_EVERY
                    print("loaded %d MiB%s" % (sent >> 20, " of %d MiB" % (total >> 20) if total else ""))
                    sys.stdout.flush()
            proc.stdin.close()
        except (IOError, OSError) as exc:
            # psql ended (connection lost): its message is on the standard error
            if exc.errno != errno.EPIPE:
                raise
    finally:
        code = proc.wait()
        relay.join()
        devnull.close()
    sys.stdout.flush()
    if code:
        raise SystemExit("psql stopped with exit code %d after %d MiB of the dump: the database is "
                         "half loaded" % (code, sent >> 20))
    print("loaded %d MiB: the whole dump" % (sent >> 20))
    print(ERRORS_MARK + str(errors[0]))
    return errors[0]


def after_load(odoo, database, neutralize, base_url):
    """Neutralize (Odoo's own), then the parameters of a copy."""
    config = odoo.tools.config
    cr = odoo.sql_db.db_connect(database).cursor()
    try:
        if neutralize:
            import odoo.modules.neutralize

            odoo.modules.neutralize.neutralize_database(cr)
            print("neutralized by Odoo (neutralize.sql of every installed module)")
        if not base_url:
            base_url = "http://localhost:%s" % (config.get("http_port") or config.get("xmlrpc_port"))
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for name, value in (("database.secret", str(uuid.uuid4())), ("database.uuid", str(uuid.uuid1())),
                            ("database.create_date", now), ("web.base.url", base_url)):
            cr.execute("UPDATE ir_config_parameter SET value = %s WHERE key = %s", (value, name))
            if not cr.rowcount:
                cr.execute("INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)", (name, value))
        cr.commit()
        print("a new database.uuid and database.secret; web.base.url = %s" % base_url)
    finally:
        cr.close()


def main(argv):
    config, database, neutralize, base_url = argv[1], argv[2], argv[3] == "1", argv[4]
    total = int(argv[5] or 0)
    source = getattr(sys.stdin, "buffer", sys.stdin)
    head, key = read_head(source)
    # Odoo 19 and later import their parts only when asked
    import odoo
    import odoo.sql_db
    import odoo.tools
    import odoo.tools.misc

    odoo.tools.config.parse_config(["-c", config])
    if neutralize:
        try:
            import odoo.modules.neutralize  # noqa: F401
        except ImportError:
            raise SystemExit("this Odoo has no neutralize (Odoo 16 and later): nothing was touched")
    drop_and_create(odoo, database)
    print("database %s dropped and made again" % database)
    sys.stdout.flush()
    load(odoo, database, source, head, key, total)
    after_load(odoo, database, neutralize, base_url)


if __name__ == "__main__":
    main(sys.argv)
