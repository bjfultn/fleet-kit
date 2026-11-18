"""Startup seeding in bin/mention-watcher.py.

The watcher imports with side effects (fleet.json, agent configs, certifi), so
each test builds a throwaway fleet, imports a fresh copy of the module against
it, and replaces discord_get with a fake Discord that honours limit= and after=.
Nothing here talks to the network or tmux.

    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

KIT = Path(__file__).resolve().parent.parent
WATCHER = KIT / "bin" / "mention-watcher.py"

ADA_BOT = "100000000000000001"
ADA_CHANNEL = "200000000000000001"
GENERAL = "400000000000000001"
DISCORD_EPOCH_MS = 1420070400000


def snowflake(epoch_seconds: float) -> str:
    return str((int(epoch_seconds * 1000) - DISCORD_EPOCH_MS) << 22)


def mention(msg_id: str, content: str = "hi") -> dict:
    return {
        "id": msg_id,
        "content": content,
        "author": {"id": "300000000000000001", "username": "owner"},
        "mentions": [{"id": ADA_BOT}],
        "timestamp": "",
    }


class FakeDiscord:
    """Channel histories keyed by id. While `up` is False every call fails the
    way discord_get fails (returns None): network down, 429, 5xx."""

    def __init__(self):
        self.up = True
        self.history: dict[str, list[dict]] = {}

    def post(self, channel: str, msg: dict) -> None:
        self.history.setdefault(channel, []).append(msg)

    def get(self, path: str):
        if not self.up:
            return None
        url = urlparse(path)
        channel = url.path.split("/")[2]
        q = parse_qs(url.query)
        limit = int(q.get("limit", ["50"])[0])
        msgs = sorted(self.history.get(channel, []), key=lambda m: int(m["id"]))
        if "after" in q:
            after = int(q["after"][0])
            page = [m for m in msgs if int(m["id"]) > after][:limit]
        else:
            page = msgs[-limit:]
        return list(reversed(page))  # Discord answers newest-first


class SeedTests(unittest.TestCase):
    def setUp(self):
        try:
            import certifi  # noqa: F401  the watcher refuses to import without it
        except ImportError:
            self.skipTest("certifi not installed")
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "fleet.json").write_text(json.dumps({"general_channel_id": GENERAL}))
        ada = root / "agents" / "ada"
        ada.mkdir(parents=True)
        (ada / "config.json").write_text(json.dumps({
            "name": "Ada", "alias": "ada",
            "discord": {"bot_id": ADA_BOT, "private_channel_id": ADA_CHANNEL},
        }))

        env = {"FLEET_ROOT": str(root), "DISCORD_BOT_TOKEN": "test-not-a-token"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "path", [str(KIT / "bin"), *sys.path]):
            spec = importlib.util.spec_from_file_location("mention_watcher_under_test", WATCHER)
            mw = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mw)
        self.mw = mw

        self.discord = FakeDiscord()
        self.woken: list[str] = []
        self.logged: list[str] = []
        for name, value in {
            "discord_get": self.discord.get,
            "wake_agent": lambda window, author, channel, content, ts="", msg_id="":
                self.woken.append(msg_id),
            "should_wake_agent": lambda window: True,
            "is_allowed": lambda *a: (True, ""),
            "log": self.logged.append,
        }.items():
            patcher = mock.patch.object(mw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(mw.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(mw.GROK, "handle", lambda msg, channel: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_old_mentions_are_not_replayed_when_the_seed_fails(self):
        self.discord.post(ADA_CHANNEL, mention(snowflake(time.time() - 3600), "old"))
        self.discord.up = False
        self.mw.seed_last_seen()
        self.discord.up = True
        self.mw.poll()
        self.mw.poll()
        self.assertEqual(self.woken, [])

    def test_new_mentions_still_arrive_after_a_failed_seed(self):
        self.discord.post(ADA_CHANNEL, mention(snowflake(time.time() - 3600), "old"))
        self.discord.up = False
        self.mw.seed_last_seen()
        self.discord.up = True
        new_id = snowflake(time.time() + 2)
        self.discord.post(ADA_CHANNEL, mention(new_id, "new"))
        self.mw.poll()
        self.assertEqual(self.woken, [new_id])

    def test_failed_seed_is_logged(self):
        self.discord.up = False
        self.mw.seed_last_seen()
        self.assertTrue(any(ADA_CHANNEL in line and "could not" in line
                            for line in self.logged), self.logged)

    def test_successful_seed_skips_history_and_delivers_new(self):
        self.discord.post(ADA_CHANNEL, mention(snowflake(time.time() - 3600), "old"))
        self.mw.seed_last_seen()
        new_id = snowflake(time.time() + 2)
        self.discord.post(ADA_CHANNEL, mention(new_id, "new"))
        self.mw.poll()
        self.assertEqual(self.woken, [new_id])

    def test_empty_channel_delivers_its_first_message(self):
        self.mw.seed_last_seen()
        first = snowflake(time.time() + 2)
        self.discord.post(ADA_CHANNEL, mention(first, "first"))
        self.mw.poll()
        self.assertEqual(self.woken, [first])

    def test_every_watched_channel_has_a_position_after_seeding(self):
        self.discord.up = False
        self.mw.seed_last_seen()
        self.assertEqual(set(self.mw.last_seen), set(self.mw.CHANNELS))


if __name__ == "__main__":
    unittest.main()
