"""Fork transcript builder: refusal rules and offline provider-payload contract."""

from __future__ import annotations

import json

import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentData,
    ContentImage,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    get_model,
)
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import ToolCall, tool
from stub_anthropic import FINAL_TEXT, start_stub

from forklab.fork import ForkRefused, build_fork_prefix

FORK = "toolu_fork_0002"
EARLIER = "toolu_rec_0001"
LIVE = "toolu_live_0001"
RECORDED = "FAIL: add(2, 3) == -1\n"
SUBSTITUTED = "FAIL in src/other.py:12  \n→ assertion\t(arm b)"
LIVE_RESULT = "live result\n"


def recording(fork_turn_content=None, early_content=None) -> list:
    """A recorded trajectory: one earlier tool round, then the fork call and its real result."""
    return [
        ChatMessageSystem(content="You are a coding agent."),
        ChatMessageUser(content="Fix the bug."),
        ChatMessageAssistant(
            content=early_content if early_content is not None else "Let me look.",
            tool_calls=[ToolCall(id=EARLIER, function="list_dir", arguments={"path": "."})],
        ),
        ChatMessageTool(content="src\ntests\n", tool_call_id=EARLIER, function="list_dir"),
        ChatMessageAssistant(
            content=fork_turn_content if fork_turn_content is not None else "Running tests.",
            tool_calls=[ToolCall(id=FORK, function="run_tests", arguments={})],
        ),
        ChatMessageTool(content=RECORDED, tool_call_id=FORK, function="run_tests"),
        ChatMessageAssistant(content="The add function subtracts."),
    ]


# ------------------------------------------------------------ builder rules
def test_prefix_is_recording_up_to_fork_call_plus_substitution():
    rec = recording()
    before = [m.model_dump() for m in rec]
    prefix = build_fork_prefix(rec, FORK, SUBSTITUTED)
    assert [m.model_dump() for m in prefix[:-1]] == before[:5]
    last = prefix[-1]
    assert isinstance(last, ChatMessageTool)
    assert (last.tool_call_id, last.function, last.content) == (FORK, "run_tests", SUBSTITUTED)
    assert all(m.content != RECORDED for m in prefix)
    assert [m.model_dump() for m in rec] == before  # recording not mutated
    assert all(a is not b for a, b in zip(prefix, rec, strict=False))  # deep copies


@pytest.mark.parametrize("where", ["fork_turn", "earlier_turn"])
@pytest.mark.parametrize("part", [
    pytest.param(ContentReasoning(reasoning="hmm", signature="sig"), id="thinking"),
    pytest.param(ContentReasoning(reasoning="", signature="sig", redacted=True), id="redacted-thinking"),
    pytest.param(ContentData(data={"opaque": 1}), id="provider-data"),
    pytest.param(ContentImage(image="data:image/png;base64,AAAA"), id="image"),
])
def test_non_text_content_in_prefix_is_refused(where, part):
    content = [part, ContentText(text="ok")]
    rec = recording(fork_turn_content=content) if where == "fork_turn" else recording(early_content=content)
    with pytest.raises(ForkRefused, match="only plain text"):
        build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_text_parts_are_allowed():
    rec = recording(fork_turn_content=[ContentText(text="a"), ContentText(text="b")])
    assert build_fork_prefix(rec, FORK, SUBSTITUTED)[-1].content == SUBSTITUTED


def test_reasoning_after_the_fork_point_does_not_block():
    rec = recording()
    rec[-1] = ChatMessageAssistant(content=[ContentReasoning(reasoning="later", signature="s"),
                                            ContentText(text="x")])
    build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_unknown_call_id_is_refused():
    with pytest.raises(ForkRefused, match="found in 0"):
        build_fork_prefix(recording(), "toolu_nope", SUBSTITUTED)


def test_duplicate_call_id_is_refused():
    rec = recording()
    rec.insert(2, ChatMessageAssistant(content="dup", tool_calls=[ToolCall(id=FORK, function="run_tests", arguments={})]))
    with pytest.raises(ForkRefused, match="found in 2"):
        build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_multi_call_turn_is_refused():
    rec = recording()
    rec[4] = ChatMessageAssistant(content="two", tool_calls=[
        ToolCall(id=FORK, function="run_tests", arguments={}),
        ToolCall(id="toolu_sib", function="list_dir", arguments={}),
    ])
    with pytest.raises(ForkRefused, match="single-call"):
        build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_missing_recorded_result_is_refused():
    rec = recording()[:5]
    with pytest.raises(ForkRefused, match="0 results"):
        build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_unanswered_earlier_call_is_refused():
    rec = recording()
    del rec[3]
    with pytest.raises(ForkRefused, match="without exactly one result"):
        build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_tool_message_for_unknown_call_is_refused():
    rec = recording()
    rec.insert(2, ChatMessageTool(content="x", tool_call_id="toolu_ghost", function="f"))
    with pytest.raises(ForkRefused, match="unknown tool call"):
        build_fork_prefix(rec, FORK, SUBSTITUTED)


def test_non_string_substitution_is_refused():
    with pytest.raises(ForkRefused, match="string"):
        build_fork_prefix(recording(), FORK, ["not", "a", "string"])


# ------------------------------------------------------------ payload contract
@tool
def run_tests():
    async def execute() -> str:
        """Run the tests."""
        return LIVE_RESULT

    return execute


@tool
def list_dir():
    async def execute(path: str) -> str:
        """List a directory.

        Args:
            path: Directory to list.
        """
        return ""

    return execute


def _blocks(payload: dict):
    for m in payload["messages"]:
        if isinstance(m["content"], list):
            yield from m["content"]


def test_provider_payload_carries_fork_prefix_without_thinking_blocks(tmp_path):
    prefix = build_fork_prefix(recording(), FORK, SUBSTITUTED)
    server, received = start_stub(FORK, LIVE, "run_tests")
    try:
        model = get_model(
            "anthropic/claude-sonnet-5-5",
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            api_key="stub-key", streaming=False, memoize=False,
            config=GenerateConfig(max_tokens=1024, max_retries=0, timeout=30),
        )
        [log] = eval(
            Task(dataset=[Sample(input=prefix)],
                 solver=[use_tools(list_dir(), run_tests()), generate(tool_calls="loop")],
                 message_limit=12),
            model=model, log_dir=str(tmp_path / "logs"), display="none",
        )
    finally:
        server.shutdown()

    assert log.status == "success" and len(log.samples) == 1 and log.samples[0].error is None
    assert log.samples[0].output.completion == FINAL_TEXT
    assert [p for p, _ in received] == ["/v1/messages", "/v1/messages"]
    first, second = (json.loads(b) for _, b in received)

    # Shape: user, assistant[text, tool_use], user[tool_result], assistant[text, tool_use], user[tool_result]
    roles = [m["role"] for m in first["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    blocks = list(_blocks(first))
    assert [b["id"] for b in blocks if b["type"] == "tool_use"] == [EARLIER, FORK]
    results = [b for b in blocks if b["type"] == "tool_result"]
    assert [b["tool_use_id"] for b in results] == [EARLIER, FORK]
    fork_text = "".join(c["text"] for c in results[-1]["content"] if c["type"] == "text")
    assert fork_text == SUBSTITUTED

    # Fail-closed thinking rule: no reasoning blocks are replayed in the prefix.
    assert not any(b["type"] in ("thinking", "redacted_thinking") for b in blocks)
    # The recorded (replaced) result never reaches the provider.
    assert RECORDED not in json.dumps(first)
    # Inspect still requests adaptive thinking with drop_block for this model;
    # harmless only because the prefix carries no thinking blocks to drop.
    assert first.get("thinking", {}).get("block_binding") == {"prefix_mismatch_behavior": "drop_block"}

    last = second["messages"][-1]["content"][-1]
    assert last["tool_use_id"] == LIVE
