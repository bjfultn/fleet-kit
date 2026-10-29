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
# Network probes use a small connect() classifier run as the agent. The
# firewall's drop makes connect() fail at once with EPERM (or time out), which
# is "blocked". "refused" means the packet got through to a closed port, and
# "open" means it connected: both FAIL.
set -u

usage() { echo "usage: $0 AGENT [--app]" >&2; exit 2; }

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
AGENT=$1
APP=0
if [ "$#" -eq 2 ]; then
    [ "$2" = "--app" ] || usage
    APP=1
fi
if ! [[ $AGENT =~ ^[a-z][a-z0-9-]{1,23}$ ]]; then
    echo "verify: AGENT must match ^[a-z][a-z0-9-]{1,23}\$" >&2
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
PASSES=0
SKIPS=0
TEAM_SOCK=/run/$AGENT-broker/team.sock

cd / || exit 2

as_agent() {
    setpriv --reuid="$AGENT" --regid="$AGENT" --init-groups --reset-env "$@"
}

pass() { echo "PASS $1"; PASSES=$((PASSES + 1)); }
skip() { echo "SKIP $1 ($2)"; SKIPS=$((SKIPS + 1)); }
fail() {
    if [ -n "${2:-}" ]; then echo "FAIL $1 ($2)"; else echo "FAIL $1"; fi
    FAILS=$((FAILS + 1))
}
have() { command -v "$1" >/dev/null 2>&1; }

# Positive control. Every negative check below proves nothing unless the
# commands really run as the agent, so stop dead if they do not.
got=$(as_agent id -un 2>&1)
if [ "$got" != "$AGENT" ]; then
    echo "FAIL running as $AGENT (got: ${got:-nothing})"
    exit 2
fi
pass "running as $AGENT"

# expect_rc NAME RC EXPECT: pass when the command's exit status is 0 (expect=ok)
# or nonzero (expect=refused). A timeout (124) is never a pass.
expect_rc() {
    local name=$1 rc=$2 want=$3
    if [ "$rc" -eq 124 ]; then
        fail "$name" "timed out"
    elif [ "$want" = ok ]; then
        if [ "$rc" -eq 0 ]; then pass "$name"; else fail "$name" "exit $rc"; fi
    else
        if [ "$rc" -ne 0 ]; then pass "$name"; else fail "$name" "succeeded"; fi
    fi
}

# --- files ---------------------------------------------------------------

# No -xdev: /home, /opt and /var are often separate mounts. The pruned trees
# are system trees or the agent's own home.
out=$(as_agent timeout 300 find / \
    \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /usr \
       -o -path /snap -o -path /etc/ssl -o -path /etc/ca-certificates \
       -o -path /var/lib/docker -o -path "/home/$AGENT" \) -prune -o \
    -readable \
    \( -name '*.env' -o -name '.env*' -o -name '*.key' -o -name '*.pem' \
       -o -name 'token*' -o -name 'access.json' \) \
    -not -path '*/node_modules/*' -not -path '*/site-packages/*' \
    -not -path '*/dist-packages/*' -print 2>/dev/null)
rc=$?
hits=$(printf '%s\n' "$out" | head -5 | sed '/^$/d')
if [ "$rc" -eq 124 ]; then
    fail "no readable secret-named files outside its home" "timed out"
elif [ -z "$hits" ]; then
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
    first=$(as_agent timeout 120 find "$d" -readable -print -quit 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 124 ]; then fail "$name" "timed out"
    elif [ -z "$first" ]; then pass "$name"
    else fail "$name" "reads $first"; fi
done

op_env=$OPERATOR_HOME/.claude/channels/discord/.env
if [ -e "$op_env" ]; then
    as_agent timeout 30 cat "$op_env" >/dev/null 2>&1
    expect_rc "cannot read operator discord .env" "$?" refused
else
    skip "cannot read operator discord .env" "$op_env does not exist"
fi

if [ -e "$BROKER_ETC/token" ]; then
    as_agent timeout 30 cat "$BROKER_ETC/token" >/dev/null 2>&1
    expect_rc "cannot read broker token" "$?" refused
else
    fail "cannot read broker token" "$BROKER_ETC/token missing, confinement unproven"
fi

as_agent timeout 30 ls "$BROKER_ETC" >/dev/null 2>&1
expect_rc "cannot list $BROKER_ETC" "$?" refused
as_agent timeout 30 ls "$BROKER_STATE" >/dev/null 2>&1
expect_rc "cannot list $BROKER_STATE" "$?" refused

# --- privilege and scheduling ----------------------------------------------

if have sudo; then
    as_agent timeout 30 sudo -n true >/dev/null 2>&1
    expect_rc "sudo -n refused" "$?" refused
else
    skip "sudo -n refused" "sudo not installed"
fi

if have crontab; then
    out=$(as_agent timeout 30 crontab -l 2>&1)
    rc=$?
    if [ "$rc" -eq 124 ]; then
        fail "crontab refused" "timed out"
    elif echo "$out" | grep -qiE 'not allowed|permission'; then
        pass "crontab refused"
    else
        fail "crontab refused" "no refusal message"
    fi
else
    skip "crontab refused" "crontab not installed"
fi

if have at; then
    out=$(as_agent timeout 30 at -l 2>&1)
    rc=$?
    if [ "$rc" -eq 124 ]; then
        fail "at refused" "timed out"
    elif echo "$out" | grep -qiE 'not allowed|permission'; then
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
    sshd_conn="user=$AGENT,host=localhost,addr=127.0.0.1"
    cfg=$(sshd -T -C "$sshd_conn" 2>&1)
    if [ $? -ne 0 ]; then
        # Without a running sshd, the privilege separation dir may be missing.
        mkdir -p /run/sshd
        cfg=$(sshd -T -C "$sshd_conn" 2>&1)
        sshd_rc=$?
    else
        sshd_rc=0
    fi
    if [ "$sshd_rc" -ne 0 ]; then
        skip "sshd DenyUsers lists $AGENT" "sshd -T failed: $(echo "$cfg" | head -1)"
    elif echo "$cfg" | grep -iE '^denyusers ' | grep -qE "(^| )$AGENT( |\$)"; then
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
    as_agent timeout 10 python3 -I -c '
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

# probe HOST PORT: prints blocked, open, refused or error:<Name>.
PROBE='
import socket, sys
s = socket.socket()
s.settimeout(5)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
except PermissionError:
    print("blocked")
except (TimeoutError, socket.timeout):
    print("blocked")
except ConnectionRefusedError:
    print("refused")
except OSError as exc:
    print("error:" + type(exc).__name__)
else:
    print("open")
'
probe_agent() { as_agent timeout 6 python3 -I -c "$PROBE" "$1" "$2" 2>/dev/null; }
probe_root() { timeout 6 python3 -I -c "$PROBE" "$1" "$2" 2>/dev/null; }

# expect_blocked NAME HOST PORT
expect_blocked() {
    local got
    got=$(probe_agent "$2" "$3")
    if [ "$got" = blocked ]; then pass "$1"; else fail "$1" "probe says ${got:-nothing (timed out?)}, want blocked"; fi
}

if have python3 && have timeout; then
    # Control: root must be able to reach the loopback service, or there is
    # nothing for the agent's probe to prove.
    if [ "$(probe_root 127.0.0.1 22)" = open ]; then
        expect_blocked "loopback service blocked (127.0.0.1:22)" 127.0.0.1 22
    else
        skip "loopback service blocked (127.0.0.1:22)" "control: root cannot connect to 127.0.0.1:22"
    fi

    gw=$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit }}')
    if [ -z "$gw" ]; then
        skip "LAN gateway blocked" "no default route"
    elif ! [[ $gw =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]; then
        skip "LAN gateway blocked" "gateway $gw is not in a range the firewall blocks"
    else
        expect_blocked "LAN gateway blocked ($gw:80)" "$gw" 80
    fi

    # The box's own primary address: reaching it would be a way round the
    # loopback drop.
    src=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
    if [ -z "$src" ]; then
        skip "own address blocked" "no primary address found"
    elif [ "$(probe_root "$src" 22)" = open ]; then
        expect_blocked "own address blocked ($src:22)" "$src" 22
    else
        skip "own address blocked ($src:22)" "control: root cannot connect to $src:22"
    fi
else
    skip "loopback, own address and LAN probes" "python3 or timeout not installed"
fi

if have curl; then
    as_agent curl -m5 -sI https://discord.com >/dev/null 2>&1
    expect_rc "https to discord.com works" "$?" ok
else
    skip "https to discord.com works" "curl not installed"
fi

as_agent getent hosts discord.com >/dev/null 2>&1
expect_rc "dns resolves discord.com" "$?" ok

if nft list table inet "${AGENT}_egress" >/dev/null 2>&1; then
    pass "nft table inet ${AGENT}_egress loaded"
else
    fail "nft table inet ${AGENT}_egress loaded"
fi

# --- the one allowed route --------------------------------------------------

as_agent timeout 60 env "TEAM_SOCKET=$TEAM_SOCK" team read general 1 >/dev/null 2>&1
expect_rc "team read general 1 works" "$?" ok

# --- inside the running app unit ---------------------------------------------

if [ "$APP" -eq 1 ]; then
    pid=$(systemctl show -p MainPID --value "$AGENT-app.service" 2>/dev/null)
    if [ -z "$pid" ] || [ "$pid" = 0 ]; then
        fail "app unit running" "$AGENT-app.service has no MainPID"
    else
        pass "app unit running (pid $pid)"
        ns() { nsenter -t "$pid" -m -S "$AGENT_UID" -G "$AGENT_GID" -- "$@"; }
        # Positive control for the nsenter wrapper, as above.
        got=$(ns id -un 2>&1)
        if [ "$got" != "$AGENT" ]; then
            echo "FAIL running as $AGENT in unit (got: ${got:-nothing})"
            exit 2
        fi
        pass "running as $AGENT in unit"
        for d in $PRIVATE_DIRS; do
            timeout 30 nsenter -t "$pid" -m -S "$AGENT_UID" -G "$AGENT_GID" -- ls "$d" >/dev/null 2>&1
            expect_rc "in unit: ls $d refused" "$?" refused
        done
        uids=$(ns sh -c 'for d in /proc/[0-9]*; do stat -c %u "$d"; done' 2>/dev/null | sort -u | tr '\n' ' ')
        if [ "$uids" = "$AGENT_UID " ]; then
            pass "in unit: /proc shows only $AGENT pids"
        else
            fail "in unit: /proc shows only $AGENT pids" "uids seen: ${uids:-none}"
        fi
        timeout 60 nsenter -t "$pid" -m -S "$AGENT_UID" -G "$AGENT_GID" -- env "TEAM_SOCKET=$TEAM_SOCK" team read general 1 >/dev/null 2>&1
        expect_rc "in unit: team read general 1 works" "$?" ok
    fi
fi

echo
echo "$PASSES pass, $FAILS fail, $SKIPS skip"
if [ "$FAILS" -eq 0 ]; then
    echo "all checks passed"
    exit 0
fi
echo "$FAILS check(s) FAILED"
exit 1
