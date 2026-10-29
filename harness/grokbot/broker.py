"""The Grok Bot broker: the only process that holds the Discord token.

A hosted Grok Bot agent runs commands on this box under its own unprivileged
uid. That uid must never see the bot token, so it talks to this daemon over a
unix socket instead. One JSON line in, one JSON line out, per connection:

    post   {channel, text, reply_to?}      -> {ok, message_id}
    reply  {channel, message_id, text}     -> {ok, message_id}
    ask    {agent, text, wait?}            -> {ok, ticket, status, reply}
    check  {ticket?}                       -> {ok, tickets: [...]}
    read   {channel, n?}                   -> {ok, messages: [...]}

Anything else, or any failure, is {"ok": false, "error": "plain sentence"}.

The broker only posts to the channels in its config, pings only fleet agents
it can resolve by name (never @everyone, never a human), and caps outgoing
volume. The mention watcher (bin/grokbot_wake.py) reads tickets.json so that a
reply to one of the agent's asks is captured here instead of waking the agent.

Discord is protected both ways: outgoing messages are capped per minute, and
every GET (read, check, ask polling) draws on one shared per-minute budget.

The lock covers ticket and rate state only. It is never held across a Discord
call or the wait in `ask`, so a slow ask cannot stall a read.

Stdlib only. The token is read once at startup and appears in no log line, no
exception and no repr. Logs carry op, target, text length and ok/error, never
message bodies.
"""

import argparse
import json
import math
import os
import re
import secrets
import signal
import socket
import socketserver
import stat
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://example.invalid/fleet-kit, 1.0)"
HTTP_TIMEOUT = 15
MAX_WAIT = 120
POLL_INTERVAL = 3.0
READ_DEFAULT = 20
READ_MAX = 50
SETTLE_SECONDS = 4          # a reply's last chunk must be this old to count as complete
PAGE = 100                  # messages per after= fetch
MAX_PAGES = 3
MAX_TICKETS = 200
CHECK_POLL_MAX = 5          # working tickets polled per check call
CHECK_POLL_AGE = 24 * 3600  # older working tickets are listed but not polled
MAX_ASKS = 3                # concurrent asks being waited on
MAX_CONNS = 8               # concurrent connections served
REQUEST_DEADLINE = 10       # seconds for the request line to arrive, wall-clock

DEFAULTS = {
    "rate_per_minute": 6,
    "max_len": 1800,
    "ask_wait": 60,
    "ticket_days": 7,
    "max_line": 65536,
    "conn_timeout": 90,
    "router": "sonnet",
    "lookups_per_minute": 30,
    "request_deadline": REQUEST_DEADLINE,
}


class BrokerError(Exception):
    """A failure whose message is a plain sentence safe to show the caller."""


class RateLimited(BrokerError):
    """Discord answered 429. The op stops; nothing else is tried."""


class LookupBudget(BrokerError):
    """The broker's own cap on Discord GETs is used up for now."""


# -- Discord REST --------------------------------------------------------------

class Discord:
    """The two REST calls the broker needs. Holds the token; never shows it."""

    def __init__(self, token, opener=None, base=API_BASE):
        self._auth = "Bot " + token
        self._open = opener or urllib.request.urlopen
        self._base = base

    def __repr__(self):
        return "Discord()"

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method, headers={
            "Authorization": self._auth,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        })
        try:
            with self._open(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                retry_header = exc.headers.get("Retry-After") if exc.headers else None
            finally:
                exc.close()
            if exc.code == 429:
                raise RateLimited("Discord rate-limited, try again in %d s"
                                  % _retry_seconds(raw, retry_header)) from None
            raise BrokerError("Discord refused the request (HTTP %d)." % exc.code) from None
        except (urllib.error.URLError, OSError):
            raise BrokerError("Could not reach Discord.") from None
        except ValueError:
            raise BrokerError("Discord sent a reply I could not read.") from None

    def post(self, channel_id, text, reply_to=None, mention_ids=()):
        body = {
            "content": text,
            "allowed_mentions": {"parse": [], "users": [str(i) for i in mention_ids],
                                 "replied_user": False},
        }
        if reply_to:
            body["message_reference"] = {"message_id": str(reply_to),
                                         "fail_if_not_exists": False}
        out = self._call("POST", "/channels/%s/messages" % channel_id, body)
        if not isinstance(out, dict) or "id" not in out:
            raise BrokerError("Discord sent a reply I could not read.")
        return out

    def messages(self, channel_id, after=None, limit=50):
        query = {"limit": int(limit)}
        if after is not None:
            query["after"] = str(after)
        out = self._call("GET", "/channels/%s/messages?%s"
                         % (channel_id, urllib.parse.urlencode(query)))
        return out if isinstance(out, list) else []


def _retry_seconds(raw, header):
    value = None
    try:
        value = json.loads(raw).get("retry_after")
    except (ValueError, AttributeError):
        pass
    if not isinstance(value, (int, float)):
        try:
            value = float(header)
        except (TypeError, ValueError):
            value = 1
    return max(1, math.ceil(value))


# -- Text handling -------------------------------------------------------------

_AT_START = r"(?:(?<=\s)|^)@"
_AT_END = r"(?=[.,;:!?)\]]*(?:\s|$))"
_MENTION_RE = re.compile(_AT_START + r"([A-Za-z0-9_-]+)" + _AT_END)
# @everyone / @here are neutralized wherever they could be read as a broadcast:
# after punctuation or quotes, before 's, or chained. Only an address-like
# "word@here" (bob@here.com) is left alone.
_BROADCAST_RE = re.compile(r"(?:(?<![\w.])|(?<=everyone)|(?<=here))@(everyone|here)\b")


def mentions(text, agents):
    """Rewrite @name to <@id> for fleet agents only; return (text, ids).

    A name counts only at the start of the text or after whitespace, and only
    when followed by end, whitespace or punctuation, so emails and URLs are
    left alone. @everyone and @here become plain words.
    """
    text = _BROADCAST_RE.sub(r"\1", text)
    known = {name.lower(): spec["bot_id"] for name, spec in agents.items()}
    ids = []

    def sub(match):
        bot_id = known.get(match.group(1).lower())
        if not bot_id:
            return match.group(0)
        if bot_id not in ids:
            ids.append(bot_id)
        return "<@%s>" % bot_id

    return _MENTION_RE.sub(sub, text), ids


def speakable(text, agents=None, channels=None):
    """Plain text for a voice assistant: no markdown, no raw Discord markup."""
    by_id = {str(s["bot_id"]): n for n, s in (agents or {}).items()}
    chan_by_id = {str(v): k for k, v in (channels or {}).items()}
    text = re.sub(r"<@&\d+>", "a role", text)
    text = re.sub(r"<@!?(\d+)>", lambda m: by_id.get(m.group(1), "someone"), text)
    text = re.sub(r"<#(\d+)>", lambda m: chan_by_id.get(m.group(1), "a channel"), text)
    text = re.sub(r"<a?:\w+:\d+>", "", text)
    text = re.sub(r"^\s*```[^\n]*\n?", "", text, flags=re.M)
    text = re.sub(r"^#{1,6}[ \t]+", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"\1", text)
    return text.strip()


# -- Reply matching ------------------------------------------------------------

def _id_key(msg):
    try:
        return int(msg.get("id", 0))
    except (TypeError, ValueError):
        return 0


def find_reply(messages, ask_msg_id, expected_authors):
    """Locate the reply to one ask: (chunks, last_message, other_follows).

    The reply is the first message (oldest first) whose message_reference
    points at the ask and whose author is expected. Continuation chunks are the
    messages that follow it from that same author with nobody else in between;
    a chunk that is itself threaded to some other message ends the run.
    other_follows is True when a message from a different author comes right
    after the last chunk, which means the reply is finished. No reply is
    ([], None, False).
    """
    ordered = sorted(messages, key=_id_key)
    ask = str(ask_msg_id)
    expected = {str(a) for a in expected_authors}

    def author(m):
        return str((m.get("author") or {}).get("id", ""))

    def ref(m):
        r = (m.get("message_reference") or {}).get("message_id")
        return None if r is None else str(r)

    start = None
    for i, m in enumerate(ordered):
        if ref(m) == ask and author(m) in expected:
            start = i
            break
    if start is None:
        return [], None, False
    who = author(ordered[start])
    last = ordered[start]
    chunks = [last.get("content") or ""]
    other_follows = False
    for m in ordered[start + 1:]:
        if author(m) != who:
            other_follows = True
            break
        if ref(m) not in (None, ask):
            break
        chunks.append(m.get("content") or "")
        last = m
    return chunks, last, other_follows


def collect_reply(messages, ask_msg_id, expected_authors):
    """Chunks of the reply to one ask, as a list of strings, or []."""
    return find_reply(messages, ask_msg_id, expected_authors)[0]


def _message_epoch(msg):
    """Discord timestamp as epoch seconds, or None if absent or unreadable."""
    try:
        return datetime.fromisoformat(str(msg.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def reply_settled(last, other_follows, now):
    """A reply is complete once someone else speaks after it, or its last chunk
    is SETTLE_SECONDS old. A missing timestamp counts as old: Discord always
    sends one, so this only happens with odd input, and waiting forever on it
    would be worse."""
    if other_follows:
        return True
    ts = _message_epoch(last)
    return ts is None or now - ts >= SETTLE_SECONDS


def clamp_wait(value, default=0):
    if value is None:
        value = default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise BrokerError("wait must be a number of seconds.") from None
    if value != value:
        raise BrokerError("wait must be a number of seconds.")
    return max(0.0, min(float(MAX_WAIT), value))


# -- The broker ----------------------------------------------------------------

class Broker:
    def __init__(self, config, discord, clock=time.time, log=None):
        self.cfg = {**DEFAULTS, **config}
        self.discord = discord
        self.clock = clock
        self.poll_interval = POLL_INTERVAL
        self.sleep = time.sleep
        self._log_fn = log
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._sent = deque()     # outgoing message times, for the rate limit
        self._gets = deque()     # Discord GET times, for the lookup budget
        self._asks = 0           # asks currently waiting for an answer
        self._tickets = []
        self.agents = self.cfg["agents"]
        self.channels = self.cfg["channels"]
        self._bot_names = {str(s["bot_id"]): n for n, s in self.agents.items()}
        state_dir = self.cfg["state_dir"]
        os.makedirs(state_dir, mode=0o750, exist_ok=True)
        os.chmod(state_dir, 0o750)
        self._tickets_path = os.path.join(state_dir, "tickets.json")
        self._load()

    # -- tickets (callers hold self._lock) --

    def _load(self):
        try:
            with open(self._tickets_path) as fh:
                data = json.load(fh)
            self._tickets = [t for t in data.get("tickets", []) if isinstance(t, dict)]
        except (OSError, ValueError, AttributeError):
            self._tickets = []
        self._prune()

    def _prune(self):
        """Drop expired tickets, then enforce MAX_TICKETS (oldest answered go
        first, then oldest working). True if any went."""
        before = len(self._tickets)
        cutoff = self.clock() - self.cfg["ticket_days"] * 86400
        kept = [t for t in self._tickets if t.get("asked_at", 0) >= cutoff]
        excess = len(kept) - MAX_TICKETS
        if excess > 0:
            for want_answered in (True, False):
                doomed = sorted((t for t in kept if (t.get("status") == "answered") == want_answered),
                                key=lambda t: t.get("asked_at", 0))[:excess]
                ids = {id(t) for t in doomed}
                kept = [t for t in kept if id(t) not in ids]
                excess -= len(doomed)
                if excess <= 0:
                    break
        self._tickets = kept
        return len(kept) != before

    def _save(self):
        self._prune()
        state_dir = os.path.dirname(self._tickets_path)
        fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".tickets-")
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w") as fh:
                json.dump({"tickets": self._tickets}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp, self._tickets_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _persist(self):
        """Save, but a disk problem must not lose a ticket the caller holds."""
        try:
            self._save()
        except OSError as exc:
            self._log("save", "tickets", 0, False, note=type(exc).__name__)

    def _new_ticket_id(self):
        taken = {t.get("ticket") for t in self._tickets}
        while True:
            tid = secrets.token_hex(4)
            if tid not in taken:
                return tid

    # -- rate state --

    @staticmethod
    def _take_slot(stamps, limit, now):
        """Caller holds the lock. 0 if a slot was taken, else seconds to wait."""
        while stamps and now - stamps[0] >= 60:
            stamps.popleft()
        if len(stamps) >= limit:
            return max(1, math.ceil(60 - (now - stamps[0]))) if stamps else 60
        stamps.append(now)
        return 0

    def _reserve_rate(self):
        """One outgoing message. A slot stays spent even if the post then fails."""
        with self._lock:
            wait = self._take_slot(self._sent, self.cfg["rate_per_minute"], self.clock())
        if wait:
            raise BrokerError("Too many messages, limit is %d a minute. Try again in %d s."
                              % (self.cfg["rate_per_minute"], wait))

    def _reserve_get(self):
        """One Discord GET, shared by read, check and ask polling."""
        with self._lock:
            wait = self._take_slot(self._gets, self.cfg["lookups_per_minute"], self.clock())
        if wait:
            raise LookupBudget("Too many lookups, try again in %d s" % wait)

    # -- logging --

    def _log(self, op, target, length, ok, note=None):
        line = "%s %s %s len=%d %s" % (
            datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds"),
            op, target or "-", length, "ok" if ok else "error")
        if note:
            line += " " + note
        if self._log_fn:
            self._log_fn(line)
            return
        path = self.cfg.get("log_file")
        if not path:
            return
        try:
            with self._log_lock:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
                with os.fdopen(fd, "a") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass

    def _log_target(self, op, req):
        """What to log for the request's subject: a resolved name, never raw input."""
        def clip(v):
            return repr(v)[:40]
        if op in ("post", "reply", "read"):
            try:
                return self._channel(req.get("channel"))[0]
            except BrokerError:
                return clip(req.get("channel"))
        if op == "ask":
            key = str(req.get("agent")).strip().lower()
            return key if key == "team" or key in self.agents else clip(req.get("agent"))
        v = req.get("ticket")
        return "-" if v is None else clip(v)

    # -- request validation --

    def _channel(self, name):
        key = str(name or "").strip().lstrip("#").lower()
        if key in self.channels:
            return key, self.channels[key]
        for cname, cid in self.channels.items():
            if key == str(cid):
                return cname, cid
        raise BrokerError("That channel is not allowed. Choices: %s."
                          % ", ".join(sorted(self.channels)))

    def _text(self, req):
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BrokerError("There is no text to send.")
        return text.strip()

    def _check_len(self, text):
        if len(text) > self.cfg["max_len"]:
            raise BrokerError("That message is %d characters, the limit is %d."
                              % (len(text), self.cfg["max_len"]))

    def _speak(self, text):
        return speakable(text, self.agents, self.channels)

    # -- dispatch --

    def handle(self, request):
        if not isinstance(request, dict):
            return {"ok": False, "error": "The request must be a JSON object."}
        op = request.get("op")
        fn = {"post": self._op_post, "reply": self._op_reply, "ask": self._op_ask,
              "check": self._op_check, "read": self._op_read}.get(op)
        if fn is None:
            self._log("unknown", "-", 0, False)
            return {"ok": False, "error": "Unknown op. Choices: ask, check, post, read, reply."}
        target = self._log_target(op, request)
        length = len(request["text"]) if isinstance(request.get("text"), str) else 0
        try:
            out = fn(request)
        except BrokerError as exc:
            self._log(op, target, length, False)
            return {"ok": False, "error": str(exc)}
        out["ok"] = True
        self._log(op, target, length, True)
        return out

    # -- ops --

    def _send(self, channel_name, text, reply_to=None, extra_ids=()):
        name, channel_id = self._channel(channel_name)
        text, ids = mentions(self._text({"text": text}), self.agents)
        for extra in extra_ids:
            if extra not in ids:
                ids.append(extra)
        self._check_len(text)
        self._reserve_rate()
        msg = self.discord.post(channel_id, text, reply_to=reply_to, mention_ids=ids)
        return channel_id, str(msg["id"])

    def _op_post(self, req):
        reply_to = req.get("reply_to")
        _, mid = self._send(req.get("channel"), req.get("text"),
                            reply_to=str(reply_to) if reply_to else None)
        return {"message_id": mid}

    def _op_reply(self, req):
        mid = str(req.get("message_id") or "")
        if not mid.isdigit():
            raise BrokerError("A reply needs the numeric id of the message to reply to.")
        _, new_id = self._send(req.get("channel"), req.get("text"), reply_to=mid)
        return {"message_id": new_id}

    def _ask_target(self, name):
        key = str(name or "").strip().lower()
        if key == "team":
            router = self.cfg["router"]
            return "team", "general", self.agents[router]["bot_id"]
        if key in self.agents:
            spec = self.agents[key]
            return key, spec["channel"], spec["bot_id"]
        raise BrokerError("I don't know that agent. Choices: %s, team."
                          % ", ".join(sorted(self.agents)))

    def _expected(self, agent):
        if agent == "team":
            return {str(s["bot_id"]) for s in self.agents.values()}
        spec = self.agents.get(agent)
        return {str(spec["bot_id"])} if spec else set()

    def _op_ask(self, req):
        agent, channel_name, target_id = self._ask_target(req.get("agent"))
        # The whole connection is bounded by conn_timeout; leave room to answer.
        wait = min(clamp_wait(req.get("wait"), default=self.cfg["ask_wait"]),
                   max(0, self.cfg["conn_timeout"] - 10))
        text = self._text(req)
        if not text.startswith("<@%s>" % target_id):
            text = "<@%s> %s" % (target_id, text)
        with self._lock:
            if self._asks >= MAX_ASKS:
                raise BrokerError("Already waiting on three answers, try check later")
            self._asks += 1
        try:
            return self._ask(agent, channel_name, target_id, text, wait)
        finally:
            with self._lock:
                self._asks -= 1

    def _ask(self, agent, channel_name, target_id, text, wait):
        channel_id, ask_id = self._send(channel_name, text, extra_ids=[target_id])
        # From here the ask is out in the world. Whatever goes wrong, the caller
        # keeps its ticket and can check later.
        with self._lock:
            ticket = {"ticket": self._new_ticket_id(), "agent": agent,
                      "channel_id": str(channel_id), "ask_message_id": ask_id,
                      "asked_at": self.clock(), "status": "working", "reply": None}
            self._tickets.append(ticket)
            self._persist()
        working = {"ticket": ticket["ticket"], "status": "working", "reply": None}
        try:
            deadline = time.monotonic() + wait
            while wait > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sleep(min(self.poll_interval, remaining))
                try:
                    reply = self._poll(ticket)
                except RateLimited:
                    break                # Discord said slow down: stop asking it
                except BrokerError:
                    continue             # budget or a Discord hiccup: next tick
                if reply is not None:
                    return {"ticket": ticket["ticket"], "status": "answered",
                            "reply": self._speak(reply)}
        except Exception as exc:  # noqa: BLE001 - the post already happened
            self._log("ask-wait", agent, 0, False, note=type(exc).__name__)
        return working

    def _fetch_after(self, channel_id, after):
        """Messages after one id, up to MAX_PAGES pages, each page a lookup."""
        out = []
        cursor = str(after)
        for _ in range(MAX_PAGES):
            self._reserve_get()
            page = self.discord.messages(channel_id, after=cursor, limit=PAGE)
            out.extend(page)
            if len(page) < PAGE:
                break
            cursor = str(max(_id_key(m) for m in page))
        return out

    def _poll(self, ticket):
        """One look for one ticket. Returns the reply once it is complete, else
        None; a reply still arriving (or split and not yet finished) is None."""
        msgs = self._fetch_after(ticket["channel_id"], ticket["ask_message_id"])
        chunks, last, other_follows = find_reply(
            msgs, ticket["ask_message_id"], self._expected(ticket["agent"]))
        if not chunks or not reply_settled(last, other_follows, self.clock()):
            return None
        joined = "\n".join(chunks)
        with self._lock:
            ticket["status"] = "answered"
            ticket["reply"] = joined
            self._persist()
        return joined

    def _op_check(self, req):
        want = req.get("ticket")
        with self._lock:
            if self._prune():
                self._persist()
            rows = [t for t in self._tickets if want is None or t["ticket"] == str(want)]
        if want is not None and not rows:
            raise BrokerError("I have no ticket by that name. Tickets expire after %d days."
                              % self.cfg["ticket_days"])
        now = self.clock()
        fresh = sorted((t for t in rows if t["status"] == "working"
                        and now - t["asked_at"] < CHECK_POLL_AGE),
                       key=lambda t: t["asked_at"], reverse=True)[:CHECK_POLL_MAX]
        errors = {}
        for t in fresh:
            try:
                self._poll(t)
            except LookupBudget:
                raise
            except RateLimited as exc:
                errors[t["ticket"]] = str(exc)
                break                    # first 429 ends the whole op
            except BrokerError as exc:
                errors[t["ticket"]] = str(exc)
        with self._lock:
            out = []
            for t in sorted(rows, key=lambda t: t["asked_at"], reverse=True):
                row = {"ticket": t["ticket"], "agent": t["agent"], "status": t["status"],
                       "reply": self._speak(t["reply"]) if t["reply"] else None,
                       "asked_at": t["asked_at"]}
                if t["ticket"] in errors:
                    row["error"] = errors[t["ticket"]]
                out.append(row)
        return {"tickets": out}

    def _op_read(self, req):
        _, channel_id = self._channel(req.get("channel"))
        try:
            n = int(req.get("n", READ_DEFAULT))
        except (TypeError, ValueError):
            raise BrokerError("n must be a whole number.") from None
        n = max(1, min(READ_MAX, n))
        self._reserve_get()
        out = []
        for m in sorted(self.discord.messages(channel_id, limit=n), key=_id_key):
            a = m.get("author") or {}
            name = self._bot_names.get(str(a.get("id", ""))) \
                or a.get("global_name") or a.get("username") or "someone"
            ref = (m.get("message_reference") or {}).get("message_id")
            out.append({"id": str(m.get("id", "")), "author": name,
                        "text": self._speak(m.get("content") or ""),
                        "ts": m.get("timestamp", ""),
                        "reply_to": None if ref is None else str(ref)})
        return {"messages": out}


# -- Socket server -------------------------------------------------------------

def peer_allowed(uid, config):
    return uid == config["allowed_uid"]


def _peer_uid(sock):
    pid, uid, gid = struct.unpack(
        "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid


class _TooLong(Exception):
    pass


class _Handler(socketserver.BaseRequestHandler):
    def _send(self, obj):
        try:
            self.request.settimeout(10)
            self.request.sendall(json.dumps(obj).encode() + b"\n")
        except OSError:
            pass

    def _read_line(self, cfg):
        """The request line, or None on a silent close. The deadline is wall
        clock across all recvs, so a client dribbling bytes cannot hold on."""
        deadline = time.monotonic() + min(cfg["request_deadline"], cfg["conn_timeout"])
        limit = cfg["max_line"]
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.request.settimeout(remaining)
            chunk = self.request.recv(4096)
            if not chunk:
                return buf or None
            buf += chunk
            if b"\n" in buf:
                line = buf.split(b"\n", 1)[0]
                if len(line) > limit:
                    raise _TooLong
                return line
            if len(buf) > limit:
                raise _TooLong

    def handle(self):
        cfg = self.server.cfg
        try:
            allowed = peer_allowed(_peer_uid(self.request), cfg)
        except (OSError, AttributeError):
            allowed = False
        if not allowed:
            self._send({"ok": False, "error": "not allowed"})
            return
        try:
            data = self._read_line(cfg)
        except TimeoutError:
            self._send({"ok": False, "error": "The request timed out."})
            return
        except _TooLong:
            self._send({"ok": False, "error": "The request is too long."})
            return
        except OSError:
            return
        if not data:
            return
        try:
            request = json.loads(data)
        except (ValueError, RecursionError):
            self._send({"ok": False, "error": "The request is not valid JSON."})
            return
        try:
            response = self.server.broker.handle(request)
        except Exception as exc:  # one bad request must not take the daemon down
            self.server.broker._log("internal", type(exc).__name__, 0, False)
            response = {"ok": False, "error": "The broker hit an internal error."}
        self._send(response)


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, path, handler):
        self.slots = threading.BoundedSemaphore(MAX_CONNS)
        super().__init__(path, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'{"ok": false, "error": "busy"}\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def server_bind(self):
        path = self.server_address
        try:
            is_socket = stat.S_ISSOCK(os.stat(path).st_mode)
        except FileNotFoundError:
            is_socket = False
        if is_socket:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(2)
            try:
                probe.connect(path)
            except ConnectionRefusedError:
                os.unlink(path)  # stale socket from a previous run
            except FileNotFoundError:
                pass
            except (TimeoutError, PermissionError):
                # Something may own the path but will not talk to us: never unlink it.
                raise SystemExit("socket path in use and not answering; "
                                 "remove it if no broker is running") from None
            else:
                raise BrokerError("broker already running")
            finally:
                probe.close()
        old = os.umask(0o117)
        try:
            super().server_bind()
        finally:
            os.umask(old)
        os.chmod(path, 0o660)


def make_server(broker, config):
    cfg = {**DEFAULTS, **config}
    server = _Server(cfg["socket"], _Handler)
    server.broker = broker
    server.cfg = cfg
    return server


def load_config(path):
    with open(path) as fh:
        cfg = {**DEFAULTS, **json.load(fh)}
    for key in ("channels", "agents", "token_file", "allowed_uid", "socket", "state_dir"):
        if key not in cfg:
            raise SystemExit("broker: config is missing %s" % key)
    if cfg["router"] not in cfg["agents"] or "general" not in cfg["channels"]:
        raise SystemExit("broker: config needs the router in agents and a general channel")
    for name, spec in cfg["agents"].items():
        if spec.get("channel") not in cfg["channels"]:
            raise SystemExit("broker: agent %s has a channel that is not in channels" % name)
    return cfg


def serve(config_path):
    cfg = load_config(config_path)
    with open(cfg["token_file"]) as fh:
        token = fh.read().strip()
    if not token:
        raise SystemExit("broker: token_file is empty")
    broker = Broker(cfg, Discord(token))
    try:
        server = make_server(broker, cfg)
    except BrokerError as exc:
        raise SystemExit("broker: %s" % exc) from None
    signal.signal(signal.SIGTERM,
                  lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print("broker: listening on %s" % cfg["socket"], file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.unlink(cfg["socket"])
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grok Bot Discord broker")
    ap.add_argument("--config", required=True, help="path to broker.json")
    serve(ap.parse_args(argv).config)


if __name__ == "__main__":
    main()
