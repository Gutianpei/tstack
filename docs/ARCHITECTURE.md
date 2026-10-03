# tstack architecture

tstack is a `swarm` command (Python 3.9+, standard library only) plus text files: role prompts,
agent instructions, a memory-repo template, herdr settings. It runs on macOS and Linux.

## Layout

```
bin/swarm               thin launcher: finds ../lib next to itself (symlinks resolved), runs tstack.cli
lib/tstack/
  util.py               SwarmError, KIT path, time stamps, atomic writes, flock-based file locks
  config.py             settings: load, merge with DEFAULTS, validate; `swarm config`
  agent.py              the agent CLI (default Claude Code): start args, idle states, context use
  herdr.py              every herdr call (workspaces, tabs, panes, agents, pop-ups)
  tasks.py              task folders: init, new, set, verdict, list, check; herdr pop-ups (notices.json)
  messaging.py          say (safe prompt) and wake (waiter that reports when an agent finishes)
  managers.py           add/remove-manager, up, start-worker, restart
  commands.py           the list of modules that register subcommands
  cli.py                argparse: builds `swarm` from commands.MODULES, maps SwarmError to exit 2
  server.py             feature: gpus, run, reserve, release, worktree, storage, disk (docs/server.md)
  gpu.py                GPU discovery backends (nvidia-smi, none), reservation and lock files
  background.py         run a swarm subcommand in the background: tmux or nohup backends
  routine.py            feature: the routine loop and the pilot log (docs/routine.md)
  night.py              feature: the night runner (docs/routine.md)
  slack.py              feature: the Slack bridge and its chat Transport (docs/slack.md)
  upkeep.py             feature: backup, restore, weekly review, inventory, suggest-managers, memory (docs/upkeep.md)
  upkeep_snapshot.py    what a backup reads: git checkouts, memory and data folders, unique files
  upkeep_store.py       where a backup goes: disk and s3 stores
  history.py            reads Claude Code transcripts for `swarm suggest-managers`
  memory_tools.py       memory node checks, archiving, bundles for `swarm memory`
roles/                  first prompts, handoffs and memory-node templates for managers
claude/                 CLAUDE.md block, rules and skills that install.sh puts in ~/.claude
memory-template/        starting memory repo; git-hooks/pre-commit guards it
herdr/config.toml       herdr sections install.sh appends when missing
docs/                   this file, one page per optional layer, ADAPTING.md, a sample night-jobs file
SWARM.md                rules every manager and worker follows
install.sh              idempotent installer (bash 3.2+), `--dry-run` to preview
```

Dependencies point one way: `util` <- `config` <- `agent` <- `herdr` <- `tasks` <- `messaging`
<- `managers`. Feature modules may import any of them; core modules never import a feature. The
routine reaches `server` and `slack` only through `importlib`, so either can be removed.

## Adding a command module

1. Write `lib/tstack/<feature>.py`. Use `config.section("<feature>")` for settings, `herdr.call`
   for herdr, `tasks.find_task`/`tasks.locked` for task files, `SwarmError` for expected failures.
2. Give it a `register(sub)` that adds its subcommands:

   ```python
   def register(sub):
       p = sub.add_parser("gpus", help="show GPU reservations")
       p.add_argument("--all", action="store_true")
       p.set_defaults(run=cmd_gpus)   # cmd_gpus(args) -> int exit code
   ```

3. In `lib/tstack/commands.py`, uncomment (or add) its `from . import <feature>` line and its
   entry in `MODULES`. Those lines are kept apart by blank lines so parallel branches merge cleanly.
4. Optional hooks: `config.VALIDATORS.append(fn)` (fn(settings) -> problems) for
   `swarm config check`; `tasks.CHECKS.append(fn)` (fn() -> problems) for `swarm check`.
5. Defaults for the feature's settings go in `config.DEFAULTS["<feature>"]` and
   `config.example.json`; path keys go in `config.PATH_KEYS`. Mac-only commands need a fallback.

## Settings

File: `$TSTACK_CONFIG`, else `~/.config/tstack/config.json`. It holds only what you change; every
missing key or section falls back to `config.DEFAULTS` (mirrored in `config.example.json`).
`$SWARM_DIR` overrides `data_dir`. Both variables are passed on to panes that swarm creates.

| Section | Keys |
|---|---|
| top level | `user`, `workspace` (`~/work`), `memory_dir` (`~/tstack-memory`), `data_dir` (`~/tstack-data`), `bin_dir` (`~/.local/bin`) |
| `agent` | `cmd` (executable, `claude`), `herdr_kind` (`herdr agent start --kind`, `claude`), `manager_model`, `worker_model`, `model_flag` (`--model`), `name_flag` (`-n`), `extra_args`, `exit_command` (`/exit`), `idle_states` (herdr statuses meaning ready), `context_regex` + `context_reports` (`used`/`left`: how to read context use off the screen), `start_timeout_ms` |
| `swarm` | `overall`, `managers` (written by `swarm add-manager`), `max_workers` |
| `server` | `scratch_dir` (`""` = `<workspace>/scratch`), `ckpts_dir` (`""` = `<workspace>/ckpts`), `worktree_root` (`""` = `workspace`), `gpu_backend`, `gpu_command`, `gpu_locks`, `gpu_busy_mib`, `reserve_max_hours`, `disk_min_free_gb`, `disk_paths`; see [server.md](server.md) |
| `routine` | loop timing, morning and context restarts, quiet time, context source, notices, scheduled prompts, disk alert, and `night` (night runner); see [routine.md](routine.md) |
| `slack` | `channel`, `allowed_users`, `token_file`, `api_base`, `backend` (chat transport), `poll_seconds`, `notices` (`bridge`/`routine`/`off`), `say_wait_minutes`, `runner`; see [slack.md](slack.md) |
| `upkeep` | `backend` (`disk`/`s3`), `disk_path`, `s3_uri`, `aws_profile`, `backup_time`, `roots`, `skip`, `protected`, size limits, retention (`keep_daily`, `keep_weekly`, `prune`), `weekly_day`, `review_owner`, `runner`, `history_dir`; see [upkeep.md](upkeep.md) |

Another agent CLI: set `agent.cmd` and `agent.herdr_kind` to a kind herdr supports, adjust the
flags and `exit_command`, and set `context_regex` to whatever its screen prints about context.
`install.sh` writes the `~/.claude` instructions, skills and permissions only when
`herdr_kind` is `claude`. [ADAPTING.md](ADAPTING.md) covers this and the other swap points.
