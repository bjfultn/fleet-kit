# How it fits together

Three moving parts. Everything else is config.

## 1. tmux, one window per agent

`agent-manager.sh` reads each `agents/<name>/config.json` and opens a tmux
window named after the agent's alias, then types the `claude` invocation into
it. Agents are interactive Claude Code sessions, not headless calls, which is
why they can be driven by typing into the pane.

Startup commands are injected after the session reaches its ready prompt. The
poller waits for the `❯` glyph rather than sleeping a fixed interval, because a
resumed session can take minutes to load.

Two details in that injection path were paid for the hard way:

- The command text and its Enter are sent as **separate** keystrokes. Combined,
  the trailing CR races the TUI and can land before the input commits, so the
  submit reads an empty buffer and the command sits in the prompt unsent.
- There is no blind second Enter as a safety net. Once the first submit lands,
  the TUI may be showing a predicted next command, and a second Enter would
  accept it.

Each start writes a generation token to `/tmp`. A restart changes the token and
any older background poller sees the change and aborts, so a restart mid-startup
does not leave two pollers typing into the same pane.

## 2. The mention watcher

The Discord plugin gives each agent its own bot identity and delivers messages
into the session. It cannot wake an idle one, and it drops bot-authored
messages outright, so agents cannot see each other at all through it.

`mention-watcher.py` polls the Discord REST API every 5s across every channel
in the fleet. When it sees a message that mentions an agent, and that agent's
pane looks idle, it types the message into the pane.

It builds its agent map and channel list from the agent configs at startup.
This used to be two hand-maintained literals, which meant every bot id existed
in two places and drifted. A drifted entry fails silently: the watcher simply
never wakes that agent. An agent with no `discord.bot_id` is now a hard startup
error instead.

Authorisation comes from the same `access.json` the plugin uses, so there is
one allowlist rather than two that disagree. `MENTION_WATCHER_ENFORCE=0` logs
what it would drop without changing behaviour; run that way first, read the
log, then set it to 1.

**Waking an agent types text into a tool-capable pane.** Treat the allowlist as
a real security boundary.

## 3. fleet.json

Fleet-wide settings the scripts read at startup.

| Key | Default | Notes |
|---|---|---|
| `tmux_session` | `agents` | Also namespaces the generation tokens, so two fleets can share a box. |
| `timezone` | unset | Unset means the agents inherit the host's. |
| `agents_dir` | `$FLEET_ROOT/agents` | Absolute, or relative to `FLEET_ROOT`. |
| `general_channel_id` | unset | Watched in addition to each agent's private channel. |
| `log_dir` | `$FLEET_ROOT/logs` | |
| `python` | `python3` | Must be 3.10+ for the watcher. |
| `watcher_token_file` | first agent's `.env` | Any bot in the server can poll. |
| `defaults.auto_compact_window` | `200000` | |

## What is deliberately not here

`fleet-start.sh` starts agents and the watcher and nothing else. The version
this was extracted from also launched a dashboard, a deploy service and a
daemon, which meant an unrelated service failing to bind took the whole fleet
start down with it. Give other services their own boot jobs.
