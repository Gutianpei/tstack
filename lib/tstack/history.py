"""Suggest managers and memory nodes from your Claude Code session history.

    swarm suggest-managers [--days 60] [--history-dir ~/.claude/projects] [--json] [--out FILE] [--snippets 3]

Claude Code writes one transcript per session to `~/.claude/projects/<encoded folder>/<session id>.jsonl`.
This command reads those files (read only, on this machine, no model calls), groups sessions by the
folder they ran in, merges folders that the same sessions keep touching, and suggests one manager (and
one memory node) per group of repeated work. It prints a Markdown report, or JSON with --json, and
writes a file only when you pass --out.

How a session is read: its folder is the most common `cwd`; its prompts are the messages you typed
(tool results, system notes and slash-command wrappers are skipped); the folders it edited come from
the file paths in its tool calls. Sessions started by a swarm manager for a worker, and first prompts
repeated many times (scripts), are counted but not used for suggestions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from . import config
from .util import SwarmError

DEFAULT_DIR = "~/.claude/projects"
SNIPPET_CHARS = 120
# A group needs this much activity to anchor a manager of its own.
ACTIVE_SESSIONS = 3
ACTIVE_DAYS = 2
DORMANT_DAYS = 30
# Two folders share a manager when this many sessions touch both, and those sessions are at least
# LINK_SHARE of all sessions that touch either.
LINK_SESSIONS = 3
LINK_SHARE = 0.3
# A folder must appear this often in a session's edits before the session counts as touching it.
MIN_HITS = 3
SCRIPT_REPEATS = 5
WORKER_PROMPT = re.compile(r"^You are [\w.-]+, a (worker|swarm manager|swarm worker)\b")
PATH_KEYS = ("file_path", "path", "notebook_path")
STOP = set("""
about above after again also always another because been before being below between both
build call came can cannot check code come could create does doing done each else even every file files
find first from gave give going good have here into just keep know last later like look made make more
most much must need next none not note now only other over please really right same should show some
still such sure take than that them then there these they thing think this those through very want
well were what when where which while will with without work would write your yours
""".split())


class Session:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.id = path.stem
        self.cwds: Counter = Counter()
        self.prompts: List[str] = []
        self.days: Set[str] = set()
        self.first: Optional[dt.datetime] = None
        self.last: Optional[dt.datetime] = None
        self.touched: Counter = Counter()  # path -> hits from tool calls
        self.kind = "human"  # human | worker | script

    @property
    def cwd(self) -> str:
        return self.cwds.most_common(1)[0][0] if self.cwds else ""


def parse_time(text: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def human_text(message: Any) -> str:
    """The text of a message the user typed, or '' for tool results, system notes and command wrappers."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if not text or text.startswith(("<", "Caveat:", "[Request interrupted")):
        return ""
    return text


def read_session(path: Path, since: dt.datetime) -> Optional[Session]:
    """Parse one transcript. None when it has no activity since `since` or no prompts."""
    session = Session(path)
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            if '"user"' not in line and '"tool_use"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            when = parse_time(str(entry.get("timestamp", "")))
            if entry.get("cwd"):
                session.cwds[str(entry["cwd"])] += 1
            if entry.get("type") == "assistant":
                content = (entry.get("message") or {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        args = block.get("input") or {}
                        for key in PATH_KEYS:
                            if isinstance(args.get(key), str) and args[key].startswith("/"):
                                session.touched[args[key]] += 1
                continue
            if entry.get("type") != "user" or entry.get("isMeta"):
                continue
            origin = entry.get("origin")
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                continue
            text = human_text(entry.get("message"))
            if not text:
                continue
            if when is not None:
                session.first = min(session.first, when) if session.first else when
                session.last = max(session.last, when) if session.last else when
                if when >= since:
                    session.days.add(when.strftime("%Y-%m-%d"))
            session.prompts.append(text)
    if not session.prompts or not session.last or session.last < since or not session.days:
        return None
    if WORKER_PROMPT.match(session.prompts[0]):
        session.kind = "worker"
    return session


def project_files(history_dir: Path) -> Iterator[Path]:
    if history_dir.is_dir():
        for folder in sorted(history_dir.iterdir()):
            if folder.is_dir():
                yield from sorted(folder.glob("*.jsonl"))


# --- grouping ---------------------------------------------------------------------------------


def project_key(path: str, workspace: Path, home: Path) -> str:
    """The folder a path belongs to: the first folder under the workspace (worktree folders such as
    `app-wt` count as `app`), else the first two folders under home, else the path itself."""
    p = Path(path)
    for base, depth in ((workspace, 1), (home, 2)):
        try:
            parts = p.relative_to(base).parts
        except ValueError:
            continue
        if not parts:
            return "(root)"
        key = "/".join(parts[:depth])
        return re.sub(r"-(wt|worktrees?)$", "", key)
    return str(p)


def words(texts: List[str]) -> Counter:
    seen: Counter = Counter()
    for text in texts:
        seen.update({w for w in re.findall(r"[a-z][a-z0-9_-]{3,}", text.lower()) if w not in STOP})
    return seen


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "work"


def link_groups(sessions: List[Session], workspace: Path, home: Path) -> Dict[str, str]:
    """Union projects whose sessions overlap. Returns project -> group root project."""
    touches: List[Set[str]] = []
    for s in sessions:
        counts = Counter(project_key(p, workspace, home) for p, n in s.touched.items() for _ in range(n))
        keys = {k for k, n in counts.items() if n >= MIN_HITS}
        keys.add(project_key(s.cwd, workspace, home))
        touches.append(keys - {"(root)"})
    per: Dict[str, int] = Counter(k for keys in touches for k in keys)
    pair: Dict[Tuple[str, str], int] = Counter()
    for keys in touches:
        ordered = sorted(keys)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pair[(a, b)] += 1
    parent = {k: k for k in per}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for (a, b), n in pair.items():
        if n >= LINK_SESSIONS and n / (per[a] + per[b] - n) >= LINK_SHARE:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb if per[ra] >= per[rb] else ra] = ra if per[ra] >= per[rb] else rb
    return {k: find(k) for k in parent}


def analyse(history_dir: Path, days: int, snippets: int, workspace: Path, home: Path,
            now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=days)
    sessions, scanned = [], 0
    for path in project_files(history_dir):
        scanned += 1
        found = read_session(path, since)
        if found:
            sessions.append(found)
    firsts = Counter(s.prompts[0][:200] for s in sessions)
    for s in sessions:
        if s.kind == "human" and firsts[s.prompts[0][:200]] >= SCRIPT_REPEATS:
            s.kind = "script"
    people = [s for s in sessions if s.kind == "human"]
    roots = link_groups(people, workspace, home)
    groups: Dict[str, List[Session]] = defaultdict(list)
    members: Dict[str, Set[str]] = defaultdict(set)
    for s in people:
        key = project_key(s.cwd, workspace, home)
        if key == "(root)":
            groups["(root)"].append(s)
            continue
        root = roots.get(key, key)
        groups[root].append(s)
        members[root].add(key)
    out: List[Dict[str, Any]] = []
    for root, items in groups.items():
        days_active = sorted({d for s in items for d in s.days})
        last = max(s.last for s in items if s.last)
        top = words([p for s in items for p in s.prompts]).most_common(8)
        ordered = sorted(items, key=lambda s: s.first or now)
        kind = "unassigned" if root == "(root)" else (
            "manager" if len(items) >= ACTIVE_SESSIONS and len(days_active) >= ACTIVE_DAYS else "occasional")
        if kind == "manager" and (now - last).days > DORMANT_DAYS:
            kind = "dormant"
        out.append({
            "name": slugify(root.split("/")[-1]) if root != "(root)" else "(workspace root)",
            "suggest": kind,
            "folders": sorted(members.get(root) or {root}),
            "sessions": len(items),
            "prompts": sum(len(s.prompts) for s in items),
            "active_days": len(days_active),
            "first_active": days_active[0],
            "last_active": last.strftime("%Y-%m-%d"),
            "topics": [w for w, n in top if n >= 2][:6],
            "snippets": [re.sub(r"\s+", " ", s.prompts[0])[:SNIPPET_CHARS] for s in ordered[:snippets]],
        })
    out.sort(key=lambda g: (g["suggest"] != "manager", -g["sessions"]))
    return {"days": days, "history_dir": str(history_dir), "transcripts_scanned": scanned,
            "sessions": len(sessions), "human_sessions": len(people),
            "worker_sessions": sum(s.kind == "worker" for s in sessions),
            "script_sessions": sum(s.kind == "script" for s in sessions), "groups": out}


def render(result: Dict[str, Any]) -> str:
    lines = [f"# Manager suggestions from the last {result['days']} days", "",
             f"Read {result['transcripts_scanned']} transcripts under `{result['history_dir']}`: "
             f"{result['sessions']} sessions with activity ({result['human_sessions']} yours, "
             f"{result['worker_sessions']} swarm worker, {result['script_sessions']} scripted). Read only; "
             "nothing was written.", ""]
    heads = {"manager": "Suggested managers (repeated work on one folder)",
             "occasional": "Occasional folders (fold into an existing manager, or skip)",
             "dormant": "Dormant (a memory node is enough; no manager)",
             "unassigned": "Sessions started in the workspace root or home (no folder to anchor on)"}
    for kind, head in heads.items():
        group = [g for g in result["groups"] if g["suggest"] == kind]
        if not group:
            continue
        lines += [f"## {head}", ""]
        for g in group:
            lines.append(f"- **{g['name']}**: {g['sessions']} sessions on {g['active_days']} days, "
                         f"last {g['last_active']}; folders: {', '.join(g['folders'])}"
                         + (f"; topics: {', '.join(g['topics'])}" if g["topics"] else ""))
            for snippet in g["snippets"]:
                lines.append(f"  - first prompt: \"{snippet}\"")
            if kind == "manager":
                lines.append(f"  - to create: `swarm add-manager {g['name']} --role domain --cwd <folder>`")
        lines.append("")
    if not result["groups"]:
        lines.append("No sessions in this window. Check `--history-dir` and `--days`.")
    return "\n".join(lines).rstrip() + "\n"


def cmd_suggest(args: argparse.Namespace) -> int:
    values = config.load(required=False)
    history_dir = Path(args.history_dir or config.section("upkeep", values).get("history_dir") or DEFAULT_DIR).expanduser()
    if not history_dir.is_dir():
        raise SwarmError(f"no history folder at {history_dir}; pass --history-dir")
    workspace = Path(str(values.get("workspace") or "~/work")).expanduser()
    result = analyse(history_dir, args.days, max(0, args.snippets), workspace, Path.home())
    text = json.dumps(result, indent=2) + "\n" if args.json else render(result)
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("suggest-managers", help="suggest managers and nodes from Claude Code history",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=60, help="how far back to look (default 60)")
    p.add_argument("--history-dir", help=f"transcripts folder (default upkeep.history_dir, else {DEFAULT_DIR})")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p.add_argument("--out", metavar="FILE", help="write the report to FILE instead of printing it")
    p.add_argument("--snippets", type=int, default=3, help="first prompts to show per group (0 for none)")
    p.set_defaults(run=cmd_suggest)
