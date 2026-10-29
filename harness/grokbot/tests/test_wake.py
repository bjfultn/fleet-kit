"""Tests for bin/grokbot_wake.py: waking Grok Bot agents by routine webhook.

No Discord, no network, no real pass store. Ids below are placeholders.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
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

DENVER = ZoneInfo("America/Denver")


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
    return datetime(2026, 10, 28, 12, 0, tzinfo=DENVER)


def at_2am():
    return datetime(2026, 10, 28, 2, 0, tzinfo=DENVER)


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


class PayloadTests(unittest.TestCase):
    def test_build_payload_fields(self):
        m = msg(content="hi <@111111111111111111> <script>", username="bj")
        p = gw.build_payload(m, GENERAL)
        self.assertEqual(set(p), {"channel_id", "message_id", "author",
                                  "author_id", "text", "ts"})
        self.assertEqual(p["channel_id"], GENERAL)
        self.assertEqual(p["message_id"], "900000000000000001")
        self.assertEqual(p["author"], "bj")
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


class TargetTests(unittest.TestCase):
    def test_tag_wakes(self):
        m = msg(mentions=[BOT])
        self.assertTrue(gw.grokbot_targets(m, GENERAL, agent(), set()))

    def test_private_channel_untagged_wakes(self):
        self.assertTrue(gw.grokbot_targets(msg(), PRIVATE, agent(), set()))

    def test_bot_author_on_allow_bots_wakes(self):
        m = msg(author=TEAMBOT, mentions=[BOT], bot=True)
        self.assertTrue(gw.grokbot_targets(m, GENERAL, agent(), set()))

    def test_own_message_never_wakes(self):
        m = msg(author=BOT, mentions=[BOT], bot=True)
        a = agent(allow_bots={TEAMBOT, BOT})
        self.assertFalse(gw.grokbot_targets(m, PRIVATE, a, set()))

    def test_other_channel_untagged_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(), GENERAL, agent(), set()))

    def test_everyone_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(everyone=True), GENERAL, agent(), set()))

    def test_role_tag_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(roles=["888888888888888888"]),
                                            GENERAL, agent(), set()))

    def test_tag_of_someone_else_does_not_wake(self):
        self.assertFalse(gw.grokbot_targets(msg(mentions=["999999999999999999"]),
                                            GENERAL, agent(), set()))

    def test_grokbot_enforces_allowlist_when_watcher_enforce_off(self):
        old = os.environ.get("MENTION_WATCHER_ENFORCE")
        os.environ["MENTION_WATCHER_ENFORCE"] = "0"
        try:
            # unlisted author in the private channel
            self.assertFalse(gw.grokbot_targets(
                msg(author=STRANGER, mentions=[BOT]), PRIVATE, agent(), set()))
            # allowed author, tagged, but in a channel the agent cannot see
            self.assertFalse(gw.grokbot_targets(
                msg(author=HUMAN, mentions=[BOT]), OTHER, agent(), set()))
        finally:
            if old is None:
                del os.environ["MENTION_WATCHER_ENFORCE"]
            else:
                os.environ["MENTION_WATCHER_ENFORCE"] = old

    def test_empty_allowlists_fail_closed(self):
        a = agent(allowed_channels=set(), allow_from=set(), allow_bots=set())
        self.assertFalse(gw.grokbot_targets(msg(mentions=[BOT]), PRIVATE, a, set()))

    def test_reply_to_albert_ask_does_not_wake(self):
        ask = "800000000000000001"
        m = msg(author=TEAMBOT, mentions=[BOT], reply_to=ask, bot=True)
        self.assertFalse(gw.grokbot_targets(m, PRIVATE, agent(), {ask}))
        # a reply to some other message still wakes
        m2 = msg(author=TEAMBOT, mentions=[BOT], reply_to="800000000000000002", bot=True)
        self.assertTrue(gw.grokbot_targets(m2, PRIVATE, agent(), {ask}))

    def test_thread_id(self):
        self.assertEqual(gw.thread_id(msg(mid="5")), "5")
        self.assertEqual(gw.thread_id(msg(mid="5", reply_to="3")), "3")


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

    def test_missing_or_garbage_file_is_empty_and_logs_once(self):
        logs = []
        with tempfile.TemporaryDirectory() as d:
            r = gw.TicketReader(os.path.join(d, "none.json"), logs.append)
            self.assertEqual(r.ask_ids(), set())
            self.assertEqual(r.ask_ids(), set())
            self.assertEqual(len(logs), 1)
            p = os.path.join(d, "bad.json")
            with open(p, "w") as fh:
                fh.write("{not json")
            logs2 = []
            r2 = gw.TicketReader(p, logs2.append)
            self.assertEqual(r2.ask_ids(), set())
            self.assertEqual(r2.ask_ids(), set())
            self.assertEqual(len(logs2), 1)

    def test_unset_path_is_empty(self):
        self.assertEqual(gw.TicketReader("", lambda s: None).ask_ids(), set())


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

    def test_thread_hop_cap(self):
        clock = FakeClock()
        logs = []
        g = gw.WakeGate(clock, log=logs.append)
        out = []
        for _ in range(5):
            out.append(g.admit("gizmo", "thread-1", True, noon()))
            clock.t += 200
        self.assertEqual(out, ["send"] * 4 + ["drop"])
        self.assertEqual(len(logs), 1)
        # a different thread is unaffected
        self.assertEqual(g.admit("gizmo", "thread-2", True, noon()), "send")
        # humans are not capped
        for _ in range(6):
            clock.t += 200
            self.assertEqual(g.admit("gizmo", "thread-1", False, noon()), "send")
        # the cap is per hour
        clock.t += 3600
        self.assertEqual(g.admit("gizmo", "thread-1", True, noon()), "send")

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
            t = datetime(2026, 10, 28, hour, 30, tzinfo=DENVER)
            self.assertEqual(g3.admit("gizmo", "t", True, t), expect, hour)

    def test_quiet_drain_keeps_bot_messages_until_morning(self):
        clock = FakeClock()
        g = gw.WakeGate(clock, quiet_hours={"gizmo"})
        g.hold("gizmo", {"message_id": "b"}, True)
        clock.t += 1000
        self.assertEqual(g.drain("gizmo", at_2am()), [])
        morning = datetime(2026, 10, 28, 7, 0, tzinfo=DENVER)
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
            self.assertFalse(gw.grokbot_targets(msg(mentions=[BOT]), PRIVATE, g, set()))
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


if __name__ == "__main__":
    unittest.main()
