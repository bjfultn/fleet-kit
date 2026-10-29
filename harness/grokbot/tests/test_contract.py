"""Contract between the broker's ticket file and the watcher's reader.

Tickets are written by the real Broker save path and read back by the real
TicketReader; the suppression decision is made by grokbot_targets. No
hand-written ticket JSON, so a change to either side's schema breaks here.
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "bin"))

import broker as bk  # noqa: E402
import grokbot_wake as gw  # noqa: E402
from test_broker import (CURTIS, CURTIS_CH, FakeDiscord, GENERAL, HUMAN,  # noqa: E402
                         SONNET, make_config)

AGENT_BOT = "444444444444444444"      # the hosted agent's own bot id
AGENT_PRIVATE = "555555555555555555"  # its private channel


class ContractTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = td.name
        self.now = float(int(time.time()))
        self.discord = FakeDiscord(clock=lambda: self.now)
        self.broker = bk.Broker(make_config(self.tmp), self.discord, clock=lambda: self.now)
        self.broker.sleep = lambda s: None
        self.reader = gw.TicketReader(os.path.join(self.tmp, "state", "tickets.json"),
                                      lambda s: None)
        self.agent = {"alias": "nova", "bot_id": AGENT_BOT,
                      "private_channel_id": AGENT_PRIVATE,
                      "allowed_channels": {AGENT_PRIVATE, CURTIS_CH, GENERAL},
                      "allow_from": {HUMAN}, "allow_bots": {CURTIS, SONNET}}

    def wakes(self, stored, bot=True):
        """Would the watcher wake the agent for this stored Discord message?"""
        author = stored["author"]["id"]
        msg = {"id": stored["id"], "content": stored["content"],
               "author": {"id": author, "bot": bot},
               "mentions": [{"id": AGENT_BOT}],
               "message_reference": stored.get("message_reference")}
        return gw.grokbot_targets(msg, CURTIS_CH, self.agent, self.reader.asks())

    def ask(self):
        r = self.broker.handle({"op": "ask", "agent": "curtis", "text": "do it", "wait": 0})
        return r["ticket"], self.discord.posts[-1]["id"]

    def test_working_ticket_holds_back_the_expected_author_only(self):
        _, ask_id = self.ask()
        theirs = self.discord.add(CURTIS_CH, CURTIS, "on it", reply_to=ask_id)
        human = self.discord.add(CURTIS_CH, HUMAN, "any news?", reply_to=ask_id)
        other = self.discord.add(CURTIS_CH, SONNET, "me too", reply_to=ask_id)
        self.assertFalse(self.wakes(theirs))
        self.assertTrue(self.wakes(human, bot=False))
        self.assertTrue(self.wakes(other))

    def test_captured_reply_is_suppressed_and_a_later_one_wakes(self):
        ticket, ask_id = self.ask()
        first = self.discord.add(CURTIS_CH, CURTIS, "on it", reply_to=ask_id)
        self.broker.handle({"op": "check", "ticket": ticket})
        self.assertEqual(self.broker.handle({"op": "check", "ticket": ticket})
                         ["tickets"][0]["status"], "answered")
        result = self.discord.add(CURTIS_CH, CURTIS, "done, here is the result",
                                  reply_to=ask_id)
        self.assertFalse(self.wakes(first))
        self.assertTrue(self.wakes(result))

    def test_continuation_chunks_are_suppressed(self):
        ticket, ask_id = self.ask()
        one = self.discord.add(CURTIS_CH, CURTIS, "part one", reply_to=ask_id)
        two = self.discord.add(CURTIS_CH, CURTIS, "part two")
        self.broker.handle({"op": "check", "ticket": ticket})
        self.assertFalse(self.wakes(one))
        self.assertFalse(self.wakes(two))


if __name__ == "__main__":
    unittest.main()
