"""Where the upkeep backup goes. Two stores ship: `disk` (a folder) and `s3` (the `aws` CLI).

A store is a small key/value interface over "files under a prefix". Keys are relative to the
store's root, for example `runs/2026-10-01/MANIFEST.tsv` or `files/work/data/big.bin`, and always
use `/`. To add a backend, subclass `Store`, implement the methods below and add the class to
`STORES`; then `upkeep.backend` can name it. See docs/upkeep.md.

Every `put` is verified by reading the stored size back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .util import SwarmError


class Store:
    """The interface a backend implements."""

    name = ""

    def describe(self) -> str:
        """Where this store writes, for messages."""
        raise NotImplementedError

    def put(self, src: Path, key: str) -> Tuple[bool, str]:
        """Copy one local file to `key` and verify it. Returns (ok, what was checked or what failed)."""
        raise NotImplementedError

    def get_text(self, key: str) -> Optional[str]:
        """The object's text, or None when it does not exist."""
        raise NotImplementedError

    def fetch(self, key: str, dest: Path) -> None:
        """Copy one object to the local path `dest`."""
        raise NotImplementedError

    def list(self, prefix: str) -> Dict[str, int]:
        """Key relative to `prefix` -> size in bytes, for every object under `prefix` (ends with `/`)."""
        raise NotImplementedError

    def folders(self, prefix: str) -> List[str]:
        """Names of the folders directly under `prefix` (ends with `/`)."""
        raise NotImplementedError

    def delete(self, keys: Iterable[str]) -> List[str]:
        """Delete objects. Returns one error line per failure; a missing object is not an error."""
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> None:
        for key in self.list(prefix):
            self.delete([prefix + key])

    # Shared helpers built on the calls above.

    def put_text(self, key: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".txt") as handle:
            handle.write(text)
        try:
            ok, detail = self.put(Path(handle.name), key)
            if not ok:
                raise SwarmError(f"upkeep: write {key}: {detail}")
        finally:
            os.unlink(handle.name)

    def download(self, prefix: str, dest: Path) -> int:
        """Copy every object under `prefix` into `dest`, keeping relative paths. Returns the count."""
        found = self.list(prefix)
        for key in found:
            target = dest / key
            target.parent.mkdir(parents=True, exist_ok=True)
            self.fetch(prefix + key, target)
        return len(found)


class DiskStore(Store):
    name = "disk"

    def __init__(self, folder: str) -> None:
        if not folder:
            raise SwarmError("upkeep.disk_path is empty; set it or pass --dest")
        self.root = Path(folder).expanduser()

    def describe(self) -> str:
        return str(self.root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        path = (root / key).resolve()
        if path != root and root not in path.parents:
            raise SwarmError(f"upkeep: key {key} leaves {root}")
        return path

    def put(self, src: Path, key: str) -> Tuple[bool, str]:
        try:
            dest = self._path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            shutil.copyfile(str(src), str(part))
            os.replace(str(part), str(dest))
            if dest.stat().st_size != src.stat().st_size:
                return False, f"stored size {dest.stat().st_size} differs from local {src.stat().st_size}"
        except OSError as error:
            return False, f"copy error: {error}"
        return True, "size matches"

    def get_text(self, key: str) -> Optional[str]:
        path = self._path(key)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def fetch(self, key: str, dest: Path) -> None:
        shutil.copyfile(str(self._path(key)), str(dest))

    def list(self, prefix: str) -> Dict[str, int]:
        base = self._path(prefix)
        found: Dict[str, int] = {}
        if base.is_dir():
            for folder, _, names in os.walk(str(base)):
                for name in names:
                    if not name.endswith(".part"):
                        path = Path(folder) / name
                        found[path.relative_to(base).as_posix()] = path.stat().st_size
        return found

    def folders(self, prefix: str) -> List[str]:
        base = self._path(prefix)
        return sorted(item.name for item in base.iterdir() if item.is_dir()) if base.is_dir() else []

    def delete(self, keys: Iterable[str]) -> List[str]:
        errors = []
        for key in keys:
            try:
                self._path(key).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(f"{key}: {error}")
        return errors

    def delete_prefix(self, prefix: str) -> None:
        base = self._path(prefix)
        if base.is_dir() and base != self.root.resolve():
            shutil.rmtree(str(base))


class S3Store(Store):
    """An S3 prefix, through the `aws` CLI. Credentials come from the CLI's own setup
    (environment, shared config, `AWS_PROFILE`, or `upkeep.aws_profile`)."""

    name = "s3"

    def __init__(self, uri: str, profile: str = "") -> None:
        if not uri.startswith("s3://") or len(uri) <= len("s3://"):
            raise SwarmError("upkeep.s3_uri must look like s3://bucket/prefix")
        bucket, _, prefix = uri[len("s3://"):].partition("/")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.profile = profile

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _aws(self, *args: str, check: bool = False) -> "subprocess.CompletedProcess[str]":
        if not shutil.which("aws"):
            raise SwarmError("the aws CLI is not on PATH; install it or use the disk backend "
                             "(upkeep.backend = \"disk\")")
        cmd = ["aws", *args] + (["--profile", self.profile] if self.profile else [])
        done = subprocess.run(cmd, capture_output=True, text=True)
        if check and done.returncode != 0:
            raise SwarmError(f"aws {' '.join(args[:2])} failed: {done.stderr.strip() or done.stdout.strip()}")
        return done

    def _url(self, key: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{key}"

    def _size(self, key: str) -> Optional[int]:
        done = self._aws("s3api", "head-object", "--bucket", self.bucket, "--key", self.prefix + key)
        if done.returncode != 0:
            return None
        try:
            return int(json.loads(done.stdout)["ContentLength"])
        except (ValueError, KeyError, TypeError):
            return None

    def put(self, src: Path, key: str) -> Tuple[bool, str]:
        done = self._aws("s3", "cp", str(src), self._url(key), "--only-show-errors")
        if done.returncode != 0:
            return False, f"aws s3 cp failed: {done.stderr.strip()}"
        size = self._size(key)
        if size != src.stat().st_size:
            return False, f"stored size {size} differs from local {src.stat().st_size}"
        return True, "size matches"

    def get_text(self, key: str) -> Optional[str]:
        if self._size(key) is None:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "object"
            self.fetch(key, target)
            return target.read_text(encoding="utf-8")

    def fetch(self, key: str, dest: Path) -> None:
        self._aws("s3", "cp", self._url(key), str(dest), "--only-show-errors", check=True)

    def list(self, prefix: str) -> Dict[str, int]:
        done = self._aws("s3", "ls", self._url(prefix), "--recursive")
        found: Dict[str, int] = {}
        start = self.prefix + prefix
        for line in done.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[2].isdigit() and parts[3].startswith(start):
                found[parts[3][len(start):]] = int(parts[2])
        return found

    def folders(self, prefix: str) -> List[str]:
        done = self._aws("s3", "ls", self._url(prefix))
        names = []
        for line in done.stdout.splitlines():
            line = line.strip()
            if line.startswith("PRE "):
                names.append(line[4:].rstrip("/"))
        return sorted(names)

    def delete(self, keys: Iterable[str]) -> List[str]:
        errors = []
        for key in keys:
            done = self._aws("s3", "rm", self._url(key), "--only-show-errors")
            if done.returncode != 0:
                errors.append(f"{key}: {done.stderr.strip()}")
        return errors

    def delete_prefix(self, prefix: str) -> None:
        self._aws("s3", "rm", self._url(prefix), "--recursive", "--only-show-errors", check=True)


# Name -> class. `upkeep.backend` picks one; open_store() in upkeep.py builds it from the settings.
STORES = {"disk": DiskStore, "s3": S3Store}
