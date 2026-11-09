#!/bin/bash
# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install an OmniaCLI .deb in a container with a real systemd, for every target
# distribution, and check it end to end: the service starts, the generated
# admin password logs in, a server saved through the API is pulled and its
# (dummy) Odoo service restarted through sudo, then reinstall, remove, purge.
#
# An image named <image>+localpy also gets a working python3.12 and a broken
# python3.13 in /usr/local/bin: the venv must be built on the python3.12.
#
# Usage: packaging/debian/test_deb.sh <package.deb> [image ...]
set -euo pipefail

DEB="$(readlink -f "${1:?usage: test_deb.sh <package.deb> [image ...]}")"
shift
IMAGES=("$@")
[ ${#IMAGES[@]} -gt 0 ] || IMAGES=(debian:11 debian:11+localpy ubuntu:22.04 debian:12 ubuntu:24.04 debian:13)
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGS="$(mktemp -d)"

# The checks, run as root inside the container.
read -r -d '' CHECKS <<'EOS' || true
set -euo pipefail
step() { echo "--- $*"; }
fail() { echo "CHECK FAILED: $*"; exit 1; }
# a command's output has the pattern: read to its end, as grep -q does not
# (it stops at the first match; with pipefail, a curl still writing a long
# page then fails on the broken pipe, and the check with it)
has() { grep "$@" >/dev/null; }
# the login form, as a browser fills it: the session cookie in /tmp/cookies-<user>
login() {
    [ "$(curl -s -o /dev/null -w '%{http_code}' -c "/tmp/cookies-$1" -H "Origin: http://localhost:8090" \
          --data-urlencode "username=$1" --data-urlencode "password=$2" localhost:8090/login)" = 303 ] \
        || fail "login of $1"
}
export DEBIAN_FRONTEND=noninteractive

step "install"
apt-get install -y -qq /opt/omniacli-test/omniacli.deb > /tmp/install.log 2>&1 || { tail -30 /tmp/install.log; fail "apt-get install"; }
# the links through SSH: ssh, ssh-keygen and ssh-keyscan come with the package
for tool in ssh ssh-keygen ssh-keyscan; do command -v "$tool" >/dev/null || fail "$tool not installed with the package"; done
grep -E '^omniacli:' /tmp/install.log || true
dpkg -s omniacli | has '^Status: install ok installed' || fail "package not installed"
for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && break; sleep 1; done
systemctl is-active --quiet omniacli || { journalctl -u omniacli --no-pager | tail -20; fail "service not active"; }
systemctl is-enabled --quiet omniacli || fail "service not enabled"
[ "$(stat -c '%U:%G %a' /etc/omniacli/omniacli.yaml)" = "root:omniacli 640" ] || fail "config permissions"
[ "$(stat -c '%U %a' /etc/omniacli/initial-admin-password)" = "root 600" ] || fail "password file permissions"
visudo -cf /etc/sudoers.d/omniacli >/dev/null || fail "sudoers file invalid"
PW="$(cat /etc/omniacli/initial-admin-password)"
[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:8090/)" = 401 ] || fail "no 401 without login"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Origin: http://localhost:8090" -d "username=admin&password=wrong" localhost:8090/login)" = 401 ] \
    || fail "wrong password accepted"
login admin "$PW"
[ "$(curl -s -o /dev/null -w '%{http_code}' -b /tmp/cookies-admin localhost:8090/)" = 200 ] || fail "admin login"
[ "$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$PW" localhost:8090/)" = 401 ] || fail "HTTP Basic still accepted"
echo "python: $(/opt/omniacli/venv/bin/python -V), $(/opt/omniacli/venv/bin/pip freeze | grep -iE '^(fastapi|starlette)==' | tr '\n' ' ')"
VENV_HOME="$(sed -n 's/^home = //p' /opt/omniacli/venv/pyvenv.cfg)"
echo "venv built on $VENV_HOME"
[ "$VENV_HOME" = "$EXPECT_HOME" ] || fail "venv built on $VENV_HOME, expected $EXPECT_HOME"
/opt/omniacli/venv/bin/python -c 'import mcp' || fail "mcp not installed"
if [ "$EXPECT_HOME" = /opt/omniacli/python/bin ]; then
    # preinstalled by the image from the host's cache, with its sha256: postinst takes it as it is
    grep -q 'downloading Python' /tmp/install.log && fail "the bundled Python downloaded although it was there"
    [ -x /opt/omniacli/python/bin/python3 ] || fail "no bundled Python"
else
    [ ! -e /opt/omniacli/python ] || fail "/opt/omniacli/python created with a usable local Python"
fi

step "a server with a git repository and a dummy Odoo service"
cat > /etc/systemd/system/odoo-dummy.service <<'EOF'
[Service]
ExecStart=/bin/sleep infinity
EOF
systemctl daemon-reload && systemctl start odoo-dummy
SYSTEMCTL="$(command -v systemctl)"
echo "omniacli ALL=(root) NOPASSWD: $SYSTEMCTL --no-ask-password restart odoo-dummy" >> /etc/sudoers.d/omniacli
visudo -cf /etc/sudoers.d/omniacli >/dev/null || fail "sudoers file invalid after the edit"
# root plays the developer on repositories owned by omniacli (test only)
git config --global --add safe.directory '*'
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
git init -q --bare /srv/remote.git && git -C /srv/remote.git symbolic-ref HEAD refs/heads/main
git clone -q /srv/remote.git /srv/dev 2>/dev/null
git -C /srv/dev checkout -q -b main && git -C /srv/dev commit -q --allow-empty -m one
git -C /srv/dev push -q origin main
git clone -q -b main /srv/remote.git /srv/odoo && chown -R omniacli:omniacli /srv/odoo /srv/remote.git
mkdir -p /srv/venv/bin && printf '#!/bin/sh\necho "update $*"\n' > /srv/venv/bin/click-odoo-update
chmod 755 /srv/venv/bin/click-odoo-update
touch /srv/odoo.conf /srv/odoo.log
API=(-s -b /tmp/cookies-admin -H "Origin: http://localhost:8090" -H "Content-Type: application/json")
curl "${API[@]}" localhost:8090/api/servers/save -d '{"original": null, "name": "demo", "server": {"production": {
  "database": "DEMO", "odoo_config": "/srv/odoo.conf", "log_file": "/srv/odoo.log", "venv_bin": "/srv/venv/bin",
  "restart": {"type": "systemd", "service": "odoo-dummy", "sudo": true},
  "repositories": [{"name": "odoo", "path": "/srv/odoo", "branch": "main"}]}}}' | has '"ok":true' \
  || fail "server not saved"
[ "$(stat -c %U /var/lib/omniacli/servers.yaml)" = omniacli ] || fail "servers.yaml owner"
grep -q "status: draft" /var/lib/omniacli/servers.yaml || fail "a new server is not a draft"

step "a user created from the Users page, as user omniacli"
curl "${API[@]}" localhost:8090/api/access/users/save \
  -d '{"name": "anna", "role": "developer", "password": "anna-pass-1", "new": true}' | has '"ok":true' \
  || fail "user not saved"
[ "$(stat -c '%U %a' /var/lib/omniacli/users.yaml)" = "omniacli 600" ] || fail "users.yaml owner or mode"
login anna anna-pass-1
[ "$(curl -s -o /dev/null -w '%{http_code}' -b /tmp/cookies-anna localhost:8090/api/info)" = 200 ] || fail "new user login"

step "a link to itself: token from the Users page, link from the Links page, pages through the proxy"
LINK_TOKEN="$(curl "${API[@]}" localhost:8090/api/access/links/save -d '{"name": "self", "role": "developer", "new": true}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
curl "${API[@]}" localhost:8090/api/links/save \
  -d "{\"name\": \"me\", \"url\": \"http://127.0.0.1:8090\", \"token\": \"$LINK_TOKEN\", \"new\": true}" | has '"ok":true' \
  || fail "link not saved"
[ "$(stat -c '%U %a' /var/lib/omniacli/links.yaml)" = "omniacli 600" ] || fail "links.yaml owner or mode"
curl "${API[@]}" -X POST localhost:8090/api/links/me/test | has '"role":"developer"' || fail "Test link"
curl -s -b /tmp/cookies-admin -H "Accept: text/html" localhost:8090/links/me/ | has "Linked instance <strong>me</strong>" \
  || fail "page through the link"

step "Test & activate: the checks that can pass here"
curl "${API[@]}" -X POST localhost:8090/api/servers/demo/test > /tmp/checks.json
python3 - <<'PY' || fail "activation checks"
import json
data = json.load(open("/tmp/checks.json"))
checks = {(c["target"], c["name"]): c for c in data["checks"]}
for line in data["checks"]:
    print(("ok  " if line["ok"] else line["level"][:4]), line["target"], line["name"], "-", line["detail"])
must_pass = ["Odoo config", "Log file", "click-odoo-update", "Repository odoo", "Service", "Restart permission"]
bad = [n for n in must_pass if not checks[("production", n)]["ok"]]
# no Odoo here: these must fail, so the server must stay a draft
assert not checks[("production", "Database")]["ok"] and data["status"] == "draft", data["status"]
assert not bad, bad
PY
# no Odoo in the container: activate it by hand to test the actions
sed -i 's/status: draft/status: active/' /var/lib/omniacli/servers.yaml
systemctl restart omniacli
for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && break; sleep 1; done
# links.yaml read again at startup: the link has its row in Linked, and its
# servers come through it (they are on its own Dashboard, not in Servers)
curl -s -b /tmp/cookies-admin localhost:8090/ | has '<span class="machine-row" data-link="me">' || fail "linked row"
curl -s -b /tmp/cookies-admin localhost:8090/links/me/api/servers | has '"demo"' || fail "linked servers"

step "git pull + restart through the web service"
git -C /srv/dev commit -q --allow-empty -m two && git -C /srv/dev push -q origin main
PID_BEFORE="$(systemctl show -p MainPID --value odoo-dummy)"
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/demo/production/pull-restart)"
JOB="${LOCATION##*/}"
for i in $(seq 60); do
    STATUS="$(curl -s -b /tmp/cookies-admin "localhost:8090/api/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    case "$STATUS" in success|failed|interrupted) break ;; esac
    sleep 1
done
if [ "$STATUS" != success ]; then
    curl -s -b /tmp/cookies-admin "localhost:8090/api/jobs/$JOB" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("\n".join(d["lines"]))'
    fail "pull-restart job $STATUS"
fi
[ "$(git -C /srv/odoo log -1 --format=%s)" = two ] || fail "repository not pulled"
PID_AFTER="$(systemctl show -p MainPID --value odoo-dummy)"
[ "$PID_AFTER" != "$PID_BEFORE" ] && [ "$PID_AFTER" != 0 ] || fail "odoo-dummy not restarted"
echo "pulled, odoo-dummy restarted ($PID_BEFORE -> $PID_AFTER)"

step "a user of role user stops, starts and restarts the systemd service"
echo "omniacli ALL=(root) NOPASSWD: $SYSTEMCTL --no-ask-password start odoo-dummy" >> /etc/sudoers.d/omniacli
echo "omniacli ALL=(root) NOPASSWD: $SYSTEMCTL --no-ask-password stop odoo-dummy" >> /etc/sudoers.d/omniacli
visudo -cf /etc/sudoers.d/omniacli >/dev/null || fail "sudoers invalid with start and stop"
curl "${API[@]}" localhost:8090/api/access/users/save \
  -d '{"name": "luca", "role": "user", "password": "luca-pass-1", "new": true}' | has '"ok":true' || fail "user luca not saved"
login luca luca-pass-1
LUCA=(-s -b /tmp/cookies-luca -H "Origin: http://localhost:8090")
curl -s -b /tmp/cookies-luca localhost:8090/servers/demo | has "service active" || fail "service state not shown"
for action in stop start restart; do
    PID_BEFORE="$(systemctl show -p MainPID --value odoo-dummy)"
    LOCATION="$(curl "${LUCA[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/demo/production/$action)"
    [ -n "$LOCATION" ] || fail "$action refused to role user"
    JOB="${LOCATION##*/}"
    for i in $(seq 60); do
        STATUS="$(curl -s -b /tmp/cookies-admin "localhost:8090/api/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
        case "$STATUS" in success|failed|interrupted) break ;; esac
        sleep 1
    done
    [ "$STATUS" = success ] || { curl -s -b /tmp/cookies-admin "localhost:8090/api/jobs/$JOB"; fail "$action job $STATUS"; }
    STATE="$(systemctl is-active odoo-dummy || true)"
    echo "$action by luca: odoo-dummy $STATE (pid $PID_BEFORE -> $(systemctl show -p MainPID --value odoo-dummy))"
    case "$action" in
        stop) [ "$STATE" = inactive ] || fail "not stopped"
              curl -s -b /tmp/cookies-luca localhost:8090/servers/demo | has "/servers/demo/production/start" || fail "no Start button when stopped" ;;
        start) [ "$STATE" = active ] || fail "not started" ;;
        restart) [ "$STATE" = active ] && [ "$(systemctl show -p MainPID --value odoo-dummy)" != "$PID_BEFORE" ] || fail "not restarted" ;;
    esac
done
[ "$(curl "${LUCA[@]}" -o /dev/null -w '%{http_code}' -X POST localhost:8090/servers/demo/production/pull)" = 403 ] \
  || fail "role user may pull"

step "load metrics of the systemd unit, read as user omniacli"
sleep 12
curl -s -b /tmp/cookies-admin "localhost:8090/api/metrics?server=demo" > /tmp/metrics.json
python3 - <<'PY' || fail "metrics"
import json
info = json.load(open("/tmp/metrics.json"))["instances"]["demo/production"]
print(info["source"], info["points"][-1])
assert info["source"] == "systemd unit odoo-dummy", info["source"]
assert info["points"][-1]["procs"] == 1 and info["points"][-1]["mem"] > 0
PY

step "run_as: the venv's programs as user odoo, its database through peer"
apt-get install -y -qq postgresql python3-psycopg2 > /tmp/pg.log 2>&1 || { tail -20 /tmp/pg.log; fail "postgresql install"; }
for i in $(seq 30); do su postgres -c 'psql -qAtc "select 1"' >/dev/null 2>&1 && break; sleep 1; done
adduser --quiet --system --group --home /var/lib/odoo odoo
su postgres -c "createuser odoo && createdb -O odoo DEMO_RUNAS"
sudo -u odoo psql -q -d DEMO_RUNAS -c "CREATE TABLE ir_module_module (name varchar, latest_version varchar);
  INSERT INTO ir_module_module VALUES ('base', '18.0.1.0');" || fail "database of user odoo"
# an Odoo source folder: odoo-bin, an importable odoo, a requirements.txt
mkdir -p /srv/odoo-src/odoo
touch /srv/odoo-src/odoo-bin
echo 'from . import release' > /srv/odoo-src/odoo/__init__.py
echo 'version = "18.0"' > /srv/odoo-src/odoo/release.py
echo six > /srv/odoo-src/requirements.txt
printf '[options]\n' > /srv/runas.conf
# the venv belongs to odoo; its click-odoo scripts tell who runs them
install -d -o odoo -g odoo /srv/runas-venv
sudo -u odoo python3 -m venv --system-site-packages /srv/runas-venv
SITE="$(sudo -u odoo /srv/runas-venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo 'print("click_odoo stub")' | sudo -u odoo tee "$SITE/click_odoo.py" >/dev/null
sudo -u odoo tee /srv/runas-venv/bin/click-odoo-update >/dev/null <<'EOF'
#!/bin/sh
echo "update as $(id -un), PYTHONPATH=$PYTHONPATH, HOME=$HOME, cwd=$(pwd)"
EOF
sudo -u odoo tee /srv/runas-venv/bin/click-odoo-backupdb >/dev/null <<'EOF'
#!/bin/sh
for last; do :; done
echo "backup as $(id -un) to $last"
echo dump > "$last"
EOF
chmod 755 /srv/runas-venv/bin/click-odoo-update /srv/runas-venv/bin/click-odoo-backupdb
curl "${API[@]}" localhost:8090/api/servers/save -d '{"original": null, "name": "runas", "server": {"test": {
  "database": "DEMO_RUNAS", "odoo_config": "/srv/runas.conf", "log_file": "/srv/odoo.log", "venv_bin": "/srv/runas-venv/bin",
  "odoo_bin": "/srv/odoo-src/odoo-bin", "run_as": "odoo",
  "restart": {"type": "systemd", "service": "odoo-dummy", "sudo": true},
  "repositories": [{"name": "odoo", "path": "/srv/odoo", "branch": "main"}]}}}' | has '"ok":true' \
  || fail "run_as server not saved"
runas_checks() {
    curl "${API[@]}" -X POST localhost:8090/api/servers/runas/test > /tmp/runas.json
    python3 - "$@" <<'PY'
import json, sys
data = json.load(open("/tmp/runas.json"))
checks = {c["name"]: c for c in data["checks"] if c["target"] == "test"}
for name in ("Run as", "Odoo importable by click-odoo", "Database"):
    print(("ok  " if checks[name]["ok"] else "erro"), name, "-", checks[name]["detail"])
if sys.argv[1:] == ["first"]:
    assert not checks["Run as"]["ok"], "Run as passed without a sudoers line"
    print(checks["Run as"]["detail"].split("add to /etc/sudoers.d/omniacli: ", 1)[1], file=open("/tmp/line", "w"))
else:
    bad = [n for n, c in checks.items() if not c["ok"] and c["level"] == "error"]
    assert not bad, bad
    assert checks["Start/Stop permission"]["ok"], checks["Start/Stop permission"]["detail"]
    assert data["status"] == "active", data["status"]
PY
}
runas_checks first || fail "run_as checks without sudoers"
# the line the check asks for, as is
cat /tmp/line >> /etc/sudoers.d/omniacli
visudo -cf /etc/sudoers.d/omniacli >/dev/null || fail "sudoers invalid with the run_as line"
runas_checks || fail "run_as checks with the sudoers line"
wait_job() {
    local job="$1" status
    for i in $(seq 120); do
        status="$(curl -s -b /tmp/cookies-admin "localhost:8090/api/jobs/$job" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
        case "$status" in success|failed|interrupted) break ;; esac
        sleep 1
    done
    curl -s -b /tmp/cookies-admin "localhost:8090/api/jobs/$job" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["lines"]))' > /tmp/job.log
    [ "$status" = success ] || { cat /tmp/job.log; return 1; }
}
JOB="$(curl "${API[@]}" localhost:8090/servers/runas/test/x/odoo/update -d '{"mode": "changed"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["job"])')"
wait_job "$JOB" || fail "update as odoo"
grep -q "update as odoo, PYTHONPATH=/srv/odoo-src" /tmp/job.log || { cat /tmp/job.log; fail "update not run as odoo with PYTHONPATH"; }
# /var/lib/omniacli (mode 750) is closed to odoo: its programs run in /
grep -q "update as odoo, .*, cwd=/$" /tmp/job.log || { cat /tmp/job.log; fail "update as odoo not run in /"; }
grep -o 'update as .*' /tmp/job.log
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/runas/test/backup)"
wait_job "${LOCATION##*/}" || fail "backup as odoo"
grep -o 'backup as .*' /tmp/job.log
BACKUP="$(ls /var/lib/omniacli/backups/runas/test/)"
[ "$(cat "/var/lib/omniacli/backups/runas/test/$BACKUP")" = dump ] || fail "backup file"
ls /var/tmp | has omniacli-backup && fail "backup staging folder left in /var/tmp"
[ "$(curl -s -b /tmp/cookies-admin "localhost:8090/backups/runas/test/$BACKUP")" = dump ] || fail "backup made as odoo not downloadable"
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/runas/test/x/odoo/pip-requirements)"
wait_job "${LOCATION##*/}" || fail "pip requirements as odoo"
[ "$(stat -c %U "$SITE/six.py")" = odoo ] || fail "six not installed by odoo"
echo "update, backup ($(stat -c '%U %a' "/var/lib/omniacli/backups/runas/test/$BACKUP")) and pip ran as odoo"
# purge keeps the backups, and only them: this one stays for the purge below

step "a server installed from a signed package: Test, update, nothing new, an unsigned one refused"
# demo-server: a conffile edited here (as API_SERVER.py is) and a postinst
mk_deb() {
    local d="/tmp/demo-$1"
    rm -rf "$d" && mkdir -p "$d/DEBIAN" "$d/etc/demo"
    printf 'Package: demo-server\nVersion: %s\nArchitecture: all\nMaintainer: t <t@t>\nDescription: demo\n' "$1" > "$d/DEBIAN/control"
    echo /etc/demo/demo.conf > "$d/DEBIAN/conffiles"
    echo "conf $1" > "$d/etc/demo/demo.conf"
    dpkg-deb -b --root-owner-group "$d" /srv/www/demo-server.deb >/dev/null
}
mkdir -p /srv/www /srv/sign
mk_deb 1.0 && dpkg -i /srv/www/demo-server.deb >/dev/null && echo "edited here" >> /etc/demo/demo.conf
mk_deb 2.0
export OMNIACLI_KEY_PASSPHRASE=test-passphrase
PUB="$(/opt/omniacli/venv/bin/omniacli package-key /srv/sign/key 2>/dev/null)" || fail "package-key"
/opt/omniacli/venv/bin/omniacli package-sign --key /srv/sign/key /srv/www/demo-server.deb >/dev/null || fail "package-sign"
(cd /srv/www && sha256sum -c demo-server.deb.sha256 >/dev/null) || fail "the .sha256 is not what sha256sum checks"
/opt/omniacli/venv/bin/omniacli package-verify --public-key "$PUB" /srv/www/demo-server.deb | has "version 2.0" \
  || fail "package-verify"
(cd /srv/www && exec python3 -m http.server 8099 --bind 127.0.0.1 >/tmp/www.log 2>&1) &
WWW_PID=$!
for i in $(seq 30); do curl -s -o /dev/null http://127.0.0.1:8099/ && break; sleep 1; done
printf '[Service]\nExecStart=/bin/sleep infinity\n' > /etc/systemd/system/demo-pkg.service
systemctl daemon-reload && systemctl start demo-pkg
echo "omniacli ALL=(root) NOPASSWD: $SYSTEMCTL --no-ask-password restart demo-pkg" >> /etc/sudoers.d/omniacli
curl "${API[@]}" localhost:8090/api/servers/save -d "{\"original\": null, \"name\": \"pkg\", \"server\": {\"type\": \"generic\",
  \"production\": {\"log_file\": \"/srv/odoo.log\", \"restart\": {\"type\": \"systemd\", \"service\": \"demo-pkg\", \"sudo\": true},
  \"repositories\": [], \"package\": {\"url\": \"http://127.0.0.1:8099/demo-server.deb\", \"name\": \"demo-server\",
  \"public_key\": \"$PUB\"}}}}" | has '"ok":true' || fail "package server not saved"
# without its sudoers line the Test says which one: added as it says
curl "${API[@]}" -X POST localhost:8090/api/servers/pkg/test > /tmp/checks.json
python3 - <<'PY' > /tmp/sudoers-line || fail "package checks"
import json
data = json.load(open("/tmp/checks.json"))
checks = {c["name"]: c for c in data["checks"]}
for name in ("Package", "Package tools", "Package folder", "Installed package"):
    assert checks[name]["ok"], (name, checks[name]["detail"])
perm = checks["Package install permission"]
assert not perm["ok"] and data["status"] == "draft", perm
print(perm["detail"].split("/etc/sudoers.d/omniacli: ", 1)[1])
PY
cat /tmp/sudoers-line >> /etc/sudoers.d/omniacli
visudo -cf /etc/sudoers.d/omniacli >/dev/null || fail "sudoers invalid with the line the Test printed"
curl "${API[@]}" -X POST localhost:8090/api/servers/pkg/test | has '"status":"active"' || fail "package server not activated"
PID_BEFORE="$(systemctl show -p MainPID --value demo-pkg)"
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/pkg/production/pull-restart)"
wait_job "${LOCATION##*/}" || fail "package update job"
[ "$(dpkg-query -W -f='${Version}' demo-server)" = 2.0 ] || fail "demo-server 2.0 not installed"
grep -q "edited here" /etc/demo/demo.conf || fail "the conffile edited here was replaced"
[ "$(systemctl show -p MainPID --value demo-pkg)" != "$PID_BEFORE" ] || fail "demo-pkg not restarted"
[ ! -e /var/lib/omniacli/packages/pkg_production.deb ] || fail "the download was kept"
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/pkg/production/pull)"
{ wait_job "${LOCATION##*/}" && grep -q "already installed" /tmp/job.log; } || fail "nothing new not seen"
grep -q "published 2.0 (signed" /tmp/job.log || fail "the signed version not read from the manifest"
# whoever writes on the download server, without the private key: a new
# .deb and its manifest, the old signature
mk_deb 3.0
python3 - <<'PY'
import hashlib, json, os
deb = "/srv/www/demo-server.deb"
data = json.load(open(deb + ".json"))
data.update(version="3.0", size=os.path.getsize(deb), sha256=hashlib.sha256(open(deb, "rb").read()).hexdigest())
open(deb + ".json", "w").write(json.dumps(data, indent=2) + "\n")
PY
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/pkg/production/pull)"
wait_job "${LOCATION##*/}" && fail "an unsigned manifest was accepted"
grep -q "does not verify" /tmp/job.log || fail "no reason for the refused manifest"
[ "$(dpkg-query -W -f='${Version}' demo-server)" = 2.0 ] || fail "the unsigned .deb was installed"
kill "$WWW_PID" 2>/dev/null || true
echo "demo-server 2.0 installed, conffile kept, restarted; an unsigned 3.0 refused"

step "OMNIACLI_USER: the service as a workstation user, kept by an upgrade, then omniacli again"
wait_up() { for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && return 0; sleep 1; done; return 1; }
service_user() { stat -c %U "/proc/$(systemctl show -p MainPID --value omniacli)"; }
owners() { stat -c '%U:%G' /var/lib/omniacli /var/lib/omniacli/jobs /var/lib/omniacli/servers.yaml | sort -u; }
DROPIN=/etc/systemd/system/omniacli.service.d/omniacli-user.conf
# a workspace closed to other users (744), as on a development workstation
adduser --quiet --disabled-password --gecos "" dev
install -d -o dev -g dev -m 744 /home/dev/workspace
printf '[options]\ndb_name = DEV\n' > /home/dev/workspace/odoo.conf
chown dev:dev /home/dev/workspace/odoo.conf
FORM='{"values": {"odoo_config": "/home/dev/workspace/odoo.conf"}}'
curl "${API[@]}" localhost:8090/x/odoo/form/config -d "$FORM" | has "Permission denied" \
  || fail "the workspace of dev readable by omniacli: the test proves nothing"
echo "OMNIACLI_USER=nobody-here" >> /etc/default/omniacli
dpkg-reconfigure omniacli > /tmp/reconf.log 2>&1 && fail "an unknown OMNIACLI_USER accepted"
grep -q "no such user" /tmp/reconf.log || { cat /tmp/reconf.log; fail "no reason for the unknown user"; }
sed -i 's/^OMNIACLI_USER=.*/OMNIACLI_USER=dev/' /etc/default/omniacli
dpkg-reconfigure omniacli > /tmp/reconf.log 2>&1 || { tail -20 /tmp/reconf.log; fail "dpkg-reconfigure with OMNIACLI_USER=dev"; }
wait_up || fail "service not up as dev"
[ "$(service_user)" = dev ] || fail "service runs as $(service_user), not dev"
[ "$(owners)" = dev:omniacli ] || fail "data owned by $(owners), not dev:omniacli"
login admin "$PW"
curl "${API[@]}" localhost:8090/x/odoo/form/config -d "$FORM" | has '"database":"DEV"' || fail "config of dev not read as dev"
# git and the restart as dev: the repository is the developer's, sudoers names dev
chown -R dev:dev /srv/odoo /srv/remote.git
echo "dev ALL=(root) NOPASSWD: $SYSTEMCTL --no-ask-password restart odoo-dummy" >> /etc/sudoers.d/omniacli
visudo -cf /etc/sudoers.d/omniacli >/dev/null || fail "sudoers invalid with the line of dev"
git -C /srv/dev commit -q --allow-empty -m three && git -C /srv/dev push -q origin main
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/demo/production/pull-restart)"
wait_job "${LOCATION##*/}" || fail "pull-restart as dev"
[ "$(git -C /srv/odoo log -1 --format=%s)" = three ] || fail "repository not pulled as dev"
[ "$(stat -c %U /srv/odoo/.git/FETCH_HEAD)" = dev ] || fail "git did not run as dev"
dpkg -i /opt/omniacli-test/omniacli.deb > /tmp/reinstall.log 2>&1 || { tail -20 /tmp/reinstall.log; fail "upgrade as dev"; }
wait_up || fail "service not up after the upgrade"
[ "$(service_user)" = dev ] || fail "the upgrade gave the service back to $(service_user)"
[ "$(owners)" = dev:omniacli ] || fail "the upgrade gave the data back to $(owners)"
login admin "$PW"
curl "${API[@]}" localhost:8090/api/access/users/save \
  -d '{"name": "marta", "role": "user", "password": "marta-pass-1", "new": true}' | has '"ok":true' \
  || fail "users.yaml not written as dev after the upgrade"
# OMNIACLI_USER reaches the running service: a development machine, the shell page opens
curl -s -b /tmp/cookies-admin localhost:8090/options | has 'id="machine">development<' || fail "OMNIACLI_USER=dev is not a development machine"
[ "$(curl -s -o /dev/null -w '%{http_code}' -b /tmp/cookies-admin localhost:8090/servers/demo/production/shell)" = 200 ] \
    || fail "no shell page on a development machine"
echo "as dev: config read, pulled and restarted, upgrade kept it"
sed -i '/^OMNIACLI_USER=/d' /etc/default/omniacli
dpkg-reconfigure omniacli > /tmp/reconf.log 2>&1 || { tail -20 /tmp/reconf.log; fail "dpkg-reconfigure back to omniacli"; }
wait_up || fail "service not up as omniacli again"
[ "$(service_user)" = omniacli ] || fail "service runs as $(service_user), not omniacli again"
[ "$(owners)" = omniacli:omniacli ] || fail "data owned by $(owners), not omniacli again"
[ ! -e "$DROPIN" ] || fail "drop-in left without OMNIACLI_USER"
# without it, a server: the shell page refused, also to an admin
login admin "$PW"
curl -s -b /tmp/cookies-admin localhost:8090/options | has 'id="machine">server<' || fail "without OMNIACLI_USER not a server"
[ "$(curl -s -o /dev/null -w '%{http_code}' -b /tmp/cookies-admin localhost:8090/servers/demo/production/shell)" = 403 ] \
    || fail "the shell page opens on a server"
chown -R omniacli:omniacli /srv/odoo /srv/remote.git
login admin "$PW"

step "OMNIACLI_WORKSPACES: the storages bound on root's paths, their sudoers lines, the room of their disks"
WS_DEFAULT=/srv/omniacli/default_workspace
WS_SUDOERS=/etc/sudoers.d/omniacli-workspaces
HELPER=/opt/omniacli/venv/bin/omniacli
bound_of() { /opt/omniacli/venv/bin/python -c 'import sys; from omniacli.config import bound_root; print(bound_root(sys.argv[1]))' "$1"; }
[ ! -e "$WS_DEFAULT" ] || fail "$WS_DEFAULT made without OMNIACLI_WORKSPACES"
# a second storage on a user's disk: the folders above it are the user's
WS_USER=/srv/userdisk/mine/workspaces
install -d -o dev -g dev -m 777 /srv/userdisk
install -d -o dev -g dev -m 755 /srv/userdisk/mine
install -d -o root -g root -m 755 "$WS_USER"
echo real > "$WS_USER/marker"
printf 'workspaces:\n  storage:\n    default: %s\n    mine: %s\n' "$WS_DEFAULT" "$WS_USER" >> /etc/omniacli/omniacli.yaml
echo OMNIACLI_WORKSPACES=1 >> /etc/default/omniacli
dpkg-reconfigure omniacli > /tmp/reconf.log 2>&1 || { tail -20 /tmp/reconf.log; fail "dpkg-reconfigure with OMNIACLI_WORKSPACES=1"; }
wait_up || fail "service not up with OMNIACLI_WORKSPACES=1"
grep 'storage ' /tmp/reconf.log || true
[ "$(stat -c '%U:%G %a' "$WS_DEFAULT")" = "root:root 755" ] || fail "$WS_DEFAULT is $(stat -c '%U:%G %a' "$WS_DEFAULT")"
B_DEFAULT=$(bound_of default); B_USER=$(bound_of mine)
[ "$B_USER" = /var/lib/omniacli-storage/mine ] || fail "the storage mine is bound on $B_USER, not on its name"
for b in "$B_DEFAULT" "$B_USER"; do
    findmnt -n "$b" >/dev/null || { tail -20 /tmp/reconf.log; fail "$b not bound by the package"; }
    grep -qx "omniacli ALL=(root) NOPASSWD: /opt/omniacli/venv/bin/omniacli compose-helper $b/\* \*" "$WS_SUDOERS" \
        || { cat "$WS_SUDOERS"; fail "no compose-helper line for $b"; }
    systemctl is-active --quiet "omniacli-storage-$(basename "$b").service" || fail "the unit of $b is not active"
    systemctl is-enabled --quiet "omniacli-storage-$(basename "$b").service" || fail "the unit of $b is not enabled"
done
[ "$(cat "$B_USER/marker")" = real ] || fail "$B_USER does not show $WS_USER"
grep -q "compose-helper $WS_USER/" "$WS_SUDOERS" && fail "a compose-helper line names the storage itself, not its root path"
# the room of the disks, through the page's API: sudo, the root helper's space
login admin "$PW"
curl -s -b /tmp/cookies-admin localhost:8090/api/workspaces/space > /tmp/space.json
/opt/omniacli/venv/bin/python -c 'import json, sys
room = json.load(open("/tmp/space.json"))["roots"][sys.argv[1]][""]
assert room["free"] > 0 and room["physical"] == room["free"] and room["promised"] == 0, room' default \
    || { cat /tmp/space.json; fail "the room of $WS_DEFAULT"; }
curl -s -b /tmp/cookies-admin localhost:8090/workspaces | has "<option value=\"mine\">mine ($WS_USER)</option>" \
    || fail "the storage mine ($WS_USER) not offered"
# a workspace's disk on the user's disk, through the root path; after the storage's check.
# A container's /dev is made at its start: loop devices the kernel adds later
# (several of these tests at once, the host's own) get their nodes here
for i in $(seq 0 255); do [ -e "/dev/loop$i" ] || mknod "/dev/loop$i" b 7 "$i" 2>/dev/null || true; done
"$HELPER" workspace-helper disk w1 mine 1 > /tmp/disk.json 2>&1 || { cat /tmp/disk.json
    journalctl -u "$(systemd-escape -p --suffix=mount "$B_USER/w1")" --no-pager -o cat | tail -8
    losetup -a; ls /dev/loop* | tr '\n' ' '; fail "the disk of w1"; }
findmnt -n "$B_USER/w1" >/dev/null || fail "the disk of w1 not mounted on $B_USER/w1"
grep -q "Requires=omniacli-storage-$(basename "$B_USER").service" /etc/systemd/system/*w1.mount || fail "the disk of w1 does not wait for its storage's check"
# the user, everything running, renames a folder above the storage and plants its own
su dev -s /bin/sh -c "mv /srv/userdisk/mine /srv/userdisk/moved && mkdir -p $WS_USER && echo planted > $WS_USER/marker" \
    || fail "dev could not rename its own folders"
[ "$(cat "$B_USER/marker")" = real ] || fail "the root path followed the rename"
findmnt -n "$B_USER/w1" >/dev/null || fail "the disk of w1 lost by the rename"
mv "$B_USER" "$B_USER.x" 2>/dev/null && fail "the root path was moved"
# a boot: the units again; the path now holds the planted folder, the storage's unit refuses it
W1_UNIT=$(systemd-escape -p --suffix=mount "$B_USER/w1")
systemctl stop "$W1_UNIT" omniacli-storage-mine.service
findmnt -n "$B_USER" >/dev/null && fail "the storage's unit stopped leaves $B_USER bound"
systemctl start "$W1_UNIT" 2>/dev/null && fail "the disk of w1 mounted on a planted storage"
findmnt -n "$B_USER" >/dev/null && fail "the planted folder stays bound on $B_USER"
journalctl -u "omniacli-storage-$(basename "$B_USER").service" --no-pager -o cat | has -E "not the folder set up|root's alone" \
    || fail "the check did not say why"
touch "$B_USER/x" 2>/dev/null && fail "something was written beneath $B_USER (not immutable)"
"$HELPER" workspace-helper disk w1 mine 1 > /tmp/disk.json 2>&1 && fail "an action took the planted storage"
grep -qE "not the folder set up|root's alone" /tmp/disk.json || { cat /tmp/disk.json; fail "the action did not say why"; }
# taken again on purpose: the planted folder is the user's, refused
"$HELPER" workspace-helper storage-reset mine > /tmp/reset.json 2>&1 && fail "storage-reset took a folder of the user's"
grep -q "root's alone" /tmp/reset.json || { cat /tmp/reset.json; fail "storage-reset did not say why"; }
# the real one back: bound and checked again, the disk mounts
su dev -s /bin/sh -c "rm -rf /srv/userdisk/mine && mv /srv/userdisk/moved /srv/userdisk/mine"
systemctl start "$W1_UNIT" || { journalctl -u "omniacli-storage-$(basename "$B_USER").service" --no-pager | tail -5; fail "the real storage back is not taken"; }
[ "$(cat "$B_USER/marker")" = real ] && findmnt -n "$B_USER/w1" >/dev/null || fail "the real storage and w1 not back"
# a new disk: the workspaces stopped, the folder copied (the disks kept sparse),
# only the folder changed in the main configuration, a boot: the storage is
# bound from its new folder, its name and all that uses it as they were
WS_NEW=/srv/newdisk/omniacli
install -d -o root -g root -m 755 /srv/newdisk "$WS_NEW"
systemctl stop "$W1_UNIT"
cp -a --sparse=always "$WS_USER/." "$WS_NEW/"
echo new > "$WS_NEW/marker"
chattr -i "$WS_NEW/w1" 2>/dev/null; rmdir "$WS_NEW/w1" 2>/dev/null; mkdir -p "$WS_NEW/w1"
sed -i "s#    mine: $WS_USER#    mine: $WS_NEW#" /etc/omniacli/omniacli.yaml
grep -q "mine: $WS_NEW" /etc/omniacli/omniacli.yaml || fail "the storage's folder not changed in the configuration"
systemctl stop omniacli-storage-mine.service
systemctl start "$W1_UNIT" || { journalctl -u omniacli-storage-mine.service --no-pager | tail -5; fail "the storage on its new disk is not taken"; }
[ "$(cat "$B_USER/marker")" = new ] || fail "$B_USER does not show the new folder $WS_NEW"
findmnt -n "$B_USER/w1" >/dev/null || fail "the disk of w1 not mounted from the new disk"
grep -qx "$WS_NEW" "/var/lib/omniacli-storage/.ids/mine.id" || fail "the new folder of mine not kept"
# the helper finds it there (space, not disk: a disk mounted again here waits for
# its loop device, which no udev of a container announces)
"$HELPER" workspace-helper space > /tmp/space2.json 2>&1 || { cat /tmp/space2.json; fail "the helper on the storage on its new disk"; }
/opt/omniacli/venv/bin/python -c 'import json, sys
root = next(r for r in json.loads(open("/tmp/space2.json").read().splitlines()[-1])["roots"] if r["root"] == "mine")
assert root["folder"] == sys.argv[1] and "w1" in root["images"], root' "$WS_NEW" \
    || { cat /tmp/space2.json; fail "the storage mine not on its new disk for the helper"; }
echo "storages bound: $B_DEFAULT, $B_USER; a planted folder refused at a boot; a new disk taken at a boot"
sed -i '/^OMNIACLI_WORKSPACES=/d' /etc/default/omniacli
dpkg-reconfigure omniacli > /tmp/reconf.log 2>&1 || { tail -20 /tmp/reconf.log; fail "dpkg-reconfigure without OMNIACLI_WORKSPACES"; }
wait_up || fail "service not up without OMNIACLI_WORKSPACES"
[ ! -e "$WS_SUDOERS" ] || fail "$WS_SUDOERS left without OMNIACLI_WORKSPACES"
[ -d "$WS_DEFAULT" ] || fail "$WS_DEFAULT removed: the workspaces' disks live there"
login admin "$PW"

step "reinstall keeps config, servers and password"
HASH_BEFORE="$(sha256sum /etc/omniacli/omniacli.yaml)"
dpkg -i /opt/omniacli-test/omniacli.deb > /tmp/reinstall.log 2>&1 || { tail -20 /tmp/reinstall.log; fail "reinstall"; }
grep -q 'downloading Python' /tmp/reinstall.log && fail "bundled Python downloaded again on reinstall"
for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && break; sleep 1; done
systemctl is-active --quiet omniacli || fail "service not active after reinstall"
[ "$(sha256sum /etc/omniacli/omniacli.yaml)" = "$HASH_BEFORE" ] || fail "config changed by reinstall"
curl -s -b /tmp/cookies-admin localhost:8090/ | has demo || fail "server lost after reinstall (or its session)"

step "remove and purge"
apt-get remove -y -qq omniacli >/dev/null 2>&1 || fail "remove"
systemctl is-active --quiet omniacli && fail "service still running after remove"
[ ! -e /opt/omniacli/venv ] || fail "venv left after remove"
[ ! -e /opt/omniacli/python ] || fail "bundled Python left after remove"
[ -e /etc/omniacli/omniacli.yaml ] || fail "config removed by remove"
# what the service and the steps above wrote besides the backups
echo "before purge: $(ls -A /var/lib/omniacli | tr '\n' ' ')"
apt-get purge -y -qq omniacli >/dev/null 2>&1 || fail "purge"
[ ! -e /etc/omniacli ] || fail "/etc/omniacli left after purge"
[ "$(ls -A /var/lib/omniacli)" = backups ] || fail "left after purge besides the backups: $(ls -A /var/lib/omniacli | tr '\n' ' ')"
[ "$(cat "/var/lib/omniacli/backups/runas/test/$BACKUP")" = dump ] || fail "backup lost by purge"
[ -d /srv/omniacli/default_workspace ] || fail "the default storage of the workspaces removed by purge"
echo "ALL CHECKS PASSED"
EOS

# the Python a local python3.12 is taken from: the package's bundled one
BUNDLED_URL="$(dpkg-deb --ctrl-tarfile "$DEB" | tar -xO ./postinst | sed -n 's/^BUNDLED_URL=//p')"
BUNDLED_SHA256="$(dpkg-deb --ctrl-tarfile "$DEB" | tar -xO ./postinst | sed -n 's/^BUNDLED_SHA256=//p')"
# downloaded once into a cache of this machine, checked against the package's
# sha256: the images take it from there, never from github.com at each run
# (debian:11 installs it where the package looks for it, so its postinst
# finds it and does not download it; the download itself is not run here)
CACHE="${OMNIACLI_TEST_CACHE:-$HOME/.cache/omniacli-test}"
mkdir -p "$CACHE"
PY_TARBALL="$CACHE/python-$BUNDLED_SHA256.tar.gz"
# (checked when downloaded; a file there is used as it is, also one put there
# by hand: a tar.gz of the build's python/ folder)
if [ ! -s "$PY_TARBALL" ]; then
    for try in 1 2 3 4 5; do
        curl -sfL --max-time 300 -o "$PY_TARBALL.tmp" "$BUNDLED_URL" \
            && [ "$(sha256sum "$PY_TARBALL.tmp" | cut -d' ' -f1)" = "$BUNDLED_SHA256" ] \
            && mv "$PY_TARBALL.tmp" "$PY_TARBALL" && break
        echo "the bundled Python not downloaded (try $try of 5), again in 20 s" >&2
        sleep 20
    done
    [ -f "$PY_TARBALL" ] || { echo "the bundled Python cannot be downloaded from $BUNDLED_URL" >&2; exit 1; }
fi
cp "$PY_TARBALL" "$CACHE/python.tar.gz"

test_one() {
    local image="${1%+localpy}" tag="omniacli-test-${1//[:\/+]/-}" log="$LOGS/${1//[:\/+]/_}.log" cid
    local expect=/usr/bin localpy=true
    if [ "$1" != "$image" ]; then
        expect=/usr/local/bin
        localpy="tar -xzf /tmp/python.tar.gz -C /usr/local/lib \
 && ln -s /usr/local/lib/python/bin/python3.12 /usr/local/bin/python3.12 \
 && printf '#!/bin/sh\\nexit 1\\n' > /usr/local/bin/python3.13 && chmod 755 /usr/local/bin/python3.13"
    elif [ "$image" = debian:11 ]; then
        expect=/opt/omniacli/python/bin
        # the bundled Python where the package keeps it, with its sha256: postinst takes it
        localpy="mkdir -p /opt/omniacli && tar -xzf /tmp/python.tar.gz -C /opt/omniacli \
 && echo $BUNDLED_SHA256 > /opt/omniacli/python/.sha256"
    fi
    {
        docker build -q -t "$tag" -f - "$CACHE" >/dev/null <<EOF
FROM $image
ENV DEBIAN_FRONTEND=noninteractive
COPY python.tar.gz /tmp/python.tar.gz
RUN if grep -q '^11' /etc/debian_version 2>/dev/null; then \
      sed -i 's#http://deb.debian.org/debian-security#http://archive.debian.org/debian-security#' /etc/apt/sources.list; fi \
 && apt-get update -qq && apt-get install -y -qq systemd systemd-sysv sudo curl >/dev/null \
 && rm -f /usr/sbin/policy-rc.d \
 && $localpy
# (the images forbid service starts with policy-rc.d exit 101: a real machine does not)
CMD ["/sbin/init"]
EOF
        cid="$(docker run -d --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
            -v "$DEB:/opt/omniacli-test/omniacli.deb:ro" "$tag")"
        for i in $(seq 30); do
            docker exec "$cid" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
            sleep 1
        done
        docker exec -e EXPECT_HOME="$expect" "$cid" bash -c "apt-get update -qq >/dev/null; $CHECKS"
        local rc=$?
        # the immutable folders of the storages' binds and of the workspaces'
        # disks: docker cannot remove a container that keeps them
        docker exec "$cid" sh -c 'for m in $(findmnt -rn -o TARGET | grep "^/var/lib/omniacli-storage/" | sort -r); do umount "$m"; done
            for d in /var/lib/omniacli-storage/* /srv/*/*/workspaces/* /srv/*/*/workspaces /srv/omniacli/default_workspace/* /srv/newdisk/omniacli/*; do
                [ -d "$d" ] && chattr -i "$d" 2>/dev/null; done; true' >/dev/null 2>&1 || true
        docker rm -f "$cid" >/dev/null
        return $rc
    } > "$log" 2>&1
}

status=0
for image in "${IMAGES[@]}"; do
    ( if test_one "$image"; then echo "$image: OK"; else echo "$image: FAILED"; fi ) &
done
wait
for image in "${IMAGES[@]}"; do
    log="$LOGS/${image//[:\/+]/_}.log"
    echo "===== $image"
    cat "$log"
    grep -q "ALL CHECKS PASSED" "$log" || status=1
done
rm -rf "$LOGS"
exit $status
