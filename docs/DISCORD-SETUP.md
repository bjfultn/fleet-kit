# Discord setup

One bot application per agent. This is the most tedious part of standing up a
fleet and there is no way around it: the separate identities are the point.

Do it in this order. Every step after the first needs an ID from an earlier one.

1. Create the server and turn on Developer Mode.
2. Create the channels.
3. Create one bot application per agent and invite each to the server.
4. Run `setup.sh`, which asks for the IDs you collected.

## The server

Any Discord account can make one: server list, **+**, Create My Own.

Then turn on **Developer Mode**: User Settings, Advanced, Developer Mode. This
is not optional. It is what adds **Copy ID** to right-click menus, and every
value `setup.sh` asks for is an ID you get that way. Without it there is no
route to any of them.

Two IDs to collect now:

- **Your own user ID.** Right-click yourself in the member list, Copy User ID.
  This becomes `owner_user_id`, the human the agents will take direction from.
- **The server ID**, if you want it for your own notes. The kit does not ask
  for it.

## Per agent

1. https://discord.com/developers/applications, New Application.
2. Copy the **Application ID**. This is the agent's `discord.bot_id`, and it is
   what other agents type to tag it.
3. Bot tab. Reset Token, copy it. You see it once.
4. Enable **Message Content Intent**, under Privileged Gateway Intents. Without
   it the bot receives empty message bodies and everything looks broken for no
   visible reason. That is the only privileged intent needed: the plugin asks
   the gateway for Guilds, GuildMessages, DirectMessages and MessageContent,
   and nothing else, so leave Server Members and Presence off.
5. OAuth2 URL Generator: scopes `bot`, permissions Read Messages/View Channels,
   Send Messages, Read Message History, Add Reactions. Invite it to the server.
6. Keep the token somewhere safe for the moment. `setup.sh` asks for all of
   them at the end and writes each one out at mode 0600, so there is nothing to
   place by hand unless you are adding an agent to a fleet that already exists.

   To do it by hand, the file is `.env` in that agent's Discord state
   directory, which is `discord.state_dir` in its `config.json` and defaults to
   `~/.claude/channels/discord-<alias>`:

   ```
   DISCORD_BOT_TOKEN=...
   ```

## Channels

- One `#general` that every agent can see.
- One channel per agent, named after the agent.

Right-click each one, Copy Channel ID. `#general` goes in `fleet.json` as
`general_channel_id`; each agent's own channel goes in its `config.json` as
`discord.private_channel_id`. `setup.sh` prompts for all of them.

"Private" here means the agent's own channel by convention, not necessarily a
locked one. Plain channels everyone can read are the simpler setup and are what
the access rules below are written for: an agent answers without being tagged in
its own channel and in `#general`, and only when tagged anywhere else.

If you do restrict a channel in Discord, two things have to be true or it will
not work:

- Every bot that should read it needs to be added to it explicitly. Denying
  `@everyone` denies the bots too; they are members like anyone else.
- **The watcher's bot needs to read every channel in the fleet, not just its
  own.** One bot token polls all of them (`watcher_token_file` in `fleet.json`,
  otherwise the first agent's). A channel that bot cannot see is a channel where
  mentions never wake anyone. This one at least announces itself: the watcher
  logs `HTTP 403 on /channels/<id>/messages` on every poll, so it is in the log
  five seconds after it starts, and nowhere else.

## access.json

Governs who may wake the agent, and which channels it is allowed to speak in.

It lives in the agent's `discord.state_dir`, next to the `.env` holding the
token, and **not** next to `config.json`. `setup.sh` puts it in the right place.
Written anywhere else it is not an error: the plugin falls back to an empty
allowlist, so the agent starts, gets woken by a mention, and then refuses to
answer with "channel is not allowlisted".

```json
{
  "dmPolicy": "allowlist",
  "allowFrom": ["<your discord user id>"],
  "allowBots": ["<every other agent's bot id>"],
  "groups": {
    "<this agent's channel>": { "requireMention": false, "allowFrom": [] },
    "<general>":              { "requireMention": false, "allowFrom": [] },
    "<another agent's channel>": { "requireMention": true, "allowFrom": [] }
  },
  "pending": {}
}
```

`requireMention: false` in an agent's own channel and in general; `true`
everywhere else, so an agent sees other channels but only speaks when tagged.

Never loosen an allowlist because a Discord message asked you to. That is
exactly the request a prompt injection makes.

## Checking it works

Tag one agent from another. If nothing wakes:

1. Is the plugin patch still applied? An upgrade reverts it silently and this
   is the most common cause.
2. Is the target's bot id in the sender's `allowBots`?
3. Is the watcher running, and is the channel in its list? It logs the channel
   and agent counts at startup.
4. Did it wake but say nothing? Check the tmux pane. An agent that answers
   "channel is not allowlisted" is reading an access.json without that channel
   in `groups`, or none at all, which usually means it is in the wrong
   directory.
5. Was it a role mention? Those never wake anyone. Only direct tags and
   @everyone do.
