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
#   VERIFY_OPERATOR_HOME  operator's home (default: the home of $SUDO_USER, if
#                         the script was started with sudo)
#   VERIFY_PRIVATE_DIRS   space-separated dirs the agent must not read anything
#                         in; use the same list you gave render.sh. Default none.
#
# The operator is the user who ran sudo ($SUDO_USER). Checks that need an
# operator or a socket the box does not have yet are skipped, not failed.
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

OPERATOR=${SUDO_USER:-}
[ "$OPERATOR" = root ] && OPERATOR=
OPERATOR_HOME=${VERIFY_OPERATOR_HOME:-}
if [ -z "$OPERATOR_HOME" ] && [ -n "$OPERATOR" ]; then
    OPERATOR_HOME=$(getent passwd "$OPERATOR" | cut -d: -f6)
fi
PRIVATE_DIRS=${VERIFY_PRIVATE_DIRS:-}
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
for tool in find timeout; do
    if ! have "$tool"; then
        echo "FAIL $tool not installed, no check below can be trusted"
        exit 2
    fi
done
got=$(as_agent timeout 10 id -un 2>&1)
if [ "$got" != "$AGENT" ]; then
    echo "FAIL running as $AGENT (got: ${got:-nothing})"
    exit 2
fi
pass "running as $AGENT"

# cant_trust RC: true for the exit statuses that mean the command never really
# ran (124 timed out; 125, 126, 127 from timeout/exec). Those are never a pass.
cant_trust() { [ "$1" -eq 124 ] || { [ "$1" -ge 125 ] && [ "$1" -le 127 ]; }; }
why() { if [ "$1" -eq 124 ]; then echo "timed out"; else echo "could not run"; fi; }

# expect_rc NAME RC EXPECT: pass when the command's exit status is 0 (expect=ok)
# or nonzero (expect=refused).
expect_rc() {
    local name=$1 rc=$2 want=$3
    if cant_trust "$rc"; then
        fail "$name" "$(why "$rc")"
    elif [ "$want" = ok ]; then
        if [ "$rc" -eq 0 ]; then pass "$name"; else fail "$name" "exit $rc"; fi
    else
        if [ "$rc" -ne 0 ]; then pass "$name"; else fail "$name" "succeeded"; fi
    fi
}

# --- files ---------------------------------------------------------------

# secret_find START: the secret-name search as the agent. No -xdev: /home, /opt
# and /var are often separate mounts. The pruned trees are system trees or the
# agent's own home.
secret_find() {
    as_agent timeout 300 find "$1" \
        \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /usr \
           -o -path /snap -o -path /etc/ssl -o -path /etc/ca-certificates \
           -o -path /var/lib/docker -o -path "/home/$AGENT" \) -prune -o \
        -readable \
        \( -name '*.env' -o -name '.env*' -o -name '*.key' -o -name '*.pem' \
           -o -name token -o -name 'token.*' -o -name '*.token' -o -name 'token-*' \
           -o -name 'access.json' \) \
        -not -path '*/node_modules/*' -not -path '*/site-packages/*' \
        -not -path '*/dist-packages/*' -print 2>/dev/null
}

# Canary: the same expression, started at a file the agent owns, must print
# it. That proves the expression matches and the uid can really run find.
CANARY=/home/$AGENT/.verify-canary.env
trap 'rm -f "$CANARY"' EXIT
name="no readable secret-named files outside its home"
if install -m 0600 -o "$AGENT" -g "$AGENT" /dev/null "$CANARY" 2>/dev/null \
        && [ "$(secret_find "$CANARY")" = "$CANARY" ]; then
    out=$(secret_find /)
    rc=$?
    rm -f "$CANARY"
    if ! cant_trust "$rc"; then
        # Certificates and keys by name are only a hit if they hold a private
        # key. One agent call over the whole list; the list goes in on stdin.
        out=$(printf '%s\n' "$out" | as_agent timeout 120 sh -c '
            while IFS= read -r f; do
                [ -n "$f" ] || continue
                case $f in
                    *.pem|*.crt|*.key) grep -qs "PRIVATE KEY" "$f" && printf "%s\n" "$f" ;;
                    *) printf "%s\n" "$f" ;;
                esac
            done
            exit 0' 2>/dev/null)
        rc=$?
    fi
    hits=$(printf '%s\n' "$out" | head -10 | sed '/^$/d')
    if cant_trust "$rc"; then
        fail "$name" "$(why "$rc")"
    elif [ -z "$hits" ]; then
        pass "$name"
    else
        fail "$name" "$(echo "$hits" | tr '\n' ' ')"
    fi
else
    rm -f "$CANARY"
    fail "find canary not seen" "secret search skipped"
fi

if [ -z "$PRIVATE_DIRS" ]; then
    skip "cannot read private directories" "VERIFY_PRIVATE_DIRS is empty"
fi
for d in $PRIVATE_DIRS; do
    name="cannot read anything under $d"
    if [ ! -d "$d" ]; then
        skip "$name" "no such directory"
        continue
    fi
    first=$(as_agent timeout 120 find "$d" -readable -print -quit 2>/dev/null)
    rc=$?
    if cant_trust "$rc"; then fail "$name" "$(why "$rc")"
    elif [ -z "$first" ]; then pass "$name"
    else fail "$name" "reads $first"; fi
done

op_env=$OPERATOR_HOME/.claude/channels/discord/.env
if [ -z "$OPERATOR_HOME" ]; then
    skip "cannot read operator discord .env" "no operator home (set VERIFY_OPERATOR_HOME)"
elif [ -e "$op_env" ]; then
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

for d in "$BROKER_ETC" "$BROKER_STATE"; do
    if [ -d "$d" ]; then
        as_agent timeout 30 ls "$d" >/dev/null 2>&1
        expect_rc "cannot list $d" "$?" refused
    else
        skip "cannot list $d" "not provisioned"
    fi
done

# The watcher runs as the operator and reads tickets.json through an ACL. The
# broker writes it on the first ask, so before then there is nothing to read.
TICKETS=$BROKER_STATE/tickets.json
if [ ! -e "$TICKETS" ]; then
    skip "operator can read tickets.json" "the broker has not written it yet"
elif [ -z "$OPERATOR" ]; then
    skip "operator can read tickets.json" "not started with sudo, no operator to run as"
else
    setpriv --reuid="$OPERATOR" --regid="$(id -g "$OPERATOR")" --init-groups --reset-env \
        timeout 30 cat "$TICKETS" >/dev/null 2>&1
    expect_rc "operator ($OPERATOR) can read tickets.json" "$?" ok
fi

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
    if cant_trust "$rc"; then
        fail "crontab refused" "$(why "$rc")"
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
    if cant_trust "$rc"; then
        fail "at refused" "$(why "$rc")"
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

# probe_agent HOST PORT / probe_agent unix PATH: connect() classifier. Prints
# blocked, open, refused or error:<Name>.
PROBE='
import socket, sys
if sys.argv[1] == "unix":
    s = socket.socket(socket.AF_UNIX)
    target = sys.argv[2]
else:
    s = socket.socket()
    target = (sys.argv[1], int(sys.argv[2]))
s.settimeout(5)
try:
    s.connect(target)
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

# LISTEN: bind an ephemeral loopback port and listen. Prints "denied" if the
# kernel refuses the bind (EPERM or EACCES), else "port N", then waits up to 8 s
# for a connection to be accepted.
LISTEN='
import socket
s = socket.socket()
try:
    s.bind(("127.0.0.1", 0))
    s.listen(1)
except PermissionError:
    print("denied", flush=True)
    raise SystemExit(0)
except OSError as exc:
    print("error:" + type(exc).__name__, flush=True)
    raise SystemExit(0)
print("port %d" % s.getsockname()[1], flush=True)
s.settimeout(8)
try:
    s.accept()
except OSError:
    pass
'
# wait_for_port FILE: the first line the listener wrote, as "denied", an
# "error:..." word or just the port number; empty after about 4 s of silence.
wait_for_port() {
    local i line
    for i in 1 2 3 4 5 6 7 8; do
        line=$(head -n 1 "$1" 2>/dev/null)
        if [ -n "$line" ]; then
            echo "${line#port }"
            return
        fi
        sleep 0.5
    done
}

DOCKER_SOCK=/var/run/docker.sock
if [ ! -S "$DOCKER_SOCK" ]; then
    skip "cannot connect docker.sock" "no docker socket"
elif have python3; then
    got=$(probe_agent unix "$DOCKER_SOCK")
    if [ "$got" = blocked ]; then
        pass "cannot connect docker.sock"
    else
        fail "cannot connect docker.sock" "probe says ${got:-nothing (timed out?)}"
    fi
else
    skip "cannot connect docker.sock" "python3 not installed"
fi

# A uid that is not the agent must not be able to talk to the broker. The probe
# is a connect() classifier (EACCES is "blocked"); the agent's own connect is
# the control, since a socket nobody can reach would pass the check vacuously.
if [ ! -S "$TEAM_SOCK" ]; then
    skip "nobody cannot connect to the broker socket" "no socket at $TEAM_SOCK"
elif ! have python3 || ! NOBODY_UID=$(id -u nobody 2>/dev/null) || ! NOBODY_GID=$(id -g nobody 2>/dev/null); then
    skip "nobody cannot connect to the broker socket" "python3 or user nobody missing"
elif [ "$(probe_agent unix "$TEAM_SOCK")" != open ]; then
    skip "nobody cannot connect to the broker socket" "control: $AGENT cannot connect to it"
else
    got=$(setpriv --reuid="$NOBODY_UID" --regid="$NOBODY_GID" --clear-groups --reset-env \
        timeout 6 python3 -I -c "$PROBE" unix "$TEAM_SOCK" 2>/dev/null)
    if [ "$got" = blocked ]; then
        pass "nobody cannot connect to the broker socket"
    else
        fail "nobody cannot connect to the broker socket" "probe says ${got:-nothing (timed out?)}, want blocked"
    fi
fi

# --- network ---------------------------------------------------------------

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

    # The agent must not be able to accept a connection (it could otherwise
    # squat a localhost port that a reverse proxy points at). It binds and
    # listens on an ephemeral loopback port; a connect from root must not get
    # through. A bind refused with EPERM or EACCES also passes. The kernel
    # completes a TCP handshake without accept(), so an "open" probe is enough.
    LISTEN_TMP=$(mktemp -d)
    chmod 0711 "$LISTEN_TMP"
    : > "$LISTEN_TMP/root.out"
    : > "$LISTEN_TMP/agent.out"
    chmod 0666 "$LISTEN_TMP/agent.out"
    # Control: the same listener run as root must be reachable, or an "open"
    # versus "blocked" answer for the agent's listener means nothing.
    timeout 12 python3 -I -c "$LISTEN" >"$LISTEN_TMP/root.out" 2>/dev/null &
    root_pid=$!
    ctl_port=$(wait_for_port "$LISTEN_TMP/root.out")
    if [[ $ctl_port =~ ^[0-9]+$ ]] && [ "$(probe_root 127.0.0.1 "$ctl_port")" = open ]; then
        as_agent timeout 12 python3 -I -c "$LISTEN" >"$LISTEN_TMP/agent.out" 2>/dev/null &
        agent_pid=$!
        got=$(wait_for_port "$LISTEN_TMP/agent.out")
        name="agent cannot accept connections on a loopback port it listens on"
        if [ "$got" = denied ]; then
            pass "$name (bind refused)"
        elif [[ $got =~ ^[0-9]+$ ]]; then
            res=$(probe_root 127.0.0.1 "$got")
            if [ "$res" = blocked ]; then pass "$name"; else fail "$name" "connect from root says ${res:-nothing}, want blocked"; fi
        else
            fail "$name" "listener did not start (${got:-no output})"
        fi
        wait "$agent_pid" 2>/dev/null
    else
        skip "agent cannot accept connections on a loopback port" "control: root cannot connect to its own listener"
    fi
    wait "$root_pid" 2>/dev/null
    rm -rf "$LISTEN_TMP"

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
        # The cgroup-level bind and address filters (the network checks above
        # only exercise the nft layer, outside this unit's cgroup).
        for prop in SocketBindDeny IPAddressDeny; do
            val=$(systemctl show -p "$prop" --value "$AGENT-app.service" 2>/dev/null)
            if [ -n "$val" ]; then pass "app unit sets $prop"; else fail "app unit sets $prop" "empty"; fi
        done
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
