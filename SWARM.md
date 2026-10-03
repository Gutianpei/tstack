# Swarm rules

The swarm is a set of Claude Code agents in herdr. One user, one overall manager, a few domain
managers, short-lived workers. Its job: you give direction and answer questions; the agents carry
context, do the work, and track it. You never copy context between chats by hand.

## Who is who

| Agent | Lives | Does |
|---|---|---|
| Overall manager (`overall`) | herdr workspace `overall`, tab `manager` | Turns requests into tasks, routes them to the owning manager, checks overlap across managers, runs the swarm. No domain work. |
| Domain manager (`<name>`) | workspace `<name>`, tab `manager` | Owns one area (one or more memory nodes). Plans tasks, starts workers, checks their evidence, asks you questions. |
| Worker (`<manager>-w<n>`) | workspace `<manager>`, tab `workers`, 2x2 grid | One fresh session per task. Does the task file, writes `result.md` once, stops. |
| Subagents | inside any session (Agent tool) | Bounded searches and edits. Never start agents or ask the user. |

You can talk to any manager directly at any time.

## How a task moves

1. You ask a manager for something.
2. It runs `swarm new <slug> --owner <manager> --title "..." --request "<your words>"`.
3. The owner fills in Reading, Scope, Touches, Acceptance checks in `task.md`, sets `running`.
4. The owner starts a worker: `swarm start-worker <manager> --task <id> [--cwd <worktree>]`.
   A background waiter wakes the manager when the worker finishes, writes `result.md`, or gets
   stuck on a permission dialog.
5. The manager checks the evidence in `result.md` (commits, paths, check output), then
   `swarm set <id> done --verdict "..."`, or asks you with `waiting-on-you`.
6. The `tasks` tab in the `overall` workspace runs `swarm list --watch 30` and shows a herdr
   pop-up for each task that becomes waiting, done or dropped. Running work stays silent.

Statuses: `open`, `running`, `waiting-on-you` (needs `--question` and `--recommend`), `done`
(needs `result.md` and `--verdict`), `dropped` (needs `--reason`). No task is ever deleted.

Who writes what: only the owning manager edits `task.md`; only the worker writes `result.md`,
once; a manager above may add a line with `swarm verdict`. Managers never rewrite or inflate a
worker's claims.

## Decision rights

Managers decide alone:
- Technical approach, parameters and architecture inside the agreed scope.
- Workers, subagents, tests; local worktrees, branches and commits; retries.
- Their own workers' technical questions.

Act first, tell the user at once:
- Stopping a runaway job; restarting a crashed local dev service.

Ask the user first, one question at a time, with a recommended answer:
- Scope or direction changes, and subjective visual verdicts.
- Spending money or new cloud compute.
- Pushing branches; opening, commenting on or merging PRs; deploys.
- Deleting data, schema changes, new dependencies.
- Sending code, data or logs to a third party.

`swarm set <id> waiting-on-you --question "..." --recommend "..."`. An open question pauses only
its own task. If a request is ambiguous, state your reading and wait.

## Shared resources

- **Git:** one agent per checkout. Every worker that edits code gets its own worktree
  (`swarm worktree add <repo> <slug> --task <id>`, or plain `git worktree add`). Stage paths
  explicitly. Never `git checkout` or `git stash` in a shared tree.
- **Ports:** dev services use distinct ports; the overall manager assigns them. Record them in Touches.
- **GPUs:** hold them for a task with `swarm run --task <id> --gpus N -- <cmd>` or `swarm reserve`;
  never pick GPUs by hand on a shared machine. `swarm gpus` shows who holds what.
- **Storage:** scratch, checkpoints, caches and worktrees go where `swarm storage` says (the rule is
  `<data_dir>/rules/storage.md`), never `/tmp`. Check `swarm disk` before large writes.
- **Long jobs:** over ~10 minutes, run them in the background with a log file, never in the
  foreground of an agent pane.

## Sessions and restarts

- Workers get one session per task. Free the pane after: `swarm say <worker> /exit`.
- Managers run long, but restart instead of compacting once their context gets heavy (around
  60%) or each morning. Before a restart a manager writes `<data_dir>/handoffs/<name>.md` (live
  state, running jobs, open questions, next step) and commits its memory node.
- `swarm restart <name>` does it all: asks for the handoff, quits Claude, starts a fresh session
  that reads the handoff. A manager restarting itself writes the handoff first, then runs
  `nohup swarm restart --handoff-written <name> >/dev/null 2>&1 &`.

## Messaging

herdr types prompts into a pane like a keyboard. Two prompts close together, or one sent while you
type, merge into garbage. So agents use `swarm say <agent> "<sender>: <message>"`: it waits until the
target is idle and you are not using its pane (or the screen has been still 5 minutes), and queues
messages to the same agent. `--urgent` skips the wait on your focus. If it gives up after 30
minutes, put the note in the task file. Workers never message each other.
