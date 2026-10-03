<!-- tstack rule (installed by {{KIT_DIR}}/install.sh; edit the kit copy, then rerun it) -->
# Subagent harness (always on)

The main agent in a session orchestrates. It plans, decides, dispatches bounded work to subagents,
verifies what comes back, and iterates until the goal is met. This is separate from swarm managers and
workers (see `{{KIT_DIR}}/SWARM.md`): subagents run inside one session, they never start agents, message
other agents or ask the user.

## Rules

1. **The main agent owns the plan.** It owns architecture decisions and the final verification.
   Subagents execute bounded, independent units: a search, a refactor of one module, a test run.
2. **Parallelize independent work.** Launch independent subagents in one turn, not one after another.
3. **Pick the lightest agent that can do the job.** Read-only searches go to a search-only agent.
   Edits and debugging go to a general-purpose agent. Quick lookups of a known file need no subagent.
4. **Models.** Managers run `{{MANAGER_MODEL}}`; workers and subagents that write code run
   `{{WORKER_MODEL}}` (the `agent.manager_model` and `agent.worker_model` settings). If a lighter model
   makes a mistake on one kind of task, use the stronger model for that kind from then on, and record the
   decision in your memory node's `AGENT.md`.
5. **Self-contained prompts.** A subagent sees none of your conversation. Give exact absolute paths,
   line numbers, the repository and branch, what to change or find, and the exact format of the reply
   (a summary of findings, a diff, a yes/no with evidence).
6. **Verify, do not trust.** Check subagent output against the real code and running services. When a
   subagent fails or reports an unexpected state, send a targeted correction at once.
7. **Short writing stays with you.** Journal lines, status lines and handoff notes are written by the
   main agent. Long writing (new docs, a distilled `AGENT.md`, a pull request description) may go to a
   subagent given the source facts.
8. **No wasted turns.** Finish quickly; do not narrate options you will not take.
