"""Host-side workspace snapshots: capture, content-addressed store, restore, verify.

Roadmap §1.2. Capture uses lstat only and never follows symlinks. Anything
the snapshot cannot represent faithfully (FIFOs, sockets, devices, hard
links) is refused rather than approximated. Restore writes into an empty
directory and is verified against the manifest; any difference on a
compared field is a MISMATCH.

Not captured or compared: ctime, atime, inode numbers, directory sizes,
symlink permission bits, extended attributes, ACLs and file flags.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

COMPARED_FIELDS = ("type", "mode", "uid", "gid", "size", "mtime_ns", "sha256", "link")
ROOT = "."


class SnapshotError(Exception):
    """The workspace or manifest cannot be snapshotted or restored faithfully."""


Manifest = dict[str, dict]


def _entry_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "unsupported"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest(root: str | os.PathLike) -> Manifest:
    """Describe every entry under `root` (and root itself as ".")."""
    root = os.fspath(root)
    out: Manifest = {}

    def record(full: str, rel: str) -> None:
        st = os.lstat(full)
        kind = _entry_type(st.st_mode)
        if kind == "unsupported":
            raise SnapshotError(f"unsupported entry type at {rel!r}: mode {oct(st.st_mode)}")
        if kind == "file" and st.st_nlink > 1:
            raise SnapshotError(f"hard link at {rel!r} (nlink={st.st_nlink}) cannot be restored faithfully")
        out[rel] = {
            "type": kind,
            "mode": None if kind == "symlink" else stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size if kind == "file" else None,
            "mtime_ns": st.st_mtime_ns,
            "sha256": _sha256_file(full) if kind == "file" else None,
            "link": os.readlink(full) if kind == "symlink" else None,
        }

    if _entry_type(os.lstat(root).st_mode) != "dir":
        raise SnapshotError(f"workspace root is not a directory: {root}")
    record(root, ROOT)
    def fail(err: OSError) -> None:
        raise SnapshotError(f"cannot read {err.filename!r}: {err.strerror}") from err

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            record(full, PurePosixPath(os.path.relpath(full, root)).as_posix())
    return out


def diff(expected: Manifest, actual: Manifest, fields: tuple[str, ...] = COMPARED_FIELDS) -> list[dict]:
    problems: list[dict] = []
    for path in sorted(set(expected) | set(actual)):
        if path not in actual:
            problems.append({"path": path, "problem": "missing"})
        elif path not in expected:
            problems.append({"path": path, "problem": "unexpected"})
        else:
            for f in fields:
                if expected[path].get(f) != actual[path].get(f):
                    problems.append({"path": path, "field": f,
                                     "expected": expected[path].get(f), "actual": actual[path].get(f)})
    return problems


def manifest_id(m: Manifest) -> str:
    return hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Store:
    """Content-addressed store: blobs/<sha256> and manifests/<manifest_id>.json."""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "manifests").mkdir(parents=True, exist_ok=True)

    def _publish(self, final: Path, write: Callable[[BinaryIO], None]) -> None:
        """Write via a unique temp file in the same directory, then atomically rename.

        Concurrent publishers of the same content each use their own temp file;
        os.replace makes the final name appear complete or not at all.
        """
        fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def _store_file(self, src: str, expected_sha: str, rel: str) -> None:
        final = self.root / "blobs" / expected_sha
        if final.exists():
            return  # content-addressed; corruption is caught when the blob is read

        def write(dst: BinaryIO) -> None:
            fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                if _copy_hashing(f, dst) != expected_sha:
                    raise SnapshotError(f"{rel!r} changed during capture")

        self._publish(final, write)

    def capture(self, workspace: str | os.PathLike) -> str:
        """Snapshot `workspace` into the store; return the manifest id.

        File contents are streamed in fixed-size chunks; no file is held in
        memory whole.
        """
        workspace = os.fspath(workspace)
        m = manifest(workspace)
        for rel, e in m.items():
            if e["type"] == "file":
                self._store_file(os.path.join(workspace, rel), e["sha256"], rel)
        if manifest(workspace) != m:
            raise SnapshotError("workspace changed during capture")
        mid = manifest_id(m)
        data = json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
        self._publish(self.root / "manifests" / f"{mid}.json", lambda f: f.write(data))
        return mid

    def load_manifest(self, mid: str) -> Manifest:
        m = json.loads((self.root / "manifests" / f"{mid}.json").read_bytes())
        if manifest_id(m) != mid:
            raise SnapshotError(f"manifest {mid} is corrupt")
        return m

    def restore(self, mid: str, dest: str | os.PathLike) -> list[dict]:
        """Restore manifest `mid` into empty directory `dest`; return the verify diff.

        An empty list means MATCH on every compared field. Ownership is not
        changed (that needs privileges); it is compared, so a workspace
        captured under a different uid/gid restores as a MISMATCH.
        """
        m = self.load_manifest(mid)
        dest = os.fspath(dest)
        _validate(m)
        os.makedirs(dest, exist_ok=True)
        if os.listdir(dest):
            raise SnapshotError(f"restore destination is not empty: {dest}")

        for rel in sorted(m, key=lambda p: (p.count("/"), p)):
            e = m[rel]
            if rel == ROOT:
                continue
            full = os.path.join(dest, rel)
            if e["type"] == "dir":
                os.mkdir(full, 0o700)
            elif e["type"] == "symlink":
                os.symlink(e["link"], full)
            else:
                self._restore_blob(e["sha256"], full)

        # Modes and mtimes last, deepest first: creating children changes a
        # directory's mtime, and a restrictive parent mode would block later
        # operations on its children.
        for rel in sorted(m, key=lambda p: (-p.count("/") if p != ROOT else 1, p)):
            e = m[rel]
            full = dest if rel == ROOT else os.path.join(dest, rel)
            if e["type"] != "symlink":
                os.chmod(full, e["mode"])
            os.utime(full, ns=(e["mtime_ns"], e["mtime_ns"]), follow_symlinks=False)

        return diff(m, manifest(dest))

    def _restore_blob(self, sha: str, dest_path: str) -> None:
        """Stream blob `sha` into a new file, verifying its digest.

        A digest mismatch raises, so restore never returns with corrupt
        content; the partially restored destination must then be discarded.
        """
        src_fd = os.open(self.root / "blobs" / sha, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(src_fd, "rb") as src:
            dst_fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(dst_fd, "wb") as dst:
                if _copy_hashing(src, dst) != sha:
                    raise SnapshotError(f"blob {sha} is corrupt")


CHUNK = 1 << 20


def _copy_hashing(src: BinaryIO, dst: BinaryIO) -> str:
    """Copy src to dst in CHUNK-sized pieces; return the sha256 of what was copied."""
    h = hashlib.sha256()
    for chunk in iter(lambda: src.read(CHUNK), b""):
        h.update(chunk)
        dst.write(chunk)
    return h.hexdigest()


def _validate(m: Manifest) -> None:
    """Reject manifests whose paths could escape or that are not a tree of real dirs."""
    if m.get(ROOT, {}).get("type") != "dir":
        raise SnapshotError("manifest has no root directory entry")
    for rel, e in m.items():
        if rel == ROOT:
            continue
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts or "." in p.parts or str(p) != rel:
            raise SnapshotError(f"unsafe path in manifest: {rel!r}")
        parent = str(p.parent) if str(p.parent) != "." else ROOT
        if m.get(parent, {}).get("type") != "dir":
            raise SnapshotError(f"parent of {rel!r} is not a directory in the manifest")
        if e.get("type") not in ("dir", "file", "symlink"):
            raise SnapshotError(f"unsupported type in manifest at {rel!r}")
