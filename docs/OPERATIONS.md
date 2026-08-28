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

## Logs

`log_dir` in `fleet.json`, default `$FLEET_ROOT/logs`. The watcher writes a
heartbeat with request and error counts every 15 minutes, which is the fastest
way to tell "the link is down" from "nobody said anything".
