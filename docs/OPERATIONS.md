# Operations

## Day to day

```
bin/agent-manager.sh status
bin/agent-manager.sh restart <name>
bin/agent-manager.sh stop-all
bin/fleet-start.sh
tmux attach -t agents
```

## Restarting one thing

Restart one agent with `agent-manager.sh restart <name>`. Do not run
`fleet-start.sh` to fix a single agent: it restarts everything, including the
agents that were fine.

## Boot

`templates/launchd/` has a plist template for macOS. Point it at
`bin/fleet-start.sh` with `RunAtLoad`.

Note that launchd caches the plist. After editing one, `launchctl unload` then
`load` it; `kickstart` alone can keep running the old definition.

On Linux this is the piece to replace: a systemd unit with
`Restart=on-failure`, plus something to keep the machine awake.

## Sleep

A sleeping box looks exactly like a fleet-wide outage. On macOS,
`sudo pmset -a disablesleep 1`, and keep a `caffeinate` job running.

## MCP servers

Which MCP servers your agents can reach is host state, not fleet state. Nothing
in this repo configures them, on purpose: the useful ones are specific to what
your team does, and most of them are wrapped around an API key.

`claude mcp add` writes to one of three scopes, and for a fleet the difference
matters more than it does for a single user, because each agent runs in its own
working directory.

```sh
claude mcp add <name> -s user    -e KEY=value -- npx some-mcp-server
claude mcp add <name> -s local   -- npx some-mcp-server
claude mcp add <name> -s project -- npx some-mcp-server
```

- **user** is global to the account. Every agent gets the server, whichever
  directory it runs from. This is what you want for anything the whole team
  should be able to reach.
- **local** is that one directory, stored per project in `~/.claude.json`. Since
  an agent's working directory is its own, this scopes a server to a single
  agent. Useful when only one of them should hold a given credential.
- **project** writes a `.mcp.json` into the directory, which means it can be
  committed and travels with the repo. Do not put credentials in it.

Two consequences worth planning for:

**None of it is in this repo, so none of it comes back with a rebuild.** A
restored fleet has its agents, personas, memory and Discord wiring, and no MCP
servers at all. Keep your own note of what you registered and with which keys.

**A server added at user scope reaches every agent.** That includes agents you
would not have given the credential to deliberately. Scope by what each agent
is supposed to be able to do, not by what is convenient to type once.

Restart an agent after changing its servers. A running session keeps the set it
started with, and, as with any config change, replies still working is not
evidence the change took.

## Things that fail quietly

**The plugin patch reverted.** Agents stop waking each other, nothing logs an
error, human mentions still work so it looks fine from the outside. Check this
first whenever agent-to-agent messaging stops.

**An agent config with no `bot_id`.** Now a hard startup error in the watcher,
but if you see the watcher refusing to start, this is why.

**Role mentions.** They never wake anyone. This reads as an agent ignoring you.

**A stale session.** An agent started before a config change keeps the old
config. Restart it after editing `config.json`, and confirm from the transcript
rather than from the fact that replies still work.

**An MCP server registered in the wrong scope.** The agent simply does not have
the tool. It will improvise around the gap rather than report a missing server,
so this reads as the agent being bad at the task.

## Logs

`log_dir` in `fleet.json`, default `$FLEET_ROOT/logs`. The watcher writes a
heartbeat with request and error counts every 15 minutes, which is the fastest
way to tell "the link is down" from "nobody said anything".
