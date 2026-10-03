# Adapting tstack to your company

tstack is meant to be copied and changed. Everything company-specific sits behind a setting or a
small class, so most adaptations are a settings change or one new class. This page lists the
swap points, from the most common to the least, and what to keep.

Before changing code, read [ARCHITECTURE.md](ARCHITECTURE.md): one module per layer, core modules
never import a feature, and every feature module registers its commands in `commands.py`. A whole
layer you do not want can be removed by deleting its import and `MODULES` entry there.

## The agent CLI (`agent` section)

Default: Claude Code. herdr starts and watches the agents, so another CLI must be a kind herdr
supports (`herdr agent start --kind <kind>`). Everything tstack knows about the CLI is in
`lib/tstack/agent.py`, read from these settings:

| Key | Claude Code value | What to set for another CLI |
|---|---|---|
| `cmd` | `claude` | its executable |
| `herdr_kind` | `claude` | the herdr kind for it |
| `manager_model`, `worker_model`, `model_flag` | `opus`, `sonnet`, `--model` | its model names and flag; empty `model_flag` passes no model |
| `name_flag` | `-n` | a flag that names the session, or `""` |
| `extra_args` | `[]` | flags every session gets (permission mode, for example) |
| `exit_command` | `/exit` | what typed into the pane quits it (used by restarts) |
| `idle_states` | `["idle", "done"]` | herdr statuses that mean "ready for a prompt" |
| `context_regex`, `context_reports` | Claude Code's "context left" line, `left` | a regex with one number group that matches what the CLI prints about context, and whether that number is `used` or `left` |

Other places that assume Claude Code, and what to do about each:

- **Agent instructions.** `install.sh` writes `~/.claude/CLAUDE.md`, rules, skills and permissions
  only when `herdr_kind` is `claude`. For another CLI, give it the text of `claude/CLAUDE.md`
  (filled in), `SWARM.md` and `<data_dir>/rules/storage.md` in whatever file it reads at start, and
  allow it to run `swarm` without asking.
- **Routine context reading.** `routine.context_source` `auto` falls back to Claude Code
  transcripts (`routine.transcript_dir`). Set it to `screen` for another CLI so only
  `agent.context_regex` is used. Also set `routine.question_regex` to match the CLI's dialogs.
- **Night runner.** Set `routine.night.headless_args`, `extra_args` and `resume_flag` to the CLI's
  one-shot mode. The runner reads a JSON object with `result`, `session_id` and `is_error` from the
  last output line, and falls back to plain text.
- **History scanner.** `swarm suggest-managers` reads Claude Code transcripts (`history.py`). For
  another CLI, write a reader that yields the same session records, or skip the command.

## GPUs (`server.gpu_backend`)

`lib/tstack/gpu.py` finds GPUs through a backend: a class with `name`, `__init__(values)` and
`read() -> GpuState` (lists of `Gpu` and `Process`, plus `note` or `error`). Two ship:
`nvidia-smi` and `none`. To support another vendor or a cluster API:

1. Write the class in `gpu.py` (for example by parsing `rocm-smi` or calling your scheduler).
2. Add it to `BACKENDS` and set `server.gpu_backend` to its `name`.

Reservations, lock files, `swarm run` and `swarm check` work the same with any backend. If a
batch scheduler (Slurm, Kubernetes) owns the GPUs, set `gpu_backend` to `none`, have agents submit
jobs through the scheduler, and say so in the storage rule or `SWARM.md`. Outside jobs that need
whole GPUs can take a lock in `server.gpu_locks`; see [server.md](server.md).

## Storage paths

All paths are settings, so this is configuration, not code:

- `workspace`, `memory_dir`, `data_dir`, `bin_dir` at the top level.
- `server.scratch_dir` and `server.ckpts_dir` (empty: under `workspace`), `server.worktree_root`
  (empty: `workspace`), and `server.disk_paths` for extra mounts to watch.

After changing them run `swarm storage --write-rule`. The rule agents read is the template
`claude/rules/storage.md`; edit it for your machines (a network filesystem that must not hold
checkpoints, a quota, a cache variable your stack uses). It may use `{{SCRATCH_DIR}}`,
`{{CKPTS_DIR}}`, `{{WORKTREE_ROOT}}`, `{{DATA_DIR}}` and `{{WORKSPACE}}`.

## Backup store (`upkeep.backend`)

`lib/tstack/upkeep_store.py` has a `Store` base class with `describe`, `put`, `get_text`,
`fetch`, `list`, `folders` and `delete` (optionally `delete_prefix`). `disk` and `s3` ship; the
`s3` one shells out to the `aws` CLI, so S3-compatible stores work through your AWS config. To
add GCS, Azure Blob, rsync or an internal object store: subclass `Store`, add it to `STORES`, build
it in `open_store()` in `upkeep.py`, and set `upkeep.backend`. Test with
`swarm upkeep run-once --dest <scratch location>` first. Details: [upkeep.md](upkeep.md#swapping-the-backend).

## Chat transport (`slack.backend`)

All chat traffic in `lib/tstack/slack.py` goes through a `Transport` with five methods: `auth`,
`history`, `replies`, `post`, `react`. The shipped `SlackWebAPI` uses a bot token and polling, so
nothing is exposed to the internet. For Mattermost, Matrix, Discord, Teams or a company chat
gateway, write a class with the same methods, return it from `transport()` for a new
`slack.backend` value, and keep the bridge logic as it is. Details:
[slack.md](slack.md#swapping-the-backend).

To post task notices somewhere else (email, a pager), add a consumer to
`<data_dir>/state/notices.json` as described in [routine.md](routine.md#swapping-a-backend).

## Background runner and services

The routine loop and the night runner start through `lib/tstack/background.py`: a backend is a
class with `start(name, argv, log)`, `running(name)`, `stop(name)` and `where(name)`; `tmux` and
`nohup` ship, chosen by `routine.backend` and `routine.night.backend`. Add a class to `BACKENDS` for
another runner. The Slack bridge (`slack.runner`) and upkeep (`upkeep.runner`) have their own
`nohup`/`tmux` code for now.

For a service manager, do not add a backend: run the foreground command from the service instead
(`swarm routine loop`, `swarm slack run`, `swarm upkeep run-once` on a timer). launchd and
`systemd --user` samples are in [routine.md](routine.md#optional-run-the-loop-as-a-service),
[slack.md](slack.md#running-it-at-login-optional-samples) and [upkeep.md](upkeep.md#macos-and-linux-differences).

## The terminal host (herdr)

Every herdr call goes through `lib/tstack/herdr.py` (`herdr.call` plus helpers for agents,
workspaces, tabs, panes, screens and pop-ups); `managers.py` and `routine.py` also call
`herdr.call` directly for a few pane and workspace commands. Replacing herdr with plain tmux or
another multiplexer is possible but is the largest change: you need agent start with a kind, agent
status (idle, working, blocked), screen reads, typed prompts, and pop-ups. Point `HERDR_BIN` at a
fake script to test anything that calls herdr without a live one.

## What to keep

These are the parts that make the swarm work; change them only on purpose.

- **Tasks are folders on disk** with `task.md` (written by the owning manager) and `result.md`
  (written once by the worker). No database, no server, and nothing is deleted.
- **Decision rights** in [SWARM.md](../SWARM.md): what managers decide alone and what goes to you.
  Tighten them for your company (deploys, data access, third-party services), do not drop them.
- **Evidence before done.** `swarm set done` needs `result.md` and a verdict.
- **Safe messaging.** `swarm say` waits for an idle agent and for you to look away, because typed
  prompts that collide become garbage.
- **Restart, do not compact.** Managers write a handoff and start fresh.
- **Memory rules.** One node per project, `AGENT.md` small and current, journals append only, text
  moved to archives rather than deleted, one node per commit.
- **One-way dependencies** between modules, so a layer can be removed or replaced alone.

## Checklist for a company fork

1. Set `user`, paths and models in `config.example.json` to your defaults.
2. Edit `claude/rules/storage.md` and the decision rights in `SWARM.md` for your policies.
3. Pick the GPU backend, backup store and chat transport; add classes where needed.
4. Replace the Slack app manifest name in [slack.md](slack.md) if you rename the bot.
5. Run `swarm config check`, `swarm -h`, and the install check from
   [GETTING_STARTED.md](../GETTING_STARTED.md) with `HOME=$(mktemp -d)`.
