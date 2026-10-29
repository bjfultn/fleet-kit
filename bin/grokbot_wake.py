"""Waking Grok Bot agents: the webhook side of the mention watcher.

A Claude agent is woken by typing into its tmux pane. A Grok Bot agent
(`platform: "grok-bot"` in its config.json) is hosted by xAI and has no pane,
so the watcher POSTs a JSON body to a routine webhook instead:

    {"messages": [{"channel_id", "message_id", "author", "author_id",
                   "text", "ts"}, ...]}

The body is always a list, even for one message, so a burst that was held and
coalesced looks the same as a single wake to the receiving routine.

This module is import-safe on purpose. mention-watcher.py reads its env and
loads every agent at import time, so none of the logic that needs a test lives
there. Stdlib only.

The text of a Discord message reaches a third party's servers from here, so the
allowlist check (grokbot_targets) is not optional and does not look at
MENTION_WATCHER_ENFORCE. A Claude agent's dry-run mode is for tuning; this one
has nothing to tune.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

PLATFORM = "grok-bot"

TEXT_LIMIT = 1500          # characters of message text sent to the webhook
WEBHOOK_TIMEOUT = 10       # seconds
IN_FLIGHT_SECONDS = 120    # one wake per agent per this long; the rest coalesce
HOP_CAP = 4                # bot-authored wakes per thread per hour
HOP_WINDOW = 3600
MAX_HELD = 20              # held payloads per agent; oldest dropped beyond this
QUIET_START_HOUR = 22      # bot-authored wakes held 22:00 to 07:00 local
QUIET_END_HOUR = 7
QUIET_TZ = "America/Denver"


class FleetConfigError(Exception):
    """An agent config the watcher cannot work with. The caller exits on it."""


# ── Text handling ───────────────────────────────────────────────────────────

# Discord's own markup we must not mangle: <@id>, <@&id>, <#id>, <:name:id>,
# <a:name:id>, <t:stamp>. Anything else starting with "<" gets neutered so a
# message body cannot close a tag and inject top-level text. The watcher uses
# this same function for its tmux wakes.
_MENTION_PREFIXES = ("@", "#", ":", "t:", "a:")


def sanitize(content: str) -> str:
    out = []
    for i, ch in enumerate(content):
        if ch == "<" and not content[i + 1:].startswith(_MENTION_PREFIXES):
            out.append("&lt;")
        else:
            out.append(ch)
    return "".join(out)


def thread_id(msg: dict) -> str:
    """The message this one replies to, else the message itself."""
    ref = msg.get("message_reference") or {}
    return str(ref.get("message_id") or msg.get("id") or "")


def build_payload(msg: dict, channel_id: str) -> dict:
    author = msg.get("author") or {}
    # Sanitize first, cut second: the cap is then a hard bound on what is sent.
    text = sanitize(msg.get("content") or "")[:TEXT_LIMIT]
    return {
        "channel_id": str(channel_id),
        "message_id": str(msg.get("id", "")),
        "author": author.get("username", "unknown"),
        "author_id": str(author.get("id", "")),
        "text": text,
        "ts": msg.get("timestamp", ""),
    }


# ── Who may wake the agent ──────────────────────────────────────────────────

def grokbot_targets(msg: dict, channel_id: str, agent: dict, ask_ids: set) -> bool:
    """Should this message wake this Grok Bot agent?

    Every condition must hold:
      - the author is not the agent itself;
      - the channel is one the agent's own access.json lists under groups;
      - the author is in that file's allowFrom (humans) or allowBots (bots);
      - the message tags the agent by user mention (not @everyone/@here, not a
        role), or was posted in the agent's private channel;
      - it is not a reply to one of the agent's own asks. Those replies are
        answers to a request the agent made, collected through the broker's
        ticket file; waking on them would start a ping loop.

    Only the top-level allowFrom is honoured, not a per-group allowFrom: the
    agent record is deliberately flat, and the stricter reading fails closed.
    """
    author_id = str((msg.get("author") or {}).get("id", ""))
    bot_id = str(agent.get("bot_id", ""))
    if not author_id or author_id == bot_id:
        return False
    if str(channel_id) not in agent.get("allowed_channels", ()):
        return False
    if author_id not in agent.get("allow_from", ()) and author_id not in agent.get("allow_bots", ()):
        return False

    tagged = any(str(m.get("id", "")) == bot_id for m in (msg.get("mentions") or []))
    private = str(agent.get("private_channel_id", ""))
    in_private = bool(private) and str(channel_id) == private
    if not (tagged or in_private):
        return False

    ref = (msg.get("message_reference") or {}).get("message_id")
    if ref is not None and str(ref) in ask_ids:
        return False
    return True


# ── The broker's ticket file (read-only) ────────────────────────────────────

class TicketReader:
    """Set of ask message ids from the broker's tickets file.

    Missing or unreadable is an empty set, logged once per failure streak so a
    broker that is down does not fill the log every poll. The hop cap in
    WakeGate is what stops a loop while the file is unavailable.
    """

    def __init__(self, path: str, log):
        self.path = path
        self.log = log
        self._warned = False
        self._mtime = None
        self._ids: set = set()

    def ask_ids(self) -> set:
        if not self.path:
            self._fail("no grokbot.tickets_file configured")
            return set()
        try:
            mtime = os.path.getmtime(self.path)
            if mtime != self._mtime:
                with open(self.path) as fh:
                    data = json.load(fh)
                self._ids = {str(t["ask_message_id"]) for t in data.get("tickets", [])
                             if t.get("ask_message_id")}
                self._mtime = mtime
            self._warned = False
            return set(self._ids)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._mtime = None
            self._ids = set()
            self._fail(f"tickets file {self.path} unreadable ({type(exc).__name__}), "
                       "treating as no asks")
            return set()

    def _fail(self, why: str):
        if not self._warned:
            self._warned = True
            self.log(f"  {why}")


# ── Throttling ──────────────────────────────────────────────────────────────

def in_quiet_hours(now_local: datetime) -> bool:
    h = now_local.hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def now_quiet_tz() -> datetime:
    """Current time where quiet hours are measured. Raises if tzdata is absent."""
    return datetime.now(ZoneInfo(QUIET_TZ))


class WakeGate:
    """Decides when a wake may go out, and holds what may not.

    clock is a callable returning seconds (time.monotonic in the watcher; a
    fake in tests). quiet_hours is the set of aliases whose bot-authored wakes
    are held overnight; messages from humans are never held for that reason.

    Protocol for the caller, per message that already passed grokbot_targets:

        verdict = gate.admit(alias, thread_id, is_bot, now_local)
        if verdict == "drop": skip it
        gate.hold(alias, payload, is_bot)
        if verdict == "send":
            batch = gate.drain(alias, now_local, force=True)
            if not send(batch): gate.failed(alias)

    and once per poll, for every agent: batch = gate.drain(alias, now_local).
    """

    def __init__(self, clock, quiet_hours=(), log=None):
        self.clock = clock
        self.quiet = set(quiet_hours)
        self.log = log or (lambda s: None)
        self._last_send: dict = {}
        self._held: dict = {}       # alias -> list[(payload, is_bot)]
        self._batch: dict = {}      # alias -> entries of the last drain
        self._hops: dict = {}       # (alias, thread) -> deque[timestamp]

    def _in_flight(self, alias) -> bool:
        last = self._last_send.get(alias)
        return last is not None and self.clock() - last < IN_FLIGHT_SECONDS

    def _quiet(self, alias, now_local) -> bool:
        return alias in self.quiet and now_local is not None and in_quiet_hours(now_local)

    def admit(self, alias, thread, is_bot_author, now_local) -> str:
        now = self.clock()
        if is_bot_author:
            key = (alias, thread)
            hops = self._hops.setdefault(key, deque())
            while hops and now - hops[0] >= HOP_WINDOW:
                hops.popleft()
            if len(hops) >= HOP_CAP:
                self.log(f"  {alias}: DROP bot wake, thread {thread} hit the hop cap "
                         f"({HOP_CAP}/hour)")
                return "drop"
            hops.append(now)
            if len(self._hops) > 500:
                self._sweep(now)
            if self._quiet(alias, now_local):
                return "hold"
        if self._in_flight(alias):
            return "hold"
        self._last_send[alias] = now
        return "send"

    def _sweep(self, now):
        for k in [k for k, h in self._hops.items() if not h or now - h[-1] >= HOP_WINDOW]:
            del self._hops[k]

    def hold(self, alias, payload, is_bot=False):
        q = self._held.setdefault(alias, [])
        q.append((payload, bool(is_bot)))
        if len(q) > MAX_HELD:
            del q[:len(q) - MAX_HELD]
            self.log(f"  {alias}: held queue full, dropped the oldest")

    def drain(self, alias, now_local=None, force=False) -> list:
        """Held payloads that may go now, oldest first; marks a send.

        force=True is for the caller that was just told "send": a message that
        is about to wake the agent takes everything held with it. Otherwise
        nothing is released while a wake is in flight, and during quiet hours
        only the human-authored entries are.
        """
        q = self._held.get(alias) or []
        if not q:
            return []
        if not force and self._in_flight(alias):
            return []
        if not force and self._quiet(alias, now_local):
            take = [e for e in q if not e[1]]
            keep = [e for e in q if e[1]]
        else:
            take, keep = list(q), []
        if not take:
            return []
        self._held[alias] = keep
        self._batch[alias] = take
        self._last_send[alias] = self.clock()
        return [p for p, _ in take]

    def failed(self, alias):
        """The last drained batch did not go out: put it back, keep the backoff."""
        entries = self._batch.pop(alias, [])
        if entries:
            q = self._held.setdefault(alias, [])
            q[:0] = entries
            if len(q) > MAX_HELD:
                del q[:len(q) - MAX_HELD]


# ── The webhook call ────────────────────────────────────────────────────────

def load_webhook(pass_key: str, runner=subprocess.run, log=None):
    """(url, key) from the pass entry: line 1 is the URL, line 2 the key.

    None if pass fails or the entry is malformed. Never logs the contents.
    """
    say = log or (lambda s: None)
    try:
        res = runner(["pass", "show", pass_key], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"webhook secret unavailable ({type(exc).__name__})")
        return None
    if res.returncode != 0:
        if res.returncode == 2:
            say(f"secret store locked (pass rc=2), cannot read {pass_key}")
        else:
            say(f"webhook secret unavailable (pass rc={res.returncode}) for {pass_key}")
        return None
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        say(f"webhook entry {pass_key} needs the URL on line 1 and the key on line 2")
        return None
    url, key = lines[0], lines[1]
    if not url.startswith("https://"):
        say(f"webhook entry {pass_key}: URL is not https, refusing to send a bearer key")
        return None
    return url, key


def wake_grokbot(alias, pass_key, body, log, opener=urllib.request.urlopen,
                 runner=subprocess.run, context=None) -> bool:
    """POST body to the agent's routine webhook. True on 2xx.

    The URL and key are secrets: neither is ever written to the log.
    """
    secret = load_webhook(pass_key, runner, log=lambda s: log(f"  wake {alias} skipped: {s}"))
    if secret is None:
        return False
    url, key = secret
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "MentionWatcher/1.0"})
    kwargs = {"timeout": WEBHOOK_TIMEOUT}
    if context is not None:
        kwargs["context"] = context
    try:
        resp = opener(req, **kwargs)
        status = getattr(resp, "status", None) or resp.getcode()
        try:
            resp.close()
        except Exception:
            pass
    except urllib.error.HTTPError as exc:
        exc.close()
        log(f"Failed to wake {alias} (webhook): HTTP {exc.code}")
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None)
        detail = type(reason).__name__ if reason is not None else type(exc).__name__
        log(f"Failed to wake {alias} (webhook): {type(exc).__name__}/{detail}")
        return False
    if 200 <= status < 300:
        log(f"Woke {alias} (webhook)")
        return True
    log(f"Failed to wake {alias} (webhook): HTTP {status}")
    return False


# ── Loading agents from disk ────────────────────────────────────────────────

def _read_access(path):
    """(allowed_channels, allow_from, allow_bots) from an access.json, or None."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        groups = data.get("groups") or {}
        return (
            {str(k) for k in groups},
            {str(x) for x in (data.get("allowFrom") or [])},
            {str(x) for x in (data.get("allowBots") or [])},
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def refresh_access(agent: dict, log) -> None:
    """Re-read the agent's access.json when it changed. Fails closed.

    An unreadable file empties the allowlists (logged once) rather than keeping
    the last good copy: a revoked grant must not outlive its file.
    """
    path = agent.get("access_path")
    if not path:
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime is not None and mtime == agent.get("_access_mtime"):
        return
    parsed = _read_access(path) if mtime is not None else None
    if parsed is None:
        if not agent.get("_access_warned"):
            log(f"  access.json for {agent.get('alias')} unreadable, allowing nobody")
        agent["_access_warned"] = True
        agent["_access_mtime"] = None
        parsed = (set(), set(), set())
    else:
        agent["_access_warned"] = False
        agent["_access_mtime"] = mtime
    agent["allowed_channels"], agent["allow_from"], agent["allow_bots"] = parsed


def load_fleet_agents(agents_dir: str, general_channel_id: str = ""):
    """Build the tmux map, the grok-bot map and the watch list from agent configs.

    Returns (agents, grok_agents, channels):
      agents       bot id -> tmux window, for Claude agents
      grok_agents  alias -> agent record for `platform: "grok-bot"` agents
      channels     every channel to poll, de-duplicated, in discovery order

    Deriving these from the configs means an agent that exists on disk is an
    agent that gets watched; the old hand-kept literals drifted, and a drifted
    entry fails silently. An agent with no discord.bot_id cannot be woken (and
    its own messages could not be recognised), so that is an error here rather
    than a mystery at 3am.

    Grok-bot agents are kept out of `agents` so the tmux paths never see them.
    Their private channel and every channel in their access.json groups join
    the watch list.
    """
    agents: dict = {}
    groks: dict = {}
    channels: list = []
    missing: list = []

    def watch(cid):
        cid = str(cid or "").strip()
        if cid and cid not in channels:
            channels.append(cid)

    watch(general_channel_id)

    if not os.path.isdir(agents_dir):
        raise FleetConfigError(f"no agents directory at {agents_dir}")

    for name in sorted(os.listdir(agents_dir)):
        cfg_path = os.path.join(agents_dir, name, "config.json")
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError) as exc:
            raise FleetConfigError(f"cannot read {cfg_path}: {exc}")

        window = cfg.get("alias") or cfg.get("name", name).lower()
        discord_cfg = cfg.get("discord", {})
        bot_id = str(discord_cfg.get("bot_id") or "").strip()
        private = str(discord_cfg.get("private_channel_id") or "").strip()

        if cfg.get("platform") == PLATFORM:
            if not bot_id:
                missing.append(name)
            gb = cfg.get("grokbot") or {}
            state_dir = str(discord_cfg.get("state_dir") or "").strip()
            agent = {
                "alias": window,
                "bot_id": bot_id,
                "private_channel_id": private,
                "allowed_channels": set(),
                "allow_from": set(),
                "allow_bots": set(),
                "access_path": (os.path.join(os.path.expanduser(state_dir), "access.json")
                                if state_dir else ""),
                "webhook_pass_key": str(gb.get("webhook_pass_key") or "").strip(),
                "tickets_file": str(gb.get("tickets_file") or "").strip(),
                "quiet_hours": bool(gb.get("quiet_hours")),
            }
            refresh_access(agent, lambda s: None)
            groks[window] = agent
            watch(private)
            watch(discord_cfg.get("general_channel_id"))
            for cid in sorted(agent["allowed_channels"]):
                watch(cid)
            continue

        if bot_id:
            agents[bot_id] = window
        else:
            missing.append(name)

        watch(private)
        # An agent may also name a general channel of its own; honour it so a
        # fleet can be split across more than one shared room.
        watch(discord_cfg.get("general_channel_id"))

    if missing:
        raise FleetConfigError(
            "these agents have no discord.bot_id in config.json: "
            + ", ".join(missing)
            + "\nWithout it they can never be woken by a mention. Add the bot's "
            "application id (Discord Developer Portal -> your app -> Application ID)."
        )
    if not agents and not groks:
        raise FleetConfigError(f"no agents found under {agents_dir}")

    return agents, groks, channels
