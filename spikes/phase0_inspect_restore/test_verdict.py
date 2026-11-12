"""Regression tests: missing, failed or malformed samples must never pass."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from verdict import (
    EXPECTED_COMPLETION,
    EXPECTED_LIVE,
    EXPECTED_SAMPLES,
    FILES_ONLY_SAMPLE,
    FORK_ID,
    LIVE_ID,
    SUBSTITUTED,
    SampleRecord,
    evaluate,
)


def first_body(text: str = SUBSTITUTED, sort_keys: bool = False) -> bytes:
    payload = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "run"},
                {"type": "tool_use", "id": FORK_ID, "name": "run_tests", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": FORK_ID,
                 "content": [{"type": "text", "text": text}]},
            ]},
        ],
    }
    return json.dumps(payload, sort_keys=sort_keys).encode()


def second_body(text: str = EXPECTED_LIVE) -> bytes:
    payload = json.loads(first_body())
    payload["messages"] += [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": LIVE_ID, "name": "run_tests", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": LIVE_ID, "content": text}]},
    ]
    return json.dumps(payload).encode()


def good(sample_id: str) -> SampleRecord:
    diff = [{"path": "x", "field": "mode"}] if sample_id == FILES_ONLY_SAMPLE else []
    return SampleRecord(
        manifest={},
        restore_diff=diff,
        requests=[("/v1/messages", first_body()), ("/v1/messages", second_body())],
        completion=EXPECTED_COMPLETION,
    )


def all_good() -> dict[str, SampleRecord]:
    return {sid: good(sid) for sid in EXPECTED_SAMPLES}


def test_all_good_passes():
    v = evaluate(all_good())["verdict"]
    assert v["passed"]
    assert v["Q1_prefix_accepted_as_input"]
    assert v["Q2_tar_setup_restore_exact"]
    assert v["Q3_substituted_text_equal_after_json_decode"]
    assert v["loop_resumed_with_live_result"]
    assert not v["observed_files_only_restore_exact"]


@pytest.mark.parametrize("missing", EXPECTED_SAMPLES)
def test_missing_sample_fails(missing):
    records = all_good()
    del records[missing]
    v = evaluate(records)["verdict"]
    assert not v["passed"]
    assert v["missing_samples"] == [missing]
    for key in ("Q1_prefix_accepted_as_input", "Q3_substituted_text_equal_after_json_decode",
                "loop_resumed_with_live_result"):
        assert not v[key]


def test_unexpected_sample_fails():
    records = all_good()
    records["extra"] = good("extra")
    assert not evaluate(records)["verdict"]["passed"]


def test_no_samples_fails():
    assert not evaluate({})["verdict"]["passed"]


@pytest.mark.parametrize("sid", EXPECTED_SAMPLES)
@pytest.mark.parametrize("mutate", [
    pytest.param(lambda r: setattr(r, "error", "boom"), id="sample-error"),
    pytest.param(lambda r: setattr(r, "requests", []), id="no-requests"),
    pytest.param(lambda r: setattr(r, "requests", r.requests[:1]), id="first-only"),
    pytest.param(lambda r: setattr(r, "requests", r.requests[1:]), id="second-only"),
    pytest.param(lambda r: r.requests.append(r.requests[-1]), id="extra-request"),
    pytest.param(lambda r: r.requests.insert(0, ("/v1/other", b"{}")), id="other-endpoint"),
    pytest.param(lambda r: setattr(r, "requests", [("/v1/messages", b"not json")] * 2),
                 id="malformed-body"),
    pytest.param(lambda r: r.requests.__setitem__(0, ("/v1/messages", first_body("other"))),
                 id="wrong-substituted-text"),
    pytest.param(lambda r: r.requests.__setitem__(1, ("/v1/messages", second_body("PASS\n"))),
                 id="wrong-live-result"),
    pytest.param(lambda r: setattr(r, "completion", None), id="no-completion"),
    pytest.param(lambda r: setattr(r, "manifest", None), id="no-manifest"),
    pytest.param(lambda r: setattr(r, "restore_diff", None), id="no-restore-diff"),
])
def test_any_failed_sample_fails_overall(sid, mutate):
    records = all_good()
    mutate(records[sid])
    result = evaluate(records)
    assert not result["samples"][sid]["passed"]
    assert not result["verdict"]["passed"]


@pytest.mark.parametrize("sid", ["tar_restore_a", "tar_restore_b"])
def test_tar_restore_diff_fails(sid):
    records = all_good()
    records[sid].restore_diff = [{"path": "src", "field": "mtime_ns"}]
    v = evaluate(records)["verdict"]
    assert not v["Q2_tar_setup_restore_exact"]
    assert not v["passed"]


def test_raw_bytes_differ_but_decoded_text_equal():
    records = all_good()
    records["tar_restore_b"].requests[0] = ("/v1/messages", first_body(sort_keys=True))
    v = evaluate(records)["verdict"]
    assert not v["observed_first_request_raw_bytes_identical"]
    assert v["Q3_substituted_text_equal_after_json_decode"]
    assert v["passed"]
