"""Isolation contract of the per-call executor, checked against a real Docker daemon.

Each test asserts one property from roadmap §1.1 by observing behaviour,
not by inspecting the command line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from forklab import executor
from forklab.executor import ExecutorError, Limits, run_call
from forklab.snapshot import Store, diff, manifest

IMAGE = "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available"),
]


@pytest.fixture
def dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, tmp_path


def fresh_out(base):
    return Path(tempfile.mkdtemp(prefix="out-", dir=base))


TEST_LIMITS = Limits(timeout_s=60)


def py(dirs, code, limits=TEST_LIMITS):
    ws, base = dirs
    return run_call(IMAGE, ws, ["python3", "-c", code], fresh_out(base), limits)


def test_runs_and_captures_output(dirs):
    r = py(dirs, "import sys; print('hi'); print('err', file=sys.stderr); sys.exit(3)")
    assert (r.exit_code, r.stdout, r.stderr, r.timed_out) == (3, b"hi\n", b"err\n", False)


def test_runs_as_host_uid_not_root(dirs):
    r = py(dirs, "import os; print(os.getuid(), os.getgid())")
    assert r.stdout.split() == [str(os.getuid()).encode(), str(os.getgid()).encode()]


def test_no_capabilities_and_no_new_privileges(dirs):
    r = py(dirs, "print(open('/proc/self/status').read())")
    status = dict(line.split(":\t", 1) for line in r.stdout.decode().splitlines() if ":\t" in line)
    assert status["CapEff"].strip() == "0000000000000000"
    assert status["CapPrm"].strip() == "0000000000000000"
    assert status["NoNewPrivs"].strip() == "1"


def test_no_network(dirs):
    r = py(dirs, (
        "import os, socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
        "print(len(open('/proc/net/route').readlines()) - 1)\n"
        "up = [i for i in os.listdir('/sys/class/net') if i != 'lo'\n"
        "      and os.path.isdir(f'/sys/class/net/{i}')\n"
        "      and open(f'/sys/class/net/{i}/operstate').read().strip() != 'down']\n"
        "print(up)\n"
    ))
    lines = r.stdout.decode().splitlines()
    assert lines[0].startswith("BLOCKED")
    # The kernel creates fallback tunnel devices (sit0, gre0, ...) in every
    # namespace; they are down. What matters: no routes, no non-loopback link up.
    assert lines[1] == "0"
    assert lines[2] == "[]"


def test_root_filesystem_is_read_only(dirs):
    r = py(dirs, (
        "import errno\n"
        "for p in ['/etc/x', '/usr/x', '/opt/x', '/x']:\n"
        "    try:\n"
        "        open(p, 'w')\n"
        "        print('WROTE', p)\n"
        "    except OSError as e:\n"
        "        print(errno.errorcode[e.errno])\n"
    ))
    assert b"WROTE" not in r.stdout
    assert set(r.stdout.decode().split()) <= {"EROFS", "EACCES"}


def test_workspace_and_out_are_writable_and_visible_on_host(dirs):
    ws, base = dirs
    out = fresh_out(base)
    run_call(IMAGE, ws, ["python3", "-c",
             "open('/workspace/a.txt','w').write('ws'); open('/out/b.txt','w').write('out')"], out)
    assert (ws / "a.txt").read_text() == "ws"
    assert (out / "b.txt").read_text() == "out"
    assert (ws / "a.txt").stat().st_uid == os.getuid()


def test_tmp_is_discarded_between_calls(dirs):
    first = py(dirs, "open('/tmp/x','w').write('1'); print('ok')")
    second = py(dirs, "import os; print(os.path.exists('/tmp/x'))")
    assert first.stdout == b"ok\n"
    assert second.stdout == b"False\n"


def test_no_process_outlives_the_call(dirs):
    ws, _ = dirs
    r = py(dirs, (
        "import subprocess\n"
        "subprocess.Popen(['sh', '-c', 'sleep 3; touch /workspace/late'], start_new_session=True)\n"
        "print('spawned')\n"
    ))
    assert r.stdout == b"spawned\n"
    time.sleep(5)
    assert not (ws / "late").exists()


def test_timeout_kills_and_removes_container(dirs):
    ws, _ = dirs
    r = py(dirs, "import time; time.sleep(60); open('/workspace/finished','w')", Limits(timeout_s=3))
    assert r.timed_out and r.exit_code is None
    assert r.duration_s < 30
    assert _gone(r.container_id)
    assert not (ws / "finished").exists()


def test_pids_limit_is_enforced(dirs):
    r = py(dirs, (
        "import subprocess\n"
        "procs = []\n"
        "try:\n"
        "    for _ in range(100):\n"
        "        procs.append(subprocess.Popen(['sleep', '30']))\n"
        "except OSError as e:\n"
        "    print('LIMITED at', len(procs))\n"
        "else:\n"
        "    print('UNLIMITED')\n"
    ), Limits(pids=32, timeout_s=60))
    assert r.stdout.startswith(b"LIMITED at")
    assert int(r.stdout.split()[-1]) < 32


def test_host_environment_does_not_leak(dirs, monkeypatch):
    monkeypatch.setenv("FORKLAB_HOST_SENTINEL", "leak")
    r = py(dirs, "import os; print(sorted(os.environ))")
    assert b"FORKLAB_HOST_SENTINEL" not in r.stdout
    assert b"PYTHONHASHSEED" in r.stdout


def test_workspace_symlink_resolves_inside_container(dirs, tmp_path):
    ws, _ = dirs
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("HOST-ONLY")
    os.symlink(host_secret, ws / "leak")
    r = py(dirs, "import os; print(os.path.exists('/workspace/leak'))")
    assert r.stdout == b"False\n"


def test_output_is_capped(dirs):
    r = py(dirs, "import sys; sys.stdout.write('x' * 5000)", Limits(output_bytes=1000, timeout_s=60))
    assert len(r.stdout) == 1000 and r.stdout_truncated
    assert r.stdout_total_bytes == 5000


def test_output_flood_is_drained_while_running_and_kept_bounded(dirs):
    """A producer that writes far more than the cap and never exits: output is
    drained during the call (total grows far past the cap) while only the cap
    is retained, and the timeout still removes the container."""
    r = py(dirs, (
        "import sys\n"
        "chunk = 'x' * 65536\n"
        "while True:\n"
        "    sys.stdout.write(chunk); sys.stderr.write(chunk)\n"
    ), Limits(output_bytes=1000, timeout_s=5))
    assert r.timed_out
    assert len(r.stdout) == 1000 and len(r.stderr) == 1000
    assert r.stdout_truncated and r.stderr_truncated
    assert r.stdout_total_bytes > 100 * 1000 and r.stderr_total_bytes > 100 * 1000
    assert _gone(r.container_id)


def test_unpinned_image_is_refused(dirs):
    ws, base = dirs
    with pytest.raises(ExecutorError, match="pinned"):
        run_call("python:3.12-slim", ws, ["true"], fresh_out(base))


def test_docker_failure_is_infrastructure_error(dirs):
    ws, base = dirs
    out = fresh_out(base)
    with pytest.raises(ExecutorError, match="create failed"):
        run_call(IMAGE, ws, ["true"], out, Limits(memory="not-a-size", timeout_s=60))
    assert not (out / ".forklab-started").exists()


def test_tool_exit_codes_that_docker_also_uses_are_results(dirs):
    ws, base = dirs
    missing = run_call(IMAGE, ws, ["/no/such/binary"], fresh_out(base), Limits(timeout_s=60))
    assert missing.exit_code == 127
    for code in (125, 126):
        assert py(dirs, f"import sys; sys.exit({code})").exit_code == code


def test_non_empty_out_dir_is_refused(dirs):
    ws, base = dirs
    out = fresh_out(base)
    (out / ".forklab-started").write_text("")
    with pytest.raises(ExecutorError, match="fresh, empty"):
        run_call(IMAGE, ws, ["true"], out)


def _gone(container_id: str) -> bool:
    r = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=False)
    return r.returncode != 0 and "no such" in r.stderr.lower()


def _forklab_containers() -> list[str]:
    r = subprocess.run(["docker", "ps", "-aq", "--filter", "name=forklab-"],
                       capture_output=True, text=True, check=True)
    return r.stdout.split()


FAKE_DOCKER = """#!/bin/bash
case "$1:$FAKE_MODE" in
  start:slow-start) sleep 5 ;;
  create:slow-create) sleep 10 ;;
  rm:rm-fails) echo "fake rm failure" >&2; exit 1 ;;
  inspect:inspect-fails) echo "fake daemon unreachable" >&2; exit 1 ;;
esac
exec "$REAL_DOCKER" "$@"
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """Route the executor through a wrapper that injects daemon faults."""
    script = tmp_path / "fake-docker"
    script.write_text(FAKE_DOCKER)
    script.chmod(0o755)
    monkeypatch.setenv("REAL_DOCKER", shutil.which("docker"))
    monkeypatch.setattr(executor, "DOCKER", str(script))
    assert _forklab_containers() == []
    yield lambda mode: monkeypatch.setenv("FAKE_MODE", mode)
    for cid in _forklab_containers():
        subprocess.run(["docker", "rm", "--force", cid], capture_output=True, check=False)


def test_delayed_start_is_removed_by_id_and_stays_gone(dirs, fake_docker):
    fake_docker("slow-start")
    with pytest.raises(ExecutorError, match="never started"):
        py(dirs, "print('hi')", Limits(timeout_s=1))
    time.sleep(6)  # past the injected delay: no late container may appear
    assert _forklab_containers() == []


def test_failed_removal_fails_closed(dirs, fake_docker, monkeypatch):
    monkeypatch.setattr(executor, "REMOVAL_VERIFY_S", 1.0)
    fake_docker("rm-fails")
    with pytest.raises(ExecutorError, match="still exists after removal"):
        py(dirs, "print('hi')")


def test_unverifiable_removal_fails_closed(dirs, fake_docker):
    fake_docker("inspect-fails")
    with pytest.raises(ExecutorError, match="cannot verify removal"):
        py(dirs, "print('hi')")


def test_create_timeout_fails_closed(dirs, fake_docker, monkeypatch):
    monkeypatch.setattr(executor, "CONTROL_TIMEOUT_S", 3.0)
    fake_docker("slow-create")
    with pytest.raises(ExecutorError, match="create timed out"):
        py(dirs, "print('hi')")


def test_replay_on_restored_clone_reproduces_after_state(dirs, tmp_path):
    """Roadmap §1.3 replay check: restore S_before into a clone, re-run the
    call, and compare the result with S_after on every compared field."""
    ws, base = dirs
    (ws / "data.txt").write_text("3\n")
    store = Store(tmp_path / "store")
    code = "n = int(open('data.txt').read()); open('data.txt','w').write(f'{n*2}\\n'); print(n*2)"

    before = store.capture(ws)
    original = run_call(IMAGE, ws, ["python3", "-c", code], fresh_out(base), Limits(timeout_s=60))
    after = store.capture(ws)

    clone = tmp_path / "clone"
    assert store.restore(before, clone) == []
    replay = run_call(IMAGE, clone, ["python3", "-c", code], fresh_out(base), Limits(timeout_s=60))

    assert replay.stdout == original.stdout == b"6\n"
    expected, actual = store.load_manifest(after), manifest(clone)
    content_fields = ("type", "mode", "uid", "gid", "size", "sha256", "link")
    assert diff(expected, actual, content_fields) == []
    # mtimes of written entries reflect wall-clock time of each run, so they
    # differ; this is the declared limit of replay, not a restore failure.
    assert {d["path"] for d in diff(expected, actual, ("mtime_ns",))} <= {".", "data.txt"}
