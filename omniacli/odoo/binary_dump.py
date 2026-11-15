# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by the venv's python of an Odoo instance (as the user it runs as),
with the folder of odoo-bin on PYTHONPATH: the database's binary columns,
and a backup of it with some of them emptied.

    binary_dump.py scan <odoo config> <database>
        prints COLUMNS_MARK and, as JSON, the database's size on disk, its
        filestore and every bytea column: {size, filestore: {path, bytes,
        files}, columns: [{schema, table, column, bytes, rows, not_null}]}
    binary_dump.py room <odoo config> <database>
        prints ROOM_MARK and, as JSON, where a restore of the database
        writes and what it drops first: {data_directory (None when
        PostgreSQL does not tell it), db_host, db_port, database_bytes (0:
        no such database), filestore: {path, bytes, files}, temp}
    binary_dump.py dump <odoo config> <database> <dest zip> <columns> <filestore 0|1>
        writes the zip Odoo restores (dump.sql, manifest.json, filestore/)
        with the columns emptied; columns: JSON [[schema, table, column]]
    binary_dump.py modules <odoo config> <database>
        reads on its standard input, as JSON, {module: version of its
        manifest}, and prints MODULES_MARK and, as JSON, {module: {state,
        installed, available, newer}}: the version in the database, the
        manifest's made canonical by Odoo's adapt_version, and whether the
        one on disk is newer (Odoo's parse_version, as its update_list)

pg_dump cannot leave a column out, so the dump is made of its sections on
one snapshot: the schema (pre-data), the data of every table but those that
hold an emptied column, the rows of those tables copied here with the
column empty (NULL, or '' where it is NOT NULL), then indexes and
constraints (post-data). dump.sql restores as pg_dump's own.

Odoo 10 runs on Python 2.7: this file is Python 2.7 and 3. It uses only
what Odoo 10 to 20 all have: tools.config.parse_config and filestore,
sql_db.connection_info_for, tools.misc.exec_pg_environ, release,
modules.module.adapt_version, tools.parse_version.parse_version.
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
ROOM_MARK = "OMNIACLI_RESTORE_ROOM="
MODULES_MARK = "OMNIACLI_MODULE_VERSIONS="
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


def folder_size(folder):
    """{path, bytes, files} of a folder's files (0 when it does not exist)."""
    size = files = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                size += os.lstat(os.path.join(root, name)).st_size
                files += 1
            except OSError:
                pass
    return {"path": folder, "bytes": size, "files": files}


def room(odoo, database):
    """Where a restore of database writes, and what it drops first. Read on
    the maintenance database postgres: the database may not exist."""
    import psycopg2

    config = odoo.tools.config
    found = {"data_directory": None, "db_host": config.get("db_host") or None,
             "db_port": int(config.get("db_port") or 5432), "database_bytes": 0,
             "filestore": folder_size(odoo.tools.config.filestore(database)),
             "temp": tempfile.gettempdir()}
    cnx = connect(odoo, "postgres")
    try:
        cr = cnx.cursor()
        cr.execute("SELECT pg_database_size(datname) FROM pg_database WHERE datname = %s", (database,))
        row = cr.fetchone()
        found["database_bytes"] = int(row[0]) if row else 0
        try:
            # superuser or pg_read_all_settings only
            cr.execute("SHOW data_directory")
            found["data_directory"] = cr.fetchone()[0]
        except psycopg2.Error:
            cnx.rollback()
    finally:
        cnx.close()
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


def module_versions(cr, odoo, manifests):
    """{module: {state, installed, available, newer}} of every module of
    manifests (its manifest's version, None: Odoo's default 1.0) or of the
    database. installed: latest_version of ir_module_module (None: not
    installed); available: the manifest's, canonical (None: not on disk)."""
    from odoo.modules.module import adapt_version
    from odoo.tools.parse_version import parse_version

    def canonical(version):
        try:
            return adapt_version(version or "1.0")
        except ValueError:  # Odoo 19 and later refuse a malformed one
            return version

    cr.execute("SELECT name, state, latest_version FROM ir_module_module")
    found = {}
    for name, state, installed in cr.fetchall():
        found[name] = {"state": state, "installed": installed if state != "uninstalled" else None,
                       "available": None, "newer": False}
    for name, version in manifests.items():
        row = found.setdefault(name, {"state": None, "installed": None, "available": None, "newer": False})
        row["available"] = canonical(version)
        if row["installed"]:
            row["newer"] = parse_version(row["available"]) > parse_version(row["installed"])
    return found


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
    import odoo.modules.module

    odoo.tools.config.parse_config(["-c", config])
    if mode == "room":
        print(ROOM_MARK + json.dumps(room(odoo, database)))
        return
    cnx = connect(odoo, database)
    try:
        if mode == "scan":
            cr = cnx.cursor()
            cr.execute("SELECT pg_database_size(current_database())")
            size = int(cr.fetchone()[0])
            print(COLUMNS_MARK + json.dumps({"size": size, "columns": scan(cr),
                                             "filestore": folder_size(odoo.tools.config.filestore(database))}))
            return
        if mode == "modules":
            manifests = json.loads(sys.stdin.read() or "{}")
            print(MODULES_MARK + json.dumps(module_versions(cnx.cursor(), odoo, manifests)))
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
