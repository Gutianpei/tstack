"""Task folders under <data_dir>/tasks: create them, move them, list them, check them.

A task is a folder holding task.md (front matter plus fixed sections, written by its owner) and,
once a worker finishes, result.md (written once by the worker).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import config, herdr
from .util import SwarmError, file_lock, log_stamp, now_stamp, one_line, today, write_atomic

STATUSES = ["open", "running", "waiting-on-you", "done", "dropped"]
ACTIVE = ("open", "running", "waiting-on-you")
KEYS = ["id", "title", "status", "owner", "created", "updated", "waiting_since", "worker"]
REQUIRED_KEYS = ["id", "title", "status", "owner", "created", "updated"]
NO_QUESTION = "None."
QUESTION = "Question for the user"
SECTIONS = [
    ("Request", ""),
    ("Reading", "How the owner reads the request."),
    ("Scope", "- In scope:\n- Do not touch:"),
    ("Touches", "- Repo and branch:\n- Worktree:\n- Ports:"),
    ("Acceptance checks", "1."),
    (QUESTION, NO_QUESTION),
    ("Verdicts", ""),
    ("Log", ""),
]
RESULT_TEMPLATE = """# Result: {id}

Written once by the worker. Managers add verdicts in task.md, not here.

## Summary

## Evidence

- Commits:
- Paths:
- Checks run and their output:

## Open issues

- None.
"""

# Feature modules add checks to `swarm check`: CHECKS.append(fn), fn() -> [problem, ...].
CHECKS: List[Callable[[], List[str]]] = []


# --- owners ---------------------------------------------------------------------------


def owners(values: Optional[Dict[str, Any]] = None) -> List[str]:
    swarm = config.section("swarm", values if values is not None else config.load())
    return [str(swarm.get("overall") or "overall"), *map(str, swarm.get("managers") or {})]


# --- task files -------------------------------------------------------------------------


def parse(text: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    if not text.startswith("---\n"):
        raise ValueError("task.md does not start with ---")
    head, _, body = text[4:].partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    sections: Dict[str, str] = {}
    current = None
    for line in body.splitlines():
        match = re.match(r"^## (.+)$", line)
        if match:
            current = match.group(1).strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return meta, {key: value.strip("\n") for key, value in sections.items()}


def render(meta: Dict[str, str], sections: Dict[str, str]) -> str:
    keys = KEYS + [key for key in meta if key not in KEYS]
    head = "\n".join(f"{key}: {meta.get(key, '')}".rstrip() for key in keys)
    names = [name for name, _ in SECTIONS] + [name for name in sections if name not in dict(SECTIONS)]
    body = "\n\n".join(f"## {name}\n\n{sections.get(name, '')}".rstrip() for name in names)
    return f"---\n{head}\n---\n\n{body}\n"


class Task:
    def __init__(self, folder: Path):
        self.folder = folder
        self.meta, self.sections = parse((folder / "task.md").read_text())

    @property
    def id(self) -> str:
        return self.meta.get("id", self.folder.name)

    @property
    def status(self) -> str:
        return self.meta.get("status", "open")

    @property
    def result(self) -> Path:
        return self.folder / "result.md"

    def save(self) -> None:
        write_atomic(self.folder / "task.md", render(self.meta, self.sections))

    def log(self, by: str, text: str) -> None:
        line = f"- {log_stamp()} {by}: {one_line(text)}"
        self.sections["Log"] = (self.sections.get("Log", "").rstrip("\n") + "\n" + line).strip("\n")
        self.meta["updated"] = now_stamp()


def tasks_root() -> Path:
    return config.data_dir() / "tasks"


def load_tasks() -> List[Task]:
    found = []
    root = tasks_root()
    if root.exists():
        for folder in sorted(root.iterdir()):
            if (folder / "task.md").exists():
                with contextlib.suppress(ValueError, OSError):
                    found.append(Task(folder))
    return found


def find_task(query: str) -> Task:
    tasks = load_tasks()
    exact = [task for task in tasks if task.id == query]
    hits = exact or [task for task in tasks if query in task.id]
    if not hits:
        raise SwarmError(f"no task matches {query!r}")
    if len(hits) > 1:
        raise SwarmError(f"{query!r} matches several tasks: {', '.join(task.id for task in hits)}")
    return hits[0]


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Serialize task edits across processes."""
    with file_lock(config.data_dir() / ".lock"):
        yield


def add_verdict(task: Task, by: str, text: str) -> None:
    line = f"- {log_stamp()} {by}: {one_line(text)} ([result](result.md))"
    task.sections["Verdicts"] = (task.sections.get("Verdicts", "").rstrip("\n") + "\n" + line).strip("\n")


# --- commands ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    root = config.data_dir()
    for sub in ("tasks", "handoffs", "logs", "templates"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    blank = {"id": "<id>", "title": "<title>", "status": "open", "owner": "<owner>",
             "created": "<now>", "updated": "<now>"}
    (root / "templates" / "task.md").write_text(render(blank, dict(SECTIONS)))
    (root / "templates" / "result.md").write_text(RESULT_TEMPLATE.format(id="<id>"))
    print(f"swarm data folder ready: {root}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    values = config.load()
    if args.owner not in owners(values):
        raise SwarmError(f"unknown owner {args.owner}; allowed: {', '.join(owners(values))}")
    slug = re.sub(r"[^a-z0-9]+", "-", args.slug.casefold()).strip("-")[:48].strip("-")
    if not slug:
        raise SwarmError("the slug needs letters or digits")
    with locked():
        base = f"{today()}-{slug}"
        task_id, number = base, 2
        while (tasks_root() / task_id).exists():
            task_id, number = f"{base}-{number}", number + 1
        folder = tasks_root() / task_id
        folder.mkdir(parents=True)
        stamp = now_stamp()
        meta = {"id": task_id, "title": one_line(args.title), "status": "open", "owner": args.owner,
                "created": stamp, "updated": stamp}
        sections = dict(SECTIONS)
        request = args.request or "(not given)"
        sections["Request"] = "\n".join("> " + line for line in request.splitlines())
        sections["Log"] = f"- {log_stamp()} {args.by or args.owner}: created"
        (folder / "task.md").write_text(render(meta, sections))
    print(folder)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    with locked():
        task = find_task(args.id)
        old, new = task.status, args.status
        if old == new:
            raise SwarmError(f"{task.id} is already {new}")
        by = args.by or task.meta.get("owner", "?")
        if new == "waiting-on-you":
            if not (args.question and args.recommend):
                raise SwarmError("waiting-on-you needs --question and --recommend")
            task.meta["waiting_since"] = now_stamp()
            task.sections[QUESTION] = f"{args.question}\n\nRecommended answer: {args.recommend}"
            note = f"waiting on the user: {args.question}"
        else:
            if old == "waiting-on-you":
                task.meta["waiting_since"] = ""
                task.sections[QUESTION] = NO_QUESTION
            note = f"{old} -> {new}"
        if new == "done":
            if not task.result.exists():
                raise SwarmError(f"done needs {task.result}")
            if not args.verdict:
                raise SwarmError("done needs --verdict")
            add_verdict(task, by, args.verdict)
        if new == "dropped":
            if not args.reason:
                raise SwarmError("dropped needs --reason")
            note += f" (reason: {args.reason})"
        task.meta["status"] = new
        task.log(by, note)
        task.save()
    print(f"{task.id}: {old} -> {new}")
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    with locked():
        task = find_task(args.id)
        add_verdict(task, args.by, args.text)
        task.log(args.by, "verdict added")
        task.save()
    print(f"{task.id}: verdict added")
    return 0


def age(stamp: str) -> str:
    try:
        seconds = (datetime.now().astimezone() - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return "?"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes // 1440}d"


def listing(show_all: bool, owner: Optional[str]) -> str:
    tasks = [task for task in load_tasks() if not owner or task.meta.get("owner") == owner]
    day = today()
    recent = lambda t: show_all or t.meta.get("updated", "").startswith(day)  # noqa: E731
    groups = [
        ("Waiting on you", [t for t in tasks if t.status == "waiting-on-you"]),
        ("Running", [t for t in tasks if t.status == "running"]),
        ("Open", [t for t in tasks if t.status == "open"]),
        ("Done" if show_all else "Done today", [t for t in tasks if t.status == "done" and recent(t)]),
        ("Dropped" if show_all else "Dropped today", [t for t in tasks if t.status == "dropped" and recent(t)]),
    ]
    lines = []
    for heading, items in groups:
        if not items:
            continue
        lines.append(f"{heading} ({len(items)})")
        for task in items:
            worker = f" [{task.meta['worker']}]" if task.meta.get("worker") and task.status in ACTIVE else ""
            lines.append(f"  {task.id}  @{task.meta.get('owner')}{worker}  {task.meta.get('title')}"
                         f"  ({age(task.meta.get('updated', ''))} ago)")
            if task.status == "waiting-on-you":
                for line in task.sections.get(QUESTION, "").splitlines():
                    if line.strip():
                        lines.append(f"      {line.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() or "No tasks."


NOTICE_STATUSES = ("waiting-on-you", "done", "dropped")
NOTICE_DAYS = 7


def notice_key(task: Task) -> str:
    """What makes a notice new (same rule as the Slack bridge): a task that waits again is new,
    a second verdict is not."""
    if task.status == "waiting-on-you":
        return f"waiting-on-you@{task.meta.get('waiting_since', '')}"
    return task.status


def popups(consumer: str = "herdr") -> int:
    """Show one herdr pop-up per task that newly waits on the user, or was newly done or dropped
    (done and dropped within NOTICE_DAYS). Returns the number shown.

    What was shown is `seen.<consumer>` in <data_dir>/state/notices.json, the notice state shared
    with the Slack bridge (format in docs/slack.md), under the flock state/notices.lock. `swarm list
    --watch` and the routine both call this, so each task pops up once whichever sees it first. A
    missing consumer is a first run: finished tasks are recorded without a pop-up.
    """
    path = config.data_dir() / "state" / "notices.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    shown = 0
    with file_lock(path.with_suffix(".lock")):
        state: Dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(path.read_text())
            state = loaded if isinstance(loaded, dict) else {}
        state.setdefault("version", 1)
        seen_all = state.setdefault("seen", {})
        first = consumer not in seen_all
        seen = seen_all.setdefault(consumer, {})
        cutoff = time.time() - NOTICE_DAYS * 86400
        for task in load_tasks():
            if task.status not in NOTICE_STATUSES or seen.get(task.id) == notice_key(task):
                continue
            if task.status != "waiting-on-you":
                try:
                    if datetime.fromisoformat(task.meta.get("updated", "")).timestamp() < cutoff:
                        continue
                except ValueError:
                    continue
            seen[task.id] = notice_key(task)
            if first and task.status != "waiting-on-you":
                continue
            owner = task.meta.get("owner", "?")
            if task.status == "waiting-on-you":
                question = (task.sections.get(QUESTION, "").splitlines() or [""])[0]
                herdr.notify(f"@{owner} needs you: {task.meta.get('title')}", question, "request")
            else:
                herdr.notify(f"{task.status}: {task.meta.get('title')}", f"@{owner} · {task.id}", "done")
            shown += 1
        write_atomic(path, json.dumps(state, indent=1) + "\n")
    return shown


def cmd_list(args: argparse.Namespace) -> int:
    if not args.watch:
        print(listing(args.all, args.owner))
        return 0
    while True:
        with contextlib.suppress(Exception):
            popups()
        text = listing(args.all, args.owner)
        sys.stdout.write("\033[2J\033[H" + f"swarm tasks · {datetime.now():%H:%M:%S} · every {args.watch}s\n\n"
                         + text + "\n")
        sys.stdout.flush()
        time.sleep(args.watch)


def check_task(folder: Path, allowed_owners: List[str]) -> List[str]:
    """Everything wrong with one task folder."""
    file = folder / "task.md"
    if not file.is_file():
        return ["task.md is missing"]
    try:
        meta, sections = parse(file.read_text())
    except (OSError, ValueError) as error:
        return [f"front matter does not parse: {error}"]
    problems = [f"missing key: {key}" for key in REQUIRED_KEYS if not meta.get(key)]
    if meta.get("id") and meta["id"] != folder.name:
        problems.append(f"id {meta['id']} differs from its folder")
    for key, allowed in (("status", STATUSES), ("owner", allowed_owners)):
        if meta.get(key) and meta[key] not in allowed:
            problems.append(f"{key} {meta[key]!r} is not one of: {', '.join(allowed)}")
    for key in ("created", "updated"):
        with contextlib.suppress(KeyError):
            try:
                datetime.fromisoformat(meta[key])
            except ValueError:
                problems.append(f"{key} {meta[key]!r} is not an ISO timestamp")
    names = [name for name, _ in SECTIONS]
    if [name for name in sections if name in names] != names:
        problems.append("headings are missing or out of order")
    status = meta.get("status")
    if status == "waiting-on-you":
        if not meta.get("waiting_since"):
            problems.append("waiting-on-you without waiting_since")
        if sections.get(QUESTION, "") in ("", NO_QUESTION):
            problems.append("waiting-on-you without a question")
    if status == "done":
        if not (folder / "result.md").is_file():
            problems.append("done without result.md")
        if not [line for line in sections.get("Verdicts", "").splitlines() if line.startswith("- ")]:
            problems.append("done without a verdict")
    if status == "dropped" and "reason:" not in sections.get("Log", ""):
        problems.append("dropped without a reason in the log")
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    allowed = owners()
    root = tasks_root()
    problems = []
    folders = sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []
    for folder in folders:
        if args.id and args.id not in folder.name:
            continue
        problems += [f"{folder.name}: {problem}" for problem in check_task(folder, allowed)]
    if not args.id:
        for check in CHECKS:
            problems += check()
    for problem in problems:
        print(problem)
    print(f"checked {len(folders)} task folder(s): {'ok' if not problems else str(len(problems)) + ' problem(s)'}")
    return 1 if problems else 0


def register(sub: argparse._SubParsersAction) -> None:
    sub.add_parser("init", help="create the data folder and templates").set_defaults(run=cmd_init)

    p = sub.add_parser("new", help="create a task")
    p.add_argument("slug")
    p.add_argument("--owner", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--request", help="the user's words, verbatim")
    p.add_argument("--by")
    p.set_defaults(run=cmd_new)

    p = sub.add_parser("set", help="move a task: " + "|".join(STATUSES))
    p.add_argument("id")
    p.add_argument("status", choices=STATUSES)
    p.add_argument("--by")
    p.add_argument("--question")
    p.add_argument("--recommend")
    p.add_argument("--reason")
    p.add_argument("--verdict")
    p.set_defaults(run=cmd_set)

    p = sub.add_parser("verdict", help="add a verdict line to a task")
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--by", required=True)
    p.set_defaults(run=cmd_verdict)

    p = sub.add_parser("list", help="show tasks; --watch N redraws and shows herdr pop-ups")
    p.add_argument("--all", action="store_true")
    p.add_argument("--owner")
    p.add_argument("--watch", type=int, default=0, metavar="SECONDS")
    p.set_defaults(run=cmd_list)

    p = sub.add_parser("check", help="validate task files (and checks other modules add)")
    p.add_argument("id", nargs="?", help="only folders whose name contains this")
    p.set_defaults(run=cmd_check)
