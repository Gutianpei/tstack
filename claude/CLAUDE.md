<!-- tstack:begin (managed by {{KIT_DIR}}/install.sh; edit there, then rerun it) -->
# tstack — personal memory and swarm

## Memory
{{USER}}'s personal, cross-repo memory lives at `{{MEMORY_DIR}}` (its `README.md` maps every node).
It is advisory: verify pointers against the live repo. If the task maps to no node, ignore this.

- **On start:** pick the node from the working folder and the task (or `/resume <node>`). Read
  `nodes/<node>/AGENT.md` and the last 2 entries of `journal.md`. "What did we try before?" →
  `grep -ri '<topic>' {{MEMORY_DIR}}/nodes/*/HISTORY.md`.
- **On finishing substantive work:** append a dated entry to the node's `journal.md` (what changed,
  why, next) and re-distill `AGENT.md` to live work only (12 KB max). A finished effort becomes one
  row in `HISTORY.md`. Move, never delete: cut text goes verbatim to `AGENT.archive.md` /
  `journal.archive.md`.
- **Commit only your node:** `git -C {{MEMORY_DIR}} add -- nodes/<node>/ && git -C {{MEMORY_DIR}} commit -m "<node>: ..." -- nodes/<node>/`.
  Never `commit -a` / `add -A` there; other sessions share the checkout.
- Pointers, not copies. Never write memory content into shared team repos.

## Swarm (only when you are a swarm agent)
If your first prompt names you a swarm manager or worker, follow `{{KIT_DIR}}/SWARM.md`.
- Talk to another agent only with `swarm say <agent> "<your name>: <message>"`, never raw `herdr agent prompt`.
- Workers: do exactly the task file, write `result.md` once, then stop. Never message other workers.
- When another agent's message arrives while {{USER}} is talking to you, answer it briefly, then
  say in one line where {{USER}}'s thread stands.

## Storage (shared servers)
Scratch, checkpoints, caches and worktrees have fixed homes, never `/tmp`: read
`{{DATA_DIR}}/rules/storage.md` before large writes or a new worktree. `swarm storage` shows the paths.

## Long jobs
Run anything over ~10 minutes in the background with a log
(`nohup <cmd> > {{DATA_DIR}}/logs/<name>.log 2>&1 &`), not in the foreground of an agent pane.
<!-- tstack:end -->
