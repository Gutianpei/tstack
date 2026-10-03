# Shared servers: GPUs, worktrees, storage, disk

The server module (`lib/tstack/server.py`, GPU logic in `lib/tstack/gpu.py`) is for running agents
on a shared dev server or workstation: several agents, one machine, a few GPUs, disks that fill up.
On a laptop without GPUs everything except the GPU claims still works.

| Command | What it does |
|---|---|
| `swarm gpus [--json]` | each GPU's memory, use, state and reservation; the outside GPU locks; big unreserved processes |
| `swarm run --task ID (--gpus N\|--gpu LIST) [--wait S] [--allow-busy] -- CMD` | hold GPUs while `CMD` runs |
| `swarm reserve --task ID (--gpus N\|--gpu LIST) --hours H [--allow-busy]` | hold GPUs for some hours with no command |
| `swarm release --task ID [--gpu LIST]` | give back a task's reservations |
| `swarm worktree add <repo> <slug> [--task ID] [--base REF] [--branch B]` | a git worktree for a worker |
| `swarm worktree list [repo] [--json]` | worktrees swarm made or that sit under the worktree root |
| `swarm worktree remove <slug> [--repo R] [--force]` | remove one; refuses with uncommitted changes |
| `swarm storage [--rule\|--write-rule]` | where scratch, checkpoints, caches and worktrees go, with free space |
| `swarm disk [--json]` | free space per path; exit 1 when any is under `disk_min_free_gb` |

`swarm check` also reports expired reservations, stale runs, reservations of unknown tasks and
reservations still held by done or dropped tasks.

## Setup

Nothing beyond the normal `install.sh`. It writes the storage rule to `<data_dir>/rules/storage.md`
and the CLAUDE.md block points agents at it. Then set the paths for your machine:

```sh
swarm config path                     # the settings file to edit
swarm storage                         # check the resolved paths and free space
swarm storage --write-rule            # rewrite the agent rule after changing paths
swarm gpus                            # check that GPUs are seen
```

- **Linux with NVIDIA GPUs:** needs `nvidia-smi` on `PATH` (it ships with the driver). Nothing else.
- **Linux or macOS without GPUs:** `swarm gpus` prints `no GPUs found (...)` and exits 0; `swarm run`
  and `swarm reserve` refuse with that message and exit 2. Set `server.gpu_backend` to `"none"` to
  say so explicitly.
- **Containers with a small or ephemeral root disk:** point `scratch_dir` and `ckpts_dir` at the
  persistent volume (usually under your home folder or a mounted data disk), not at `/tmp`.

No daemon is involved. For a periodic disk alert, the routine daemon calls `disk_status()` (below);
without it, `swarm disk` from cron works too: `*/30 * * * * swarm disk >/dev/null || <your notifier>`.

## Settings (`server` section)

| Key | Default | Meaning |
|---|---|---|
| `scratch_dir` | `""` | scratch code, one-off scripts, logs, `$TMPDIR`; empty means `<workspace>/scratch` |
| `ckpts_dir` | `""` | checkpoints, weights, datasets; caches under `cache/`; empty means `<workspace>/ckpts` |
| `worktree_root` | `""` | worktrees go to `<worktree_root>/<repo>-wt/<slug>`; empty means the `workspace` |
| `gpu_backend` | `"nvidia-smi"` | how GPUs are discovered: `nvidia-smi` or `none` |
| `gpu_command` | `"nvidia-smi"` | executable the `nvidia-smi` backend runs |
| `gpu_locks` | `{}` | `{"name": "/path/to/file.lock"}`, lock files of outside jobs (below) |
| `gpu_busy_mib` | `1024` | an unreserved GPU whose processes use this much memory is busy |
| `reserve_max_hours` | `72` | upper bound for `swarm reserve --hours` |
| `disk_min_free_gb` | `20` | `swarm disk` warns below this; `0` turns the warning off |
| `disk_paths` | `[]` | extra paths `swarm disk` measures, for example a shared data mount |

`swarm config check` validates these.

## GPU reservations

Each GPU has at most one owner. Only tasks that are `open`, `running` or `waiting-on-you` may claim.

- `swarm run` is the normal path. It claims the lowest free GPUs (or exactly `--gpu 3,4`), runs the
  command with `CUDA_VISIBLE_DEVICES` and `CUDA_DEVICE_ORDER=PCI_BUS_ID`, frees them when the command
  exits, and exits with the command's code. `--wait 600` retries every 10 s for 10 minutes instead
  of refusing at once. A task may `run` on GPUs it has itself reserved; the reservation comes back
  afterwards.
- `swarm reserve` holds GPUs for a worker pane without a command, up to `reserve_max_hours`, and
  prints the `export CUDA_VISIBLE_DEVICES=...` line. Reserving again extends the expiry.
- `swarm release` gives back reservations. A live run frees itself when its command exits.

Files under `<data_dir>/reservations/`:

- `gpu-<n>.json`: the reservation (`task`, `owner`, `by`, `kind` = `reserve`|`run`, `pane`, `pid`,
  `command`, `created`, `expires`).
- `gpu-<n>.lock`: a `run` holds it with an exclusive `flock`; the child command inherits the fd, so
  the GPUs stay held while the command lives, even if `swarm` itself is killed.
- `log.jsonl`: one line per `reserve`, `run-start`, `run-end`, `release`, `replaced-expired`,
  `replaced-stale`.
- `.lock`: serializes every read-decide-write.

States shown by `swarm gpus`: `free`, `busy` (unreserved processes over `gpu_busy_mib`; `--allow-busy`
ignores this), `reserved`, `expired` (a reserve past its time), `stale` (a run whose lock is free:
its process died), `held by an unknown process`. Expired and stale count as free; the next claim
replaces them and `swarm check` reports them until then. The task's front matter gets a `gpus:` key
listing what it holds, and its Log gets reserve/release lines.

### Outside jobs (`gpu_locks`)

Some jobs need whole GPUs and do not go through swarm, for example a script that takes every GPU.
List a lock file for each:

```json
"server": {"gpu_locks": {"full-node": "~/locks/full-node.lock"}}
```

The outside job takes its file with an exclusive lock while it runs, for example
`flock ~/locks/full-node.lock ./train-all.sh` on Linux (macOS has no `flock` binary; use
`python3 -c 'import fcntl,subprocess,sys; f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX); sys.exit(subprocess.call(sys.argv[2:]))' ~/locks/full-node.lock ./train-all.sh`).
Every `swarm run` holds all listed files in shared mode, so the outside job waits until no run is
active. While a file is held exclusively, `swarm run` and `swarm reserve` refuse (`run --wait` waits).
A `reserve` holds no lock after it returns, so outside jobs do not see reservations.

### Swapping the GPU backend

Discovery is a small class in `lib/tstack/gpu.py`:

```python
class MyBackend:
    name = "my-backend"
    def __init__(self, values): ...          # values = merged settings
    def read(self) -> GpuState:              # GpuState(gpus=[Gpu(index, uuid, used_mib, total_mib, utilization)],
        ...                                  #          processes=[Process(gpu, pid, used_mib, name)],
                                             #          note="why the list is empty", error="backend failed")

BACKENDS[MyBackend.name] = MyBackend
```

Return an empty `gpus` list with a `note` when the machine has none, and set `error` when the tool
exists but failed. Then set `server.gpu_backend` to its name. Reservations, locks and commands do not
change. `CUDA_VISIBLE_DEVICES` is what `run` exports; another vendor needs its own variable there
(`run_child` in `gpu.py`).

## Worktrees for workers

Any worker that edits code gets its own git worktree, so parallel workers never share a checkout.

```sh
swarm worktree add myrepo fix-login --task 2026-10-03-fix-login   # prints the path
swarm start-worker <manager> --task 2026-10-03-fix-login --cwd <that path>
```

- `<repo>` is a path or a folder name under `workspace`; a path inside another worktree resolves to
  the main checkout.
- The worktree is `<worktree_root or workspace>/<repo>-wt/<slug>` on a new branch `<slug>` from
  `--base` (default the repo's `HEAD`). `--branch` names another branch; an existing branch is
  checked out instead of created.
- `--task` fills the task's Touches section (`Repo and branch`, `Worktree`) and logs it.
- `swarm worktree list` shows each one with its branch, task, and `[uncommitted changes]` or `[missing]`.
- `swarm worktree remove <slug>` refuses while `git status` shows changes (untracked files count);
  `--force` removes anyway. The branch is kept; delete it yourself once merged.

swarm records what it made in `<data_dir>/worktrees.json`.

## Storage rule

`claude/rules/storage.md` is a template filled from `scratch_dir`, `ckpts_dir`, `worktree_root`,
`workspace` and `data_dir`. `swarm storage --rule` prints it, `swarm storage --write-rule` writes it
to `<data_dir>/rules/storage.md` (install.sh does this), and the CLAUDE.md block tells agents to read
it before large writes. It says: scratch and temp files in `scratch_dir`, checkpoints, weights and
caches (`HF_HOME`, `TORCH_HOME`, `PIP_CACHE_DIR`, `XDG_CACHE_HOME`) in `ckpts_dir`, worktrees under
the worktree root, nothing large on `/tmp` or the root filesystem, and check `swarm disk` first.
With another agent CLI, give it the written file as an instruction.

## Disk status API

```python
from tstack import server
rows = server.disk_status(values)   # values = config.load(); omit to load it
# [{"name": "scratch", "key": "server.scratch_dir", "path": "...", "exists": True,
#   "free_gb": 120.4, "total_gb": 500.0, "used_pct": 75.9, "min_free_gb": 20.0, "low": False}, ...]
low = [row for row in rows if row["low"]]
```

Paths: scratch, checkpoints, worktree root, workspace, data folder, then `disk_paths`; duplicates
are dropped. A missing folder is measured at its nearest existing parent. It never raises for one bad
path (`free_gb` is `None` then).
