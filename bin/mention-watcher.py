#!/usr/bin/env python3
"""
Mention Watcher — watches Discord for agent mentions and wakes idle agents.

Polls Discord REST API for recent messages, checks for @mentions or @everyone,
and sends a keystroke to the target agent's tmux pane if they're idle.

No LLM calls. Stdlib only apart from certifi, which is required rather than
optional: the python builds on this box ship an empty system CA store, so
without it there is nothing to verify Discord's certificate against.

Needs Python 3.10+ for the `X | None` type syntax used below.
"""

import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone

# This file annotates with `X | None`, which 3.9 parses and then raises a
# TypeError on at def time. It works today only because start-all-agents.sh
# exports PATH with /usr/local/bin (3.14) ahead of /usr/bin (3.9.6), which is
# an implicit coupling nobody wrote down. Say so out loud instead of dying
# with "unsupported operand type(s) for |" forty lines later.
if sys.version_info < (3, 10):
    sys.exit(
        "mention-watcher: needs Python 3.10+ for `X | None` annotations, got "
        f"{sys.version.split()[0]} at {sys.executable}.\n"
        "Set \"python\" in fleet.json to a 3.10+ interpreter. On macOS the system\n"
        "/usr/bin/python3 is 3.9 and will not run this; Homebrew's is usually\n"
        "/usr/local/bin/python3 or /opt/homebrew/bin/python3."
    )

# macOS python builds ship an empty system CA store. Verified on this box:
# ssl.create_default_context().get_ca_certs() returns 0 certs, so the plain
# default context cannot verify anything. certifi is therefore a hard
# requirement, not a nicety.
#
# The old fallback answered a missing certifi by setting check_hostname=False
# and verify_mode=CERT_NONE, which silently sent the bot token over a
# connection nobody had authenticated. Refuse to start instead.
try:
    import certifi
except ImportError:
    sys.exit(
        "mention-watcher: certifi is required (python3 -m pip install certifi).\n"
        "Refusing to run with certificate verification disabled."
    )
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# ── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
POLL_INTERVAL = 5  # seconds

# ── Fleet config ────────────────────────────────────────────────────────────
# FLEET_ROOT holds fleet.json and agents/. bin/ sits directly under it, so a
# plain checkout needs no environment set; export FLEET_ROOT to point elsewhere.

FLEET_ROOT = os.environ.get(
    "FLEET_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
try:
    with open(os.path.join(FLEET_ROOT, "fleet.json")) as fh:
        FLEET = json.load(fh)
except FileNotFoundError:
    sys.exit(
        f"mention-watcher: no fleet.json at {FLEET_ROOT}/fleet.json.\n"
        "Copy fleet.example.json to fleet.json, or export FLEET_ROOT."
    )

TMUX_SESSION = FLEET.get("tmux_session", "agents")
FLEET_TZ = FLEET.get("timezone", "")

AGENTS_DIR = os.path.expanduser(FLEET.get("agents_dir") or os.path.join(FLEET_ROOT, "agents"))
if not os.path.isabs(AGENTS_DIR):
    AGENTS_DIR = os.path.join(FLEET_ROOT, AGENTS_DIR)


def _load_fleet_agents() -> tuple[dict[str, str], list[str]]:
    """Build the bot-id -> tmux-window map and the watch list from agent configs.

    Both used to be literal dicts maintained by hand, which meant every id
    existed in two places: here and the agent's own config.json. They drifted,
    and a drifted entry fails silently -- the watcher simply never wakes that
    agent, with nothing in any log to say why. Deriving them means an agent that
    exists on disk is an agent that gets watched.

    An agent with no discord.bot_id cannot be woken (we would not recognise its
    own messages to avoid self-wakes), so that is a hard error at startup rather
    than a mystery at 3am.
    """
    agents: dict[str, str] = {}
    channels: list[str] = []
    missing: list[str] = []

    general = str(FLEET.get("general_channel_id") or "").strip()
    if general:
        channels.append(general)

    if not os.path.isdir(AGENTS_DIR):
        sys.exit(f"mention-watcher: no agents directory at {AGENTS_DIR}")

    for name in sorted(os.listdir(AGENTS_DIR)):
        cfg_path = os.path.join(AGENTS_DIR, name, "config.json")
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError) as exc:
            sys.exit(f"mention-watcher: cannot read {cfg_path}: {exc}")

        window = cfg.get("alias") or cfg.get("name", name).lower()
        discord_cfg = cfg.get("discord", {})

        bot_id = str(discord_cfg.get("bot_id") or "").strip()
        if bot_id:
            agents[bot_id] = window
        else:
            missing.append(name)

        private = str(discord_cfg.get("private_channel_id") or "").strip()
        if private and private not in channels:
            channels.append(private)

        # An agent may also name a general channel of its own; honour it so a
        # fleet can be split across more than one shared room.
        other = str(discord_cfg.get("general_channel_id") or "").strip()
        if other and other not in channels:
            channels.append(other)

    if missing:
        sys.exit(
            "mention-watcher: these agents have no discord.bot_id in config.json: "
            + ", ".join(missing)
            + "\nWithout it they can never be woken by a mention. Add the bot's "
            "application id (Discord Developer Portal -> your app -> Application ID)."
        )
    if not agents:
        sys.exit(f"mention-watcher: no agents found under {AGENTS_DIR}")

    return agents, channels


AGENTS, CHANNELS = _load_fleet_agents()

ENFORCE = os.environ.get("MENTION_WATCHER_ENFORCE", "0") == "1"

# Track the last message ID we've processed per channel
last_seen: dict[str, str] = {}

# ── Access control ──────────────────────────────────────────────────────────
# Waking an agent types text straight into a tool-capable pane, so the author of
# a mention has to be someone that agent already trusts. Source of truth is the
# discord plugin's own access.json (allowFrom = humans, allowBots = bots), so
# there is one allowlist to maintain rather than two that drift apart.
#
# ENFORCE=0 (default) logs what WOULD be dropped without changing behaviour.
# Flip to 1 only after reading the log. See MENTION_WATCHER_ENFORCE below.


# window name → (access.json path, mtime, parsed) — reloaded when the file changes
_access_cache: dict[str, tuple[str, float, dict]] = {}


def _access_path(window_name: str) -> str | None:
    """Resolve an agent's access.json from its config.json discord.state_dir."""
    cfg = os.path.join(AGENTS_DIR, window_name, "config.json")
    try:
        with open(cfg) as fh:
            state_dir = json.load(fh).get("discord", {}).get("state_dir", "")
    except Exception:
        return None
    if not state_dir:
        return None
    return os.path.join(os.path.expanduser(state_dir), "access.json")


def load_access(window_name: str) -> dict | None:
    """Return the agent's parsed access.json, re-reading it when it changes."""
    path = _access_cache.get(window_name, (None,))[0] or _access_path(window_name)
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _access_cache.get(window_name)
    if cached and cached[1] == mtime:
        return cached[2]
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception as e:
        log(f"  access.json unreadable for {window_name}: {e}")
        return None
    _access_cache[window_name] = (path, mtime, data)
    return data


def is_allowed(window_name: str, author_id: str, channel_id: str) -> tuple[bool, str]:
    """Is this author permitted to wake this agent? Returns (allowed, reason)."""
    access = load_access(window_name)
    if access is None:
        # Fail closed only when enforcing — a missing file must not silently
        # deafen an agent during the dry run.
        return (not ENFORCE, "no access.json")
    groups = access.get("groups") or {}
    if channel_id not in groups:
        # The plugin would not deliver this channel to the agent at all, so
        # waking them for it would be a channel the watcher alone can reach.
        return False, "channel not in groups"
    if author_id in (access.get("allowFrom") or []):
        return True, "allowFrom"
    if author_id in (access.get("allowBots") or []):
        return True, "allowBots"
    if author_id in (groups[channel_id].get("allowFrom") or []):
        return True, "group allowFrom"
    return False, "not in allowlist"


# Discord's own markup we must not mangle: <@id>, <@&id>, <#id>, <:name:id>,
# <a:name:id>, <t:stamp>. Anything else starting with "<" gets neutered so a
# message body cannot close the <channel> tag and inject top-level text.
_MENTION_PREFIXES = ("@", "#", ":", "t:", "a:")


def oldest_first(messages: list) -> list:
    """Sort Discord messages oldest-first by snowflake id.

    Snowflakes are numeric and arrive as strings. Sorting them as strings
    orders by digit count first, so a shorter id sorts before a longer one
    no matter which is actually older. The last_seen comparison below
    already uses int(); this keeps the two consistent.
    """
    return sorted(messages, key=lambda m: int(m["id"]))


def attr(value: str) -> str:
    """Escape a value for use inside a double-quoted attribute.

    The body goes through sanitize(). The attributes went through nothing, so
    a username holding a double quote could close user=" and write its own
    text at the top level of the prompt, which is the same injection sanitize()
    exists to stop. Discord's username rules make that unlikely today; not
    relying on unlikely is the point of this change.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def sanitize(content: str) -> str:
    out = []
    for i, ch in enumerate(content):
        if ch == "<" and not content[i + 1:].startswith(_MENTION_PREFIXES):
            out.append("&lt;")
        else:
            out.append(ch)
    return "".join(out)

# ── Discord REST API ────────────────────────────────────────────────────────
# One keep-alive connection, not a fresh TLS handshake per request. The old
# urlopen-per-call path opened ~100 connections a minute (8 channels every 5s)
# and macOS answered a slice of them with ENETDOWN — that churn is where the
# ~500 errors/hour in this log came from, and it is not a network fault.

API_HOST = "discord.com"
CONN_MAX_AGE = 600       # recycle the socket this often regardless of health
FAIL_STREAK_ALERT = 3    # consecutive failures before we call the link degraded
HEARTBEAT_EVERY = 900    # seconds between "still polling, here's the tally" lines

_conn: http.client.HTTPSConnection | None = None
_conn_opened = 0.0

STATS = {"requests": 0, "errors": 0, "retries": 0, "reconnects": 0}
_fail_streak = 0
_degraded_since: float | None = None
_degraded_errors_at_start = 0
_last_heartbeat = 0.0


def _close_conn():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


def _get_conn() -> http.client.HTTPSConnection:
    global _conn, _conn_opened
    if _conn is None or time.monotonic() - _conn_opened > CONN_MAX_AGE:
        _close_conn()
        _conn = http.client.HTTPSConnection(API_HOST, 443, timeout=10, context=SSL_CTX)
        _conn_opened = time.monotonic()
        STATS["reconnects"] += 1
    return _conn


def _record_ok():
    """Any answer from Discord, including a 4xx, proves the link works."""
    global _fail_streak, _degraded_since
    _fail_streak = 0
    if _degraded_since is not None:
        down = int(time.monotonic() - _degraded_since)
        n = STATS["errors"] - _degraded_errors_at_start
        log(f"NETWORK RECOVERED after {down}s ({n} failed requests during the outage)")
        _degraded_since = None


def _record_fail(path: str, err: BaseException | None):
    """Count every failure, log only the transition into a degraded state.

    A lone failed request is normal and self-heals on the next 5s poll, so
    logging each one is exactly what buried the real outages in this file.
    Only a sustained streak earns a line, plus one line when it clears.
    """
    global _fail_streak, _degraded_since, _degraded_errors_at_start
    _fail_streak += 1
    STATS["errors"] += 1
    if _degraded_since is None and _fail_streak >= FAIL_STREAK_ALERT:
        _degraded_since = time.monotonic()
        _degraded_errors_at_start = STATS["errors"] - _fail_streak
        log(f"NETWORK DEGRADED — {_fail_streak} consecutive failures, "
            f"last: GET {path} → {type(err).__name__}: {err}")


def heartbeat(force: bool = False):
    """Positive evidence that the poller looked and got answers.

    Without it the log has no healthy state at all, so silence is ambiguous
    between "nothing happened" and "the watcher is dead".
    """
    global _last_heartbeat
    now = time.monotonic()
    if not force and now - _last_heartbeat < HEARTBEAT_EVERY:
        return
    _last_heartbeat = now
    req, err = STATS["requests"], STATS["errors"]
    rate = (err / req * 100) if req else 0.0
    state = "DEGRADED" if _degraded_since is not None else "ok"
    log(f"heartbeat {state}: {req} requests, {err} errors ({rate:.1f}%), "
        f"{STATS['retries']} retries, {STATS['reconnects']} connections, "
        f"{len(last_seen)}/{len(CHANNELS)} channels tracked")


def discord_get(path: str) -> list | dict | None:
    """GET the Discord API over the pooled connection.

    A pooled socket the server has already closed fails on the NEXT request
    rather than at connect time, so the single silent retry on a fresh
    connection is part of the design, not padding — without it, pooling would
    raise the error rate instead of lowering it.
    """
    url = f"/api/v10{path}"
    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "User-Agent": "MentionWatcher/1.0",
        "Accept": "application/json",
    }
    STATS["requests"] += 1
    last_err: BaseException | None = None
    for attempt in (0, 1):
        try:
            conn = _get_conn()
            conn.request("GET", url, headers=headers)
            resp = conn.getresponse()
            body = resp.read()          # must drain before the socket is reusable
            if resp.will_close:
                _close_conn()
            _record_ok()
            if resp.status == 429:
                retry_after = json.loads(body or b"{}").get("retry_after", 5)
                log(f"Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
                return None
            if resp.status >= 400:
                log(f"HTTP {resp.status} on {path}")
                return None
            return json.loads(body)
        except (OSError, http.client.HTTPException) as e:
            last_err = e
            _close_conn()
            if attempt == 0:
                STATS["retries"] += 1
    _record_fail(path, last_err)
    return None


def fetch_messages(channel_id: str, after: str | None = None) -> list:
    path = f"/channels/{channel_id}/messages?limit=10"
    if after:
        path += f"&after={after}"
    result = discord_get(path)
    return result if isinstance(result, list) else []

# ── tmux helpers ────────────────────────────────────────────────────────────

# Claude Code draws a running turn as a spinner line carrying an elapsed time
# and a live token counter:
#     ✽ Envisioning… (1m 30s · ↓ 4.7k tokens)
# and a finished one in the past tense with no counter:
#     ✻ Brewed for 46m 13s
# Match on the structure rather than the wording. The glyph rotates and the
# verb is randomized flavor text, but "ellipsis, then an elapsed time in
# parentheses" is functional UI and should survive a reskin.
#
# Note the near miss this has to reject: an idle pane can still show
# "✻ Running scheduled task (Aug 26 9:49am)", which is a spinner glyph and a
# parenthesized value on one line. Requiring the ellipsis and a leading digit
# is what separates the two.
#
# This check used to look for the literal "esc to interrupt". Claude Code
# stopped printing that, so it never matched and the "❯" fallback below it
# decided every answer. The prompt box is drawn mid-turn too, so every pane
# read idle. If you change this, verify against a live mid-turn pane instead
# of a remembered format.
_SPINNER_RE = re.compile(r"(?:…|\.\.\.)\s*\(\d+\s*[hms]")


def should_wake_agent(window_name: str) -> bool:
    """Whether this process should deliver the mention itself.

    Standing down is only correct when we can see the agent is mid-turn.
    The Discord plugin hands a mention to a live session as an interjection,
    so waking it again would just duplicate the message.

    Every other answer means wake, including "the pane would not read". A
    pane that will not read usually means the agent is not running normally,
    and that is precisely when the plugin cannot deliver either. Calling that
    busy drops the mention for good, because last_seen has already advanced
    past it and nothing retries. Waking an agent that did not need it costs
    one redundant message. The other mistake is silent, which is why the
    unknown case fails toward delivery.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{TMUX_SESSION}:{window_name}", "-p"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            log(f"  {window_name}: pane unreadable (tmux rc={result.returncode}), waking anyway")
            return True
        if _SPINNER_RE.search(result.stdout):
            return False  # mid-turn, the plugin delivers this one
        return True
    except Exception as e:
        log(f"  {window_name}: pane unreadable ({e}), waking anyway")
        return True


def format_timestamp(iso_ts: str) -> str:
    """Convert Discord ISO timestamp to 'YYYY-MM-DD HH:MM MT (HH:MM UTC)' format."""
    try:
        dt_utc = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        # Mountain Time is UTC-6 (MDT) or UTC-7 (MST)
        # Use the system's timezone offset calculation
        import subprocess as _sp
        result = _sp.run(
            ["date", "-j", "-f", "%Y-%m-%dT%H:%M:%S%z",
             dt_utc.strftime("%Y-%m-%dT%H:%M:%S+0000"), "+%Y-%m-%d %H:%M %Z"],
            capture_output=True, text=True, timeout=2,
            env={**os.environ, **({"TZ": FLEET_TZ} if FLEET_TZ else {})}
        )
        if result.returncode == 0:
            local = result.stdout.strip()
            utc = dt_utc.strftime("%H:%M UTC")
            return f"{local} ({utc})"
    except Exception:
        pass
    return ""


def wake_agent(window_name: str, author: str, channel_id: str, content: str, timestamp: str = "", msg_id: str = ""):
    """Send the actual message to the agent's tmux pane so they can respond immediately.

    Formats as a <channel> XML tag matching the Discord plugin format so agents
    recognize it as a Discord message and reply via MCP tools.
    """
    # Truncate very long messages
    if len(content) > 500:
        content = content[:500] + "..."
    # Neutralize any tag-like markup so the body can't close <channel> and
    # inject text the agent would read as top-level instructions
    content = sanitize(content)
    # Build the <channel> tag the same way the Discord plugin does
    # Agents use chat_id to reply and message_id to react
    ts_attr = f' ts="{attr(timestamp)}"' if timestamp else ""
    id_attr = f' message_id="{attr(msg_id)}"' if msg_id else ""
    prompt = (
        f'<channel source="plugin:discord:discord" chat_id="{attr(channel_id)}"'
        f'{id_attr} user="{attr(author)}"{ts_attr}>{content}</channel>'
    )
    def _send(args, what):
        """Run one tmux send-keys and say so plainly if it did not land."""
        result = subprocess.run(
            ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{window_name}"] + args,
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return True
        detail = (result.stderr or b"").decode(errors="replace").strip()
        log(f"Failed to wake {window_name}: {what} rc={result.returncode} {detail}".rstrip())
        return False

    try:
        # Send text literally (avoids tmux interpreting content as key names),
        # then Enter as its own keystroke.
        #
        # Neither call used to be checked, so this logged "Woke <name>" even
        # when tmux had refused. A window that does not exist reported a
        # successful wake, which is the same class of mistake as the pane
        # check reporting a working agent it could not see.
        if not _send(["-l", prompt], "sending message"):
            return
        if not _send(["Enter"], "sending Enter (message text is left at the prompt)"):
            return
        log(f"Woke {window_name}")
    except Exception as e:
        log(f"Failed to wake {window_name}: {e}")

# ── Main loop ───────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def extract_mentioned_agents(message: dict) -> set[str]:
    """Return set of tmux window names for agents mentioned in this message."""
    targets = set()

    # Skip messages from bots that aren't humans — we only want to wake agents
    # when a human OR another bot tags them
    author_id = message.get("author", {}).get("id", "")

    # Check @everyone / @here
    if message.get("mention_everyone", False):
        targets.update(AGENTS.values())
        # Don't wake the sender if it's an agent
        if author_id in AGENTS:
            targets.discard(AGENTS[author_id])
        return targets

    # Check explicit mentions
    for mention in message.get("mentions", []):
        mention_id = mention.get("id", "")
        if mention_id in AGENTS:
            targets.add(AGENTS[mention_id])

    # Don't wake the sender if it's an agent
    if author_id in AGENTS:
        targets.discard(AGENTS[author_id])

    return targets


def seed_last_seen():
    """On startup, seed last_seen with current latest message IDs so we don't
    process old messages."""
    log("Seeding channel positions...")
    for channel_id in CHANNELS:
        messages = fetch_messages(channel_id)
        if messages:
            # Messages come newest-first from Discord
            last_seen[channel_id] = messages[0]["id"]
            log(f"  #{channel_id}: last_seen={messages[0]['id']}")
        time.sleep(0.5)  # Be gentle on rate limits


def poll():
    """Check all channels for new messages with mentions."""
    for channel_id in CHANNELS:
        try:
            after = last_seen.get(channel_id)
            messages = fetch_messages(channel_id, after)

            if not messages:
                continue

            # Discord returns newest-first; process oldest-first.
            messages = oldest_first(messages)

            for msg in messages:
                msg_id = msg["id"]
                # Update last_seen
                if not after or int(msg_id) > int(after or "0"):
                    last_seen[channel_id] = msg_id

                targets = extract_mentioned_agents(msg)
                if not targets:
                    continue

                author = msg.get("author", {}).get("username", "unknown")
                author_id = msg.get("author", {}).get("id", "")
                content = msg.get("content", "")
                log(f"Mention from {author}: \"{content[:80]}\" → targets: {targets}")

                msg_timestamp = msg.get("timestamp", "")
                for window_name in targets:
                    allowed, reason = is_allowed(window_name, author_id, channel_id)
                    if not allowed:
                        verb = "DROP" if ENFORCE else "WOULD DROP"
                        log(f"  {verb} {window_name}: author {author} ({author_id}) {reason}")
                        if ENFORCE:
                            continue
                    if should_wake_agent(window_name):
                        log(f"  {window_name}: waking up")
                        wake_agent(window_name, author, channel_id, content, msg_timestamp, msg_id)
                    else:
                        log(f"  {window_name}: mid-turn, leaving it to the plugin")
        except Exception as e:
            log(f"Error processing channel {channel_id}: {e}")

        # Small delay between channels
        time.sleep(0.3)


def main():
    if not BOT_TOKEN:
        print("Error: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    log("Mention watcher starting")
    log(f"Watching {len(CHANNELS)} channels for {len(AGENTS)} agents")
    log(f"Poll interval: {POLL_INTERVAL}s")

    seed_last_seen()
    log("Ready")
    heartbeat(force=True)

    while True:
        try:
            poll()
            heartbeat()
        except KeyboardInterrupt:
            log("Shutting down")
            break
        except Exception as e:
            log(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
