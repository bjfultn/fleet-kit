"""Tests for harness/grokbot/team.py, the client the grok-bot uid runs.

No network. Most tests inject a fake connect; the default unix-socket path is
exercised for real against a one-shot server on a temp unix socket.
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import broker as bk  # noqa: E402
import team  # noqa: E402

TEAM_PY = os.path.join(HERE, "..", "team.py")

NOT_RUNNING = "team: the team helper isn't running\n"
BUSY = "team: the team helper is busy, try again in a moment\n"
NO_ANSWER = "team: no answer from the team helper\n"


class FakeBroker:
    """Stands in for the socket call: records each request and timeout, then
    returns the scripted response or raises the scripted error."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def run(argv, connect):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        code = team.main(argv, connect=connect)
    return code, out.getvalue(), err.getvalue()


class OneShotServer:
    """Accepts one connection on a unix socket, records the request line, then
    hands the connection to `handler`. Used for the real-socket tests."""

    def __init__(self, path, handler):
        self.handler = handler
        self.seen = None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        conn, _ = self.sock.accept()
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.seen = buf
            self.handler(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(timeout=5)
        self.sock.close()


def reply_with(obj):
    def handler(conn):
        conn.sendall(json.dumps(obj).encode() + b"\n")
    return handler


def close_silently(conn):
    return None


class TeamTests(unittest.TestCase):

    def test_ask_prints_reply(self):
        fake = FakeBroker({"ok": True, "ticket": "ab12cd34", "status": "answered",
                           "reply": "Sonnet says hi"})
        code, out, err = run(["ask", "sonnet", "--wait", "5", "hello", "there"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Sonnet says hi\n")
        self.assertEqual(err, "")
        request, timeout = fake.calls[0]
        self.assertEqual(request, {"op": "ask", "agent": "sonnet",
                                   "text": "hello there", "wait": 5})
        self.assertEqual(timeout, team.ask_timeout(5))

    def test_ask_working_prints_ticket_hint(self):
        fake = FakeBroker({"ok": True, "ticket": "ab12cd34", "status": "working",
                           "reply": None})
        code, out, err = run(["ask", "team", "anyone home"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Still working. Ticket ab12cd34. "
                              "Run: team check ab12cd34\n")
        self.assertEqual(err, "")
        request, timeout = fake.calls[0]
        self.assertEqual(request["wait"], 60)
        self.assertEqual(timeout, team.ask_timeout(60))

    def test_team_reports_broker_error(self):
        fake = FakeBroker({"ok": False, "error": "I don't know that agent."})
        code, out, err = run(["ask", "nobody", "hi"], fake)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "team: I don't know that agent.\n")

    def test_broker_down_message(self):
        for exc in (FileNotFoundError(), ConnectionRefusedError(), PermissionError()):
            with self.subTest(exc=type(exc).__name__):
                code, out, err = run(["post", "general", "hi"], FakeBroker(error=exc))
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err, NOT_RUNNING)

    def test_read_formats_lines(self):
        fake = FakeBroker({"ok": True, "messages": [
            {"id": "1", "author": "Sonnet", "text": "hi", "ts": "", "reply_to": None},
            {"id": "2", "author": "Curtis", "text": "second\nline", "ts": "", "reply_to": None},
        ]})
        code, out, err = run(["read", "general"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Sonnet: hi\nCurtis: second line\n")
        self.assertEqual(err, "")
        self.assertEqual(fake.calls[0][0], {"op": "read", "channel": "general"})

    def test_read_with_count_and_none(self):
        fake = FakeBroker({"ok": True, "messages": []})
        code, out, _ = run(["read", "general", "5"], fake)
        self.assertEqual((code, out), (0, "No messages.\n"))
        self.assertEqual(fake.calls[0][0], {"op": "read", "channel": "general", "n": 5})

    def test_post_joins_words(self):
        fake = FakeBroker({"ok": True, "message_id": "42"})
        code, out, err = run(["post", "general", "hello", "there"], fake)
        self.assertEqual((code, out, err), (0, "Sent.\n", ""))
        self.assertEqual(fake.calls[0][0],
                         {"op": "post", "channel": "general", "text": "hello there"})

    def test_reply_sends_message_id(self):
        fake = FakeBroker({"ok": True, "message_id": "43"})
        code, out, _ = run(["reply", "general", "12345", "on", "it"], fake)
        self.assertEqual((code, out), (0, "Sent.\n"))
        self.assertEqual(fake.calls[0][0], {"op": "reply", "channel": "general",
                                            "message_id": "12345", "text": "on it"})

    def test_check_formats_one_line_per_ticket(self):
        fake = FakeBroker({"ok": True, "tickets": [
            {"ticket": "t1", "agent": "sonnet", "status": "answered", "reply": "yes",
             "asked_at": 2.0},
            {"ticket": "t2", "agent": "curtis", "status": "working", "reply": None,
             "asked_at": 1.0},
            {"ticket": "t3", "agent": "cage", "status": "working", "reply": None,
             "asked_at": 0.5, "error": "rate limited"},
        ]})
        code, out, err = run(["check"], fake)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "t1 sonnet: yes\n"
                              "t2 curtis: still working\n"
                              "t3 cage: couldn't check (rate limited)\n")
        self.assertEqual(fake.calls[0][0], {"op": "check"})

    def test_check_single_ticket_and_none_open(self):
        fake = FakeBroker({"ok": True, "tickets": []})
        code, out, _ = run(["check", "t9"], fake)
        self.assertEqual((code, out), (0, "No open questions.\n"))
        self.assertEqual(fake.calls[0][0], {"op": "check", "ticket": "t9"})

    def test_busy_on_reset_or_broken_pipe(self):
        for exc in (ConnectionResetError(), BrokenPipeError()):
            with self.subTest(exc=type(exc).__name__):
                code, out, err = run(["post", "general", "hi"], FakeBroker(error=exc))
                self.assertEqual((code, out, err), (1, "", BUSY))

    def test_busy_when_broker_answers_busy(self):
        fake = FakeBroker({"ok": False, "error": "busy"})
        code, out, err = run(["post", "general", "hi"], fake)
        self.assertEqual((code, out, err), (1, "", BUSY))

    def test_no_answer_on_timeout(self):
        fake = FakeBroker(error=socket.timeout())
        code, out, err = run(["read", "general"], fake)
        self.assertEqual((code, out, err), (1, "", NO_ANSWER))

    def test_ask_timeout_says_it_may_have_been_sent(self):
        fake = FakeBroker(error=socket.timeout())
        code, out, err = run(["ask", "sonnet", "hi"], fake)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("may already have been sent", err)
        self.assertIn("team check", err)

    def test_ask_timeout_outlasts_the_brokers_worst_case(self):
        # post, then the wait, then a final poll of MAX_PAGES Discord calls
        for wait in (0, 1, 60, 80, 120, 500):
            worst = (min(wait, bk.MAX_WAIT) + bk.HTTP_TIMEOUT
                     + bk.MAX_PAGES * bk.HTTP_TIMEOUT)
            self.assertGreater(team.ask_timeout(wait), worst, wait)
        self.assertEqual((team.BROKER_HTTP_TIMEOUT, team.BROKER_MAX_PAGES,
                          team.BROKER_MAX_WAIT),
                         (bk.HTTP_TIMEOUT, bk.MAX_PAGES, bk.MAX_WAIT))

    def test_check_says_how_many_older_answers_were_left_out(self):
        fake = FakeBroker({"ok": True, "omitted": 4, "tickets": [
            {"ticket": "t1", "agent": "cage", "status": "answered", "reply": "done"}]})
        code, out, err = run(["check"], fake)
        self.assertEqual(code, 0)
        self.assertIn("t1 cage: done", out)
        self.assertIn("4 older answered not shown", out)

    def test_timeouts_for_ask_and_other_ops(self):
        post = FakeBroker({"ok": True, "message_id": "1"})
        run(["post", "general", "hi"], post)
        self.assertEqual(post.calls[0][1], 30)
        ask = FakeBroker({"ok": True, "ticket": "t", "status": "working", "reply": None})
        run(["ask", "sonnet", "--wait", "10", "hi"], ask)
        self.assertEqual(ask.calls[0][1], team.ask_timeout(10))

    def test_socket_path_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("TEAM_SOCKET", None)
            self.assertEqual(team.socket_path(), "/run/grokbot-broker/team.sock")
            env["TEAM_SOCKET"] = "/tmp/elsewhere.sock"
            self.assertEqual(team.socket_path(), "/tmp/elsewhere.sock")

    def test_dash_text_is_not_an_option(self):
        fake = FakeBroker({"ok": True, "message_id": "42"})
        run(["post", "general", "-hello", "there"], fake)
        self.assertEqual(fake.calls[-1][0]["text"], "-hello there")
        run(["post", "general", "hello", "-world"], fake)
        self.assertEqual(fake.calls[-1][0]["text"], "hello -world")
        run(["reply", "general", "12345", "-ok", "then"], fake)
        self.assertEqual(fake.calls[-1][0]["text"], "-ok then")
        self.assertEqual(fake.calls[-1][0]["message_id"], "12345")
        ask = FakeBroker({"ok": True, "ticket": "t", "status": "working", "reply": None})
        run(["ask", "sonnet", "-hello", "there"], ask)
        self.assertEqual(ask.calls[0][0], {"op": "ask", "agent": "sonnet",
                                           "text": "-hello there", "wait": 60})

    def test_wait_before_dash_text(self):
        ask = FakeBroker({"ok": True, "ticket": "t", "status": "working", "reply": None})
        run(["ask", "sonnet", "--wait", "5", "-hello", "there"], ask)
        self.assertEqual(ask.calls[0][0], {"op": "ask", "agent": "sonnet",
                                           "text": "-hello there", "wait": 5})
        self.assertEqual(ask.calls[0][1], team.ask_timeout(5))
        run(["ask", "sonnet", "--wait=7", "hi"], ask)
        self.assertEqual(ask.calls[1][0]["wait"], 7)
        self.assertEqual(ask.calls[1][0]["text"], "hi")

    def _ask(self, argv):
        fake = FakeBroker({"ok": True, "ticket": "t", "status": "working", "reply": None})
        code, out, err = run(argv, fake)
        self.assertEqual((code, err), (0, ""), argv)
        return fake.calls[0][0]

    def test_ask_wait_right_after_ask(self):
        req = self._ask(["ask", "--wait", "0", "sonnet", "-x", "y"])
        self.assertEqual(req, {"op": "ask", "agent": "sonnet", "text": "-x y", "wait": 0})
        req = self._ask(["ask", "--wait=4", "sonnet", "hi"])
        self.assertEqual((req["wait"], req["text"]), (4, "hi"))

    def test_ask_wait_right_after_agent(self):
        req = self._ask(["ask", "sonnet", "--wait", "5", "hi", "there"])
        self.assertEqual((req["wait"], req["text"]), (5, "hi there"))
        req = self._ask(["ask", "sonnet", "--wait=7", "hi"])
        self.assertEqual((req["wait"], req["text"]), (7, "hi"))

    def test_ask_wait_trailing(self):
        req = self._ask(["ask", "sonnet", "hi", "there", "--wait", "5"])
        self.assertEqual((req["wait"], req["text"]), (5, "hi there"))
        req = self._ask(["ask", "sonnet", "for", "me", "--wait=9"])
        self.assertEqual((req["wait"], req["text"]), (9, "for me"))

    def test_ask_wait_only_in_three_places(self):
        # Mid-text, or a trailing --wait with no int, stays literal text.
        req = self._ask(["ask", "sonnet", "please", "--wait", "for", "me"])
        self.assertEqual((req["wait"], req["text"]), (60, "please --wait for me"))
        req = self._ask(["ask", "sonnet", "hi", "--wait"])
        self.assertEqual((req["wait"], req["text"]), (60, "hi --wait"))
        req = self._ask(["ask", "sonnet", "hi", "--wait", "abc"])
        self.assertEqual((req["wait"], req["text"]), (60, "hi --wait abc"))
        # Only the trailing pair is the wait; the mid-text one is text.
        req = self._ask(["ask", "sonnet", "a", "--wait", "5", "b", "--wait", "7"])
        self.assertEqual((req["wait"], req["text"]), (7, "a --wait 5 b"))

    def test_oserror_is_not_running(self):
        fake = FakeBroker(error=OSError("AF_UNIX path too long"))
        code, out, err = run(["check"], fake)
        self.assertEqual((code, out, err), (1, "", NOT_RUNNING))


class RealSocketTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "team.sock")
        env = mock.patch.dict(os.environ, {"TEAM_SOCKET": self.path})
        env.start()
        self.addCleanup(env.stop)

    def test_default_connect_round_trip(self):
        with OneShotServer(self.path, reply_with({"ok": True, "message_id": "42"})) as srv:
            code, out, err = run(["post", "general", "hello", "there"], None)
        self.assertEqual((code, out, err), (0, "Sent.\n", ""))
        self.assertEqual(json.loads(srv.seen),
                         {"op": "post", "channel": "general", "text": "hello there"})

    def test_default_connect_busy_when_closed_without_reply(self):
        with OneShotServer(self.path, close_silently):
            code, out, err = run(["post", "general", "hi"], None)
        self.assertEqual((code, out, err), (1, "", BUSY))

    def test_default_connect_busy_line(self):
        with OneShotServer(self.path, reply_with({"ok": False, "error": "busy"})):
            code, out, err = run(["check"], None)
        self.assertEqual((code, out, err), (1, "", BUSY))

    def test_default_connect_broker_down(self):
        code, out, err = run(["check"], None)
        self.assertEqual((code, out, err), (1, "", NOT_RUNNING))

    def test_overlong_socket_path_is_not_running(self):
        self.path = "/tmp/" + "x" * 120
        with mock.patch.dict(os.environ, {"TEAM_SOCKET": self.path}):
            code, out, err = run(["check"], None)
        self.assertEqual((code, out, err), (1, "", NOT_RUNNING))

    def test_cli_overlong_socket_path_has_no_traceback(self):
        env = dict(os.environ, TEAM_SOCKET="/tmp/" + "x" * 120)
        proc = subprocess.run([sys.executable, TEAM_PY, "post", "general", "hi"],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, NOT_RUNNING)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
