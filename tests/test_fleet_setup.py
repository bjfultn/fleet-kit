"""End-to-end checks of bin/fleet-setup.py, run the way CLAUDE.md says to run it:
--spec, --root, no terminal. HOME points into the temp dir so each agent's
state dir (and the access.json written there) never lands in a real home.

    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
SETUP = KIT / "bin" / "fleet-setup.py"

# Answers a person could type. Quotes, a backslash, a tab and a non-ASCII
# character: each one breaks a JSON string when pasted in unescaped.
AWKWARD = {
    "name": 'Ada "the Librarian"',
    "role": "Research \\ Reference",
    "personality": 'Calm, says "no" often.\tPrecise — allergic to guesses.',
}


def spec(**agent_overrides) -> dict:
    agent = {
        "name": "Ada",
        "alias": "ada",
        "bot_id": "100000000000000001",
        "private_channel_id": "200000000000000001",
        "role": "Research Librarian",
        "personality": "Patient.",
    }
    agent.update(agent_overrides)
    peer = {
        "name": "Bo",
        "alias": "bo",
        "bot_id": "100000000000000002",
        "private_channel_id": "200000000000000002",
    }
    return {
        "tmux_session": "agents",
        "owner_user_id": "300000000000000001",
        "general_channel_id": "400000000000000001",
        "agents": [agent, peer],
    }


class SetupRendersValidJson(unittest.TestCase):
    def run_setup(self, answers: dict, *extra: str) -> tuple[subprocess.CompletedProcess, Path]:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tmp = Path(td.name)
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(answers))
        root = tmp / "fleet"
        env = {**os.environ, "HOME": str(tmp / "home")}
        proc = subprocess.run(
            [sys.executable, str(SETUP), "--spec", str(spec_path),
             "--root", str(root), "--skip-preflight", *extra],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
            timeout=60)
        return proc, tmp

    def test_quotes_and_backslashes_round_trip_into_config_json(self):
        proc, tmp = self.run_setup(spec(**AWKWARD))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cfg = json.loads((tmp / "fleet" / "agents" / "ada" / "config.json").read_text())
        for key, value in AWKWARD.items():
            self.assertEqual(cfg[key], value)
        # Non-ASCII stays readable rather than turning into \\u escapes.
        raw = (tmp / "fleet" / "agents" / "ada" / "config.json").read_text()
        self.assertIn("—", raw)

    def test_every_written_json_file_parses(self):
        proc, tmp = self.run_setup(spec(**AWKWARD))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        files = list((tmp / "fleet").rglob("*.json")) + list((tmp / "home").rglob("*.json"))
        names = sorted(p.name for p in files)
        self.assertIn("access.json", names)
        self.assertIn("config.json", names)
        for p in files:
            with self.subTest(path=str(p.relative_to(tmp))):
                json.loads(p.read_text())

    def test_access_json_is_unchanged_by_escaping(self):
        proc, tmp = self.run_setup(spec())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        access = json.loads(
            (tmp / "home" / ".claude" / "channels" / "discord-ada" / "access.json").read_text())
        self.assertEqual(access["allowFrom"], ["300000000000000001"])
        self.assertEqual(access["allowBots"], ["100000000000000002"])
        self.assertIn("200000000000000001", access["groups"])
        self.assertIn("200000000000000002", access["groups"])
        self.assertTrue(access["groups"]["200000000000000002"]["requireMention"])

    def test_dry_run_still_succeeds(self):
        proc, _ = self.run_setup(spec(**AWKWARD), "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Dry run: nothing written.", proc.stdout)


if __name__ == "__main__":
    unittest.main()
