"""Slack bridge: one Slack channel (or DM) talks to the swarm, and task notices come back.

    swarm slack start | stop | status | logs | test     run the bridge in the background
    swarm slack run [--once]                             the poll loop in the foreground
    swarm slack post TEXT [--task ID | --thread TS]      post as the bot (agents reply this way)

Messages from `slack.allowed_users` in `slack.channel` become prompts to the overall manager
(`@name text` picks a manager); a few words are commands (`help`, `status`, `list`, `tasks
<owner>`, `screen <agent>`). Each task that becomes waiting, done or dropped gets one thread; a
reply in a waiting task's thread goes to the task's owner as the answer. The bot token comes from
$SLACK_BOT_TOKEN or `slack.token_file`, never from the settings file. See docs/slack.md.

The chat backend sits behind `Transport`; `SlackWebAPI` is the default (plain Web API over urllib,
polling). Another chat tool or Slack Socket Mode only needs a class with the same five methods.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, herdr, messaging, tasks
from .util import SWARM_BIN, SwarmError, file_lock, log_stamp, one_line, write_atomic

TMUX_SESSION = "tstack-slack"
NOTICE_STATUSES = ("waiting-on-you", "done", "dropped")
NOTICE_DAYS = 7
MAX_POST = 3500
HELP = """tstack bridge commands (top-level messages in this channel):
  help                 this list
  status               agents and their state, task counts per owner
  list [all]           the task list (`all` adds earlier done and dropped tasks)
  tasks <owner>        tasks of one owner
  screen <agent>       the visible screen of an agent (secrets masked)
  @<manager> <text>    send <text> to that manager
  anything else        goes to the overall manager
Reply in a task's thread to answer its question; the reply goes to the task's owner."""

# Outgoing text passes through these; matches become [masked]. Not a guarantee, a safety net.
SECRET_PATTERNS = [
    r"xox[abposr]-[A-Za-z0-9-]{10,}",
    r"xapp-[A-Za-z0-9-]{10,}",
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    r"sk-[A-Za-z0-9_-]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"glpat-[A-Za-z0-9_-]{20,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)\S+",
]


def mask(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        if "(\\s*[=:]\\s*)" in pattern:
            text = re.sub(pattern, lambda m: f"{m.group(1)}{m.group(2)}[masked]", text)
        else:
            text = re.sub(pattern, "[masked]", text)
    return text


# --- settings and files -----------------------------------------------------------------


def settings() -> Dict[str, Any]:
    return config.section("slack")


def state_dir() -> Path:
    return config.data_dir() / "state"


def log_file() -> Path:
    return config.data_dir() / "logs" / "slack.log"


def pid_file() -> Path:
    return state_dir() / "slack.pid"


def token(required: bool = True) -> Optional[str]:
    """$SLACK_BOT_TOKEN, else the first line of slack.token_file."""
    value = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if value:
        return value
    name = str(settings().get("token_file") or "")
    file = Path(name).expanduser() if name else None
    if file and file.is_file():
        if file.stat().st_mode & 0o077:
            log(f"warning: {file} is readable by others; run chmod 600 {file}")
        value = (file.read_text().strip().splitlines() or [""])[0].strip()
        if value:
            return value
    if required:
        where = f"or put it in {file}" if file else "or set slack.token_file"
        raise SwarmError(f"no Slack bot token: export SLACK_BOT_TOKEN=xoxb-... {where} (see docs/slack.md)")
    return None


def log(text: str) -> None:
    print(f"{log_stamp()} {text}", flush=True)


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(data, indent=1) + "\n")


# --- shared notice state: <data_dir>/state/notices.json (format in docs/slack.md) ---------


def notices_path() -> Path:
    return state_dir() / "notices.json"


def notice_key(task: tasks.Task) -> str:
    """What makes a notice new: a task that waits again is new, a second verdict is not."""
    if task.status == "waiting-on-you":
        return f"waiting-on-you@{task.meta.get('waiting_since', '')}"
    return task.status


def notice_text(task: tasks.Task) -> str:
    owner, title = task.meta.get("owner", "?"), task.meta.get("title", task.id)
    if task.status == "waiting-on-you":
        question = task.sections.get(tasks.QUESTION, "").strip()
        return f"❓ @{owner} needs you: {title}\n{question}\n_Reply in this thread to answer._ ({task.id})"
    if task.status == "done":
        verdicts = [line[2:] for line in task.sections.get("Verdicts", "").splitlines() if line.startswith("- ")]
        text = f"✅ done: {title} (@{owner} · {task.id})"
        if verdicts:
            text += f"\nverdict: {verdicts[-1].replace(' ([result](result.md))', '')}"
        if task.result.exists():
            summary = result_summary(task.result)
            if summary:
                text += f"\n{summary}"
            text += f"\nresult: {task.result}"
        return text
    reason = re.findall(r"\(reason: (.*)\)\s*$", task.sections.get("Log", ""), re.M)
    return f"🗑 dropped: {title} (@{owner} · {task.id})" + (f"\nreason: {reason[-1]}" if reason else "")


def result_summary(path: Path) -> str:
    text = path.read_text(errors="replace")
    match = re.search(r"^## Summary\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    summary = (match.group(1) if match else "").strip()
    return summary[:600] + ("…" if len(summary) > 600 else "")


def due_notices(seen: Optional[Dict[str, str]], all_tasks: List[tasks.Task]) -> List[tasks.Task]:
    """Tasks whose current notice key differs from `seen`. Recent ones only (NOTICE_DAYS)."""
    cutoff = time.time() - NOTICE_DAYS * 86400
    due = []
    for task in all_tasks:
        if task.status not in NOTICE_STATUSES:
            continue
        if task.status != "waiting-on-you":
            try:
                if datetime.fromisoformat(task.meta.get("updated", "")).timestamp() < cutoff:
                    continue
            except ValueError:
                continue
        if seen is not None and seen.get(task.id) == notice_key(task):
            continue
        due.append(task)
    return due


def post_notices(transport: "Transport", channel: str, consumer: str = "slack") -> int:
    """Post one notice per task that became waiting, done or dropped; later notices of a task go in
    its thread. Callable from other modules (the routine daemon) when they own notices. Returns posts made."""
    posted = 0
    with file_lock(notices_path().with_suffix(".lock")):
        state = load_json(notices_path(), {})
        state.setdefault("version", 1)
        seen_all = state.setdefault("seen", {})
        threads = state.setdefault("threads", {})
        first = consumer not in seen_all
        seen = seen_all.setdefault(consumer, {})
        for task in due_notices(None if first else seen, tasks.load_tasks()):
            if first and task.status != "waiting-on-you":
                seen[task.id] = notice_key(task)  # already finished before the bridge first ran
                continue
            thread = threads.get(task.id) or {}
            parent = thread.get("ts") if thread.get("channel") == channel else None
            try:
                ts = transport.post(channel, mask(notice_text(task)), thread_ts=parent, broadcast=bool(parent))
            except SwarmError as error:
                log(f"notice for {task.id} not posted (retry next cycle): {error}")
                continue
            if not parent:
                threads[task.id] = {"channel": channel, "ts": ts}
            seen[task.id] = notice_key(task)
            posted += 1
            log(f"notice: {task.id} {task.status}")
        known = {task.id for task in tasks.load_tasks()}
        for name in list(threads):
            if name not in known:
                del threads[name]
        save_json(notices_path(), state)
    return posted


def task_threads() -> Dict[str, Dict[str, str]]:
    return load_json(notices_path(), {}).get("threads") or {}


def remember_thread(task_id: str, channel: str, ts: str) -> None:
    with file_lock(notices_path().with_suffix(".lock")):
        state = load_json(notices_path(), {})
        state.setdefault("version", 1)
        state.setdefault("threads", {})[task_id] = {"channel": channel, "ts": ts}
        save_json(notices_path(), state)


# --- transport --------------------------------------------------------------------------


class Transport:
    """What the bridge needs from a chat tool. Messages are dicts with `ts`, `user`, `text` and,
    in a thread, `thread_ts`; `ts` values sort as numbers."""

    def auth(self) -> Dict[str, Any]:
        """Check the credentials; return at least {"user_id": <the bot's own user id>}."""
        raise NotImplementedError

    def history(self, channel: str, oldest: str) -> List[Dict[str, Any]]:
        """Top-level messages newer than `oldest`, oldest first."""
        raise NotImplementedError

    def replies(self, channel: str, thread_ts: str, oldest: str) -> List[Dict[str, Any]]:
        """Replies in one thread newer than `oldest`, oldest first, without the parent."""
        raise NotImplementedError

    def post(self, channel: str, text: str, thread_ts: Optional[str] = None, broadcast: bool = False) -> str:
        """Post a message; return its ts."""
        raise NotImplementedError

    def react(self, channel: str, ts: str, name: str) -> None:
        """Add an emoji reaction; failures may be ignored."""
        raise NotImplementedError


class SlackWebAPI(Transport):
    """Slack Web API over urllib with a bot token. Waits out HTTP 429 using Retry-After."""

    def __init__(self, bot_token: str, base: str = "https://slack.com/api", retries: int = 4):
        self.token, self.base, self.retries = bot_token, base.rstrip("/"), retries

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(f"{self.base}/{method}", data=body, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode() or "{}")
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < self.retries:
                    wait = min(int(error.headers.get("Retry-After") or 30), 600)
                    log(f"slack {method}: rate limited, waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise SwarmError(f"slack {method}: HTTP {error.code}")
            except (urllib.error.URLError, OSError, ValueError) as error:
                raise SwarmError(f"slack {method}: {getattr(error, 'reason', error)}")
            if not payload.get("ok"):
                raise SwarmError(f"slack {method}: {payload.get('error') or 'not ok'}")
            return payload
        raise SwarmError(f"slack {method}: still rate limited")

    def _paged(self, method: str, key: str, **params: Any) -> List[Dict[str, Any]]:
        found, cursor = [], None
        while True:
            payload = self.call(method, limit=200, cursor=cursor, **params)
            found += payload.get(key) or []
            cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return found

    def auth(self) -> Dict[str, Any]:
        return self.call("auth.test")

    def history(self, channel: str, oldest: str) -> List[Dict[str, Any]]:
        found = self._paged("conversations.history", "messages", channel=channel, oldest=oldest)
        return sorted(found, key=lambda m: float(m.get("ts", 0)))

    def replies(self, channel: str, thread_ts: str, oldest: str) -> List[Dict[str, Any]]:
        found = self._paged("conversations.replies", "messages", channel=channel, ts=thread_ts, oldest=oldest)
        found = [m for m in found if m.get("ts") != thread_ts and float(m.get("ts", 0)) > float(oldest)]
        return sorted(found, key=lambda m: float(m.get("ts", 0)))

    def post(self, channel: str, text: str, thread_ts: Optional[str] = None, broadcast: bool = False) -> str:
        if len(text) > MAX_POST:
            text = "…" + text[-MAX_POST:]
        payload = self.call("chat.postMessage", channel=channel, text=text, thread_ts=thread_ts,
                            reply_broadcast="true" if thread_ts and broadcast else None,
                            unfurl_links="false", unfurl_media="false")
        return str(payload.get("ts") or "")

    def react(self, channel: str, ts: str, name: str) -> None:
        try:
            self.call("reactions.add", channel=channel, timestamp=ts, name=name)
        except SwarmError as error:
            log(f"reaction {name} skipped: {error}")


def transport() -> Transport:
    """The configured backend. Add another `slack.backend` value here to swap it."""
    values = settings()
    backend = values.get("backend") or "webapi"
    if backend == "webapi":
        return SlackWebAPI(token() or "", str(values.get("api_base") or "https://slack.com/api"))
    raise SwarmError(f"unknown slack.backend {backend!r}; known: webapi")


# --- the bridge -------------------------------------------------------------------------


class Bridge:
    def __init__(self, chat: Transport):
        values = settings()
        self.chat = chat
        self.channel = str(values.get("channel") or "")
        self.allowed = {str(user) for user in values.get("allowed_users") or []}
        self.notices = str(values.get("notices") or "bridge")
        self.say_wait = float(values.get("say_wait_minutes") or 30)
        if not self.channel:
            raise SwarmError("set slack.channel to the channel or DM id (C..., G... or D...)")
        if not self.allowed:
            raise SwarmError("set slack.allowed_users to the Slack user ids allowed to command the swarm")
        self.me = str(chat.auth().get("user_id") or "")
        self.state_path = state_dir() / "slack.json"
        self.state = load_json(self.state_path, {})
        self.workers: List[threading.Thread] = []

    def save(self) -> None:
        save_json(self.state_path, self.state)

    def accept(self, message: Dict[str, Any]) -> bool:
        if message.get("subtype") or message.get("bot_id") or message.get("user") == self.me:
            return False
        return str(message.get("user")) in self.allowed and bool(str(message.get("text") or "").strip())

    def reply(self, ts: str, text: str, code: bool = False) -> None:
        text = mask(text)
        if len(text) > MAX_POST:
            text = "…" + text[-MAX_POST:]
        try:
            self.chat.post(self.channel, f"```\n{text}\n```" if code else text, thread_ts=ts)
        except SwarmError as error:
            log(f"reply not posted: {error}")

    def forward(self, ts: str, to: str, prompt: str) -> None:
        """messaging.say in a thread, since it waits for the agent to be idle."""
        def run() -> None:
            try:
                sent = messaging.say(to, prompt, wait_minutes=self.say_wait)
            except SwarmError as error:
                self.reply(ts, f"not sent: {error}")
                log(f"say {to} failed: {error}")
                return
            if sent:
                self.chat.react(self.channel, ts, "white_check_mark")
                log(f"say {to}: delivered")
            else:
                self.reply(ts, f"{to} stayed busy for {self.say_wait:g} min; not sent. Send it again later.")
                log(f"say {to}: busy, not sent")
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.workers.append(worker)

    def handle(self, message: Dict[str, Any]) -> None:
        ts, text = str(message["ts"]), str(message.get("text") or "").strip()
        words = text.split()
        word = words[0].casefold()
        log(f"message {ts}: {one_line(text)[:80]}")
        if word == "help":
            self.reply(ts, HELP, code=True)
        elif word == "status" and len(words) == 1:
            self.reply(ts, status_text(), code=True)
        elif word == "list" and len(words) <= 2 and words[1:] in ([], ["all"]):
            self.reply(ts, tasks.listing(len(words) == 2, None), code=True)
        elif word == "tasks" and len(words) == 2:
            self.reply(ts, tasks.listing(True, words[1].lstrip("@")), code=True)
        elif word == "screen" and len(words) == 2:
            name = words[1].lstrip("@")
            try:
                lines = herdr.screen(name).rstrip().splitlines()[-40:]
                self.reply(ts, "\n".join(lines) or f"{name}: empty screen", code=True)
            except Exception as error:  # herdr missing or agent gone
                self.reply(ts, f"no screen for {name}: {error}")
        else:
            to, body = tasks.owners()[0], text
            match = re.match(r"^@([\w.-]+)\s+(.+)$", text, re.S)
            if match:
                to, body = match.group(1), match.group(2)
            self.chat.react(self.channel, ts, "eyes")
            self.forward(ts, to, f"slack, from the user: {one_line(body)} "
                                 f"(reply with: swarm slack post --thread {ts} \"<text>\")")

    def handle_answer(self, task_id: str, message: Dict[str, Any]) -> None:
        ts, text = str(message["ts"]), one_line(message.get("text") or "")
        try:
            task = tasks.find_task(task_id)
        except SwarmError as error:
            log(f"answer for {task_id} dropped: {error}")
            return
        owner = task.meta.get("owner") or tasks.owners()[0]
        what = "the answer to its waiting-on-you question" if task.status == "waiting-on-you" else "a note"
        log(f"thread reply for {task_id} -> {owner}")
        self.chat.react(self.channel, ts, "eyes")
        self.forward(ts, owner, f"slack, from the user, {what} on task {task.id}: {text} "
                                f"(record it in the task; reply with: swarm slack post --task {task.id} \"<text>\")")

    def cycle(self) -> None:
        # 1. top-level messages. The first run starts from now; old history is never replayed.
        oldest = self.state.get("oldest")
        if oldest is None:
            self.state["oldest"] = f"{time.time():.6f}"
            self.save()
        else:
            for message in self.chat.history(self.channel, oldest):
                self.state["oldest"] = max(str(message["ts"]), self.state["oldest"], key=float)
                self.save()
                if message.get("thread_ts") and message.get("thread_ts") != message.get("ts"):
                    continue
                if self.accept(message):
                    self.handle(message)
        # 2. replies in the threads of active tasks.
        seen = self.state.setdefault("replies", {})
        active = {task.id: task for task in tasks.load_tasks() if task.status in tasks.ACTIVE}
        for task_id, thread in task_threads().items():
            if task_id not in active or thread.get("channel") != self.channel:
                continue
            parent = str(thread["ts"])
            since = seen.get(parent) or parent
            for message in self.chat.replies(self.channel, parent, since):
                seen[parent] = max(str(message["ts"]), seen.get(parent) or parent, key=float)
                self.save()
                if self.accept(message):
                    self.handle_answer(task_id, message)
        live = {str(thread.get("ts")) for thread in task_threads().values()}
        for parent in [parent for parent in seen if parent not in live]:
            del seen[parent]
        self.save()
        # 3. task notices, unless another daemon owns them.
        if self.notices == "bridge":
            post_notices(self.chat, self.channel)

    def wait_workers(self) -> None:
        for worker in self.workers:
            worker.join()

    def run(self, poll: float) -> None:
        log(f"bridge up: channel {self.channel}, users {', '.join(sorted(self.allowed))}, "
            f"notices: {self.notices}, every {poll:g}s")
        while True:
            try:
                self.cycle()
            except SwarmError as error:
                log(f"cycle error: {error}")
            except Exception as error:  # keep the daemon alive; the log shows what broke
                log(f"cycle error: {type(error).__name__}: {error}")
            self.workers = [worker for worker in self.workers if worker.is_alive()]
            time.sleep(poll)


def status_text() -> str:
    lines = []
    try:
        for item in herdr.agents():
            if item.get("name"):
                lines.append(f"{item['name']:<20} {item.get('agent_status', '?')}")
    except SwarmError as error:
        lines.append(f"herdr: {error}")
    counts: Dict[str, Dict[str, int]] = {}
    for task in tasks.load_tasks():
        if task.status in tasks.ACTIVE:
            per = counts.setdefault(task.meta.get("owner", "?"), {})
            per[task.status] = per.get(task.status, 0) + 1
    lines.append("")
    for owner in tasks.owners():
        per = counts.get(owner, {})
        lines.append(f"@{owner}: " + (", ".join(f"{n} {s}" for s, n in per.items()) or "no active tasks"))
    return "\n".join(lines).strip()


# --- daemon control ---------------------------------------------------------------------


def running_pid() -> Optional[int]:
    try:
        pid = int(pid_file().read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def tmux_running() -> bool:
    return bool(shutil.which("tmux")) and subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True).returncode == 0


def cmd_start(args: argparse.Namespace) -> int:
    if args.foreground:
        return cmd_run(argparse.Namespace(once=False))
    if running_pid() or tmux_running():
        print("slack bridge already running; `swarm slack status`")
        return 0
    from_env = bool(os.environ.get("SLACK_BOT_TOKEN"))
    token()  # fail here, not in the background, when the token or the settings are missing
    values = settings()
    if not values.get("channel") or not values.get("allowed_users"):
        raise SwarmError("set slack.channel and slack.allowed_users first (docs/slack.md)")
    log_file().parent.mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(SWARM_BIN), "slack", "run"]
    runner = args.runner or values.get("runner") or "nohup"
    if runner == "tmux":
        if not shutil.which("tmux"):
            raise SwarmError("slack.runner is tmux but tmux is not on PATH; install it or use --runner nohup")
        if from_env:
            raise SwarmError("the tmux runner reads the token from slack.token_file, not $SLACK_BOT_TOKEN")
        env = " ".join(f"{k}={shlex.quote(os.environ[k])}" for k in ("TSTACK_CONFIG", "SWARM_DIR", "HERDR_BIN")
                       if os.environ.get(k))
        shell = f"{env} {' '.join(map(shlex.quote, command))} >> {shlex.quote(str(log_file()))} 2>&1".strip()
        subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_SESSION, shell], check=True)
        print(f"slack bridge started in tmux session {TMUX_SESSION}; log {log_file()}")
        return 0
    if runner != "nohup":
        raise SwarmError(f"unknown slack.runner {runner!r}; use nohup or tmux")
    with open(log_file(), "a") as out:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    pid_file().write_text(f"{child.pid}\n")
    time.sleep(2)
    if child.poll() is not None:
        raise SwarmError(f"slack bridge exited at once (code {child.returncode}); see {log_file()}")
    print(f"slack bridge started, pid {child.pid}; log {log_file()}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    stopped = False
    pid = running_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        stopped = True
    pid_file().unlink(missing_ok=True)
    if tmux_running():
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
        stopped = True
    print("slack bridge stopped" if stopped else "slack bridge was not running")
    return 0


def tail(path: Path, lines: int) -> List[str]:
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines()[-lines:]


def cmd_status(args: argparse.Namespace) -> int:
    values = settings()
    pid = running_pid()
    where = f"pid {pid}" if pid else (f"tmux session {TMUX_SESSION}" if tmux_running() else "")
    print(f"slack bridge: {'running, ' + where if where else 'stopped'}")
    print(f"channel: {values.get('channel') or '(unset)'}   allowed users: "
          f"{', '.join(map(str, values.get('allowed_users') or [])) or '(none)'}")
    print(f"notices: {values.get('notices') or 'bridge'}   token: "
          f"{'found' if token(required=False) else 'missing'}   log: {log_file()}")
    for line in tail(log_file(), 8):
        print(f"  {line}")
    return 0 if where else 1


def cmd_logs(args: argparse.Namespace) -> int:
    if not log_file().exists():
        raise SwarmError(f"no log yet at {log_file()}")
    if args.follow:
        return subprocess.call(["tail", "-n", str(args.lines), "-f", str(log_file())])
    print("\n".join(tail(log_file(), args.lines)))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    chat = transport()
    info = chat.auth()
    print(f"auth ok: bot user {info.get('user_id')} ({info.get('user', '?')}) in team {info.get('team', '?')}")
    channel = str(settings().get("channel") or "")
    if not channel:
        raise SwarmError("auth works, but slack.channel is unset; nothing posted")
    ts = chat.post(channel, "👋 tstack bridge test: this bot can post here. Send `help` for commands.")
    print(f"posted hello to {channel} (ts {ts})")
    chat.history(channel, ts)
    print(f"read {channel}: ok")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    bridge = Bridge(transport())
    if args.once:
        bridge.cycle()
        bridge.wait_workers()
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    bridge.run(float(settings().get("poll_seconds") or 10))
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    chat, channel = transport(), str(settings().get("channel") or "")
    if not channel:
        raise SwarmError("set slack.channel first")
    thread = args.thread
    if args.task:
        task = tasks.find_task(args.task)
        thread = (task_threads().get(task.id) or {}).get("ts")
        if not thread:
            thread = chat.post(channel, f"📋 {task.meta.get('title')} (@{task.meta.get('owner')} · {task.id})")
            remember_thread(task.id, channel, thread)
    ts = chat.post(channel, mask(args.text), thread_ts=thread)
    print(f"posted (ts {ts})")
    return 0


def check_settings(values: Dict[str, Any]) -> List[str]:
    found = values.get("slack")
    if not isinstance(found, dict) or not found.get("channel"):
        return []  # bridge not set up; nothing to check
    problems = []
    if not isinstance(found.get("allowed_users"), list) or not found["allowed_users"]:
        problems.append("slack.allowed_users must list at least one Slack user id")
    if found.get("notices") not in ("bridge", "routine", "off"):
        problems.append("slack.notices must be bridge, routine or off")
    if found.get("runner") not in ("nohup", "tmux"):
        problems.append("slack.runner must be nohup or tmux")
    if not isinstance(found.get("poll_seconds"), (int, float)) or found["poll_seconds"] < 1:
        problems.append("slack.poll_seconds must be a number >= 1")
    return problems


config.VALIDATORS.append(check_settings)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("slack", help="Slack bridge: start|stop|status|logs|test|run|post",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    q = actions.add_parser("start", help="run the bridge in the background (nohup or tmux)")
    q.add_argument("--foreground", action="store_true", help="run in this terminal instead")
    q.add_argument("--runner", choices=["nohup", "tmux"], help="override slack.runner")
    q.set_defaults(run=cmd_start)

    actions.add_parser("stop", help="stop the background bridge").set_defaults(run=cmd_stop)
    actions.add_parser("status", help="running or not, settings, recent log").set_defaults(run=cmd_status)

    q = actions.add_parser("logs", help="print (or -f follow) the bridge log")
    q.add_argument("-f", "--follow", action="store_true")
    q.add_argument("-n", "--lines", type=int, default=40)
    q.set_defaults(run=cmd_logs)

    actions.add_parser("test", help="check the token (auth.test) and post a hello").set_defaults(run=cmd_test)

    q = actions.add_parser("run", help="the poll loop in the foreground (what start runs)")
    q.add_argument("--once", action="store_true", help="one poll cycle, wait for deliveries, exit")
    q.set_defaults(run=cmd_run)

    q = actions.add_parser("post", help="post a message as the bot (how agents reply)")
    q.add_argument("text")
    where = q.add_mutually_exclusive_group()
    where.add_argument("--task", help="post in this task's thread (created if missing)")
    where.add_argument("--thread", help="post in the thread of this message ts")
    q.set_defaults(run=cmd_post)
