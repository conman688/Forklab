"""Phase 0 spike: can Inspect host a forklab fork?

Q1  Can a Sample take a message list with prior assistant tool calls and
    tool results as its input?
Q2  Can per-sample setup restore a workspace (with metadata) before the
    solver runs?
Q3  Does the first resumed model request carry the substituted tool result
    (equal text after JSON decoding) in the provider payload?

The model is Inspect's real Anthropic provider pointed at a local stub
endpoint, so we observe the exact HTTP request bodies with no API key and
no spend. Each sample runs in its own eval with its own stub endpoint, so
every request is attributed to exactly one sample. Pass/fail is decided by
`verdict.evaluate`. Run from the repo root:

    uv run python spikes/phase0_inspect_restore/spike.py
"""

from __future__ import annotations

import base64
import json
import platform
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)
from inspect_ai.solver import Generate, TaskState, generate, solver, use_tools
from inspect_ai.tool import ToolCall, tool
from inspect_ai.util import sandbox

sys.path.insert(0, str(Path(__file__).parent))
import snapshot
from verdict import (
    EXPECTED_COMPLETION,
    EXPECTED_SAMPLES,
    FORK_ID,
    LIVE_ID,
    SUBSTITUTED,
    SampleRecord,
    evaluate,
)

HERE = Path(__file__).parent
EVIDENCE = HERE / "evidence"
MODEL = "anthropic/claude-sonnet-5-5"


# ---------------------------------------------------------------- stub API
def start_stub(fail: bool = False) -> tuple[ThreadingHTTPServer, list[tuple[str, bytes]]]:
    """Start a stub Anthropic endpoint that records (path, raw body) pairs.

    With fail=True it answers every request with HTTP 500 (negative control).
    """
    received: list[tuple[str, bytes]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                received.append((self.path, body))
            if fail:
                self.send_response(500)
                self.end_headers()
                return
            if not self.path.startswith("/v1/messages"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                last = json.loads(body)["messages"][-1]
                result_ids = [
                    b.get("tool_use_id") for b in last.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                self.send_response(400)
                self.end_headers()
                return
            if FORK_ID in result_ids:
                content = [{"type": "tool_use", "id": LIVE_ID, "name": "run_tests", "input": {}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": EXPECTED_COMPLETION}]
                stop = "end_turn"
            out = json.dumps({
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": "stub", "content": content,
                "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


# ---------------------------------------------------------------- task
@tool
def run_tests():
    async def execute() -> str:
        """Run the test suite in /workspace."""
        result = await sandbox().exec(["python3", "/workspace/tests/run.py"])
        return result.stdout + result.stderr

    return execute


@solver
def capture_manifest():
    """First solver step: record what the sandbox looks like before the agent acts."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        result = await sandbox().exec(
            ["python3", "/opt/forklab/snapshot.py", "manifest", "/workspace"]
        )
        state.store.set("manifest", result.stdout if result.success else None)
        state.store.set("manifest_stderr", result.stderr)
        return state

    return solve


def prefix() -> list:
    return [
        ChatMessageSystem(content="You are a coding agent. Fix the bug in /workspace."),
        ChatMessageUser(content="The tests fail. Investigate and fix."),
        ChatMessageAssistant(
            content="I'll run the tests.",
            tool_calls=[ToolCall(id=FORK_ID, function="run_tests", arguments={})],
        ),
        ChatMessageTool(content=SUBSTITUTED, tool_call_id=FORK_ID, function="run_tests"),
    ]


def data_uri(data: bytes) -> str:
    return "data:application/octet-stream;base64," + base64.b64encode(data).decode()


def samples(entries: list[snapshot.Entry]) -> dict[str, Sample]:
    tool_file = {"/opt/forklab/snapshot.py": str(HERE / "snapshot.py")}
    tar_files = {**tool_file, "/tmp/forklab_snapshot.tar": data_uri(snapshot.build_tar(entries))}
    tar_setup = (
        "set -e\n"
        "tar --numeric-owner --same-owner -xpf /tmp/forklab_snapshot.tar -C /workspace\n"
        "rm /tmp/forklab_snapshot.tar\n"
    )
    plain_files = {
        **tool_file,
        **{f"/workspace/{e.path}": data_uri(e.data) for e in entries if e.type == "file"},
    }
    built = {
        "tar_restore_a": Sample(id="tar_restore_a", input=prefix(), files=tar_files, setup=tar_setup),
        "tar_restore_b": Sample(id="tar_restore_b", input=prefix(), files=tar_files, setup=tar_setup),
        "files_only": Sample(id="files_only", input=prefix(), files=plain_files),
    }
    assert tuple(built) == EXPECTED_SAMPLES
    return built


def run_one(sample: Sample, expected: dict, fail: bool = False) -> SampleRecord | None:
    """Run one sample in its own eval against its own stub endpoint.

    Returns None if the eval produced no record for this sample, so the
    evaluator sees it as missing rather than as a pass.
    """
    server, received = start_stub(fail)
    try:
        model = get_model(
            MODEL,
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            api_key="stub-key",
            streaming=False,
            memoize=False,
            config=GenerateConfig(max_tokens=1024, max_retries=0, timeout=30),
        )
        task = Task(
            dataset=[sample],
            solver=[capture_manifest(), use_tools(run_tests()), generate(tool_calls="loop")],
            sandbox=("docker", str(HERE / "compose.yaml")),
            message_limit=10,
        )
        try:
            [log] = eval(task, model=model, log_dir=str(EVIDENCE / "logs"), display="none")
        except Exception as ex:  # noqa: BLE001 -- an eval crash is a failed sample, not a skipped one
            return SampleRecord(error=f"eval raised: {ex!r}", requests=list(received))
    finally:
        server.shutdown()

    matches = [s for s in log.samples or [] if s.id == sample.id]
    if len(matches) != 1:
        return None
    s = matches[0]
    raw = s.store.get("manifest")
    try:
        manifest = json.loads(raw) if raw else None
    except ValueError:
        manifest = None
    error = s.error.message if s.error else None
    if error is None and log.status != "success":
        error = f"eval status {log.status}"
    return SampleRecord(
        error=error,
        manifest=manifest,
        restore_diff=snapshot.diff(expected, manifest) if manifest is not None else None,
        requests=list(received),
        completion=s.output.completion if s.output else None,
    )


def main() -> int:
    """Usage: spike.py [--fail-sample SAMPLE_ID]

    --fail-sample makes that sample's stub return HTTP 500, as an end-to-end
    negative control; the run must then exit 1. Evidence goes to
    evidence/negative_control/ so the positive evidence is not overwritten.
    """
    global EVIDENCE
    fail_sample = None
    if len(sys.argv) == 3 and sys.argv[1] == "--fail-sample":
        fail_sample = sys.argv[2]
        if fail_sample not in EXPECTED_SAMPLES:
            sys.exit(f"unknown sample {fail_sample!r}; expected one of {EXPECTED_SAMPLES}")
        EVIDENCE = EVIDENCE / "negative_control"
    elif len(sys.argv) != 1:
        sys.exit("usage: spike.py [--fail-sample SAMPLE_ID]")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    entries = snapshot.fixture_entries()
    expected = snapshot.expected_manifest(entries)

    records: dict[str, SampleRecord] = {}
    for sid, sample in samples(entries).items():
        rec = run_one(sample, expected, fail=(sid == fail_sample))
        if rec is not None:
            records[sid] = rec

    result = evaluate(records)
    first = next(
        (r.requests[0][1] for r in records.values() if r.requests), None
    )
    (EVIDENCE / "first_turn_request.json").write_text(
        json.dumps(json.loads(first), indent=2, sort_keys=True, ensure_ascii=False) if first else "{}"
    )
    for sid, rec in records.items():
        result["samples"][sid]["error"] = rec.error
        result["samples"][sid]["restore_diff"] = rec.restore_diff
        result["samples"][sid]["request_paths"] = [p for p, _ in rec.requests]
    result["generated_at"] = datetime.now(UTC).isoformat()
    result["negative_control_failed_sample"] = fail_sample
    result["environment"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "inspect_ai": version("inspect_ai"),
        "anthropic_sdk": version("anthropic"),
        "docker_server": subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, check=False).stdout.strip(),
        "model_string": MODEL,
    }
    (EVIDENCE / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
    )
    print(json.dumps(result["verdict"], indent=2))
    return 0 if result["verdict"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
