#!/usr/bin/env python3
"""Provision a fleet: directories, configs, personas, access lists, boot job.

Run through setup.sh, which picks a 3.10+ interpreter first.

Three modes:
    --spec FILE   answers from JSON instead of prompting (this is how it is tested)
    --dry-run     render everything, write nothing
    (neither)     prompt for each answer

Bot tokens are never accepted from a spec file and never echoed. They are read
from the terminal and written straight to each agent's .env at mode 0600.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATES = ROOT / "templates"

SNOWFLAKE = re.compile(r"^\d{17,20}$")
ALIAS_OK = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


# ── output ──────────────────────────────────────────────────────────────────

def say(msg: str = "") -> None:
    print(msg)


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"setup: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ── preflight ───────────────────────────────────────────────────────────────

def preflight() -> list[str]:
    """Check what the fleet needs to actually run. Returns a list of problems.

    Reported all at once rather than failing on the first: someone standing this
    up for the first time should get one list to work through, not five rounds
    of run-fix-rerun.
    """
    problems: list[str] = []

    if not shutil.which("tmux"):
        problems.append("tmux is not installed. The fleet runs agents in tmux windows.")

    if not shutil.which("claude"):
        problems.append(
            "the `claude` CLI is not on PATH. Install Claude Code and make sure a "
            "login is already set up; this script does not authenticate for you."
        )

    if sys.version_info < (3, 10):
        problems.append(f"Python 3.10+ required, running {sys.version.split()[0]}.")

    try:
        import certifi  # noqa: F401
    except ImportError:
        problems.append(
            f"certifi is not installed for {sys.executable}. The mention watcher "
            "refuses to run without it, because macOS Python ships an empty CA "
            "store and the alternative is sending a bot token over an "
            "unverified connection. Fix: "
            f"{sys.executable} -m pip install certifi"
        )

    return problems


# ── prompting ───────────────────────────────────────────────────────────────

def prompt_line(prompt: str) -> str:
    """input(), but with something useful to say when there is nobody there.

    Run without a terminal — from a script, or by an agent helping someone set
    this up — every prompt raises EOFError and the whole thing exits on a raw
    Python traceback that says nothing about what to do instead.
    """
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        die("nothing on stdin: this asks questions and needs a terminal.\n"
            "       To provision without one, put the answers in a JSON file "
            "and pass --spec.\n"
            "       See spec.example.json. Bot tokens are never read from a "
            "spec; write\n"
            "       each one to its agent's state directory as .env afterwards.")


class Answers:
    """Prompts, or canned answers from a spec file. Same interface either way."""

    def __init__(self, spec: dict | None):
        self.spec = spec
        self.interactive = spec is None

    def ask(self, key: str, prompt: str, default: str = "", validate=None) -> str:
        if not self.interactive:
            val = str(self.spec.get(key, default))  # type: ignore[union-attr]
            if validate:
                err = validate(val)
                if err:
                    die(f"spec key {key!r}: {err}")
            return val

        while True:
            suffix = f" [{default}]" if default else ""
            val = prompt_line(f"  {prompt}{suffix}: ") or default
            if validate:
                err = validate(val)
                if err:
                    warn(err)
                    continue
            return val

    def ask_agents(self) -> list[dict]:
        if not self.interactive:
            return list(self.spec.get("agents", []))  # type: ignore[union-attr]

        agents: list[dict] = []
        say()
        say("Now the agents. One Discord bot application per agent; see")
        say("docs/DISCORD-SETUP.md if you have not created them yet.")
        while True:
            say()
            n = len(agents) + 1
            name = prompt_line(f"  Agent {n} display name (blank to finish): ")
            if not name:
                break
            a = {"name": name}
            a["alias"] = self.ask(
                "", "  alias (lowercase, used as the tmux window name)",
                name.lower(), v_alias)
            a["role"] = self.ask("", "  role", "")
            a["personality"] = self.ask("", "  personality (one or two sentences)", "")
            a["responsibilities"] = self.ask("", "  responsibilities (comma separated)", "")
            a["lane"] = self.ask("", "  lane (what this agent owns)", "")
            a["claim_emoji"] = self.ask("", "  claim emoji", ":robot:")
            a["bot_id"] = self.ask("", "  Discord Application ID", "", v_snowflake)
            a["private_channel_id"] = self.ask(
                "", "  private channel id", "", v_snowflake)
            a["private_channel_name"] = self.ask(
                "", "  private channel name (without #)", a["alias"])
            a["model"] = self.ask("", "  model", "claude-sonnet-5")
            a["effort"] = self.ask("", "  effort", "medium")
            agents.append(a)
        return agents


def v_snowflake(v: str) -> str | None:
    return None if SNOWFLAKE.match(v) else "must be a Discord id (17-20 digits)"


def v_alias(v: str) -> str | None:
    return None if ALIAS_OK.match(v) else "lowercase letters, digits, - and _; must start with a letter"


def v_nonempty(v: str) -> str | None:
    return None if v.strip() else "cannot be empty"


# ── rendering ───────────────────────────────────────────────────────────────

def agent_state_dir(agent: dict) -> Path:
    """Where the Discord plugin keeps this agent's session state and .env.

    One function so the path in config.json and the path the token is written to
    cannot diverge. They did once: a spec-file override moved the directory but
    not the config, which put the token somewhere the agent never looks and
    produced an agent that starts fine and never connects.
    """
    return Path(os.path.expanduser(
        agent.get("state_dir") or f"~/.claude/channels/discord-{agent['alias']}"))


FLEET_KEYS = {"tmux_session", "timezone", "general_channel_id",
              "owner_user_id", "log_dir", "agents"}
AGENT_KEYS = {"name", "alias", "role", "personality", "responsibilities",
              "lane", "claim_emoji", "bot_id", "private_channel_id",
              "private_channel_name", "model", "effort", "state_dir"}


def validate_spec_keys(spec: dict) -> None:
    """Reject spec keys nothing reads.

    Unknown keys used to be ignored in silence, so a spec asking for something
    the tool does not support provisioned a fleet that did not match it and
    exited 0. A "fleet_root" that nothing honours put a whole test fleet inside
    the kit checkout while the run reported success. The root comes from --root;
    everything else is here. Guessing at a key name should be an error, not a
    surprise later.
    """
    problems = []
    # Keys starting with _ are comments. JSON has none, and fleet.example.json
    # already uses the convention, so a spec written in the same style should
    # not be rejected for it.
    def unknown(keys, known):
        return sorted(k for k in set(keys) - known if not k.startswith("_"))

    for k in unknown(spec, FLEET_KEYS):
        problems.append(f"unknown key {k!r} (the fleet root is set with --root)"
                        if k in ("fleet_root", "root")
                        else f"unknown key {k!r}")
    for a in spec.get("agents", []):
        if not isinstance(a, dict):
            problems.append(f"agents entries must be objects, got {type(a).__name__}")
            continue
        who = a.get("name") or a.get("alias") or "?"
        for k in unknown(a, AGENT_KEYS):
            problems.append(f"{who}: unknown agent key {k!r}")
    if problems:
        for m in problems:
            warn(m)
        die("spec file has keys this tool does not read; fix or remove them")


def validate_agents(agents: list[dict]) -> None:
    """Check every agent record, whichever mode produced it.

    Interactive answers are validated at the prompt; spec files were not, so a
    hand-written JSON with a blank bot_id used to provision cleanly and exit 0.
    That agent then runs, replies to humans, and can never be woken by another
    agent, with nothing logging why. The mode that is easiest to get wrong is
    the one that most needs the check, so it lives here, after collection, and
    covers both.

    Every problem is reported, not just the first, and nothing is written until
    they are all clear.
    """
    if not agents:
        die("no agents defined; nothing to do.")

    problems: list[str] = []
    for i, a in enumerate(agents, 1):
        who = a.get("name") or a.get("alias") or f"agent {i}"
        for field, check in (("alias", v_alias),
                             ("bot_id", v_snowflake),
                             ("private_channel_id", v_snowflake)):
            err = check(str(a.get(field, "")))
            if err:
                problems.append(f"{who}: {field} {err}")
        if not str(a.get("name", "")).strip():
            problems.append(f"agent {i}: name cannot be empty")

    for field, label in (("alias", "Aliases are tmux window names"),
                         ("bot_id", "each agent needs its own bot"),
                         ("private_channel_id", "each agent needs its own channel")):
        vals = [str(a.get(field, "")) for a in agents]
        dupes = {v for v in vals if v and vals.count(v) > 1}
        if dupes:
            problems.append(f"duplicate {field}: {', '.join(sorted(dupes))}. {label}.")

    if problems:
        for pr in problems:
            print(f"setup: {pr}", file=sys.stderr)
        raise SystemExit(1)


def render(template: Path, values: dict[str, str]) -> str:
    """Fill {{PLACEHOLDER}} slots.

    Any slot left unfilled is an error rather than a literal {{FOO}} shipped
    into a live config, which is the kind of thing that reads fine in a diff and
    then fails at 3am.
    """
    text = template.read_text()
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", str(val))
    leftover = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
    if leftover:
        die(f"{template.name}: no value for {', '.join(sorted(leftover))}")
    return text


def build_access(agent: dict, all_agents: list[dict], owner_id: str,
                 general_id: str) -> str:
    """Who may wake this agent.

    Its own channel and general do not require a mention; every other agent's
    channel does, so an agent can read the room but only speaks when tagged.
    """
    peers = [a for a in all_agents if a["alias"] != agent["alias"]]
    groups: dict[str, dict] = {
        agent["private_channel_id"]: {"requireMention": False, "allowFrom": []},
    }
    if general_id:
        groups[general_id] = {"requireMention": False, "allowFrom": []}
    for p in peers:
        groups.setdefault(
            p["private_channel_id"], {"requireMention": True, "allowFrom": []})

    return render(TEMPLATES / "agent" / "access.json.tmpl", {
        "OWNER_USER_ID": owner_id,
        "PEER_BOT_IDS": ", ".join(json.dumps(p["bot_id"]) for p in peers),
        "GROUPS": json.dumps(groups, indent=2).replace("\n", "\n  ").rstrip(),
    })


def build_roster(agent: dict, all_agents: list[dict]) -> str:
    peers = [a for a in all_agents if a["alias"] != agent["alias"]]
    if not peers:
        return "You are the only agent in this fleet."
    return "\n".join(
        f"- **{p['name']}** — {p.get('role') or 'no role set'}. "
        f"Tag: `<@{p['bot_id']}>`"
        for p in peers
    )


def build_agent_table(all_agents: list[dict]) -> str:
    rows = "\n".join(
        f"- **{a['name']}** — {a.get('role') or 'no role set'}. "
        f"Channel: #{a.get('private_channel_name') or a['alias']}"
        for a in all_agents
    )
    return rows


def build_emoji_table(all_agents: list[dict]) -> str:
    return " · ".join(
        f"{a['name']} = {a.get('claim_emoji') or ':robot:'}" for a in all_agents
    )


# ── writing ─────────────────────────────────────────────────────────────────

class Writer:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.written: list[str] = []

    def write(self, path: Path, content: str, mode: int = 0o644) -> None:
        rel = path
        if self.dry_run:
            say(f"--- {rel} (mode {mode:o}) ---")
            say(content if len(content) < 1500 else content[:1500] + "\n  ...")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        self.written.append(str(rel))

    def mkdir(self, path: Path, mode: int = 0o755) -> None:
        if self.dry_run:
            return
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)


def main() -> int:
    ap = argparse.ArgumentParser(prog="setup.sh", add_help=True)
    ap.add_argument("--spec", type=Path, help="JSON answers instead of prompting")
    ap.add_argument("--dry-run", action="store_true", help="render, write nothing")
    ap.add_argument("--root", type=Path, default=ROOT,
                    help="fleet root to write into (default: this checkout)")
    ap.add_argument("--skip-preflight", action="store_true")
    args = ap.parse_args()

    say("fleet-kit setup")
    say("===============")
    say()

    if not args.skip_preflight:
        problems = preflight()
        if problems:
            say("Preflight found problems:")
            for p in problems:
                say(f"  - {p}")
            say()
            if args.spec or args.dry_run:
                say("(continuing anyway: non-interactive run)")
            else:
                if input("  Continue anyway? [y/N]: ").strip().lower() != "y":
                    return 1
        else:
            say("Preflight OK: tmux, claude, python, certifi all present.")
        say()

    spec = json.loads(args.spec.read_text()) if args.spec else None
    if spec is not None:
        validate_spec_keys(spec)
    ans = Answers(spec)

    root: Path = args.root.resolve()
    writer = Writer(args.dry_run)

    say("Fleet settings")
    tmux_session = ans.ask("tmux_session", "tmux session name", "agents")
    timezone = ans.ask("timezone", "timezone (blank to inherit the host's)", "")
    general_id = ans.ask("general_channel_id", "#general channel id", "", v_snowflake)
    owner_id = ans.ask("owner_user_id", "your Discord user id", "", v_snowflake)
    log_dir = ans.ask("log_dir", "log directory", str(root / "logs"))

    agents = ans.ask_agents()
    validate_agents(agents)

    # ── fleet.json ──────────────────────────────────────────────────────────
    fleet = {
        "tmux_session": tmux_session,
        "general_channel_id": general_id,
        "log_dir": log_dir,
        "python": sys.executable,
        "defaults": {"auto_compact_window": 200000},
    }
    if timezone:
        fleet["timezone"] = timezone
    writer.write(root / "fleet.json", json.dumps(fleet, indent=2) + "\n")

    shared_prompt = root / "shared" / "context.md"
    writer.write(shared_prompt, render(TEMPLATES / "context.md.tmpl", {
        "TIMEZONE": timezone or "the host's local time",
        "AGENT_TABLE": build_agent_table(agents),
        "CLAIM_EMOJI_TABLE": build_emoji_table(agents),
    }))

    # ── per agent ───────────────────────────────────────────────────────────
    for a in agents:
        adir = root / "agents" / a["alias"]
        state_dir = agent_state_dir(a)

        common = {
            "NAME": a["name"],
            "ROLE": a.get("role", ""),
            "PERSONALITY": a.get("personality", ""),
            "ALIAS": a["alias"],
            "BOT_ID": a["bot_id"],
            "PRIVATE_CHANNEL_ID": a["private_channel_id"],
            "PRIVATE_CHANNEL_NAME": a.get("private_channel_name") or a["alias"],
            "GENERAL_CHANNEL_ID": general_id,
            "STATE_DIR": str(state_dir),
        }

        writer.write(adir / "config.json", render(
            TEMPLATES / "agent" / "config.json.tmpl",
            {**common,
             "MODEL": a.get("model", "claude-sonnet-5"),
             "EFFORT": a.get("effort", "medium"),
             "SHARED_PROMPT": str(shared_prompt)}))

        resp = a.get("responsibilities", "")
        writer.write(adir / "CLAUDE.md", render(
            TEMPLATES / "agent" / "CLAUDE.md.tmpl",
            {**common,
             "AGENT_DIR": str(adir),
             "RESPONSIBILITIES": "\n".join(
                 f"- {r.strip()}" for r in resp.split(",") if r.strip()
             ) or "- (not set yet)",
             "TEAM_ROSTER": build_roster(a, agents),
             "CLAIM_EMOJI": a.get("claim_emoji", ":robot:"),
             "LANE": a.get("lane") or a.get("role") or "not set",
             "MEMORY_DIR": str(adir / "memory")}))

        writer.mkdir(adir / "memory")
        writer.write(adir / "memory" / "MEMORY.md",
                     f"# {a['name']}'s memory index\n\nOne line per memory:\n"
                     "`- [Title](file.md) — short hook`\n")

        # State dir is 0700 and the token file 0600: it holds a credential that
        # can post as this agent anywhere the bot is invited.
        writer.mkdir(state_dir, 0o700)

        # access.json belongs NEXT TO the token, in the state dir the plugin is
        # pointed at by DISCORD_STATE_DIR. It used to be written into the agent
        # directory, where it looks right to a human reading the repo and is
        # read by nothing: the plugin falls back to an empty allowlist, and the
        # agent starts, gets woken by a mention, and then refuses to reply with
        # "channel is not allowlisted". Verified on a cold start 2026-08-28.
        writer.write(state_dir / "access.json",
                     build_access(a, agents, owner_id, general_id))

    # ── tokens ──────────────────────────────────────────────────────────────
    # Deliberately never read from a spec file. A token in a JSON file on disk
    # is a token in a backup, in a git stash, and eventually in a paste.
    if not args.dry_run and ans.interactive:
        say()
        say("Bot tokens. Paste each one; it will not echo.")
        say("Leave blank to skip and write the .env yourself later.")
        for a in agents:
            env_path = agent_state_dir(a) / ".env"
            if env_path.exists():
                say(f"  {a['name']}: .env already exists, leaving it alone.")
                continue
            tok = getpass.getpass(f"  {a['name']} bot token: ").strip()
            if not tok:
                warn(f"{a['name']}: no token written. That agent cannot connect yet.")
                continue
            # Create at 0600 rather than creating then chmod'ing. Between
            # those two calls the file sits at whatever the umask allows,
            # which on a shared box is long enough for anyone to read a live
            # bot token. Open with the mode you want.
            fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"DISCORD_BOT_TOKEN={tok}\n")
            say(f"  {a['name']}: token written to {env_path}")

    # ── boot job ────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        label = ans.ask("launchd_label", "launchd label",
                        f"com.fleet.{tmux_session}")
        plist = render(TEMPLATES / "launchd" / "fleet.plist.tmpl", {
            "LABEL": label,
            "FLEET_ROOT": str(root),
            "LOG_DIR": os.path.expanduser(log_dir),
        })
        writer.write(root / f"{label}.plist", plist)

    say()
    if args.dry_run:
        say("Dry run: nothing written.")
        return 0

    say(f"Wrote {len(writer.written)} files under {root}")
    say()
    say("Next:")
    n = 0

    def step(text: str, *rest: str) -> None:
        nonlocal n
        n += 1
        say(f"  {n}. {text}")
        for line in rest:
            say(f"     {line}")

    # Tokens first when they are missing, which is every --spec run: the token
    # step only happens in the interactive path, so a non-interactive provision
    # finishes "successfully" with no credentials anywhere. Listing the other
    # steps without this one hands whoever is following along a checklist that
    # cannot work, and the failure it produces looks like a broken fleet rather
    # than a missing step.
    missing = [a for a in agents if not (agent_state_dir(a) / ".env").exists()]
    if missing:
        step("Write each bot token. One line, DISCORD_BOT_TOKEN=..., mode 0600:")
        for a in missing:
            say(f"       {agent_state_dir(a) / '.env'}   ({a['name']})")
        say("     Create the file at 0600 rather than chmod'ing afterwards, and")
        say("     do not put tokens in the spec file or in shell history.")

    step("bin/apply-plugin-patch.sh   (the Discord plugin patch. Without it",
         "agents cannot wake each other and nothing logs an error.)")
    if sys.platform == "darwin":
        step(f"cp {label}.plist ~/Library/LaunchAgents/ && "
             f"launchctl load ~/Library/LaunchAgents/{label}.plist")
    else:
        step("Write a boot job for bin/fleet-start.sh (see docs/OPERATIONS.md).")
    step("bin/fleet-start.sh")
    say()
    say("Then tag one agent from another in Discord. If nothing wakes, run")
    say("bin/apply-plugin-patch.sh --check first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
