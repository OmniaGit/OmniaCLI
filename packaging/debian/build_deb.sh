#!/bin/bash
# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the OmniaCLI Debian package and put it on the share.
#
#   packaging/debian/build_deb.sh             test build: the branch with the
#                                             latest commit, matteo_dev_<year> or
#                                             <year>; the version is in the name
#   packaging/debian/build_deb.sh --release   release build: branch <year>, for
#                                             download, named OmniaCLI.deb
#   packaging/debian/build_deb.sh --check DIR development check: the working
#                                             tree, uncommitted files included,
#                                             version <year>.1.0~check, into DIR;
#                                             no tag, nothing on the share
#   packaging/debian/build_deb.sh --ci VERSION DIR
#                                             CI build (GitHub release): the
#                                             checked out commit (HEAD), with the
#                                             release's version <year>.1.<n>, into
#                                             DIR; no tag, nothing on the share
#
# The package is built from the branch's commit (git archive), never from the
# working tree. Its version is <year>.1.<n>, n one above every number the tags
# omniacli-v<year>.1.* and the packages on the share know, those of the
# package's former name omniaci included; the built commit is tagged
# omniacli-v<version> locally (pushing the tag is the operator's).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
YEAR="${YEAR:-$(date +%Y)}"
SERIES="$YEAR.1"
TAG_PREFIX="omniacli-v"
RELEASE_BRANCH="${RELEASE_BRANCH:-$YEAR}"
DEV_BRANCHES="${DEV_BRANCHES:-matteo_dev_$YEAR}"
# the build machine's own folders (the share, the builder's Python) are set
# in build.local.sh next to this script, not versioned; without it: dist/
# in the checkout and the python3 of the PATH
# shellcheck source=/dev/null
[ -f "$HERE/build.local.sh" ] && . "$HERE/build.local.sh"
ARTEFACTS="${ARTEFACTS:-$ROOT/dist}"
PYTHON="${PYTHON:-python3}"
# the package was named omniaci up to 2026.1.2: its build numbers are not reused
OLD_TAG_PREFIX="omniaci-v"
OLD_ARTEFACTS="$(dirname "$ARTEFACTS")/OmniaCI"

die() { echo "build_deb: $*" >&2; exit 1; }

release=""
check=""
ci=""
case "${1:-}" in
    --release) release=1 ;;
    --check) check="${2:?--check needs an output folder}" ;;
    --ci)
        ci="${2:?--ci needs the version and an output folder}"
        check="${3:?--ci needs an output folder}"
        [[ "$ci" =~ ^[0-9]{4}\.1\.[0-9]+$ ]] || die "--ci: version $ci is not <year>.1.<n>"
        ;;
    "") ;;
    *) die "unknown argument $1" ;;
esac

# The branch to build: RELEASE_BRANCH for a release, otherwise the one of
# DEV_BRANCHES and RELEASE_BRANCH that holds the latest commit.
pick_branch() {
    if [ -n "$release" ]; then
        git -C "$ROOT" rev-parse -q --verify "refs/heads/$RELEASE_BRANCH" >/dev/null \
            || die "OmniaCLI has no branch $RELEASE_BRANCH"
        echo "$RELEASE_BRANCH"
        return
    fi
    local best="" best_date=0 branch date
    for branch in $DEV_BRANCHES $RELEASE_BRANCH; do
        git -C "$ROOT" rev-parse -q --verify "refs/heads/$branch" >/dev/null || continue
        date="$(git -C "$ROOT" log -1 --format=%ct "refs/heads/$branch")"
        if [ "$date" -gt "$best_date" ]; then best="$branch"; best_date="$date"; fi
    done
    [ -n "$best" ] || die "OmniaCLI has none of the branches $DEV_BRANCHES $RELEASE_BRANCH"
    echo "$best"
}

# One above every build number the tags and the share know.
next_version() {
    local last=0 n
    for n in $(git -C "$ROOT" tag -l "$TAG_PREFIX$SERIES.*" "$OLD_TAG_PREFIX$SERIES.*" \
                 | sed "s/^\($TAG_PREFIX\|$OLD_TAG_PREFIX\)${SERIES//./\\.}\.//") \
             $(ls "$ARTEFACTS" "$OLD_ARTEFACTS" 2>/dev/null \
               | sed -n "s/^omniac\(li\|i\)_${SERIES//./\\.}\.\([0-9]*\)_all\.deb$/\2/p"); do
        [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -gt "$last" ] && last="$n"
    done
    echo "$SERIES.$((last + 1))"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SRC="$WORK/src"
PKG="$WORK/pkg"
mkdir -p "$SRC"
if [ -n "$ci" ]; then
    COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
    VERSION="$ci"
    echo "OmniaCLI ${COMMIT:0:8} -> omniacli $VERSION (CI build)"
    git -C "$ROOT" archive "$COMMIT" | tar -x -C "$SRC"
elif [ -n "$check" ]; then
    VERSION="$SERIES.0~check"
    echo "OmniaCLI working tree -> omniacli $VERSION (check build)"
    (cd "$ROOT" && git ls-files -z -co --exclude-standard | tar --null -T - -c) | tar -x -C "$SRC"
else
    BRANCH="$(pick_branch)"
    COMMIT="$(git -C "$ROOT" rev-parse "refs/heads/$BRANCH")"
    VERSION="$(next_version)"
    echo "OmniaCLI $BRANCH ${COMMIT:0:8} -> omniacli $VERSION ($([ -n "$release" ] && echo release || echo test) build)"
    git -C "$ROOT" archive "$COMMIT" | tar -x -C "$SRC"
fi
[ -d "$SRC/packaging/debian/requirements" ] \
    || die "the commit has no packaging/debian/requirements: run make_locks.sh and commit"

# the wheel carries the build's version
# (a Python version: "~check" is Debian syntax only)
sed -i "s/^version = \".*\"/version = \"${VERSION%~check}\"/" "$SRC/pyproject.toml"
"$PYTHON" -m pip wheel -q --disable-pip-version-check --no-deps -w "$WORK/dist" "$SRC" \
    || die "the wheel could not be built"

FILES="$SRC/packaging/debian/files"
install -d "$PKG/DEBIAN" "$PKG/opt/omniacli/dist" "$PKG/opt/omniacli/requirements" \
    "$PKG/usr/bin" "$PKG/usr/lib/systemd/system" "$PKG/usr/share/omniacli" \
    "$PKG/usr/share/doc/omniacli" "$PKG/etc/default" "$PKG/etc/sudoers.d"
install -m 644 "$WORK"/dist/omniacli-*.whl "$PKG/opt/omniacli/dist/"
install -m 644 "$SRC"/packaging/debian/requirements/py*.txt "$PKG/opt/omniacli/requirements/"
ln -s /opt/omniacli/venv/bin/omniacli "$PKG/usr/bin/omniacli"
install -m 644 "$FILES/omniacli.service" "$PKG/usr/lib/systemd/system/omniacli.service"
install -m 644 "$FILES/omniacli.yaml.in" "$PKG/usr/share/omniacli/omniacli.yaml.in"
install -m 644 "$FILES/default" "$PKG/etc/default/omniacli"
install -m 440 "$FILES/sudoers" "$PKG/etc/sudoers.d/omniacli"
install -m 644 "$SRC/README.md" "$SRC/servers.example.yaml" "$SRC/LICENSE" "$PKG/usr/share/doc/omniacli/"
printf 'OmniaCLI\nCopyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C\nLicense: AGPL-3.0-or-later (GNU Affero General Public License v3 or later),\nfull text in /usr/share/doc/omniacli/LICENSE\n' \
    > "$PKG/usr/share/doc/omniacli/copyright"
install -m 755 "$FILES/postinst" "$FILES/prerm" "$FILES/postrm" "$PKG/DEBIAN/"
printf '/etc/default/omniacli\n/etc/sudoers.d/omniacli\n' > "$PKG/DEBIAN/conffiles"
SIZE="$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$FILES/control.in" > "$PKG/DEBIAN/control"

DEB="$WORK/omniacli_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null || die "dpkg-deb failed"

if [ -n "$check" ]; then
    mkdir -p "$check"
    cp -f "$DEB" "$check/"
    echo "Package: $check/$(basename "$DEB")"
    exit 0
fi

mkdir -p "$ARTEFACTS"

# a release is the download, named without version so its link does not change
if [ -n "$release" ]; then
    published="$ARTEFACTS/OmniaCLI.deb"
else
    published="$ARTEFACTS/$(basename "$DEB")"
fi
cp -f "$DEB" "$published"
echo "Package: $published ($VERSION)"

git -C "$ROOT" tag "$TAG_PREFIX$VERSION" "$COMMIT" \
    && echo "tagged $TAG_PREFIX$VERSION on OmniaCLI $BRANCH ${COMMIT:0:8}"

# the share keeps this test package and the three before it
ls "$ARTEFACTS" | grep -E '^omniacli_[0-9.]+_all\.deb$' | sort -V \
    | grep -vx "$(basename "$DEB")" | head -n -3 | while read -r old; do
        rm -f "${ARTEFACTS:?}/${old:?}"
        echo "$old removed from the share"
    done
