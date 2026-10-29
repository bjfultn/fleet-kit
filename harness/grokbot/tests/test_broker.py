"""Tests for harness/grokbot/broker.py.

No network and no real token. Ids below are placeholders. FakeDiscord stands in
for the REST client; the Discord class itself is tested through an injected
opener, and the socket server is exercised for real on a temp unix socket.
"""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import broker as bk  # noqa: E402

GENERAL = "300000000000000001"
NOVA = "300000000000000002"
CURTIS_CH = "300000000000000003"
SONNET_CH = "300000000000000004"
CAGE_CH = "300000000000000005"
REID_CH = "300000000000000006"
SONNET = "111111111111111111"
CURTIS = "222222222222222222"
CAGE = "333333333333333333"
HUMAN = "999999999999999999"
TOKEN = "TESTTOKEN.not-a-real-secret"


def make_config(tmp, **over):
    cfg = {
        "channels": {"nova": NOVA, "general": GENERAL, "curtis": CURTIS_CH,
                     "sonnet": SONNET_CH, "cage": CAGE_CH, "reid": REID_CH},
        "agents": {"sonnet": {"bot_id": SONNET, "channel": "sonnet"},
                   "curtis": {"bot_id": CURTIS, "channel": "curtis"},
                   "cage": {"bot_id": CAGE, "channel": "cage"}},
        "router": "sonnet",
        "token_file": os.path.join(tmp, "token"),
        "allowed_uid": os.getuid(),
        "socket": os.path.join(tmp, "broker.sock"),
        "state_dir": os.path.join(tmp, "state"),
        "log_file": os.path.join(tmp, "broker.log"),
    }
    cfg.update(over)
    return cfg


class FakeDiscord:
    """Records posts; serves per-channel message lists like the REST API does
    (newest first). Message ids count up so they sort like snowflakes."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.posts = []
        self.fail_post = None
        self.reactions = []
        self.fail_react = None
        self.fail_messages = None
        self.on_post = None
        self.on_messages = None
        self.message_calls = []
        self._next = 1000
        self._msgs = {}
        self._lock = threading.Lock()

    def _id(self):
        with self._lock:
            self._next += 1
            return str(self._next)

    def add(self, channel_id, author_id, content, reply_to=None, username="someone", age=60):
        """age: seconds before the fake clock that Discord stamped the message.
        The default is old enough to count as a finished reply."""
        stamp = datetime.fromtimestamp(self.clock() - age, timezone.utc).isoformat()
        msg = {"id": self._id(), "content": content, "timestamp": stamp,
               "author": {"id": author_id, "username": username}}
        if reply_to:
            msg["message_reference"] = {"message_id": str(reply_to)}
        self._msgs.setdefault(channel_id, []).append(msg)
        return msg

    def post(self, channel_id, text, reply_to=None, mention_ids=()):
        if self.fail_post:
            raise self.fail_post
        rec = {"channel_id": channel_id, "text": text, "reply_to": reply_to,
               "mention_ids": list(mention_ids)}
        self.posts.append(rec)
        msg = self.add(channel_id, "888888888888888888", text, reply_to, username="grok")
        rec["id"] = msg["id"]
        if self.on_post:
            self.on_post(rec)
        return {"id": msg["id"]}

    def react(self, channel_id, message_id, emoji):
        if self.fail_react:
            raise self.fail_react
        self.reactions.append({"channel_id": channel_id, "message_id": message_id,
                               "emoji": emoji})

    def messages(self, channel_id, after=None, limit=50):
        self.message_calls.append((channel_id, after, limit))
        if self.on_messages:
            self.on_messages(len(self.message_calls))
        if self.fail_messages:
            raise self.fail_messages
        out = sorted((m for m in self._msgs.get(channel_id, [])
                      if after is None or int(m["id"]) > int(after)), key=lambda m: int(m["id"]))
        # like Discord: with after= you get the page just past it, without it the newest
        out = out[:limit] if after is not None else out[-limit:]
        out.reverse()
        return out


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        self.now = 1_800_000_000.0
        self.discord = FakeDiscord(clock=lambda: self.now)
        self.broker = self.make()

    def make(self, **over):
        b = bk.Broker(make_config(self.tmp, **over), self.discord, clock=lambda: self.now)
        b.poll_backoff = (0.01,)
        return b

    def tickets_on_disk(self):
        with open(os.path.join(self.tmp, "state", "tickets.json")) as fh:
            return json.load(fh)["tickets"]


class TestPost(BrokerCase):
    def test_post_rejects_unlisted_channel(self):
        r = self.broker.handle({"op": "post", "channel": "secret", "text": "hi"})
        self.assertFalse(r["ok"])
        self.assertIn("general", r["error"])
        r = self.broker.handle({"op": "post", "channel": "123456789012345678", "text": "hi"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.discord.posts, [])

    def test_post_never_pings_everyone_or_humans(self):
        r = self.broker.handle({"op": "post", "channel": "general",
                                "text": "@everyone @here <@%s> hello @sonnet" % HUMAN})
        self.assertTrue(r["ok"])
        rec = self.discord.posts[0]
        self.assertEqual(rec["mention_ids"], [SONNET])
        self.assertNotIn("@everyone", rec["text"])
        self.assertNotIn("@here", rec["text"])
        self.assertIn("<@%s>" % SONNET, rec["text"])

    def test_discord_payload_allowed_mentions(self):
        seen = {}

        def opener(req, timeout=None):
            seen["body"] = json.loads(req.data)
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["timeout"] = timeout
            return io.BytesIO(b'{"id": "42"}')

        d = bk.Discord(TOKEN, opener=opener)
        out = d.post(GENERAL, "hi <@%s>" % SONNET, reply_to="77", mention_ids=[SONNET])
        self.assertEqual(out["id"], "42")
        self.assertEqual(seen["body"]["allowed_mentions"],
                         {"parse": [], "users": [SONNET], "replied_user": False})
        self.assertEqual(seen["body"]["message_reference"]["message_id"], "77")
        self.assertEqual(seen["headers"]["authorization"], "Bot " + TOKEN)
        self.assertEqual(seen["headers"]["user-agent"],
                         "DiscordBot (https://example.invalid/fleet-kit, 1.0)")
        self.assertEqual(seen["timeout"], 15)

    def test_mention_rewrite_ignores_emails_and_urls(self):
        agents = make_config("/x")["agents"]
        text, ids = bk.mentions("mail bob@sonnet.com or https://x.io/@sonnet or a@curtis", agents)
        self.assertEqual(ids, [])
        self.assertEqual(text, "mail bob@sonnet.com or https://x.io/@sonnet or a@curtis")
        for src, want in (("(@here) ping", "(here) ping"), ('"@here" ping', '"here" ping'),
                          ("@everyone's call", "everyone's call"), ("@here, @everyone!", "here, everyone!")):
            self.assertEqual(bk.mentions(src, agents), (want, []))
        text, ids = bk.mentions("@everyone@here", agents)
        self.assertNotIn("@", text)
        self.assertEqual(ids, [])
        text, ids = bk.mentions("write bob@here.com or x@everyone.org", agents)
        self.assertEqual((text, ids), ("write bob@here.com or x@everyone.org", []))
        text, ids = bk.mentions("@sonnet, ask @cage. and @nobody", agents)
        self.assertEqual(ids, [SONNET, CAGE])
        self.assertEqual(text, "<@%s>, ask <@%s>. and @nobody" % (SONNET, CAGE))
        text, ids = bk.mentions("hey @curtis! @everyone", agents)
        self.assertEqual(ids, [CURTIS])
        self.assertEqual(text, "hey <@%s>! everyone" % CURTIS)

    def test_rate_limit_seventh_message_refused(self):
        for i in range(6):
            self.assertTrue(self.broker.handle(
                {"op": "post", "channel": "general", "text": "m%d" % i})["ok"])
        r = self.broker.handle({"op": "post", "channel": "general", "text": "m7"})
        self.assertFalse(r["ok"])
        self.assertEqual(len(self.discord.posts), 6)
        self.now += 61
        self.assertTrue(self.broker.handle(
            {"op": "post", "channel": "general", "text": "later"})["ok"])

    def test_failed_post_keeps_its_rate_slot(self):
        self.discord.fail_post = bk.BrokerError("Discord said no (HTTP 500).")
        for _ in range(6):
            r = self.broker.handle({"op": "post", "channel": "general", "text": "x"})
            self.assertIn("500", r["error"])
        self.discord.fail_post = None
        r = self.broker.handle({"op": "post", "channel": "general", "text": "x"})
        self.assertFalse(r["ok"])
        self.assertIn("Too many messages", r["error"])

    def test_ask_counts_against_rate_limit(self):
        for i in range(5):
            self.assertTrue(self.broker.handle(
                {"op": "post", "channel": "general", "text": "m%d" % i})["ok"])
        self.assertTrue(self.broker.handle(
            {"op": "ask", "agent": "cage", "text": "q", "wait": 0})["ok"])
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "q2", "wait": 0})
        self.assertFalse(r["ok"])
        self.assertEqual(len(self.discord.posts), 6)

    def test_text_over_max_len_refused(self):
        r = self.broker.handle({"op": "post", "channel": "general", "text": "a" * 1801})
        self.assertFalse(r["ok"])
        self.assertIn("1800", r["error"])
        self.assertTrue(self.broker.handle(
            {"op": "post", "channel": "general", "text": "a" * 1800})["ok"])

    def test_reply_sets_reply_to(self):
        r = self.broker.handle({"op": "reply", "channel": "general",
                                "message_id": "555", "text": "ok"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.discord.posts[0]["reply_to"], "555")

    def test_bad_requests_get_plain_errors(self):
        for req in ({"op": "dance"}, {}, {"op": "post"}, {"op": "ask", "agent": "nobody", "text": "x"},
                    {"op": "post", "channel": "general", "text": "  "}):
            r = self.broker.handle(req)
            self.assertFalse(r["ok"])
            self.assertIsInstance(r["error"], str)
        r = self.broker.handle({"op": "ask", "agent": "nobody", "text": "x"})
        self.assertIn("curtis", r["error"])
        self.assertIn("team", r["error"])


class TestReact(BrokerCase):
    def react(self, **over):
        req = {"op": "react", "channel": "general", "message_id": "555",
               "emoji": "\N{THUMBS UP SIGN}"}
        req.update(over)
        return self.broker.handle(req)

    def test_react_calls_discord_with_ids_and_emoji(self):
        self.assertEqual(self.react(), {"ok": True})
        self.assertEqual(self.discord.reactions, [
            {"channel_id": GENERAL, "message_id": "555", "emoji": "\N{THUMBS UP SIGN}"}])
        self.assertEqual(self.discord.posts, [])

    def test_react_accepts_channel_id_and_strips_the_emoji(self):
        self.assertTrue(self.react(channel=NOVA, emoji=" \N{THUMBS UP SIGN}\n")["ok"])
        self.assertEqual(self.discord.reactions[0]["channel_id"], NOVA)
        self.assertEqual(self.discord.reactions[0]["emoji"], "\N{THUMBS UP SIGN}")

    def test_react_accepts_a_zwj_sequence_up_to_eight_code_points(self):
        family = "\N{MAN}\u200d\N{WOMAN}\u200d\N{GIRL}\u200d\N{BOY}"   # 7 code points
        self.assertTrue(self.react(emoji=family)["ok"])
        self.assertTrue(self.react(emoji="\N{THUMBS UP SIGN}" * 8)["ok"])

    def test_react_refuses_a_non_numeric_message_id(self):
        for bad in ("abc", "12 3", "../x", "", None, "\u00b2", "-5", "1.5"):
            r = self.react(message_id=bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn("numeric id", r["error"])
        self.assertEqual(self.discord.reactions, [])

    def test_react_accepts_a_numeric_message_id_sent_as_a_number(self):
        self.assertTrue(self.react(message_id=555)["ok"])
        self.assertEqual(self.discord.reactions[0]["message_id"], "555")

    def test_react_rejects_unlisted_channel(self):
        for bad in ("secret", "123456", None):
            r = self.react(channel=bad)
            self.assertFalse(r["ok"])
            self.assertIn("general", r["error"])
        self.assertEqual(self.discord.reactions, [])

    def test_react_refuses_anything_but_one_emoji(self):
        for bad in ("", "  ", "ok", "\N{THUMBS UP SIGN} \N{THUMBS UP SIGN}", "<:x:1>",
                    "\N{THUMBS UP SIGN}x", "@everyone", "1\ufe0f\u20e3", None, 5,
                    ["\N{THUMBS UP SIGN}"], "\N{THUMBS UP SIGN}" * 9, "\N{THUMBS UP SIGN}\t\N{THUMBS UP SIGN}"):
            r = self.react(emoji=bad)
            self.assertFalse(r["ok"], repr(bad))
            self.assertEqual(r["error"], "Use one emoji, like \N{THUMBS UP SIGN}.")
        self.assertEqual(self.discord.reactions, [])

    def test_react_without_an_emoji_key_is_refused(self):
        r = self.broker.handle({"op": "react", "channel": "general", "message_id": "5"})
        self.assertFalse(r["ok"])
        self.assertIn("Use one emoji", r["error"])

    def test_react_consumes_the_per_minute_budget(self):
        for _ in range(6):
            self.assertTrue(self.react()["ok"])
        r = self.react()
        self.assertFalse(r["ok"])
        self.assertIn("Too many messages", r["error"])
        self.assertEqual(len(self.discord.reactions), 6)
        self.now += 61
        self.assertTrue(self.react()["ok"])

    def test_react_shares_the_budget_with_posts(self):
        for i in range(5):
            self.assertTrue(self.broker.handle(
                {"op": "post", "channel": "general", "text": "m%d" % i})["ok"])
        self.assertTrue(self.react()["ok"])
        self.assertFalse(self.react()["ok"])
        self.assertFalse(self.broker.handle(
            {"op": "post", "channel": "general", "text": "more"})["ok"])

    def test_refused_requests_keep_their_budget(self):
        for _ in range(10):
            self.react(emoji="ok")
            self.react(message_id="x")
            self.react(channel="secret")
        self.assertTrue(self.react()["ok"])

    def test_failed_react_keeps_its_rate_slot_and_shows_the_error(self):
        self.discord.fail_react = bk.BrokerError("Discord refused the request (HTTP 404).")
        for _ in range(6):
            self.assertIn("404", self.react()["error"])
        self.discord.fail_react = None
        self.assertIn("Too many messages", self.react()["error"])

    def test_react_logs_the_channel_name_with_zero_length(self):
        self.react()
        self.react(channel="bad\nFAKE", emoji="ok")
        with open(os.path.join(self.tmp, "broker.log")) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(" react general len=0 ok", lines[0])
        self.assertIn(" react 'bad\\nFAKE' len=0 error", lines[1])
        self.assertNotIn("\N{THUMBS UP SIGN}", "".join(lines))

    def test_unknown_op_lists_react(self):
        r = self.broker.handle({"op": "dance"})
        self.assertEqual(r["error"], "Unknown op. Choices: ask, check, post, react, read, reply.")


class TestAsk(BrokerCase):
    def reply_from(self, agent_id, channel, text, ref_of=None):
        def hook(rec):
            if ref_of is None or rec["text"].find(ref_of) >= 0:
                self.discord.add(channel, agent_id, text, reply_to=rec["id"])
        self.discord.on_post = hook

    def test_ask_answered_by_reply_to(self):
        self.reply_from(CURTIS, CURTIS_CH, "Disk is **fine**, 40% used.")
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "how is disk?", "wait": 2})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["reply"], "Disk is fine, 40% used.")
        rec = self.discord.posts[0]
        self.assertEqual(rec["channel_id"], CURTIS_CH)
        self.assertTrue(rec["text"].startswith("<@%s> " % CURTIS))
        self.assertEqual(rec["mention_ids"], [CURTIS])
        t = self.tickets_on_disk()[0]
        self.assertEqual(t["status"], "answered")
        self.assertEqual(t["ask_message_id"], rec["id"])
        self.assertEqual(t["channel_id"], CURTIS_CH)
        self.assertEqual(t["agent"], "curtis")
        self.assertEqual(t["asked_at"], self.now)

    def test_ask_ignores_unthreaded_agent_messages(self):
        def hook(rec):
            self.discord.add(CURTIS_CH, CURTIS, "talking to someone else")
            self.discord.add(CURTIS_CH, CURTIS, "replying to the human", reply_to="1")
        self.discord.on_post = hook
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "status?", "wait": 0.1})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "working")
        self.assertIsNone(r["reply"])

    def test_ask_ignores_reply_from_wrong_author(self):
        self.reply_from(HUMAN, CURTIS_CH, "I am not curtis")
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "status?", "wait": 0.1})
        self.assertEqual(r["status"], "working")

    def test_two_open_asks_get_their_own_replies(self):
        r1 = self.broker.handle({"op": "ask", "agent": "curtis", "text": "first", "wait": 0})
        r2 = self.broker.handle({"op": "ask", "agent": "curtis", "text": "second", "wait": 0})
        self.assertEqual(r1["status"], "working")
        a1, a2 = (p["id"] for p in self.discord.posts)
        # answers land in reverse order, back to back from the same author
        self.discord.add(CURTIS_CH, CURTIS, "answer two", reply_to=a2)
        self.discord.add(CURTIS_CH, CURTIS, "answer one", reply_to=a1)
        out = self.broker.handle({"op": "check"})
        by = {t["ticket"]: t for t in out["tickets"]}
        self.assertEqual(by[r1["ticket"]]["reply"], "answer one")
        self.assertEqual(by[r2["ticket"]]["reply"], "answer two")
        self.assertEqual(by[r1["ticket"]]["status"], "answered")

    def test_ask_team_tags_router_in_general(self):
        self.reply_from(CAGE, GENERAL, "Cage here, I can take it.")
        r = self.broker.handle({"op": "ask", "agent": "team", "text": "who has the PR?", "wait": 2})
        rec = self.discord.posts[0]
        self.assertEqual(rec["channel_id"], GENERAL)
        self.assertTrue(rec["text"].startswith("<@%s> " % SONNET))
        self.assertEqual(rec["mention_ids"], [SONNET])
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["reply"], "Cage here, I can take it.")
        self.assertEqual(self.tickets_on_disk()[0]["agent"], "team")

    def test_ask_times_out_to_working_with_ticket(self):
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "ping", "wait": 0.1})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "working")
        self.assertTrue(r["ticket"])
        self.assertEqual(self.tickets_on_disk()[0]["status"], "working")
        self.assertIsNone(self.tickets_on_disk()[0]["reply"])

    def test_ask_wait_is_clamped_and_zero_returns_at_once(self):
        t0 = time.monotonic()
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "ping", "wait": 0})
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual(r["status"], "working")
        self.assertEqual(bk.clamp_wait(-5), 0)
        self.assertEqual(bk.clamp_wait(9999), 120)
        self.assertEqual(bk.clamp_wait(None, default=60), 60)

    def test_reply_arriving_in_pieces_is_not_returned_partial(self):
        def post_hook(rec):
            self.discord.add(CURTIS_CH, CURTIS, "part one", reply_to=rec["id"], age=0)

        def poll_hook(n):
            if n == 2:   # between polls: the second chunk lands, time passes
                self.now += 5
                self.discord.add(CURTIS_CH, CURTIS, "part two", age=5)
        self.discord.on_post = post_hook
        self.discord.on_messages = poll_hook
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "long", "wait": 2})
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["reply"], "part one\npart two")
        self.assertEqual(self.tickets_on_disk()[0]["reply"], "part one\npart two")

    def test_unsettled_reply_times_out_working_without_partial_text(self):
        self.discord.on_post = lambda rec: self.discord.add(
            CURTIS_CH, CURTIS, "part one", reply_to=rec["id"], age=0)
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "long", "wait": 0.1})
        self.assertEqual(r["status"], "working")
        self.assertIsNone(r["reply"])
        self.assertIsNone(self.tickets_on_disk()[0]["reply"])
        out = self.broker.handle({"op": "check"})
        self.assertEqual(out["tickets"][0]["status"], "working")
        self.now += 5   # the last chunk is now 5 s old: settled
        out = self.broker.handle({"op": "check"})
        self.assertEqual(out["tickets"][0]["status"], "answered")
        self.assertEqual(out["tickets"][0]["reply"], "part one")

    def test_reply_followed_by_another_author_is_settled_at_once(self):
        def hook(rec):
            self.discord.add(CURTIS_CH, CURTIS, "part one", reply_to=rec["id"], age=0)
            self.discord.add(CURTIS_CH, HUMAN, "thanks", age=0)
        self.discord.on_post = hook
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "q", "wait": 1})
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["reply"], "part one")

    def test_ticket_ids_are_8_hex(self):
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 0})
        self.assertRegex(r["ticket"], r"^[0-9a-f]{8}$")

    def test_lookup_budget_is_shared_by_read_and_check(self):
        b = self.make(lookups_per_minute=2)
        self.assertTrue(b.handle({"op": "read", "channel": "general"})["ok"])
        self.assertTrue(b.handle({"op": "read", "channel": "general"})["ok"])
        r = b.handle({"op": "read", "channel": "general"})
        self.assertFalse(r["ok"])
        self.assertRegex(r["error"], r"^Too many lookups, try again in \d+ s$")
        b.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 0})
        r = b.handle({"op": "check"})
        self.assertFalse(r["ok"])
        self.assertIn("Too many lookups", r["error"])
        self.now += 61
        self.assertTrue(b.handle({"op": "read", "channel": "general"})["ok"])
        self.assertEqual(len(self.discord.message_calls), 3)

    def test_ask_poll_waits_when_budget_is_spent(self):
        b = self.make(lookups_per_minute=0)
        r = b.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 0.1})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "working")
        self.assertEqual(self.discord.message_calls, [])

    def test_check_stops_the_op_at_first_429(self):
        for _ in range(3):
            self.now += 1
            self.broker.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 0})
        self.discord.fail_messages = bk.RateLimited("Discord rate-limited, try again in 3 s")
        out = self.broker.handle({"op": "check"})
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.discord.message_calls), 1)
        self.assertEqual(len(out["tickets"]), 3)
        self.assertEqual(sum("error" in t for t in out["tickets"]), 1)

    def test_ask_stops_polling_at_429(self):
        self.discord.fail_messages = bk.RateLimited("Discord rate-limited, try again in 3 s")
        t0 = time.monotonic()
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 5})
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(r["status"], "working")
        self.assertEqual(len(self.discord.message_calls), 1)

    def test_check_polls_five_newest_and_skips_stale(self):
        for _ in range(7):
            self.now += 10   # spread out so the 6 a minute limit is not hit
            self.broker.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 0})
        out = self.broker.handle({"op": "check"})
        self.assertEqual(len(out["tickets"]), 7)
        newest = {p["id"] for p in self.discord.posts[2:]}
        self.assertEqual({c[1] for c in self.discord.message_calls}, newest)
        self.assertEqual(len(self.discord.message_calls), 5)
        self.discord.message_calls.clear()
        self.now += 25 * 3600
        out = self.broker.handle({"op": "check"})
        self.assertEqual(self.discord.message_calls, [])
        self.assertEqual(len(out["tickets"]), 7)
        self.assertTrue(all(t["status"] == "working" for t in out["tickets"]))

    def test_ticket_store_is_capped_answered_dropped_first(self):
        def t(i, status):
            return {"ticket": "%08x" % i, "agent": "cage", "channel_id": CAGE_CH,
                    "ask_message_id": str(i), "asked_at": self.now - 1000 + i,
                    "status": status, "reply": None}
        b = self.broker
        b._tickets = [t(i, "working") for i in range(150)] + \
                     [t(1000 + i, "answered") for i in range(100)]
        b._prune()
        self.assertEqual(len(b._tickets), 200)
        self.assertEqual(sum(x["status"] == "working" for x in b._tickets), 150)
        self.assertEqual(min(x["ask_message_id"] for x in b._tickets if x["status"] == "answered"),
                         "1050")
        b._tickets = [t(i, "working") for i in range(205)]
        b._prune()
        self.assertEqual(len(b._tickets), 200)
        self.assertEqual(min(int(x["ask_message_id"]) for x in b._tickets), 5)

    def test_check_pages_up_to_three_pages(self):
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "q", "wait": 0})
        ask_id = self.discord.posts[0]["id"]
        for _ in range(229):
            self.discord.add(CURTIS_CH, HUMAN, "chatter")
        self.discord.add(CURTIS_CH, CURTIS, "found it", reply_to=ask_id)
        out = self.broker.handle({"op": "check", "ticket": r["ticket"]})
        self.assertEqual(out["tickets"][0]["reply"], "found it")
        self.assertEqual(len(self.discord.message_calls), 3)

    def test_paging_stops_after_three_pages(self):
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "q", "wait": 0})
        ask_id = self.discord.posts[0]["id"]
        for _ in range(310):
            self.discord.add(CURTIS_CH, HUMAN, "chatter")
        self.discord.add(CURTIS_CH, CURTIS, "too far", reply_to=ask_id)
        out = self.broker.handle({"op": "check", "ticket": r["ticket"]})
        self.assertEqual(out["tickets"][0]["status"], "working")
        self.assertEqual(len(self.discord.message_calls), 3)

    def test_fourth_concurrent_ask_refused(self):
        threads = [threading.Thread(target=self.broker.handle, args=({
            "op": "ask", "agent": "cage", "text": "q%d" % i, "wait": 1},)) for i in range(3)]
        for th in threads:
            th.start()
        end = time.monotonic() + 5
        while len(self.discord.posts) < 3 and time.monotonic() < end:
            time.sleep(0.01)
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "q4", "wait": 0})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "Already waiting on three answers, try check later")
        for th in threads:
            th.join(10)
        self.assertTrue(self.broker.handle({"op": "ask", "agent": "cage", "text": "q5", "wait": 0})["ok"])

    def test_unexpected_error_while_waiting_keeps_the_ticket(self):
        self.discord.fail_messages = ValueError("odd")
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 1})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "working")
        self.assertTrue(r["ticket"])
        with open(os.path.join(self.tmp, "broker.log")) as fh:
            self.assertIn("ask-wait", fh.read())

    def test_save_failure_still_returns_the_ticket(self):
        with mock.patch.object(self.broker, "_save", side_effect=OSError("disk full")):
            r = self.broker.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 0})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "working")
        self.assertEqual(len(self.broker.handle({"op": "check"})["tickets"]), 1)
        with open(os.path.join(self.tmp, "broker.log")) as fh:
            self.assertIn("save", fh.read())

    def test_log_never_carries_raw_client_strings(self):
        self.broker.handle({"op": "post", "channel": "bad\nFAKE LINE", "text": "x"})
        self.broker.handle({"op": "ask", "agent": "../evil\r\nX" + "y" * 100, "text": "x"})
        self.broker.handle({"op": "check", "ticket": "t\nZ"})
        with open(os.path.join(self.tmp, "broker.log")) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("'bad\\nFAKE LINE'", lines[0])
        self.assertLess(len(lines[1]), 120)

    def test_check_joins_split_reply(self):
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "long one", "wait": 0})
        ask_id = self.discord.posts[0]["id"]
        self.discord.add(CURTIS_CH, CURTIS, "part one", reply_to=ask_id)
        self.discord.add(CURTIS_CH, CURTIS, "part two")
        self.discord.add(CURTIS_CH, CURTIS, "part three")
        out = self.broker.handle({"op": "check", "ticket": r["ticket"]})
        t = out["tickets"][0]
        self.assertEqual(t["status"], "answered")
        self.assertEqual(t["reply"], "part one\npart two\npart three")
        self.assertEqual(self.tickets_on_disk()[0]["reply"], "part one\npart two\npart three")

    def test_ask_polling_backs_off_3_6_then_10_seconds(self):
        clock = {"t": 0.0}
        sleeps = []

        def sleep(secs):
            sleeps.append(secs)
            clock["t"] += secs

        b = bk.Broker(make_config(self.tmp), self.discord, clock=lambda: self.now)
        b.sleep = sleep
        b.monotonic = lambda: clock["t"]
        r = b.handle({"op": "ask", "agent": "cage", "text": "q", "wait": 60})
        self.assertEqual(r["status"], "working")
        self.assertEqual(sleeps[:5], [3.0, 6.0, 10.0, 10.0, 10.0])
        self.assertAlmostEqual(sum(sleeps), 60.0)
        polls = len(self.discord.message_calls)
        self.assertEqual(polls, len(sleeps))
        # three asks polling at once still leave lookup budget for read and check
        self.assertLessEqual(3 * polls, b.cfg["lookups_per_minute"] - 5)

    def test_check_without_a_ticket_shows_working_plus_five_recent_answers(self):
        b = self.make()
        for i in range(8):
            b._tickets.append({"ticket": "a%d" % i, "agent": "cage", "channel_id": CAGE_CH,
                               "ask_message_id": str(100 + i), "asked_at": self.now - 1000 + i,
                               "status": "answered", "reply": "r%d" % i,
                               "reply_message_ids": [], "expected_author_ids": [CAGE]})
        b._tickets.append({"ticket": "w1", "agent": "cage", "channel_id": CAGE_CH,
                           "ask_message_id": "200", "asked_at": self.now - 5000,
                           "status": "working", "reply": None,
                           "reply_message_ids": [], "expected_author_ids": [CAGE]})
        out = b.handle({"op": "check"})
        names = [t["ticket"] for t in out["tickets"]]
        self.assertEqual(names, ["a7", "a6", "a5", "a4", "a3", "w1"])
        self.assertEqual(out["omitted"], 3)
        # naming a ticket still finds an old answered one
        one = b.handle({"op": "check", "ticket": "a0"})
        self.assertEqual([t["ticket"] for t in one["tickets"]], ["a0"])
        self.assertNotIn("omitted", one)

    def test_ticket_records_expected_authors_and_captured_reply_ids(self):
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "long one", "wait": 0})
        t = self.tickets_on_disk()[0]
        self.assertEqual(t["expected_author_ids"], [CURTIS])
        self.assertEqual(t["reply_message_ids"], [])
        ask_id = self.discord.posts[0]["id"]
        a = self.discord.add(CURTIS_CH, CURTIS, "part one", reply_to=ask_id)
        b = self.discord.add(CURTIS_CH, CURTIS, "part two")
        self.broker.handle({"op": "check", "ticket": r["ticket"]})
        t = self.tickets_on_disk()[0]
        self.assertEqual(t["status"], "answered")
        self.assertEqual(t["reply_message_ids"], [a["id"], b["id"]])

    def test_team_ticket_expects_every_fleet_agent(self):
        self.broker.handle({"op": "ask", "agent": "team", "text": "anyone", "wait": 0})
        self.assertEqual(self.tickets_on_disk()[0]["expected_author_ids"],
                         sorted([SONNET, CURTIS, CAGE]))

    def test_old_ticket_without_the_new_fields_loads(self):
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        with open(os.path.join(self.tmp, "state", "tickets.json"), "w") as fh:
            json.dump({"tickets": [{"ticket": "old1", "agent": "cage", "channel_id": CAGE_CH,
                                    "ask_message_id": "5", "asked_at": self.now,
                                    "status": "working", "reply": None}]}, fh)
        b = self.make()
        self.assertEqual(b._tickets[0]["reply_message_ids"], [])
        self.assertEqual(b._tickets[0]["expected_author_ids"], [])
        self.assertEqual(b.handle({"op": "check", "ticket": "old1"})["tickets"][0]["status"],
                         "working")

    def test_split_reply_stops_at_other_author(self):
        msgs = [
            {"id": "1", "content": "ask", "author": {"id": "8"}},
            {"id": "2", "content": "one", "author": {"id": CURTIS},
             "message_reference": {"message_id": "1"}},
            {"id": "3", "content": "two", "author": {"id": CURTIS}},
            {"id": "4", "content": "interloper", "author": {"id": HUMAN}},
            {"id": "5", "content": "after", "author": {"id": CURTIS}},
        ]
        got = bk.collect_reply(list(reversed(msgs)), "1", {CURTIS})
        self.assertEqual(got, ["one", "two"])
        self.assertEqual(bk.collect_reply(msgs, "9", {CURTIS}), [])

    def test_later_threaded_reply_from_expected_author_is_kept(self):
        # "on it", someone else talks, then the result threaded to the ask:
        # the watcher held the result back, so the broker must take it.
        msgs = [
            {"id": "2", "content": "on it", "author": {"id": CURTIS},
             "message_reference": {"message_id": "1"}},
            {"id": "3", "content": "interloper", "author": {"id": HUMAN}},
            {"id": "4", "content": "result", "author": {"id": CURTIS},
             "message_reference": {"message_id": "1"}},
            {"id": "5", "content": "unthreaded", "author": {"id": CURTIS}},
        ]
        run, other_follows = bk.reply_messages(list(reversed(msgs)), "1", {CURTIS})
        self.assertEqual([m["id"] for m in run], ["2", "4"])
        self.assertTrue(other_follows)

    def test_continuation_stops_at_another_threaded_reply(self):
        msgs = [
            {"id": "2", "content": "one", "author": {"id": CURTIS},
             "message_reference": {"message_id": "1"}},
            {"id": "3", "content": "for other ask", "author": {"id": CURTIS},
             "message_reference": {"message_id": "0"}},
        ]
        self.assertEqual(bk.collect_reply(msgs, "1", {CURTIS}), ["one"])

    def test_check_drops_tickets_older_than_7_days(self):
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "ping", "wait": 0})
        self.assertEqual(len(self.tickets_on_disk()), 1)
        self.now += 7 * 86400 + 1
        out = self.broker.handle({"op": "check"})
        self.assertEqual(out["tickets"], [])
        self.assertEqual(self.tickets_on_disk(), [])
        out = self.broker.handle({"op": "check", "ticket": r["ticket"]})
        self.assertFalse(out["ok"])

    def test_ask_discord_error_leaves_no_ticket(self):
        self.discord.fail_post = bk.BrokerError("Discord rate-limited, try again in 3 s")
        r = self.broker.handle({"op": "ask", "agent": "cage", "text": "ping", "wait": 1})
        self.assertFalse(r["ok"])
        self.assertIn("rate-limited", r["error"])
        self.assertEqual(self.broker.handle({"op": "check"})["tickets"], [])

    def test_check_error_is_per_ticket(self):
        self.broker.handle({"op": "ask", "agent": "cage", "text": "ping", "wait": 0})
        self.discord.fail_messages = bk.BrokerError("Discord said no (HTTP 500).")
        out = self.broker.handle({"op": "check"})
        self.assertTrue(out["ok"])
        t = out["tickets"][0]
        self.assertEqual(t["status"], "working")
        self.assertIn("500", t["error"])

    def test_tickets_survive_restart_and_file_mode(self):
        self.broker.handle({"op": "ask", "agent": "cage", "text": "ping", "wait": 0})
        path = os.path.join(self.tmp, "state", "tickets.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o750)
        again = self.make()
        self.assertEqual(len(again.handle({"op": "check"})["tickets"]), 1)

    def test_log_has_no_bodies_or_token(self):
        self.broker.handle({"op": "post", "channel": "general", "text": "SECRETBODY words"})
        self.broker.handle({"op": "ask", "agent": "cage", "text": "ANOTHERBODY", "wait": 0})
        with open(os.path.join(self.tmp, "broker.log")) as fh:
            log = fh.read()
        self.assertNotIn("SECRETBODY", log)
        self.assertNotIn("ANOTHERBODY", log)
        lines = log.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("post", lines[0])
        self.assertIn("general", lines[0])
        self.assertIn("ok", lines[0])


class TestLogging(BrokerCase):
    def logged(self, **over):
        lines = []
        b = bk.Broker(make_config(self.tmp, **over), self.discord,
                      clock=lambda: self.now, log=lines.append)
        return b, lines

    def test_no_log_file_logs_to_stderr(self):
        b = self.make(log_file=None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            b.handle({"op": "post", "channel": "general", "text": "hello"})
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("post general len=5 ok", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "broker.log")))

    def test_log_file_still_supported(self):
        self.broker.handle({"op": "post", "channel": "general", "text": "hello"})
        with open(os.path.join(self.tmp, "broker.log")) as fh:
            self.assertIn("post general", fh.read())

    def test_repeated_error_lines_collapse_within_a_minute(self):
        b, lines = self.logged()
        for _ in range(50):
            b.handle({"op": "bogus"})
        self.assertEqual(len(lines), 1)
        self.assertIn("unknown", lines[0])
        self.now += 59
        b.handle({"op": "bogus"})
        self.assertEqual(len(lines), 1)
        self.now += 2          # 61 s after the first line
        b.handle({"op": "bogus"})
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("(suppressed 50)"), lines[1])
        # no suppression note when nothing was held back
        self.now += 120
        b.handle({"op": "bogus"})
        self.assertEqual(len(lines), 3)
        self.assertNotIn("suppressed", lines[2])

    def test_distinct_errors_and_ok_lines_are_not_collapsed(self):
        b, lines = self.logged()
        for _ in range(3):
            b.handle({"op": "post", "channel": "general", "text": "hi"})
        b.handle({"op": "post", "channel": "nope", "text": "hi"})
        b.handle({"op": "read", "channel": "nope"})
        b.handle({"op": "post", "channel": "nope", "text": "hi"})
        ok = [ln for ln in lines if " ok" in ln]
        errors = [ln for ln in lines if " error" in ln]
        self.assertEqual(len(ok), 3)
        self.assertEqual(len(errors), 2)

    def test_repeat_table_stays_bounded(self):
        b, _ = self.logged()
        for i in range(bk.LOG_REPEAT_KEYS + 50):
            b.handle({"op": "post", "channel": "no-such-%d" % i, "text": "x"})
            self.now += 1
        self.assertLessEqual(len(b._repeats), bk.LOG_REPEAT_KEYS + 1)


class TestRead(BrokerCase):
    def test_read_returns_speakable_oldest_first(self):
        self.discord.add(GENERAL, HUMAN, "**hello** there", username="operator_user")
        self.discord.add(GENERAL, SONNET, "see <@%s>" % CURTIS, reply_to="5")
        out = self.broker.handle({"op": "read", "channel": "general", "n": 10})
        self.assertTrue(out["ok"])
        first, second = out["messages"]
        self.assertEqual(first["text"], "hello there")
        self.assertEqual(first["author"], "operator_user")
        self.assertEqual(second["author"], "sonnet")
        self.assertEqual(second["text"], "see curtis")
        self.assertEqual(second["reply_to"], "5")
        self.assertIsNone(first["reply_to"])
        self.assertEqual(set(first), {"id", "author", "text", "ts", "reply_to"})

    def test_read_rejects_unlisted_channel_and_caps_n(self):
        self.assertFalse(self.broker.handle({"op": "read", "channel": "nope"})["ok"])
        calls = []
        orig = self.discord.messages
        self.discord.messages = lambda cid, after=None, limit=50: calls.append(limit) or orig(cid, after, limit)
        self.broker.handle({"op": "read", "channel": "general", "n": 500})
        self.broker.handle({"op": "read", "channel": "general"})
        self.assertEqual(calls, [50, 20])


class TestSpeakable(unittest.TestCase):
    def test_speakable_strips_markdown(self):
        agents = {"curtis": {"bot_id": CURTIS}}
        src = ("# Title\nSome **bold** and *italic* and __under__ and `code`.\n"
               "```py\nprint(1)\n```\nSee [the docs](https://example.com/x) "
               "ask <@%s> or <@!%s> or <@%s>. snake_case_name stays." % (CURTIS, CURTIS, HUMAN))
        out = bk.speakable(src, agents)
        self.assertNotIn("#", out)
        self.assertNotIn("*", out)
        self.assertNotIn("`", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("<@", out)
        self.assertIn("Some bold and italic and under and code.", out)
        self.assertIn("See the docs ask curtis or curtis or someone.", out.replace("\n", " "))
        self.assertIn("snake_case_name", out)
        self.assertIn("print(1)", out)


class TestDiscordClient(unittest.TestCase):
    def http_error(self, code, body=b"{}", headers=None):
        return urllib.error.HTTPError("https://discord.com/x", code, "msg",
                                      headers or {}, io.BytesIO(body))

    def client(self, exc):
        def opener(req, timeout=None):
            raise exc
        return bk.Discord(TOKEN, opener=opener)

    def test_429_uses_retry_after_rounded_up(self):
        d = self.client(self.http_error(429, b'{"retry_after": 2.3}'))
        with self.assertRaises(bk.BrokerError) as cm:
            d.messages(GENERAL)
        self.assertEqual(str(cm.exception), "Discord rate-limited, try again in 3 s")

    def test_429_falls_back_to_header(self):
        d = self.client(self.http_error(429, b"not json", {"Retry-After": "4"}))
        with self.assertRaises(bk.BrokerError) as cm:
            d.messages(GENERAL)
        self.assertIn("in 4 s", str(cm.exception))

    def test_other_errors_never_leak_token(self):
        d = self.client(self.http_error(403, b'{"message": "Missing Access"}'))
        with self.assertRaises(bk.BrokerError) as cm:
            d.post(GENERAL, "hi")
        self.assertIn("403", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        d = self.client(urllib.error.URLError("boom " + TOKEN))
        with self.assertRaises(bk.BrokerError) as cm:
            d.post(GENERAL, "hi")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, repr(d))

    def test_react_puts_to_the_encoded_path_with_no_body(self):
        seen = {}

        def opener(req, timeout=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            seen["data"] = req.data
            return io.BytesIO(b"")          # Discord answers 204 with no body

        out = bk.Discord(TOKEN, opener=opener).react(GENERAL, "555", "\N{THUMBS UP SIGN}")
        self.assertIsNone(out)
        self.assertEqual(seen["method"], "PUT")
        self.assertEqual(seen["url"], "%s/channels/%s/messages/555/reactions/%%F0%%9F%%91%%8D/@me"
                         % (bk.API_BASE, GENERAL))
        self.assertIsNone(seen["data"])

    def test_react_encodes_every_reserved_character(self):
        seen = {}

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            return io.BytesIO(b"")

        bk.Discord(TOKEN, opener=opener).react(GENERAL, "5", "a/b?c#d%e")
        self.assertTrue(seen["url"].endswith("/reactions/a%2Fb%3Fc%23d%25e/@me"), seen["url"])

    def test_react_errors_map_like_post(self):
        d = self.client(self.http_error(429, b'{"retry_after": 1.2}'))
        with self.assertRaises(bk.RateLimited) as cm:
            d.react(GENERAL, "5", "\N{THUMBS UP SIGN}")
        self.assertIn("in 2 s", str(cm.exception))
        d = self.client(self.http_error(404, b'{"message": "Unknown Message"}'))
        with self.assertRaises(bk.BrokerError) as cm:
            d.react(GENERAL, "5", "\N{THUMBS UP SIGN}")
        self.assertIn("404", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_messages_query(self):
        seen = {}

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            return io.BytesIO(b"[]")

        bk.Discord(TOKEN, opener=opener).messages(GENERAL, after="12", limit=100)
        self.assertIn("/channels/%s/messages?" % GENERAL, seen["url"])
        self.assertIn("after=12", seen["url"])
        self.assertIn("limit=100", seen["url"])


class RedirectServers:
    """Two local HTTP servers: `front` answers 302 to `back`, which records what
    reaches it. Used to prove an Authorization header is never forwarded."""

    def __init__(self):
        import http.server
        self.back_seen = []
        outer = self

        class Back(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.back_seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"[]")
            do_POST = do_GET

            def log_message(self, *a):
                pass

        self.back = http.server.HTTPServer(("127.0.0.1", 0), Back)

        class Front(http.server.BaseHTTPRequestHandler):
            def _redirect(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/x" % outer.back.server_port)
                self.end_headers()
            do_GET = _redirect
            do_POST = _redirect

            def log_message(self, *a):
                pass

        self.front = http.server.HTTPServer(("127.0.0.1", 0), Front)
        self.threads = [threading.Thread(target=srv.serve_forever, daemon=True)
                        for srv in (self.front, self.back)]
        for t in self.threads:
            t.start()

    @property
    def front_url(self):
        return "http://127.0.0.1:%d" % self.front.server_port

    def close(self):
        for srv in (self.front, self.back):
            srv.shutdown()
            srv.server_close()


class TestNoRedirects(unittest.TestCase):
    def test_discord_client_does_not_follow_redirects(self):
        servers = RedirectServers()
        self.addCleanup(servers.close)
        d = bk.Discord(TOKEN, base=servers.front_url)
        with self.assertRaises(bk.BrokerError) as cm:
            d.messages(GENERAL)
        self.assertIn("HTTP 302", str(cm.exception))
        with self.assertRaises(bk.BrokerError):
            d.post(GENERAL, "hi")
        self.assertEqual(servers.back_seen, [])   # the Authorization header went nowhere


def rpc(path, req, timeout=10):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        try:
            s.sendall(req if isinstance(req, bytes) else json.dumps(req).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass   # the broker already answered and closed ("busy"); read its reply
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf) if buf else None
    finally:
        s.close()


class TestServer(BrokerCase):
    def serve(self, poll=0.05, **over):
        cfg = make_config(self.tmp, **over)
        broker = bk.Broker(cfg, self.discord)
        broker.poll_backoff = (poll,)
        server = bk.make_server(broker, cfg)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        def stop():
            server.shutdown()
            server.server_close()
            t.join(5)
        self.addCleanup(stop)
        return cfg["socket"]

    def test_peer_uid_rejected(self):
        cfg = {"allowed_uid": 4242}
        self.assertTrue(bk.peer_allowed(4242, cfg))
        self.assertFalse(bk.peer_allowed(os.getuid() + 1, cfg))
        path = self.serve(allowed_uid=os.getuid() + 1)
        self.assertEqual(rpc(path, {"op": "check"}), {"ok": False, "error": "not allowed"})

    def test_socket_mode_and_stale_socket(self):
        stale = os.path.join(self.tmp, "broker.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(stale)
        s.close()
        path = self.serve()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o660)
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_socket_group_chowns_the_socket(self):
        fake = mock.Mock(gr_gid=4321)
        with mock.patch.object(bk.grp, "getgrnam", return_value=fake) as g, \
                mock.patch.object(bk.os, "chown") as chown:
            path = self.serve(socket_group="example")
        g.assert_called_once_with("example")
        chown.assert_called_once_with(path, -1, 4321)

    def test_unknown_socket_group_fails_startup_with_one_line(self):
        cfg = make_config(self.tmp, socket_group="no-such-group-here")
        broker = bk.Broker(cfg, self.discord)
        with mock.patch.object(bk.grp, "getgrnam", side_effect=KeyError("x")):
            with self.assertRaises(SystemExit) as cm:
                bk.make_server(broker, cfg)
        self.assertIn("socket_group", str(cm.exception.code))
        self.assertNotIn("\n", str(cm.exception.code))
        self.assertFalse(os.path.exists(cfg["socket"]))

    def test_malformed_input_gets_plain_errors(self):
        path = self.serve(max_line=200)
        for raw in (b"not json\n", b"[1, 2]\n", b'{"op": "dance"}\n', b"x" * 500 + b"\n"):
            r = rpc(path, raw)
            self.assertFalse(r["ok"])
            self.assertIsInstance(r["error"], str)
            self.assertNotIn("Traceback", r["error"])
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_deeply_nested_json_is_a_plain_error(self):
        path = self.serve()
        r = rpc(path, b"[" * 50000 + b"\n")
        self.assertFalse(r["ok"])
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_dribbling_client_is_cut_off_at_the_deadline(self):
        path = self.serve(request_deadline=1)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        stop = threading.Event()

        def dribble():
            while not stop.is_set():
                try:
                    s.sendall(b"a")
                except OSError:
                    return
                time.sleep(0.2)
        t = threading.Thread(target=dribble)
        t.start()
        t0 = time.monotonic()
        try:
            s.settimeout(5)
            data = s.recv(4096)
        finally:
            took = time.monotonic() - t0
            stop.set()
            t.join(5)
            s.close()
        self.assertIn(b"timed out", data)
        self.assertGreater(took, 0.8)
        self.assertLess(took, 2.5)

    def test_ninth_concurrent_connection_gets_busy(self):
        path = self.serve()
        holders = []
        for _ in range(8):
            h = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            h.connect(path)
            holders.append(h)
        time.sleep(0.3)   # let the accept loop take all eight
        try:
            self.assertEqual(rpc(path, {"op": "check"}), {"ok": False, "error": "busy"})
        finally:
            for h in holders:
                h.close()
        end = time.monotonic() + 3
        while True:
            r = rpc(path, {"op": "check"})
            if r["ok"] or time.monotonic() > end:
                break
            time.sleep(0.05)
        self.assertTrue(r["ok"])

    def test_rate_limit_holds_across_concurrent_connections(self):
        path = self.serve()
        results = []

        def go(i):
            results.append(rpc(path, {"op": "post", "channel": "general", "text": "m%d" % i}))
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sum(r["ok"] for r in results), 6)
        self.assertEqual(len(self.discord.posts), 6)
        self.assertTrue(all("Too many messages" in r["error"] for r in results if not r["ok"]))

    def test_second_broker_on_a_live_socket_refuses_to_start(self):
        path = self.serve()
        cfg = make_config(self.tmp)
        with self.assertRaises(bk.BrokerError) as cm:
            bk.make_server(bk.Broker(cfg, self.discord), cfg)
        self.assertIn("broker already running", str(cm.exception))
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_unanswering_socket_path_is_never_unlinked(self):
        path = os.path.join(self.tmp, "broker.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        s.close()
        cfg = make_config(self.tmp)
        for exc in (PermissionError("no"), TimeoutError("slow")):
            with mock.patch.object(socket.socket, "connect", side_effect=exc):
                with self.assertRaises(SystemExit) as cm:
                    bk.make_server(bk.Broker(cfg, self.discord), cfg)
            self.assertEqual(str(cm.exception.code), "socket path in use and not answering; "
                             "remove it if no broker is running")
            self.assertTrue(os.path.exists(path))

    def test_handler_exception_does_not_kill_server(self):
        path = self.serve()
        self.discord.messages = None  # calling it raises TypeError inside read
        r = rpc(path, {"op": "read", "channel": "general"})
        self.assertFalse(r["ok"])
        self.assertNotIn("Traceback", r["error"])
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_slow_ask_does_not_block_read(self):
        path = self.serve(poll=0.3)
        self.discord.add(GENERAL, HUMAN, "hello")
        posted = threading.Event()
        self.discord.on_post = lambda rec: posted.set()
        result = {}

        def do_ask():
            result["ask"] = rpc(path, {"op": "ask", "agent": "cage", "text": "slow", "wait": 3})

        t = threading.Thread(target=do_ask)
        t.start()
        self.assertTrue(posted.wait(5))
        t0 = time.monotonic()
        r = rpc(path, {"op": "read", "channel": "general"}, timeout=2)
        took = time.monotonic() - t0
        self.assertTrue(r["ok"])
        self.assertLess(took, 3)
        self.assertTrue(t.is_alive(), "ask should still be waiting")
        ticket = rpc(path, {"op": "check"}, timeout=2)
        self.assertEqual(ticket["tickets"][0]["status"], "working")
        t.join(10)
        self.assertEqual(result["ask"]["status"], "working")


if __name__ == "__main__":
    unittest.main()
