<!-- tstack rule (installed by {{KIT_DIR}}/install.sh; edit the kit copy, then rerun it) -->
# Long-running tasks (always on)

A command that may run longer than about 10 minutes must not run in the foreground of an agent pane.
Agents run in herdr panes. Restarting herdr, closing a laptop lid, a dropped network or a closed terminal
ends processes started in a pane. A detached process survives them, and its log keeps high-volume
output out of the pane you use to watch agents.

## What counts

Model training and long evaluations, dataset conversions, index builds, media renders, large downloads
and multi-gigabyte syncs. When unsure, assume it is long.

## Start it detached, with a log

Prefer `tmux` when it is installed; use `nohup` otherwise. Name the job and send all output to a log
file under `{{DATA_DIR}}/logs/`:

```bash
mkdir -p {{DATA_DIR}}/logs
# tmux available:
tmux new-session -d -s <job> '<command> 2>&1 | tee {{DATA_DIR}}/logs/<job>.log'
# no tmux:
nohup <command> > {{DATA_DIR}}/logs/<job>.log 2>&1 &
```

Do not put logs in `/tmp`; the system clears it.

## Check, read, stop

```bash
tmux has-session -t <job> 2>/dev/null && echo running || echo stopped   # tmux
tail -n 40 {{DATA_DIR}}/logs/<job>.log                                  # progress
tmux capture-pane -pt <job> -S -50                                      # scrollback, tmux only
tmux kill-session -t <job>                                              # stop (tmux)
kill <pid>                                                              # stop (nohup; note the pid from `$!`)
```

## Rules

- Name every job, so another session can find it.
- Report the log path to the user when you start the job.
- A job that outlives your session needs a line in the task file or the handoff: its name, its log and
  what finishing looks like.
- Never block on a job that takes hours. Poll its log, or let the swarm waiter wake you.
