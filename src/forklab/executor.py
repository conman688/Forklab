"""Per-call disposable container executor (roadmap §1.1).

Every tool call runs in a fresh container with:
- the root filesystem read-only and no network;
- all capabilities dropped and no-new-privileges set;
- pid, memory and CPU limits;
- a size-limited tmpfs at /tmp that is discarded when the call ends;
- no daemon-side log storage (`--log-driver none`).

Only the host workspace (/workspace) and a per-call output dir (/out) are
writable.

Lifecycle: `docker create` returns the container ID synchronously; the call
is then run with `docker start --attach <id>`, and in every path the
container is removed by that ID and its absence is verified with
`docker inspect`. If removal cannot be verified, run_call raises
ExecutorError. Removing the container kills every process in its PID
namespace.

stdout and stderr are drained concurrently while the call runs; only the
first `output_bytes` of each are kept and the rest is discarded, so captured
output uses bounded memory and no disk.

NOT BOUNDED: disk writes to /workspace and /out. They are plain bind mounts
with no storage quota, so agent code can fill the host (or Docker VM) disk.
Neither the timeout nor the memory limit bounds filesystem writes. The
executor is therefore NOT safe against disk exhaustion, and must not be used
for untrusted trials until a quota-backed workspace and output mechanism
exists (roadmap §1.1 precondition).

The container runs as the host runner's uid:gid (non-root), not a fixed
1000:1000. That keeps files written to the bind-mounted workspace owned by
the runner, so host-side snapshots compare ownership exactly.

Not detected: network attempts (they fail but are not logged), writes to
/tmp, time dependence, side channels.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Overridable in tests to inject daemon faults (slow start, failed removal).
DOCKER = "docker"

FIXED_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
    "LANG": "C.UTF-8",
    "HOME": "/tmp",
}

# Docker reports its own failures with exit codes (125-127) that a tool can
# also return, so exit codes cannot tell "the call ran" from "docker failed".
# Instead a launcher inside the container writes a start marker to /out, then
# execs the call. No marker means the container never ran the call: an
# infrastructure failure. Code under test could delete the marker, which would
# turn its result into an infrastructure failure; it cannot turn a docker
# failure into a result. The marker stays in out_dir.
#
# out_dir must be fresh and empty for every call. On Docker Desktop (macOS,
# virtiofs) a bind-mounted directory reused after the host deleted a file
# that a container created can refuse to recreate that file in the next
# container ("Directory nonexistent"). For the same reason the host must
# never mutate a workspace between calls; it only reads it (snapshots).
START_MARKER = ".forklab-started"
LAUNCHER = ["/bin/sh", "-c", f': > /out/{START_MARKER} && exec "$@"', "forklab-launcher"]

CONTROL_TIMEOUT_S = 60.0  # for docker create / rm / inspect
REMOVAL_VERIFY_S = 10.0


class ExecutorError(Exception):
    """The isolation contract could not be established or verified."""


@dataclass(frozen=True)
class Limits:
    pids: int = 256
    memory: str = "2g"
    cpus: str = "2"
    tmpfs_size: str = "256m"
    timeout_s: float = 120.0
    output_bytes: int = 1 << 20


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class CallResult:
    exit_code: int | None  # None when the call timed out
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_total_bytes: int  # bytes produced, including discarded ones
    stderr_total_bytes: int
    timed_out: bool
    duration_s: float
    container_id: str


def create_args(image: str, workspace: Path, out_dir: Path, name: str, limits: Limits) -> list[str]:
    args = [
        DOCKER, "create", "--name", name,
        "--log-driver", "none",
        "--read-only",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(limits.pids),
        "--memory", limits.memory, "--memory-swap", limits.memory,
        "--cpus", limits.cpus,
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_size},mode=1777",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--hostname", "forklab",
        "--workdir", "/workspace",
        "--mount", f"type=bind,source={workspace},target=/workspace",
        "--mount", f"type=bind,source={out_dir},target=/out",
    ]
    for key, value in FIXED_ENV.items():
        args += ["--env", f"{key}={value}"]
    return [*args, image]


class _CappedReader(threading.Thread):
    """Drain a pipe to EOF, keeping only the first `cap` bytes."""

    def __init__(self, pipe, cap: int) -> None:
        super().__init__(daemon=True)
        self.pipe, self.cap = pipe, cap
        self.kept = bytearray()
        self.total = 0

    def run(self) -> None:
        for chunk in iter(lambda: self.pipe.read(65536), b""):
            self.total += len(chunk)
            room = self.cap - len(self.kept)
            if room > 0:
                self.kept += chunk[:room]
        self.pipe.close()


def _docker(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([DOCKER, *args], capture_output=True, text=True,
                              check=False, timeout=CONTROL_TIMEOUT_S)
    except subprocess.TimeoutExpired as ex:
        raise ExecutorError(f"docker {args[0]} timed out") from ex


def _remove_and_verify(ref: str) -> None:
    """Remove container `ref` (ID or name) and prove it no longer exists."""
    rm = _docker("rm", "--force", ref)
    deadline = time.monotonic() + REMOVAL_VERIFY_S
    while True:
        r = _docker("inspect", "--format", "{{.Id}}", ref)
        if r.returncode != 0 and "no such" in r.stderr.lower():
            return
        if r.returncode != 0:
            raise ExecutorError(f"cannot verify removal of {ref}: {r.stderr.strip()}")
        if time.monotonic() > deadline:
            raise ExecutorError(
                f"container {ref} still exists after removal "
                f"(rm exit {rm.returncode}: {rm.stderr.strip()})"
            )
        time.sleep(0.2)


def run_call(
    image: str,
    workspace: str | os.PathLike,
    argv: list[str],
    out_dir: str | os.PathLike,
    limits: Limits = DEFAULT_LIMITS,
) -> CallResult:
    """Run `argv` once in a disposable container against `workspace`.

    `out_dir` must be an existing, empty directory used for this call only.
    Raises ExecutorError for any infrastructure failure, including a
    container whose removal cannot be verified.
    """
    workspace = Path(workspace).resolve(strict=True)
    out_dir = Path(out_dir).resolve(strict=True)
    if not workspace.is_dir() or not out_dir.is_dir():
        raise ExecutorError("workspace and out_dir must be existing directories")
    if "@sha256:" not in image:
        raise ExecutorError(f"image must be pinned by digest: {image!r}")
    if any(out_dir.iterdir()):
        raise ExecutorError(f"out_dir must be a fresh, empty directory: {out_dir}")
    marker = out_dir / START_MARKER

    name = f"forklab-{uuid.uuid4().hex}"
    try:
        created = subprocess.run(
            [*create_args(image, workspace, out_dir, name, limits), *LAUNCHER, *argv],
            capture_output=True, text=True, check=False, timeout=CONTROL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as ex:
        # The daemon may still create the container after this point, so a
        # name lookup now cannot prove there is none. Remove what exists and
        # fail closed, naming the container an operator may need to clean up.
        _remove_and_verify(name)
        raise ExecutorError(
            f"docker create timed out; a container named {name} may still appear"
        ) from ex
    if created.returncode != 0:
        _remove_and_verify(name)
        raise ExecutorError(f"docker create failed: {created.stderr.strip()}")
    cid = created.stdout.strip()

    timed_out = False
    exit_code: int | None = None
    proc: subprocess.Popen | None = None
    readers: list[_CappedReader] = []
    start = time.monotonic()
    try:
        proc = subprocess.Popen([DOCKER, "start", "--attach", cid], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        readers = [_CappedReader(proc.stdout, limits.output_bytes),
                   _CappedReader(proc.stderr, limits.output_bytes)]
        for r in readers:
            r.start()
        try:
            exit_code = proc.wait(timeout=limits.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        try:
            _remove_and_verify(cid)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            for r in readers:
                r.join(timeout=10)
    duration = time.monotonic() - start
    out_r, err_r = readers
    stderr = bytes(err_r.kept)

    if not marker.exists():
        raise ExecutorError(
            f"container never started the call (exit {exit_code}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return CallResult(
        exit_code=None if timed_out else exit_code,
        stdout=bytes(out_r.kept), stderr=stderr,
        stdout_truncated=out_r.total > limits.output_bytes,
        stderr_truncated=err_r.total > limits.output_bytes,
        stdout_total_bytes=out_r.total, stderr_total_bytes=err_r.total,
        timed_out=timed_out, duration_s=duration, container_id=cid,
    )
