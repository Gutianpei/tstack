"""Running agents on shared dev servers and workstations: GPUs, worktrees, storage, disk.

    swarm gpus [--json]                               GPUs, their reservations, the outside locks
    swarm run --task ID (--gpus N|--gpu LIST) -- CMD  hold GPUs while CMD runs
    swarm reserve --task ID (--gpus N|--gpu LIST) --hours H
    swarm release --task ID [--gpu LIST]
    swarm worktree add|list|remove                    one git worktree per worker
    swarm storage [--rule|--write-rule]               where scratch, checkpoints, caches, worktrees go
    swarm disk [--json]                               free space per configured path

The GPU model lives in gpu.py. Settings: the `server` section (see docs/server.md).
Other modules call `disk_status(values)` for disk alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, gpu, tasks
from .util import KIT, SwarmError, file_lock, fill, now_stamp, write_atomic

WAIT_POLL = 10
COMMAND_LIMIT = 500


def settings(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return config.section("server", values if values is not None else config.load())


def server_path(key: str, values: Dict[str, Any]) -> Optional[Path]:
    """A path key of the server section, expanded; None when empty."""
    value = settings(values).get(key)
    return Path(str(value)).expanduser() if value else None


def worktree_base(values: Dict[str, Any]) -> Path:
    """Folder holding `<repo>-wt/` folders: server.worktree_root, else the workspace."""
    return server_path("worktree_root", values) or config.path("workspace", values)


def storage_dir(key: str, values: Dict[str, Any]) -> Path:
    """server.scratch_dir or server.ckpts_dir; empty means <workspace>/scratch or <workspace>/ckpts."""
    default = {"scratch_dir": "scratch", "ckpts_dir": "ckpts"}[key]
    return server_path(key, values) or config.path("workspace", values) / default


# --- GPU commands -----------------------------------------------------------------------


def cmd_gpus(args: argparse.Namespace) -> int:
    values = config.load()
    state = gpu.read_gpus(values)
    slots = gpu.survey(state, values)
    threshold = gpu.busy_mib(values)
    locks = [(name, path, gpu.lock_state(path)) for name, path in gpu.outside_locks(values)]
    live = {slot.index for slot in slots if slot.state == "reserved"}
    loose = [p for p in state.processes if p.used_mib >= threshold and p.gpu not in live]
    if args.json:
        print(json.dumps({
            "backend": gpu.backend(values).name, "note": state.note, "error": state.error,
            "gpus": [{"index": g.index, "uuid": g.uuid, "memory_used_mib": g.used_mib,
                      "memory_total_mib": g.total_mib, "utilization": g.utilization,
                      "state": s.state, "reservation": s.data} for g, s in zip(state.gpus, slots)],
            "locks": [{"name": name, "path": str(path), "state": lock} for name, path, lock in locks],
            "unreserved_processes": [p.__dict__ for p in loose],
        }, indent=2))
        return 1 if state.error else 0
    if state.error:
        print(f"GPU state unavailable: {state.error}")
        return 1
    if not state.gpus:
        print(f"no GPUs found ({state.note})")
        return 0
    rows = [["GPU", "MEMORY (MiB)", "UTIL", "STATE", "RESERVATION"]]
    for g, slot in zip(state.gpus, slots):
        memory = "?" if g.used_mib is None or g.total_mib is None else f"{g.used_mib}/{g.total_mib}"
        rows.append([str(g.index), memory, "?" if g.utilization is None else f"{g.utilization}%",
                     slot.state, reservation_column(slot)])
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    for name, path, lock in locks:
        print(f"lock {name} {path}: {lock}")
    for p in loose:
        print(f"GPU {p.gpu}: pid {p.pid} ({p.name}) uses {p.used_mib} MiB with no reservation")
    return 0


def reservation_column(slot: gpu.Slot) -> str:
    data = slot.data
    if data is None or slot.state in ("free", "busy", "held by an unknown process"):
        return "-"
    head = f"{data.get('task') or '?'} @{data.get('owner') or '?'} {slot.kind or '?'}"
    if slot.kind == "run":
        command = str(data.get("command") or "")
        return f"{head} pid {data.get('pid')}: {command[:40] + ('…' if len(command) > 40 else '')}"
    return f"{head} until {str(data.get('expires') or '?')[:16].replace('T', ' ')}"


def cmd_reserve(args: argparse.Namespace) -> int:
    values = config.load()
    limit = float(settings(values).get("reserve_max_hours") or 72)
    if not 0 < args.hours <= limit:
        raise SwarmError(f"--hours must be above 0 and at most {limit:g} (server.reserve_max_hours)")
    task = gpu.claimable(args.task)
    by = args.by or task.meta.get("owner", "?")
    wanted = gpu.parse_list(args.gpu) if args.gpu else None
    with gpu.state_lock():
        held = gpu.outside_holders(values)
        if held:
            raise SwarmError("; ".join(held) + "; try later")
        state = gpu.read_gpus(values)
        gpu.need_gpus(state)
        slots = gpu.survey(state, values)
        try:
            chosen = gpu.pick(slots, task.id, wanted, args.gpus, args.allow_busy, gpu.busy_mib(values))
        except gpu.Blocked as blocked:
            raise SwarmError("cannot reserve: " + "; ".join(blocked.problems))
        expires = (datetime.now().astimezone() + timedelta(hours=args.hours)).isoformat(timespec="seconds")
        by_index = {slot.index: slot for slot in slots}
        for index in chosen:
            slot = by_index[index]
            gpu.log_replacement(slot, by, task.id)
            kept = (slot.data or {}) if slot.state == "reserved" and slot.kind == "reserve" else {}
            gpu.write_reservation(index, task, by, "reserve", gpu.pane_of(args.pane) or kept.get("pane"),
                                  None, expires, kept.get("created"))
            gpu.log_event("reserve", index, task.id, by, "reserve", expires=expires)
        text = ",".join(str(index) for index in chosen)
        gpu.note_on_task(task.id, by, f"reserved GPUs {text} until {expires[:16].replace('T', ' ')}")
    print(f"export CUDA_VISIBLE_DEVICES={text} CUDA_DEVICE_ORDER=PCI_BUS_ID")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command_args)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SwarmError("swarm run needs a command after --")
    values = config.load()
    task = gpu.claimable(args.task)
    by = args.by or task.meta.get("owner", "?")
    wanted = gpu.parse_list(args.gpu) if args.gpu else None
    deadline = time.monotonic() + args.wait if args.wait else None
    while True:
        try:
            claim = gpu.claim_for_run(task, by, gpu.pane_of(args.pane), wanted, args.gpus, args.allow_busy,
                                      shlex.join(command)[:COMMAND_LIMIT], values)
            break
        except gpu.Blocked as blocked:
            left = deadline - time.monotonic() if deadline else 0.0
            if left <= 0:
                raise SwarmError("cannot run: " + "; ".join(blocked.problems))
            time.sleep(min(WAIT_POLL, left))
    text = ",".join(str(index) for index in claim.gpus)
    print(f"swarm: running on GPU {text} for {task.id}", file=sys.stderr)
    code = 127
    try:
        code = gpu.run_child(command, text, claim.handles)
    finally:
        gpu.release_run(task.id, by, claim, code)
    return code


def cmd_release(args: argparse.Namespace) -> int:
    task = tasks.find_task(args.task)
    by = args.by or task.meta.get("owner", "?")
    wanted = gpu.parse_list(args.gpu) if args.gpu else None
    with gpu.state_lock():
        held = {index: gpu.make_slot(index) for index, _ in gpu.reservation_files()}
        held = {index: slot for index, slot in held.items() if slot.task == task.id}
        if wanted is not None:
            missing = [str(index) for index in wanted if index not in held]
            if missing:
                raise SwarmError(f"{task.id} holds no reservation on GPU {','.join(missing)}")
        chosen = wanted if wanted is not None else sorted(held)
        running = [i for i in chosen if held[i].state == "reserved" and held[i].kind == "run"]
        if running and wanted is not None:
            raise SwarmError(f"GPU {','.join(map(str, running))} runs a command for {task.id}; "
                             "it frees itself when that command exits")
        released = []
        for index in chosen:
            if index in running:
                continue
            gpu.reservation_file(index).unlink()
            gpu.log_event("release", index, task.id, by, held[index].kind, state=held[index].state)
            released.append(index)
        if running:
            print(f"swarm: GPU {','.join(map(str, running))} stays held by a running command", file=sys.stderr)
        if not released:
            gpu.note_on_task(task.id, by, None)
            print(f"{task.id}: nothing to release")
            return 0
        text = ",".join(str(index) for index in released)
        gpu.note_on_task(task.id, by, f"released GPUs {text}")
    print(f"{task.id}: released GPUs {text}")
    return 0


# --- worktrees --------------------------------------------------------------------------


def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *arguments], capture_output=True, text=True)


def git_ok(cwd: Path, *arguments: str) -> str:
    done = git(cwd, *arguments)
    if done.returncode:
        raise SwarmError(f"git {' '.join(arguments[:2])} in {cwd}: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def main_repo(given: str, values: Dict[str, Any]) -> Path:
    """The main checkout of a repo given as a path or as a folder name under the workspace."""
    candidate = Path(given).expanduser()
    if not candidate.exists():
        candidate = config.path("workspace", values) / given
    if not candidate.is_dir():
        raise SwarmError(f"no repo at {given} or {candidate}")
    common = git(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise SwarmError(f"{candidate} is not a git repo")
    return Path(common.stdout.strip()).parent.resolve()


def registry() -> Path:
    return config.data_dir() / "worktrees.json"


def load_registry() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(registry().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_registry(data: Dict[str, Dict[str, Any]]) -> None:
    registry().parent.mkdir(parents=True, exist_ok=True)
    write_atomic(registry(), json.dumps(data, indent=2) + "\n")


def record_touch(task_id: str, path: Path, branch: str, repo: Path, by: str) -> str:
    """Write the worktree into the task's Touches section; return the task id."""
    with tasks.locked():
        task = tasks.find_task(task_id)
        lines = task.sections.get("Touches", "").splitlines()
        entries = {"- Repo and branch:": f"{repo.name}, `{branch}`", "- Worktree:": f"`{path}`"}
        for prefix, value in entries.items():
            spots = [i for i, line in enumerate(lines) if line.startswith(prefix)]
            if spots and lines[spots[0]].strip() == prefix:
                lines[spots[0]] = f"{prefix} {value}"
            elif value not in "\n".join(lines):
                lines.insert(spots[-1] + 1 if spots else len(lines), f"{prefix} {value}")
        task.sections["Touches"] = "\n".join(lines)
        task.log(by, f"worktree {path} on branch {branch}")
        task.save()
        return task.id


def cmd_worktree_add(args: argparse.Namespace) -> int:
    values = config.load()
    repo = main_repo(args.repo, values)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", args.slug).strip("-.")
    if not slug:
        raise SwarmError("the slug needs letters or digits")
    task = tasks.find_task(args.task) if args.task else None
    branch = args.branch or slug
    path = worktree_base(values) / f"{repo.name}-wt" / slug
    if path.exists():
        raise SwarmError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.parent.resolve() / slug
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        if args.base:
            raise SwarmError(f"branch {branch} exists; drop --base or pass --branch with a new name")
        git_ok(repo, "worktree", "add", str(path), branch)
    else:
        git_ok(repo, "worktree", "add", "-b", branch, str(path), args.base or "HEAD")
    with file_lock(config.data_dir() / ".worktrees.lock"):
        data = load_registry()
        data[str(path)] = {"repo": str(repo), "slug": slug, "branch": branch,
                           "task": task.id if task else "", "created": now_stamp()}
        save_registry(data)
    if task:
        record_touch(task.id, path, branch, repo, args.by or task.meta.get("owner", "?"))
    print(path)
    print(f"branch {branch} in {repo}" + (f"; recorded in {task.id} Touches" if task else ""), file=sys.stderr)
    return 0


def worktrees_of(repo: Path) -> List[Dict[str, str]]:
    """`git worktree list --porcelain`, minus the main checkout."""
    found: List[Dict[str, str]] = []
    for block in git_ok(repo, "worktree", "list", "--porcelain").split("\n\n"):
        item: Dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            item[key] = value or "yes"
        if item.get("worktree"):
            found.append(item)
    return found[1:]


def known_worktrees(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every worktree under the worktree folders, plus every one swarm recorded, with its state."""
    data = load_registry()
    repos = {Path(entry["repo"]) for entry in data.values() if entry.get("repo")}
    base = worktree_base(values).resolve()
    for checkout in base.glob("*-wt/*/.git") if base.is_dir() else []:
        common = git(checkout.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode == 0:
            repos.add(Path(common.stdout.strip()).parent.resolve())
    rows = []
    seen = set()
    for repo in sorted(repos):
        if not (repo / ".git").exists():
            continue
        for item in worktrees_of(repo):
            path = Path(item["worktree"]).resolve()
            under = str(path).startswith(str(base) + os.sep)
            if str(path) not in data and not under:
                continue
            entry = data.get(str(path), {})
            seen.add(str(path))
            dirty = path.is_dir() and bool(git(path, "status", "--porcelain").stdout.strip())
            rows.append({"path": str(path), "repo": str(repo), "slug": entry.get("slug") or path.name,
                         "branch": item.get("branch", "").replace("refs/heads/", "") or "(detached)",
                         "task": entry.get("task", ""), "dirty": dirty,
                         "missing": not path.is_dir()})
    for path, entry in data.items():
        if path not in seen:
            rows.append({"path": path, "repo": entry.get("repo", ""), "slug": entry.get("slug", ""),
                         "branch": entry.get("branch", ""), "task": entry.get("task", ""), "dirty": False,
                         "missing": True})
    return rows


def cmd_worktree_list(args: argparse.Namespace) -> int:
    values = config.load()
    rows = known_worktrees(values)
    if args.repo:
        repo = main_repo(args.repo, values)
        rows = [row for row in rows if row["repo"] == str(repo)]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"no worktrees under {worktree_base(values)}")
        return 0
    for row in rows:
        flags = ", ".join(word for word, on in (("uncommitted changes", row["dirty"]),
                                                 ("missing", row["missing"])) if on)
        task = f"  task {row['task']}" if row["task"] else ""
        print(f"{row['slug']}  {Path(row['repo']).name}  {row['branch']}  {row['path']}{task}"
              + (f"  [{flags}]" if flags else ""))
    return 0


def cmd_worktree_remove(args: argparse.Namespace) -> int:
    values = config.load()
    rows = known_worktrees(values)
    hits = [row for row in rows if row["slug"] == args.slug or row["path"] == str(Path(args.slug).expanduser().resolve())]
    if args.repo:
        repo = str(main_repo(args.repo, values))
        hits = [row for row in hits if row["repo"] == repo]
    if not hits:
        raise SwarmError(f"no worktree {args.slug!r}; see `swarm worktree list`")
    if len(hits) > 1:
        raise SwarmError(f"{args.slug!r} names several worktrees ({', '.join(r['path'] for r in hits)}); "
                         "pass --repo")
    row = hits[0]
    path, repo = Path(row["path"]), Path(row["repo"])
    if path.is_dir():
        changes = git(path, "status", "--porcelain").stdout.strip()
        if changes and not args.force:
            raise SwarmError(f"{path} has uncommitted changes; commit them or pass --force:\n{changes}")
        git_ok(repo, "worktree", "remove", *(["--force"] if args.force else []), str(path))
    else:
        git(repo, "worktree", "prune")
    with file_lock(config.data_dir() / ".worktrees.lock"):
        data = load_registry()
        data.pop(str(path), None)
        save_registry(data)
    print(f"removed {path}; branch {row['branch']} kept (delete it with git -C {repo} branch -d {row['branch']})")
    return 0


# --- storage and disk -------------------------------------------------------------------


def storage_paths(values: Dict[str, Any]) -> List[Dict[str, str]]:
    """The places agents write to, in the order the rule lists them."""
    found = [
        {"name": "scratch", "key": "server.scratch_dir", "path": storage_dir("scratch_dir", values),
         "use": "scratch code, one-off scripts, logs, temp files ($TMPDIR)"},
        {"name": "checkpoints", "key": "server.ckpts_dir", "path": storage_dir("ckpts_dir", values),
         "use": "checkpoints, model weights, datasets, caches (HF_HOME, pip, torch)"},
        {"name": "worktrees", "key": "server.worktree_root", "path": worktree_base(values),
         "use": "git worktrees for workers: <root>/<repo>-wt/<slug>"},
        {"name": "workspace", "key": "workspace", "path": config.path("workspace", values),
         "use": "durable repos and projects"},
        {"name": "data", "key": "data_dir", "path": config.data_dir(values),
         "use": "swarm tasks, reservations, logs"},
    ]
    for extra in settings(values).get("disk_paths") or []:
        found.append({"name": Path(str(extra)).name or str(extra), "key": "server.disk_paths",
                      "path": Path(str(extra)).expanduser(), "use": "extra path to watch"})
    return [{**item, "path": str(item["path"])} for item in found if item["path"]]


def free_space(path: Path) -> Any:
    """Disk usage of the nearest existing folder at or above `path`."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe))
    except OSError:
        return None


def disk_status(values: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Free space for every storage path; `low` is True under server.disk_min_free_gb.

    Each item: name, key, path, exists, free_gb, total_gb, used_pct, min_free_gb, low.
    Paths on one filesystem share their numbers. Missing paths are measured at their nearest
    existing parent. Routine alerts call this; it never raises for a single bad path.
    """
    values = values if values is not None else config.load()
    try:
        minimum = float(settings(values).get("disk_min_free_gb") or 0)
    except (TypeError, ValueError):
        minimum = 0.0
    result = []
    seen = set()
    for item in storage_paths(values):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        usage = free_space(Path(item["path"]))
        row: Dict[str, Any] = {"name": item["name"], "key": item["key"], "path": item["path"],
                               "exists": Path(item["path"]).exists(), "min_free_gb": minimum,
                               "free_gb": None, "total_gb": None, "used_pct": None, "low": False}
        if usage:
            row["free_gb"] = round(usage.free / 1e9, 1)
            row["total_gb"] = round(usage.total / 1e9, 1)
            row["used_pct"] = round(100 * usage.used / usage.total, 1) if usage.total else None
            row["low"] = bool(minimum) and usage.free / 1e9 < minimum
        result.append(row)
    return result


def cmd_disk(args: argparse.Namespace) -> int:
    rows = disk_status()
    low = [row for row in rows if row["low"]]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if low else 0
    for row in rows:
        free = "?" if row["free_gb"] is None else f"{row['free_gb']:.1f} GB free of {row['total_gb']:.0f}"
        mark = "  LOW" if row["low"] else ""
        print(f"{row['name']:<12} {free:<26} {row['path']}{'' if row['exists'] else ' (not created yet)'}{mark}")
    if low:
        print(f"warning: {len(low)} path(s) under server.disk_min_free_gb = {low[0]['min_free_gb']:.0f} GB")
    return 1 if low else 0


RULE_FILE = KIT / "claude" / "rules" / "storage.md"


def storage_rule(values: Dict[str, Any]) -> str:
    keys = {"SCRATCH_DIR": str(storage_dir("scratch_dir", values)),
            "CKPTS_DIR": str(storage_dir("ckpts_dir", values)),
            "WORKTREE_ROOT": str(worktree_base(values)), "DATA_DIR": str(config.data_dir(values)),
            "WORKSPACE": str(config.path("workspace", values))}
    return fill(RULE_FILE.read_text(), keys)


def rule_path() -> Path:
    return config.data_dir() / "rules" / "storage.md"


def cmd_storage(args: argparse.Namespace) -> int:
    values = config.load()
    if args.rule:
        print(storage_rule(values), end="")
        return 0
    if args.write_rule:
        rule_path().parent.mkdir(parents=True, exist_ok=True)
        write_atomic(rule_path(), storage_rule(values))
        print(rule_path())
        return 0
    status = {row["path"]: row for row in disk_status(values)}
    for item in storage_paths(values):
        row = status.get(item["path"], {})
        free = "?" if row.get("free_gb") is None else f"{row['free_gb']:.1f} GB free"
        print(f"{item['name']:<12} {item['path']}  ({free}{'' if row.get('exists') else ', not created yet'})")
        print(f"{'':<12} {item['use']}  [{item['key']}]")
    print(f"rule for agents: {rule_path()}" + ("" if rule_path().exists() else " (not written; swarm storage --write-rule)"))
    return 0


# --- settings check ---------------------------------------------------------------------


def validate(values: Dict[str, Any]) -> List[str]:
    server = settings(values)
    problems = []
    for key in ("gpu_busy_mib", "reserve_max_hours", "disk_min_free_gb"):
        if key in server and not isinstance(server[key], (int, float)):
            problems.append(f"server.{key} must be a number")
    if not isinstance(server.get("gpu_locks", {}), dict):
        problems.append("server.gpu_locks must be an object of name -> lock file")
    if not isinstance(server.get("disk_paths", []), list):
        problems.append("server.disk_paths must be a list")
    name = str(server.get("gpu_backend") or gpu.NvidiaSmi.name)
    if name not in gpu.BACKENDS:
        problems.append(f"server.gpu_backend {name!r} is not one of: {', '.join(gpu.BACKENDS)}")
    return problems


config.VALIDATORS.append(validate)
tasks.CHECKS.append(gpu.check_reservations)


# --- parser -----------------------------------------------------------------------------


def add_gpu_choice(parser: argparse.ArgumentParser) -> None:
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--gpus", type=int, metavar="N", help="take the N lowest-index free GPUs")
    choice.add_argument("--gpu", metavar="LIST", help="take exactly these GPUs, for example 3,4")


def add_claim_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", required=True, metavar="ID", help="task the GPUs belong to (open/running/waiting)")
    add_gpu_choice(parser)
    parser.add_argument("--allow-busy", action="store_true", help="take GPUs busy with unreserved processes")
    parser.add_argument("--by", help="who claims; default the task owner")
    parser.add_argument("--pane", help="pane id to record; default $HERDR_PANE_ID or $TMUX_PANE")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("gpus", help="show GPUs, their reservations and the outside GPU locks")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_gpus)

    p = sub.add_parser("run", help="hold GPUs for a task while a command runs: swarm run --task ID --gpus 1 -- CMD")
    add_claim_options(p)
    p.add_argument("--wait", type=float, metavar="S", help=f"retry every {WAIT_POLL}s for S seconds instead of refusing")
    p.add_argument("command_args", nargs=argparse.REMAINDER, metavar="-- CMD")
    p.set_defaults(run=cmd_run)

    p = sub.add_parser("reserve", help="hold GPUs for a task for some hours, with no command")
    add_claim_options(p)
    p.add_argument("--hours", type=float, required=True, metavar="H", help="up to server.reserve_max_hours")
    p.set_defaults(run=cmd_reserve)

    p = sub.add_parser("release", help="give back a task's GPU reservations")
    p.add_argument("--task", required=True, metavar="ID")
    p.add_argument("--gpu", metavar="LIST", help="only these GPUs")
    p.add_argument("--by")
    p.set_defaults(run=cmd_release)

    p = sub.add_parser("worktree", help="git worktrees for workers under server.worktree_root")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True
    q = actions.add_parser("add", help="create <root>/<repo>-wt/<slug> on a new branch <slug>; prints its path")
    q.add_argument("repo", help="repo path, or a folder name under the workspace")
    q.add_argument("slug")
    q.add_argument("--task", metavar="ID", help="record the worktree in this task's Touches section")
    q.add_argument("--base", metavar="REF", help="start the new branch here (default HEAD of the repo)")
    q.add_argument("--branch", help="branch name (default the slug); an existing branch is checked out")
    q.add_argument("--by")
    q.set_defaults(run=cmd_worktree_add)
    q = actions.add_parser("list", help="worktrees under the root and those swarm created")
    q.add_argument("repo", nargs="?")
    q.add_argument("--json", action="store_true")
    q.set_defaults(run=cmd_worktree_list)
    q = actions.add_parser("remove", help="remove a worktree (refuses with uncommitted changes); keeps its branch")
    q.add_argument("slug", help="slug or path")
    q.add_argument("--repo", help="pick the repo when several have this slug")
    q.add_argument("--force", action="store_true", help="remove even with uncommitted changes")
    q.set_defaults(run=cmd_worktree_remove)

    p = sub.add_parser("storage", help="where scratch, checkpoints, caches and worktrees go; free space")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--rule", action="store_true", help="print the storage rule for agents, filled in")
    mode.add_argument("--write-rule", action="store_true", help="write it to <data_dir>/rules/storage.md")
    p.set_defaults(run=cmd_storage)

    p = sub.add_parser("disk", help="free space per configured path; exit 1 under server.disk_min_free_gb")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_disk)
