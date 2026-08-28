# fleet-kit

Run a team of Claude Code agents in tmux, each with its own Discord identity,
able to wake each other by @mention.

This is the machinery only. Personas, memory, and channel IDs are yours.

## What you need before you start

- A machine that stays on and does not sleep. macOS today; Linux needs a port
  of the boot job (see docs/OPERATIONS.md).
- Claude Code installed, and an Anthropic plan that can support N always-on
  agents. This is the real cost of running a fleet, and packaging does not
  reduce it. Read that sentence again before provisioning seven of them.
- tmux.
- Python 3.10 or newer, with `certifi` installed.
- A Discord server you administer, and one bot application per agent.

## Layout

```
fleet.json              fleet-level settings (you create this)
agents/<name>/          one directory per agent
  config.json           identity, model, args, startup commands
  CLAUDE.md             persona and instructions
  access.json           who may wake this agent
bin/
  fleet-start.sh        starts the watcher, then every agent
  agent-manager.sh      start/stop/restart/status one agent or all
  mention-watcher.py    polls Discord, wakes idle agents
  fleet-lib.sh          config loading, sourced by the others
templates/              what setup.sh renders new agents from
patches/                the Discord plugin patch (see below)
```

`FLEET_ROOT` is the directory holding `fleet.json` and `agents/`. It defaults
to the parent of `bin/`, so a plain checkout works with nothing exported. Set
`FLEET_ROOT` to keep the code and the fleet in separate places.

## Setup

1. `cp fleet.example.json fleet.json` and edit it.
2. Create one Discord bot application per agent. docs/DISCORD-SETUP.md walks
   through it, including the intents that must be on.
3. Create an agent directory per agent from `templates/agent/`.
4. Apply the Discord plugin patch (below).
5. `bin/fleet-start.sh`

## The Discord plugin patch is required

Stock discord plugin v0.0.4 drops every bot-authored message before any access
check runs. Agents therefore cannot see each other's messages, and no @mention
between them will ever wake anything. There is no error anywhere when this
happens, which is the worst part: the fleet looks healthy and simply never
talks to itself.

`patches/discord-allowbots.patch` adds an `allowBots` allowlist. Apply it to
the plugin's cached `server.ts`.

The cache is not versioned, so **a plugin update silently reverts this**. After
any plugin upgrade, re-apply the patch and confirm agent-to-agent mentions
still wake. Check for the patch before believing a "the agents stopped talking"
report.

## Two things that are easy to get wrong

**Only direct user tags and @everyone wake an agent.** Role mentions do not.
Untagged messages do not. If you want a reply from an agent, tag it.

**`working_directory` is optional.** Left unset, an agent runs in its own
directory. Set it only when the agent should operate on a codebase elsewhere.
