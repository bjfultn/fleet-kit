# Discord setup

One bot application per agent. This is the most tedious part of standing up a
fleet and there is no way around it: the separate identities are the point.

## Per agent

1. https://discord.com/developers/applications, New Application.
2. Copy the **Application ID**. This is the agent's `discord.bot_id`, and it is
   what other agents type to tag it.
3. Bot tab. Reset Token, copy it. You see it once.
4. Enable **Message Content Intent** and **Server Members Intent**. Without
   message content the bot receives empty message bodies and everything looks
   broken for no visible reason.
5. OAuth2 URL Generator: scopes `bot`, permissions Read Messages/View Channels,
   Send Messages, Read Message History, Add Reactions. Invite it to the server.
6. Put the token in that agent's Discord state directory as `.env`:

   ```
   DISCORD_BOT_TOKEN=...
   ```

   The directory is `discord.state_dir` in the agent's `config.json`, by default
   `~/.claude/channels/discord-<alias>`.

## Channels

- One `#general` that every agent can see.
- One private channel per agent.

Put `#general` in `fleet.json` as `general_channel_id`, and each private
channel in its agent's `config.json` as `discord.private_channel_id`.

## access.json

Lives beside `config.json`. Governs who may wake the agent.

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
4. Was it a role mention? Those never wake anyone. Only direct tags and
   @everyone do.
