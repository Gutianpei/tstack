# Upkeep: backups, weekly review, history scanner, memory tools

Upkeep is the part of tstack that keeps your work recoverable and your memory tidy. Its pieces:

| Piece | Command | What it does |
|---|---|---|
| Nightly backup | `swarm upkeep run-once\|start\|stop\|status` | Copies the work git cannot rebuild (unpushed commits, uncommitted changes, untracked files) plus the memory and data folders to a backend. |
| Restore | `swarm upkeep restore` | Rebuilds checkouts, memory and data from a run. |
| Weekly review | `swarm upkeep weekly` | Lists the files that exist nowhere else and turns the keep-or-drop question into a swarm task for the overall manager. |
| Inventory | `swarm upkeep inventory` | Read-only look at sizes, checkouts and files with no other copy. |
| History scanner | `swarm suggest-managers` | Reads Claude Code history and suggests managers and memory nodes. |
| Memory tools | `swarm memory check\|archive\|backup` | Keeps `AGENT.md` and `journal.md` within size limits without losing text. |

Nothing here pushes, changes a repo, or deletes a local file. The backup is off until you run it.

## Setup

Requirements: `python3` 3.9+ and `git`. Optional: `aws` (S3 backend), `tmux` (runs the nightly loop in
a detached session; without it the loop runs as a detached process).

1. Install tstack (`./install.sh`). That writes `~/.config/tstack/config.json` with an `upkeep` section.
2. Choose a backend in that file (see [Settings](#settings)). The default is `disk`, writing to
   `~/tstack-backups`. Point `disk_path` at another disk, a mounted network share or a synced folder for
   a copy that survives losing this machine.
3. Run one backup and read the log: `swarm upkeep run-once`.
4. Start the nightly loop: `swarm upkeep start`. Check on it with `swarm upkeep status`.

### macOS and Linux differences

The code is the same on both. Only how the nightly job is scheduled differs. Pick one of these.

**A. The built-in loop** (`swarm upkeep start`). It runs in tmux session `tstack-upkeep` if tmux is
installed, else as a detached process (pid in `<data_dir>/upkeep/loop.pid`). It logs to
`<data_dir>/logs/upkeep.log`. After a reboot, run `swarm upkeep start` again, or use B.

**B. The system scheduler**, which survives reboots and runs `run-once` each night. These are optional
samples; tstack never installs them. Replace `HOME_DIR` with your home folder (`echo $HOME`). With B the built-in loop is not running, so nothing
writes the weekly review: add a second weekly job that runs `swarm upkeep weekly`.

macOS, `~/Library/LaunchAgents/dev.tstack.upkeep.plist`, loaded with
`launchctl load ~/Library/LaunchAgents/dev.tstack.upkeep.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.tstack.upkeep</string>
  <key>ProgramArguments</key><array>
    <string>HOME_DIR/.local/bin/swarm</string><string>upkeep</string><string>run-once</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>HOME_DIR/tstack-data/logs/upkeep.log</string>
  <key>StandardErrorPath</key><string>HOME_DIR/tstack-data/logs/upkeep.log</string>
</dict></plist>
```

Linux, `~/.config/systemd/user/tstack-upkeep.service` and `.timer`, enabled with
`systemctl --user enable --now tstack-upkeep.timer` (run `loginctl enable-linger $USER` so it runs when
you are logged out):

```ini
# tstack-upkeep.service
[Service]
Type=oneshot
ExecStart=%h/.local/bin/swarm upkeep run-once
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin

# tstack-upkeep.timer
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

A laptop that sleeps at 03:00 misses the run on macOS (launchd runs it on wake) and on Linux when
`Persistent=true` is set. The built-in loop only runs while the machine is awake.

## Settings

Settings live in the `upkeep` section of the settings file (`$TSTACK_CONFIG`, else
`~/.config/tstack/config.json`). Missing keys use the defaults below. `swarm config check` validates them.

| Key | Default | Meaning |
|---|---|---|
| `backend` | `disk` | `disk` or `s3` (or a backend you add, see below). |
| `disk_path` | `~/tstack-backups` | Folder for the `disk` backend. |
| `s3_uri` | `""` | `s3://bucket/prefix` for the `s3` backend. |
| `aws_profile` | `""` | Passed to the `aws` CLI as `--profile` when set. Otherwise the CLI's normal credential lookup applies. |
| `backup_time` | `03:00` | Local time of the nightly run (`HH:MM`). |
| `roots` | `[]` | Folders searched for git checkouts (6 levels deep). Empty means the top-level `workspace`. |
| `skip` | caches, venvs, `node_modules` | Shell-style name patterns never scanned. |
| `protected` | `[]` | Paths a weekly `drop` must refuse to touch. |
| `min_size_mb` | `0` | Files with no other copy smaller than this are ignored. |
| `max_file_mb` | `512` | Larger files with no other copy are listed but not uploaded. |
| `upload_files` | `true` | Copy files with no other copy to `files/` in the backend. |
| `files_cap_gib` | `20` | Upload cap per night, oldest files first. |
| `keep_daily`, `keep_weekly` | `7`, `4` | Retention: the newest 7 runs, plus the newest run of this week and of each of the 4 weeks before. |
| `prune` | `true` | Apply retention after a run with no failures. |
| `weekly_day` | `sun` | `mon`..`sun`: the nightly loop also writes the weekly review that day. `""` turns it off. |
| `review_owner` | `""` | Owner of the review task. Empty means `swarm.overall`. |
| `runner` | `auto` | `auto`, `tmux` or `nohup`: how `start` runs the loop. |
| `history_dir` | `~/.claude/projects` | Claude Code transcripts read by `swarm suggest-managers`. |

Top-level keys also matter: `memory_dir` and `data_dir` are backed up, and `workspace` is the default root.

Example S3 setup:

```json
"upkeep": { "backend": "s3", "s3_uri": "s3://my-bucket/people/me/tstack", "aws_profile": "backup" }
```

## Commands

```
swarm upkeep run-once [--backend disk|s3] [--dest PATH_OR_URI]
swarm upkeep start | stop | status
swarm upkeep restore RUN TARGET [--only REGEX] [--files] [--dry-run]
swarm upkeep weekly [--days 7] [--out FILE] [--no-task]
swarm upkeep weekly --mark keep|drop [--include-keep] [--dry-run] PATH...
swarm upkeep inventory [--what sizes|worktrees|files|all]
swarm suggest-managers [--days 60] [--history-dir DIR] [--json] [--out FILE] [--snippets 3]
swarm memory check NODE...|all [--idle] [--rewrite]
swarm memory archive agent NODE... [--reason TEXT]
swarm memory archive journals NODE...|all [--apply] [--mine]
swarm memory backup [--backend ...] [--dest ...]
```

`--backend` and `--dest` on any upkeep command override the settings for that call, which is the
easy way to test: `swarm upkeep run-once --backend disk --dest /tmp/try`.

### What one run writes

Each run writes `runs/<YYYY-MM-DD>/` in the backend:

| Path | Holds |
|---|---|
| `repos/<repo>.unpushed.bundle` | `git bundle` of every branch, tag and the newest stash that no remote has. A repo with no remote is bundled whole. Worktrees share their repo's bundle. |
| `checkouts/<checkout>.patch` | Uncommitted changes to tracked files (`git diff HEAD --binary`). |
| `checkouts/<checkout>.untracked.tar.gz` | Untracked files git does not ignore (each up to 100 MB, 2 GiB per checkout). |
| `memory.tar.gz`, `data.tar.gz` | The `memory_dir` repo (with its `.git`) and the `data_dir` (tasks, handoffs; not logs). |
| `settings.tar.gz` | The tstack settings file, shell and git rc files, `~/.claude` instructions, rules and skills. |
| `MANIFEST.tsv` | One row per checkout: path, remote, branch, head commit, which files above belong to it. |
| `local-only.tsv` | Every file with no other copy, and why (`outside-git`, `git-ignored`, `skipped:<reason>`). |
| `log.txt` | The run's log. |

A run with no failures also writes `LATEST` and then applies retention. Files with no other copy go
once to `files/<path>` (never pruned by retention), with a ledger at `files/_ledger/uploads.tsv`.

Credential-looking files are never copied: names such as `.env`, `*.pem`, `*token*`, `.ssh/`, or
content such as private keys and well-known token shapes. Files changed in the last 2 hours wait for the
next night. The check after each upload compares the stored size with the local size; a failure is logged
as `FAILED upload ...` and the run exits 1.

### Restore

1. Install tstack and restore `~/.config/tstack/config.json` first (the backend settings), or pass
   `--backend` and `--dest`.
2. Look before writing: `swarm upkeep restore --dry-run latest ~/restore-test` lists every file in the run
   and what would be rebuilt.
3. Test in a scratch folder: `swarm upkeep restore latest ~/restore-test`. For each checkout it clones the
   recorded remote (or starts a new repo when there is none), fetches the unpushed bundle, checks out the
   recorded branch and commit, applies the patch and unpacks the untracked files. It verifies the commit
   and ends with `restore finished with N errors`. Existing folders are never touched. Checkouts come
   back under `TARGET/<path relative to home>`; worktrees come back as ordinary clones.
4. `memory.tar.gz`, `data.tar.gz` and `settings.tar.gz` are unpacked under `TARGET/_tstack/` for you to move
   into place. Nothing is restored over live settings.
5. `--files` also copies back `files/` (never overwriting). `--only REGEX` limits checkouts and files.
6. Set up credentials again (`gh auth login`, SSH keys, `aws configure`); the backup never holds them.

To restore an older run, give its date: `swarm upkeep restore 2026-10-01 ~/restore-test`.

### Weekly keep-or-drop review

`swarm upkeep weekly` reads the ledger, writes a Markdown report to `<data_dir>/upkeep/weekly/<date>.md`
and creates a swarm task owned by `review_owner` (default the overall manager) whose Reading points at the
report. The manager asks you folder by folder, then applies each answer:

```
swarm upkeep weekly --mark drop --dry-run work/scratch/old-run
swarm upkeep weekly --mark drop work/scratch/old-run
swarm upkeep weekly --mark keep 'work/data/*'
```

`drop` deletes only the backup copy, and later runs skip that path. `keep` marks it reviewed. A folder
covers everything under it; `folder/*` covers only the files directly in it. A path under
`upkeep.protected` is refused. `--no-task` writes the report only.

### Swapping the backend

A backend is a class with these methods (`lib/tstack/upkeep_store.py`, class `Store`): `describe`, `put`
(copy and verify), `get_text`, `fetch`, `list`, `folders`, `delete`, and optionally `delete_prefix`. To add
one (GCS, Azure, an rsync target, a WebDAV share):

1. Subclass `Store` in `upkeep_store.py` and implement the methods. Keys are relative paths with `/`.
2. Add it to `STORES`, for example `STORES["gcs"] = GcsStore`.
3. In `open_store()` in `upkeep.py`, build it from the settings. A custom class gets the `--dest` value, or
   an empty string, unless you add a branch that reads your own keys.
4. Set `"backend": "gcs"`. Run `swarm upkeep run-once` against a temp location first.

The `s3` backend shells out to the `aws` CLI, so it works with anything `aws` can reach with
`--endpoint-url` set in your AWS config (MinIO, R2, and similar).

## History scanner

`swarm suggest-managers --days 60` reads `~/.claude/projects/*/*.jsonl` (change with `--history-dir` or
`upkeep.history_dir`) and prints which folders you keep returning to. It never writes to the history,
makes no network calls, and writes a file only for `--out`. It:

- takes each session's folder from its most common working directory and its prompts from the messages
  you typed;
- groups sessions by the first folder under `workspace` (a worktree folder such as `app-wt` counts as
  `app`), or the first two folders under home;
- merges folders that the same sessions keep editing (at least 3 shared sessions and 30% overlap);
- ignores sessions a swarm manager started for a worker, and first prompts repeated 5 or more times
  (scripts);
- suggests a manager for a group with at least 3 sessions on 2 days, calls quieter groups "occasional",
  and groups idle for 30 days "dormant" (a memory node is enough).

`--json` gives the same result as data. `--snippets 0` leaves out the first-prompt excerpts.

## Memory tools

The memory repo (`memory_dir`) holds one folder per node. These commands find it from `--root`, else the
repo around the current folder, else `memory_dir`.

- `swarm memory check all` checks each node: `AGENT.md` at most 12 KB (4 KB with `--idle`) and linking
  `HISTORY.md`, `HISTORY.md` a four-column table with rows under 320 characters, `journal.md` at most
  32 KB. `--rewrite` adds checks for an uncommitted rewrite: the archive must end with the committed
  `AGENT.md`, and no journal line may disappear.
- `swarm memory archive agent NODE` appends the committed `AGENT.md` verbatim to `AGENT.archive.md`
  before you rewrite it.
- `swarm memory archive journals NODE|all` is a dry run; `--apply` moves entries older than 14 days
  (keeping at least the newest 3, and fitting 32 KB) to `journal.archive.md`, verified against the
  original. It skips a journal changed in the last hour unless you pass `--mine`.
- `swarm memory backup` bundles the committed refs of the memory repo into the upkeep backend at
  `memory/tstack-<date>.bundle` and `memory/tstack-latest.bundle`. The memory repo often has no remote,
  so this is its off-machine copy (the nightly run also tars it).

`git-hooks/pre-commit` (enabled on the memory repo by `install.sh`) rejects commits that touch more than
one node, nodes over the size limits, and journals that lose lines that are not in the archive.
Overrides: `TSTACK_MULTI=1`, `TSTACK_SIZE=1`, `TSTACK_JOURNAL_EDIT=1`. The `/curate` skill uses these
commands.

## Rules for agents

`install.sh` copies `claude/rules/*.md` to `~/.claude/rules/` and the `plain-english` skill to
`~/.claude/skills/`, filling in paths and model names from your settings. It leaves files it did not
write alone, and the files carry a `tstack` marker so a rerun updates them.

- `plain-english.md`: answer first, short sentences, no invented labels; applies to replies and docs.
- `long-tasks.md`: run anything over about 10 minutes detached (tmux or nohup) with a log under
  `<data_dir>/logs/`.
- `subagent-harness.md`: the main agent plans and verifies, subagents do bounded work in parallel.
