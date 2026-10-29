"""Tests for harness/grokbot/broker.py.

No network and no real token. Ids below are placeholders. FakeDiscord stands in
for the REST client; the Discord class itself is tested through an injected
opener, and the socket server is exercised for real on a temp unix socket.
"""

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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import broker as bk  # noqa: E402

GENERAL = "300000000000000001"
ALBERT = "300000000000000002"
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
        "channels": {"albert": ALBERT, "general": GENERAL, "curtis": CURTIS_CH,
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

    def __init__(self):
        self.posts = []
        self.fail_post = None
        self.fail_messages = None
        self.on_post = None
        self._next = 1000
        self._msgs = {}
        self._lock = threading.Lock()

    def _id(self):
        with self._lock:
            self._next += 1
            return str(self._next)

    def add(self, channel_id, author_id, content, reply_to=None, username="someone"):
        msg = {"id": self._id(), "content": content, "timestamp": "2026-10-28T12:00:00+00:00",
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

    def messages(self, channel_id, after=None, limit=50):
        if self.fail_messages:
            raise self.fail_messages
        out = [m for m in self._msgs.get(channel_id, [])
               if after is None or int(m["id"]) > int(after)]
        out.sort(key=lambda m: int(m["id"]), reverse=True)
        return out[:limit]


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        self.now = 1_800_000_000.0
        self.discord = FakeDiscord()
        self.broker = self.make()

    def make(self, **over):
        b = bk.Broker(make_config(self.tmp, **over), self.discord, clock=lambda: self.now)
        b.poll_interval = 0.01
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
        self.assertIn("DiscordBot", seen["headers"]["user-agent"])
        self.assertEqual(seen["timeout"], 15)

    def test_mention_rewrite_ignores_emails_and_urls(self):
        agents = make_config("/x")["agents"]
        text, ids = bk.mentions("mail bob@sonnet.com or https://x.io/@sonnet or a@curtis", agents)
        self.assertEqual(ids, [])
        self.assertEqual(text, "mail bob@sonnet.com or https://x.io/@sonnet or a@curtis")
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

    def test_failed_post_does_not_burn_rate(self):
        self.discord.fail_post = bk.BrokerError("Discord said no (HTTP 500).")
        for _ in range(8):
            r = self.broker.handle({"op": "post", "channel": "general", "text": "x"})
            self.assertFalse(r["ok"])
        self.discord.fail_post = None
        self.assertTrue(self.broker.handle(
            {"op": "post", "channel": "general", "text": "x"})["ok"])

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


class TestRead(BrokerCase):
    def test_read_returns_speakable_oldest_first(self):
        self.discord.add(GENERAL, HUMAN, "**hello** there", username="bj_user")
        self.discord.add(GENERAL, SONNET, "see <@%s>" % CURTIS, reply_to="5")
        out = self.broker.handle({"op": "read", "channel": "general", "n": 10})
        self.assertTrue(out["ok"])
        first, second = out["messages"]
        self.assertEqual(first["text"], "hello there")
        self.assertEqual(first["author"], "bj_user")
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

    def test_messages_query(self):
        seen = {}

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            return io.BytesIO(b"[]")

        bk.Discord(TOKEN, opener=opener).messages(GENERAL, after="12", limit=100)
        self.assertIn("/channels/%s/messages?" % GENERAL, seen["url"])
        self.assertIn("after=12", seen["url"])
        self.assertIn("limit=100", seen["url"])


def rpc(path, req, timeout=10):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(req if isinstance(req, bytes) else json.dumps(req).encode() + b"\n")
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
    def serve(self, **over):
        cfg = make_config(self.tmp, **over)
        broker = bk.Broker(cfg, self.discord)
        broker.poll_interval = 0.05
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

    def test_malformed_input_gets_plain_errors(self):
        path = self.serve(max_line=200)
        for raw in (b"not json\n", b"[1, 2]\n", b'{"op": "dance"}\n', b"x" * 500 + b"\n"):
            r = rpc(path, raw)
            self.assertFalse(r["ok"])
            self.assertIsInstance(r["error"], str)
            self.assertNotIn("Traceback", r["error"])
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_handler_exception_does_not_kill_server(self):
        path = self.serve()
        self.discord.messages = None  # calling it raises TypeError inside read
        r = rpc(path, {"op": "read", "channel": "general"})
        self.assertFalse(r["ok"])
        self.assertNotIn("Traceback", r["error"])
        self.assertTrue(rpc(path, {"op": "check"})["ok"])

    def test_slow_ask_does_not_block_read(self):
        path = self.serve()
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
        self.assertLess(took, 1.5)
        self.assertTrue(t.is_alive(), "ask should still be waiting")
        ticket = rpc(path, {"op": "check"}, timeout=2)
        self.assertEqual(ticket["tickets"][0]["status"], "working")
        t.join(10)
        self.assertEqual(result["ask"]["status"], "working")


if __name__ == "__main__":
    unittest.main()
