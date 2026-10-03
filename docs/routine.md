# The routine, the pilot log and the night runner

Three helpers that keep a swarm running without you watching it:

- **The routine** (`swarm routine`): an always-on loop. It restarts managers in fresh sessions each
  morning and whenever their context gets heavy, sends scheduled prompts, raises a low-disk alert,
  and keeps herdr's pop-ups and sidebar labels in step with the task folders.
- **The pilot log** (`swarm pilot-log`): one row per day of five measures, so you can tell whether
  the swarm is saving you time.
- **The night runner** (`swarm night`): a queue of prompts or tasks that fresh headless agent
  sessions work through overnight, with a report waiting in the morning.

Code: `lib/tstack/routine.py`, `lib/tstack/night.py`, `lib/tstack/background.py`.

## Setup

Nothing extra is needed beyond tstack itself: Python 3.9+, herdr, and the agent CLI (Claude Code by
default). tmux is optional.

### macOS

```bash
brew install tmux                 # optional; without it the loop runs with nohup
swarm routine check               # each manager's state and context use; problems in the settings
swarm routine once --dry-run      # what one pass would do right now
swarm routine start               # run the loop in the background
swarm routine status              # running? last pass, pending restarts, recent log
```

### Linux

```bash
sudo apt install tmux             # or dnf/pacman; optional, as above
swarm routine check
swarm routine once --dry-run
swarm routine start
```

The loop survives closing your terminal but not a reboot. Run `swarm routine start` after a login,
or use one of the optional service samples at the end of this page.

Only one loop runs at a time: a second `swarm routine loop` (from a service, say, while tmux runs
one) stops at once with "another routine loop already runs". The same holds for the night runner.

## Commands

| Command | What it does |
|---|---|
| `swarm routine start` | Start the loop in the background (tmux session or nohup process named `routine.session`) |
| `swarm routine stop` | Stop it, whichever backend started it |
| `swarm routine status` | Running or not, last pass, morning restart date, pending restarts, last log lines |
| `swarm routine logs [-n N] [-f]` | The log, `<data_dir>/logs/routine.log` |
| `swarm routine check` | Read-only table: each manager's herdr status, context use and where it was read, quiet time, notes (question dialog, over the line, restart pending). Exits 1 if the settings have problems |
| `swarm routine once [--dry-run] [--morning]` | One pass now. `--dry-run` prints which managers it would restart and why, which scheduled prompts are due and the disk state, and changes nothing. `--morning` treats the morning restart as due |
| `swarm routine loop` | The loop in the foreground (what `start` and the service samples run) |
| `swarm pilot-log [show] [--last N]` | Print the pilot log, or only its last N rows |
| `swarm pilot-log add [--date D] --transfers N --status-turns X/Y --surprises N --questions '...' --time '...' --notes '...' [--replace]` | Add a day's row (today by default). Refuses a second row for a day unless `--replace` |
| `swarm night run "<prompt>" [--task ID] [--cwd DIR] [--verify CMD] [--max-attempts N] [--minutes M] [--now] [--dry-run] [--no-start] [--foreground]` | Queue one job and start the runner |
| `swarm night run-file <jobs.json> [same flags] [--dry-run]` | Queue every job in a file; flags fill keys a job leaves out |
| `swarm night status` | Runner state, window, the queue |
| `swarm night logs [-n N] [-f]` | `<data_dir>/logs/night.log` |
| `swarm night stop [--drop-queued]` | Stop the runner and the job it is running; `--drop-queued` also drops jobs not started |
| `swarm night report [--date YYYY-MM-DD]` | The latest morning report, or one night's |

Restarting one manager by hand stays `swarm restart <name>`; the routine calls the same code.

## What one pass does

Every `interval_seconds` (60):

1. **Morning restart.** Once a day, at `morning_restart`, every manager becomes due for a restart.
   A manager with no memory node folder or no herdr agent is skipped and named in one log line. If
   the loop is first started after that time, it waits for tomorrow instead of restarting everyone.
2. **Context restart.** Every `check_every_minutes` (10), a manager whose context use is at
   `context_restart_percent` (60) or more becomes due.
3. **Waiting for a safe moment.** A due restart happens only when the manager is idle, no question
   or approval dialog is open, and its screen has not changed for `quiet_minutes` (10), so it never
   cuts into a conversation with you. Until then it stays due and the log says why, once. On a
   morning restart the overall manager goes last, after every other manager is done.
4. **The restart** is `swarm restart <name>`: it asks for the handoff, waits for it, sends
   `agent.exit_command`, and starts a fresh session in the same pane with the role's first prompt.
   It can take a while (up to 45 minutes for the handoff); the loop waits for it.
5. **Morning report.** When the morning restarts are done, the routine tells the overall manager
   which ones happened and asks for today's pilot-log row (`pilot_log`: true).
6. **Scheduled prompts and disk** (every `check_every_minutes`), see below.
7. **Task notices** (every pass): herdr pop-ups and labels, and Slack when `slack.notices` is `routine`; see below.

A failing step is logged and the loop tries again on the next pass. State lives in
`<data_dir>/routine-state.json` (due restarts, screen hashes for quiet time, sent prompts, labels).

### Reading context use

`context_source` chooses how:

- `screen`: `agent.context_regex` over the pane's visible text (the core setting). Claude Code shows
  "Context left until auto-compact: N%" only when the context is nearly full, so on its own this
  sees a heavy session late.
- `transcript`: Claude Code writes each session to
  `<transcript_dir>/<start folder with / and . as ->/<session>.jsonl`, and a session started with
  `-n <name>` (tstack's default `agent.name_flag`) records that name near the top. The routine finds
  the newest transcript named after the manager (in the manager's start folder first, else in any
  project folder), reads the token use of the last main-thread reply (input + cache read + cache
  creation), and divides by `context_window_tokens`. This reads the real number at any fill level.
- `auto` (default): the screen when it says something, else the transcript.

For another agent CLI, set `context_source` to `screen` and `agent.context_regex` /
`agent.context_reports` to whatever that CLI prints, or extend `transcript_context()`.

### Question dialogs

A manager waiting on you is never restarted: herdr status `blocked` counts as an open dialog, and so
does a match of `question_regex` in the last 15 lines of its screen (default: Claude Code's
"Enter to select", "Esc to cancel", "Do you want to proceed?" prompts).

### Scheduled prompts

Each entry in `routine.scheduled_prompts` sends one message to one agent on a fixed cycle:

```json
{"agent": "backend", "start_date": "2026-10-05", "every_days": 14, "after": "09:00",
 "message": "swarm routine: cleanup day. Run your queued cleanups, then go idle."}
```

Due on `start_date` and every `every_days` days after, once the clock passes `after`, at most once
that day, even when the agent stays busy and the message is not delivered. A day the routine is not
running is skipped. A malformed entry is logged and skipped; `swarm config check` reports it.

### Disk alert

If a `server` module with `disk_status(cfg) -> list[dict]` is installed (the server feature), the
routine calls it every `check_every_minutes`. An entry counts as low when it has `ok: false`, `low`,
`warn`, or `status` of low/warn/warning/alert; its `message`, or `path` and `free_gb` /
`free_bytes` and `min_free_gb`, make the alert text. When something is low it shows a herdr pop-up and
tells `disk_alert.agent` (default the overall manager), at most once every `disk_alert.every_hours`.
Without the module the alert is skipped silently (`once --dry-run` says so). Turn it off with
`"disk_alert": {"enabled": false}`.

### Task notices (herdr and Slack)

Every task that newly waits on you, or is newly done or dropped, is announced once per delivery
channel. All channels share one state file, `<data_dir>/state/notices.json` (format in
`docs/slack.md`), updated under the flock `<data_dir>/state/notices.lock`:
`seen.<consumer>.<task id>` holds the last notice key each consumer delivered
(`waiting-on-you@<waiting_since>` while waiting, else the status). Done and dropped tasks count only
within 7 days. A consumer missing from the file is on its first run: finished tasks are recorded
without a notice, waiting tasks are announced.

With `herdr_notices` on (default) each pass:

1. Shows herdr pop-ups as consumer `herdr` (`tasks.popups()`). `swarm list --watch` calls the same
   function with the same consumer, so a task pops up once no matter which of the two sees it
   first, and running both is safe.
2. Labels the sidebar: each manager's pane gets the token `task` (its most urgent task, e.g.
   `ingest +2`), each manager's workspace gets `tasks` (e.g. `1 waiting · 2 running`). It writes a
   label only when it changed since the routine last wrote it.

Slack: who posts Slack notices is `slack.notices`. With `bridge` (default) the Slack bridge does;
with `routine`, each routine pass calls `slack.post_notices(slack.transport(), slack.channel)`
(consumer `slack`, same threads, so the bridge still routes thread replies to task owners); with
`off`, nobody. herdr pop-ups and Slack posts are separate consumers, so you get one of each.

Pop-ups need `[ui.toast] delivery = "herdr"`; labels show where `$task` is in
`[ui.sidebar.agents] rows` and `$tasks` in `[ui.sidebar.spaces] rows` of herdr's config.

## The pilot log

`<data_dir>/pilot-log.md`, one row per day, created by the first `swarm pilot-log add`:

```markdown
# Pilot log

One row per day. After the morning restart the routine asks the overall manager to add today's row
with `swarm pilot-log add`. The five measures: ...

| Date | Context passed by hand | Status-check turns | Surprises | Questions to you (unneeded) | Your time vs output | Notes |
|---|---|---|---|---|---|---|
| 2026-10-03 | 0 | 2/31 | 0 | 3 (1 unneeded) | about 40 min | first day |
```

The measures: context you carried between agents by hand (target 0); status-check turns out of all
your turns (under 1 in 10); surprises: collisions over GPUs, checkouts, ports or storage, unnoticed
job failures, false "done" reports (0); questions that reached you and how many were not needed;
your supervising time against the work done. Rows stay in date order; the file is plain Markdown and
can be edited by hand.

## The night runner

Queue work during the day; a runner starts it inside the window (`window_start` to `window_end`,
default 22:00 to 07:00) and works through it one job at a time. Each job is a fresh headless session:

```text
<agent.cmd> <night.headless_args> <agent.model_flag> <model> <night.extra_args> [<resume_flag> <session>] <prompt>
claude -p --output-format json --model sonnet --permission-mode acceptEdits "<prompt>"
```

With `verify`, the runner runs that shell command in the job's folder after each turn. While it
fails, it resumes the same session with the failure output, up to `max_attempts` turns. A job with
`task` gets the normal worker prompt for that task folder (so it writes `result.md`), plus any
`prompt` as extra instructions. Every prompt ends with a note that nobody can answer questions
before morning. A turn longer than `job_minutes` is stopped.

No job starts after `window_end`; a running one finishes. When the queue empties or the window ends,
the runner writes `<data_dir>/night/reports/<night>.md` (the night is filed under the evening it
started), copies it to `<data_dir>/night/report.md`, shows a herdr pop-up and tells `report_to`
(default the overall manager; `"none"` for nobody). Jobs left in the queue wait for the next night;
the runner exits when the queue is empty. The report lists per job: status, turns, time, the check's
last exit code, `result.md` for task jobs, `git status`/`diff --stat` of the folder, the last reply,
and the command to resume the session.

Jobs file (`.json`: a list, or `{"jobs": [...]}`; any other file is one prompt). Sample:
[`night-jobs.example.json`](night-jobs.example.json).

```json
{"jobs": [
  {"title": "Fix the failing unit tests", "prompt": "Run the unit tests and fix the code.",
   "cwd": "~/work/myrepo", "verify": "python3 -m pytest -q", "max_attempts": 4},
  {"task": "2026-10-05-ingest", "prompt": "Only the dry-run part tonight."}
]}
```

Keys: `prompt` or `task` (one is required), `title`, `cwd` (default `night.cwd`, then the task
owner's folder, then `workspace`), `model`, `verify`, `max_attempts`, `minutes`.

Permissions: an unattended Claude Code session cannot ask you anything. The default
`extra_args` (`--permission-mode acceptEdits`) lets it edit files but not run arbitrary commands.
Widen it deliberately, for example `["--permission-mode", "acceptEdits", "--allowedTools",
"Bash(pytest:*)"]`, and give each job its own git worktree.

## Settings

All under the `routine` section of the settings file (`swarm config get routine`); every key is
optional.

| Key | Default | Meaning |
|---|---|---|
| `backend` | `auto` | How the loop runs in the background: `tmux`, `nohup`, or `auto` (tmux when on PATH) |
| `session` | `tstack-routine` | tmux session / pid-file name |
| `interval_seconds` | 60 | Time between passes |
| `morning_restart` | `06:00` | Daily restart of every manager; `""` turns it off |
| `context_restart_percent` | 60 | Restart at this context use; 0 turns it off |
| `check_every_minutes` | 10 | Context check, scheduled prompts and disk alert this often |
| `quiet_minutes` | 10 | A due restart waits for the screen to be still this long |
| `context_source` | `auto` | `screen`, `transcript`, or `auto` |
| `transcript_dir` | `~/.claude/projects` | Where Claude Code keeps session transcripts |
| `context_window_tokens` | 200000 | Tokens that count as 100% when reading transcripts |
| `question_regex` | Claude Code dialogs | A pending dialog at the bottom of the screen |
| `say_wait_minutes` | 10 | How long a routine message waits for a busy agent |
| `herdr_notices` | true | herdr task pop-ups and sidebar labels (Slack notices follow `slack.notices`) |
| `pilot_log` | true | Ask the overall manager for the pilot-log row after the morning restart |
| `scheduled_prompts` | `[]` | See above |
| `disk_alert` | `{"enabled": true, "agent": "", "every_hours": 24}` | See above |
| `night.session` / `night.backend` | `tstack-night` / `auto` | Like the loop's |
| `night.window_start` / `night.window_end` | `22:00` / `07:00` | Jobs start only inside this window (may cross midnight) |
| `night.headless_args` | `["-p", "--output-format", "json"]` | Flags for one headless turn |
| `night.extra_args` | `["--permission-mode", "acceptEdits"]` | What an unattended run may do |
| `night.resume_flag` | `--resume` | Continue a session after a failed check |
| `night.model` | `""` | Default model; `""` means `agent.worker_model` |
| `night.max_attempts` | 3 | Turns per job while its check fails |
| `night.job_minutes` | 120 | Time limit per turn |
| `night.report_to` | `""` | Agent told about the report; `""` the overall manager, `"none"` nobody |
| `night.cwd` | `""` | Default job folder; `""` means `workspace` |

`swarm config check` validates the times, numbers, backend names and scheduled prompts.

## Swapping a backend

- **Background runner** (`background.py`): a backend is a class with `start(name, argv, log)`,
  `running(name)`, `stop(name)` and `where(name)`. Two ship, `Tmux` and `Nohup`; add a class to
  `BACKENDS` and set `routine.backend` / `routine.night.backend` to its name. For a service manager
  (launchd, systemd), skip `start` and run `swarm routine loop` from the service instead (below).
- **Agent CLI**: the routine reads agents only through `agent.py` (idle states, context regex, exit
  command) and restarts through `swarm restart`. For another CLI's night runs, set `agent.cmd`,
  `agent.model_flag`, and `night.headless_args` / `extra_args` / `resume_flag`; the runner reads a
  JSON object with `result`, `session_id` and `is_error` from the last output line and falls back to
  plain text.
- **Notices**: a new delivery channel is a new consumer name in `state/notices.json`: read and
  write `seen.<name>` under the lock as `tasks.popups()` does, and add it as a step in
  `routine.notices()`. Alert texts go through `swarm say`.
- **Disk check**: any module `tstack.server` with `disk_status(cfg) -> list[dict]` works.

## Optional: run the loop as a service

These are samples; tstack does not install them. Use either a service or `swarm routine start`, not
both (the second loop would refuse to start anyway). Replace `HOME_DIR` with your home folder (`echo $HOME`; launchd does not expand `~`); the service needs a
PATH that finds `herdr`, `claude` and `git`.

### macOS: launchd

`~/Library/LaunchAgents/dev.tstack.routine.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.tstack.routine</string>
  <key>ProgramArguments</key>
  <array>
    <string>HOME_DIR/.local/bin/swarm</string>
    <string>routine</string>
    <string>loop</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>HOME_DIR/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>HOME_DIR/tstack-data/logs/routine.out</string>
  <key>StandardErrorPath</key><string>HOME_DIR/tstack-data/logs/routine.out</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.tstack.routine.plist   # start
launchctl bootout gui/$(id -u)/dev.tstack.routine                                   # stop
```

### Linux: systemd --user

`~/.config/systemd/user/tstack-routine.service`:

```ini
[Unit]
Description=tstack routine loop

[Service]
ExecStart=%h/.local/bin/swarm routine loop
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now tstack-routine     # start now and at login
journalctl --user -u tstack-routine -f           # its output (the log file has the same lines)
loginctl enable-linger "$USER"                   # optional: keep it running after you log out
```

herdr must be running for restarts and notices to do anything; when it is not, each pass logs the
error and the loop tries again a pass later.
