"""Tools that keep a tstack memory repo small and lossless.

    swarm memory check [NODE ... | all] [--idle] [--rewrite]    size and format policy per node
    swarm memory archive agent NODE ... [--reason TEXT]          copy the committed AGENT.md to AGENT.archive.md
    swarm memory archive journals [NODE ... | all] [--apply]     move old journal entries to journal.archive.md
    swarm memory backup [--backend disk|s3] [--dest ...]         bundle the repo into the upkeep backend

The memory repo is found from --root, else the git repo around the current folder when it has a
`nodes/` folder, else `memory_dir` in the settings file. Nothing here ever deletes text: archives only grow
and every move is checked against the original. `python3 -m tstack.memory_tools guard` is the check the
memory repo's pre-commit hook runs (journals are append-only).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .util import SwarmError, write_atomic

AGENT_LIMIT = 12 * 1024
IDLE_LIMIT = 4 * 1024
JOURNAL_LIMIT = 32 * 1024
HISTORY_TARGET = 8 * 1024
ROW_LIMIT = 320
LONG_LINE = 400
WINDOW_DAYS = 14
MIN_KEEP = 3
RECENT_SECONDS = 3600
ENTRY = re.compile(r"^## \[?\d{4}-\d{2}")
ENTRY_DATE = re.compile(r"^## \[?(\d{4}-\d{2}-\d{2})")
POINTER = "> Older entries are in [journal.archive.md](journal.archive.md).\n"


# --- finding the repo ------------------------------------------------------------------------------


def find_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        root, source = Path(explicit).expanduser(), "--root"
    else:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if top.returncode == 0 and (Path(top.stdout.strip()) / "nodes").is_dir():
            return Path(top.stdout.strip())
        root, source = config.path("memory_dir", config.load(required=False)), "memory_dir in the settings file"
    root = root.resolve()
    if not (root / "nodes").is_dir():
        raise SwarmError(f"{root} (from {source}) has no nodes/ folder, so it is not a tstack memory repo")
    return root


def pick_nodes(root: Path, names: List[str], need: str = "") -> List[str]:
    nodes = root / "nodes"
    if names == ["all"]:
        return sorted(n.name for n in nodes.iterdir() if n.is_dir() and (not need or (n / need).is_file()))
    return names


def run_git(root: Path, *args: str) -> Optional[bytes]:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    return done.stdout if done.returncode == 0 else None


def read(path: Path) -> Optional[bytes]:
    return path.read_bytes() if path.exists() else None


def changed_lines(root: Path, path: str, sign: str) -> List[str]:
    """Non-blank lines the working copy of `path` adds ('+') or removes ('-') since HEAD."""
    if run_git(root, "cat-file", "-e", f"HEAD:{path}") is None:
        if sign == "-":
            return []
        text = read(root / path) or b""
        return [x for x in text.decode("utf-8", "replace").splitlines() if x.strip()]
    out = (run_git(root, "diff", "HEAD", "-U0", "--no-renames", "--", path) or b"").decode("utf-8", "replace")
    return [x[1:] for x in out.splitlines()
            if not x.startswith(("+++", "---")) and x.startswith(sign) and x[1:].strip()]


# --- check -------------------------------------------------------------------------------------------


def check_node(root: Path, node: str, idle: bool, rewrite: bool) -> Tuple[List[str], List[str], List[str]]:
    folder = root / "nodes" / node
    problems: List[str] = []
    warnings: List[str] = []
    sizes: List[str] = []
    agent = read(folder / "AGENT.md")
    limit = IDLE_LIMIT if idle else AGENT_LIMIT
    if agent is None:
        problems.append("AGENT.md is missing")
    else:
        sizes.append(f"AGENT.md: {len(agent)} bytes (limit {limit})")
        if len(agent) > limit:
            problems.append(f"AGENT.md is {len(agent)} bytes; the limit is {limit}")
        if b"HISTORY.md" not in agent:
            problems.append("AGENT.md does not link HISTORY.md")
        long = [x for x in agent.decode("utf-8", "replace").splitlines() if len(x) > LONG_LINE]
        if long:
            warnings.append(f"AGENT.md has {len(long)} line(s) over {LONG_LINE} characters; split them")
    history = read(folder / "HISTORY.md")
    if history is None:
        problems.append("HISTORY.md is missing")
    else:
        rows = [x for x in history.decode("utf-8", "replace").splitlines() if x.startswith("|")]
        body = rows[2:]
        sizes.append(f"HISTORY.md: {len(history)} bytes, {len(body)} rows")
        if len(rows) < 2 or not re.match(r"^\|\s*:?-+", rows[1]):
            problems.append("HISTORY.md has no table header and separator row")
        over = [x for x in body if len(x) > ROW_LIMIT]
        if over:
            problems.append(f"{len(over)} HISTORY.md row(s) over {ROW_LIMIT} characters, for example {over[0][:70]!r}")
        bad = [x for x in rows if x.count("|") - x.count("\\|") != 5]
        if bad:
            problems.append(f"{len(bad)} HISTORY.md row(s) without exactly 4 columns, for example {bad[0][:70]!r}")
        if len(history) > HISTORY_TARGET:
            warnings.append(f"HISTORY.md is {len(history)} bytes; the target is about 8 KB")
    journal = read(folder / "journal.md")
    if journal is not None:
        sizes.append(f"journal.md: {len(journal)} bytes (limit {JOURNAL_LIMIT})")
        if len(journal) > JOURNAL_LIMIT:
            problems.append(f"journal.md is {len(journal)} bytes; run `swarm memory archive journals --apply {node}`")
    if rewrite:
        old = run_git(root, "show", f"HEAD:nodes/{node}/AGENT.md")
        archive = read(folder / "AGENT.archive.md") or b""
        if old is not None and not archive.endswith(old):
            problems.append("AGENT.archive.md does not end with the committed AGENT.md; "
                            f"run `swarm memory archive agent {node}` first")
        jpath, apath = f"nodes/{node}/journal.md", f"nodes/{node}/journal.archive.md"
        lost = Counter(changed_lines(root, jpath, "-")) - (Counter(changed_lines(root, apath, "+"))
                                                          + Counter(changed_lines(root, jpath, "+")))
        if lost:
            problems.append(f"journal.md loses {sum(lost.values())} line(s) that are not in journal.archive.md, "
                            f"for example {next(lost.elements())[:70]!r}")
        gone = changed_lines(root, apath, "-")
        if gone:
            problems.append(f"journal.archive.md loses {len(gone)} line(s); the archive only grows")
    return problems, warnings, sizes


def cmd_check(args: argparse.Namespace) -> int:
    root = find_root(args.root)
    nodes = pick_nodes(root, args.nodes)
    failed = 0
    for node in nodes:
        print(f"== {node}")
        if not (root / "nodes" / node).is_dir():
            print("PROBLEMS:\n- no such node folder")
            failed += 1
            continue
        problems, warnings, sizes = check_node(root, node, args.idle, args.rewrite)
        for line in sizes:
            print(line)
        for warning in warnings:
            print(f"warning: {warning}")
        print("OK" if not problems else "PROBLEMS:\n- " + "\n- ".join(problems))
        failed += bool(problems)
    if len(nodes) > 1:
        print(f"\n{len(nodes) - failed} of {len(nodes)} nodes OK")
    return 1 if failed else 0


# --- archive agent -----------------------------------------------------------------------------------


def archive_agent(root: Path, node: str, reason: str) -> None:
    folder = root / "nodes" / node
    old = run_git(root, "show", f"HEAD:nodes/{node}/AGENT.md")
    if old is None:
        print(f"{node}: no committed AGENT.md; nothing to archive")
        return
    current_agent = read(folder / "AGENT.md")
    if current_agent is not None and current_agent != old:
        print(f"{node}: warning: AGENT.md has uncommitted edits; only the committed version is archived")
    path = folder / "AGENT.archive.md"
    current = read(path)
    if current is not None and current.endswith(old):
        print(f"{node}: AGENT.archive.md already ends with the committed AGENT.md; nothing appended")
        return
    header = f"## Full AGENT.md before the {time.strftime('%Y-%m-%d')} {reason} (verbatim)\n\n".encode()
    if current is None:
        new = f"# {node} — AGENT.md archive\n\n".encode() + header + old
    else:
        sep = b"" if current.endswith(b"\n\n") else (b"\n" if current.endswith(b"\n") else b"\n\n")
        new = current + sep + header + old
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(new)
    os.replace(str(tmp), str(path))
    if path.read_bytes() != new or not new.endswith(old):
        raise SwarmError(f"{path} does not match what was written; check it by hand")
    print(f"{node}: appended the committed AGENT.md ({len(old)} bytes) to AGENT.archive.md")


# --- archive journals --------------------------------------------------------------------------------


def split_entries(lines: List[str]) -> Tuple[List[str], List[List[str]]]:
    preamble: List[str] = []
    entries: List[List[str]] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and ENTRY.match(line):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            preamble.append(line)
    return preamble, entries


def with_pointer(preamble: List[str]) -> List[str]:
    if any("journal.archive.md" in x for x in preamble):
        return list(preamble)
    if preamble and preamble[0].startswith("# "):
        rest = preamble[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        return [preamble[0], "\n", POINTER, "\n", *rest]
    return [POINTER, "\n", *preamble]


def plan_journal(root: Path, node: str) -> Optional[Dict[str, Any]]:
    jpath = root / "nodes" / node / "journal.md"
    apath = root / "nodes" / node / "journal.archive.md"
    original = jpath.read_text(encoding="utf-8")
    preamble, entries = split_entries(original.splitlines(keepends=True))
    new_pre = with_pointer(preamble)
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - WINDOW_DAYS * 86400))
    kept: List[List[str]] = []
    total = len("".join(new_pre).encode())
    for entry in reversed(entries):
        size = len("".join(entry).encode())
        date = ENTRY_DATE.match(entry[0])
        in_window = bool(date) and date.group(1) >= cutoff
        if kept and total + size > JOURNAL_LIMIT:
            break
        if len(kept) >= MIN_KEEP and not in_window:
            break
        kept.insert(0, entry)
        total += size
    moved = entries[: len(entries) - len(kept)]
    if not moved:
        return None
    moved_text = "".join("".join(e) for e in moved)
    old_archive = apath.read_text(encoding="utf-8") if apath.exists() else ""
    if old_archive:
        sep = "" if old_archive.endswith("\n\n") else ("\n" if old_archive.endswith("\n") else "\n\n")
        new_archive = old_archive + sep + moved_text
    else:
        new_archive = (f"# {node} — journal archive\n\n"
                       "Entries moved verbatim out of `journal.md`, oldest first. `journal.md` keeps the last "
                       f"{WINDOW_DAYS} days (at least the newest {MIN_KEEP} entries) within {JOURNAL_LIMIT // 1024} KB.\n\n"
                       + moved_text)
    new_journal = "".join(new_pre) + "".join("".join(e) for e in kept)
    if not new_archive.endswith(moved_text) or any(x not in new_archive for e in moved for x in e if x.strip()):
        raise SwarmError(f"{node}: the planned archive does not hold every moved line; nothing written")
    return {"jpath": jpath, "apath": apath, "new_journal": new_journal, "new_archive": new_archive,
            "entries": len(entries), "moved": len(moved), "lines": sum(len(e) for e in moved),
            "before": len(original.encode()), "after": len(new_journal.encode()),
            "first_kept": kept[0][0].strip()[:70] if kept else "", "mtime": jpath.stat().st_mtime}


def cmd_archive(args: argparse.Namespace) -> int:
    root = find_root(args.root)
    if args.what == "agent":
        missing = [n for n in args.nodes if not (root / "nodes" / n).is_dir()]
        if missing:
            raise SwarmError(f"no such node folder: {', '.join(missing)}")
        for node in args.nodes:
            archive_agent(root, node, args.reason)
        return 0
    failed = False
    for node in pick_nodes(root, args.nodes, need="journal.md"):
        if not (root / "nodes" / node / "journal.md").is_file():
            print(f"{node:15s} no such node, or it has no journal.md")
            failed = True
            continue
        plan = plan_journal(root, node)
        if plan is None:
            print(f"{node:15s} nothing to move")
            continue
        recent = not args.mine and time.time() - plan["mtime"] < RECENT_SECONDS
        print(f"{node:15s} entries {plan['entries']:3d}, move {plan['moved']:3d} ({plan['lines']:5d} lines); "
              f"journal {plan['before'] / 1024:6.1f} KB -> {plan['after'] / 1024:4.1f} KB; "
              f"first kept: {plan['first_kept']}" + ("   SKIPPED: changed in the last hour (use --mine)" if recent else ""))
        if args.apply and not recent:
            write_atomic(plan["apath"], plan["new_archive"])
            write_atomic(plan["jpath"], plan["new_journal"])
    if not args.apply:
        print("dry run: pass --apply to write the move")
    return 1 if failed else 0


# --- backup ------------------------------------------------------------------------------------------


def cmd_backup(args: argparse.Namespace) -> int:
    from .upkeep import Settings, open_store  # late: upkeep needs the full config

    root = find_root(args.root)
    store = open_store(Settings(backend=args.backend or "", dest=args.dest or ""))
    day = dt.date.today().isoformat()
    tmp = root / ".git" / "tstack-backup.bundle"
    done = subprocess.run(["git", "-C", str(root), "bundle", "create", str(tmp), "--all"], capture_output=True, text=True)
    if done.returncode != 0:
        raise SwarmError(f"git bundle failed: {done.stderr.strip()}")
    try:
        for key in (f"memory/tstack-{day}.bundle", "memory/tstack-latest.bundle"):
            ok, detail = store.put(tmp, key)
            if not ok:
                raise SwarmError(f"upload {key}: {detail}")
    finally:
        tmp.unlink()
    head = (run_git(root, "rev-parse", "--short", "HEAD") or b"?").decode().strip()
    print(f"memory repo at {head} bundled to {store.describe()}/memory/tstack-{day}.bundle (committed refs only)")
    print("restore: copy memory/tstack-latest.bundle out of the backend, then `git clone tstack-latest.bundle <memory_dir>`")
    return 0


# --- guard (pre-commit) ------------------------------------------------------------------------------


def staged_lines(path: str, sign: str) -> List[str]:
    done = subprocess.run(["git", "diff", "--cached", "-U0", "--no-renames", "--", path], capture_output=True)
    out = done.stdout.decode("utf-8", "replace")
    return [x[1:] for x in out.splitlines()
            if not x.startswith(("+++", "---")) and x.startswith(sign) and x[1:].strip()]


def guard() -> int:
    """Fail a commit that removes journal lines without adding them to the same node's archive."""
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "--no-renames", "-z"], capture_output=True)
    staged = [p for p in names.stdout.decode("utf-8", "replace").split("\0") if p]
    problems = []
    for path in staged:
        match = re.match(r"^nodes/([^/]+)/journal\.md$", path)
        if not match:
            continue
        archive = f"nodes/{match.group(1)}/journal.archive.md"
        lost = Counter(staged_lines(path, "-")) - (Counter(staged_lines(archive, "+")) + Counter(staged_lines(path, "+")))
        if lost:
            problems.append(f"{path} loses {sum(lost.values())} line(s) that are not in journal.archive.md "
                            f"(for example {next(lost.elements())[:60]!r})")
    if problems:
        print("tstack: journals are append-only. Move old entries with `swarm memory archive journals --apply <node>`.",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("  A deliberate correction: TSTACK_JOURNAL_EDIT=1 git commit ...", file=sys.stderr)
        return 1
    return 0


# --- command table -----------------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("memory", help="check and archive memory nodes, back up the memory repo",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    acts = p.add_subparsers(dest="action", metavar="<action>")
    acts.required = True
    root_help = "memory repo (default: the git repo around the current folder, else memory_dir)"

    a = acts.add_parser("check", help="check nodes against the size and format policy")
    a.add_argument("nodes", nargs="+", metavar="NODE", help='node names, or "all"')
    a.add_argument("--idle", action="store_true", help="apply the 4 KB AGENT.md limit of an idle node")
    a.add_argument("--rewrite", action="store_true", help="also check that an uncommitted rewrite lost nothing")
    a.add_argument("--root", help=root_help)
    a.set_defaults(run=cmd_check)

    a = acts.add_parser("archive", help="move text out of AGENT.md or journal.md without losing it")
    a.add_argument("what", choices=["agent", "journals"])
    a.add_argument("nodes", nargs="+", metavar="NODE", help='node names, or "all" (journals only)')
    a.add_argument("--reason", default="rewrite", help='agent: header wording, for example "size-policy trim"')
    a.add_argument("--apply", action="store_true", help="journals: write the move (default is a dry run)")
    a.add_argument("--mine", action="store_true", help="journals: the change in the last hour was this session's")
    a.add_argument("--root", help=root_help)
    a.set_defaults(run=cmd_archive)

    a = acts.add_parser("backup", help="bundle the memory repo into the upkeep backend")
    a.add_argument("--backend", help="disk or s3 (default upkeep.backend)")
    a.add_argument("--dest", help="override the backend location")
    a.add_argument("--root", help=root_help)
    a.set_defaults(run=cmd_backup)


if __name__ == "__main__":
    sys.exit(guard() if sys.argv[1:] == ["guard"] else 2)
