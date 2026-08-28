#!/bin/bash
# Shared config loading for the fleet scripts. Sourced, never run directly.
#
# FLEET_ROOT is the directory holding fleet.json and agents/. It defaults to the
# parent of bin/, so a plain git checkout works with no environment set. Export
# FLEET_ROOT to point the same checkout at a fleet living somewhere else.

FLEET_ROOT="${FLEET_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
FLEET_CONFIG="$FLEET_ROOT/fleet.json"

if [ ! -f "$FLEET_CONFIG" ]; then
    echo "fleet: no fleet.json at $FLEET_CONFIG" >&2
    echo "       copy fleet.example.json to fleet.json, or export FLEET_ROOT." >&2
    exit 1
fi

# fleet_get <dotted.key> <default>
# Reads one value out of fleet.json. Missing keys return the default rather
# than an empty string, so a typo in a key name cannot silently become "".
fleet_get() {
    "${FLEET_PYTHON:-python3}" - "$FLEET_CONFIG" "$1" "$2" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
node = cfg
for part in sys.argv[2].split("."):
    if not isinstance(node, dict) or part not in node:
        print(sys.argv[3]); sys.exit(0)
    node = node[part]
print(node if node is not None else sys.argv[3])
PYEOF
}

# Resolve the interpreter first: every other fleet_get call uses it.
FLEET_PYTHON="$(python3 - "$FLEET_CONFIG" <<'PYEOF'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("python") or "python3")
except Exception:
    print("python3")
PYEOF
)"
FLEET_PYTHON="${FLEET_PYTHON/#\~/$HOME}"

AGENTS_DIR="$(fleet_get agents_dir "$FLEET_ROOT/agents")"
AGENTS_DIR="${AGENTS_DIR/#\~/$HOME}"
case "$AGENTS_DIR" in /*) ;; *) AGENTS_DIR="$FLEET_ROOT/$AGENTS_DIR" ;; esac

TMUX_SESSION="$(fleet_get tmux_session agents)"
FLEET_TZ="$(fleet_get timezone "")"
FLEET_LOG_DIR="$(fleet_get log_dir "$FLEET_ROOT/logs")"
FLEET_LOG_DIR="${FLEET_LOG_DIR/#\~/$HOME}"
FLEET_COMPACT_WINDOW="$(fleet_get defaults.auto_compact_window 200000)"

export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"
