"""Safe prompts between agents (say) and waiters that report when an agent finishes (wake)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from . import agent, config, herdr
from .tasks import find_task
from .util import SwarmError, file_lock, log_stamp


def say(name: str, message: str, urgent: bool = False, wait_minutes: float = 30) -> bool:
    """Prompt an agent once it is idle and the user is not using its pane. Messages to one agent queue up."""
    deadline = time.time() + wait_minutes * 60
    with file_lock(config.data_dir() / "logs" / f".say-{name}.lock"):
        last_screen: Optional[str] = None
        still_since, calm = time.time(), 0
        while time.time() < deadline:
            found = herdr.agent_named(name)
            if found is None:
                raise SwarmError(f"no live agent named {name}")
            if found.get("focused") and not urgent:
                now = herdr.screen(name)
                if now != last_screen:
                    last_screen, still_since = now, time.time()
                if time.time() - still_since < 300:
                    calm = 0
                    time.sleep(5)
                    continue
            calm = calm + 1 if agent.is_idle(found.get("agent_status")) else 0
            if calm >= 3:
                herdr.prompt(name, message)
                time.sleep(2)  # keep two queued messages from merging into one prompt
                return True
            time.sleep(5)
    return False


def cmd_say(args: argparse.Namespace) -> int:
    if say(args.agent, args.message, args.urgent, args.wait_minutes):
        return 0
    print(f"{args.agent} stayed busy for {args.wait_minutes:g} min; not sent. Put the note in the task file.",
          file=sys.stderr)
    return 1


def cmd_wake(args: argparse.Namespace) -> int:
    """Wait until the watched agent finishes its turn (or writes result.md), then tell another agent."""
    result = find_task(args.task).result if args.task else None
    time.sleep(float(os.environ.get("WAKE_DELAY", "15")))
    seen_working, settled, blocked_told = False, 0, False
    deadline = time.time() + args.hours * 3600
    why = "timed out"
    while time.time() < deadline:
        if result is not None and result.exists():
            why = f"wrote {result}"
            break
        found = herdr.agent_named(args.watched)
        if found is None:
            why = "is gone (exited or lost its name); read its pane"
            break
        status = found.get("agent_status")
        if status == "working":
            seen_working, settled = True, 0
        elif status == "blocked":
            settled = 0
            if not blocked_told:
                blocked_told = True
                say(args.to, f"swarm: {args.watched} is blocked on an approval or question dialog ({args.note}). "
                             f"Check with: herdr agent read {args.watched} --source visible")
        elif agent.is_idle(status):
            settled += 1
            if settled >= 3 and (seen_working or settled >= 20):
                why = "finished its turn without result.md" if result else "finished its turn"
                break
        time.sleep(15)
    message = f"swarm: {args.watched} {why} ({args.note})."
    if args.task:
        message += f" Task {args.task}: check result.md, then set a verdict."
    print(f"{log_stamp()} {message}")
    say(args.to, message, wait_minutes=120)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("say", help="prompt an agent once it is idle and you are not typing in it")
    p.add_argument("agent")
    p.add_argument("message")
    p.add_argument("--urgent", action="store_true", help="do not wait while the user has the pane focused")
    p.add_argument("--wait-minutes", type=float, default=30)
    p.set_defaults(run=cmd_say)

    p = sub.add_parser("wake", help="wait until <watched> finishes, then tell --to <agent>")
    p.add_argument("watched")
    p.add_argument("--to", required=True)
    p.add_argument("--task")
    p.add_argument("--note", default="")
    p.add_argument("--hours", type=float, default=24)
    p.set_defaults(run=cmd_wake)
