---
name: resume
description: Resume a tstack memory node so a fresh session continues prior work. Use when the user runs /resume, says "resume <topic>", or asks to pick up where a past session left off. The node map is {{MEMORY_DIR}}/README.md.
---

# resume <node>

1. **Pick the node:** the argument; else infer from the working folder and task; else read
   `{{MEMORY_DIR}}/README.md`. If still ambiguous, ask.
2. **Load:** `{{MEMORY_DIR}}/nodes/<node>/AGENT.md` (with its Runbook) and the last few entries of
   `journal.md`. Read `HISTORY.md` when past attempts matter.
3. **Verify:** confirm the top pointers (paths, branches, PRs) still exist before relying on them.
4. **Report and continue:** a 3 to 5 line summary (mission, status, next step, any live-ops note),
   then continue the task.

On finishing: dated `journal.md` entry, re-distilled `AGENT.md` (live work only, 12 KB max),
finished efforts as one `HISTORY.md` row each, removed text appended verbatim to
`AGENT.archive.md`. Then commit only this node:
`git -C {{MEMORY_DIR}} add -- nodes/<node>/ && git -C {{MEMORY_DIR}} commit -m "<node>: ..." -- nodes/<node>/`.
