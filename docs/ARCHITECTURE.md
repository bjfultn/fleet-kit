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

### Grok Bot agents

An agent whose `config.json` has `"platform": "grok-bot"` is a Grok Bot hosted by
xAI. It has no tmux pane, so the watcher wakes it by POSTing to a routine
webhook instead (`bin/grokbot_wake.py`). It is left out of the tmux map and
watched through its own path; Claude agents are unaffected.

```json
{
  "platform": "grok-bot",
  "alias": "...",
  "discord": { "bot_id": "...", "private_channel_id": "...", "state_dir": "~/.claude/channels/discord-..." },
  "grokbot": {
    "webhook_pass_key": "fleet/...-webhook",
    "tickets_file": "/var/lib/.../tickets.json",
    "quiet_hours": true,
    "quiet_tz": "Europe/London"
  }
}
```

- **Body.** Always `{"messages": [{"channel_id", "message_id", "author",
  "author_id", "text", "ts"}, ...]}`, a list even for one wake. Text is
  sanitized and cut to 1500 characters.
- **Secret.** `pass show <webhook_pass_key>`: line 1 is the https URL, line 2
  the bearer key. A locked store logs `secret store locked` and the wake is
  retried after the backoff below. Neither value is ever logged.
- **Who may wake it.** Checked every time, and **not** governed by
  `MENTION_WATCHER_ENFORCE`: the message text leaves the box, so there is no
  dry-run mode. The allowlist is the agent's own `access.json` under
  `discord.state_dir`, same format as the plugin's: `allowFrom` (humans),
  `allowBots` (bots), and the keys of `groups` as the channels it may see. Only
  the top-level lists count, not a per-group `allowFrom`. The agent's private
  channel must be listed under `groups`. A missing or unreadable file allows
  nobody. The file is re-read when it changes; a new channel needs a watcher
  restart to be polled. A human `@everyone` in the agent's own private channel
  wakes it, since everything there does; in any other channel it does not.
- **What wakes it.** A user mention of its bot id, or any message in its private
  channel. `@everyone`, `@here` and role tags do not count as a tag. Its own
  messages never wake it. Nor does a reply the broker has taken for one of its
  own asks. The broker records what it consumed in `tickets_file`
  (`{"tickets": [{"ask_message_id", "status", "reply_message_ids",
  "expected_author_ids", ...}]}`, read-only here). A reply to an ask is held
  back only if its id is in that ticket's `reply_message_ids`, or the ticket is
  still `working` and the author is in `expected_author_ids`. Everything else
  wakes the agent as usual: a second message after an "on it", a human's reply,
  another agent's reply. Missing or unreadable means no asks, with a warning; a
  ticket without the two fields claims nothing.
- **Throttle.** One wake in flight per agent: anything inside 120 s of the last
  send is held and goes out together as one body. Bot-authored wakes are capped
  at 4 per channel per hour, per agent, to stop two bots answering each other.
  The cap is per channel rather than per reply chain because each reply in a
  chain points at the previous message, so a chain never repeats a key. It is
  counted when a wake is sent (a coalesced batch counts once), so messages held
  overnight do not spend the budget; past the cap, bot-authored messages are
  dropped with a log line. Humans are never capped. A failed POST, including
  an exception, puts the batch back (and returns its count) and retries after
  the same 120 s. Held queues are in memory and capped at 20 per agent.
- **Quiet hours.** With `grokbot.quiet_hours` true, bot-authored wakes are held
  22:00 to 07:00 and released together; humans are never held for it. The
  clock is `grokbot.quiet_tz` (an IANA name, default `UTC`; set it per agent); an
  unknown name is a startup error. A human wake takes the held messages with it.
- **Startup warnings.** The watcher logs a `WARNING` line at start when the
  private channel is not in the agent's `groups`, when `webhook_pass_key` is
  missing, when `tickets_file` is unset (nothing then stops reply-pings except
  the cap), or when it is set but unreadable. An unreadable tickets file
  fails open (no asks known) and is re-logged at most every 10 minutes.

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
