"""Tests for bin/grokbot_wake.py: waking Grok Bot agents by routine webhook.

No Discord, no network, no real pass store. Ids below are placeholders.
"""

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "bin"))

import grokbot_wake as gw  # noqa: E402

BOT = "111111111111111111"        # the grok-bot agent's own bot id
PRIVATE = "222222222222222222"    # its private channel
GENERAL = "333333333333333333"    # a shared channel it can see
OTHER = "444444444444444444"      # a channel it must never see
HUMAN = "555555555555555555"      # allowlisted human
TEAMBOT = "666666666666666666"    # allowlisted bot
STRANGER = "777777777777777777"   # on no list

LOCAL = ZoneInfo("America/Chicago")


def agent(**over):
    a = {
        "alias": "gizmo",
        "bot_id": BOT,
        "private_channel_id": PRIVATE,
        "allowed_channels": {PRIVATE, GENERAL},
        "allow_from": {HUMAN},
        "allow_bots": {TEAMBOT},
    }
    a.update(over)
    return a


def msg(author=HUMAN, content="hello", mid="900000000000000001", mentions=(),
        everyone=False, roles=(), reply_to=None, bot=False, username="someone"):
    m = {
        "id": mid,
        "content": content,
        "timestamp": "2026-10-28T12:00:00.000000+00:00",
        "author": {"id": author, "username": username, "bot": bot},
        "mentions": [{"id": i} for i in mentions],
        "mention_everyone": everyone,
        "mention_roles": list(roles),
    }
    if reply_to:
        m["message_reference"] = {"message_id": reply_to}
    return m


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def noon():
    return datetime(2026, 10, 28, 12, 0, tzinfo=LOCAL)


def at_2am():
    return datetime(2026, 10, 28, 2, 0, tzinfo=LOCAL)


class Resp:
    def __init__(self, status):
        self.status = status

    def getcode(self):
        return self.status

    def read(self):
        return b""

    def close(self):
        pass


def ok_runner(stdout="https://hooks.example.invalid/w/abc\nsecret-key-123\n"):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


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


class RedirectTests(unittest.TestCase):
    def test_webhook_opener_does_not_follow_redirects(self):
        servers = RedirectServers()
        self.addCleanup(servers.close)
        req = urllib.request.Request(servers.front_url + "/hook", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer secret-key-123"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            gw.no_redirect_open(req, timeout=5)
        cm.exception.close()
        self.assertEqual(cm.exception.code, 302)
        self.assertEqual(servers.back_seen, [])

    def test_wake_uses_the_no_redirect_opener_by_default(self):
        import inspect
        default = inspect.signature(gw.wake_grokbot).parameters["opener"].default
        self.assertIs(default, gw.no_redirect_open)

    def test_a_redirect_is_a_failed_wake_not_a_follow(self):
        def opener(req, timeout=None, **kw):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, io.BytesIO(b""))
        logs = []
        ok = gw.wake_grokbot("gizmo", "fleet/x", {"messages": []}, logs.append,
                             opener=opener, runner=ok_runner())
        self.assertFalse(ok)
        self.assertTrue(any("HTTP 302" in line for line in logs))


class PayloadTests(unittest.TestCase):
    def test_build_payload_fields(self):
        m = msg(content="hi <@111111111111111111> <script>", username="operator")
        p = gw.build_payload(m, GENERAL)
        self.assertEqual(set(p), {"channel_id", "message_id", "author",
                                  "author_id", "text", "ts"})
        self.assertEqual(p["channel_id"], GENERAL)
        self.assertEqual(p["message_id"], "900000000000000001")
        self.assertEqual(p["author"], "operator")
        self.assertEqual(p["author_id"], HUMAN)
        self.assertEqual(p["ts"], "2026-10-28T12:00:00.000000+00:00")
        # sanitized: discord mention markup survives, other tags are neutered
        self.assertIn("<@111111111111111111>", p["text"])
        self.assertIn("&lt;script>", p["text"])

    def test_build_payload_truncates(self):
        p = gw.build_payload(msg(content="x" * 5000), GENERAL)
        self.assertEqual(len(p["text"]), 1500)
        p = gw.build_payload(msg(content="<" * 5000), GENERAL)
        self.assertLessEqual(len(p["text"]), 1500)


class WebhookTests(unittest.TestCase):
    def test_load_webhook_parses_two_lines(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://hooks.example.invalid/w/abc\nkey-1\n", stderr="")

        self.assertEqual(gw.load_webhook("fleet/x", runner=run),
                         ("https://hooks.example.invalid/w/abc", "key-1"))
        self.assertEqual(calls[0][-1], "fleet/x")

    def test_load_webhook_none_when_pass_fails_or_malformed(self):
        def bad(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope")
        self.assertIsNone(gw.load_webhook("k", runner=bad))
        self.assertIsNone(gw.load_webhook("k", runner=ok_runner("onlyoneline\n")))
        # a bearer key must never travel over plain http
        self.assertIsNone(gw.load_webhook("k", runner=ok_runner("http://h.example.invalid/\nk\n")))

    def test_wake_with_locked_secret_logs_and_returns(self):
        logs, opened = [], []

        def locked(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="gpg: decryption failed")

        ok = gw.wake_grokbot("gizmo", "fleet/x", {"messages": []}, logs.append,
                             opener=lambda *a, **k: opened.append(1), runner=locked)
        self.assertFalse(ok)
        self.assertEqual(opened, [])
        self.assertEqual(len(logs), 1)
        self.assertIn("secret store locked", logs[0])

    def test_wake_posts_bearer_json(self):
        seen = {}

        def opener(req, timeout=None, **kw):
            seen["req"], seen["timeout"] = req, timeout
            return Resp(200)

        logs = []
        body = {"messages": [{"message_id": "1"}]}
        ok = gw.wake_grokbot("gizmo", "fleet/x", body, logs.append,
                             opener=opener, runner=ok_runner())
        self.assertTrue(ok)
        req = seen["req"]
        self.assertEqual(req.full_url, "https://hooks.example.invalid/w/abc")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-key-123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data), body)
        self.assertEqual(seen["timeout"], 10)
        self.assertTrue(any("Woke gizmo" in line for line in logs))
        # neither the key nor the url may reach the log
        for line in logs:
            self.assertNotIn("secret-key-123", line)
            self.assertNotIn("hooks.example.invalid", line)

    def test_wake_non_2xx_is_false_and_logged(self):
        for opener in (
            lambda req, timeout=None, **k: Resp(500),
            lambda req, timeout=None, **k: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, io.BytesIO(b""))),
        ):
            logs = []
            ok = gw.wake_grokbot("gizmo", "fleet/x", {"messages": []}, logs.append,
                                 opener=opener, runner=ok_runner())
            self.assertFalse(ok)
            self.assertEqual(len(logs), 1)
            self.assertRegex(logs[0], r"50\d")
            self.assertNotIn("secret-key-123", logs[0])
            self.assertNotIn("hooks.example.invalid", logs[0])

    def test_wake_network_error_is_false(self):
        def opener(req, timeout=None, **k):
            raise urllib.error.URLError(TimeoutError("timed out"))
        logs = []
        self.assertFalse(gw.wake_grokbot("gizmo", "k", {"messages": []}, logs.append,
                                         opener=opener, runner=ok_runner()))
        self.assertEqual(len(logs), 1)


    def test_wake_http_exception_is_false_not_raised(self):
        def opener(req, timeout=None, **k):
            raise http.client.RemoteDisconnected("closed")
        logs = []
        self.assertFalse(gw.wake_grokbot("gizmo", "k", {"messages": []}, logs.append,
                                         opener=opener, runner=ok_runner()))
        self.assertEqual(len(logs), 1)
        self.assertIn("RemoteDisconnected", logs[0])


class TargetTests(unittest.TestCase):
    def test_tag_wakes(self):
        m = msg(mentions=[BOT])
        self.assertTrue(gw.grokbot_targets(m, GENERAL, agent(), {}))

    def test_private_channel_untagged_wakes(self):
        self.assertTrue(gw.grokbot_targets(msg(), PRIVATE, agent(), {}))

    def test_bot_author_on_allow_bots_wakes(self):
        m = msg(author=TEAMBOT, mentions=[BOT], bot=True)
        self.assertTrue(gw.grokbot_targets(m, GENERAL, agent(), {}))

    def test_own_message_never_wakes(self):
        m = msg(author=BOT, mentions=[BOT], bot=True)
        a = agent(allow_bots={TEAMBOT, BOT})
        self.assertFalse(gw.grokbot_targets(m, PRIVATE, a, {}))

    def test_other_channel_untagged_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(), GENERAL, agent(), {}))

    def test_everyone_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(everyone=True), GENERAL, agent(), {}))

    def test_role_tag_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(roles=["888888888888888888"]),
                                            GENERAL, agent(), {}))

    def test_tag_of_someone_else_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(mentions=["999999999999999999"]),
                                            GENERAL, agent(), {}))

    def test_grokbot_enforces_allowlist_when_watcher_enforce_off(self):
        old = os.environ.get("MENTION_WATCHER_ENFORCE")
        os.environ["MENTION_WATCHER_ENFORCE"] = "0"
        try:
            # unlisted author in the private channel
            self.assertFalse(gw.grokbot_targets(
                msg(author=STRANGER, mentions=[BOT]), PRIVATE, agent(), {}))
            # allowed author, tagged, but in a channel the agent cannot see
            self.assertFalse(gw.grokbot_targets(
                msg(author=HUMAN, mentions=[BOT]), OTHER, agent(), {}))
        finally:
            if old is None:
                del os.environ["MENTION_WATCHER_ENFORCE"]
            else:
                os.environ["MENTION_WATCHER_ENFORCE"] = old

    def test_empty_allowlists_fail_closed(self):
        a = agent(allowed_channels=set(), allow_from=set(), allow_bots=set())
        self.assertFalse(gw.grokbot_targets(msg(mentions=[BOT]), PRIVATE, a, {}))

    def test_reply_to_own_ask_does_not_wake(self):
        # a reply the broker recorded as consumed does not wake
        ask = "800000000000000001"
        m = msg(author=TEAMBOT, mentions=[BOT], reply_to=ask, bot=True)
        asks = {ask: gw.Ask(reply_ids={m["id"]}, working=False, authors={TEAMBOT})}
        self.assertFalse(gw.grokbot_targets(m, PRIVATE, agent(), asks))
        # a reply to some other message still wakes
        m2 = msg(author=TEAMBOT, mid="900000000000000002", mentions=[BOT],
                 reply_to="800000000000000002", bot=True)
        self.assertTrue(gw.grokbot_targets(m2, PRIVATE, agent(), asks))

    def test_on_it_then_result_second_reply_wakes(self):
        ask = "800000000000000001"
        first = msg(author=TEAMBOT, mid="900000000000000001", mentions=[BOT],
                    reply_to=ask, bot=True, content="on it")
        later = msg(author=TEAMBOT, mid="900000000000000009", mentions=[BOT],
                    reply_to=ask, bot=True, content="here is the result")
        # the broker captured "on it" and closed the ticket
        asks = {ask: gw.Ask(reply_ids={first["id"]}, working=False, authors={TEAMBOT})}
        self.assertFalse(gw.grokbot_targets(first, PRIVATE, agent(), asks))
        self.assertTrue(gw.grokbot_targets(later, PRIVATE, agent(), asks))

    def test_human_reply_to_ask_wakes(self):
        ask = "800000000000000001"
        m = msg(author=HUMAN, mentions=[BOT], reply_to=ask)
        # even while the ticket is working: a human is not an expected author
        asks = {ask: gw.Ask(working=True, authors={TEAMBOT})}
        self.assertTrue(gw.grokbot_targets(m, PRIVATE, agent(), asks))

    def test_non_target_agent_reply_wakes(self):
        ask = "800000000000000001"
        other_bot = "888888888888888888"
        m = msg(author=other_bot, mentions=[BOT], reply_to=ask, bot=True)
        a = agent(allow_bots={TEAMBOT, other_bot})
        asks = {ask: gw.Ask(working=True, authors={TEAMBOT})}
        self.assertTrue(gw.grokbot_targets(m, PRIVATE, a, asks))

    def test_working_ticket_and_expected_author_is_suppressed(self):
        ask = "800000000000000001"
        m = msg(author=TEAMBOT, mentions=[BOT], reply_to=ask, bot=True)
        asks = {ask: gw.Ask(working=True, authors={TEAMBOT})}
        self.assertFalse(gw.grokbot_targets(m, PRIVATE, agent(), asks))
        # the same message once the ticket is answered without it: wakes
        asks = {ask: gw.Ask(working=False, authors={TEAMBOT})}
        self.assertTrue(gw.grokbot_targets(m, PRIVATE, agent(), asks))

    def test_old_ticket_without_fields_claims_nothing(self):
        ask = "800000000000000001"
        m = msg(author=TEAMBOT, mentions=[BOT], reply_to=ask, bot=True)
        self.assertTrue(gw.grokbot_targets(m, PRIVATE, agent(), {ask: gw.Ask()}))

    def test_everyone_in_private_channel_wakes_but_not_in_general(self):
        # a human @everyone in the agent's own channel is addressed to it
        self.assertTrue(gw.grokbot_targets(msg(everyone=True), PRIVATE, agent(), {}))
        self.assertFalse(gw.grokbot_targets(msg(everyone=True), GENERAL, agent(), {}))


class TicketTests(unittest.TestCase):
    def test_reads_ask_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tickets.json")
            with open(p, "w") as fh:
                json.dump({"tickets": [
                    {"ticket": "t1", "agent": "a", "channel_id": "1",
                     "ask_message_id": "11", "asked_at": 1, "status": "open", "reply": None},
                    {"ticket": "t2", "agent": "a", "channel_id": "1",
                     "ask_message_id": "12", "asked_at": 2, "status": "done", "reply": "x"},
                ]}, fh)
            self.assertEqual(gw.TicketReader(p, lambda s: None).ask_ids(), {"11", "12"})

    def test_reads_reply_ids_status_and_authors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tickets.json")
            with open(p, "w") as fh:
                json.dump({"tickets": [
                    {"ticket": "t1", "ask_message_id": "11", "status": "working",
                     "reply_message_ids": [], "expected_author_ids": [TEAMBOT]},
                    {"ticket": "t2", "ask_message_id": "12", "status": "answered",
                     "reply_message_ids": ["21", "22"], "expected_author_ids": [TEAMBOT]},
                    {"ticket": "t3", "ask_message_id": "13", "status": "working"},
                ]}, fh)
            asks = gw.TicketReader(p, lambda s: None).asks()
            self.assertTrue(asks["11"].working)
            self.assertEqual(asks["11"].authors, {TEAMBOT})
            self.assertEqual(asks["12"].reply_ids, {"21", "22"})
            self.assertFalse(asks["12"].working)
            # an old ticket with neither field claims nothing
            self.assertFalse(asks["13"].captured("99", TEAMBOT))

    def test_rewritten_file_is_picked_up(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tickets.json")
            r = gw.TicketReader(p, lambda s: None)
            for status in ("working", "answered"):
                with open(p + ".new", "w") as fh:
                    json.dump({"tickets": [{"ask_message_id": "11", "status": status}]}, fh)
                os.rename(p + ".new", p)
                self.assertEqual(r.asks()["11"].working, status == "working")

    def test_missing_or_garbage_file_is_empty_and_logs_once_per_interval(self):
        logs = []
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as d:
            r = gw.TicketReader(os.path.join(d, "none.json"), logs.append, clock)
            self.assertEqual(r.ask_ids(), set())
            self.assertEqual(r.asks(), {})
            clock.t += gw.TICKET_WARN_INTERVAL - 1
            self.assertEqual(r.ask_ids(), set())
            self.assertEqual(len(logs), 1)
            self.assertIn("WARNING", logs[0])
            clock.t += 2
            r.ask_ids()
            self.assertEqual(len(logs), 2)
            p = os.path.join(d, "bad.json")
            with open(p, "w") as fh:
                fh.write("{not json")
            logs2 = []
            r2 = gw.TicketReader(p, logs2.append, FakeClock())
            self.assertEqual(r2.ask_ids(), set())
            self.assertEqual(r2.ask_ids(), set())
            self.assertEqual(len(logs2), 1)

    def test_unset_path_is_empty_and_quiet(self):
        logs = []
        self.assertEqual(gw.TicketReader("", logs.append).ask_ids(), set())
        self.assertEqual(logs, [])


class GateTests(unittest.TestCase):
    def test_gate_coalesces_burst(self):
        clock = FakeClock()
        g = gw.WakeGate(clock)
        verdicts = []
        for i in range(3):
            v = g.admit("gizmo", "t%d" % i, False, noon())
            verdicts.append(v)
            p = {"message_id": str(i)}
            if v != "drop":
                g.hold("gizmo", p, False)
            if v == "send":
                self.assertEqual(g.drain("gizmo", noon(), force=True), [p])
            clock.t += 5
        self.assertEqual(verdicts, ["send", "hold", "hold"])
        # still in flight: nothing admissible yet
        self.assertEqual(g.drain("gizmo", noon()), [])
        clock.t += 120
        self.assertEqual([p["message_id"] for p in g.drain("gizmo", noon())], ["1", "2"])
        self.assertEqual(g.drain("gizmo", noon()), [])

    def test_other_agents_are_independent(self):
        g = gw.WakeGate(FakeClock())
        self.assertEqual(g.admit("a", "t", False, noon()), "send")
        self.assertEqual(g.admit("b", "t", False, noon()), "send")

    def test_send_after_window_is_send(self):
        clock = FakeClock()
        g = gw.WakeGate(clock)
        self.assertEqual(g.admit("a", "t", False, noon()), "send")
        clock.t += 120
        self.assertEqual(g.admit("a", "t", False, noon()), "send")

    def _wake(self, g, alias, channel, is_bot, n, t=None):
        """admit + hold + drain, the way GrokWaker drives the gate."""
        v = g.admit(alias, channel, is_bot, t or noon())
        if v == "send":
            g.hold(alias, {"message_id": str(n), "channel_id": channel}, is_bot)
            g.drain(alias, t or noon(), force=True)
        return v

    def test_channel_hop_cap(self):
        clock = FakeClock()
        logs = []
        g = gw.WakeGate(clock, log=logs.append)
        out = []
        for n in range(5):
            out.append(self._wake(g, "gizmo", GENERAL, True, n))
            clock.t += 200
        self.assertEqual(out, ["send"] * 4 + ["drop"])
        self.assertEqual(len(logs), 1)
        self.assertIn(GENERAL, logs[0])
        # a different channel and a different agent are unaffected
        self.assertEqual(self._wake(g, "gizmo", PRIVATE, True, 9), "send")
        self.assertEqual(self._wake(g, "other", GENERAL, True, 9), "send")
        # humans are not capped, and do not spend the budget
        for n in range(6):
            clock.t += 200
            self.assertEqual(self._wake(g, "gizmo", GENERAL, False, n), "send")
        # the cap is per hour
        clock.t += 3600
        self.assertEqual(self._wake(g, "gizmo", GENERAL, True, 99), "send")

    def test_hop_cap_counts_at_send_time_not_admit_time(self):
        clock = FakeClock()
        g = gw.WakeGate(clock, quiet_hours={"gizmo"})
        # six bot messages overnight: all held, none dropped, none counted
        for n in range(6):
            self.assertEqual(g.admit("gizmo", GENERAL, True, at_2am()), "hold")
            g.hold("gizmo", {"message_id": str(n), "channel_id": GENERAL}, True)
        morning = datetime(2026, 10, 28, 7, 0, tzinfo=LOCAL)
        clock.t += 8 * 3600
        # they go out as one wake, which counts once
        self.assertEqual(len(g.drain("gizmo", morning)), 6)
        clock.t += 200
        self.assertEqual(self._wake(g, "gizmo", GENERAL, True, 50, morning), "send")

    def test_failed_send_does_not_spend_hop_budget(self):
        clock = FakeClock()
        g = gw.WakeGate(clock)
        for n in range(gw.HOP_CAP):
            self.assertEqual(g.admit("a", GENERAL, True, noon()), "send")
            g.hold("a", {"message_id": str(n), "channel_id": GENERAL}, True)
            g.drain("a", noon(), force=True)
            g.failed("a")
            clock.t += 200
        self.assertEqual(g.admit("a", GENERAL, True, noon()), "send")

    def test_quiet_hours_hold_bots_not_humans(self):
        clock = FakeClock()
        g = gw.WakeGate(clock, quiet_hours={"gizmo"})
        self.assertEqual(g.admit("gizmo", "t1", True, at_2am()), "hold")
        g.hold("gizmo", {"message_id": "bot1"}, True)
        clock.t += 500
        self.assertEqual(g.admit("gizmo", "t2", False, at_2am()), "send")
        g.hold("gizmo", {"message_id": "human1"}, False)
        # a human wake carries the held bot message with it
        self.assertEqual([p["message_id"] for p in g.drain("gizmo", at_2am(), force=True)],
                         ["bot1", "human1"])

    def test_quiet_hours_off_by_default_and_outside_window(self):
        g = gw.WakeGate(FakeClock())
        self.assertEqual(g.admit("gizmo", "t", True, at_2am()), "send")
        g2 = gw.WakeGate(FakeClock(), quiet_hours={"gizmo"})
        self.assertEqual(g2.admit("gizmo", "t", True, noon()), "send")
        for hour, expect in ((21, "send"), (22, "hold"), (6, "hold"), (7, "send")):
            g3 = gw.WakeGate(FakeClock(), quiet_hours={"gizmo"})
            t = datetime(2026, 10, 28, hour, 30, tzinfo=LOCAL)
            self.assertEqual(g3.admit("gizmo", "t", True, t), expect, hour)

    def test_quiet_drain_keeps_bot_messages_until_morning(self):
        clock = FakeClock()
        g = gw.WakeGate(clock, quiet_hours={"gizmo"})
        g.hold("gizmo", {"message_id": "b"}, True)
        clock.t += 1000
        self.assertEqual(g.drain("gizmo", at_2am()), [])
        morning = datetime(2026, 10, 28, 7, 0, tzinfo=LOCAL)
        self.assertEqual([p["message_id"] for p in g.drain("gizmo", morning)], ["b"])

    def test_failed_send_requeues_and_backs_off(self):
        clock = FakeClock()
        g = gw.WakeGate(clock)
        self.assertEqual(g.admit("a", "t", False, noon()), "send")
        g.hold("a", {"message_id": "1"}, False)
        batch = g.drain("a", noon(), force=True)
        self.assertEqual(len(batch), 1)
        g.failed("a")
        self.assertEqual(g.drain("a", noon()), [])        # backed off
        clock.t += 120
        self.assertEqual(g.drain("a", noon()), batch)     # retried

    def test_held_queue_is_bounded(self):
        g = gw.WakeGate(FakeClock())
        for i in range(gw.MAX_HELD + 10):
            g.hold("a", {"message_id": str(i)}, False)
        got = g.drain("a", noon(), force=True)
        self.assertEqual(len(got), gw.MAX_HELD)
        self.assertEqual(got[-1]["message_id"], str(gw.MAX_HELD + 9))


class LoaderTests(unittest.TestCase):
    def _write(self, root, name, cfg, access=None):
        d = os.path.join(root, name)
        os.makedirs(d)
        with open(os.path.join(d, "config.json"), "w") as fh:
            json.dump(cfg, fh)
        if access is not None:
            state = os.path.join(root, "state-" + name)
            os.makedirs(state)
            with open(os.path.join(state, "access.json"), "w") as fh:
                json.dump(access, fh)

    def test_grokbot_agent_not_in_tmux_map(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "claudey", {
                "alias": "claudey",
                "discord": {"bot_id": "100000000000000001",
                            "private_channel_id": "200000000000000001"}})
            self._write(root, "gizmo", {
                "platform": "grok-bot", "alias": "gizmo",
                "discord": {"bot_id": BOT, "private_channel_id": PRIVATE,
                            "state_dir": os.path.join(root, "state-gizmo")},
                "grokbot": {"webhook_pass_key": "fleet/gizmo-webhook",
                            "tickets_file": "/nonexistent/tickets.json",
                            "quiet_hours": True}},
                access={"allowFrom": [HUMAN], "allowBots": [TEAMBOT],
                        "groups": {PRIVATE: {}, GENERAL: {"allowFrom": []}}})
            agents, groks, channels = gw.load_fleet_agents(root, general_channel_id=OTHER)
            self.assertEqual(agents, {"100000000000000001": "claudey"})
            self.assertEqual(set(groks), {"gizmo"})
            g = groks["gizmo"]
            self.assertEqual(g["bot_id"], BOT)
            self.assertEqual(g["private_channel_id"], PRIVATE)
            self.assertEqual(g["allowed_channels"], {PRIVATE, GENERAL})
            self.assertEqual(g["allow_from"], {HUMAN})
            self.assertEqual(g["allow_bots"], {TEAMBOT})
            self.assertEqual(g["webhook_pass_key"], "fleet/gizmo-webhook")
            self.assertEqual(g["tickets_file"], "/nonexistent/tickets.json")
            self.assertTrue(g["quiet_hours"])
            for c in (OTHER, "200000000000000001", PRIVATE, GENERAL):
                self.assertIn(c, channels)
            self.assertEqual(len(channels), len(set(channels)))

    def test_grokbot_without_access_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "gizmo", {
                "platform": "grok-bot", "alias": "gizmo",
                "discord": {"bot_id": BOT, "private_channel_id": PRIVATE,
                            "state_dir": os.path.join(root, "nowhere")}})
            _, groks, channels = gw.load_fleet_agents(root)
            g = groks["gizmo"]
            self.assertEqual(g["allowed_channels"], set())
            self.assertFalse(gw.grokbot_targets(msg(mentions=[BOT]), PRIVATE, g, {}))
            self.assertIn(PRIVATE, channels)

    def test_missing_bot_id_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "gizmo", {"platform": "grok-bot", "alias": "gizmo",
                                        "discord": {}})
            with self.assertRaises(gw.FleetConfigError):
                gw.load_fleet_agents(root)

    def test_access_refresh_picks_up_edits(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "gizmo", {
                "platform": "grok-bot", "alias": "gizmo",
                "discord": {"bot_id": BOT, "private_channel_id": PRIVATE,
                            "state_dir": os.path.join(root, "state-gizmo")}},
                access={"allowFrom": [HUMAN], "groups": {PRIVATE: {}}})
            _, groks, _ = gw.load_fleet_agents(root)
            g = groks["gizmo"]
            self.assertEqual(g["allow_from"], {HUMAN})
            p = os.path.join(root, "state-gizmo", "access.json")
            with open(p, "w") as fh:
                json.dump({"allowFrom": [], "allowBots": [TEAMBOT], "groups": {}}, fh)
            os.utime(p, (1, 1))  # force a distinct mtime
            gw.refresh_access(g, lambda s: None)
            self.assertEqual(g["allow_from"], set())
            self.assertEqual(g["allow_bots"], {TEAMBOT})
            self.assertEqual(g["allowed_channels"], set())


    def test_quiet_tz_defaults_and_overrides(self):
        for tz_cfg, want in ((None, "UTC"), ("Europe/London", "Europe/London")):
            with tempfile.TemporaryDirectory() as root:
                gb = {"quiet_hours": True}
                if tz_cfg:
                    gb["quiet_tz"] = tz_cfg
                self._write(root, "gizmo", {
                    "platform": "grok-bot", "alias": "gizmo",
                    "discord": {"bot_id": BOT, "private_channel_id": PRIVATE},
                    "grokbot": gb})
                _, groks, _ = gw.load_fleet_agents(root)
                self.assertEqual(groks["gizmo"]["quiet_tz"], want)

    def test_bad_quiet_tz_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "gizmo", {
                "platform": "grok-bot", "alias": "gizmo",
                "discord": {"bot_id": BOT, "private_channel_id": PRIVATE},
                "grokbot": {"quiet_hours": True, "quiet_tz": "Not/AZone"}})
            with self.assertRaises(gw.FleetConfigError):
                gw.load_fleet_agents(root)


def gagent(**over):
    a = agent(webhook_pass_key="fleet/x", tickets_file="", quiet_hours=False,
              quiet_tz="America/Chicago", allowed_channels={PRIVATE, GENERAL})
    a.update(over)
    return a


class WakerHarness:
    """A GrokWaker with fakes for send, log, clock and local time."""

    def __init__(self, agents, tickets=None, send=None, now=None):
        self.agents = agents
        self.logs = []
        self.sent = []          # (alias, body)
        self.clock = FakeClock()
        self.local = now or noon()
        self.tz_asked = []
        self.fail = set()       # aliases whose send returns False
        self.raise_for = set()  # aliases whose send raises
        self.gate = gw.WakeGate(
            self.clock, quiet_hours={a for a, x in agents.items() if x["quiet_hours"]},
            log=self.logs.append)
        tk = tickets or {a: gw.TicketReader("", self.logs.append, self.clock) for a in agents}

        def send_(alias, key, body):
            if alias in self.raise_for:
                raise http.client.RemoteDisconnected("boom")
            if alias in self.fail:
                return False
            self.sent.append((alias, body))
            return True

        def now_(tz):
            self.tz_asked.append(tz)
            return self.local

        self.w = gw.GrokWaker(agents, self.gate, tk, send or send_, self.logs.append, now_)

    def ids(self, i=-1):
        return [m["message_id"] for m in self.sent[i][1]["messages"]]


class WakerTests(unittest.TestCase):
    def test_bot_reply_chain_hits_the_channel_cap(self):
        # bot -> agent -> bot -> agent ...: each message replies to the one
        # before it, so a per-reply-parent key would never accumulate.
        h = WakerHarness({"gizmo": gagent()})
        prev = None
        n = 0
        sent_counts = []
        for i in range(5):
            n += 1
            bot_id = "9%017d" % n
            h.w.handle(msg(author=TEAMBOT, mid=bot_id, reply_to=prev, bot=True,
                           mentions=[BOT]), GENERAL)
            sent_counts.append(len(h.sent))
            n += 1
            prev = "9%017d" % n       # the agent's own reply, which never wakes
            h.w.handle(msg(author=BOT, mid=prev, reply_to=bot_id, bot=True), GENERAL)
            h.clock.t += 200
        self.assertEqual(sent_counts, [1, 2, 3, 4, 4])
        self.assertTrue(any("DROP" in line for line in h.logs))
        # a different channel is unaffected
        h.w.handle(msg(author=TEAMBOT, mid="9" + "0" * 16 + "7", bot=True), PRIVATE)
        self.assertEqual(len(h.sent), 5)

    def test_send_exception_requeues_and_retries(self):
        h = WakerHarness({"gizmo": gagent()})
        h.raise_for = {"gizmo"}
        h.w.handle(msg(mid="900000000000000001"), PRIVATE)   # must not raise
        self.assertEqual(h.sent, [])
        self.assertTrue(any("RemoteDisconnected" in line for line in h.logs))
        h.raise_for = set()
        h.w.drain()                       # still backing off
        self.assertEqual(h.sent, [])
        h.clock.t += gw.IN_FLIGHT_SECONDS
        h.w.drain()
        self.assertEqual(h.ids(), ["900000000000000001"])

    def test_failed_send_returning_false_requeues(self):
        h = WakerHarness({"gizmo": gagent()})
        h.fail = {"gizmo"}
        h.w.handle(msg(mid="900000000000000001"), PRIVATE)
        h.fail = set()
        h.clock.t += gw.IN_FLIGHT_SECONDS
        h.w.drain()
        self.assertEqual(h.ids(), ["900000000000000001"])

    def test_one_failing_agent_does_not_stop_the_others_draining(self):
        other_bot = "121212121212121212"
        h = WakerHarness({
            "a": gagent(alias="a"),
            "b": gagent(alias="b", bot_id=other_bot, private_channel_id=GENERAL,
                        allowed_channels={GENERAL}),
        })
        # a message in each agent's private channel, then a second one held
        h.w.handle(msg(mid="900000000000000001"), PRIVATE)
        h.w.handle(msg(mid="900000000000000002"), GENERAL)
        h.w.handle(msg(mid="900000000000000003"), PRIVATE)
        h.w.handle(msg(mid="900000000000000004"), GENERAL)
        self.assertEqual(len(h.sent), 2)
        h.clock.t += gw.IN_FLIGHT_SECONDS
        h.raise_for = {"a"}
        h.w.drain()
        self.assertEqual([x[0] for x in h.sent], ["a", "b", "b"])
        self.assertEqual(h.ids(), ["900000000000000004"])
        h.raise_for = set()
        h.clock.t += gw.IN_FLIGHT_SECONDS
        h.w.drain()
        self.assertEqual(h.sent[-1][0], "a")
        self.assertEqual(h.ids(), ["900000000000000003"])

    def test_error_in_one_agent_does_not_block_handling_for_the_next(self):
        a = gagent(alias="a")
        b = gagent(alias="b", bot_id="121212121212121212")
        h = WakerHarness({"a": a, "b": b})
        h.w.tickets["a"] = None   # a's ticket lookup now raises
        h.w.handle(msg(mid="900000000000000001"), PRIVATE)
        self.assertEqual([x[0] for x in h.sent], ["b"])
        self.assertTrue(any("error handling" in line for line in h.logs))

    def test_quiet_hours_hold_then_morning_drain(self):
        h = WakerHarness({"gizmo": gagent(quiet_hours=True, quiet_tz="Europe/London")},
                         now=at_2am())
        h.w.handle(msg(author=TEAMBOT, mid="900000000000000001", bot=True), PRIVATE)
        h.clock.t += 500
        h.w.drain()
        self.assertEqual(h.sent, [])
        self.assertEqual(set(h.tz_asked), {"Europe/London"})
        h.local = datetime(2026, 10, 28, 7, 0, tzinfo=LOCAL)
        h.w.drain()
        self.assertEqual(len(h.sent), 1)
        self.assertEqual(h.ids(), ["900000000000000001"])

    def test_quiet_hours_human_goes_straight_through(self):
        h = WakerHarness({"gizmo": gagent(quiet_hours=True)}, now=at_2am())
        h.w.handle(msg(mid="900000000000000001"), PRIVATE)
        self.assertEqual(len(h.sent), 1)

    def test_non_quiet_agent_never_asks_for_local_time(self):
        h = WakerHarness({"gizmo": gagent()})
        h.w.handle(msg(mid="900000000000000001"), PRIVATE)
        h.w.drain()
        self.assertEqual(h.tz_asked, [])

    def test_unaddressed_and_unlisted_messages_send_nothing(self):
        h = WakerHarness({"gizmo": gagent()})
        h.w.handle(msg(mid="900000000000000001"), GENERAL)                         # untagged
        h.w.handle(msg(author=STRANGER, mid="900000000000000002", mentions=[BOT]), GENERAL)
        self.assertEqual(h.sent, [])

    def _ticket_harness(self, d, ticket):
        p = os.path.join(d, "t.json")
        with open(p, "w") as fh:
            json.dump({"tickets": [ticket]}, fh)
        logs = []
        return WakerHarness({"gizmo": gagent(tickets_file=p)},
                            tickets={"gizmo": gw.TicketReader(p, logs.append)})

    def test_reply_to_own_ask_is_ignored_via_tickets(self):
        with tempfile.TemporaryDirectory() as d:
            h = self._ticket_harness(d, {
                "ask_message_id": "800000000000000001", "status": "answered",
                "reply_message_ids": ["900000000000000001"],
                "expected_author_ids": [TEAMBOT]})
            h.w.handle(msg(author=TEAMBOT, mid="900000000000000001", bot=True,
                           reply_to="800000000000000001"), PRIVATE)
            self.assertEqual(h.sent, [])

    def test_unconsumed_reply_to_own_ask_wakes(self):
        with tempfile.TemporaryDirectory() as d:
            h = self._ticket_harness(d, {
                "ask_message_id": "800000000000000001", "status": "answered",
                "reply_message_ids": ["900000000000000001"],
                "expected_author_ids": [TEAMBOT]})
            h.w.handle(msg(author=TEAMBOT, mid="900000000000000002", bot=True,
                           reply_to="800000000000000001"), PRIVATE)
            self.assertEqual(h.ids(), ["900000000000000002"])

    def test_startup_check_warns(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "none.json")
            logs = []
            clock = FakeClock()
            agents = {
                "a": gagent(alias="a", allowed_channels={GENERAL}, tickets_file=missing),
                "b": gagent(alias="b", webhook_pass_key="", tickets_file=""),
                "c": gagent(alias="c", tickets_file=missing),
            }
            h = WakerHarness(agents, tickets={
                "a": gw.TicketReader(missing, logs.append, clock),
                "b": gw.TicketReader("", logs.append, clock),
                "c": gw.TicketReader(missing, logs.append, clock)})
            h.w.log = logs.append
            h.w.startup_check()
            text = "\n".join(logs)
            self.assertIn("a: private channel", text)
            self.assertNotIn("c: private channel", text)
            self.assertIn("b: no grokbot.webhook_pass_key", text)
            self.assertIn("b: no grokbot.tickets_file", text)
            self.assertEqual(text.count("none.json"), 2)   # a and c, unreadable


if __name__ == "__main__":
    unittest.main()
