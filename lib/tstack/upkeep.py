"""Upkeep: a nightly backup of unpushed work, memory and data, a weekly keep-or-drop review, restore.

    swarm upkeep run-once [--backend disk|s3] [--dest PATH_OR_URI]   one backup now
    swarm upkeep start | stop | status                               the nightly loop (tmux, else nohup)
    swarm upkeep restore [RUN] TARGET [--only REGEX] [--files] [--dry-run]
    swarm upkeep weekly [--days 7] [--no-task] | --mark keep|drop PATH...
    swarm upkeep inventory [--what sizes|worktrees|files|all]        read-only look at what is on disk

Each run writes `runs/<date>/` in the backend (see docs/upkeep.md for the files) and, when it had no
failures, `LATEST`. Files with no other copy go to `files/`, which retention never prunes; the weekly
review decides what stays there. Settings: the `upkeep` section of the settings file.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config, history, memory_tools, tasks
from .upkeep_snapshot import (GIB, MIB, Candidate, Checkout, find_checkouts, git, git_ok, group_of,
                              local_only, make_patch, rel_key, repo_top, skipper, slug_of, strip_remote,
                              tar_folder, tar_paths, tar_untracked, unpushed_bundle, unrel_key)
from .upkeep_store import STORES, DiskStore, S3Store, Store
from .util import SWARM_BIN, SwarmError, log_stamp, today

TMUX_SESSION = "tstack-upkeep"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SETTLE_SECONDS = 2 * 3600
LEDGER = "files/_ledger/uploads.tsv"
LEDGER_COLUMNS = ["date", "path", "bytes", "source", "status", "marked", "group"]
MANIFEST_COLUMNS = ["slug", "path", "repo", "branch", "head", "upstream", "remote", "bundle", "patch",
                    "untracked_tar", "tar_files", "skipped"]
# Settings and scripts outside git, relative to home, that a rebuilt machine needs.
SETTINGS_FILES = [".gitconfig", ".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".tmux.conf",
                  ".config/herdr/config.toml", ".claude/CLAUDE.md", ".claude/settings.json",
                  ".claude/rules", ".claude/skills"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --- settings --------------------------------------------------------------------------------


class Settings:
    def __init__(self, values: Optional[Dict[str, Any]] = None, backend: str = "", dest: str = "") -> None:
        self.values = values if values is not None else config.load()
        self.u = config.section("upkeep", self.values)
        self.home = Path.home()
        self.backend = backend or str(self.u.get("backend") or "disk")
        self.dest = dest
        self.memory = config.path("memory_dir", self.values)
        self.data = config.data_dir(self.values)
        self.config_file = config.config_path()
        roots = self.u.get("roots") or [self.values.get("workspace")]
        self.roots = [Path(str(r)).expanduser() for r in roots]
        self.skip = skipper(self.u.get("skip") or [])
        self.protected = [Path(str(p)).expanduser() for p in self.u.get("protected") or []]
        self.state = self.data / "upkeep"
        self.log_file = self.data / "logs" / "upkeep.log"

    def num(self, key: str, default: float) -> float:
        value = self.u.get(key, default)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def open_store(settings: Settings) -> Store:
    kind = settings.backend
    if kind not in STORES:
        raise SwarmError(f"unknown upkeep backend {kind!r}; known: {', '.join(sorted(STORES))}")
    if kind == "disk":
        return DiskStore(settings.dest or str(settings.u.get("disk_path") or ""))
    if kind == "s3":
        return S3Store(settings.dest or str(settings.u.get("s3_uri") or ""), str(settings.u.get("aws_profile") or ""))
    return STORES[kind](settings.dest)  # a custom backend gets the --dest value, else an empty string


def validate(values: Dict[str, Any]) -> List[str]:
    u = config.section("upkeep", values)
    problems = []
    backend = u.get("backend", "disk")
    if backend not in STORES:
        problems.append(f"upkeep.backend must be one of {', '.join(sorted(STORES))}")
    elif backend == "s3" and not str(u.get("s3_uri") or "").startswith("s3://"):
        problems.append("upkeep.s3_uri must look like s3://bucket/prefix when backend is s3")
    elif backend == "s3" and not shutil.which("aws"):
        problems.append("upkeep.backend is s3 but the aws CLI is not on PATH")
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(u.get("backup_time", "03:00"))):
        problems.append("upkeep.backup_time must be HH:MM")
    if str(u.get("weekly_day", "")) not in WEEKDAYS + [""]:
        problems.append("upkeep.weekly_day must be one of mon..sun, or empty")
    for key in ("roots", "skip", "protected"):
        if not isinstance(u.get(key, []), list):
            problems.append(f"upkeep.{key} must be a list")
    return problems


# --- run log and lock -------------------------------------------------------------------------


class Log:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failures = 0

    def __call__(self, text: str) -> None:
        line = f"{log_stamp()} {text}"
        self.lines.append(line)
        print(line, flush=True)

    def fail(self, text: str) -> None:
        self.failures += 1
        self("FAILED " + text)


@contextlib.contextmanager
def run_lock(settings: Settings) -> Iterator[None]:
    settings.state.mkdir(parents=True, exist_ok=True)
    with open(str(settings.state / "run.lock"), "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SwarmError("another upkeep run is in progress")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_tsv(path: Path, header: List[str], rows: List[List[str]]) -> None:
    clean = [[str(c).replace("\t", " ").replace("\n", " ") for c in row] for row in rows]
    path.write_text("\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in clean), encoding="utf-8")


def read_tsv(text: str) -> List[Dict[str, str]]:
    lines = [line for line in text.splitlines() if line]
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


# --- the nightly run ----------------------------------------------------------------------------


def keep_runs(runs: List[str], daily: int, weekly: int) -> set:
    """The newest `daily` runs, plus the newest run of this ISO week and each of the `weekly` weeks before."""
    ordered = sorted((r for r in runs if DATE.match(r)), reverse=True)
    keep = set(ordered[:daily])
    newest: Dict[Tuple[int, int], str] = {}
    for run in ordered:
        year, week, _ = dt.date.fromisoformat(run).isocalendar()
        newest.setdefault((year, week), run)
    for _, run in sorted(newest.items(), reverse=True)[: weekly + 1]:
        keep.add(run)
    return keep


def settings_paths(settings: Settings) -> List[Path]:
    paths = [settings.home / item for item in SETTINGS_FILES] + [settings.config_file]
    seen, out = set(), []
    for path in paths:
        if path.exists() and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def run_backup(settings: Settings, store: Store, log: Log) -> int:
    """One backup. Returns the number of failures."""
    run = today()
    prefix = f"runs/{run}/"
    settings.state.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="work-", dir=str(settings.state)))
    try:
        for name in ("repos", "checkouts"):
            (work / name).mkdir()

        def put(local: Path, key: str) -> bool:
            ok, detail = store.put(local, prefix + key)
            if not ok:
                log.fail(f"upload {key}: {detail}")
            return ok

        log(f"backup {run} to {store.describe()} ({store.name})")
        checkouts = find_checkouts(settings.roots, settings.skip)
        repos = {item.common for item in checkouts.values()}
        log(f"found {len(checkouts)} checkouts in {len(repos)} repos under {', '.join(map(str, settings.roots))}")

        bundles: Dict[Path, str] = {}
        for common in sorted(repos):
            top = repo_top(common)
            name = slug_of(top, settings.home)
            out = work / "repos" / f"{name}.unpushed.bundle"
            try:
                if unpushed_bundle(common, out):
                    log(f"repo {rel_key(top, settings.home)}: unpushed bundle {out.stat().st_size / MIB:.1f} MiB")
                    if put(out, f"repos/{name}.unpushed.bundle"):
                        bundles[common] = f"repos/{name}.unpushed.bundle"
                    out.unlink()
            except (RuntimeError, OSError) as error:
                log.fail(f"repo {rel_key(top, settings.home)}: {error}")

        manifest: List[List[str]] = []
        for path, item in checkouts.items():
            item.slug = slug_of(path, settings.home)
            try:
                item.tracked = set(x for x in git("ls-files", "-z", cwd=path, check=False).split("\0") if x)
                manifest.append(snapshot_checkout(settings, item, bundles, work, put, log))
            except (RuntimeError, OSError) as error:
                log.fail(f"checkout {rel_key(path, settings.home)}: {error}")
        manifest_file = work / "MANIFEST.tsv"
        write_tsv(manifest_file, MANIFEST_COLUMNS, manifest)
        put(manifest_file, "MANIFEST.tsv")

        for label, folder, skip_dirs in (("memory", settings.memory, ()), ("data", settings.data, ("logs", "upkeep"))):
            if not folder.is_dir():
                log(f"{label}: {folder} does not exist, skipped")
                continue
            out = work / f"{label}.tar.gz"
            count, left_out = tar_folder(folder, out, skip_dirs)
            log(f"{label}: {count} files, {out.stat().st_size / MIB:.1f} MiB"
                + (f"; left out {len(left_out)} credential-like files" if left_out else ""))
            put(out, f"{label}.tar.gz")
            out.unlink()
        out = work / "settings.tar.gz"
        members, left_out = tar_paths(settings_paths(settings), settings.home, out)
        log(f"settings: {len(members)} files" + (f"; left out {len(left_out)} credential-like files" if left_out else ""))
        put(out, "settings.tar.gz")

        found = local_only(settings.roots, settings.home, checkouts, settings.skip,
                           int(settings.num("min_size_mb", 0) * MIB))
        log(f"local-only: {len(found)} files, {sum(c.bytes for c in found) / GIB:.2f} GiB with no other copy")
        listing = work / "local-only.tsv"
        write_tsv(listing, ["path", "bytes", "mtime", "source"],
                  [[c.key, str(c.bytes), str(c.mtime), c.source] for c in found])
        put(listing, "local-only.tsv")
        if settings.u.get("upload_files", True):
            upload_files(settings, store, found, log)

        log(f"done with {log.failures} failure(s)")
        log_file = work / "log.txt"
        log_file.write_text("\n".join(log.lines) + "\n", encoding="utf-8")
        put(log_file, "log.txt")
        if log.failures == 0:
            store.put_text("LATEST", run + "\n")
            prune(settings, store, log)
        return log.failures
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


def snapshot_checkout(settings: Settings, item: Checkout, bundles: Dict[Path, str], work: Path, put: Any,
                      log: Log) -> List[str]:
    path = item.path
    branch = git("symbolic-ref", "--short", "-q", "HEAD", cwd=path, check=False).strip() or "DETACHED"
    head = git("rev-parse", "HEAD", cwd=path, check=False).strip() or "-"
    upstream = ""
    if branch != "DETACHED":
        upstream = git("for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}", cwd=path,
                       check=False).strip()
    remote = strip_remote(git("remote", "get-url", "origin", cwd=path, check=False))
    patch = tar_key = "-"
    diff = work / "checkouts" / f"{item.slug}.patch"
    if make_patch(path, diff):
        if put(diff, f"checkouts/{item.slug}.patch"):
            patch = f"checkouts/{item.slug}.patch"
        diff.unlink()
    tar = work / "checkouts" / f"{item.slug}.untracked.tar.gz"
    if tar_untracked(item, tar):
        if put(tar, f"checkouts/{item.slug}.untracked.tar.gz"):
            tar_key = f"checkouts/{item.slug}.untracked.tar.gz"
        tar.unlink()
    if item.skipped:
        log(f"checkout {rel_key(path, settings.home)}: {len(item.skipped)} untracked files left out "
            "(credential-like or too large)")
    return [item.slug, rel_key(path, settings.home), rel_key(repo_top(item.common), settings.home), branch, head,
            upstream or "-", remote or "-", bundles.get(item.common, "-"), patch, tar_key,
            str(len(item.tarred)), str(len(item.skipped))]


def prune(settings: Settings, store: Store, log: Log) -> None:
    if not settings.u.get("prune", True):
        return
    runs = [r for r in store.folders("runs/") if DATE.match(r)]
    keep = keep_runs(runs, int(settings.num("keep_daily", 7)), int(settings.num("keep_weekly", 4)))
    for run in sorted(set(runs) - keep):
        store.delete_prefix(f"runs/{run}/")
        log(f"retention: removed run {run}")


# --- files with no other copy: upload and ledger ---------------------------------------------------


def load_ledger(store: Store) -> Dict[str, Dict[str, str]]:
    text = store.get_text(LEDGER)
    return {row["path"]: row for row in read_tsv(text)} if text else {}


def save_ledger(store: Store, rows: Dict[str, Dict[str, str]]) -> None:
    text = "\t".join(LEDGER_COLUMNS) + "\n" + "".join(
        "\t".join(str(row.get(c, "")) for c in LEDGER_COLUMNS) + "\n" for _, row in sorted(rows.items()))
    store.put_text(LEDGER, text)


def stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def upload_files(settings: Settings, store: Store, found: List[Candidate], log: Log) -> None:
    """Copy files with no other copy to files/<path>, oldest first, up to `files_cap_gib` per night."""
    cap = int(settings.num("files_cap_gib", 20) * GIB)
    max_file = int(settings.num("max_file_mb", 512) * MIB)
    rows = load_ledger(store)
    existing = {k: v for k, v in store.list("files/").items() if not k.startswith("_ledger/")}
    now, todo, counts = time.time(), [], defaultdict(int)
    for item in found:
        reason = ""
        if rows.get(item.key, {}).get("status") == "drop":
            reason = "ledger-drop"
        elif item.bytes > max_file:
            reason = "over-max-file"
        elif now - item.mtime < SETTLE_SECONDS:
            reason = "recent"
        elif existing.get(item.key) == item.bytes:
            reason = "in-files"
        if reason:
            counts[reason] += 1
        else:
            todo.append(item)
    for reason, count in sorted(counts.items()):
        log(f"upload: skipped {reason}: {count} files")
    todo.sort(key=lambda c: (c.mtime, c.key))
    total, sent, failed, deferred = 0, 0, 0, 0
    for item in todo:
        if total + item.bytes > cap:
            deferred += 1
            continue
        ok, detail = store.put(item.path, "files/" + item.key)
        if not ok:
            failed += 1
            log.fail(f"upload files/{item.key}: {detail}")
            continue
        total += item.bytes
        sent += 1
        old = rows.get(item.key, {})
        keep = old.get("status") == "keep"
        rows[item.key] = {"date": stamp(), "path": item.key, "bytes": str(item.bytes), "source": item.source,
                          "status": "keep" if keep else "new", "marked": old.get("marked", "") if keep else "",
                          "group": item.group}
    if sent or not store.get_text(LEDGER):
        save_ledger(store, rows)
    log(f"upload: copied {sent} files, {total / GIB:.2f} GiB; deferred {deferred} (cap {cap / GIB:g} GiB); "
        f"{failed} failed; ledger has {len(rows)} rows")


# --- restore ---------------------------------------------------------------------------------------


def latest_run(store: Store) -> str:
    text = store.get_text("LATEST")
    if text and text.strip():
        return text.strip()
    runs = sorted(r for r in store.folders("runs/") if DATE.match(r))
    if not runs:
        raise SwarmError(f"no runs in {store.describe()}")
    return runs[-1]


def safe_extract(archive: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(str(archive)) as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise SwarmError(f"archive member {member.name} leaves {dest}")
            if member.issym() or member.islnk():
                if os.path.isabs(member.linkname) or ".." in Path(member.linkname).parts:
                    continue
            tar.extract(member, str(dest))
            count += 1
    return count


def restore_checkout(row: Dict[str, str], run_dir: Path, target: Path) -> int:
    """Rebuild one checkout under `target`. Returns the number of problems."""
    dest = target / row["path"]
    if dest.exists():
        print(f"skip {row['path']}: already exists")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    remote = row["remote"]
    if remote != "-" and git_ok("clone", "-q", remote, str(dest)):
        how = f"cloned {remote}"
    else:
        shutil.rmtree(str(dest), ignore_errors=True)
        git("init", "-q", str(dest))
        how = "new repo (remote missing or unreachable)"
    problems = 0
    if row["bundle"] != "-":
        bundle = run_dir / row["bundle"]
        specs = []
        for line in git("bundle", "list-heads", str(bundle), check=False).splitlines():
            ref = line.split(" ", 1)[1] if " " in line else ""
            if ref.startswith("refs/heads/"):
                specs.append(f"+{ref}:refs/upkeep/heads/{ref[len('refs/heads/'):]}")
            elif ref.startswith("refs/tags/"):
                specs.append(f"+{ref}:{ref}")
            elif ref == "refs/stash":
                specs.append("+refs/stash:refs/upkeep/stash")
        if specs and git_ok("fetch", "-q", str(bundle), *specs, cwd=dest):
            for ref in git("for-each-ref", "--format=%(refname)", "refs/upkeep/heads", cwd=dest).split():
                name = ref[len("refs/upkeep/heads/"):]
                git_ok("branch", name, ref, cwd=dest)
            how += "; fetched unpushed commits"
        else:
            problems += 1
            print(f"ERROR {row['path']}: could not fetch the unpushed bundle")
    if row["head"] != "-":
        flags = ["-B", row["branch"]] if row["branch"] != "DETACHED" else ["--detach"]
        if not git_ok("checkout", "-q", "-f", *flags, row["head"], cwd=dest):
            problems += 1
            print(f"ERROR {row['path']}: commit {row['head'][:10]} is not available")
    if row["patch"] != "-" and not git_ok("apply", "--binary", str(run_dir / row["patch"]), cwd=dest):
        problems += 1
        print(f"ERROR {row['path']}: the uncommitted-changes patch did not apply")
    if row["untracked_tar"] != "-":
        safe_extract(run_dir / row["untracked_tar"], dest)
    head = git("rev-parse", "HEAD", cwd=dest, check=False).strip()
    if row["head"] != "-" and head != row["head"]:
        problems += 1
        print(f"ERROR {row['path']}: at {head[:10] or 'no commit'}, recorded {row['head'][:10]}")
    print(f"{'ok   ' if not problems else 'FIXME'} {row['path']}: {how}")
    return problems


def cmd_restore(args: argparse.Namespace) -> int:
    settings = Settings(backend=args.backend or "", dest=args.dest or "")
    store = open_store(settings)
    run = args.run_id
    if run == "latest":
        run = latest_run(store)
    prefix = f"runs/{run}/"
    keys = store.list(prefix)
    if not keys:
        raise SwarmError(f"no run {run} in {store.describe()}")
    text = store.get_text(prefix + "MANIFEST.tsv")
    rows = read_tsv(text or "")
    if args.only:
        rows = [r for r in rows if re.search(args.only, r["path"])]
    target = Path(args.target).expanduser()
    if args.dry_run:
        print(f"dry run: restore of run {run} from {store.describe()} into {target}; nothing is written")
        print(f"files in the run ({len(keys)}):")
        for key, size in sorted(keys.items()):
            print(f"  {size:>12}  {key}")
        for row in rows:
            extras = [x for x in ("bundle", "patch", "untracked_tar") if row[x] != "-"]
            print(f"  checkout {row['path']}: branch {row['branch']} at {row['head'][:10]}, "
                  f"remote {row['remote']}, from run: {', '.join(extras) or 'nothing extra (pushed, clean)'}")
        if args.files:
            ledger = {k: v for k, v in load_ledger(store).items() if v.get("status") != "drop"}
            print(f"files with no other copy ({len(ledger)}):")
            for key in sorted(ledger):
                print(f"  {ledger[key]['bytes']:>12}  files/{key}")
        return 0
    run_dir = settings.state / f"restore-{run}"
    shutil.rmtree(str(run_dir), ignore_errors=True)
    run_dir.mkdir(parents=True)
    store.download(prefix, run_dir)
    target.mkdir(parents=True, exist_ok=True)
    errors = 0
    for row in rows:
        errors += restore_checkout(row, run_dir, target)
    if not args.only:
        for label in ("memory", "data", "settings"):
            archive = run_dir / f"{label}.tar.gz"
            if archive.exists():
                count = safe_extract(archive, target / "_tstack" / label)
                print(f"ok    {label}: {count} files unpacked in {target / '_tstack' / label}")
    if args.files:
        copied = 0
        for key in sorted(k for k in store.list("files/") if not k.startswith("_ledger/")):
            dest = unrel_key(key, target)
            if args.only and not re.search(args.only, key):
                continue
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                store.fetch("files/" + key, dest)
                copied += 1
        print(f"ok    files: {copied} copied (existing files are never overwritten)")
    print(f"restore finished with {errors} errors; run downloaded to {run_dir}")
    return 1 if errors else 0


# --- weekly review ------------------------------------------------------------------------------


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/*"):
        return (os.path.dirname(path) or ".") == pattern[:-2]
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def protected_hit(settings: Settings, key: str) -> bool:
    target = str(unrel_key(key, settings.home).resolve())
    for guard in settings.protected:
        g = str(guard.resolve())
        if target == g or target.startswith(g + "/") or g.startswith(target + "/"):
            return True
    return False


def weekly_report(settings: Settings, store: Store, days: int) -> Tuple[str, int]:
    rows = load_ledger(store)
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    new = [r for r in rows.values() if r["status"] == "new" and r["date"][:10] >= since]
    lines = [f"# Backup files to review, {since} to {today()}", "",
             f"{len(new)} files ({sum(int(r['bytes']) for r in new) / GIB:.2f} GiB) with no other copy were "
             f"copied to `{store.describe()}/files/` in the last {days} days. Ask the user, folder by folder, "
             "whether to keep or drop the backup copy. Dropping deletes only the backup copy; local files stay.", "",
             "Apply answers (add `--dry-run` first): `swarm upkeep weekly --mark keep|drop <folder or path> ...`. "
             "A folder covers everything under it; `folder/*` only the files directly in it.", ""]
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in new:
        groups[row["group"]].append(row)
    if groups:
        lines += ["| Folder | Files | MiB | Uploaded | Source |", "|---|---|---|---|---|"]
    for key, group in sorted(groups.items(), key=lambda p: -sum(int(r["bytes"]) for r in p[1])):
        dates = sorted(r["date"][:10] for r in group)
        when = dates[0] if dates[0] == dates[-1] else f"{dates[0]} to {dates[-1]}"
        sources = ", ".join(sorted({r["source"] for r in group}))
        flag = " PROTECTED" if any(protected_hit(settings, r["path"]) for r in group) else ""
        lines.append(f"| `{key}`{flag} | {len(group)} | {sum(int(r['bytes']) for r in group) / MIB:.1f} | {when} | {sources} |")
    if not groups:
        lines.append("Nothing to review.")
    return "\n".join(lines) + "\n", len(new)


def create_review_task(settings: Settings, report_path: Path, count: int, owner: str) -> str:
    args = argparse.Namespace(slug="upkeep-weekly-review", owner=owner, by="upkeep",
                              title=f"Weekly backup review: {count} files to keep or drop",
                              request=f"Weekly upkeep review. {count} files with no other copy were backed up this "
                                      f"week. Report: {report_path}")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tasks.cmd_new(args)
    folder = Path(out.getvalue().strip().splitlines()[-1])
    with tasks.locked():
        task = tasks.Task(folder)
        task.sections["Reading"] = ("Ask the user keep or drop for each folder in the report, one question at a "
                                    f"time with a recommendation, then apply the answers with `swarm upkeep weekly "
                                    f"--mark keep|drop ...`.\n\nReport: `{report_path}`")
        task.save()
    return task.id


def cmd_weekly(args: argparse.Namespace) -> int:
    settings = Settings(backend=args.backend or "", dest=args.dest or "")
    store = open_store(settings)
    if args.mark:
        return mark(settings, store, args)
    text, count = weekly_report(settings, store, args.days)
    out = Path(args.out) if args.out else settings.state / "weekly" / f"{today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"report saved to {out}")
    if count and not args.no_task:
        owner = str(settings.u.get("review_owner") or config.section("swarm", settings.values).get("overall") or "overall")
        print(f"created task {create_review_task(settings, out, count, owner)} for {owner}")
    return 0


def mark(settings: Settings, store: Store, args: argparse.Namespace) -> int:
    if not args.paths:
        raise SwarmError("--mark needs at least one path or folder")
    rows = load_ledger(store)
    patterns = []
    for item in args.paths:
        star = item == "*" or item.endswith("/*")
        base = item[:-2] if item.endswith("/*") else ("." if item == "*" else item)
        if base.startswith(("~", "/")):
            base = rel_key(Path(base).expanduser(), settings.home)
        patterns.append(base + ("/*" if star else ""))
    states = {"new", "keep"} if args.mark == "drop" and args.include_keep else {"new"}
    hits = [r for r in rows.values() if r["status"] in states and any(matches(r["path"], p) for p in patterns)]
    if args.mark == "drop":
        blocked = [r["path"] for r in hits if protected_hit(settings, r["path"])]
        if blocked:
            raise SwarmError(f"refused: {len(blocked)} files are under upkeep.protected, for example {blocked[0]}")
    print(f"{'would mark' if args.dry_run else 'marking'} {len(hits)} files {args.mark}")
    for row in hits[:20]:
        print(f"  {row['path']}")
    if len(hits) > 20:
        print(f"  ... and {len(hits) - 20} more")
    if args.dry_run or not hits:
        return 0
    if args.mark == "drop":
        failed = set(e.split(":", 1)[0] for e in store.delete("files/" + r["path"] for r in hits))
        hits = [r for r in hits if "files/" + r["path"] not in failed]
    for row in hits:
        row["status"], row["marked"] = args.mark, stamp()
    save_ledger(store, rows)
    print(f"ledger updated: {len(hits)} files now {args.mark}")
    return 0


# --- the nightly loop ------------------------------------------------------------------------------


def next_run(backup_time: str, after: Optional[dt.datetime] = None) -> dt.datetime:
    now = after or dt.datetime.now()
    hour, minute = map(int, backup_time.split(":"))
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return due if due > now else due + dt.timedelta(days=1)


def backup_and_review(settings: Settings) -> None:
    log = Log()
    try:
        with run_lock(settings):
            failures = run_backup(settings, open_store(settings), log)
        print(f"{log_stamp()} backup finished, {failures} failure(s)", flush=True)
        day = str(settings.u.get("weekly_day") or "")
        if day and WEEKDAYS[dt.date.today().weekday()] == day:
            cmd_weekly(argparse.Namespace(backend="", dest="", mark=None, days=7, out=None, no_task=False,
                                          paths=[], dry_run=False, include_keep=False))
    except (SwarmError, OSError, RuntimeError) as error:
        print(f"{log_stamp()} backup failed: {error}", flush=True)


def cmd_loop(args: argparse.Namespace) -> int:
    settings = Settings()
    print(f"{log_stamp()} upkeep loop started; backs up at {settings.u.get('backup_time', '03:00')}", flush=True)
    if args.now:
        backup_and_review(settings)
    while True:
        due = next_run(str(settings.u.get("backup_time", "03:00")))
        while dt.datetime.now() < due:
            time.sleep(min(60, max(1.0, (due - dt.datetime.now()).total_seconds())))
        backup_and_review(Settings())


def runner_alive(settings: Settings) -> str:
    """`tmux`, `pid <n>` or '' for how the loop currently runs."""
    if shutil.which("tmux") and subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION],
                                               capture_output=True).returncode == 0:
        return "tmux"
    pid_file = settings.state / "loop.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return f"pid {pid}"
        except (ValueError, OSError):
            pid_file.unlink()
    return ""


def cmd_start(args: argparse.Namespace) -> int:
    settings = Settings()
    open_store(settings)  # fail early on a bad backend setting
    if runner_alive(settings):
        print(f"upkeep loop is already running ({runner_alive(settings)})")
        return 0
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    settings.state.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SWARM_BIN), "upkeep", "loop"]
    env = {"TSTACK_CONFIG": str(config.config_path()), "SWARM_DIR": str(settings.data)}
    mode = str(settings.u.get("runner") or "auto")
    if mode in ("auto", "tmux") and shutil.which("tmux"):
        line = "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " " + \
               " ".join(shlex.quote(c) for c in cmd) + f" >> {shlex.quote(str(settings.log_file))} 2>&1"
        subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_SESSION, line], check=True)
        print(f"upkeep loop started in tmux session {TMUX_SESSION}; log {settings.log_file}")
    elif mode == "tmux":
        raise SwarmError("upkeep.runner is tmux but tmux is not on PATH")
    else:
        with open(str(settings.log_file), "a") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    env={**os.environ, **env})
        (settings.state / "loop.pid").write_text(str(proc.pid))
        print(f"upkeep loop started with a detached process (pid {proc.pid}); log {settings.log_file}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    settings = Settings()
    how = runner_alive(settings)
    if how == "tmux":
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
    elif how.startswith("pid "):
        os.kill(int(how[4:]), signal.SIGTERM)
        (settings.state / "loop.pid").unlink()
    else:
        print("upkeep loop is not running")
        return 0
    print(f"stopped upkeep loop ({how})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = Settings(backend=args.backend or "", dest=args.dest or "")
    how = runner_alive(settings)
    print(f"loop: {'RUNNING (' + how + ')' if how else 'stopped'}; backs up at {settings.u.get('backup_time', '03:00')}")
    print(f"roots: {', '.join(map(str, settings.roots))}; memory {settings.memory}; data {settings.data}")
    try:
        store = open_store(settings)
        runs = sorted(r for r in store.folders("runs/") if DATE.match(r))
        latest = store.get_text("LATEST")
        print(f"backend: {store.name} at {store.describe()}")
        print(f"runs kept ({len(runs)}): {', '.join(runs) or 'none'}; latest good: {(latest or 'none').strip()}")
    except SwarmError as error:
        print(f"backend: not usable: {error}")
    if settings.log_file.exists():
        tail = settings.log_file.read_text(errors="replace").splitlines()[-6:]
        print(f"last lines of {settings.log_file}:")
        for line in tail:
            print(f"  {line}")
    return 0


def cmd_run_once(args: argparse.Namespace) -> int:
    settings = Settings(backend=args.backend or "", dest=args.dest or "")
    store = open_store(settings)
    log = Log()
    with run_lock(settings):
        failures = run_backup(settings, store, log)
    return 1 if failures else 0


# --- inventory (read-only) --------------------------------------------------------------------------


def inventory_sizes(settings: Settings) -> None:
    for root in settings.roots:
        if not root.is_dir():
            continue
        print(f"== {root}")
        rows = []
        for entry in sorted(root.iterdir()):
            if entry.is_symlink():
                rows.append((0, f"link  {entry.name} -> {os.readlink(str(entry))}"))
                continue
            done = subprocess.run(["du", "-sk", str(entry)], capture_output=True, text=True)
            kib = int(done.stdout.split()[0]) if done.stdout.split() else 0
            newest = dt.datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d")
            flag = "  PROTECTED" if protected_hit(settings, rel_key(entry, settings.home)) else ""
            rows.append((kib, f"{kib / MIB:8.2f} GiB  {newest}  {entry.name}{flag}"))
        for _, text in sorted(rows, reverse=True):
            print(text)


def inventory_worktrees(settings: Settings, checkouts: Dict[Path, Checkout]) -> None:
    print("== checkouts (branch, uncommitted files, unpushed commits)")
    for path in checkouts:
        branch = git("symbolic-ref", "--short", "-q", "HEAD", cwd=path, check=False).strip() or "DETACHED"
        dirty = len([x for x in git("status", "--porcelain", cwd=path, check=False).splitlines() if x])
        unpushed = len([x for x in git("log", "--branches", "--not", "--remotes", "--oneline", cwd=path,
                                       check=False).splitlines() if x])
        flag = "  PROTECTED" if protected_hit(settings, rel_key(path, settings.home)) else ""
        print(f"{rel_key(path, settings.home):50}  {branch:20}  dirty {dirty:4}  unpushed {unpushed:4}{flag}")


def inventory_files(settings: Settings, checkouts: Dict[Path, Checkout]) -> None:
    for item in checkouts.values():
        item.tracked = set(x for x in git("ls-files", "-z", cwd=item.path, check=False).split("\0") if x)
        tar = Path(tempfile.mkstemp(suffix=".tar.gz")[1])
        try:
            tar_untracked(item, tar)  # fills item.tarred / item.skipped; the tar itself is discarded
        finally:
            tar.unlink()
    found = local_only(settings.roots, settings.home, checkouts, settings.skip,
                       int(settings.num("min_size_mb", 0) * MIB))
    groups: Dict[str, List[Candidate]] = defaultdict(list)
    for c in found:
        groups[c.group].append(c)
    print(f"== files with no other copy: {len(found)} files, {sum(c.bytes for c in found) / GIB:.2f} GiB")
    for key, items in sorted(groups.items(), key=lambda p: -sum(c.bytes for c in p[1]))[:40]:
        sources = ", ".join(sorted({c.source for c in items}))
        print(f"{sum(c.bytes for c in items) / MIB:10.1f} MiB  {len(items):6} files  {key}  [{sources}]")


def cmd_inventory(args: argparse.Namespace) -> int:
    settings = Settings()
    what = args.what
    if what in ("all", "sizes"):
        inventory_sizes(settings)
    if what in ("all", "worktrees", "files"):
        checkouts = find_checkouts(settings.roots, settings.skip)
        if what in ("all", "worktrees"):
            inventory_worktrees(settings, checkouts)
        if what in ("all", "files"):
            inventory_files(settings, checkouts)
    return 0


# --- command table ---------------------------------------------------------------------------------


def add_store_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=sorted(STORES), help="override upkeep.backend")
    p.add_argument("--dest", help="override the backend location (a folder for disk, s3://bucket/prefix for s3)")


def register(sub: argparse._SubParsersAction) -> None:
    config.VALIDATORS.append(validate)
    history.register(sub)
    memory_tools.register(sub)
    p = sub.add_parser("upkeep", help="nightly backup, weekly review, restore, inventory",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    acts = p.add_subparsers(dest="action", metavar="<action>")
    acts.required = True

    a = acts.add_parser("run-once", help="run one backup now, in the foreground")
    add_store_args(a)
    a.set_defaults(run=cmd_run_once)

    acts.add_parser("start", help="back up every night at upkeep.backup_time (tmux, else a detached process)"
                    ).set_defaults(run=cmd_start)
    acts.add_parser("stop", help="stop the nightly loop").set_defaults(run=cmd_stop)
    a = acts.add_parser("status", help="show the loop, the runs kept and the last log lines")
    add_store_args(a)
    a.set_defaults(run=cmd_status)

    a = acts.add_parser("loop", help="(internal) the loop that start runs")
    a.add_argument("--now", action="store_true", help="back up once before waiting for the next night")
    a.set_defaults(run=cmd_loop)

    a = acts.add_parser("restore", help="rebuild checkouts, memory and data from one run under TARGET")
    a.add_argument("run_id", metavar="RUN", help="a date such as 2026-10-01, or latest")
    a.add_argument("target", metavar="TARGET")
    a.add_argument("--only", metavar="REGEX", help="restore only checkout paths matching this")
    a.add_argument("--files", action="store_true", help="also copy back the files with no other copy")
    a.add_argument("--dry-run", action="store_true", help="list what the run holds and what would be rebuilt")
    add_store_args(a)
    a.set_defaults(run=cmd_restore)

    a = acts.add_parser("weekly", help="write the keep-or-drop report and open a swarm task for it")
    a.add_argument("--days", type=int, default=7)
    a.add_argument("--out", help="where to save the report")
    a.add_argument("--no-task", action="store_true", help="write the report only")
    a.add_argument("--mark", choices=["keep", "drop"], help="apply an answer to the listed paths")
    a.add_argument("--include-keep", action="store_true", help="let drop also change rows marked keep")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("paths", nargs="*", metavar="PATH")
    add_store_args(a)
    a.set_defaults(run=cmd_weekly)

    a = acts.add_parser("inventory", help="read-only look at sizes, checkouts and files with no other copy")
    a.add_argument("--what", choices=["all", "sizes", "worktrees", "files"], default="all")
    a.set_defaults(run=cmd_inventory)
