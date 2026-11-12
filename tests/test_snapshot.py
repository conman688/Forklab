"""Host-side snapshot fidelity: round trip, sensitivity, and fail-closed cases."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from forklab.snapshot import SnapshotError, Store, diff, manifest, manifest_id

BASE = 1_700_000_000_000_000_000


def make_workspace(root):
    """Build a workspace exercising every supported entry type and metadata field."""
    (root / "src").mkdir()
    (root / "empty").mkdir()
    (root / "locked").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "src" / "blob.bin").write_bytes(bytes(range(256)) * 3)
    (root / "secret.txt").write_text("no trailing newline")
    (root / "locked" / "inner.txt").write_text("inside a 0o500 dir")
    (root / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.symlink("src/calc.py", root / "rel_link")
    os.symlink("/etc/passwd", root / "abs_link")
    os.symlink("does/not/exist", root / "dangling")
    modes = {"src/calc.py": 0o644, "src/blob.bin": 0o640, "secret.txt": 0o600, "run.sh": 0o755,
             "locked/inner.txt": 0o444, "empty": 0o750, "src": 0o755, "locked": 0o500}
    for rel, mode in modes.items():
        os.chmod(root / rel, mode)
    for i, rel in enumerate(sorted(modes) + ["rel_link", "abs_link", "dangling", "."]):
        ns = BASE + i * 1_000_000_007 + 123_456_789
        os.utime(root / rel, ns=(ns, ns), follow_symlinks=False)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    make_workspace(root)
    yield root
    os.chmod(root / "locked", 0o700)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


def unlock(path):
    for dirpath, dirnames, _ in os.walk(path):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                os.chmod(p, 0o700)


def test_round_trip_matches_exactly(ws, store, tmp_path):
    mid = store.capture(ws)
    dest = tmp_path / "restored"
    assert store.restore(mid, dest) == []
    assert manifest(dest) == manifest(ws)
    unlock(dest)


def test_manifest_records_nanosecond_mtimes_and_symlinks_without_following(ws):
    m = manifest(ws)
    assert m["src/calc.py"]["mtime_ns"] % 1_000_000_000 != 0
    assert m["abs_link"] == {**m["abs_link"], "type": "symlink", "link": "/etc/passwd", "sha256": None}
    assert m["dangling"]["link"] == "does/not/exist"
    assert m["locked"]["mode"] == 0o500


def test_capture_is_deterministic(ws, store):
    assert store.capture(ws) == store.capture(ws)


@pytest.mark.parametrize("perturb, field", [
    (lambda r: (r / "src" / "calc.py").write_text("changed\n"), "sha256"),
    (lambda r: os.chmod(r / "run.sh", 0o700), "mode"),
    (lambda r: os.utime(r / "secret.txt", ns=(BASE, BASE)), "mtime_ns"),
    (lambda r: os.utime(r / "empty", ns=(BASE, BASE)), "mtime_ns"),
    (lambda r: os.utime(r / "rel_link", ns=(BASE, BASE), follow_symlinks=False), "mtime_ns"),
    (lambda r: (os.unlink(r / "rel_link"), os.symlink("src/blob.bin", r / "rel_link")), "link"),
])
def test_verify_detects_field_changes(ws, perturb, field):
    before = manifest(ws)
    perturb(ws)
    after = manifest(ws)
    assert any(d.get("field") == field for d in diff(before, after))


def test_verify_detects_added_and_removed_entries(ws):
    before = manifest(ws)
    (ws / "new.txt").write_text("x")
    os.unlink(ws / "secret.txt")
    problems = {(d["path"], d.get("problem")) for d in diff(before, manifest(ws))}
    assert ("new.txt", "unexpected") in problems
    assert ("secret.txt", "missing") in problems


def test_fifo_is_refused(ws):
    os.mkfifo(ws / "pipe")
    with pytest.raises(SnapshotError, match="unsupported entry type"):
        manifest(ws)


def test_socket_is_refused():
    # AF_UNIX paths are length-limited (104 bytes on macOS), so use a short dir.
    root = Path(tempfile.mkdtemp(prefix="fl", dir="/tmp"))
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(root / "sock"))
        with pytest.raises(SnapshotError, match="unsupported entry type"):
            manifest(root)
    finally:
        s.close()
        shutil.rmtree(root)


def test_hard_link_is_refused(ws):
    os.link(ws / "secret.txt", ws / "hard")
    with pytest.raises(SnapshotError, match="hard link"):
        manifest(ws)


def test_unreadable_directory_is_refused_not_skipped(ws):
    os.chmod(ws / "src", 0o000)
    try:
        with pytest.raises(SnapshotError, match="cannot read"):
            manifest(ws)
    finally:
        os.chmod(ws / "src", 0o755)


def test_capture_does_not_traverse_symlinked_directories(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host_secret").write_text("x")
    root = tmp_path / "ws2"
    root.mkdir()
    os.symlink(outside, root / "escape")
    m = manifest(root)
    assert set(m) == {".", "escape"}
    assert m["escape"]["type"] == "symlink"


def test_restore_refuses_non_empty_destination(ws, store, tmp_path):
    mid = store.capture(ws)
    dest = tmp_path / "busy"
    dest.mkdir()
    (dest / "x").write_text("x")
    with pytest.raises(SnapshotError, match="not empty"):
        store.restore(mid, dest)


def test_restore_detects_corrupt_blob(ws, store, tmp_path):
    mid = store.capture(ws)
    sha = store.load_manifest(mid)["secret.txt"]["sha256"]
    (store.root / "blobs" / sha).write_bytes(b"tampered")
    with pytest.raises(SnapshotError, match="corrupt"):
        store.restore(mid, tmp_path / "r")


def test_restore_detects_corrupt_manifest(ws, store, tmp_path):
    mid = store.capture(ws)
    path = store.root / "manifests" / f"{mid}.json"
    m = json.loads(path.read_bytes())
    m["secret.txt"]["mode"] = 0o777
    path.write_text(json.dumps(m))
    with pytest.raises(SnapshotError, match="corrupt"):
        store.restore(mid, tmp_path / "r")


@pytest.mark.parametrize("bad_path", ["../escape", "/abs", "a/../b", "./x", "src//x"])
def test_restore_rejects_unsafe_paths(store, tmp_path, bad_path):
    m = {".": {"type": "dir", "mode": 0o755, "mtime_ns": BASE},
         "src": {"type": "dir", "mode": 0o755, "mtime_ns": BASE},
         bad_path: {"type": "symlink", "link": "x", "mtime_ns": BASE}}
    mid = manifest_id(m)
    (store.root / "manifests" / f"{mid}.json").write_text(json.dumps(m, sort_keys=True, separators=(",", ":")))
    with pytest.raises(SnapshotError, match="unsafe path|parent"):
        store.restore(mid, tmp_path / "r")
    assert not (tmp_path / "escape").exists()


def test_restore_rejects_child_of_symlink(store, tmp_path):
    m = {".": {"type": "dir", "mode": 0o755, "mtime_ns": BASE},
         "link": {"type": "symlink", "link": "/tmp", "mtime_ns": BASE},
         "link/x": {"type": "file", "mode": 0o644, "mtime_ns": BASE, "sha256": "0" * 64}}
    mid = manifest_id(m)
    (store.root / "manifests" / f"{mid}.json").write_text(json.dumps(m, sort_keys=True, separators=(",", ":")))
    with pytest.raises(SnapshotError, match="parent"):
        store.restore(mid, tmp_path / "r")
