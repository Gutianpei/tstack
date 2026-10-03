"""Run a `swarm` subcommand as a long-lived background process: the routine loop, the night runner.

A backend is anything with start(name, argv, log), running(name), stop(name) and where(name).
Two ship: "tmux" (one detached session per process, attach to watch it) and "nohup" (a detached
process group with a pid file). "auto" picks tmux when it is on PATH, else nohup. To add one, write a
class with those four methods and put it in BACKENDS; settings choose it by name.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import config
from .util import SWARM_BIN, SwarmError

# Passed on to the background process so it reads the same settings, data folder and herdr.
PASS_ENV = ("TSTACK_CONFIG", "SWARM_DIR", "HERDR_BIN", "PATH")


def logs_dir() -> Path:
    path = config.data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def swarm_argv(*arguments: str) -> List[str]:
    return [sys.executable, str(SWARM_BIN), *arguments]


class Nohup:
    """A detached process group; its pid sits in <data_dir>/logs/<name>.pid."""

    label = "nohup"

    def pid_file(self, name: str) -> Path:
        return logs_dir() / f"{name}.pid"

    def pid(self, name: str) -> Optional[int]:
        try:
            pid = int(self.pid_file(name).read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def start(self, name: str, argv: List[str], log: Path) -> None:
        with open(log, "a") as handle:
            process = subprocess.Popen(argv, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
        self.pid_file(name).write_text(f"{process.pid}\n")

    def running(self, name: str) -> bool:
        return self.pid(name) is not None

    def stop(self, name: str) -> bool:
        pid = self.pid(name)
        self.pid_file(name).unlink(missing_ok=True)
        if pid is None:
            return False
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
        return True

    def where(self, name: str) -> str:
        pid = self.pid(name)
        return f"pid {pid} (pid file {self.pid_file(name)})" if pid else "not running"


class Tmux:
    """One detached tmux session per process, named after it: `tmux attach -t <name>` to watch."""

    label = "tmux"

    def _tmux(self, *arguments: str) -> subprocess.CompletedProcess:
        if not shutil.which("tmux"):
            raise SwarmError("tmux is not on PATH; install it or set the backend to nohup")
        return subprocess.run(["tmux", *arguments], capture_output=True, text=True)

    def start(self, name: str, argv: List[str], log: Path) -> None:
        env = [f"{key}={os.environ[key]}" for key in PASS_ENV if os.environ.get(key)]
        command = " ".join(shlex.quote(word) for word in ["env", *env, *argv])
        done = self._tmux("new-session", "-d", "-s", name, f"exec {command} >> {shlex.quote(str(log))} 2>&1")
        if done.returncode:
            raise SwarmError(f"tmux new-session {name}: {done.stderr.strip()}")

    def running(self, name: str) -> bool:
        return bool(shutil.which("tmux")) and self._tmux("has-session", "-t", f"={name}").returncode == 0

    def stop(self, name: str) -> bool:
        return self.running(name) and self._tmux("kill-session", "-t", f"={name}").returncode == 0

    def where(self, name: str) -> str:
        return f"tmux session {name} (tmux attach -t {name})" if self.running(name) else "not running"


BACKENDS: Dict[str, type] = {"tmux": Tmux, "nohup": Nohup}


def backend(kind: str = "auto"):
    kind = kind or "auto"
    if kind == "auto":
        kind = "tmux" if shutil.which("tmux") else "nohup"
    if kind not in BACKENDS:
        raise SwarmError(f"unknown background backend {kind!r}; use one of auto, {', '.join(BACKENDS)}")
    return BACKENDS[kind]()


def find_running(name: str):
    """The backend that runs `name` now, whichever was used to start it, or None."""
    for kind in BACKENDS:
        with contextlib.suppress(SwarmError):
            found = BACKENDS[kind]()
            if found.running(name):
                return found
    return None


def start(name: str, kind: str, *arguments: str) -> str:
    """Start `swarm <arguments>` in the background unless it already runs. Returns a status line."""
    found = find_running(name)
    if found:
        return f"{name} already runs: {found.where(name)}"
    chosen = backend(kind)
    out = logs_dir() / f"{name}.out"
    chosen.start(name, swarm_argv(*arguments), out)
    time.sleep(1)
    if not chosen.running(name):
        tail = out.read_text()[-800:] if out.exists() else ""
        raise SwarmError(f"{name} exited at once; output in {out}:\n{tail}")
    return f"{name} started: {chosen.where(name)}; output in {out}"


def stop(name: str) -> str:
    stopped = [kind for kind, cls in BACKENDS.items() if _quiet_stop(cls(), name)]
    return f"{name} stopped ({', '.join(stopped)})" if stopped else f"{name} was not running"


def _quiet_stop(found, name: str) -> bool:
    try:
        return bool(found.stop(name))
    except SwarmError:
        return False


@contextlib.contextmanager
def single_instance(name: str) -> Iterator[None]:
    """Hold a non-blocking lock for the life of a loop, so two loops never run at once."""
    path = logs_dir() / f".{name}.lock"
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SwarmError(f"another {name} loop already runs (lock {path})")
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def tail(path: Path, lines: int = 40, follow: bool = False) -> int:
    if not path.exists():
        print(f"No log yet at {path}.")
        return 0
    if follow:
        with contextlib.suppress(KeyboardInterrupt):
            return subprocess.call(["tail", "-n", str(lines), "-f", str(path)])
        return 0
    print("\n".join(path.read_text(errors="replace").splitlines()[-lines:]))
    return 0
