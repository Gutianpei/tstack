"""Every call to herdr (the terminal multiplexer that hosts the agents) goes through here."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import agent
from .util import SwarmError

HERDR = os.environ.get("HERDR_BIN") or shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")


def call(*arguments: str, timeout: int = 30) -> Dict[str, Any]:
    """Run `herdr <arguments>` and return its JSON `result`."""
    try:
        done = subprocess.run([HERDR, *arguments], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SwarmError(f"herdr not found at {HERDR}; install herdr or set HERDR_BIN")
    text = done.stdout.strip() or done.stderr.strip()
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise SwarmError(f"herdr {' '.join(arguments[:2])}: {text or 'exit ' + str(done.returncode)}")
    if done.returncode or payload.get("error"):
        error = payload.get("error") or {}
        raise SwarmError(f"herdr {' '.join(arguments[:2])}: {error.get('message') or text}")
    return payload.get("result") or {}


def agents() -> List[Dict[str, Any]]:
    return list(call("agent", "list").get("agents") or [])


def agent_named(name: str) -> Optional[Dict[str, Any]]:
    for item in agents():
        if item.get("name") == name:
            return item
    return None


def workspace_named(label: str) -> Optional[Dict[str, Any]]:
    for item in call("workspace", "list").get("workspaces") or []:
        if item.get("label") == label:
            return item
    return None


def tabs(workspace_id: str) -> List[Dict[str, Any]]:
    return list(call("tab", "list", "--workspace", workspace_id).get("tabs") or [])


def tab_named(workspace_id: str, label: str) -> Optional[Dict[str, Any]]:
    for item in tabs(workspace_id):
        if item.get("label") == label:
            return item
    return None


def tab_panes(workspace_id: str, tab_id: str) -> List[Dict[str, Any]]:
    panes = call("pane", "list", "--workspace", workspace_id).get("panes") or []
    return [pane for pane in panes if pane.get("tab_id") == tab_id]


def env_args() -> List[str]:
    """Pass a non-default settings file or data folder on to new panes."""
    return [arg for key in ("TSTACK_CONFIG", "SWARM_DIR") if os.environ.get(key)
            for arg in ("--env", f"{key}={os.environ[key]}")]


def screen(name: str) -> str:
    """The visible text of an agent's pane."""
    done = subprocess.run([HERDR, "agent", "read", name, "--source", "visible"],
                          capture_output=True, text=True, timeout=30)
    return done.stdout


def free_shell(pane_id: str) -> bool:
    """True when the pane's shell sits at its prompt with nothing in front of it."""
    try:
        info = call("pane", "process-info", "--pane", pane_id).get("process_info") or {}
    except SwarmError:
        return False
    processes, shell = list(info.get("foreground_processes") or []), info.get("shell_pid")
    return bool(processes) and shell is not None and all(item.get("pid") == shell for item in processes)


def wait_free_shell(pane_id: str, seconds: float = 20) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if free_shell(pane_id):
            return True
        time.sleep(1)
    return False


def start_agent(name: str, pane: str, model: str, prompt: str) -> None:
    """Start the configured agent CLI in a free shell pane under a herdr agent name."""
    if not wait_free_shell(pane):
        raise SwarmError(f"pane {pane} has no free shell prompt; cannot start {name}")
    timeout_ms = agent.start_timeout_ms()
    try:
        call("agent", "start", name, "--kind", agent.kind(), "--pane", pane, "--timeout", str(timeout_ms),
             "--", *agent.start_args(name, model, prompt), timeout=timeout_ms // 1000 + 30)
    except SwarmError as error:
        # herdr can time out while the agent is already busy with its first prompt. Name it then.
        time.sleep(3)
        for item in agents():
            if item.get("pane_id") == pane and not item.get("name"):
                call("agent", "rename", pane, name)
                return
        if agent_named(name):
            return
        raise SwarmError(f"{name} did not start in pane {pane}: {error}\n"
                         f"By hand: {agent.by_hand(name, pane, model, prompt)}")


def prompt(name: str, text: str) -> None:
    """Type a prompt into an agent. Use messaging.say, which waits for a safe moment."""
    call("agent", "prompt", name, text)


def run_in_pane(pane_id: str, command: str) -> None:
    call("pane", "run", pane_id, command)


def notify(title: str, body: str, sound: str = "request") -> None:
    with contextlib.suppress(Exception):
        call("notification", "show", title, "--body", body, "--sound", sound)
