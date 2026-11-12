"""Phase 0 spike: can Inspect host a forklab fork?

Q1  Can a Sample take a message list with prior assistant tool calls and
    tool results as its input?
Q2  Can per-sample setup restore a workspace (with metadata) before the
    solver runs?
Q3  Does the first resumed model request carry the substituted tool result,
    byte for byte, in the provider payload?

The model is Inspect's real Anthropic provider pointed at a local stub
endpoint, so we observe the exact HTTP request bodies with no API key and
no spend. Run from the repo root:

    uv run python spikes/phase0_inspect_restore/spike.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import ClassVar

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

HERE = Path(__file__).parent
EVIDENCE = HERE / "evidence"
FORK_ID = "toolu_fork_0001"
LIVE_ID = "toolu_live_0001"
# Deliberately awkward bytes: trailing spaces, non-ASCII, no trailing newline.
SUBSTITUTED = "FAIL: add(2, 3) == -1  \n→ see src/other.py:12\t(arm b)"
EXPECTED_LIVE = "FAIL: add(2, 3) == -1\n"
MODEL = "anthropic/claude-sonnet-5-5"


# ---------------------------------------------------------------- stub API
class StubAnthropic(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.lock:
            self.requests.append({"path": self.path, "body": body})
        if not self.path.startswith("/v1/messages"):
            self.send_response(404)
            self.end_headers()
            return
        payload = json.loads(body)
        last = payload["messages"][-1]
        result_ids = [
            b.get("tool_use_id") for b in last.get("content", [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if FORK_ID in result_ids:
            content = [{"type": "tool_use", "id": LIVE_ID, "name": "run_tests", "input": {}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "done"}]
            stop = "end_turn"
        out = json.dumps({
            "id": "msg_stub", "type": "message", "role": "assistant",
            "model": payload.get("model"), "content": content,
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


def samples(entries: list[snapshot.Entry]) -> list[Sample]:
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
    return [
        Sample(id="tar_restore_a", input=prefix(), files=tar_files, setup=tar_setup),
        Sample(id="tar_restore_b", input=prefix(), files=tar_files, setup=tar_setup),
        Sample(id="files_only", input=prefix(), files=plain_files),
    ]


# ---------------------------------------------------------------- checks
def tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if c.get("type") == "text")


def check_first_turn(payload: dict) -> dict:
    msgs = payload["messages"]
    shape = [
        (m["role"], [b["type"] for b in m["content"]] if isinstance(m["content"], list) else "str")
        for m in msgs
    ]
    tool_use = [b for m in msgs if isinstance(m["content"], list)
                for b in m["content"] if b["type"] == "tool_use"]
    tool_res = [b for m in msgs if isinstance(m["content"], list)
                for b in m["content"] if b["type"] == "tool_result"]
    last = tool_res[-1] if tool_res else {}
    return {
        "message_shape": shape,
        "system_present": bool(payload.get("system")),
        "tools_declared": [t.get("name") for t in payload.get("tools", [])],
        "tool_use_ids": [b["id"] for b in tool_use],
        "tool_result_ids": [b["tool_use_id"] for b in tool_res],
        "last_message_is_substituted_result": (
            msgs[-1]["role"] == "user" and last.get("tool_use_id") == FORK_ID
        ),
        "substituted_bytes_identical": tool_result_text(last) == SUBSTITUTED,
        "substituted_text_seen": tool_result_text(last),
    }


def main() -> int:
    EVIDENCE.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    entries = snapshot.fixture_entries()
    expected = snapshot.expected_manifest(entries)
    model = get_model(
        MODEL, base_url=base_url, api_key="stub-key", streaming=False,
        config=GenerateConfig(max_tokens=1024, max_retries=0, timeout=30),
    )
    task = Task(
        dataset=samples(entries),
        solver=[capture_manifest(), use_tools(run_tests()), generate(tool_calls="loop")],
        sandbox=("docker", str(HERE / "compose.yaml")),
        message_limit=10,
    )
    [log] = eval(task, model=model, log_dir=str(EVIDENCE / "logs"),
                 max_samples=1, display="none")
    server.shutdown()

    per_sample = {}
    for s in log.samples or []:
        raw = s.store.get("manifest")
        actual = json.loads(raw) if raw else {}
        per_sample[s.id] = {
            "error": s.error.message if s.error else None,
            "manifest_stderr": s.store.get("manifest_stderr"),
            "restore_diff": snapshot.diff(expected, actual) if raw else "manifest unavailable",
            "final_assistant": s.output.completion if s.output else None,
        }

    reqs = [r for r in StubAnthropic.requests]
    msg_reqs = [r for r in reqs if r["path"].startswith("/v1/messages")]
    first, second = [], []
    for r in msg_reqs:
        p = json.loads(r["body"])
        ids = [b.get("tool_use_id") for b in p["messages"][-1]["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(p["messages"][-1]["content"], list) else []
        (first if FORK_ID in ids else second).append((r, p))

    first_checks = [check_first_turn(p) for _, p in first]
    body_hashes = [hashlib.sha256(r["body"]).hexdigest() for r, _ in first]
    live_results = [
        tool_result_text([b for b in p["messages"][-1]["content"] if b["type"] == "tool_result"][-1])
        for _, p in second
    ]
    (EVIDENCE / "first_turn_request.json").write_text(
        json.dumps(first[0][1], indent=2, sort_keys=True, ensure_ascii=False) if first else "{}"
    )

    tar_ok = all(per_sample.get(k, {}).get("restore_diff") == [] for k in ("tar_restore_a", "tar_restore_b"))
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "inspect_ai": version("inspect_ai"),
            "anthropic_sdk": version("anthropic"),
            "docker_server": subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, check=False).stdout.strip(),
            "model_string": MODEL,
        },
        "requests_seen": [{"path": r["path"], "bytes": len(r["body"])} for r in reqs],
        "samples": per_sample,
        "first_turn": first_checks,
        "first_turn_body_sha256": body_hashes,
        "second_turn_live_tool_results": live_results,
        "verdict": {
            "Q1_prefix_accepted_as_input": bool(first_checks) and all(
                c["tool_use_ids"] == [FORK_ID] and c["tool_result_ids"] == [FORK_ID]
                for c in first_checks),
            "Q2_tar_setup_restore_exact": tar_ok,
            "Q2_files_only_restore_exact": per_sample.get("files_only", {}).get("restore_diff") == [],
            "Q3_substituted_result_byte_identical": bool(first_checks) and all(
                c["substituted_bytes_identical"] and c["last_message_is_substituted_result"]
                for c in first_checks),
            "first_turn_payloads_byte_identical": len(set(body_hashes)) == 1 and len(body_hashes) == 3,
            "loop_resumed_and_executed_tool_in_restored_workspace": (
                len(live_results) >= 2 and all(t == EXPECTED_LIVE for t in live_results[:2])
            ),
        },
    }
    (EVIDENCE / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False))
    print(json.dumps(results["verdict"], indent=2))
    required = ("Q1_prefix_accepted_as_input", "Q2_tar_setup_restore_exact",
                "Q3_substituted_result_byte_identical")
    return 0 if all(results["verdict"][k] for k in required) else 1


if __name__ == "__main__":
    sys.exit(main())
