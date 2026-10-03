"""Managers (long-lived agents, one herdr workspace each) and the workers they start."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from . import agent, config, herdr
from .messaging import say
from .tasks import ACTIVE, Task, find_task, load_tasks, locked, owners
from .util import KIT, SWARM_BIN, SwarmError, fill, today

MANAGER_TAB, WORKERS_TAB, TASKS_TAB = "manager", "workers", "tasks"

WORKER_PROMPT = (
    "You are {worker}, a worker for swarm manager {manager}. Read and carry out {task_md} exactly. "
    "Stay inside its Scope. When finished, write result.md in that task folder once, using the sections "
    "of {result_tpl}, then stop. If you hit a question only the user can answer, write it under Open issues "
    "in result.md and stop; your manager will ask the user."
)


def manager_entry(name: str, values: Dict[str, Any]) -> Dict[str, Any]:
    swarm = config.section("swarm", values)
    if name == swarm.get("overall"):
        return {"role": "overall", "node": name}
    entry = (swarm.get("managers") or {}).get(name)
    if entry is None:
        raise SwarmError(f"{name} is not a manager; known: {', '.join(owners(values))}")
    return entry


def placeholders(name: str, entry: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, str]:
    nodes = entry.get("nodes") or []
    return {
        "NAME": name, "NODE": str(entry.get("node") or name), "USER": str(values["user"]),
        "NODES": ", ".join(f"`{node}`" for node in nodes) or "(none yet)",
        "OVERALL": str(config.section("swarm", values).get("overall") or "overall"), "KIT_DIR": str(KIT),
        "MEMORY_DIR": str(config.path("memory_dir", values)), "DATA_DIR": str(config.data_dir(values)),
        "CONFIG": str(config.config_path()),
        "CWD": str(Path(str(entry.get("cwd") or values["workspace"])).expanduser()),
        "DATE": today(),
    }


def first_prompt(name: str, values: Dict[str, Any]) -> str:
    entry = manager_entry(name, values)
    role_dir = KIT / "roles" / ("overall" if entry["role"] == "overall" else "domain")
    return fill((role_dir / "first-prompt.txt").read_text().strip(), placeholders(name, entry, values))


def git(repo: Path, *arguments: str, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, **kwargs)


def cmd_add_manager(args: argparse.Namespace) -> int:
    values = config.load()
    swarm = config.section("swarm", values)
    name, role = args.name, args.role
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", name):
        raise SwarmError("a manager name is lowercase letters, digits, - or _, starting with a letter")
    entry: Dict[str, Any] = {"role": role, "node": args.node or name}
    if role == "domain":
        entry["nodes"] = [item for item in (args.nodes or "").split(",") if item]
        if args.cwd:
            entry["cwd"] = str(Path(args.cwd).expanduser())
    memory = config.path("memory_dir", values)
    node_dir = memory / "nodes" / entry["node"]
    keys = placeholders(name, entry, values)
    role_dir = KIT / "roles" / role
    plan = []
    if not node_dir.exists():
        plan.append(f"create memory node {node_dir}")
    current = (swarm.get("managers") or {}).get(name)
    if role == "overall" and swarm.get("overall") != name:
        plan.append(f"set swarm.overall = {name} in {config.config_path()}")
    if role == "domain" and current != entry:
        if current:
            raise SwarmError(f"{name} already exists with {current}; remove-manager first to change it")
        plan.append(f"add swarm.managers.{name} = {json.dumps(entry)} to {config.config_path()}")
    handoff = config.data_dir(values) / "handoffs" / f"{name}.md"
    if not handoff.exists():
        plan.append(f"write first handoff {handoff}")
    if not args.no_start:
        plan.append(f"create herdr workspace '{name}' (tabs: manager, workers{', tasks' if role == 'overall' else ''}) "
                    f"and start {agent.kind(values)} ({agent.model_for('manager', values)}) as agent '{name}'")
    print("Plan:\n" + "\n".join(f"  {index}. {line}" for index, line in enumerate(plan, 1)) if plan else "Nothing to do.")
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0
    if not node_dir.exists():
        node_dir.mkdir(parents=True)
        for file in ("AGENT.md", "journal.md", "HISTORY.md"):
            source = role_dir / file if (role_dir / file).exists() else KIT / "roles" / "common" / file
            (node_dir / file).write_text(fill(source.read_text(), keys))
        summary = "overall swarm manager" if role == "overall" else f"swarm manager for {keys['NODES']}"
        with (memory / "README.md").open("a") as handle:
            handle.write(f"| `{entry['node']}` | role node | {summary} |\n")
        git(memory, "add", "--", f"nodes/{entry['node']}", "README.md")
        done = git(memory, "commit", "-q", "-m", f"{entry['node']}: created by swarm add-manager",
                   "--", f"nodes/{entry['node']}", "README.md", env={**os.environ, "TSTACK_MULTI": "1"})
        if done.returncode:
            print(f"warning: memory commit failed: {done.stderr.strip() or done.stdout.strip()}")

    def change(raw: Dict[str, Any]) -> None:
        section = raw.setdefault("swarm", {})
        if role == "overall":
            section["overall"] = name
        else:
            section.setdefault("managers", {})[name] = entry

    values = config.update(change)
    handoff.parent.mkdir(parents=True, exist_ok=True)
    if not handoff.exists():
        handoff.write_text(fill((role_dir / "handoff.md").read_text(), keys))
    if not args.no_start:
        ensure_manager(name, values)
    print(f"Done. Manager '{name}' is ready.")
    return 0


def cmd_remove_manager(args: argparse.Namespace) -> int:
    values = config.load()
    if args.name not in (config.section("swarm", values).get("managers") or {}):
        raise SwarmError(f"{args.name} is not a domain manager")
    still = [task.id for task in load_tasks() if task.meta.get("owner") == args.name and task.status in ACTIVE]
    if args.dry_run:
        print(f"Would remove swarm.managers.{args.name} from {config.config_path()}. Memory node and handoff stay.")
    else:
        config.update(lambda raw: raw.get("swarm", {}).get("managers", {}).pop(args.name, None))
        print(f"Removed {args.name}. Its memory node and handoff stay. Close its herdr workspace by hand.")
    if still:
        print(f"warning: active tasks still owned by {args.name}: {', '.join(still)}")
    return 0


def ensure_manager(name: str, values: Dict[str, Any]) -> List[str]:
    """Create what is missing for one manager: workspace, tabs, the agent. Return what was done."""
    entry = manager_entry(name, values)
    cwd = placeholders(name, entry, values)["CWD"]
    done: List[str] = []
    workspace = herdr.workspace_named(name)
    if workspace is None:
        created = herdr.call("workspace", "create", "--label", name, "--cwd", cwd, "--no-focus", *herdr.env_args())
        workspace_id = str(created["workspace"]["workspace_id"])
        herdr.call("tab", "rename", str(created["tab"]["tab_id"]), MANAGER_TAB)
        done.append(f"workspace {name}")
    else:
        workspace_id = str(workspace["workspace_id"])
    wanted = [MANAGER_TAB, WORKERS_TAB] + ([TASKS_TAB] if entry["role"] == "overall" else [])
    for label in wanted:
        if herdr.tab_named(workspace_id, label) is None:
            created = herdr.call("tab", "create", "--workspace", workspace_id, "--label", label, "--cwd", cwd,
                                 "--no-focus", *herdr.env_args())
            done.append(f"tab {name}/{label}")
            if label == TASKS_TAB:
                pane = str(created["root_pane"]["pane_id"])
                herdr.wait_free_shell(pane)
                herdr.run_in_pane(pane, f"{shlex.quote(str(SWARM_BIN))} list --watch 30")
    if herdr.agent_named(name) is None:
        manager_tab = herdr.tab_named(workspace_id, MANAGER_TAB)
        panes = herdr.tab_panes(workspace_id, str(manager_tab["tab_id"])) if manager_tab else []
        free = [str(pane["pane_id"]) for pane in panes if not pane.get("agent") and herdr.free_shell(str(pane["pane_id"]))]
        if not free:
            raise SwarmError(f"no free shell pane in {name}/{MANAGER_TAB}; open one and rerun `swarm up`")
        herdr.start_agent(name, free[0], agent.model_for("manager", values), first_prompt(name, values))
        done.append(f"agent {name}")
    return done


def cmd_up(args: argparse.Namespace) -> int:
    values = config.load()
    overall = config.section("swarm", values).get("overall")
    for name in owners(values):
        if name == overall and not (config.path("memory_dir", values) / "nodes" / name).exists():
            print(f"{name}: no overall manager yet; run swarm add-manager {name} --role overall")
            continue
        done = ensure_manager(name, values)
        print(f"{name}: {'created ' + ', '.join(done) if done else 'all present'}")
    return 0


def cmd_start_worker(args: argparse.Namespace) -> int:
    values = config.load()
    swarm = config.section("swarm", values)
    manager = args.manager
    entry = manager_entry(manager, values)
    task = find_task(args.task)
    if task.status not in ACTIVE:
        raise SwarmError(f"{task.id} is {task.status}; only open, running or waiting tasks get workers")
    if manager != swarm.get("overall") and task.meta.get("owner") != manager:
        raise SwarmError(f"{task.id} belongs to {task.meta.get('owner')}, not {manager}")
    live = {str(item.get("name")) for item in herdr.agents() if item.get("name")}
    mentioned = " ".join((t.folder / "task.md").read_text() for t in load_tasks())
    number = 1
    while f"{manager}-w{number}" in live or re.search(rf"\b{manager}-w{number}\b", mentioned):
        number += 1
    worker = f"{manager}-w{number}"
    model = args.model or agent.model_for("worker", values)
    cwd = str(Path(args.cwd).expanduser()) if args.cwd else placeholders(manager, entry, values)["CWD"]
    prompt = (Path(args.prompt_file).read_text().strip() if args.prompt_file else WORKER_PROMPT.format(
        worker=worker, manager=manager, task_md=task.folder / "task.md",
        result_tpl=config.data_dir(values) / "templates" / "result.md"))
    workspace = herdr.workspace_named(manager)
    if workspace is None:
        raise SwarmError(f"no herdr workspace named {manager}; run swarm up")
    workspace_id = str(workspace["workspace_id"])
    tab = herdr.tab_named(workspace_id, WORKERS_TAB)
    print(f"Worker {worker} on {model} for {task.id}, cwd {cwd}")
    if args.dry_run:
        print("Dry run: nothing started.")
        return 0
    pane = None
    if tab is None:
        created = herdr.call("tab", "create", "--workspace", workspace_id, "--label", WORKERS_TAB, "--cwd", cwd,
                             "--no-focus", *herdr.env_args())
        pane = str(created["root_pane"]["pane_id"])
    else:
        panes = herdr.tab_panes(workspace_id, str(tab["tab_id"]))
        for item in panes:
            if not item.get("agent") and herdr.free_shell(str(item["pane_id"])):
                pane = str(item["pane_id"])
                herdr.run_in_pane(pane, f"cd {shlex.quote(cwd)}")
                break
        if pane is None:
            limit = int(swarm.get("max_workers") or 4)
            if len(panes) >= limit:
                raise SwarmError(f"{manager} already has {len(panes)} worker panes (swarm.max_workers={limit}). "
                                 f"Quit a finished worker first: swarm say <worker> {agent.exit_command(values)}")
            # 2x2 grid: second goes right of the first, third under the first, fourth under the second.
            ids = [str(item["pane_id"]) for item in panes]
            target, direction = (ids[0], "right") if len(ids) == 1 else (ids[(len(ids) - 2) % len(ids)], "down")
            pane = str(herdr.call("pane", "split", target, "--direction", direction, "--cwd", cwd, "--no-focus",
                                  *herdr.env_args())["pane"]["pane_id"])
    herdr.start_agent(worker, pane, model, prompt)
    with contextlib.suppress(SwarmError):
        herdr.call("pane", "report-metadata", pane, "--source", "swarm", "--agent", agent.kind(values),
                   "--token", f"task={task.meta.get('title', task.id)[:40]}")
    with locked():
        task = find_task(task.id)
        task.meta["worker"] = worker
        task.log(manager, f"started worker {worker} ({model}) in pane {pane}")
        task.save()
    spawn_waiter(worker, manager, task)
    print(f"Started {worker} in pane {pane}; it will wake {manager} when it finishes.")
    if task.status == "open":
        print(f"Next: swarm set {task.id} running --by {manager}")
    return 0


def spawn_waiter(worker: str, manager: str, task: Task) -> None:
    log = config.data_dir() / "logs" / f"wake-{worker}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as handle:
        subprocess.Popen([sys.executable, str(SWARM_BIN), "wake", worker, "--to", manager,
                          "--task", task.id, "--note", task.meta.get("title", task.id)],
                         stdout=handle, stderr=handle, stdin=subprocess.DEVNULL, start_new_session=True)


def cmd_restart(args: argparse.Namespace) -> int:
    """Ask a manager to write its handoff, quit its session, and start a fresh one in the same pane."""
    values = config.load()
    name = args.agent
    manager_entry(name, values)
    handoff = config.data_dir(values) / "handoffs" / f"{name}.md"
    found = herdr.agent_named(name)
    if found is None:
        raise SwarmError(f"no live agent named {name}; use swarm up to start it")
    if not args.handoff_written:
        before = handoff.stat().st_mtime if handoff.exists() else 0
        if not say(name, f"swarm: restart due. Write {handoff} (live state, running jobs, open questions, "
                         f"next step), update and commit your memory node, then reply 'handoff written'."):
            raise SwarmError(f"{name} stayed busy; not restarted")
        deadline = time.time() + 45 * 60
        while time.time() < deadline:
            fresh = handoff.exists() and handoff.stat().st_mtime > before
            if fresh and agent.is_idle((herdr.agent_named(name) or {}).get("agent_status"), values):
                break
            time.sleep(15)
        else:
            raise SwarmError(f"{name} did not write {handoff} in 45 min; not restarted")
    else:
        time.sleep(20)
        herdr.call("agent", "wait", name, "--timeout", str(15 * 60 * 1000), timeout=15 * 60 + 30)
    pane = str(found["pane_id"])
    herdr.prompt(name, agent.exit_command(values))
    if not herdr.wait_free_shell(pane, 60):
        raise SwarmError(f"{name} did not exit; pane {pane} left as it is")
    herdr.start_agent(name, pane, agent.model_for("manager", values), first_prompt(name, values))
    print(f"{name} restarted in pane {pane}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("add-manager", help="create a manager: memory node, settings entry, handoff, herdr workspace")
    p.add_argument("name")
    p.add_argument("--role", choices=["overall", "domain"], default="domain")
    p.add_argument("--node", help="memory node to resume from (default: the name)")
    p.add_argument("--nodes", help="comma-separated project nodes this manager owns")
    p.add_argument("--cwd", help="folder the manager and its workers start in (default: workspace)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.set_defaults(run=cmd_add_manager)

    p = sub.add_parser("remove-manager", help="take a manager out of the settings (keeps its notes)")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(run=cmd_remove_manager)

    sub.add_parser("up", help="after a herdr restart: recreate missing workspaces and managers").set_defaults(run=cmd_up)

    p = sub.add_parser("start-worker", help="start a worker for a task in the manager's workers tab")
    p.add_argument("manager")
    p.add_argument("--task", required=True)
    p.add_argument("--model")
    p.add_argument("--cwd", help="folder the worker starts in, for example its git worktree")
    p.add_argument("--prompt-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(run=cmd_start_worker)

    p = sub.add_parser("restart", help="ask for a handoff, quit the session, start a fresh one")
    p.add_argument("agent")
    p.add_argument("--handoff-written", action="store_true")
    p.set_defaults(run=cmd_restart)
