# Getting started with tstack

This guide takes you from nothing to a working swarm, then through each optional layer. Follow
the numbered steps in order. Steps that differ between a Mac and a Linux server say so.

Contents: [1. Prerequisites](#1-prerequisites) · [2. Install](#2-install) ·
[3. Memory only](#3-memory-only) · [4. The first manager](#4-the-first-manager) ·
[5. The first task, end to end](#5-the-first-task-end-to-end) ·
[6. Optional layers](#6-optional-layers) · [7. Daily life](#7-daily-life) ·
[8. Troubleshooting](#8-troubleshooting) · [9. Uninstall](#9-uninstall)

## 1. Prerequisites

You need `git`, Python 3.9 or newer, Claude Code and herdr. Check what you already have:

```bash
git --version
python3 --version          # 3.9 or newer
claude --version
herdr --version
```

### macOS

1. `git` and `python3` come with the Xcode command line tools: `xcode-select --install`. The
   system `python3` on current macOS is new enough; Homebrew's (`brew install python`) also works.
2. Install Claude Code by following its docs
   (<https://docs.anthropic.com/en/docs/claude-code>), then run `claude` once and log in.
3. Install herdr by following its docs (<https://herdr.dev>). Its installer at the time of writing:
   `curl -fsSL https://herdr.dev/install.sh | sh`.
4. Optional: `brew install tmux` for background loops, `brew install awscli` for S3 backups.

### Linux server (including headless, over SSH)

1. Packages (Debian/Ubuntu shown; use `dnf` or `pacman` elsewhere):

   ```bash
   sudo apt install git python3 tmux     # tmux is optional but useful on a server
   ```

2. Install Claude Code by following its docs. Run `claude` once and log in. Over SSH there is no
   browser on the server: Claude Code prints a URL. Open it on your laptop, sign in, and paste the
   code it shows back into the SSH session.
3. Install herdr by following its docs (<https://herdr.dev>), as on macOS.
4. Make sure `~/.local/bin` is on your `PATH` (the installer links `swarm` there, and many
   installers put `claude` and `herdr` there too). Add this to `~/.bashrc` (or `~/.zshrc`) if
   `echo $PATH` does not show it, then open a new shell:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

5. herdr keeps running on the server after you disconnect. Start it with `herdr` inside the SSH
   session; next time, `herdr` again attaches to the same session. herdr can also attach from your
   laptop with `herdr --remote <ssh-target>`; see its docs.
6. GPUs: if the server has NVIDIA GPUs, `nvidia-smi` (shipped with the driver) is all tstack
   needs.

## 2. Install

1. Clone the kit. Any folder works; this guide uses `~/work/tstack`.

   ```bash
   mkdir -p ~/work && git clone <this repo> ~/work/tstack
   cd ~/work/tstack
   ```

2. Preview the install. Nothing is written.

   ```bash
   ./install.sh --dry-run
   ```

   You see one line per planned change, ending with `Dry run: nothing written.`

3. Install.

   ```bash
   ./install.sh
   ```

   It is safe to run again at any time, for example after `git pull` or after editing a file in
   the kit. It:

   - writes `~/.config/tstack/config.json` from `config.example.json` if it is missing;
   - creates the memory repo `~/tstack-memory` with an example node, and points its git hooks at
     the kit's `git-hooks/`;
   - creates the data folder `~/tstack-data` (tasks, handoffs, logs) and writes the storage rule;
   - links `~/.local/bin/swarm` to the kit;
   - writes the `tstack` block into `~/.claude/CLAUDE.md`, and the skills and rules into
     `~/.claude/skills/` and `~/.claude/rules/`;
   - allows `swarm`, `herdr agent list|get|read` and `git -C <memory repo>` in
     `~/.claude/settings.json`, so agents can run them without a prompt each time;
   - adds the herdr sections you lack to `~/.config/herdr/config.toml` (pop-ups and sidebar
     rows) and reloads herdr. Sections you already have are left alone.

   It ends with `Done. Next: swarm config check, then swarm add-manager overall --role overall --dry-run`.
   A `WARNING: ... is not on PATH` line means step 4 of the Linux prerequisites applies to you too.

4. Check the settings:

   ```bash
   swarm config check
   ```

   You see `<path>/config.json: ok`, or a list of problems. `swarm config show` prints every
   setting with defaults filled in, and `swarm config path` prints the file to edit. Edit only the
   keys you want to change; missing keys use the defaults. The ones to look at first:

   | Key | Default | Set it to |
   |---|---|---|
   | `user` | your login name | the name agents call you |
   | `workspace` | `~/work` | the folder holding your repos |
   | `agent.manager_model`, `agent.worker_model` | `opus`, `sonnet` | the Claude models you want |
   | `swarm.max_workers` | `4` | workers per manager at once |

   Rerun `./install.sh` after changing `user`, `memory_dir` or `data_dir`, since those are
   written into the agent instructions.

## 3. Memory only

The memory layer works without herdr or the swarm. Each project gets a node: a folder with
`AGENT.md` (live state, at most 12 KB), `journal.md` (dated entries, append only) and
`HISTORY.md` (one row per finished effort).

```bash
cd ~/tstack-memory
cp -r nodes/example nodes/myproj        # then rewrite the three files for your project
# add a row for myproj to README.md's map of nodes
git add -- nodes/myproj README.md && git commit -m "myproj: new node" -- nodes/myproj README.md
```

Then in any Claude Code session: `/resume myproj`, or just work in that project's folder; the
global instructions tell the agent to find the matching node, read it at the start and update it
at the end. `/curate all` tidies nodes that grew too long. `swarm memory check all` reports nodes
over the size limits.

The pre-commit hook refuses commits that touch more than one node, and journals that lose lines.
That is deliberate: many sessions share the repo. See [docs/upkeep.md](docs/upkeep.md#memory-tools).

Not sure which nodes to make? `swarm suggest-managers --days 60` reads your Claude Code history
and suggests nodes and managers.

## 4. The first manager

1. Start herdr (`herdr`) in a terminal and leave it open. On a server, start it inside your SSH
   session.
2. Preview, then create, the overall manager. It routes your requests and runs the swarm; it does
   no project work itself.

   ```bash
   swarm add-manager overall --role overall --dry-run
   swarm add-manager overall --role overall
   ```

   The dry run prints the plan: a memory node `nodes/overall`, a first handoff in
   `~/tstack-data/handoffs/overall.md`, and a herdr workspace `overall` with tabs `manager`,
   `workers` and `tasks`, where Claude starts as agent `overall`.

3. In herdr, open the `overall` workspace. If Claude asks whether you trust the files in
   this folder, answer yes. Claude asks this once per folder; until you answer, herdr shows the
   agent as `blocked`.
4. Add one domain manager per area of work. It owns one or more memory nodes and starts in that
   area's folder:

   ```bash
   swarm add-manager backend --nodes myproj --cwd ~/work/myproj
   ```

`swarm config get swarm` now lists both managers. After a herdr restart, `swarm up` recreates any
missing workspace and manager.

## 5. The first task, end to end

Talk to the overall manager in its `manager` tab:

> Add a `--version` flag to myproj's CLI and a test for it.

What happens, and what you see:

1. The manager runs `swarm new` and routes the task to `backend`. The `tasks` tab in `overall`
   (it runs `swarm list --watch 30`) shows the task under **Open**, then **Running**.
2. `backend` fills in the task file (Reading, Scope, Touches, Acceptance checks), makes a git
   worktree with `swarm worktree add`, and runs `swarm start-worker backend --task <id> --cwd <worktree>`.
   A worker `backend-w1` appears in the `workers` tab of the `backend` workspace. A new worktree
   folder means one more trust dialog; answer it in the worker's pane.
3. The worker does the task and writes `result.md` once: commits, paths, check output. A waiter
   wakes `backend`.
4. `backend` checks the evidence. Then either it runs `swarm set <id> done --verdict "..."` and
   herdr pops up "done", or it needs you: `swarm set <id> waiting-on-you --question "..." --recommend "..."`,
   and herdr pops up the question. Answer in the manager's pane.

Each task is a folder `~/tstack-data/tasks/<date>-<slug>/` with `task.md` and `result.md`. To
watch the same cycle without agents, run it by hand:

```bash
swarm new first-try --owner overall --title "Try the task flow" --request "Make a hello file"
swarm set <id> running                       # <id> is the folder name printed above, e.g. 2026-10-03-first-try
swarm set <id> waiting-on-you --question "Name it hello.txt?" --recommend "yes"
swarm list                                   # the question is at the top
cp ~/tstack-data/templates/result.md ~/tstack-data/tasks/<id>/result.md
swarm set <id> done --verdict "checked by hand"
swarm list --all
```

`swarm set done` refuses until `result.md` exists. Tasks are never deleted; `dropped` (with
`--reason`) is the way to abandon one. The full rules, including what managers may decide alone,
are in [SWARM.md](SWARM.md).

## 6. Optional layers

Each layer is off until you configure or start it. Turn on only what you need.

### Shared servers: GPUs, worktrees, storage, disk

```bash
swarm storage                 # resolved paths and free space
swarm gpus                    # GPUs and reservations; "no GPUs found" on a Mac is fine
```

By default scratch files go to `<workspace>/scratch`, checkpoints and caches to
`<workspace>/ckpts`, and worktrees to `<workspace>/<repo>-wt/<slug>`. On a server with a separate
data disk, point `server.scratch_dir` and `server.ckpts_dir` at it, then run
`swarm storage --write-rule` so agents see the new paths. Agents hold GPUs with
`swarm run --task <id> --gpus 1 -- <command>`. Details: [docs/server.md](docs/server.md).

### The routine (always-on loop)

```bash
swarm routine check           # each manager's state and context use
swarm routine once --dry-run  # what one pass would do now
swarm routine start           # background: tmux if installed, else nohup
```

It restarts managers at 06:00 and when their context passes 60%, sends scheduled prompts, keeps
herdr pop-ups and sidebar labels current, and raises a low-disk alert. It survives closing the
terminal but not a reboot: run `swarm routine start` after logging in, or run it as a service:

- **macOS:** a launchd agent in `~/Library/LaunchAgents/`.
- **Linux:** a `systemd --user` unit, plus `loginctl enable-linger $USER` so it keeps running
  after you log out of SSH.

Both samples are in [docs/routine.md](docs/routine.md#optional-run-the-loop-as-a-service). A
service needs an explicit `PATH` that finds `swarm`, `herdr`, `claude` and `git`; it does not read
your shell profile.

### The night runner

```bash
swarm night run "Run the unit tests in ~/work/myproj and fix what fails" \
  --cwd ~/work/myproj --verify "python3 -m pytest -q" --dry-run
swarm night run "..." --cwd ~/work/myproj --verify "python3 -m pytest -q"
swarm night report            # next morning
```

Jobs start between 22:00 and 07:00 in fresh headless sessions that may edit files but not run
arbitrary commands, unless you widen `routine.night.extra_args`. Details:
[docs/routine.md](docs/routine.md#the-night-runner).

### The Slack bridge

Create a Slack app from the manifest in [docs/slack.md](docs/slack.md#setup), store its bot token
in `~/.config/tstack/slack-token` (mode 600), and set `slack.channel` and `slack.allowed_users`.
Then:

```bash
swarm config check
swarm slack test              # checks the token, posts a hello
swarm slack start
```

The bridge only makes outgoing HTTPS calls, so it works behind NAT and on a laptop. Messages from
allowed users go to the overall manager, `@backend ...` goes to `backend`, and each task that needs
you gets a thread you can answer in.

### Upkeep: backups and the weekly review

```bash
swarm upkeep run-once         # one backup now, to ~/tstack-backups by default
swarm upkeep start            # every night at 03:00
```

It copies what git cannot rebuild (unpushed commits, uncommitted changes, untracked files) plus
the memory and data folders. For an off-machine copy, set `upkeep.disk_path` to another disk or a
synced folder, or `upkeep.backend` to `s3` with `upkeep.s3_uri`. On Sundays it writes a
keep-or-drop review and opens a task for it. Details and the launchd/systemd samples:
[docs/upkeep.md](docs/upkeep.md).

## 7. Daily life

- **Talk to managers, not workers.** Any manager, any time, in its `manager` tab or over Slack.
- **Answer questions when they pop up.** Each one comes with a recommended answer; only its own
  task waits.
- **Look at the list.** `swarm list` shows questions waiting on you at the top. The `tasks` tab in
  `overall` keeps it live.
- **Managers restart, they don't compact.** The routine does it each morning and when context gets
  heavy; by hand it is `swarm restart <name>`. The manager writes a handoff first and the fresh
  session reads it.
- **Keep the pilot log.** After the morning restart the routine asks the overall manager to add
  today's row (five measures, such as how often you had to pass context by hand); over weeks
  `swarm pilot-log show` tells you whether the swarm saves you time.
- **Update the kit** with `git pull && ./install.sh`. Your settings, memory and tasks are kept.
- **A separate test swarm:** `TSTACK_CONFIG=~/tstack-test.json swarm ...` with its own `data_dir`.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `swarm: command not found` | `~/.local/bin` (or your `bin_dir`) is not on `PATH`. Add it to your shell profile and open a new shell. |
| `herdr not found at ...` | Install herdr, put it on `PATH`, or set `HERDR_BIN=/path/to/herdr`. |
| `agent.cmd 'claude' is not on PATH` from `swarm config check` | Install Claude Code or put it on `PATH`; with another CLI, see [docs/ADAPTING.md](docs/ADAPTING.md). |
| An agent shows `blocked` in herdr | A dialog waits in its pane, usually the trust-folder question for a new folder. Answer it there. |
| A manager did not start | `swarm add-manager` prints the exact `herdr agent start ...` command to paste. Check that `claude` starts by hand in that folder and that you are logged in. |
| Messages to an agent never arrive | `swarm say` waits until the agent is idle and you are not typing in its pane. Look away from the pane, or use `--urgent`. |
| A memory commit is refused | The pre-commit hook: one node per commit, size limits, journals append only. Commit nodes separately; `swarm memory check <node>` says what is too big. |
| `another routine loop already runs` | A loop already runs (tmux, nohup or a service). `swarm routine status`, then `swarm routine stop` before starting another way. |
| Routine or bridge works by hand but not as a service | The service's `PATH` misses `herdr`, `claude` or `git`. Set it in the unit or plist. |
| Services stop when you log out of a Linux server | `loginctl enable-linger $USER`. |
| `no GPUs found` | Expected without NVIDIA GPUs. On a GPU server, check that `nvidia-smi` runs. |
| `swarm slack test` says no token | Write the token to `slack.token_file` or export `SLACK_BOT_TOKEN`. tmux and services do not see your shell's variables; use the file. |
| Pop-ups or sidebar labels missing | Your herdr config needs `[ui.toast] delivery = "herdr"` and the sidebar rows from the kit's `herdr/config.toml`. install.sh adds missing sections but never changes ones you have. |

Logs are in `~/tstack-data/logs/`. `swarm check` validates task files and reservations.

## 9. Uninstall

Stop the background parts, then remove what install wrote. Your memory repo, tasks and backups are
yours; keep or delete them.

```bash
swarm routine stop; swarm night stop; swarm slack stop; swarm upkeep stop
swarm remove-manager <name>          # for each manager; notes and handoffs stay
rm ~/.local/bin/swarm
rm -r ~/.claude/skills/resume ~/.claude/skills/curate ~/.claude/skills/plain-english
rm ~/.claude/rules/long-tasks.md ~/.claude/rules/plain-english.md ~/.claude/rules/subagent-harness.md
```

Then by hand:

- delete the block between `<!-- tstack:begin` and `<!-- tstack:end -->` in `~/.claude/CLAUDE.md`;
- remove the `Bash(swarm:*)`, `Bash(herdr agent ...)` and `Bash(git -C <memory repo>:*)` entries
  from `permissions.allow` in `~/.claude/settings.json`;
- remove the tstack sections from `~/.config/herdr/config.toml` if you do not want them;
- remove any launchd plists or systemd units you added;
- delete `~/.config/tstack/`, and, if you no longer want them, `~/tstack-data`, `~/tstack-memory`
  and `~/tstack-backups`.
