"""Per-call disposable container executor (roadmap §1.1).

Every tool call runs in a fresh `docker run --rm` container with:
- the root filesystem read-only and no network;
- all capabilities dropped and no-new-privileges set;
- pid, memory and CPU limits;
- a size-limited tmpfs at /tmp that is discarded when the call ends.

Only the host workspace (/workspace) and a per-call output dir (/out) are
writable. When the call ends, the container is removed, which kills every
process in its PID namespace; the executor then checks that the container
is gone and raises if it is not.

The container runs as the host runner's uid:gid (non-root), not a fixed
1000:1000. That keeps files written to the bind-mounted workspace owned by
the runner, so host-side snapshots compare ownership exactly.

Not detected: network attempts (they fail but are not logged), writes to
/tmp, time dependence, side channels.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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
    timed_out: bool
    duration_s: float
    container_name: str


def docker_args(image: str, workspace: Path, out_dir: Path, name: str, limits: Limits) -> list[str]:
    args = [
        "docker", "run", "--rm", "--name", name,
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


def _read_capped(f, cap: int) -> tuple[bytes, bool]:
    f.seek(0)
    data = f.read(cap + 1)
    return data[:cap], len(data) > cap


def _container_exists(name: str) -> bool:
    r = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"name=^{name}$"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise ExecutorError(f"docker ps failed: {r.stderr.strip()}")
    return bool(r.stdout.strip())


def run_call(
    image: str,
    workspace: str | os.PathLike,
    argv: list[str],
    out_dir: str | os.PathLike,
    limits: Limits = DEFAULT_LIMITS,
) -> CallResult:
    """Run `argv` once in a disposable container against `workspace`.

    `out_dir` must be an existing, empty directory used for this call only.
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
    cmd = [*docker_args(image, workspace, out_dir, name, limits), *LAUNCHER, *argv]
    timed_out = False
    start = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            exit_code: int | None = proc.wait(timeout=limits.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
        finally:
            # The docker CLI exiting does not stop the container; remove it explicitly.
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        duration = time.monotonic() - start
        stdout, out_trunc = _read_capped(out, limits.output_bytes)
        stderr, err_trunc = _read_capped(err, limits.output_bytes)

    if _container_exists(name):
        raise ExecutorError(f"container {name} still exists after the call")
    if not marker.exists():
        raise ExecutorError(
            f"container never started the call (exit {exit_code}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return CallResult(exit_code, stdout, stderr, out_trunc, err_trunc, timed_out, duration, name)
