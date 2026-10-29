#!/usr/bin/env python3
"""team: the grok-bot client. Talks to the broker over its unix socket.

Installed for the grok-bot uid as /usr/local/bin/team. Each call sends one JSON
line to the broker and reads one JSON line back. Output is read aloud by a
voice assistant, so it is plain text and short.

    team post <channel> <text...>
    team reply <channel> <message-id> <text...>
    team ask <agent|team> <text...> [--wait N]
    team check [ticket]
    team read <channel> [n] [--ids]
    team react <channel> <message-id> <emoji>
    team brief

The socket is $TEAM_SOCKET, or /run/grokbot-broker/team.sock.

Stdlib only.
"""

import argparse
import json
import os
import re
import socket
import sys

DEFAULT_SOCKET = "/run/grokbot-broker/team.sock"
DEFAULT_WAIT = 60
OP_TIMEOUT = 30          # seconds, for every op that is not ask

# The broker's worst case for an ask is: the post (one Discord call, up to
# BROKER_HTTP_TIMEOUT), then the wait (the broker clamps it to BROKER_MAX_WAIT),
# then a final poll that began just before the deadline and can make
# BROKER_MAX_PAGES Discord calls. These mirror HTTP_TIMEOUT, MAX_WAIT and
# MAX_PAGES in broker.py (a test keeps them in step). The margin covers the
# socket round trip and a loaded box.
BROKER_HTTP_TIMEOUT = 15
BROKER_MAX_WAIT = 120
BROKER_MAX_PAGES = 3
ASK_MARGIN = 15

NOT_RUNNING = "team: the team helper isn't running"
BUSY = "team: the team helper is busy, try again in a moment"
NO_ANSWER = "team: no answer from the team helper"
NO_ANSWER_ASK = ("team: no answer from the team helper in time. The ask may already "
                 "have been sent. Run: team check")


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


def ask_timeout(wait):
    """Seconds the client will wait for the broker to answer an ask."""
    return (max(0, min(wait, BROKER_MAX_WAIT)) + BROKER_HTTP_TIMEOUT
            + BROKER_MAX_PAGES * BROKER_HTTP_TIMEOUT + ASK_MARGIN)


def _build(args):
    """Turn parsed args into (request, timeout) for the broker."""
    op = args.op
    if op == "post":
        return {"op": "post", "channel": args.channel, "text": _text(args.text)}, OP_TIMEOUT
    if op == "reply":
        return ({"op": "reply", "channel": args.channel, "message_id": args.message_id,
                 "text": _text(args.text)}, OP_TIMEOUT)
    if op == "react":
        return ({"op": "react", "channel": args.channel, "message_id": args.message_id,
                 "emoji": args.emoji}, OP_TIMEOUT)
    if op == "brief":
        return {"op": "brief"}, OP_TIMEOUT
    if op == "ask":
        return ({"op": "ask", "agent": args.agent, "text": _text(args.text),
                 "wait": args.wait}, ask_timeout(args.wait))
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


def _is_count(text):
    return re.fullmatch(r"[0-9]+", text) is not None


def _wait_at(tokens, i):
    """(seconds, width) when tokens[i] is `--wait N` or `--wait=N`, else None.
    A --wait with no whole number after it is not a wait option."""
    if i < 0 or i >= len(tokens):
        return None
    tok = tokens[i]
    if tok == "--wait":
        if i + 1 < len(tokens) and _is_count(tokens[i + 1]):
            return int(tokens[i + 1]), 2
        return None
    if tok.startswith("--wait=") and _is_count(tok[len("--wait="):]):
        return int(tok[len("--wait="):]), 1
    return None


def _normalize_ask(argv):
    """ask takes --wait in three places only: right after 'ask', right after
    the agent, or as the final token(s). Those are pulled out; everything else
    after the agent is text, placed after '--' so a leading '-' stays literal."""
    rest = argv[1:]
    wait = None
    pos = 0
    while _wait_at(rest, pos):
        wait, width = _wait_at(rest, pos)
        pos += width
    if pos >= len(rest):
        return argv                      # no agent: let argparse say so
    agent = rest[pos]
    pos += 1
    while _wait_at(rest, pos):
        wait, width = _wait_at(rest, pos)
        pos += width
    text = rest[pos:]
    for start in (len(rest) - 2, len(rest) - 1):
        hit = _wait_at(rest, start) if start >= pos else None
        if hit and start + hit[1] == len(rest):
            wait = hit[0]
            text = rest[pos:start]
            break
    out = ["ask", agent]
    if wait is not None:
        out += ["--wait", str(wait)]
    if text:
        out += ["--"] + text
    return out


def _normalize(argv):
    """Put '--' in front of the text words of post, reply and ask, so a word
    starting with '-' is never read as an option."""
    if not argv:
        return argv
    op = argv[0]
    if op == "ask":
        return _normalize_ask(argv)
    if op == "post":
        start = 2
    elif op == "reply":
        start = 3
    else:
        return argv
    if start >= len(argv):
        return argv
    return argv[:start] + ["--"] + argv[start:]


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
    p.add_argument("--ids", action="store_true", help="show each message's id")

    p = sub.add_parser("react", help="react to a message with an emoji")
    p.add_argument("channel")
    p.add_argument("message_id")
    p.add_argument("emoji")

    sub.add_parser("brief", help="read the team brief: who is on the team, house rules")

    return ap


def _fail(message):
    print(message, file=sys.stderr)
    return 1


def _format_ask(resp):
    if resp.get("status") == "answered":
        return resp.get("reply") or ""
    return "Still working. Ticket %s. Run: team check %s" % (resp.get("ticket"),
                                                             resp.get("ticket"))


def _format_check(rows, omitted=0):
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
    if omitted:
        lines.append("%d older answered not shown. Run: team check TICKET" % omitted)
    return "\n".join(lines)


def _format_read(messages, ids=False):
    if not messages:
        return "No messages."
    return "\n".join("%s%s: %s" % ("[%s] " % m.get("id") if ids else "", m.get("author"),
                                  (m.get("text") or "").replace("\n", " "))
                     for m in messages)


def _format_ok(op, resp, ids=False):
    if op == "ask":
        return _format_ask(resp)
    if op == "check":
        return _format_check(resp.get("tickets") or [], resp.get("omitted") or 0)
    if op == "read":
        return _format_read(resp.get("messages") or [], ids)
    if op == "brief":
        return (resp.get("text") or "The team brief is empty.").rstrip("\n")
    if op == "react":
        return "Reacted."
    return "Sent."


def main(argv, connect=None):
    """Run one team command. Returns the exit status."""
    if connect is None:
        connect = unix_connect
    args = _parser().parse_args(_normalize(argv))
    request, timeout = _build(args)
    try:
        resp = connect(request, timeout)
    except (FileNotFoundError, ConnectionRefusedError, PermissionError):
        return _fail(NOT_RUNNING)
    except (ConnectionResetError, BrokenPipeError):
        return _fail(BUSY)
    except (socket.timeout, TimeoutError):
        return _fail(NO_ANSWER_ASK if args.op == "ask" else NO_ANSWER)
    except OSError:
        # Anything else from the socket (an over-long socket path, say) means
        # the helper can't be reached.
        return _fail(NOT_RUNNING)
    except ValueError:
        return _fail("team: the team helper sent a reply I couldn't read")

    if not isinstance(resp, dict):
        return _fail("team: the team helper sent a reply I couldn't read")
    if not resp.get("ok"):
        error = resp.get("error") or "unknown error"
        if error == "busy":
            return _fail(BUSY)
        return _fail("team: %s" % error)

    print(_format_ok(args.op, resp, getattr(args, "ids", False)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
