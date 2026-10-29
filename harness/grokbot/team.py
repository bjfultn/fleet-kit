#!/usr/bin/env python3
"""team: the grok-bot client. Talks to the broker over its unix socket.

Installed for the grok-bot uid as /usr/local/bin/team. Each call sends one JSON
line to the broker and reads one JSON line back. Output is read aloud by a
voice assistant, so it is plain text and short.

    team post <channel> <text...>
    team reply <channel> <message-id> <text...>
    team ask <agent|team> <text...> [--wait N]
    team check [ticket]
    team read <channel> [n]

The socket is $TEAM_SOCKET, or /run/grokbot-broker/team.sock.

Stdlib only.
"""

import argparse
import json
import os
import socket
import sys

DEFAULT_SOCKET = "/run/grokbot-broker/team.sock"
DEFAULT_WAIT = 60
OP_TIMEOUT = 30          # seconds, for every op that is not ask
WAIT_SLACK = 30          # seconds added to an ask's wait for the round trip

NOT_RUNNING = "team: the team helper isn't running"
BUSY = "team: the team helper is busy, try again in a moment"
NO_ANSWER = "team: no answer from the team helper"


def socket_path():
    return os.environ.get("TEAM_SOCKET") or DEFAULT_SOCKET


def unix_connect(request, timeout):
    """The real call: one request line out, one response line back."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path())
        sock.sendall(json.dumps(request).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    line = buf.split(b"\n", 1)[0]
    if not line:
        # The broker closed without answering: its connection cap, or a crash.
        raise ConnectionResetError("no response")
    return json.loads(line)


def _text(words):
    return " ".join(words)


def _build(args):
    """Turn parsed args into (request, timeout) for the broker."""
    op = args.op
    if op == "post":
        return {"op": "post", "channel": args.channel, "text": _text(args.text)}, OP_TIMEOUT
    if op == "reply":
        return ({"op": "reply", "channel": args.channel, "message_id": args.message_id,
                 "text": _text(args.text)}, OP_TIMEOUT)
    if op == "ask":
        return ({"op": "ask", "agent": args.agent, "text": _text(args.text),
                 "wait": args.wait}, args.wait + WAIT_SLACK)
    if op == "check":
        req = {"op": "check"}
        if args.ticket is not None:
            req["ticket"] = args.ticket
        return req, OP_TIMEOUT
    if op == "read":
        req = {"op": "read", "channel": args.channel}
        if args.n is not None:
            req["n"] = args.n
        return req, OP_TIMEOUT
    raise ValueError("unknown op %r" % op)


def _parser():
    ap = argparse.ArgumentParser(prog="team", description="Talk to the team over the broker.")
    sub = ap.add_subparsers(dest="op", required=True)

    p = sub.add_parser("post", help="post a message to a channel")
    p.add_argument("channel")
    p.add_argument("text", nargs="+")

    p = sub.add_parser("reply", help="reply to a message")
    p.add_argument("channel")
    p.add_argument("message_id")
    p.add_argument("text", nargs="+")

    p = sub.add_parser("ask", help="ask an agent, or the team, a question")
    p.add_argument("agent")
    p.add_argument("text", nargs="+")
    p.add_argument("--wait", type=int, default=DEFAULT_WAIT,
                   help="seconds to wait for an answer (default %d)" % DEFAULT_WAIT)

    p = sub.add_parser("check", help="check open questions")
    p.add_argument("ticket", nargs="?")

    p = sub.add_parser("read", help="read recent messages in a channel")
    p.add_argument("channel")
    p.add_argument("n", nargs="?", type=int)

    return ap


def _fail(message):
    print(message, file=sys.stderr)
    return 1


def _format_ask(resp):
    if resp.get("status") == "answered":
        return resp.get("reply") or ""
    return "Still working. Ticket %s. Run: team check %s" % (resp.get("ticket"),
                                                             resp.get("ticket"))


def _format_check(rows):
    if not rows:
        return "No open questions."
    lines = []
    for row in rows:
        head = "%s %s:" % (row.get("ticket"), row.get("agent"))
        if row.get("status") == "answered":
            lines.append("%s %s" % (head, row.get("reply") or ""))
        elif row.get("error"):
            lines.append("%s couldn't check (%s)" % (head, row["error"]))
        else:
            lines.append("%s still working" % head)
    return "\n".join(lines)


def _format_read(messages):
    if not messages:
        return "No messages."
    return "\n".join("%s: %s" % (m.get("author"), (m.get("text") or "").replace("\n", " "))
                     for m in messages)


def _format_ok(op, resp):
    if op == "ask":
        return _format_ask(resp)
    if op == "check":
        return _format_check(resp.get("tickets") or [])
    if op == "read":
        return _format_read(resp.get("messages") or [])
    return "Sent."


def main(argv, connect=None):
    """Run one team command. Returns the exit status."""
    if connect is None:
        connect = unix_connect
    args = _parser().parse_args(argv)
    request, timeout = _build(args)
    try:
        resp = connect(request, timeout)
    except (FileNotFoundError, ConnectionRefusedError, PermissionError):
        return _fail(NOT_RUNNING)
    except (ConnectionResetError, BrokenPipeError):
        return _fail(BUSY)
    except (socket.timeout, TimeoutError):
        return _fail(NO_ANSWER)
    except ValueError:
        return _fail("team: the team helper sent a reply I couldn't read")

    if not isinstance(resp, dict):
        return _fail("team: the team helper sent a reply I couldn't read")
    if not resp.get("ok"):
        error = resp.get("error") or "unknown error"
        if error == "busy":
            return _fail(BUSY)
        return _fail("team: %s" % error)

    print(_format_ok(args.op, resp))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
