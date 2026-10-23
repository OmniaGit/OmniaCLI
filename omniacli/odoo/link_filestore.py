# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Share an Odoo filestore through hard links: run by an instance's venv
python, as the user the instance runs as, so it imports nothing of OmniaCLI
and runs on the oldest Python of an Odoo venv (no f-strings).

    python link_filestore.py check|link SRC DST

check proves, before anything is touched, that the files of SRC can be hard
linked into DST by this user: SRC and DST's folder are on
the same filesystem, a file of SRC is readable and may be linked (the kernel's
fs.protected_hardlinks). link makes DST a tree of directories of its own
holding hard links to the files of SRC; DST must not exist (the restore
before it dropped the old one).

Odoo never rewrites a file of its filestore (it writes one only when its
path, the sha1 of the content, does not exist yet) and only unlinks one, so
the two databases share the files without touching each other's. The
checklist of SRC (its pending garbage collection) is not shared.
"""
import os
import sys


def files(src):
    """(folder, file names) of src, without its checklist."""
    for root, dirs, names in os.walk(src):
        if root == src:
            dirs[:] = [d for d in dirs if d != "checklist"]
        yield root, names


def check(src, dst):
    parent = os.path.dirname(dst)
    probe = parent
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    if os.stat(src).st_dev != os.stat(probe).st_dev:
        sys.exit("%s and %s are not on the same filesystem: no hard links between them" % (src, parent))
    sample = next((os.path.join(root, names[0]) for root, names in files(src) if names), None)
    if sample is None:
        return
    if not os.access(sample, os.R_OK):
        sys.exit("cannot read %s" % sample)
    os.makedirs(parent, exist_ok=True)
    probe_link = os.path.join(parent, ".omniacli-link-probe-%d" % os.getpid())
    try:
        os.link(sample, probe_link)
    except OSError as exc:
        sys.exit("cannot hard link %s into %s: %s" % (sample, parent, exc))
    os.unlink(probe_link)


def link(src, dst):
    count = 0
    for root, names in files(src):
        target = os.path.join(dst, os.path.relpath(root, src))
        os.makedirs(target, exist_ok=True)
        for name in names:
            os.link(os.path.join(root, name), os.path.join(target, name))
            count += 1
    print("%d files linked" % count)


def main(argv):
    if len(argv) != 4 or argv[1] not in ("check", "link"):
        sys.exit("usage: link_filestore.py check|link SRC DST")
    mode, src, dst = argv[1:]
    if not os.path.isdir(src):
        sys.exit("no filestore %s" % src)
    if mode == "check":
        check(src, dst)
    elif os.path.lexists(dst):
        sys.exit("the filestore %s already exists" % dst)
    else:
        link(src, dst)


if __name__ == "__main__":
    main(sys.argv)
