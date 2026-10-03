# Storage on shared servers

Written by `swarm storage --write-rule` from the `server` settings; edit those, then rerun it.
Applies on dev servers and containers where the root filesystem (including `/tmp`) is small or
ephemeral: filling it can evict the container, and it is wiped on restart. On a laptop it is
still a good habit.

## Where files go

| Kind | Put it in |
|---|---|
| Scratch code, one-off scripts, logs, temp files | `{{SCRATCH_DIR}}/` (export `TMPDIR={{SCRATCH_DIR}}/tmp` for tools that use it) |
| Checkpoints, model weights, datasets | `{{CKPTS_DIR}}/<project>/` |
| Download and build caches | `{{CKPTS_DIR}}/cache/`: set `HF_HOME`, `TORCH_HOME`, `PIP_CACHE_DIR`, `XDG_CACHE_HOME` there |
| Git worktrees for workers | `{{WORKTREE_ROOT}}/<repo>-wt/<slug>`, made with `swarm worktree add <repo> <slug> --task <id>` |
| Durable repos and projects | `{{WORKSPACE}}/` |
| Swarm tasks, logs, GPU reservations | `{{DATA_DIR}}/` (managed by `swarm`; do not hand-edit) |

## Do not

- Do not write large or long-lived files to `/tmp`, `/var/tmp` or anywhere else on the root
  filesystem. Small sockets and short logs are fine.
- Do not point caches, logs or build output at `/tmp`; override the tool's setting instead.
- Do not create git worktrees under `/tmp` or inside another checkout.
- Do not assume `/tmp` survives a restart.

## Before a big write

Run `swarm disk` (or `swarm storage`) to see free space per path. If a path is marked LOW, clean
up your own old outputs first, or stop and ask instead of filling the disk. Remove a worker's
worktree with `swarm worktree remove <slug>` once its branch is merged or pushed.
