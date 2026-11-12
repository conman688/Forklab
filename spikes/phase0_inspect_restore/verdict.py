"""Strict, pure evaluation of Phase 0 spike observations.

Kept free of Inspect/Docker so the pass/fail logic can be regression-tested
offline. A sample passes only if every required check holds; the spike
passes only if exactly the expected samples ran and all of them passed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

FORK_ID = "toolu_fork_0001"
LIVE_ID = "toolu_live_0001"
# Deliberately awkward text: trailing spaces, non-ASCII, a tab, no trailing newline.
SUBSTITUTED = "FAIL: add(2, 3) == -1  \n→ see src/other.py:12\t(arm b)"
EXPECTED_LIVE = "FAIL: add(2, 3) == -1\n"
EXPECTED_COMPLETION = "done"

TAR_SAMPLES = ("tar_restore_a", "tar_restore_b")
FILES_ONLY_SAMPLE = "files_only"
EXPECTED_SAMPLES = (*TAR_SAMPLES, FILES_ONLY_SAMPLE)


@dataclass
class SampleRecord:
    """What one sample run produced. `requests` are raw (path, body) pairs
    received by that sample's own stub endpoint, in arrival order."""

    error: str | None = None
    manifest: dict | None = None
    restore_diff: list | None = None
    requests: list[tuple[str, bytes]] = field(default_factory=list)
    completion: str | None = None


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _tool_result_text(block: dict) -> str | None:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if c.get("type") == "text")
    return None


def _last_tool_result(payload: dict) -> dict | None:
    messages = payload.get("messages") or []
    if not messages:
        return None
    results = [b for b in _blocks(messages[-1]) if b.get("type") == "tool_result"]
    return results[-1] if len(results) == 1 else None


def _decode(body: bytes) -> dict | None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def check_sample(sample_id: str, rec: SampleRecord) -> dict:
    """Return named checks for one sample; every value is a bool except notes."""
    paths_ok = all(path.startswith("/v1/messages") for path, _ in rec.requests)
    two = len(rec.requests) == 2
    first = _decode(rec.requests[0][1]) if two else None
    second = _decode(rec.requests[1][1]) if two else None

    fork_ids_ok = False
    substituted_equal = False
    if first is not None:
        msgs = first.get("messages") or []
        tool_use_ids = [b.get("id") for m in msgs for b in _blocks(m) if b.get("type") == "tool_use"]
        tool_result_ids = [
            b.get("tool_use_id") for m in msgs for b in _blocks(m) if b.get("type") == "tool_result"
        ]
        last = _last_tool_result(first)
        fork_ids_ok = (
            tool_use_ids == [FORK_ID]
            and tool_result_ids == [FORK_ID]
            and last is not None
            and last.get("tool_use_id") == FORK_ID
            and msgs[-1].get("role") == "user"
        )
        substituted_equal = last is not None and _tool_result_text(last) == SUBSTITUTED

    live_ok = False
    if second is not None:
        last = _last_tool_result(second)
        live_ok = (
            last is not None
            and last.get("tool_use_id") == LIVE_ID
            and _tool_result_text(last) == EXPECTED_LIVE
        )

    checks = {
        "no_error": rec.error is None,
        "manifest_captured": rec.manifest is not None and rec.restore_diff is not None,
        "only_messages_endpoint": paths_ok,
        "exactly_two_requests": two,
        "first_request_is_fork_prefix": fork_ids_ok,
        "first_request_substituted_text_equal": substituted_equal,
        "second_request_carries_live_result": live_ok,
        "final_completion_expected": rec.completion == EXPECTED_COMPLETION,
    }
    restore_exact = rec.restore_diff == []
    if sample_id in TAR_SAMPLES:
        checks["restore_exact"] = restore_exact
    return {
        "checks": checks,
        "passed": all(checks.values()),
        # Reported, not required: files_only is expected to lose metadata.
        "restore_exact": restore_exact,
        "restore_diff_count": None if rec.restore_diff is None else len(rec.restore_diff),
        "request_count": len(rec.requests),
        "first_body_sha256": hashlib.sha256(rec.requests[0][1]).hexdigest() if rec.requests else None,
    }


def evaluate(records: dict[str, SampleRecord]) -> dict:
    per_sample = {sid: check_sample(sid, rec) for sid, rec in records.items()}
    ran = set(records)
    expected = set(EXPECTED_SAMPLES)
    all_ran = ran == expected

    def every(check: str, ids=EXPECTED_SAMPLES) -> bool:
        return all_ran and all(per_sample[s]["checks"].get(check) is True for s in ids)

    hashes = [per_sample[s]["first_body_sha256"] for s in EXPECTED_SAMPLES if s in per_sample]
    verdict = {
        "expected_samples_ran": all_ran,
        "missing_samples": sorted(expected - ran),
        "unexpected_samples": sorted(ran - expected),
        "Q1_prefix_accepted_as_input": every("first_request_is_fork_prefix"),
        "Q2_tar_setup_restore_exact": every("restore_exact", TAR_SAMPLES),
        "Q3_substituted_text_equal_after_json_decode": every("first_request_substituted_text_equal"),
        "loop_resumed_with_live_result": every("second_request_carries_live_result")
        and every("final_completion_expected"),
        # Observation only: raw JSON bytes of the first request are identical
        # across samples. Not required; serialisation may legitimately vary.
        "observed_first_request_raw_bytes_identical": all_ran
        and None not in hashes
        and len(set(hashes)) == 1,
        "observed_files_only_restore_exact": all_ran
        and per_sample[FILES_ONLY_SAMPLE]["restore_exact"],
    }
    verdict["passed"] = all_ran and all(p["passed"] for p in per_sample.values())
    return {"verdict": verdict, "samples": per_sample}
