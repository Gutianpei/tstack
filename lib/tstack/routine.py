"""The routine: an always-on loop that keeps managers fresh and herdr up to date. Also the pilot log.

    swarm routine start|stop|status|logs     run the loop in the background (tmux or nohup)
    swarm routine loop                       run the loop in the foreground
    swarm routine once [--dry-run]           one pass now; --dry-run prints what it would do
    swarm routine check                      each manager's state, context use and what is due
    swarm pilot-log show|add                 the daily pilot log, one row per day

Each pass (every `interval_seconds`):
- At `morning_restart` it restarts every manager, the overall manager last, then asks the overall
  manager to add today's pilot-log row. A loop first started after that time waits for tomorrow.
- Every `check_every_minutes` it restarts any manager whose context use is at
  `context_restart_percent` or more, sends due `scheduled_prompts`, and checks the disk.
- A due restart waits until the manager is idle, no question dialog is open, and its screen has not
  changed for `quiet_minutes`, so it never cuts into a conversation. The restart itself is
  `swarm restart <name>` (managers.cmd_restart): handoff, exit, fresh session in the same pane.
- With `herdr_notices` on, it shows task pop-ups (consumer `herdr` in the shared
  <data_dir>/state/notices.json, also used by `swarm list --watch`, so a task pops up once) and
  labels each manager's pane and workspace in the sidebar. With `slack.notices` = routine it also
  posts the Slack task notices (slack.post_notices).
State: <data_dir>/routine-state.json. Log: <data_dir>/logs/routine.log.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import agent, background, config, herdr, managers, tasks
from .messaging import say
from .util import SwarmError, file_lock, today, write_atomic

MORNING = "the morning restart"
LABEL_SOURCE = "swarm"
CONTEXT_SOURCES = ("auto", "screen", "transcript")


# --- settings, state, log ----------------------------------------------------------------


def settings(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return config.section("routine", values if values is not None else config.load())


def session_name(values: Optional[Dict[str, Any]] = None) -> str:
    return str(settings(values).get("session") or "tstack-routine")


def log_path() -> Path:
    return background.logs_dir() / "routine.log"


def state_path() -> Path:
    return config.data_dir() / "routine-state.json"


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with open(log_path(), "a") as handle:
        handle.write(line + "\n")


def load_state() -> Dict[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        found = json.loads(state_path().read_text())
        if isinstance(found, dict):
            return found
    return {}


def save_state(state: Dict[str, Any]) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    write_atomic(state_path(), json.dumps(state, indent=1, sort_keys=True) + "\n")


def clock(text: str):
    return datetime.strptime(str(text).strip(), "%H:%M").time()


def manager_names(values: Dict[str, Any]) -> List[str]:
    """Every manager, the overall manager last, so the morning's closing message reaches its fresh session."""
    overall = str(config.section("swarm", values).get("overall") or "overall")
    return [name for name in tasks.owners(values) if name != overall] + [overall]


def overall_name(values: Dict[str, Any]) -> str:
    return str(config.section("swarm", values).get("overall") or "overall")


# --- reading a manager: question dialog, context use, quiet time ---------------------------


def question_open(found: Dict[str, Any], screen: str, values: Dict[str, Any]) -> bool:
    """True when the agent waits on an approval or question dialog (herdr says blocked, or the screen shows one)."""
    if found.get("agent_status") == "blocked":
        return True
    pattern = settings(values).get("question_regex")
    bottom = "\n".join(screen.splitlines()[-15:])
    return bool(pattern) and re.search(str(pattern), bottom) is not None


def transcript_slug(folder: str) -> str:
    """Claude Code keeps a session's transcript under <transcript_dir>/<cwd with every non-alphanumeric as ->/."""
    return re.sub(r"[^A-Za-z0-9]", "-", folder)


def _session_name(path: Path) -> Optional[str]:
    """The `-n` name a Claude Code transcript records near its top, or None."""
    with contextlib.suppress(OSError):
        with open(path, errors="replace") as handle:
            for _, line in zip(range(40), handle):
                if '"agent-name"' in line or '"custom-title"' in line:
                    with contextlib.suppress(ValueError):
                        item = json.loads(line)
                        return str(item.get("agentName") or item.get("customTitle") or "") or None
    return None


def _last_usage(path: Path) -> Optional[int]:
    """Tokens in context at the last main-thread reply: input + cache read + cache creation."""
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 512 * 1024))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            item = json.loads(line)
            if item.get("type") != "assistant" or item.get("isSidechain"):
                continue
            usage = item["message"]["usage"]
            return sum(int(usage.get(key) or 0) for key in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return None


def transcript_context(name: str, values: Dict[str, Any]) -> Optional[float]:
    """Context use from the newest Claude Code transcript started with `-n <name>`, or None."""
    routine = settings(values)
    root = Path(str(routine.get("transcript_dir") or "~/.claude/projects")).expanduser()
    window = float(routine.get("context_window_tokens") or 0)
    if not root.is_dir() or window <= 0:
        return None
    folders: List[Path] = []
    with contextlib.suppress(SwarmError):
        cwd = managers.placeholders(name, managers.manager_entry(name, values), values)["CWD"]
        folders = [folder for folder in [root / transcript_slug(cwd)] if folder.is_dir()]
    # The manager's folder first; when it is not there (another start folder), every project folder.
    folders = folders or [folder for folder in root.iterdir() if folder.is_dir()]
    recent = time.time() - 7 * 86400
    files = sorted((file for folder in folders for file in folder.glob("*.jsonl")
                    if file.stat().st_mtime >= recent), key=lambda file: file.stat().st_mtime, reverse=True)
    for file in files[:200]:
        if _session_name(file) == name:
            used = _last_usage(file)
            return None if used is None else round(100 * used / window, 1)
    return None


def context_percent(name: str, screen: str, values: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """(percent of context in use, where it came from). The screen wins when it says; then the transcript."""
    source = str(settings(values).get("context_source") or "auto")
    if source in ("auto", "screen"):
        used = agent.context_used(screen, values)
        if used is not None or source == "screen":
            return used, "screen"
    with contextlib.suppress(OSError):
        return transcript_context(name, values), "transcript"
    return None, "transcript"


def observe_screen(state: Dict[str, Any], name: str, screen: str, now: float) -> float:
    """Record the screen's hash; return how many seconds it has been unchanged (0 when first seen)."""
    screens = state.setdefault("screens", {})
    digest = hashlib.sha1(screen.encode("utf-8", "replace")).hexdigest()
    seen = screens.get(name)
    if not seen or seen.get("hash") != digest:
        screens[name] = {"hash": digest, "since": now}
        return 0.0
    return now - float(seen.get("since") or now)


# --- one pass ---------------------------------------------------------------------------------


def missing(name: str, found: Optional[Dict[str, Any]], values: Dict[str, Any]) -> Optional[str]:
    try:
        node = managers.manager_entry(name, values).get("node") or name
    except SwarmError as error:
        return str(error)
    folder = config.path("memory_dir", values) / "nodes" / str(node)
    if not folder.is_dir():
        return f"no memory node {folder}"
    if found is None:
        return "no herdr agent"
    return None


def run_restart(name: str) -> str:
    """managers.cmd_restart, with its printed lines folded into one result line."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            managers.cmd_restart(argparse.Namespace(agent=name, handoff_written=False))
    except SwarmError as error:
        return f"{name}: not restarted ({error})"
    except Exception as error:  # a broken restart must not stop the loop
        return f"{name}: restart failed ({error!r})"
    return out.getvalue().strip().splitlines()[-1] if out.getvalue().strip() else f"{name}: restarted"


def tell(name: str, message: str, values: Dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        print(f"  would tell {name}: {message}")
        return
    try:
        if not say(name, message, wait_minutes=float(settings(values).get("say_wait_minutes") or 10)):
            log(f"{name} stayed busy; not sent: {message}")
    except SwarmError as error:
        log(f"could not reach {name}: {error}")


def one_pass(state: Dict[str, Any], dry_run: bool = False, force_morning: bool = False,
             force_check: bool = False) -> List[str]:
    """Run one pass of the routine on `state` (saved by the caller). Returns the lines worth logging."""
    values = config.load()
    routine = settings(values)
    names = manager_names(values)
    now = datetime.now()
    stamp = time.time()
    out: List[str] = []
    say_line = (lambda text: print("  " + text)) if dry_run else out.append

    live = {str(item.get("name")): item for item in herdr.agents() if item.get("name")}
    screens = {name: herdr.screen(name) if name in live else "" for name in names}
    quiet = {name: observe_screen(state, name, screens[name], stamp) for name in names if name in live}

    # Which restarts become due.
    due: Dict[str, str] = state.setdefault("due", {})
    morning_at = str(routine.get("morning_restart") or "")
    if morning_at and "morning" not in state and now.time() >= clock(morning_at):
        state["morning"] = today()  # first start after the morning time: wait for tomorrow
    morning_due = force_morning or (morning_at and now.time() >= clock(morning_at) and state.get("morning") != today())
    check_due = force_check or stamp - float(state.get("last_check") or 0) >= float(routine.get("check_every_minutes") or 10) * 60
    skipped = {name: why for name in names if (why := missing(name, live.get(name), values))}
    if morning_due:
        state["morning"] = today()
        state["morning_results"] = []
        for name in names:
            if name not in skipped:
                due[name] = MORNING
        if skipped:
            say_line("morning restart skips: " + "; ".join(f"{n} ({w})" for n, w in skipped.items()))
    if check_due:
        state["last_check"] = stamp
        limit = float(routine.get("context_restart_percent") or 0)
        for name in names:
            if name in skipped or limit <= 0:
                continue
            used, source = context_percent(name, screens[name], values)
            if used is not None and used >= limit:
                due.setdefault(name, f"context {used:g}% (from {source}) is at least {limit:g}%")
    for name in [name for name in due if name not in names]:
        say_line(f"{name}: no longer a manager; dropped its pending restart ({due.pop(name)})")

    # Restart each due manager that is quiet; the rest wait for a later pass.
    quiet_seconds = float(routine.get("quiet_minutes") or 0) * 60
    waiting = state.setdefault("postponed", {})
    for name in names:
        if name not in due:
            continue
        reason, found = due[name], live.get(name)
        if found is None:
            say_line(f"{name}: not running, restart skipped ({reason})")
            due.pop(name)
            continue
        others_first = name == overall_name(values) and reason == MORNING and any(
            why == MORNING for other, why in due.items() if other != name)
        idle = agent.is_idle(found.get("agent_status"), values)
        if others_first:
            kind, hold = "order", "waits for the other managers' morning restarts"
        elif question_open(found, screens[name], values):
            kind, hold = "question", "a question dialog is open"
        elif not idle:
            kind, hold = "busy", f"busy ({found.get('agent_status')})"
        elif quiet.get(name, 0) < quiet_seconds:
            kind, hold = "quiet", (f"screen changed {quiet.get(name, 0) / 60:.0f} min ago; "
                                   f"needs {quiet_seconds / 60:g} min quiet")
        else:
            kind, hold = "", ""
        if hold:
            if dry_run:
                print(f"  {name}: restart due ({reason}), waiting: {hold}")
            elif waiting.get(name) != kind:  # log each kind of wait once, not every pass
                out.append(f"{name}: restart due ({reason}), waiting: {hold}")
                waiting[name] = kind
            continue
        if dry_run:
            print(f"  {name}: WOULD RESTART now ({reason})")
            if reason == MORNING:
                state.setdefault("morning_results", []).append(f"{name}: would restart")
            due.pop(name)
            continue
        result = run_restart(name)
        out.append(f"{result} [{reason}]")
        if reason == MORNING:
            state.setdefault("morning_results", []).append(result)
        due.pop(name)
        waiting.pop(name, None)
        state.get("screens", {}).pop(name, None)

    if state.get("morning_results") is not None and not any(why == MORNING for why in due.values()):
        results = state.pop("morning_results")
        if results or morning_due:
            message = ("swarm routine: morning restarts done (" + ("; ".join(results) or "none") + ").")
            if routine.get("pilot_log", True):
                message += (f" Add today's row to {pilot_path()}: swarm pilot-log add --transfers N "
                            "--status-turns X/Y --surprises N --questions 'N (M unneeded)' --time '...' --notes '...'")
            tell(overall_name(values), message, values, dry_run)

    if check_due:
        for step in (scheduled_prompts, disk_alert):
            try:
                step(state, values, now, dry_run, say_line)
            except Exception as error:  # a broken schedule must not stop the restarts
                say_line(f"{step.__name__} failed: {error!r}")
    if dry_run:
        owner = config.section("slack", values).get("notices") == "routine"
        slack_line = "posted by the routine" if owner else "left to the bridge (slack.notices)"
        print(f"  notices: herdr pop-ups and labels {'on' if routine.get('herdr_notices', True) else 'off'}; "
              f"Slack notices {slack_line}")
    else:
        notices(state, values, out)
    state["last_pass"] = datetime.now().isoformat(timespec="seconds")
    return out


# --- scheduled prompts, disk alert, herdr notices ------------------------------------------------


def scheduled_due(item: Dict[str, Any], last: Optional[str], now: datetime) -> bool:
    """Due on start_date and every every_days days after, once the clock passes `after`, once that day."""
    start = date.fromisoformat(str(item.get("start_date") or now.date().isoformat()))
    every = max(int(item.get("every_days") or 1), 1)
    day = now.date()
    if day < start or (day - start).days % every or now.time() < clock(item.get("after") or "00:00"):
        return False
    return last != day.isoformat()


def scheduled_prompts(state: Dict[str, Any], values: Dict[str, Any], now: datetime, dry_run: bool, note) -> None:
    sent = state.setdefault("scheduled", {})
    for index, item in enumerate(settings(values).get("scheduled_prompts") or []):
        try:
            key = "|".join(str(item.get(k, "")) for k in ("agent", "start_date", "every_days", "after", "message"))
            if not scheduled_due(item, sent.get(key), now):
                continue
            target, message = str(item["agent"]), str(item["message"])
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            note(f"scheduled prompt {index + 1} is malformed, skipped: {error!r}")
            continue
        if dry_run:
            print(f"  scheduled prompt {index + 1} due: would tell {target}: {message}")
            continue
        sent[key] = now.date().isoformat()  # record first, so a busy pane is not asked again every check
        tell(target, message, values, False)
        note(f"{target}: scheduled prompt {index + 1} sent")


def disk_status(values: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """server.disk_status(cfg) -> list of dicts when the server module exists, else None."""
    try:
        server = importlib.import_module(f"{__package__}.server")
    except ModuleNotFoundError as error:
        if error.name and error.name.endswith(".server"):
            return None
        raise
    function = getattr(server, "disk_status", None)
    return list(function(values) or []) if callable(function) else None


def low_disk(entry: Any) -> Optional[str]:
    """A one-line problem for a disk_status entry that is under its line, else None."""
    if not isinstance(entry, dict):
        return None
    status = str(entry.get("status") or "").lower()
    if not (entry.get("ok") is False or entry.get("low") or entry.get("warn") or status in ("low", "warn", "warning", "alert")):
        return None
    if entry.get("message"):
        return str(entry["message"])
    free = entry.get("free_gb")
    if free is None and entry.get("free_bytes") is not None:
        free = float(entry["free_bytes"]) / 1e9
    line = f"{entry.get('path', '?')}: {float(free):.0f} GB free" if free is not None else str(entry.get("path", "?"))
    return line + (f" (line {entry['min_free_gb']} GB)" if entry.get("min_free_gb") is not None else "")


def disk_alert(state: Dict[str, Any], values: Dict[str, Any], now: datetime, dry_run: bool, note) -> None:
    alert = settings(values).get("disk_alert") or {}
    if not alert or not alert.get("enabled", True):
        return
    entries = disk_status(values)
    if entries is None:
        if dry_run:
            print("  disk alert: no server module with disk_status; skipped")
        return
    problems = [problem for problem in map(low_disk, entries) if problem]
    if dry_run:
        print(f"  disk alert: {'; '.join(problems) if problems else 'all paths above their line'}")
        return
    every = float(alert.get("every_hours") or 24) * 3600
    if not problems or time.time() - float(state.get("disk_alert_at") or 0) < every:
        return
    state["disk_alert_at"] = time.time()
    target = str(alert.get("agent") or overall_name(values))
    message = "swarm routine: low disk: " + "; ".join(problems) + ". Free some space (swarm disk for details)."
    herdr.notify("Low disk", "; ".join(problems)[:120], "request")
    tell(target, message, values, False)
    note(f"disk alert sent to {target}: {'; '.join(problems)}")


ACTIVE_ORDER = ("waiting-on-you", "running", "open")
COUNT_WORDS = {"waiting-on-you": "waiting", "running": "running", "open": "open"}


def sidebar_labels(loaded: List[tasks.Task]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(most urgent task per owner, counts per owner), for example `ingest-logs +2` and `1 waiting · 2 running`."""
    by_owner: Dict[str, List[tasks.Task]] = {}
    for task in loaded:
        if task.status in ACTIVE_ORDER:
            by_owner.setdefault(str(task.meta.get("owner")), []).append(task)
    lines, counts = {}, {}
    for owner, items in by_owner.items():
        items.sort(key=lambda task: (ACTIVE_ORDER.index(task.status), task.meta.get("updated", "")))
        more = f" +{len(items) - 1}" if len(items) > 1 else ""
        lines[owner] = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", items[0].id)[:30] + more
        counts[owner] = " · ".join(f"{sum(t.status == s for t in items)} {COUNT_WORDS[s]}"
                                   for s in ACTIVE_ORDER if any(t.status == s for t in items))
    return lines, counts


def notices(state: Dict[str, Any], values: Dict[str, Any], out: List[str]) -> None:
    """Task notices. herdr pop-ups are consumer `herdr` in the shared <data_dir>/state/notices.json
    (tasks.popups, also used by `swarm list --watch`); sidebar labels are written only on change; with
    slack.notices = routine, Slack notices are posted here too (slack.post_notices, consumer `slack`)."""
    errors = state.setdefault("notice_errors", {})
    steps = []
    if settings(values).get("herdr_notices", True):
        steps += [("pop-ups", tasks.popups), ("labels", lambda: label_sidebar(state, values))]
    slack_settings = config.section("slack", values)
    if slack_settings.get("notices") == "routine" and slack_settings.get("channel"):
        steps.append(("slack notices", lambda: slack_notices(str(slack_settings["channel"]))))
    for step, action in steps:
        try:
            action()
            errors.pop(step, None)
        except Exception as error:  # herdr or Slack down, or a broken task file, must not stop the loop
            if errors.get(step) != repr(error):
                out.append(f"{step} failed, trying again next pass: {error!r}")
            errors[step] = repr(error)


def slack_notices(channel: str) -> int:
    slack = importlib.import_module(f"{__package__}.slack")
    return slack.post_notices(slack.transport(), channel)


def label_sidebar(state: Dict[str, Any], values: Dict[str, Any]) -> int:
    lines, counts = sidebar_labels(tasks.load_tasks())
    owners = set(tasks.owners(values))
    wanted: List[Tuple[str, str, str, str]] = []
    for item in herdr.agents():
        if item.get("name") in owners and item.get("pane_id"):
            wanted.append(("pane", str(item["pane_id"]), "task", lines.get(str(item["name"]), "")))
    for item in herdr.call("workspace", "list").get("workspaces") or []:
        if item.get("label") in owners:
            wanted.append(("workspace", str(item["workspace_id"]), "tasks", counts.get(str(item["label"]), "")))
    shown = state.setdefault("labels", {})
    written = 0
    for kind, target, token, value in wanted:
        key = f"{kind}:{target}:{token}"
        if shown.get(key) == value:
            continue
        change = ["--token", f"{token}={value}"] if value else ["--clear-token", token]
        extra = ["--agent", agent.kind(values)] if kind == "pane" else []
        herdr.call(kind, "report-metadata", target, "--source", LABEL_SOURCE, *extra, *change)
        shown[key] = value
        written += 1
    return written


# --- the loop and commands ------------------------------------------------------------------------


def loop() -> None:
    with background.single_instance("routine"):
        log(f"routine loop started (pid {os.getpid()})")
        while True:
            interval = 60.0
            try:
                interval = float(settings().get("interval_seconds") or 60)
                with file_lock(background.logs_dir() / ".routine-state.lock"):
                    state = load_state()
                    lines = one_pass(state)
                    save_state(state)
                for line in lines:
                    log(line)
            except Exception as error:  # the loop must outlive any one bad pass
                log(f"pass failed, trying again next pass: {error!r}")
            time.sleep(max(interval, 5))


def cmd_routine(args: argparse.Namespace) -> int:
    values = config.load()
    name = session_name(values)
    if args.action == "start":
        print(background.start(name, str(settings(values).get("backend") or "auto"), "routine", "loop"))
        print(f"Log: {log_path()}")
    elif args.action == "stop":
        print(background.stop(name))
    elif args.action == "status":
        found = background.find_running(name)
        print(f"routine: {'running, ' + found.where(name) if found else 'stopped'}")
        state = load_state()
        print(f"last pass: {state.get('last_pass') or 'never'}; morning restart done for: "
              f"{state.get('morning') or 'never'}; pending restarts: "
              f"{', '.join(f'{k} ({v})' for k, v in (state.get('due') or {}).items()) or 'none'}")
        background.tail(log_path(), 10)
    elif args.action == "logs":
        return background.tail(log_path(), args.lines, args.follow)
    elif args.action == "loop":
        loop()
    elif args.action == "check":
        return cmd_check(values)
    elif args.action == "once":
        with file_lock(background.logs_dir() / ".routine-state.lock"):
            state = load_state()
            work = copy.deepcopy(state) if args.dry_run else state
            if args.dry_run:
                print(f"Dry run of one routine pass at {datetime.now():%H:%M} (nothing sent, state not saved):")
            lines = one_pass(work, args.dry_run, args.morning, True)
            if not args.dry_run:
                save_state(state)
        for line in lines:
            log(line)
    return 0


def cmd_check(values: Dict[str, Any]) -> int:
    """Read-only: each manager's herdr state, context use, question dialog, quiet time, pending restart."""
    routine = settings(values)
    problems = validate(values)
    for problem in problems:
        print(f"problem: {problem}")
    state = load_state()
    live = {str(item.get("name")): item for item in herdr.agents() if item.get("name")}
    print(f"{'manager':<14} {'status':<9} {'context':<18} {'quiet':<8} note")
    for name in manager_names(values):
        found = live.get(name)
        why = missing(name, found, values)
        if found is None:
            print(f"{name:<14} {'-':<9} {'-':<18} {'-':<8} {why}")
            continue
        screen = herdr.screen(name)
        used, source = context_percent(name, screen, values)
        seen = (state.get("screens") or {}).get(name) or {}
        same = seen.get("hash") == hashlib.sha1(screen.encode("utf-8", "replace")).hexdigest()
        quiet = f"{(time.time() - float(seen['since'])) / 60:.0f}m" if same and seen.get("since") else "0m"
        notes = [why] if why else []
        if question_open(found, screen, values):
            notes.append("question dialog open")
        if used is not None and used >= float(routine.get("context_restart_percent") or 101):
            notes.append("context over the line")
        if name in (state.get("due") or {}):
            notes.append(f"restart pending: {state['due'][name]}")
        context = f"{used:g}% ({source})" if used is not None else f"unknown ({source})"
        print(f"{name:<14} {str(found.get('agent_status')):<9} {context:<18} {quiet:<8} {'; '.join(notes)}")
    print(f"morning restart at {routine.get('morning_restart') or 'off'}, context line "
          f"{routine.get('context_restart_percent')}%, quiet {routine.get('quiet_minutes')} min")
    return 1 if problems else 0


def validate(values: Dict[str, Any]) -> List[str]:
    routine = settings(values)
    problems = []
    for key in ("morning_restart",):
        if routine.get(key):
            try:
                clock(routine[key])
            except ValueError:
                problems.append(f"routine.{key} must be HH:MM or empty")
    for key in ("context_restart_percent", "quiet_minutes", "check_every_minutes", "interval_seconds"):
        if not isinstance(routine.get(key, 0), (int, float)):
            problems.append(f"routine.{key} must be a number")
    backends = ("auto", *background.BACKENDS)
    for key, value in (("backend", routine.get("backend")), ("night.backend", (routine.get("night") or {}).get("backend"))):
        if (value or "auto") not in backends:
            problems.append(f"routine.{key} must be one of {', '.join(backends)}")
    if routine.get("context_source", "auto") not in CONTEXT_SOURCES:
        problems.append(f"routine.context_source must be one of {', '.join(CONTEXT_SOURCES)}")
    if not isinstance(routine.get("scheduled_prompts", []), list):
        problems.append("routine.scheduled_prompts must be a list")
    for index, item in enumerate(routine.get("scheduled_prompts") or []):
        if not isinstance(item, dict) or not item.get("agent") or not item.get("message"):
            problems.append(f"routine.scheduled_prompts[{index}] needs agent and message")
            continue
        try:
            date.fromisoformat(str(item.get("start_date") or "2000-01-01"))
            clock(item.get("after") or "00:00")
        except ValueError:
            problems.append(f"routine.scheduled_prompts[{index}]: start_date YYYY-MM-DD, after HH:MM")
    night = routine.get("night") or {}
    for key in ("window_start", "window_end"):
        try:
            clock(night.get(key) or "00:00")
        except ValueError:
            problems.append(f"routine.night.{key} must be HH:MM")
    return problems


config.VALIDATORS.append(validate)


# --- pilot log ------------------------------------------------------------------------------------

PILOT_COLUMNS = ["Date", "Context passed by hand", "Status-check turns", "Surprises",
                 "Questions to you (unneeded)", "Your time vs output", "Notes"]
PILOT_HEADER = """# Pilot log

One row per day. After the morning restart the routine asks the overall manager to add today's row
with `swarm pilot-log add`. The five measures:

1. Context passed by hand: times you carried context between agents yourself. Target 0.
2. Status-check turns: your "what's the status?" turns out of all your turns. Target under 1 in 10.
3. Surprises: collisions (GPUs, checkouts, ports, storage), unnoticed job failures, false "done"
   reports. Target 0.
4. Questions to you: decisions that reached you, and how many of them were not needed.
5. Your time vs output: less time supervising without less work done; a short note.

"""


def pilot_path(values: Optional[Dict[str, Any]] = None) -> Path:
    return config.data_dir(values) / "pilot-log.md"


def pilot_rows(text: str) -> List[List[str]]:
    rows = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            rows.append(cells)
    return rows


def cmd_pilot_log(args: argparse.Namespace) -> int:
    path = pilot_path()
    table = "| " + " | ".join(PILOT_COLUMNS) + " |\n|" + "---|" * len(PILOT_COLUMNS) + "\n"
    if args.action == "show":
        if not path.exists():
            print(f"No pilot log yet at {path}; `swarm pilot-log add` starts it.")
            return 0
        if args.last:
            print(table + "\n".join("| " + " | ".join(row) + " |" for row in pilot_rows(path.read_text())[-args.last:]))
        else:
            print(path.read_text().rstrip())
        return 0
    day = args.date or today()
    date.fromisoformat(day)
    cells = [day, args.transfers, args.status_turns, args.surprises, args.questions, args.time, args.notes]
    row = "| " + " | ".join((cell or "-").replace("|", "/").replace("\n", " ") for cell in cells) + " |"
    with file_lock(background.logs_dir() / ".pilot-log.lock"):
        text = path.read_text() if path.exists() else PILOT_HEADER + table
        lines = text.rstrip("\n").splitlines()
        existing = [index for index, line in enumerate(lines) if line.startswith(f"| {day} |")]
        if existing and not args.replace:
            raise SwarmError(f"{path} already has a row for {day}; pass --replace to rewrite it")
        if existing:
            lines[existing[0]] = row
        else:  # keep rows in date order, so a late row for an earlier day lands in its place
            later = [index for index, line in enumerate(lines) if re.match(r"\| (\d{4}-\d{2}-\d{2}) \|", line)
                     and line[2:12] > day]
            lines.insert(later[0] if later else len(lines), row)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, "\n".join(lines) + "\n")
    print(f"{'Replaced' if existing else 'Added'} the {day} row in {path}:\n{row}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("routine", help="the always-on loop: morning and context restarts, scheduled prompts, notices",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["start", "stop", "status", "logs", "check", "once", "loop"])
    p.add_argument("--dry-run", action="store_true", help="once: print what the pass would do, change nothing")
    p.add_argument("--morning", action="store_true", help="once: treat the morning restart as due now")
    p.add_argument("-n", "--lines", type=int, default=40, help="logs: lines to show")
    p.add_argument("-f", "--follow", action="store_true", help="logs: keep following")
    p.set_defaults(run=cmd_routine)

    p = sub.add_parser("pilot-log", help="show the daily pilot log or add today's row")
    p.add_argument("action", nargs="?", choices=["show", "add"], default="show")
    p.add_argument("--last", type=int, default=0, help="show: only the last N rows")
    p.add_argument("--date", help="add: YYYY-MM-DD (default today)")
    p.add_argument("--transfers", default="", help="times context was passed between agents by hand")
    p.add_argument("--status-turns", default="", help="status-check turns out of all turns, e.g. 2/31")
    p.add_argument("--surprises", default="", help="collisions, unnoticed failures, false done reports")
    p.add_argument("--questions", default="", help="questions that reached the user, e.g. '4 (1 unneeded)'")
    p.add_argument("--time", default="", help="user time vs output, a short note")
    p.add_argument("--notes", default="")
    p.add_argument("--replace", action="store_true", help="add: rewrite an existing row for that date")
    p.set_defaults(run=cmd_pilot_log)
