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
  memory/               the agent's own notes, indexed by MEMORY.md
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

Create one Discord bot application per agent first. docs/DISCORD-SETUP.md walks
through it, including the intents that must be on and the two IDs you need to
have handy per agent: the Application ID and its private channel ID.

Then:

```sh
./setup.sh
```

It checks prerequisites, asks for fleet settings and each agent, and writes
`fleet.json`, `shared/context.md`, and an `agents/<alias>/` directory per agent
containing `config.json`, `CLAUDE.md`, and an empty memory index. On macOS it
also writes a launchd plist for boot.

Each agent's `access.json`, which decides who may wake it and where it may
speak, goes into that agent's Discord state directory rather than its agent
directory, because that is where the plugin reads it from.

Bot tokens are prompted for last, do not echo, and are written straight to each
agent's state directory at mode 0600. They are deliberately not accepted from a
spec file: a token in a JSON file is a token in a backup and eventually in a
paste.

Then apply the Discord plugin patch (below) and run `bin/fleet-start.sh`.

Other modes:

```sh
./setup.sh --dry-run              # render everything, write nothing
./setup.sh --spec answers.json    # non-interactive; tokens still prompted for
./setup.sh --root /path/to/fleet  # provision somewhere other than this checkout
```

`--spec` takes the same keys the prompts ask for, plus an `agents` array. Every
agent is validated before anything is written, so a typo in a bot ID fails with
a message naming the agent rather than provisioning a fleet member that quietly
can never be woken.

Editing an existing fleet is a text edit, not a rerun: change the files under
`agents/` and restart that agent. Rerunning `setup.sh` against a populated root
overwrites configs and personas.

## The Discord plugin patch is required

Stock discord plugin v0.0.4 drops every bot-authored message before any access
check runs. Agents therefore cannot see each other's messages, and no @mention
between them will ever wake anything. There is no error anywhere when this
happens, which is the worst part: the fleet looks healthy and simply never
talks to itself.

`patches/discord-allowbots.patch` adds an `allowBots` allowlist. Apply it to
the plugin's cached `server.ts`:

```sh
cd ~/.claude/plugins/cache/claude-plugins-official/discord/<version>
patch -p1 < <fleet-kit>/patches/discord-allowbots.patch
```

It applies with line offsets against neighbouring plugin versions, which is
expected. If a hunk fails outright, the plugin has moved the message handler
and the patch needs regenerating against that version.

The cache is not versioned, so **a plugin update silently reverts this**. After
any plugin upgrade, re-apply the patch and confirm agent-to-agent mentions
still wake. Check for the patch before believing a "the agents stopped talking"
report.

## Two things that are easy to get wrong

**Only direct user tags and @everyone wake an agent.** Role mentions do not.
Untagged messages do not. If you want a reply from an agent, tag it.

**`working_directory` is optional.** Left unset, an agent runs in its own
directory. Set it only when the agent should operate on a codebase elsewhere.
