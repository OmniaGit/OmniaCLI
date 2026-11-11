# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by the venv's python of an Odoo instance (as the user it runs as),
with the folder of odoo-bin on PYTHONPATH: the database's binary columns,
and a backup of it with some of them emptied.

    binary_dump.py scan <odoo config> <database>
        prints COLUMNS_MARK and, as JSON, the database's size on disk and
        every bytea column: {size, columns: [{schema, table, column, bytes,
        rows, not_null}]}
    binary_dump.py dump <odoo config> <database> <dest zip> <columns> <filestore 0|1>
        writes the zip Odoo restores (dump.sql, manifest.json, filestore/)
        with the columns emptied; columns: JSON [[schema, table, column]]

pg_dump cannot leave a column out, so the dump is made of its sections on
one snapshot: the schema (pre-data), the data of every table but those that
hold an emptied column, the rows of those tables copied here with the
column empty (NULL, or '' where it is NOT NULL), then indexes and
constraints (post-data). dump.sql restores as pg_dump's own.

Odoo 10 runs on Python 2.7: this file is Python 2.7 and 3. It uses only
what Odoo 10 to 20 all have: tools.config.parse_config and filestore,
sql_db.connection_info_for, tools.misc.exec_pg_environ, release.
"""

from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

COLUMNS_MARK = "OMNIACLI_BINARY_COLUMNS="
# the schemas of PostgreSQL itself
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def quote(name):
    """A SQL identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


def table_pattern(schema, table):
    """A pg_dump pattern naming exactly one table (quoted: no wildcard)."""
    return quote(schema) + "." + quote(table)


def copy_select(columns, emptied, not_null):
    """The SELECT whose rows are the table's, the emptied columns left empty.
    columns: the table's columns in order; emptied: the names to empty;
    not_null: those of them NOT NULL, which get '' instead of NULL."""
    out = []
    for name in columns:
        if name not in emptied:
            out.append(quote(name))
        elif name in not_null:
            out.append("''::bytea AS " + quote(name))
        else:
            out.append("NULL::bytea AS " + quote(name))
    return ", ".join(out)


def scan(cr):
    """Every bytea column of the database's tables, with the bytes it holds
    (pg_column_size: as stored, the TOAST pointer gives it without reading
    the value) and the rows that have one."""
    cr.execute(
        "SELECT n.nspname, c.relname, a.attname, a.attnotnull FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE a.atttypid = 'bytea'::regtype AND c.relkind = 'r' AND a.attnum > 0 "
        "AND NOT a.attisdropped AND n.nspname NOT IN %s AND n.nspname NOT LIKE 'pg_temp%%' "
        "ORDER BY 1, 2, 3", (SYSTEM_SCHEMAS,))
    found = []
    for schema, table, column, not_null in cr.fetchall():
        cr.execute("SELECT coalesce(sum(pg_column_size(%s)), 0), count(%s) FROM %s.%s"
                   % (quote(column), quote(column), quote(schema), quote(table)))
        size, rows = cr.fetchone()
        found.append({"schema": schema, "table": table, "column": column, "bytes": int(size),
                      "rows": int(rows), "not_null": bool(not_null)})
    return found


def table_columns(cr, schema, table):
    """The columns COPY writes, in order: not dropped, not generated
    (pg_attribute.attgenerated exists from PostgreSQL 12)."""
    cr.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'pg_catalog.pg_attribute'::regclass "
               "AND attname = 'attgenerated'")
    generated = " AND a.attgenerated = ''" if cr.fetchone() else ""
    cr.execute("SELECT a.attname, a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
               "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s "
               "AND a.attnum > 0 AND NOT a.attisdropped" + generated + " ORDER BY a.attnum",
               (schema, table))
    return cr.fetchall()


def pg_dump(env, database, snapshot, section, out, exclude_data=()):
    """One section of pg_dump, in plain SQL, appended to out."""
    cmd = ["pg_dump", "--no-owner", "--snapshot=" + snapshot, "--section=" + section]
    for schema, table in exclude_data:
        cmd.append("--exclude-table-data=" + table_pattern(schema, table))
    cmd.append(database)
    out.flush()
    code = subprocess.call(cmd, env=env, stdin=open(os.devnull, "rb"), stdout=out)
    if code:
        raise SystemExit("pg_dump --section=%s failed (exit %d)" % (section, code))


def write_dump(cnx, env, database, chosen, out):
    """dump.sql into out: the sections of pg_dump on the snapshot of cnx (a
    transaction REPEATABLE READ), the tables of chosen copied here."""
    cr = cnx.cursor()
    cr.execute("SELECT pg_export_snapshot()")
    snapshot = cr.fetchone()[0]
    tables = []
    for schema, table, column in chosen:
        if (schema, table) not in tables:
            tables.append((schema, table))
    pg_dump(env, database, snapshot, "pre-data", out)
    pg_dump(env, database, snapshot, "data", out, tables)
    for schema, table in tables:
        emptied = set(c for s, t, c in chosen if (s, t) == (schema, table))
        found = table_columns(cr, schema, table)
        names = [name for name, _ in found]
        not_null = set(name for name, nn in found if nn)
        head = "COPY %s.%s (%s) FROM stdin;\n" % (quote(schema), quote(table), ", ".join(quote(n) for n in names))
        out.write(("\n--\n-- %s.%s: data with %s emptied (OmniaCLI)\n--\n\n"
                   % (schema, table, ", ".join(sorted(emptied)))).encode("utf-8"))
        out.write(head.encode("utf-8"))
        cr.copy_expert("COPY (SELECT %s FROM %s.%s) TO STDOUT"
                       % (copy_select(names, emptied, not_null), quote(schema), quote(table)), out)
        out.write(b"\\.\n\n")
    pg_dump(env, database, snapshot, "post-data", out)
    cr.close()


def manifest(cr, odoo, database):
    """manifest.json as Odoo's dump_db_manifest writes it."""
    version = cr.connection.server_version
    cr.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
    return {
        "odoo_dump": "1",
        "db_name": database,
        "version": odoo.release.version,
        "version_info": odoo.release.version_info,
        "major_version": odoo.release.major_version,
        "pg_version": "%d.%d" % divmod(version // 100, 100),
        "modules": dict(cr.fetchall()),
    }


def add_filestore(zf, folder):
    """The filestore under filestore/ of the zip, as Odoo's backup has it."""
    for root, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            zf.write(path, os.path.join("filestore", os.path.relpath(path, folder)))


def connect(odoo, database):
    """A psycopg2 connection to the database as Odoo makes it, in a
    transaction REPEATABLE READ that only reads."""
    import psycopg2
    import psycopg2.extensions

    _, info = odoo.sql_db.connection_info_for(database)
    cnx = psycopg2.connect(**info)
    cnx.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ, readonly=True)
    return cnx


def main(argv):
    mode, config, database = argv[1], argv[2], argv[3]
    # Odoo 19 and later import their parts only when asked
    import odoo
    import odoo.release
    import odoo.sql_db
    import odoo.tools
    import odoo.tools.misc

    odoo.tools.config.parse_config(["-c", config])
    cnx = connect(odoo, database)
    try:
        if mode == "scan":
            cr = cnx.cursor()
            cr.execute("SELECT pg_database_size(current_database())")
            size = int(cr.fetchone()[0])
            print(COLUMNS_MARK + json.dumps({"size": size, "columns": scan(cr)}))
            return
        if mode != "dump":
            raise SystemExit("unknown mode %r" % mode)
        dest, chosen, filestore = argv[4], [tuple(c) for c in json.loads(argv[5])], argv[6] == "1"
        if os.path.exists(dest):
            raise SystemExit("%s already exists" % dest)
        folder = tempfile.mkdtemp(prefix="dump-", dir=os.path.dirname(os.path.abspath(dest)))
        try:
            sql = os.path.join(folder, "dump.sql")
            with open(sql, "wb") as out:
                write_dump(cnx, odoo.tools.misc.exec_pg_environ(), database, chosen, out)
            data = manifest(cnx.cursor(), odoo, database)
            zf = zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
            try:
                zf.writestr("manifest.json", json.dumps(data, indent=4))
                if filestore:
                    store = odoo.tools.config.filestore(database)
                    if os.path.isdir(store):
                        add_filestore(zf, store)
                zf.write(sql, "dump.sql")
            finally:
                zf.close()
        except BaseException:
            if os.path.exists(dest):
                os.unlink(dest)
            raise
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    finally:
        cnx.close()


if __name__ == "__main__":
    main(sys.argv)
