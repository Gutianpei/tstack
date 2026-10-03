# tstack

tstack runs a team of coding agents for one engineer. It is a `swarm` command (Python 3.9+,
standard library only) plus text files: agent instructions, role prompts, a memory-repo template
and herdr settings. The agents are Claude Code sessions by default, hosted in
[herdr](https://herdr.dev), a terminal workspace manager for coding agents. It runs on macOS and on
Linux servers. MIT licensed.

Two layers do the main work, and the rest are optional:

1. **Memory.** A private git repo of notes with one folder ("node") per project. Every agent
   session reads the matching node when it starts and updates it when it finishes. You stop
   writing long briefs, and nothing is lost when a session ends.
2. **Swarm.** Long-lived manager agents, one herdr workspace each. They turn your requests into
   task folders, hand each task to a short-lived worker agent, check the evidence the worker
   leaves, and ask you only the questions that need you.

On top of these: GPU and storage rules for shared servers, a Slack bridge, an always-on routine
that keeps managers fresh, an overnight runner, and nightly backups.

## A day with it

- **Morning.** The routine restarted every manager at 06:00 in a fresh session. Each one read its
  handoff and memory node. The overnight report waits in `swarm night report`.
- **You ask.** In the `overall` manager's pane: "the ingest job drops rows with empty ids; fix it
  and add a test." The manager opens a task (`swarm new`), routes it to the `backend` manager, and
  that manager starts a worker in its own git worktree.
- **You do something else.** When the worker finishes, a waiter wakes its manager. The manager
  reads `result.md` (commits, paths, check output), then marks the task done or asks you.
- **A question.** herdr pops up "backend needs you: push the branch?" with a recommended answer.
  You answer in the manager's pane, or in the Slack thread from your phone.
- **Evening.** You queue two unattended jobs with `swarm night run`. At 03:00 upkeep backs up every
  unpushed commit, uncommitted change and the memory repo.

## Layers

| Layer | What it adds | Needs | Optional? | Docs |
|---|---|---|---|---|
| Memory | Notes repo, `/resume` and `/curate` skills, global agent instructions | git, Claude Code | No (works alone, without herdr) | [GETTING_STARTED.md](GETTING_STARTED.md#3-memory-only) |
| Swarm | Managers, workers, task folders, safe messaging, restarts | herdr | Yes | [SWARM.md](SWARM.md) |
| Server | GPU reservations and locks, worker worktrees, storage rule, disk check | `nvidia-smi` for GPUs | Yes; without GPUs only the GPU commands refuse | [docs/server.md](docs/server.md) |
| Routine | Morning and context restarts, scheduled prompts, pop-ups, disk alert, pilot log | tmux optional | Yes | [docs/routine.md](docs/routine.md) |
| Night runner | Queue of unattended prompts or tasks, morning report | tmux optional | Yes | [docs/routine.md](docs/routine.md#the-night-runner) |
| Slack bridge | Talk to the swarm from a Slack channel, task notices in threads | a Slack app (bot token) | Yes | [docs/slack.md](docs/slack.md) |
| Upkeep | Nightly backup to disk or S3, restore, weekly review, history scanner, memory tools | `aws` CLI for S3 | Yes | [docs/upkeep.md](docs/upkeep.md) |

Optional layers do nothing until you start or configure them.

## Requirements

- macOS or Linux, `bash` 3.2+, `git`, Python 3.9+.
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code), logged in. Another agent CLI that
  herdr supports can replace it; see [docs/ADAPTING.md](docs/ADAPTING.md).
- [herdr](https://herdr.dev) for the swarm and everything built on it. Memory works without it.
- Optional: `tmux` (background loops; `nohup` is the fallback), `nvidia-smi` (GPU commands), the
  `aws` CLI (S3 backups), a Slack app (Slack bridge).

## Quick start

```bash
git clone <this repo> ~/work/tstack && cd ~/work/tstack
./install.sh --dry-run          # preview every change
./install.sh                    # safe to rerun after editing anything in the kit
swarm config check
swarm add-manager overall --role overall --dry-run
swarm add-manager overall --role overall
```

Then open herdr, go to the `overall` workspace, and talk to the agent in the `manager` tab. The full
walk-through, for macOS and for a Linux server over SSH, is [GETTING_STARTED.md](GETTING_STARTED.md).

## Repo layout

```
bin/swarm            the command (a thin launcher for lib/tstack)
lib/tstack/          the Python package; one module per layer (docs/ARCHITECTURE.md)
claude/              CLAUDE.md block, rules and skills that install.sh puts in ~/.claude
roles/               first prompts, handoffs and memory-node templates for managers
memory-template/     the starting memory repo
git-hooks/           pre-commit guard for the memory repo
herdr/config.toml    herdr settings install.sh adds when missing
docs/                architecture, one page per optional layer, adapting guide
SWARM.md             the rules every manager and worker follows
config.example.json  every setting with its default
install.sh           idempotent installer, --dry-run to preview
```

## Commands

Every command takes `-h`.

| Command | Does |
|---|---|
| `swarm init` | Create the data folder and templates (install does this) |
| `swarm config show\|get\|check\|path` | Show, read or validate settings |
| `swarm new <slug> --owner X --title T --request "..."` | New task folder; prints its path |
| `swarm set <id> <status>` | `waiting-on-you` needs `--question --recommend`; `done` needs `result.md` and `--verdict`; `dropped` needs `--reason` |
| `swarm verdict <id> "..." --by <name>` | Add a verdict line to a task |
| `swarm list [--all] [--owner X] [--watch 30]` | Tasks by status, questions waiting on you on top |
| `swarm check` | Validate task files, reservations and other modules' state |
| `swarm add-manager <name> [--role overall] [--nodes a,b] [--cwd DIR]` | Memory node, settings entry, handoff, herdr workspace |
| `swarm remove-manager <name>` | Drop it from settings; notes and handoff stay |
| `swarm up` | After a herdr restart: recreate workspaces and managers |
| `swarm start-worker <mgr> --task <id> [--cwd DIR]` | Worker in the manager's `workers` tab, plus a waiter |
| `swarm restart <mgr>` | Handoff, quit, fresh session that reads the handoff |
| `swarm say <agent> "msg" [--urgent]` | Prompt an agent once it is idle and you are not typing in it |
| `swarm wake <watched> --to <agent>` | Wait until one agent finishes, then tell another |
| `swarm gpus` / `run` / `reserve` / `release` | GPU state and reservations (server) |
| `swarm worktree add\|list\|remove` | Git worktrees for workers (server) |
| `swarm storage` / `swarm disk` | Where big files go; free space (server) |
| `swarm routine start\|stop\|status\|logs\|check\|once\|loop` | The always-on loop (routine) |
| `swarm pilot-log show\|add` | One row per day on whether the swarm saves you time (routine) |
| `swarm night run\|run-file\|status\|logs\|stop\|report` | Overnight unattended jobs (night runner) |
| `swarm slack start\|stop\|status\|logs\|test\|run\|post` | The Slack bridge |
| `swarm upkeep run-once\|start\|stop\|status\|restore\|weekly\|inventory` | Backups and the weekly review (upkeep) |
| `swarm suggest-managers` | Suggest managers and memory nodes from Claude Code history (upkeep) |
| `swarm memory check\|archive\|backup` | Keep memory nodes small without losing text (upkeep) |

## Where things live

| What | Default path | Setting |
|---|---|---|
| This kit | wherever you cloned it | |
| Settings | `~/.config/tstack/config.json` | `$TSTACK_CONFIG` |
| Memory repo | `~/tstack-memory` | `memory_dir` |
| Tasks, handoffs, logs, state | `~/tstack-data` | `data_dir` (or `$SWARM_DIR`) |
| Your repos | `~/work` | `workspace` |
| Worker worktrees | `<workspace>/<repo>-wt/<slug>` | `server.worktree_root` |
| Scratch and checkpoints | `<workspace>/scratch`, `<workspace>/ckpts` | `server.scratch_dir`, `server.ckpts_dir` |
| Backups | `~/tstack-backups` | `upkeep.disk_path` or `upkeep.s3_uri` |
| The `swarm` link | `~/.local/bin/swarm` | `bin_dir` |
| Agent instructions | the `tstack` block in `~/.claude/CLAUDE.md`, `~/.claude/rules/`, `~/.claude/skills/` | |
| herdr settings | `~/.config/herdr/config.toml` | `$HERDR_CONFIG_PATH` |

Every setting and its default is in [config.example.json](config.example.json) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#settings). Your settings file holds only what you
change. `TSTACK_CONFIG=/other/config.json swarm ...` runs a separate test swarm; panes swarm
creates inherit it.

## More

- [GETTING_STARTED.md](GETTING_STARTED.md): install, first manager, first task, each optional
  layer, troubleshooting, uninstall.
- [SWARM.md](SWARM.md): roles, how a task moves, decision rights, messaging, restarts.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, settings, adding a command.
- [docs/ADAPTING.md](docs/ADAPTING.md): running it at another company, with another agent CLI,
  GPU system, storage, backup store, chat tool or service manager.

License: [MIT](LICENSE).
