"""Snapshot spec, PAX tar builder, and lstat manifest for the Phase 0 spike.

Stdlib only: this module also runs inside the sandbox container
(`python3 snapshot.py manifest /workspace`) to measure what was restored.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from dataclasses import dataclass

# Fields compared by the spike. Symlink mode is excluded on purpose: Linux
# ignores symlink permission bits (always 0o777), so it carries no signal.
COMPARED_FIELDS = ("type", "mode", "uid", "gid", "size", "mtime_ns", "sha256", "link")

CONTAINER_UID = 1000
CONTAINER_GID = 1000


@dataclass(frozen=True)
class Entry:
    path: str  # relative, "." for the workspace root
    type: str  # "dir" | "file" | "symlink"
    mode: int
    mtime_ns: int
    data: bytes = b""
    link: str | None = None


def fixture_entries() -> list[Entry]:
    """A small workspace that exercises the metadata the roadmap declares relevant."""
    base = 1_700_000_000_000_000_000  # ns since epoch
    return [
        Entry(".", "dir", 0o755, base + 900_000_001),
        Entry("README.md", "file", 0o644, base + 123_456_789, b"spike fixture\n"),
        Entry("empty_dir", "dir", 0o750, base + 222_222_222),
        Entry("src", "dir", 0o755, base + 333_333_333),
        Entry("src/calc.py", "file", 0o644, base + 444_444_444,
              b"def add(a, b):\n    return a - b  # bug\n"),
        Entry("src/no_newline.txt", "file", 0o600, base + 555_555_555, b"no trailing newline"),
        Entry("src/blob.bin", "file", 0o644, base + 666_666_666, bytes(range(256))),
        Entry("tests", "dir", 0o755, base + 777_777_777),
        Entry("tests/run.py", "file", 0o755, base + 888_888_888,
              b"import sys\nsys.path.insert(0, '/workspace/src')\n"
              b"from calc import add\n"
              b"print('PASS' if add(2, 3) == 5 else 'FAIL: add(2, 3) == %r' % add(2, 3))\n"),
        Entry("link_to_calc", "symlink", 0o777, base + 999_999_999, link="src/calc.py"),
    ]


def expected_manifest(entries: list[Entry]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in entries:
        out[e.path] = {
            "type": e.type,
            "mode": None if e.type == "symlink" else e.mode,
            "uid": CONTAINER_UID,
            "gid": CONTAINER_GID,
            "size": len(e.data) if e.type == "file" else None,
            "mtime_ns": e.mtime_ns,
            "sha256": hashlib.sha256(e.data).hexdigest() if e.type == "file" else None,
            "link": e.link,
        }
    return out


def build_tar(entries: list[Entry]) -> bytes:
    """PAX tar with nanosecond mtimes written as explicit pax headers.

    tarfile would otherwise serialise a float mtime and lose ns precision.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for e in sorted(entries, key=lambda x: x.path):
            info = tarfile.TarInfo("." if e.path == "." else f"./{e.path}")
            info.mode = e.mode
            info.uid, info.gid = CONTAINER_UID, CONTAINER_GID
            info.uname = info.gname = ""
            info.mtime = e.mtime_ns // 1_000_000_000
            secs, frac = divmod(e.mtime_ns, 1_000_000_000)
            info.pax_headers = {"mtime": f"{secs}.{frac:09d}"}
            if e.type == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif e.type == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = e.link or ""
                tar.addfile(info)
            else:
                info.size = len(e.data)
                tar.addfile(info, io.BytesIO(e.data))
    return buf.getvalue()


def manifest(root: str) -> dict[str, dict]:
    """lstat-based manifest; never follows symlinks."""
    out: dict[str, dict] = {}

    def record(full: str, rel: str) -> None:
        st = os.lstat(full)
        if stat.S_ISDIR(st.st_mode):
            kind = "dir"
        elif stat.S_ISLNK(st.st_mode):
            kind = "symlink"
        elif stat.S_ISREG(st.st_mode):
            kind = "file"
        else:
            kind = "other"
        sha = None
        if kind == "file":
            with open(full, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
        out[rel] = {
            "type": kind,
            "mode": None if kind == "symlink" else stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size if kind == "file" else None,
            "mtime_ns": st.st_mtime_ns,
            "sha256": sha,
            "link": os.readlink(full) if kind == "symlink" else None,
        }

    record(root, ".")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            record(full, os.path.relpath(full, root))
    return out


def diff(expected: dict[str, dict], actual: dict[str, dict]) -> list[dict]:
    problems: list[dict] = []
    for path in sorted(set(expected) | set(actual)):
        if path not in actual:
            problems.append({"path": path, "problem": "missing"})
        elif path not in expected:
            problems.append({"path": path, "problem": "unexpected"})
        else:
            for field in COMPARED_FIELDS:
                if expected[path][field] != actual[path][field]:
                    problems.append({
                        "path": path, "field": field,
                        "expected": expected[path][field], "actual": actual[path][field],
                    })
    return problems


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "manifest":
        sys.exit("usage: snapshot.py manifest <root>")
    json.dump(manifest(sys.argv[2]), sys.stdout, sort_keys=True)
