#!/usr/bin/env bash
# Usage: verify-confinement.sh AGENT [--app]      (run as root)
#
# Proves the agent uid is confined: it cannot read the operator's files or the
# broker's token, cannot reach loopback or the LAN, cannot use cron, at, ssh or
# sudo, and can reach Discord only through the broker socket and the web.
# Prints PASS, FAIL or SKIP per check and exits nonzero if anything FAILed.
#
# --app also checks from inside the running app unit's mount namespace.
#
# Environment:
#   VERIFY_OPERATOR_HOME  operator's home           (default /home/ops)
#   VERIFY_PRIVATE_DIRS   dirs the agent must not read anything in
#                         (default "/opt/web /home/ops")
#
# Network probes treat only curl exit 28 (timeout) as "blocked": the firewall
# drops, it does not reject, so a refusal means the rule is not in effect.
set -u

usage() { echo "usage: $0 AGENT [--app]" >&2; exit 2; }

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
AGENT=$1
APP=0
if [ "$#" -eq 2 ]; then
    [ "$2" = "--app" ] || usage
    APP=1
fi
if ! [[ $AGENT =~ ^[a-z][a-z0-9-]{1,30}$ ]]; then
    echo "verify: AGENT must match ^[a-z][a-z0-9-]{1,30}\$" >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "verify: run as root" >&2
    exit 2
fi
if ! AGENT_UID=$(id -u "$AGENT" 2>/dev/null) || ! AGENT_GID=$(id -g "$AGENT" 2>/dev/null); then
    echo "verify: no such user: $AGENT" >&2
    exit 2
fi

OPERATOR_HOME=${VERIFY_OPERATOR_HOME:-/home/ops}
PRIVATE_DIRS=${VERIFY_PRIVATE_DIRS:-/opt/web /home/ops}
BROKER_ETC=/etc/$AGENT-broker
BROKER_STATE=/var/lib/$AGENT-broker
FAILS=0

cd / || exit 2

as_agent() {
    setpriv --reuid="$AGENT" --regid="$AGENT" --init-groups --reset-env "$@"
}

pass() { echo "PASS $1"; }
skip() { echo "SKIP $1 ($2)"; }
fail() {
    if [ -n "${2:-}" ]; then echo "FAIL $1 ($2)"; else echo "FAIL $1"; fi
    FAILS=$((FAILS + 1))
}
have() { command -v "$1" >/dev/null 2>&1; }

# check NAME rc EXPECT: pass when the command's exit status is 0 (expect=ok) or
# nonzero (expect=refused).
expect_rc() {
    local name=$1 rc=$2 want=$3
    if [ "$want" = ok ]; then
        if [ "$rc" -eq 0 ]; then pass "$name"; else fail "$name" "exit $rc"; fi
    else
        if [ "$rc" -ne 0 ]; then pass "$name"; else fail "$name" "succeeded"; fi
    fi
}

# --- files ---------------------------------------------------------------

hits=$(as_agent timeout 300 find / -xdev -readable \
    \( -name '*.env' -o -name '.env*' -o -name '*.key' -o -name '*.pem' \
       -o -name 'token*' -o -name 'access.json' \) \
    -not -path "/home/$AGENT/*" -not -path '*/node_modules/*' \
    -not -path '*/site-packages/*' 2>/dev/null | head -5)
if [ -z "$hits" ]; then
    pass "no readable secret-named files outside its home"
else
    fail "no readable secret-named files outside its home" "$(echo "$hits" | tr '\n' ' ')"
fi

for d in $PRIVATE_DIRS; do
    name="cannot read anything under $d"
    if [ ! -d "$d" ]; then
        skip "$name" "no such directory"
        continue
    fi
    first=$(as_agent find "$d" -xdev -readable -print -quit 2>/dev/null)
    if [ -z "$first" ]; then pass "$name"; else fail "$name" "reads $first"; fi
done

op_env=$OPERATOR_HOME/.claude/channels/discord/.env
if [ -e "$op_env" ]; then
    as_agent cat "$op_env" >/dev/null 2>&1
    expect_rc "cannot read operator discord .env" "$?" refused
else
    skip "cannot read operator discord .env" "$op_env does not exist"
fi

if [ -e "$BROKER_ETC/token" ]; then
    as_agent cat "$BROKER_ETC/token" >/dev/null 2>&1
    expect_rc "cannot read broker token" "$?" refused
else
    fail "cannot read broker token" "$BROKER_ETC/token missing, confinement unproven"
fi

as_agent ls "$BROKER_ETC" >/dev/null 2>&1
expect_rc "cannot list $BROKER_ETC" "$?" refused
as_agent ls "$BROKER_STATE" >/dev/null 2>&1
expect_rc "cannot list $BROKER_STATE" "$?" refused

# --- privilege and scheduling ----------------------------------------------

if have sudo; then
    as_agent sudo -n true >/dev/null 2>&1
    expect_rc "sudo -n refused" "$?" refused
else
    skip "sudo -n refused" "sudo not installed"
fi

if have crontab; then
    out=$(as_agent crontab -l 2>&1)
    if echo "$out" | grep -qiE 'not allowed|permission'; then
        pass "crontab refused"
    else
        fail "crontab refused" "no refusal message"
    fi
else
    skip "crontab refused" "crontab not installed"
fi

if have at; then
    out=$(as_agent at -l 2>&1)
    if echo "$out" | grep -qiE 'not allowed|permission'; then
        pass "at refused"
    else
        fail "at refused" "no refusal message"
    fi
else
    skip "at refused" "at not installed"
fi

for f in /etc/cron.deny /etc/at.deny; do
    if grep -qx "$AGENT" "$f" 2>/dev/null; then pass "$f lists $AGENT"; else fail "$f lists $AGENT"; fi
done
for f in /etc/cron.allow /etc/at.allow; do
    if [ -e "$f" ] && ! grep -qx "$AGENT" "$f"; then
        :   # an allow file without the agent also excludes it
    elif [ -e "$f" ]; then
        fail "$f does not list $AGENT"
    fi
done

if have sshd; then
    if sshd -T -C "user=$AGENT,host=localhost,addr=127.0.0.1" 2>/dev/null \
            | grep -iE '^denyusers ' | grep -qE "(^| )$AGENT( |\$)"; then
        pass "sshd DenyUsers lists $AGENT"
    else
        fail "sshd DenyUsers lists $AGENT"
    fi
else
    skip "sshd DenyUsers lists $AGENT" "sshd not installed"
fi

status=$(passwd -S "$AGENT" 2>/dev/null | awk '{print $2}')
case $status in
    L|LK) pass "password locked" ;;
    *) fail "password locked" "status ${status:-unknown}" ;;
esac

linger=$(loginctl show-user "$AGENT" -p Linger 2>/dev/null)
if [ "$linger" = "Linger=yes" ] || [ -e "/var/lib/systemd/linger/$AGENT" ]; then
    fail "no lingering"
else
    pass "no lingering"
fi

if have python3; then
    as_agent python3 -I -c '
import socket, sys
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("/var/run/docker.sock")
except OSError:
    sys.exit(1)
' >/dev/null 2>&1
    expect_rc "cannot connect docker.sock" "$?" refused
else
    skip "cannot connect docker.sock" "python3 not installed"
fi

# --- network ---------------------------------------------------------------

if have curl; then
    as_agent curl -m3 -s -o /dev/null http://127.0.0.1:22/ >/dev/null 2>&1
    rc=$?
    if [ "$rc" -eq 28 ]; then pass "loopback service blocked (127.0.0.1:22)"; else fail "loopback service blocked (127.0.0.1:22)" "curl exit $rc, want 28"; fi

    gw=$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit }}')
    if [ -z "$gw" ]; then
        skip "LAN gateway blocked" "no default route"
    elif ! [[ $gw =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]; then
        skip "LAN gateway blocked" "gateway $gw is not in a range the firewall blocks"
    else
        as_agent curl -m3 -s -o /dev/null "http://$gw/" >/dev/null 2>&1
        rc=$?
        if [ "$rc" -eq 28 ]; then pass "LAN gateway blocked ($gw)"; else fail "LAN gateway blocked ($gw)" "curl exit $rc, want 28"; fi
    fi

    as_agent curl -m5 -sI https://discord.com >/dev/null 2>&1
    expect_rc "https to discord.com works" "$?" ok
else
    skip "network probes" "curl not installed"
fi

as_agent getent hosts discord.com >/dev/null 2>&1
expect_rc "dns resolves discord.com" "$?" ok

if nft list table inet "${AGENT}_egress" >/dev/null 2>&1; then
    pass "nft table inet ${AGENT}_egress loaded"
else
    fail "nft table inet ${AGENT}_egress loaded"
fi

# --- the one allowed route --------------------------------------------------

as_agent team read general 1 >/dev/null 2>&1
expect_rc "team read general 1 works" "$?" ok

# --- inside the running app unit ---------------------------------------------

if [ "$APP" -eq 1 ]; then
    pid=$(systemctl show -p MainPID --value "$AGENT-app.service" 2>/dev/null)
    if [ -z "$pid" ] || [ "$pid" = 0 ]; then
        fail "app unit running" "$AGENT-app.service has no MainPID"
    else
        pass "app unit running (pid $pid)"
        ns() { nsenter -t "$pid" -m -S "$AGENT_UID" -G "$AGENT_GID" -- "$@"; }
        for d in $PRIVATE_DIRS; do
            ns ls "$d" >/dev/null 2>&1
            expect_rc "in unit: ls $d refused" "$?" refused
        done
        uids=$(ns sh -c 'for d in /proc/[0-9]*; do stat -c %u "$d"; done' 2>/dev/null | sort -u | tr '\n' ' ')
        if [ "$uids" = "$AGENT_UID " ]; then
            pass "in unit: /proc shows only $AGENT pids"
        else
            fail "in unit: /proc shows only $AGENT pids" "uids seen: ${uids:-none}"
        fi
        ns team read general 1 >/dev/null 2>&1
        expect_rc "in unit: team read general 1 works" "$?" ok
    fi
fi

echo
if [ "$FAILS" -eq 0 ]; then
    echo "all checks passed"
    exit 0
fi
echo "$FAILS check(s) FAILED"
exit 1
