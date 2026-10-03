"""GPU discovery and reservations for `swarm gpus|run|reserve|release` (commands live in server.py).

One owner per GPU. Each GPU has a reservation file `gpu-<n>.json` and a lock file `gpu-<n>.lock`
under <data_dir>/reservations/, and every claim or release is appended to log.jsonl there.

- kind "reserve": held for some hours with no command (`swarm reserve`); live until `expires`.
- kind "run": held while a command runs (`swarm run`); live exactly while its lock file is held.
  The command inherits the lock fd, so the GPUs stay held even if the swarm process dies.

A GPU is free when it has no live reservation, its lock is free and its compute processes use less
than server.gpu_busy_mib. Expired and stale reservations count as free; `swarm check` reports them.

Outside jobs that need whole GPUs and do not use swarm take one of the server.gpu_locks files with
an exclusive flock. Every `swarm run` holds all of them shared while it runs, and `swarm run` and
`swarm reserve` refuse while one is held exclusively.

GPU discovery sits behind a backend (server.gpu_backend). To add one, write a class with
`name` and `read() -> GpuState` and add it to BACKENDS.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import config, tasks
from .util import SwarmError, now_stamp, write_atomic

CLAIMABLE = tasks.ACTIVE
SMI_TIMEOUT = 20
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def settings(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return config.section("server", values if values is not None else config.load())


def busy_mib(values: Optional[Dict[str, Any]] = None) -> int:
    return int(settings(values).get("gpu_busy_mib") or 1024)


# --- backends -------------------------------------------------------------------------


@dataclass
class Gpu:
    """One GPU; the numbers are None when the backend cannot say."""
    index: int
    uuid: str = ""
    used_mib: Optional[int] = None
    total_mib: Optional[int] = None
    utilization: Optional[int] = None


@dataclass
class Process:
    """One compute process on one GPU."""
    gpu: int
    pid: int
    used_mib: int
    name: str


@dataclass
class GpuState:
    """What a backend saw. `error` is set when the backend exists but did not answer."""
    gpus: List[Gpu] = field(default_factory=list)
    processes: List[Process] = field(default_factory=list)
    note: str = ""
    error: str = ""


def as_int(text: str) -> Optional[int]:
    try:
        return int(float(text))
    except ValueError:
        return None


class NvidiaSmi:
    """Default backend: `nvidia-smi` CSV queries. server.gpu_command names the executable."""

    name = "nvidia-smi"

    def __init__(self, values: Dict[str, Any]):
        self.command = str(settings(values).get("gpu_command") or "nvidia-smi")

    def rows(self, query: str) -> List[List[str]]:
        try:
            done = subprocess.run([self.command, query, "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=SMI_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            raise SwarmError(f"{self.command} did not answer: {error}")
        if done.returncode != 0:
            raise SwarmError(f"{self.command} exited {done.returncode}: {(done.stderr or done.stdout).strip()}")
        return [[cell.strip() for cell in line.split(",")] for line in done.stdout.splitlines() if line.strip()]

    def read(self) -> GpuState:
        if not shutil.which(self.command):
            return GpuState(note=f"{self.command} is not on PATH")
        try:
            gpu_rows = self.rows("--query-gpu=index,uuid,memory.used,memory.total,utilization.gpu")
            app_rows = self.rows("--query-compute-apps=gpu_uuid,pid,used_memory,process_name")
        except SwarmError as error:
            return GpuState(error=str(error))
        gpus = [Gpu(int(row[0]), row[1], as_int(row[2]), as_int(row[3]), as_int(row[4]))
                for row in gpu_rows if len(row) >= 5 and as_int(row[0]) is not None]
        gpus.sort(key=lambda gpu: gpu.index)
        index_of = {gpu.uuid: gpu.index for gpu in gpus if gpu.uuid}
        processes = [Process(index_of[row[0]], int(row[1]), as_int(row[2]) or 0, ", ".join(row[3:]))
                     for row in app_rows
                     if len(row) >= 4 and row[0] in index_of and as_int(row[1]) is not None]
        return GpuState(gpus, processes, "" if gpus else f"{self.command} lists no GPUs")


class NoGpus:
    """For machines without GPUs, or to switch the GPU commands off."""

    name = "none"

    def __init__(self, values: Dict[str, Any]):
        pass

    def read(self) -> GpuState:
        return GpuState(note="server.gpu_backend is none")


BACKENDS = {NvidiaSmi.name: NvidiaSmi, NoGpus.name: NoGpus}


def backend(values: Optional[Dict[str, Any]] = None) -> Any:
    values = values if values is not None else config.load()
    name = str(settings(values).get("gpu_backend") or NvidiaSmi.name)
    if name not in BACKENDS:
        raise SwarmError(f"unknown server.gpu_backend {name!r}; known: {', '.join(BACKENDS)}")
    return BACKENDS[name](values)


def read_gpus(values: Optional[Dict[str, Any]] = None) -> GpuState:
    return backend(values).read()


def need_gpus(state: GpuState) -> None:
    """Refuse a claim on a machine where the backend sees no GPU."""
    if state.error:
        raise SwarmError(f"cannot read the GPUs: {state.error}")
    if not state.gpus:
        raise SwarmError(f"no GPUs found ({state.note}); this machine cannot run or reserve GPU work")


# --- lock files -------------------------------------------------------------------------


def reservations_dir() -> Path:
    path = config.data_dir() / "reservations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reservation_file(index: int) -> Path:
    return reservations_dir() / f"gpu-{index}.json"


def lock_file(index: int) -> Path:
    return reservations_dir() / f"gpu-{index}.lock"


def open_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def try_lock(handle: int, operation: int) -> bool:
    try:
        fcntl.flock(handle, operation | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def lock_held(path: Path) -> bool:
    handle = open_lock(path)
    try:
        if try_lock(handle, fcntl.LOCK_EX):
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        return True
    finally:
        os.close(handle)


HELD_OUTSIDE = "held by an outside job"


def lock_state(path: Path) -> str:
    """`free`, `shared by swarm runs` or `held by an outside job`."""
    handle = open_lock(path)
    try:
        for operation, answer in ((fcntl.LOCK_EX, "free"), (fcntl.LOCK_SH, "shared by swarm runs")):
            if try_lock(handle, operation):
                fcntl.flock(handle, fcntl.LOCK_UN)
                return answer
        return HELD_OUTSIDE
    finally:
        os.close(handle)


def outside_locks(values: Optional[Dict[str, Any]] = None) -> List[Tuple[str, Path]]:
    """server.gpu_locks as (name, path), sorted by name."""
    locks = settings(values).get("gpu_locks") or {}
    return sorted((str(name), Path(str(value)).expanduser().absolute()) for name, value in locks.items())


def outside_holders(values: Optional[Dict[str, Any]] = None) -> List[str]:
    return [f"an outside job holds the {name} lock {path} exclusively"
            for name, path in outside_locks(values) if lock_state(path) == HELD_OUTSIDE]


@contextlib.contextmanager
def state_lock() -> Iterator[None]:
    """Hold reservations/.lock around one read-decide-write of the reservation files."""
    handle = open_lock(reservations_dir() / ".lock")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


# --- reservation files ------------------------------------------------------------------


class Blocked(Exception):
    """GPUs or an outside lock are taken; `swarm run --wait` retries, everything else refuses."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class Slot:
    """One GPU's claim: its reservation file, whether its lock is held, and the resulting state."""
    index: int
    data: Optional[Dict[str, Any]]
    malformed: bool
    locked: bool
    busy_mib: int = 0
    state: str = ""

    @property
    def kind(self) -> str:
        return str((self.data or {}).get("kind") or "")

    @property
    def task(self) -> str:
        return str((self.data or {}).get("task") or "")


def read_reservation(index: int) -> Tuple[Optional[Dict[str, Any]], bool]:
    """The reservation and whether its file is unreadable or malformed."""
    try:
        data = json.loads(reservation_file(index).read_text())
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    return (data, False) if isinstance(data, dict) else (None, True)


def expired(data: Dict[str, Any]) -> bool:
    """Whether a reservation's expiry has passed; a broken stamp counts as passed."""
    if not data.get("expires"):
        return False
    try:
        return datetime.fromisoformat(str(data["expires"])) <= datetime.now().astimezone()
    except ValueError:
        return True


def make_slot(index: int, used: int = 0, threshold: int = 1024) -> Slot:
    data, malformed = read_reservation(index)
    slot = Slot(index, data, malformed, lock_held(lock_file(index)), used)
    if data is not None:
        if slot.kind == "run":
            slot.state = "reserved" if slot.locked else "stale"
        else:
            slot.state = "expired" if expired(data) else "reserved"
    elif slot.locked:
        slot.state = "held by an unknown process"
    elif malformed:
        slot.state = "stale"
    else:
        slot.state = "busy" if used >= threshold else "free"
    return slot


def survey(state: GpuState, values: Optional[Dict[str, Any]] = None) -> List[Slot]:
    threshold = busy_mib(values)
    return [make_slot(gpu.index, sum(p.used_mib for p in state.processes if p.gpu == gpu.index), threshold)
            for gpu in state.gpus]


def describe(data: Dict[str, Any]) -> str:
    who = f"task {data.get('task') or '?'} (owner {data.get('owner') or '?'})"
    if data.get("kind") == "run":
        return f"a run of {who}, pid {data.get('pid')}"
    return f"a reservation of {who} until {str(data.get('expires') or '?')[:16].replace('T', ' ')}"


def blocker(slot: Slot, task_id: str, allow_busy: bool, threshold: int) -> str:
    """Why `task_id` cannot claim this GPU; empty when it can."""
    own = slot.kind == "reserve" and slot.task == task_id
    if slot.state == "reserved" and not own:
        return f"held by {describe(slot.data or {})}"
    if slot.locked and slot.kind != "run":
        return "locked by an unknown process"
    if not allow_busy and slot.busy_mib >= threshold and slot.state != "reserved":
        return f"busy: {slot.busy_mib} MiB in use with no reservation"
    return ""


def group_reasons(reasons: Dict[int, str]) -> List[str]:
    grouped: Dict[str, List[str]] = {}
    for index in sorted(reasons):
        if reasons[index]:
            grouped.setdefault(reasons[index], []).append(str(index))
    return [f"GPU {','.join(items)} is {reason}" if len(items) == 1 else f"GPUs {','.join(items)} are {reason}"
            for reason, items in grouped.items()]


def parse_list(text: str) -> List[int]:
    """`3,4` -> [3, 4]."""
    parts = [part for part in text.replace(" ", "").split(",") if part]
    if not parts or not all(part.isdigit() for part in parts):
        raise SwarmError(f"--gpu takes comma-separated GPU indexes, for example 3,4; not {text!r}")
    return sorted({int(part) for part in parts})


def pick(slots: Sequence[Slot], task_id: str, wanted: Optional[List[int]], count: Optional[int],
         allow_busy: bool, threshold: int) -> List[int]:
    """The GPUs to claim, or Blocked naming what stands in the way."""
    by_index = {slot.index: slot for slot in slots}
    if wanted is not None:
        missing = [str(index) for index in wanted if index not in by_index]
        if missing:
            raise SwarmError(f"GPU {','.join(missing)} does not exist; this machine has "
                             f"{','.join(str(i) for i in sorted(by_index))}")
        reasons = {index: blocker(by_index[index], task_id, allow_busy, threshold) for index in wanted}
        if any(reasons.values()):
            raise Blocked(group_reasons(reasons))
        return list(wanted)
    if not count or count < 1:
        raise SwarmError("--gpus must be at least 1")
    if count > len(slots):
        raise SwarmError(f"--gpus {count}: this machine has only {len(slots)} GPU(s)")
    reasons = {slot.index: blocker(slot, task_id, allow_busy, threshold) for slot in slots}
    free = sorted(index for index, reason in reasons.items() if not reason)
    if len(free) < count:
        raise Blocked([f"only {len(free)} of {count} GPUs are free", *group_reasons(reasons)])
    return free[:count]


def write_reservation(index: int, task: tasks.Task, by: str, kind: str, pane: Optional[str],
                      command: Optional[str], expires: Optional[str], created: Optional[str] = None) -> None:
    data = {"gpu": index, "task": task.id, "owner": task.meta.get("owner", ""), "by": by, "kind": kind,
            "pane": pane, "pid": os.getpid(), "command": command, "created": created or now_stamp(),
            "expires": expires}
    write_atomic(reservation_file(index), json.dumps(data, indent=2) + "\n")


def log_event(event: str, index: int, task_id: str, by: str, kind: str, **extra: Any) -> None:
    record = {"at": now_stamp(), "event": event, "gpu": index, "task": task_id, "by": by, "kind": kind, **extra}
    with (reservations_dir() / "log.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def log_replacement(slot: Slot, by: str, claimant: str) -> None:
    """Log a claim over an expired or stale reservation and refresh the task that lost it."""
    event = {"expired": "replaced-expired", "stale": "replaced-stale"}.get(slot.state)
    if event:
        log_event(event, slot.index, slot.task, by, slot.kind)
    if slot.task and slot.task != claimant:
        with contextlib.suppress(SwarmError, OSError, ValueError):
            note_on_task(slot.task, by, None)


def reservation_files() -> List[Tuple[int, Path]]:
    found = []
    for path in reservations_dir().glob("gpu-*.json"):
        index = as_int(path.stem.split("-", 1)[1])
        if index is not None:
            found.append((index, path))
    return sorted(found)


def task_gpus(task_id: str) -> List[int]:
    """GPUs a task holds live, whatever the kind."""
    return [index for index, _ in reservation_files()
            if (slot := make_slot(index)).task == task_id and slot.state == "reserved"]


def note_on_task(task_id: str, by: str, line: Optional[str]) -> None:
    """Refresh the task's `gpus` front-matter key and log `line`. Callers hold state_lock."""
    with tasks.locked():
        task = tasks.find_task(task_id)
        task.meta["gpus"] = ",".join(str(index) for index in task_gpus(task_id))
        if line:
            task.log(by, line)
        else:
            task.meta["updated"] = now_stamp()
        task.save()


def claimable(query: str) -> tasks.Task:
    task = tasks.find_task(query)
    if task.status not in CLAIMABLE:
        raise SwarmError(f"{task.id} is {task.status}; only {', '.join(CLAIMABLE)} tasks may hold GPUs")
    return task


def pane_of(given: Optional[str]) -> Optional[str]:
    return given or os.environ.get("HERDR_PANE_ID") or os.environ.get("TMUX_PANE") or None


def check_reservations() -> List[str]:
    """For `swarm check`: reservations that are malformed, expired, stale or held by a closed task."""
    root = config.data_dir() / "reservations"
    if not root.exists():
        return []
    known = {task.id: task for task in tasks.load_tasks()}
    problems = []
    for index, path in reservation_files():
        slot = make_slot(index)
        name = f"reservations/{path.name}"
        if slot.data is None:
            problems.append(f"{name}: malformed JSON")
            continue
        task = known.get(slot.task)
        if task is None:
            problems.append(f"{name}: unknown task {slot.task!r}")
        elif slot.state == "reserved" and task.status not in CLAIMABLE:
            problems.append(f"{name}: held by {task.id}, which is {task.status}")
        if slot.state == "expired":
            problems.append(f"{name}: expired at {slot.data.get('expires')}; `swarm release --task {slot.task}`")
        if slot.state == "stale":
            problems.append(f"{name}: run of pid {slot.data.get('pid')} whose lock is free (stale); "
                            f"`swarm release --task {slot.task}`")
    return problems


# --- running a command on claimed GPUs -------------------------------------------------


@dataclass
class Claim:
    """The GPUs a run holds, the lock fds it keeps open, and the reservations it displaced."""
    gpus: List[int]
    handles: List[int]
    restore: Dict[int, Dict[str, Any]]


def claim_for_run(task: tasks.Task, by: str, pane: Optional[str], wanted: Optional[List[int]],
                  count: Optional[int], allow_busy: bool, command: str, values: Dict[str, Any]) -> Claim:
    """Take the outside locks shared and the GPU locks exclusive, then write the run files."""
    handles: List[int] = []
    threshold = busy_mib(values)
    with state_lock():
        try:
            for name, path in outside_locks(values):
                handle = open_lock(path)
                handles.append(handle)
                if not try_lock(handle, fcntl.LOCK_SH):
                    raise Blocked([f"an outside job holds the {name} lock {path} exclusively"])
            state = read_gpus(values)
            need_gpus(state)
            slots = survey(state, values)
            chosen = pick(slots, task.id, wanted, count, allow_busy, threshold)
            by_index = {slot.index: slot for slot in slots}
            for index in chosen:
                handle = open_lock(lock_file(index))
                handles.append(handle)
                if not try_lock(handle, fcntl.LOCK_EX):
                    raise Blocked([f"GPU {index} was just claimed by another process"])
            restore = {}
            for index in chosen:
                slot = by_index[index]
                log_replacement(slot, by, task.id)
                if slot.state == "reserved" and slot.kind == "reserve":
                    restore[index] = dict(slot.data or {})
                write_reservation(index, task, by, "run", pane, command, None)
                log_event("run-start", index, task.id, by, "run", command=command)
            note_on_task(task.id, by, None)
        except BaseException:
            for handle in handles:
                os.close(handle)
            raise
    return Claim(chosen, handles, restore)


def release_run(task_id: str, by: str, claim: Claim, code: int) -> None:
    """Remove the run files (restoring a displaced own reservation), log, then drop the locks."""
    with state_lock():
        for index in claim.gpus:
            data, _ = read_reservation(index)
            mine = data or {}
            if mine.get("task") == task_id and mine.get("kind") == "run" and mine.get("pid") == os.getpid():
                kept = claim.restore.get(index)
                if kept and not expired(kept):
                    write_atomic(reservation_file(index), json.dumps(kept, indent=2) + "\n")
                else:
                    with contextlib.suppress(FileNotFoundError):
                        reservation_file(index).unlink()
            log_event("run-end", index, task_id, by, "run", exit_code=code)
        with contextlib.suppress(SwarmError, OSError, ValueError):
            note_on_task(task_id, by, None)
    for handle in claim.handles:
        os.close(handle)


def run_child(command: Sequence[str], gpus: str, handles: Sequence[int]) -> int:
    """Start the command with the GPUs visible and the locks inherited; wait; return its exit code."""
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": gpus, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
    try:
        child = subprocess.Popen(list(command), env=env, pass_fds=tuple(handles))
    except OSError as error:
        raise SwarmError(f"cannot start {command[0]!r}: {error}")

    def forward(number: int, frame: Any) -> None:
        with contextlib.suppress(OSError):
            child.send_signal(number)

    previous = {number: signal.signal(number, forward) for number in FORWARDED_SIGNALS}
    try:
        status = child.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 128 - status if status < 0 else status
