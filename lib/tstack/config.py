"""Settings: $TSTACK_CONFIG, else ~/.config/tstack/config.json.

Every module reads paths, names and models from here, never from hardcoded values. The file
holds only what the user changed; missing keys and whole missing sections fall back to DEFAULTS.
Paths come back with ~ expanded. Data folder override: $SWARM_DIR.

    swarm config show            print the merged settings
    swarm config get swarm.overall
    swarm config check           report problems in the settings file
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .util import KIT, SwarmError, write_atomic

DEFAULT_PATH = "~/.config/tstack/config.json"

DEFAULTS: Dict[str, Any] = {
    "user": os.environ.get("USER", "you"),
    "workspace": "~/work",
    "memory_dir": "~/tstack-memory",
    "data_dir": "~/tstack-data",
    "bin_dir": "~/.local/bin",
    # The coding agent CLI that managers and workers run. Default: Claude Code.
    "agent": {
        "cmd": "claude",                 # executable, checked by `swarm config check`
        "herdr_kind": "claude",          # value for `herdr agent start --kind`
        "manager_model": "opus",
        "worker_model": "sonnet",
        "model_flag": "--model",         # "" to pass no model
        "name_flag": "-n",               # flag that names the session; "" for none
        "extra_args": [],                # added before the first prompt
        "exit_command": "/exit",         # typed to quit a session before a restart
        "idle_states": ["idle", "done"],  # herdr agent_status values that mean "ready for a prompt"
        # Regex over the visible screen whose group 1 is a percentage of context, and whether
        # that number is the part "used" or the part "left". Unmatched means "unknown".
        "context_regex": r"(?i)context left until auto-compact:\s*(\d+(?:\.\d+)?)%",
        "context_reports": "left",
        "start_timeout_ms": 60000,
    },
    "swarm": {
        "overall": "overall",
        "managers": {},                  # written by `swarm add-manager`
        "max_workers": 4,
    },
    # Filled in by later modules; every section may be missing or empty.
    # Shared dev servers (server.py, docs/server.md).
    "server": {
        "scratch_dir": "",                 # scratch code, logs, temp files; "" = <workspace>/scratch
        "ckpts_dir": "",                   # checkpoints, weights, datasets, caches; "" = <workspace>/ckpts
        "worktree_root": "",               # worktrees go to <this>/<repo>-wt/<slug>; "" = workspace
        "gpu_backend": "nvidia-smi",       # or "none"; see gpu.BACKENDS
        "gpu_command": "nvidia-smi",
        "gpu_locks": {},                   # name -> lock file that outside jobs flock exclusively
        "gpu_busy_mib": 1024,              # an unreserved GPU using this much memory is busy
        "reserve_max_hours": 72,
        "disk_min_free_gb": 20,            # `swarm disk` warns below this
        "disk_paths": [],                  # extra paths for `swarm disk`
    },
    # The routine loop and the night runner (lib/tstack/routine.py, night.py; docs/routine.md).
    "routine": {
        "backend": "auto",               # how loops run in the background: auto (tmux if found), tmux, nohup
        "session": "tstack-routine",     # tmux session / pid-file name of the loop
        "interval_seconds": 60,          # one pass this often
        "morning_restart": "06:00",      # restart every manager daily at HH:MM; "" turns it off
        "context_restart_percent": 60,   # restart a manager whose context use reaches this; 0 turns it off
        "check_every_minutes": 10,       # context check, scheduled prompts and disk alert this often
        "quiet_minutes": 10,             # a due restart waits until the screen has been still this long
        "context_source": "auto",        # screen (agent.context_regex), transcript, or auto (screen, then transcript)
        "transcript_dir": "~/.claude/projects",  # Claude Code session transcripts, read for context use
        "context_window_tokens": 200000,  # context size that counts as 100% when reading transcripts
        # A pending question or approval dialog at the bottom of the screen (herdr "blocked" also counts).
        "question_regex": r"Enter to select|Esc to cancel|Do you want to (proceed|make this edit|create)",
        "say_wait_minutes": 10,          # how long a routine message waits for a busy agent
        "herdr_notices": True,           # task pop-ups and sidebar labels
        "pilot_log": True,               # after the morning restart, ask the overall manager for the row
        "scheduled_prompts": [],         # {"agent", "message", "start_date", "every_days", "after"}
        "disk_alert": {"enabled": True, "agent": "", "every_hours": 24},  # uses server.disk_status when present
        "night": {
            "session": "tstack-night",
            "backend": "auto",
            "window_start": "22:00",     # no job starts outside window_start..window_end
            "window_end": "07:00",
            "headless_args": ["-p", "--output-format", "json"],  # agent.cmd flags for one headless turn
            "extra_args": ["--permission-mode", "acceptEdits"],  # what an unattended run may do
            "resume_flag": "--resume",   # continue the same session after a failed check
            "model": "",                 # "" means agent.worker_model
            "max_attempts": 3,           # turns per job while its check fails
            "job_minutes": 120,          # time limit per turn
            "report_to": "",             # agent told about the morning report; "" overall, "none" nobody
            "cwd": "",                   # default folder for jobs; "" means workspace
        },
    },
    # Slack bridge (lib/tstack/slack.py, docs/slack.md). The bot token is never stored here.
    "slack": {
        "channel": "",                   # channel or DM id: C..., G... or D...
        "allowed_users": [],             # Slack user ids (U...) whose messages are obeyed
        "token_file": "~/.config/tstack/slack-token",  # read when $SLACK_BOT_TOKEN is unset
        "api_base": "https://slack.com/api",
        "backend": "webapi",             # chat transport; see Transport in slack.py
        "poll_seconds": 10,
        "notices": "bridge",             # who posts task notices: bridge, routine or off
        "say_wait_minutes": 30,          # how long a forwarded message waits for an idle agent
        "runner": "nohup",               # background runner for `swarm slack start`: nohup or tmux
    },
    "upkeep": {
        "backend": "disk",               # disk | s3 (see docs/upkeep.md to add another)
        "disk_path": "~/tstack-backups",
        "s3_uri": "",                    # s3://bucket/prefix, used when backend is s3
        "aws_profile": "",               # passed to the aws CLI when set
        "backup_time": "03:00",          # local time of the nightly run
        "roots": [],                     # folders to scan for git checkouts; empty means `workspace`
        "skip": [".cache", "*_cache", ".venv*", "venv", "site-packages", "node_modules", "__pycache__"],
        "protected": [],                 # paths a weekly "drop" must never touch
        "min_size_mb": 0,                # files with no other copy below this size are ignored
        "max_file_mb": 512,              # larger files are listed but not uploaded
        "upload_files": True,            # copy files with no other copy to files/
        "files_cap_gib": 20,             # per-night upload cap
        "keep_daily": 7,
        "keep_weekly": 4,
        "prune": True,
        "weekly_day": "sun",             # mon..sun: the nightly loop also writes the weekly review; "" is off
        "review_owner": "",              # task owner for the review; empty means swarm.overall
        "runner": "auto",                # auto | tmux | nohup
        "history_dir": "~/.claude/projects",  # Claude Code transcripts read by `swarm suggest-managers`
    },
}

# Dotted keys that hold a path; get() returns them expanded.
PATH_KEYS = {"workspace", "memory_dir", "data_dir", "bin_dir",
             "server.scratch_dir", "server.ckpts_dir", "server.worktree_root"}
SECTIONS = ("agent", "swarm", "server", "routine", "slack", "upkeep")
# Dict values replaced whole instead of merged key by key.
NO_MERGE = {"managers", "gpu_locks"}

# Feature modules add their own checks: VALIDATORS.append(fn), fn(settings) -> [problem, ...].
VALIDATORS: List[Callable[[Dict[str, Any]], List[str]]] = []


def config_path() -> Path:
    return Path(os.environ.get("TSTACK_CONFIG") or DEFAULT_PATH).expanduser()


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict) and key not in NO_MERGE:
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_raw(path: Optional[Path] = None, required: bool = True) -> Dict[str, Any]:
    """The settings file as written, without defaults."""
    path = path or config_path()
    if not path.exists():
        if required:
            raise SwarmError(f"no settings file at {path}; run {KIT}/install.sh first")
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as error:
        raise SwarmError(f"{path} is not valid JSON: {error}")
    if not isinstance(data, dict):
        raise SwarmError(f"{path} must hold a JSON object")
    return data


def load(path: Optional[Path] = None, required: bool = True) -> Dict[str, Any]:
    """DEFAULTS overlaid with the settings file."""
    return _merge(DEFAULTS, load_raw(path, required))


def save_raw(values: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(values, indent=2) + "\n")


def update(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Apply `change` to the raw settings file, save it, return the new merged settings."""
    raw = load_raw()
    change(raw)
    save_raw(raw)
    return load()


def get(key: str, values: Optional[Dict[str, Any]] = None) -> Any:
    """A dotted key such as `agent.worker_model`; path keys come back as an expanded Path."""
    value: Any = values if values is not None else load()
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise SwarmError(f"no setting {key}")
        value = value[part]
    if key in PATH_KEYS:
        return Path(str(value)).expanduser()
    return value


def section(name: str, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One section as a dict, empty when missing or not an object."""
    found = (values if values is not None else load()).get(name)
    return found if isinstance(found, dict) else {}


def path(key: str, values: Optional[Dict[str, Any]] = None) -> Path:
    return Path(str(get(key, values))).expanduser()


def data_dir(values: Optional[Dict[str, Any]] = None) -> Path:
    if os.environ.get("SWARM_DIR"):
        return Path(os.environ["SWARM_DIR"]).expanduser()
    return path("data_dir", values)


def validate(values: Dict[str, Any]) -> List[str]:
    problems = []
    for key in ("user", "workspace", "memory_dir", "data_dir", "bin_dir"):
        if not isinstance(values.get(key), str) or not values.get(key):
            problems.append(f"{key} must be a non-empty string")
    for name in SECTIONS:
        if name in values and not isinstance(values[name], dict):
            problems.append(f"{name} must be an object")
    agent, swarm = section("agent", values), section("swarm", values)
    if not isinstance(agent.get("cmd"), str) or not agent.get("cmd"):
        problems.append("agent.cmd must name the agent executable")
    elif not shutil.which(str(agent["cmd"])):
        problems.append(f"agent.cmd {agent['cmd']!r} is not on PATH")
    if not isinstance(agent.get("extra_args"), list):
        problems.append("agent.extra_args must be a list")
    if agent.get("context_reports") not in ("used", "left"):
        problems.append("agent.context_reports must be 'used' or 'left'")
    if not isinstance(swarm.get("managers"), dict):
        problems.append("swarm.managers must be an object")
    if not isinstance(swarm.get("max_workers"), int) or swarm.get("max_workers", 0) < 1:
        problems.append("swarm.max_workers must be a positive integer")
    for check in VALIDATORS:
        problems.extend(check(values))
    return problems


# --- commands -----------------------------------------------------------------------


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "path":
        print(config_path())
        return 0
    values = load(required=args.action != "show")
    if args.action == "show":
        print(json.dumps(values, indent=2))
    elif args.action == "get":
        if not args.key:
            raise SwarmError("config get needs a key, for example swarm.overall")
        value = get(args.key, values)
        print(json.dumps(value) if isinstance(value, (dict, list)) else value)
    else:
        problems = validate(values)
        for problem in problems:
            print(f"problem: {problem}")
        print(f"{config_path()}: {'ok' if not problems else str(len(problems)) + ' problem(s)'}")
        return 1 if problems else 0
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("config", help="show, get or check settings")
    p.add_argument("action", choices=["show", "get", "check", "path"])
    p.add_argument("key", nargs="?")
    p.set_defaults(run=cmd_config)
