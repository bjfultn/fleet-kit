#!/bin/bash
# Agent Manager — start, stop, and manage Claude Code agents in tmux sessions
# Usage: agent-manager.sh <command> [agent_name]
# Commands: start, stop, restart, status, start-all, stop-all, list

# AGENTS_DIR, TMUX_SESSION, FLEET_TZ, FLEET_PYTHON and PATH all come from here.
source "$(dirname "${BASH_SOURCE[0]}")/fleet-lib.sh"

get_agent_dirs() {
    for d in "$AGENTS_DIR"/*/config.json; do
        dirname "$d"
    done
}

get_agent_name() {
    local dir="$1"
    "$FLEET_PYTHON" -c "import json; c=json.load(open('$dir/config.json')); print(c.get('alias', c['name'].lower()))"
}

is_claude_agent() {
    local dir="$1"
    "$FLEET_PYTHON" -c "import json,sys; c=json.load(open('$dir/config.json')); sys.exit(0 if c.get('platform','claude') == 'claude' and not c.get('hidden') else 1)" 2>/dev/null
}

get_agent_workdir() {
    # working_directory is optional. Left unset, an agent runs in its own
    # directory, which is what you want unless the agent is meant to operate on
    # a codebase somewhere else. Requiring it made every config file carry an
    # absolute path that is wrong on any other machine.
    local dir="$1"
    "$FLEET_PYTHON" -c "import json; print(json.load(open('$dir/config.json')).get('working_directory') or '$dir')"
}

get_agent_args() {
    local dir="$1"
    "$FLEET_PYTHON" -c "import json; print(' '.join(json.load(open('$dir/config.json'))['claude_args']))"
}

get_agent_state_dir() {
    local dir="$1"
    "$FLEET_PYTHON" -c "import json; d=json.load(open('$dir/config.json')).get('discord',{}).get('state_dir',''); print(d)" 2>/dev/null
}

get_startup_commands() {
    local dir="$1"
    "$FLEET_PYTHON" -c "import json; cmds=json.load(open('$dir/config.json')).get('startup_commands',[]); print('\n'.join(cmds))" 2>/dev/null
}

get_agent_env() {
    # Returns any extra env vars from config.json "env" field as "KEY=VALUE KEY2=VALUE2 ..."
    local dir="$1"
    "$FLEET_PYTHON" -c "
import json, os
env=json.load(open('$dir/config.json')).get('env',{})
print(' '.join(f\"{k}={v.replace('~', os.path.expanduser('~'))}\" for k,v in env.items()))
" 2>/dev/null
}

ensure_tmux_session() {
    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux new-session -d -s "$TMUX_SESSION" -n "manager"
    fi
}

start_agent() {
    local name="$1"
    local agent_dir="$AGENTS_DIR/$name"

    if [ ! -f "$agent_dir/config.json" ]; then
        echo "Agent '$name' not found in $AGENTS_DIR"
        return 1
    fi

    if ! is_claude_agent "$agent_dir"; then
        echo "Agent '$name' is not a Claude Code agent (skipping)"
        return 0
    fi

    local alias_name
    alias_name=$(get_agent_name "$agent_dir")
    local workdir
    workdir=$(get_agent_workdir "$agent_dir")
    local args
    args=$(get_agent_args "$agent_dir")

    # Drop --continue when this working directory has no prior conversation.
    #
    # `claude --continue` with nothing to continue prints "No conversation found
    # to continue" and EXITS. Interactively that leaves a dead tmux window
    # sitting at a shell prompt, which is exactly what a brand-new fleet hits on
    # its first start: every agent dies on launch and the fleet looks like it
    # came up. An earlier check cleared --continue by probing it under --print,
    # which does not behave this way. Test the mode you actually ship.
    #
    # --continue is right for every restart after the first, so it stays in the
    # config and gets stripped per launch instead. Claude Code keeps transcripts
    # in ~/.claude/projects/<key>, where the key is the RESOLVED working
    # directory with every character outside [A-Za-z0-9-] replaced by a dash,
    # truncated at 200 characters with a hash appended.
    if [[ "$args" == *--continue* ]]; then
        local real_workdir proj_key proj_dir
        real_workdir=$(cd "$workdir" 2>/dev/null && pwd -P) || real_workdir="$workdir"
        proj_key=$(printf '%s' "$real_workdir" | LC_ALL=C sed 's/[^A-Za-z0-9-]/-/g')
        proj_dir="$HOME/.claude/projects/${proj_key:0:200}"
        if [ ${#proj_key} -gt 200 ]; then
            # Past the cap the name carries a hash we cannot recompute, so match
            # on the prefix instead of guessing the whole thing.
            proj_dir=$(ls -d "$HOME/.claude/projects/${proj_key:0:200}"* 2>/dev/null | head -1)
        fi
        if [ -z "$proj_dir" ] || ! compgen -G "$proj_dir/*.jsonl" >/dev/null 2>&1; then
            echo "  No prior conversation in $workdir - starting fresh"
            args=$(echo "$args" | sed 's/--continue//')
        fi
    fi

    ensure_tmux_session

    # Check if window already exists
    if tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -q "^${alias_name}$"; then
        echo "Agent '$alias_name' is already running"
        return 0
    fi

    local state_dir
    state_dir=$(get_agent_state_dir "$agent_dir")
    local extra_env
    extra_env=$(get_agent_env "$agent_dir")
    # Extract --effort value from claude_args so statusline can display it accurately
    local agent_effort
    agent_effort=$("$FLEET_PYTHON" -c "
import json, re
args = json.load(open('$agent_dir/config.json')).get('claude_args', [])
s = ' '.join(args)
m = re.search(r'--effort\s+(\S+)', s)
print(m.group(1) if m else 'medium')
" 2>/dev/null)
    # Advisor tool: cheaper-tier agents get an Opus advisor to consult; agents
    # already running a top-tier model do not need one and it is off for them.
    # Drop this block if you want the advisor everywhere, or nowhere.
    local agent_model
    agent_model=$("$FLEET_PYTHON" -c "
import json, re
args = json.load(open('$agent_dir/config.json')).get('claude_args', [])
m = re.search(r'--model\s+(\S+)', ' '.join(args))
print(m.group(1) if m else '')
" 2>/dev/null)
    local advisor_prefix="CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1 "
    local advisor_arg=""
    case "$agent_model" in
        *sonnet*|*Sonnet*)
            advisor_prefix="CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL=1 "
            advisor_arg="--advisor opus"
            ;;
    esac
    local tz_prefix=""
    [ -n "$FLEET_TZ" ] && tz_prefix="TZ=$FLEET_TZ "
    local common_env="FORCE_COLOR=1 CLAUDE_CODE_AUTO_COMPACT_WINDOW=${FLEET_COMPACT_WINDOW} ${advisor_prefix}AGENT_EFFORT=${agent_effort:-medium} "
    local env_prefix="${tz_prefix}${common_env}"
    if [ -n "$state_dir" ]; then
        # Expand ~ to $HOME
        state_dir="${state_dir/#\~/$HOME}"
        env_prefix="DISCORD_STATE_DIR=$state_dir ${tz_prefix}${common_env}"
    fi
    if [ -n "$extra_env" ]; then
        env_prefix="${extra_env} ${env_prefix}"
    fi

    echo "Starting agent '$alias_name' in tmux..."
    tmux new-window -t "$TMUX_SESSION" -n "$alias_name"

    # Write generation token — any older background poller will see this change and abort
    local gen_token="${alias_name}_$$_$(date +%s)"
    echo "$gen_token" > "/tmp/fleet-${TMUX_SESSION}-${alias_name}-gen"

    if [[ "$args" == *--resume* ]]; then
        local args_no_resume
        args_no_resume=$(echo "$args" | sed 's/--resume [^ ]*//')
        tmux send-keys -t "$TMUX_SESSION:$alias_name" "cd $workdir && ${env_prefix}claude $args $advisor_arg || ${env_prefix}claude $args_no_resume $advisor_arg" Enter
    else
        tmux send-keys -t "$TMUX_SESSION:$alias_name" "cd $workdir && ${env_prefix}claude $args $advisor_arg" Enter
    fi

    local startup_cmds
    startup_cmds=$(get_startup_commands "$agent_dir")
    if [ -n "$startup_cmds" ]; then
        local cmd_count
        cmd_count=$(echo "$startup_cmds" | wc -l | tr -d ' ')
        echo "  Scheduling $cmd_count startup command(s) (waiting for ready prompt)"
        (
            local my_token="$gen_token"

            # Wait for the Claude Code ready prompt (❯)
            local max_wait=180  # 3 minutes max
            local elapsed=0
            echo "  Waiting for '$alias_name' to reach ready prompt..."
            while [ $elapsed -lt $max_wait ]; do
                sleep 5
                elapsed=$((elapsed + 5))

                # Abort if a newer start has happened
                if [ "$(cat /tmp/fleet-${TMUX_SESSION}-${alias_name}-gen 2>/dev/null)" != "$my_token" ]; then
                    echo "  Poller for '$alias_name' aborted (agent restarted)"
                    exit 0
                fi

                local pane_content
                pane_content=$(tmux capture-pane -t "$TMUX_SESSION:$alias_name" -p 2>/dev/null | tail -15)

                # Auto-dismiss session picker (case-insensitive match)
                if echo "$pane_content" | grep -qi 'Resume Session\|Type to search\|Type to Search'; then
                    echo "  '$alias_name' at session picker, selecting first session..."
                    tmux send-keys -t "$TMUX_SESSION:$alias_name" Enter
                    sleep 3
                    continue
                fi

                if echo "$pane_content" | grep -q '❯'; then
                    echo "  '$alias_name' is ready (${elapsed}s)"
                    break
                fi
            done
            if [ $elapsed -ge $max_wait ]; then
                echo "  WARNING: '$alias_name' did not reach ready prompt after ${max_wait}s, injecting anyway"
            fi

            # Small extra buffer after prompt appears
            sleep 2

            while IFS= read -r cmd; do
                [ -z "$cmd" ] && continue

                # Abort if a newer start has happened
                if [ "$(cat /tmp/fleet-${TMUX_SESSION}-${alias_name}-gen 2>/dev/null)" != "$my_token" ]; then
                    echo "  Poller for '$alias_name' aborted mid-injection (agent restarted)"
                    exit 0
                fi

                echo "  Injecting: $cmd"
                # Send the text and the Enter as SEPARATE keystrokes. Combining them
                # ("$cmd" Enter in one send-keys) races the TUI: the trailing CR can
                # land before the input commits the text to state, so the submit reads
                # an empty buffer and no-ops — leaving the command sitting unsent in the
                # prompt. Type, let it settle, then submit with a SINGLE Enter.
                # Do NOT add a "safety" second Enter: once the first submit lands, the
                # TUI may show a predicted next command, and a blind second Enter could
                # accept/send it. If a single Enter ever proves unreliable, verify the
                # buffer cleared via capture-pane and re-send conditionally — never blind.
                # Type the text in CHUNKS, not one shot.
                #
                # tmux delivers a multi-KB send-keys intact (measured to 8000
                # bytes into a raw-mode reader, with and without -l), but the
                # TUI on the other end does not consume it that fast: a 3128
                # char command arrived with its leading ~2800 chars GONE and
                # only the tail in the prompt. Curtis hit this on 2026-08-29 and
                # noticed solely because the fragment was visibly broken. A
                # truncation that still parses is the real hazard -- it would
                # register a cron with a silently mangled prompt, and the job
                # would then do the wrong thing on a schedule, forever, while
                # looking perfectly healthy.
                #
                # Every command at or under ~1227 chars survived, so the ceiling
                # sits somewhere above that. 400 is comfortably under it with
                # room to spare, and the pauses give the TUI time to drain.
                # send-keys -l because chunk boundaries can otherwise split a
                # token that tmux would try to read as a key name.
                _chunk=400
                _i=0
                _len=${#cmd}
                while [ $_i -lt $_len ]; do
                    tmux send-keys -t "$TMUX_SESSION:$alias_name" -l "${cmd:$_i:$_chunk}"
                    _i=$((_i + _chunk))
                    sleep 0.15
                done
                sleep 1
                tmux send-keys -t "$TMUX_SESSION:$alias_name" Enter

                # Wait for ❯ ready prompt before sending the next command (max 120s)
                local cmd_wait=0
                while [ $cmd_wait -lt 120 ]; do
                    sleep 5
                    cmd_wait=$((cmd_wait + 5))
                    if tmux capture-pane -t "$TMUX_SESSION:$alias_name" -p 2>/dev/null | tail -10 | grep -q '❯'; then
                        break
                    fi
                done
                sleep 1
            done <<< "$startup_cmds"

            # Final Enter to flush anything stuck in buffer
            tmux send-keys -t "$TMUX_SESSION:$alias_name" "" Enter
        ) &
    fi

    echo "Agent '$alias_name' started in tmux session '$TMUX_SESSION'"
}

stop_agent() {
    local name="$1"
    local agent_dir="$AGENTS_DIR/$name"

    if [ ! -f "$agent_dir/config.json" ]; then
        echo "Agent '$name' not found"
        return 1
    fi

    local alias_name
    alias_name=$(get_agent_name "$agent_dir")

    if tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -q "^${alias_name}$"; then
        echo "Stopping agent '$alias_name'..."
        # Send Ctrl-C to interrupt any running task
        tmux send-keys -t "$TMUX_SESSION:$alias_name" C-c
        sleep 2
        # Send /exit
        tmux send-keys -t "$TMUX_SESSION:$alias_name" "/exit" Enter 2>/dev/null
        sleep 2
        # Confirm "Exit anyway" if background tasks prompt appears
        tmux send-keys -t "$TMUX_SESSION:$alias_name" Enter 2>/dev/null
        sleep 1
        # Force kill the window as fallback
        tmux kill-window -t "$TMUX_SESSION:$alias_name" 2>/dev/null
        echo "Agent '$alias_name' stopped"
    else
        echo "Agent '$alias_name' is not running"
    fi
}

status_all() {
    echo "Agent Status (tmux session: $TMUX_SESSION)"
    echo "============================================"

    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        echo "No tmux session running"
        return
    fi

    local running
    running=$(tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null)

    for agent_dir in $(get_agent_dirs); do
        local name
        name=$(get_agent_name "$agent_dir")
        if ! is_claude_agent "$agent_dir"; then
            echo "  $name: EXTERNAL (not managed here)"
            continue
        fi
        if echo "$running" | grep -q "^${name}$"; then
            echo "  $name: RUNNING"
        else
            echo "  $name: STOPPED"
        fi
    done
}

start_all() {
    for agent_dir in $(get_agent_dirs); do
        local name
        name=$(basename "$agent_dir")
        # Skip hidden / non-managed agents (e.g. temp — placeholder for manual experimentation).
        # They can still be launched explicitly with `start <name>`.
        if ! is_claude_agent "$agent_dir"; then
            echo "  Skipping '$name' (hidden / not auto-started)"
            continue
        fi
        start_agent "$name"
        sleep 2  # Stagger starts
    done
}

stop_all() {
    for agent_dir in $(get_agent_dirs); do
        local name
        name=$(basename "$agent_dir")
        stop_agent "$name"
    done
}

list_agents() {
    echo "Configured agents:"
    for agent_dir in $(get_agent_dirs); do
        local name
        name=$(get_agent_name "$agent_dir")
        local dir_name
        dir_name=$(basename "$agent_dir")
        echo "  $dir_name ($name)"
    done
}

case "${1:-}" in
    start)
        if [ -z "${2:-}" ]; then echo "Usage: $0 start <agent_name>"; exit 1; fi
        start_agent "$2"
        ;;
    stop)
        if [ -z "${2:-}" ]; then echo "Usage: $0 stop <agent_name>"; exit 1; fi
        stop_agent "$2"
        ;;
    restart)
        if [ -z "${2:-}" ]; then echo "Usage: $0 restart <agent_name>"; exit 1; fi
        stop_agent "$2"
        sleep 2
        start_agent "$2"
        ;;
    status)
        status_all
        ;;
    start-all)
        start_all
        ;;
    stop-all)
        stop_all
        ;;
    list)
        list_agents
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|start-all|stop-all|list} [agent_name]"
        exit 1
        ;;
esac
