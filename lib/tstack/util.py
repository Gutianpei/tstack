"""Small helpers shared by every module: the error type, kit paths, time stamps, file locks."""

from __future__ import annotations

import contextlib
import fcntl
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator

KIT = Path(__file__).resolve().parents[2]
SWARM_BIN = KIT / "bin" / "swarm"


class SwarmError(Exception):
    """An expected failure: the CLI prints the message and exits 2."""


def now_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def fill(text: str, values: Dict[str, str]) -> str:
    """Replace {{KEY}} placeholders."""
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive flock on `path` (created if missing). Works on macOS and Linux."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
