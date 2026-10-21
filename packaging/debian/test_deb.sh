#!/bin/bash
# Install an OmniaCI .deb in a container with a real systemd, for every target
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
export DEBIAN_FRONTEND=noninteractive

step "install"
apt-get install -y -qq /opt/omniaci-test/omniaci.deb > /tmp/install.log 2>&1 || { tail -30 /tmp/install.log; fail "apt-get install"; }
grep -E '^omniaci:' /tmp/install.log || true
dpkg -s omniaci | grep -q '^Status: install ok installed' || fail "package not installed"
for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && break; sleep 1; done
systemctl is-active --quiet omniaci || { journalctl -u omniaci --no-pager | tail -20; fail "service not active"; }
systemctl is-enabled --quiet omniaci || fail "service not enabled"
[ "$(stat -c '%U:%G %a' /etc/omniaci/omniaci.yaml)" = "root:omniaci 640" ] || fail "config permissions"
[ "$(stat -c '%U %a' /etc/omniaci/initial-admin-password)" = "root 600" ] || fail "password file permissions"
visudo -cf /etc/sudoers.d/omniaci >/dev/null || fail "sudoers file invalid"
PW="$(cat /etc/omniaci/initial-admin-password)"
[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:8090/)" = 401 ] || fail "no 401 without login"
[ "$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$PW" localhost:8090/)" = 200 ] || fail "admin login"
echo "python: $(/opt/omniaci/venv/bin/python -V), $(/opt/omniaci/venv/bin/pip freeze | grep -iE '^(fastapi|starlette)==' | tr '\n' ' ')"
VENV_HOME="$(sed -n 's/^home = //p' /opt/omniaci/venv/pyvenv.cfg)"
echo "venv built on $VENV_HOME"
[ "$VENV_HOME" = "$EXPECT_HOME" ] || fail "venv built on $VENV_HOME, expected $EXPECT_HOME"
/opt/omniaci/venv/bin/python -c 'import mcp' || fail "mcp not installed"
if [ "$EXPECT_HOME" = /opt/omniaci/python/bin ]; then
    grep -q 'downloading Python' /tmp/install.log || fail "bundled Python not downloaded"
else
    [ ! -e /opt/omniaci/python ] || fail "/opt/omniaci/python created with a usable local Python"
fi

step "a server with a git repository and a dummy Odoo service"
cat > /etc/systemd/system/odoo-dummy.service <<'EOF'
[Service]
ExecStart=/bin/sleep infinity
EOF
systemctl daemon-reload && systemctl start odoo-dummy
SYSTEMCTL="$(command -v systemctl)"
echo "omniaci ALL=(root) NOPASSWD: $SYSTEMCTL --no-ask-password restart odoo-dummy" >> /etc/sudoers.d/omniaci
visudo -cf /etc/sudoers.d/omniaci >/dev/null || fail "sudoers file invalid after the edit"
# root plays the developer on repositories owned by omniaci (test only)
git config --global --add safe.directory '*'
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
git init -q --bare /srv/remote.git && git -C /srv/remote.git symbolic-ref HEAD refs/heads/main
git clone -q /srv/remote.git /srv/dev 2>/dev/null
git -C /srv/dev checkout -q -b main && git -C /srv/dev commit -q --allow-empty -m one
git -C /srv/dev push -q origin main
git clone -q -b main /srv/remote.git /srv/odoo && chown -R omniaci:omniaci /srv/odoo /srv/remote.git
mkdir -p /srv/venv/bin && printf '#!/bin/sh\necho "update $*"\n' > /srv/venv/bin/click-odoo-update
chmod 755 /srv/venv/bin/click-odoo-update
touch /srv/odoo.conf /srv/odoo.log
API=(-s -u "admin:$PW" -H "Origin: http://localhost:8090" -H "Content-Type: application/json")
curl "${API[@]}" localhost:8090/api/servers/save -d '{"original": null, "name": "demo", "server": {"production": {
  "database": "DEMO", "odoo_config": "/srv/odoo.conf", "log_file": "/srv/odoo.log", "venv_bin": "/srv/venv/bin",
  "restart": {"type": "systemd", "service": "odoo-dummy", "sudo": true},
  "repositories": [{"name": "odoo", "path": "/srv/odoo", "branch": "main"}]}}}' | grep -q '"ok":true' \
  || fail "server not saved"
[ "$(stat -c %U /var/lib/omniaci/servers.yaml)" = omniaci ] || fail "servers.yaml owner"
grep -q "status: draft" /var/lib/omniaci/servers.yaml || fail "a new server is not a draft"

step "a user created from the Users page, as user omniaci"
curl "${API[@]}" localhost:8090/api/access/users/save \
  -d '{"name": "anna", "role": "developer", "password": "anna-pass-1", "new": true}' | grep -q '"ok":true' \
  || fail "user not saved"
[ "$(stat -c '%U %a' /var/lib/omniaci/users.yaml)" = "omniaci 600" ] || fail "users.yaml owner or mode"
[ "$(curl -s -o /dev/null -w '%{http_code}' -u anna:anna-pass-1 localhost:8090/api/info)" = 200 ] || fail "new user login"

step "a link to itself: token from the Users page, link from the Links page, pages through the proxy"
LINK_TOKEN="$(curl "${API[@]}" localhost:8090/api/access/links/save -d '{"name": "self", "role": "developer", "new": true}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
curl "${API[@]}" localhost:8090/api/links/save \
  -d "{\"name\": \"me\", \"url\": \"http://127.0.0.1:8090\", \"token\": \"$LINK_TOKEN\", \"new\": true}" | grep -q '"ok":true' \
  || fail "link not saved"
[ "$(stat -c '%U %a' /var/lib/omniaci/links.yaml)" = "omniaci 600" ] || fail "links.yaml owner or mode"
curl "${API[@]}" -X POST localhost:8090/api/links/me/test | grep -q '"role":"developer"' || fail "Test link"
curl -s -u "admin:$PW" -H "Accept: text/html" localhost:8090/links/me/ | grep -q "Linked instance <strong>me</strong>" \
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
sed -i 's/status: draft/status: active/' /var/lib/omniaci/servers.yaml
systemctl restart omniaci
for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && break; sleep 1; done
# links.yaml read again at startup: the link lists this instance's server
curl -s -u "admin:$PW" localhost:8090/ | grep -q "me › demo" || fail "linked server row"

step "git pull + restart through the web service"
git -C /srv/dev commit -q --allow-empty -m two && git -C /srv/dev push -q origin main
PID_BEFORE="$(systemctl show -p MainPID --value odoo-dummy)"
LOCATION="$(curl "${API[@]}" -o /dev/null -w '%{redirect_url}' -X POST localhost:8090/servers/demo/production/pull-restart)"
JOB="${LOCATION##*/}"
for i in $(seq 60); do
    STATUS="$(curl -s -u "admin:$PW" "localhost:8090/api/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    case "$STATUS" in success|failed|interrupted) break ;; esac
    sleep 1
done
if [ "$STATUS" != success ]; then
    curl -s -u "admin:$PW" "localhost:8090/api/jobs/$JOB" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("\n".join(d["lines"]))'
    fail "pull-restart job $STATUS"
fi
[ "$(git -C /srv/odoo log -1 --format=%s)" = two ] || fail "repository not pulled"
PID_AFTER="$(systemctl show -p MainPID --value odoo-dummy)"
[ "$PID_AFTER" != "$PID_BEFORE" ] && [ "$PID_AFTER" != 0 ] || fail "odoo-dummy not restarted"
echo "pulled, odoo-dummy restarted ($PID_BEFORE -> $PID_AFTER)"

step "load metrics of the systemd unit, read as user omniaci"
sleep 12
curl -s -u "admin:$PW" "localhost:8090/api/metrics?server=demo" > /tmp/metrics.json
python3 - <<'PY' || fail "metrics"
import json
info = json.load(open("/tmp/metrics.json"))["instances"]["demo/production"]
print(info["source"], info["points"][-1])
assert info["source"] == "systemd unit odoo-dummy", info["source"]
assert info["points"][-1]["procs"] == 1 and info["points"][-1]["mem"] > 0
PY

step "reinstall keeps config, servers and password"
HASH_BEFORE="$(sha256sum /etc/omniaci/omniaci.yaml)"
dpkg -i /opt/omniaci-test/omniaci.deb > /tmp/reinstall.log 2>&1 || { tail -20 /tmp/reinstall.log; fail "reinstall"; }
grep -q 'downloading Python' /tmp/reinstall.log && fail "bundled Python downloaded again on reinstall"
for i in $(seq 30); do curl -s -o /dev/null localhost:8090 && break; sleep 1; done
systemctl is-active --quiet omniaci || fail "service not active after reinstall"
[ "$(sha256sum /etc/omniaci/omniaci.yaml)" = "$HASH_BEFORE" ] || fail "config changed by reinstall"
curl -s -u "admin:$PW" localhost:8090/ | grep -q demo || fail "server lost after reinstall"

step "remove and purge"
apt-get remove -y -qq omniaci >/dev/null 2>&1 || fail "remove"
systemctl is-active --quiet omniaci && fail "service still running after remove"
[ ! -e /opt/omniaci/venv ] || fail "venv left after remove"
[ ! -e /opt/omniaci/python ] || fail "bundled Python left after remove"
[ -e /etc/omniaci/omniaci.yaml ] || fail "config removed by remove"
apt-get purge -y -qq omniaci >/dev/null 2>&1 || fail "purge"
[ ! -e /etc/omniaci ] || fail "/etc/omniaci left after purge"
[ ! -e /var/lib/omniaci ] || fail "/var/lib/omniaci left after purge"
echo "ALL CHECKS PASSED"
EOS

# the Python a local python3.12 is taken from: the package's bundled one
BUNDLED_URL="$(dpkg-deb --ctrl-tarfile "$DEB" | tar -xO ./postinst | sed -n 's/^BUNDLED_URL=//p')"

test_one() {
    local image="${1%+localpy}" tag="omniaci-test-${1//[:\/+]/-}" log="$LOGS/${1//[:\/+]/_}.log" cid
    local expect=/usr/bin localpy=true
    if [ "$1" != "$image" ]; then
        expect=/usr/local/bin
        localpy="curl -sfL '$BUNDLED_URL' | tar -xz -C /usr/local/lib \
 && ln -s /usr/local/lib/python/bin/python3.12 /usr/local/bin/python3.12 \
 && printf '#!/bin/sh\\nexit 1\\n' > /usr/local/bin/python3.13 && chmod 755 /usr/local/bin/python3.13"
    elif [ "$image" = debian:11 ]; then
        expect=/opt/omniaci/python/bin
    fi
    {
        docker build -q -t "$tag" - >/dev/null <<EOF
FROM $image
ENV DEBIAN_FRONTEND=noninteractive
RUN if grep -q '^11' /etc/debian_version 2>/dev/null; then \
      sed -i 's#http://deb.debian.org/debian-security#http://archive.debian.org/debian-security#' /etc/apt/sources.list; fi \
 && apt-get update -qq && apt-get install -y -qq systemd systemd-sysv sudo curl >/dev/null \
 && rm -f /usr/sbin/policy-rc.d \
 && $localpy
# (the images forbid service starts with policy-rc.d exit 101: a real machine does not)
CMD ["/sbin/init"]
EOF
        cid="$(docker run -d --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
            -v "$DEB:/opt/omniaci-test/omniaci.deb:ro" "$tag")"
        for i in $(seq 30); do
            docker exec "$cid" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
            sleep 1
        done
        docker exec -e EXPECT_HOME="$expect" "$cid" bash -c "apt-get update -qq >/dev/null; $CHECKS"
        local rc=$?
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
