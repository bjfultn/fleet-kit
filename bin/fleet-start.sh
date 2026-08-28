#!/bin/bash
# Start the fleet: the mention watcher, then every agent.
#
# This starts AGENTS AND NOTHING ELSE. The version this was extracted from also
# launched a dashboard and a deploy service, which meant one unrelated service
# failing to bind took the whole fleet start down with it. Anything else you run
# on the box gets its own launchd job.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/fleet-lib.sh"

mkdir -p "$FLEET_LOG_DIR"

# ── Mention watcher ─────────────────────────────────────────────────────────
# The Discord plugin drops bot-authored messages before any access check, so
# agents cannot wake each other through it. The watcher polls the REST API and
# types into the tmux pane directly. See docs/ARCHITECTURE.md.
#
# It needs one bot token to read channels; any bot in the server will do, so it
# borrows an agent's unless fleet.json names a file of its own.

token_file="$(fleet_get watcher_token_file "")"
token_file="${token_file/#\~/$HOME}"

if [ -z "$token_file" ]; then
    # Fall back to the first agent that has a discord state_dir with a .env.
    for cfg in "$AGENTS_DIR"/*/config.json; do
        [ -f "$cfg" ] || continue
        sd=$("$FLEET_PYTHON" -c "import json;print(json.load(open('$cfg')).get('discord',{}).get('state_dir',''))" 2>/dev/null)
        [ -n "$sd" ] || continue
        sd="${sd/#\~/$HOME}"
        if [ -f "$sd/.env" ]; then token_file="$sd/.env"; break; fi
    done
fi

if [ ! -f "${token_file:-/nonexistent}" ]; then
    echo "fleet-start: no bot token file found. Set watcher_token_file in fleet.json." >&2
    exit 1
fi

watcher_token=$(grep '^DISCORD_BOT_TOKEN=' "$token_file" | cut -d= -f2- | tr -d '"'"'"' \r')
if [ -z "$watcher_token" ]; then
    echo "fleet-start: no DISCORD_BOT_TOKEN in $token_file -- the watcher cannot poll." >&2
    exit 1
fi

pkill -f "mention-watcher.py" 2>/dev/null
# ENFORCE=1 drops wakes from authors outside the agent's own access.json.
# 0 logs "WOULD DROP" only. Start at 0, read the log, then flip.
MENTION_WATCHER_ENFORCE="${MENTION_WATCHER_ENFORCE:-0}" \
FLEET_ROOT="$FLEET_ROOT" \
DISCORD_BOT_TOKEN="$watcher_token" \
    "$FLEET_PYTHON" "$(dirname "${BASH_SOURCE[0]}")/mention-watcher.py" \
    >> "$FLEET_LOG_DIR/mention-watcher.log" 2>&1 &
unset watcher_token

# ── Agents ──────────────────────────────────────────────────────────────────
"$(dirname "${BASH_SOURCE[0]}")/agent-manager.sh" start-all

echo "fleet: started at $(date)"
