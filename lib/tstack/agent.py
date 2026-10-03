"""The coding agent CLI behind managers and workers, read from the `agent` settings section.

Default: Claude Code. To plug in another CLI that herdr supports, set agent.cmd, agent.herdr_kind,
the flags, the exit command, and how its screen reports context use.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional

from . import config


def settings(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return config.section("agent", values if values is not None else config.load())


def model_for(role: str, values: Optional[Dict[str, Any]] = None) -> str:
    """Model for a "manager" or a "worker"."""
    return str(settings(values).get(f"{role}_model") or "")


def start_args(name: str, model: str, prompt: str, values: Optional[Dict[str, Any]] = None) -> List[str]:
    """Arguments after `--` in `herdr agent start ... -- <args>`."""
    agent = settings(values)
    args: List[str] = []
    if agent.get("model_flag") and model:
        args += [str(agent["model_flag"]), model]
    if agent.get("name_flag"):
        args += [str(agent["name_flag"]), name]
    args += [str(item) for item in agent.get("extra_args") or []]
    return args + [prompt]


def kind(values: Optional[Dict[str, Any]] = None) -> str:
    agent = settings(values)
    return str(agent.get("herdr_kind") or agent.get("cmd") or "claude")


def exit_command(values: Optional[Dict[str, Any]] = None) -> str:
    return str(settings(values).get("exit_command") or "/exit")


def start_timeout_ms(values: Optional[Dict[str, Any]] = None) -> int:
    return int(settings(values).get("start_timeout_ms") or 60000)


def is_idle(status: Any, values: Optional[Dict[str, Any]] = None) -> bool:
    return status in (settings(values).get("idle_states") or ["idle", "done"])


def context_used(screen: str, values: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Percent of the context window in use, read from a screen; None when it does not say."""
    agent = settings(values)
    pattern = agent.get("context_regex")
    if not pattern:
        return None
    found = None
    for found in re.finditer(str(pattern), screen):
        pass
    if found is None:
        return None
    number = float(found.group(1))
    return 100 - number if agent.get("context_reports") == "left" else number


def by_hand(name: str, pane: str, model: str, prompt: str, values: Optional[Dict[str, Any]] = None) -> str:
    """The herdr command a user can paste when a start fails."""
    words = ["herdr", "agent", "start", name, "--kind", kind(values), "--pane", pane, "--",
             *start_args(name, model, prompt, values)]
    return " ".join(shlex.quote(word) for word in words)
