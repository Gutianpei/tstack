---
name: curate
description: Tidy tstack memory nodes — check notes against live code, resolve contradictions, archive old journal entries, turn finished efforts into HISTORY rows. Use when the user runs /curate [node|all].
disable-model-invocation: true
---

# curate [node|all]

For each node (all nodes under `{{MEMORY_DIR}}/nodes/` except `example` when "all"):

1. Read `AGENT.md`, `journal.md`, `HISTORY.md`.
2. **Check against reality:** for each pointer (path, branch, PR, command) in `AGENT.md`, confirm it
   still exists. Mark stale ones or fix them.
3. **Resolve contradictions** between `AGENT.md` and newer journal entries; the newer evidence wins.
4. **Finished efforts** (shipped, merged, abandoned, parked) leave `AGENT.md` and become one
   `HISTORY.md` row: `| Dates | Tried | Verdict and why | journal dates |`, under ~300 characters.
5. **Archive with the tools, never by hand.**
   - Journal: `swarm memory archive journals <node>` is a dry run that shows what moves (entries older
     than 14 days, keeping at least the newest 3). Add `--apply` to write it (`--mine` when the last
     change to `journal.md` was your own session's; it refuses files changed in the last hour).
   - `AGENT.md`: before rewriting it, run `swarm memory archive agent <node> --reason "<why>"`. It appends
     the committed text verbatim to `AGENT.archive.md`. Then rewrite `AGENT.md` to live work only.
6. Keep `AGENT.md` under 12 KB (4 KB if the node had no work for 30 days). Verify:
   `swarm memory check <node> --rewrite` (add `--idle` for the 4 KB limit). It fails when `AGENT.md` or
   `HISTORY.md` break the format or size policy, when the archive does not end with the committed
   `AGENT.md`, or when any journal line was dropped instead of moved.
7. Commit each node separately (the memory repo's pre-commit hook rejects multi-node commits and journal
   lines that were deleted instead of archived):
   `git -C {{MEMORY_DIR}} add -- nodes/<node>/ && git -C {{MEMORY_DIR}} commit -m "<node>: curate" -- nodes/<node>/`.
8. When all nodes are done, `swarm memory check all` and `swarm memory backup` (needs an upkeep backend,
   see `docs/upkeep.md`; skip if none is configured).

Report one line per node: what changed, and anything that needs {{USER}}'s decision.
