"""The night runner: a queue of unattended prompts or tasks, each run by a fresh headless agent session
inside a time window, with a morning report.

    swarm night run "<prompt>" [--verify CMD] [--cwd DIR] [--task ID]   queue one job, start the runner
    swarm night run-file jobs.json                                    queue every job in a file
    swarm night status | logs | stop | report

The runner (`swarm night serve`, started in the background by run and run-file) waits for
`routine.night.window_start`, then runs queued jobs one at a time. No job starts after
`window_end`; one that is running finishes. Each job is a fresh session of the agent CLI in headless
mode (Claude Code: `claude -p --output-format json`). With `verify`, the runner runs that shell
command after each turn and, while it fails, resumes the same session with the failure output, up to
`max_attempts` turns. When the queue is empty or the window ends it writes
<data_dir>/night/reports/<night>.md (copied to <data_dir>/night/report.md) and tells the overall manager.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import agent, background, config, herdr, managers, tasks
from .messaging import say
from .util import SwarmError, file_lock, now_stamp, one_line, write_atomic

NIGHT_NOTE = ("\n\nYou run unattended overnight: nobody can answer a question before morning, so do not ask; "
              "decide, or stop and write the question down.")


def settings(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    found = config.section("routine", values if values is not None else config.load()).get("night")
    return {**config.DEFAULTS["routine"]["night"], **(found if isinstance(found, dict) else {})}


def root() -> Path:
    path = config.data_dir() / "night"
    (path / "reports").mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return background.logs_dir() / "night.log"


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with open(log_path(), "a") as handle:
        handle.write(line + "\n")


# --- the queue --------------------------------------------------------------------------------


@contextlib.contextmanager
def queue():
    """The job list, locked; changes are saved on exit."""
    path = root() / "queue.json"
    with file_lock(background.logs_dir() / ".night-queue.lock"):
        jobs: List[Dict[str, Any]] = []
        with contextlib.suppress(OSError, ValueError):
            jobs = list(json.loads(path.read_text()))
        yield jobs
        write_atomic(path, json.dumps(jobs, indent=1) + "\n")


def read_queue() -> List[Dict[str, Any]]:
    with queue() as jobs:
        return [dict(job) for job in jobs]


def make_job(spec: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    night = settings(values)
    if not spec.get("prompt") and not spec.get("task"):
        raise SwarmError("a night job needs a prompt or a task")
    job: Dict[str, Any] = {
        "prompt": str(spec.get("prompt") or ""), "task": str(spec.get("task") or ""),
        "cwd": str(Path(str(spec.get("cwd") or night["cwd"] or config.get("workspace", values))).expanduser()),
        "model": str(spec.get("model") or night["model"] or agent.model_for("worker", values)),
        "verify": str(spec.get("verify") or ""),
        "max_attempts": int(spec.get("max_attempts") or night["max_attempts"]),
        "minutes": float(spec.get("minutes") or night["job_minutes"]),
        "title": one_line(spec.get("title") or spec.get("prompt") or spec.get("task"))[:70],
        "status": "queued",
    }
    if job["task"]:
        task = tasks.find_task(job["task"])
        job["task"], job["title"] = task.id, str(spec.get("title") or task.meta.get("title") or task.id)
        if not spec.get("cwd") and task.meta.get("owner"):
            with contextlib.suppress(SwarmError):
                owner = str(task.meta["owner"])
                job["cwd"] = managers.placeholders(owner, managers.manager_entry(owner, values), values)["CWD"]
    if not job["verify"]:
        job["max_attempts"] = 1
    return job


def job_prompt(job: Dict[str, Any], values: Dict[str, Any]) -> str:
    if not job.get("task"):
        return job["prompt"] + NIGHT_NOTE
    task = tasks.find_task(job["task"])
    text = managers.WORKER_PROMPT.format(
        worker=f"night-{job['id']}", manager=task.meta.get("owner", "?"), task_md=task.folder / "task.md",
        result_tpl=config.data_dir(values) / "templates" / "result.md")
    return text + NIGHT_NOTE + (f" Extra instructions: {job['prompt']}" if job.get("prompt") else "")


def command(job: Dict[str, Any], prompt: str, session: Optional[str], values: Dict[str, Any]) -> List[str]:
    """The headless agent command line for one turn."""
    night, agent_settings = settings(values), agent.settings(values)
    words = [str(agent_settings.get("cmd") or "claude"), *map(str, night["headless_args"])]
    if agent_settings.get("model_flag") and job.get("model"):
        words += [str(agent_settings["model_flag"]), job["model"]]
    words += [str(word) for word in night["extra_args"]]
    if session and night.get("resume_flag"):
        words += [str(night["resume_flag"]), session]
    return words + [prompt]


# --- running jobs -------------------------------------------------------------------------------


def clock(text: str):
    return datetime.strptime(str(text).strip(), "%H:%M").time()


def in_window(now: datetime, values: Dict[str, Any]) -> bool:
    night = settings(values)
    start, end, at = clock(night["window_start"]), clock(night["window_end"]), now.time()
    return start <= at < end if start < end else (at >= start or at < end)


def night_of(now: datetime, values: Dict[str, Any]) -> str:
    """The date a night is filed under: the evening it started."""
    end = clock(settings(values)["window_end"])
    return (now - timedelta(days=1) if now.time() < end else now).strftime("%Y-%m-%d")


def parse_output(text: str) -> Dict[str, Any]:
    """Claude Code `--output-format json` prints one object with result, session_id, is_error."""
    with contextlib.suppress(ValueError):
        found = json.loads(text.strip().splitlines()[-1] if text.strip() else "")
        if isinstance(found, dict):
            return found
    return {"result": text.strip()}


def run_turn(argv: List[str], cwd: str, minutes: float, job_id: str) -> Dict[str, Any]:
    started = time.time()
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        return {"ok": False, "result": f"could not start {argv[0]}: {error}", "seconds": 0}
    with queue() as jobs:
        for job in jobs:
            if job["id"] == job_id:
                job["pid"] = process.pid
    try:
        stdout, stderr = process.communicate(timeout=minutes * 60)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate()
        return {"ok": False, "result": f"timed out after {minutes:g} min", "seconds": time.time() - started}
    parsed = parse_output(stdout)
    ok = process.returncode == 0 and not parsed.get("is_error")
    result = str(parsed.get("result") or "")
    if process.returncode:
        result = f"exit {process.returncode}. {result}\n{stderr.strip()[-1500:]}".strip()
    return {"ok": ok, "result": result, "session_id": parsed.get("session_id"), "seconds": time.time() - started,
            "cost_usd": parsed.get("total_cost_usd")}


def run_verify(command_text: str, cwd: str) -> Dict[str, Any]:
    done = subprocess.run(command_text, shell=True, cwd=cwd, capture_output=True, text=True)
    return {"code": done.returncode, "output": (done.stdout + "\n" + done.stderr).strip()[-3000:]}


def run_job(job_id: str, values: Dict[str, Any]) -> None:
    with queue() as jobs:
        job = next(item for item in jobs if item["id"] == job_id)
        job.update(status="running", started=now_stamp(), attempts=[])
        job = dict(job)
    log(f"{job_id}: start ({job['title']}) in {job['cwd']} with {job['model'] or 'default model'}")
    prompt, session, status = job_prompt(job, values), None, "failed"
    attempts: List[Dict[str, Any]] = []
    if not Path(job["cwd"]).is_dir():
        attempts.append({"ok": False, "result": f"no folder {job['cwd']}", "seconds": 0})
    for number in range(1, job["max_attempts"] + 1 if Path(job["cwd"]).is_dir() else 1):
        turn = run_turn(command(job, prompt, session, values), job["cwd"], job["minutes"], job_id)
        session = turn.get("session_id") or session
        attempts.append(turn)
        log(f"{job_id}: turn {number} {'ok' if turn['ok'] else 'failed'} in {turn['seconds']:.0f}s")
        if not turn["ok"]:
            break
        if not job["verify"]:
            status = "done"
            break
        check = run_verify(job["verify"], job["cwd"])
        turn["verify"] = check
        log(f"{job_id}: verify exit {check['code']}")
        if check["code"] == 0:
            status = "done"
            break
        status = "verify-failed"
        prompt = (f"The check `{job['verify']}` failed with exit {check['code']}. Output:\n{check['output']}\n\n"
                  "Find the cause, fix it, and make the check pass.")
    with queue() as jobs:
        for item in jobs:
            if item["id"] == job_id and item.get("status") == "running":
                item.update(status=status, finished=now_stamp(), attempts=attempts, session_id=session)
                item.pop("pid", None)
    log(f"{job_id}: {status}")


def git_summary(cwd: str) -> str:
    done = subprocess.run(["git", "-C", cwd, "status", "--short"], capture_output=True, text=True)
    if done.returncode:
        return ""
    stat = subprocess.run(["git", "-C", cwd, "diff", "--stat"], capture_output=True, text=True).stdout.strip()
    return (done.stdout.strip() + ("\n" + stat if stat else "")).strip() or "clean"


def write_report(night: str, values: Dict[str, Any]) -> Path:
    jobs = [job for job in read_queue() if job.get("night") == night]
    counts: Dict[str, int] = {}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    resume = str(settings(values).get("resume_flag") or "--resume")
    lines = [f"# Night report {night}", "", f"Written {now_stamp()}. Jobs: "
             + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"), ""]
    for job in jobs:
        attempts = job.get("attempts") or []
        seconds = sum(float(turn.get("seconds") or 0) for turn in attempts)
        lines += [f"## {job['id']}: {job['status']} · {job['title']}", "",
                  f"- Folder: `{job['cwd']}`; model `{job.get('model') or '-'}`; turns {len(attempts)}; {seconds / 60:.0f} min",
                  f"- Started {job.get('started') or '-'}, finished {job.get('finished') or '-'}"]
        if job.get("task"):
            lines.append(f"- Task `{job['task']}`; result.md: "
                         + ("written" if tasks.find_task(job["task"]).result.exists() else "not written"))
        if job.get("verify"):
            last = next((turn["verify"] for turn in reversed(attempts) if turn.get("verify")), None)
            lines.append(f"- Check `{job['verify']}`: " + (f"exit {last['code']}" if last else "not run"))
        if job.get("session_id"):
            lines.append(f"- Resume: `cd {shlex.quote(job['cwd'])} && {agent.settings(values).get('cmd')} {resume} {job['session_id']}`")
        changes = git_summary(job["cwd"]) if Path(job["cwd"]).is_dir() else ""
        if changes:
            lines += ["", "Working tree:", "```", changes[:2000], "```"]
        final = str(attempts[-1].get("result") if attempts else "")
        if final:
            lines += ["", "Last reply:", "", "> " + final[:2000].replace("\n", "\n> ")]
        lines.append("")
    path = root() / "reports" / f"{night}.md"
    write_atomic(path, "\n".join(lines))
    write_atomic(root() / "report.md", "\n".join(lines))
    return path


def tell_morning(path: Path, values: Dict[str, Any]) -> None:
    target = str(settings(values).get("report_to") or config.section("swarm", values).get("overall") or "")
    herdr.notify("Night report ready", str(path), "done")
    if target and target != "none":
        with contextlib.suppress(SwarmError):
            if herdr.agent_named(target):
                say(target, f"swarm night: the night report is at {path}. Read it and follow up on failed jobs.",
                    wait_minutes=180)


def serve(now_mode: bool) -> None:
    with background.single_instance("night"):
        log(f"night runner started (pid {os.getpid()}){' ignoring the window' if now_mode else ''}")
        ran: Optional[str] = None
        while True:
            values = config.load()
            now = datetime.now()
            queued = [job for job in read_queue() if job["status"] == "queued"]
            open_window = now_mode or in_window(now, values)
            if ran and (not queued or not open_window):
                path = write_report(ran, values)
                log(f"report written: {path}")
                tell_morning(path, values)
                ran = None
            if not queued:
                log("queue empty; night runner exits")
                return
            if not open_window:
                time.sleep(60)
                continue
            night = night_of(now, values)
            with queue() as jobs:
                for job in jobs:
                    if job["id"] == queued[0]["id"]:
                        job["night"] = night
            ran = night
            try:
                run_job(queued[0]["id"], values)
            except Exception as error:  # one broken job must not end the night
                log(f"{queued[0]['id']}: runner error {error!r}")
                with queue() as jobs:
                    for job in jobs:
                        if job["id"] == queued[0]["id"]:
                            job.update(status="failed", finished=now_stamp(), error=repr(error))


# --- commands -------------------------------------------------------------------------------------


def load_file(path: Path) -> List[Dict[str, Any]]:
    """A .json file: a list of jobs or {"jobs": [...]}. Any other file: its text is one prompt."""
    if not path.is_file():
        raise SwarmError(f"no file {path}")
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text())
        except ValueError as error:
            raise SwarmError(f"{path}: {error}")
        items = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise SwarmError(f"{path} must hold a list of job objects or {{\"jobs\": [...]}}")
        return items
    return [{"prompt": path.read_text().strip(), "title": path.stem}]


def enqueue(specs: List[Dict[str, Any]], args: argparse.Namespace) -> int:
    values = config.load()
    night = settings(values)
    jobs = [make_job(spec, values) for spec in specs]
    window = "now (--now)" if args.now else f"{night['window_start']}-{night['window_end']}"
    print(f"Night run plan: {len(jobs)} job(s), window {window}, backend {night['backend']}, "
          f"report to {night['report_to'] or config.section('swarm', values).get('overall')}")
    for index, job in enumerate(jobs, 1):
        preview = command(job, "<prompt>", None, values)
        print(f"  {index}. {job['title']}\n     folder {job['cwd']}; up to {job['max_attempts']} turn(s) of "
              f"{job['minutes']:g} min; check: {job['verify'] or 'none'}\n     runs: {' '.join(map(shlex.quote, preview))}")
        if args.dry_run:
            print("     prompt: " + one_line(job_prompt({**job, "id": f"n{index}"}, values))[:300])
    if args.dry_run:
        print("Dry run: nothing queued, runner not started.")
        return 0
    with queue() as existing:
        stamp = datetime.now().strftime("%m%d")
        for job in jobs:
            number = 1 + sum(1 for item in existing if str(item.get("id", "")).startswith(f"n{stamp}-"))
            job.update(id=f"n{stamp}-{number}", added=now_stamp())
            existing.append(job)
    print("Queued: " + ", ".join(job["id"] for job in jobs))
    if args.no_start:
        print("Runner not started (--no-start); `swarm night run-file` or `swarm night serve` starts it.")
    elif args.foreground:
        serve(args.now)
    else:
        extra = ["--now"] if args.now else []
        print(background.start(night["session"], night["backend"], "night", "serve", *extra))
    return 0


def cmd_night(args: argparse.Namespace) -> int:
    values = config.load()
    night = settings(values)
    if args.action == "run":
        if not args.prompt and not args.task:
            raise SwarmError("night run needs a prompt or --task")
        return enqueue([{"prompt": args.prompt or "", "task": args.task, "cwd": args.cwd, "model": args.model,
                         "verify": args.verify, "max_attempts": args.max_attempts, "minutes": args.minutes}], args)
    if args.action == "run-file":
        if not args.prompt:
            raise SwarmError("night run-file needs a file")
        specs = load_file(Path(args.prompt).expanduser())
        for spec in specs:
            for key in ("cwd", "model", "verify", "max_attempts", "minutes"):
                if getattr(args, key) and not spec.get(key):
                    spec[key] = getattr(args, key)
        return enqueue(specs, args)
    if args.action == "serve":
        serve(args.now)
    elif args.action == "status":
        found = background.find_running(night["session"])
        print(f"night runner: {'running, ' + found.where(night['session']) if found else 'stopped'}; "
              f"window {night['window_start']}-{night['window_end']} "
              f"({'open' if in_window(datetime.now(), values) else 'closed'} now)")
        jobs = read_queue()
        for job in jobs[-args.lines:]:
            print(f"  {job['id']:<10} {job['status']:<13} {job['title'][:60]}"
                  + (f"  (night {job['night']})" if job.get("night") else ""))
        if not jobs:
            print("  queue empty")
    elif args.action == "logs":
        return background.tail(log_path(), args.lines, args.follow)
    elif args.action == "stop":
        print(background.stop(night["session"]))
        with queue() as jobs:
            for job in jobs:
                if job.get("status") == "running":
                    with contextlib.suppress(OSError, TypeError):
                        os.killpg(int(job.get("pid")), signal.SIGTERM)
                    job.update(status="stopped", finished=now_stamp())
                    job.pop("pid", None)
                    print(f"stopped job {job['id']}")
                elif job.get("status") == "queued" and args.drop_queued:
                    job["status"] = "dropped"
                    print(f"dropped queued job {job['id']}")
    elif args.action == "report":
        path = root() / "reports" / f"{args.date}.md" if args.date else root() / "report.md"
        if not path.exists():
            print(f"No night report at {path}.")
            return 1
        print(path.read_text().rstrip())
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("night", help="queue unattended overnight prompts or tasks, with a morning report",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["run", "run-file", "status", "logs", "stop", "report", "serve"])
    p.add_argument("prompt", nargs="?", help="run: the prompt; run-file: the jobs file (.json list, or text = one prompt)")
    p.add_argument("--task", help="run: carry out this task folder's task.md (writes result.md)")
    p.add_argument("--cwd", help="folder the agent runs in (default routine.night.cwd, else workspace)")
    p.add_argument("--model")
    p.add_argument("--verify", help="shell check after each turn; exit 0 means done")
    p.add_argument("--max-attempts", type=int, help="turns while the check fails (default routine.night.max_attempts)")
    p.add_argument("--minutes", type=float, help="time limit per turn (default routine.night.job_minutes)")
    p.add_argument("--now", action="store_true", help="ignore the time window and start at once")
    p.add_argument("--dry-run", action="store_true", help="print the plan; queue nothing")
    p.add_argument("--no-start", action="store_true", help="queue only; do not start the runner")
    p.add_argument("--foreground", action="store_true", help="run the runner here instead of in the background")
    p.add_argument("--date", help="report: the night (YYYY-MM-DD) to show; default the latest")
    p.add_argument("--drop-queued", action="store_true", help="stop: also drop jobs not started yet")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(run=cmd_night)
