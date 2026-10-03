"""What the nightly backup reads: git checkouts, the memory and data folders, and files with no other copy.

Per git repo under the roots it writes `repos/<slug>.unpushed.bundle`: every branch, tag and the newest
stash that no remote has. Per checkout it writes `checkouts/<slug>.patch` (uncommitted changes to tracked
files) and `checkouts/<slug>.untracked.tar.gz` (untracked files git does not ignore). Nothing is ever
pushed, and no repo or local file is changed. Credential-looking files are never copied.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_DEPTH = 6
MAX_UNTRACKED_FILE = 100 * MIB
MAX_UNTRACKED_TAR = 2 * GIB
CONTENT_LIMIT = 5 * MIB
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}

CREDENTIAL_NAMES = re.compile(
    r"(^|/)(\.aws|\.ssh|\.netrc|\.kube|\.docker|\.gnupg)(/|$)"
    r"|(^|/)\.env($|[./])"
    r"|(^|/|[._-])(tokens?|secrets?|credentials?|cookies?|passwords?|passwd)($|/|[._-])"
    r"|\.pem$|\.key$|\.p12$|(^|/)id_(rsa|ed25519|ecdsa|dsa)|(^|/)auth\.json$|_history$"
)
CREDENTIAL_CONTENT = re.compile(
    r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----"
    r"|\bxox[abprs]-[0-9A-Za-z-]{10,}"
    r"|\bgh[pousr]_[0-9A-Za-z]{30,}|\bgithub_pat_[0-9A-Za-z_]{30,}"
    r"|\bsk-[A-Za-z0-9_-]{32,}|\bhf_[A-Za-z0-9]{30,}"
)


def credential_reason(path: Path, name: str) -> Optional[str]:
    """`credential-name`, `credential-content`, or None for a file that may go into the backup."""
    if CREDENTIAL_NAMES.search(name):
        return "credential-name"
    try:
        if not path.is_file() or path.stat().st_size > CONTENT_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return "credential-content" if CREDENTIAL_CONTENT.search(data.decode("utf-8", "replace")) else None


def skipper(patterns: Iterable[str]) -> Callable[[str], bool]:
    """A test for one file or folder name against the `upkeep.skip` name patterns (shell globs)."""
    pats = [p for p in patterns if p]
    return lambda name: any(fnmatch.fnmatch(name, p) for p in pats)


# --- git ----------------------------------------------------------------------------------


def git(*args: str, cwd: Optional[Path] = None, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
                          env=GIT_ENV)
    if done.returncode != 0 and check:
        raise RuntimeError(f"git {' '.join(args[:2])}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""


def git_ok(*args: str, cwd: Optional[Path] = None) -> bool:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
                          env=GIT_ENV).returncode == 0


def git_z(*args: str, cwd: Optional[Path] = None) -> List[str]:
    return [item for item in git(*args, cwd=cwd, check=False).split("\0") if item]


def slug_of(path: Path, home: Path) -> str:
    try:
        text = path.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        text = "_abs" + path.resolve().as_posix()
    return re.sub(r"[^A-Za-z0-9._-]+", "__", text).strip("_") or "root"


def rel_key(path: Path, home: Path) -> str:
    """A ledger/manifest path: relative to home, or `_abs/<absolute path>` outside it."""
    try:
        return path.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return "_abs/" + path.resolve().as_posix().lstrip("/")


def unrel_key(key: str, home: Path) -> Path:
    return Path("/" + key[len("_abs/"):]) if key.startswith("_abs/") else home / key


@dataclass
class Checkout:
    path: Path
    common: Path
    slug: str = ""
    tracked: Set[str] = field(default_factory=set)
    tarred: Set[str] = field(default_factory=set)
    skipped: Dict[str, str] = field(default_factory=dict)


def find_checkouts(roots: List[Path], skip: Callable[[str], bool]) -> Dict[Path, Checkout]:
    """Every git checkout under the roots (up to MAX_DEPTH levels), plus worktrees their repos list."""
    found: Dict[Path, Checkout] = {}
    for root in roots:
        if not root.is_dir():
            continue
        base = len(root.parts)
        for folder, dirs, files in os.walk(str(root)):
            here = Path(folder)
            if ".git" in dirs or ".git" in files:
                out = git("rev-parse", "--git-common-dir", cwd=here, check=False).strip()
                if out:
                    common = (here / out).resolve()
                    found.setdefault(here.resolve(), Checkout(here.resolve(), common))
            if len(here.parts) - base >= MAX_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d != ".git" and not skip(d))
    for common in sorted({item.common for item in found.values()}):
        for line in git("--git-dir", str(common), "worktree", "list", "--porcelain", check=False).splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
                if path.is_dir() and path.resolve() not in found and (path / ".git").exists():
                    found[path.resolve()] = Checkout(path.resolve(), common)
    return dict(sorted(found.items()))


def repo_top(common: Path) -> Path:
    return common.parent if common.name == ".git" else common


def strip_remote(url: str) -> str:
    return re.sub(r"(://)[^/@\s]+@", r"\1", url.strip())


# --- per repo and per checkout --------------------------------------------------------------


def unpushed_bundle(common: Path, out: Path) -> bool:
    """Bundle the branches, tags and newest stash no remote has. False when there is nothing to bundle."""
    if not git("--git-dir", str(common), "for-each-ref", "--count=1", check=False).strip():
        return False
    refs = ["--branches", "--tags"]
    if git_ok("--git-dir", str(common), "rev-parse", "-q", "--verify", "refs/stash"):
        refs.append("refs/stash")
    done = subprocess.run(["git", "--git-dir", str(common), "bundle", "create", str(out), *refs,
                           "--not", "--remotes"], capture_output=True, env=GIT_ENV)
    if done.returncode != 0:
        if out.exists():
            out.unlink()
        if b"empty bundle" in done.stderr:
            return False
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip())
    return True


def make_patch(path: Path, out: Path) -> bool:
    if not git_ok("rev-parse", "-q", "--verify", "HEAD", cwd=path):
        return False
    with out.open("wb") as handle:
        done = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=str(path), stdout=handle,
                              stderr=subprocess.DEVNULL, env=GIT_ENV)
    if done.returncode == 0 and out.stat().st_size:
        return True
    out.unlink()
    return False


def tar_untracked(item: Checkout, out: Path) -> bool:
    """Tar the untracked, non-ignored files of a checkout. Records what was tarred or skipped on `item`."""
    total = 0
    for name in git_z("ls-files", "-z", "--others", "--exclude-standard", cwd=item.path):
        full = item.path / name
        try:
            info = full.lstat()
        except OSError:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            continue
        reason = credential_reason(full, name) if stat.S_ISREG(info.st_mode) else None
        if reason is None and info.st_size > MAX_UNTRACKED_FILE:
            reason = "over-100MB"
        if reason is None and total + info.st_size > MAX_UNTRACKED_TAR:
            reason = "tar-over-2GiB"
        if reason:
            item.skipped[name] = reason
            continue
        total += info.st_size
        item.tarred.add(name)
    if not item.tarred:
        return False
    with tarfile.open(str(out), "w:gz") as archive:
        for name in sorted(item.tarred):
            archive.add(str(item.path / name), arcname=name, recursive=False)
    return True


def tar_folder(folder: Path, out: Path, skip_dirs: Iterable[str] = ()) -> Tuple[int, List[str]]:
    """Tar a folder (memory or data), leaving out credential-looking files. Returns (files, left out)."""
    count, left_out = 0, []
    skipped = set(skip_dirs)
    with tarfile.open(str(out), "w:gz") as archive:
        for here, dirs, names in os.walk(str(folder)):
            dirs[:] = sorted(d for d in dirs if not (Path(here) == folder and d in skipped))
            for name in sorted(names):
                full = Path(here) / name
                key = full.relative_to(folder).as_posix()
                if name.endswith((".sock", ".tmp", ".part")):
                    continue
                reason = credential_reason(full, key) if not full.is_symlink() else None
                if reason:
                    left_out.append(f"{key} ({reason})")
                    continue
                archive.add(str(full), arcname=key, recursive=False)
                count += 1
    return count, left_out


def tar_paths(paths: List[Path], home: Path, out: Path) -> Tuple[List[str], List[str]]:
    """Tar loose settings files and folders (names relative to home). Returns (members, left out)."""
    members, left_out = [], []
    with tarfile.open(str(out), "w:gz") as archive:
        for top in paths:
            files = [top] if top.is_file() or top.is_symlink() else \
                [Path(h) / n for h, _, ns in os.walk(str(top)) for n in ns]
            for full in sorted(files):
                key = rel_key(full, home)
                reason = credential_reason(full, key) if not full.is_symlink() else None
                if reason:
                    left_out.append(f"{key} ({reason})")
                    continue
                archive.add(str(full), arcname=key, recursive=False)
                members.append(key)
    return members, left_out


# --- files with no other copy ----------------------------------------------------------------


@dataclass
class Candidate:
    key: str
    path: Path
    bytes: int
    mtime: int
    source: str
    group: str


def group_of(key: str) -> str:
    """The folder the weekly report groups a file under: the first two path parts."""
    parts = key.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) == 2 else ".")


def local_only(roots: List[Path], home: Path, checkouts: Dict[Path, Checkout], skip: Callable[[str], bool],
               min_bytes: int = 0) -> List[Candidate]:
    """Files under the roots that git cannot rebuild: outside every checkout, git-ignored inside one,
    or untracked and left out of the tar for size. Tracked files and tarred files have another copy."""
    by_path = {str(path): item for path, item in checkouts.items()}
    owner_of: Dict[str, Optional[Checkout]] = {}
    seen: Set[Tuple[int, int]] = set()
    found: List[Candidate] = []
    for root in roots:
        if not root.is_dir():
            continue
        for folder, dirs, names in os.walk(str(root)):
            here = str(Path(folder).resolve())
            checkout = by_path.get(here, owner_of.get(os.path.dirname(here)))
            owner_of[here] = checkout
            dirs[:] = sorted(d for d in dirs if d != ".git" and not skip(d))
            for name in names:
                full = Path(folder) / name
                if name == ".git" or skip(name) or CREDENTIAL_NAMES.search(str(full)):
                    continue
                try:
                    info = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_size < min_bytes:
                    continue
                if info.st_nlink > 1:
                    if (info.st_dev, info.st_ino) in seen:
                        continue
                    seen.add((info.st_dev, info.st_ino))
                if checkout is None:
                    source = "outside-git"
                else:
                    inner = os.path.relpath(str(full), str(checkout.path))
                    if inner in checkout.tracked or inner in checkout.tarred:
                        continue
                    why = checkout.skipped.get(inner)
                    if why and why.startswith("credential"):
                        continue
                    source = f"skipped:{why}" if why else "git-ignored"
                key = rel_key(full, home)
                found.append(Candidate(key, full, info.st_size, int(info.st_mtime), source, group_of(key)))
    return found
