# tstack memory ({{USER}})

Personal, cross-repo notes so a fresh Claude Code session can continue a project without a
long brief. This repo is advisory: it points at the real sources (repos, PRs, code) and holds
what those don't — decisions and why, status, gotchas, runbooks. Verify pointers before
relying on them. Never copy this content into shared repos.

## Node files

| File | Holds | Limit |
|------|-------|-------|
| `AGENT.md` | live work only: status, next steps, decisions in force, pointers, runbook | 12 KB |
| `journal.md` | dated entries `## YYYY-MM-DD — title`, newest last, append-only | about 14 days |
| `HISTORY.md` | one table row per finished effort: dates, tried, verdict and why, journal dates | about 8 KB |
| `journal.archive.md`, `AGENT.archive.md` | text moved out of the two files above, verbatim | none |

Rules: move, never delete. Commit only your own node's folder:
`git add -- nodes/<node>/ && git commit -m "<node>: ..." -- nodes/<node>/`.
Add a node: copy `nodes/example/` to `nodes/<name>/`, rewrite the files, add a row below.

## The map

| Node | Repo / area | What it is |
|------|-------------|------------|
