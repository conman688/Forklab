"""Build the transcript for a fork at a post-tool boundary (roadmap §1.3).

The fork prefix is the recorded conversation up to and including the
assistant turn that made the fork call, followed by a tool message carrying
the substituted result. The recorded result for that call is dropped and
the call is not executed again.

Fail-closed thinking-block rule (Phase 0 finding): on Sonnet-5.5-class
models Inspect asks the API to drop, rather than reject, thinking blocks
whose prefix no longer matches. Until a live probe shows drops can be
detected, a prefix that carries reasoning cannot be claimed faithful, so
build_fork_prefix refuses it. More generally, only plain text content is
allowed in the prefix; any other content type (reasoning, provider data,
server tool use, media) is refused rather than replayed on trust.
"""

from __future__ import annotations

from collections.abc import Sequence

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentText,
)


class ForkRefused(Exception):
    """The recording cannot be forked faithfully at the requested point."""


def _check_content(index: int, message: ChatMessage) -> None:
    content = message.content
    if isinstance(content, str):
        return
    for part in content:
        if not isinstance(part, ContentText):
            raise ForkRefused(
                f"message {index} ({message.role}) contains {type(part).__name__}; "
                "only plain text is allowed in a fork prefix"
            )


def _validate_order(messages: Sequence[ChatMessage]) -> None:
    """Check `messages` as an ordered tool-call state machine.

    - System messages only before any other message.
    - Every tool call ID is unique across `messages`.
    - After an assistant turn with N tool calls, the next N messages are
      exactly their results, in call order, each naming the called function.
    - No user or assistant message while a call is pending, and no tool
      message unless it answers the next pending call.
    """
    seen: set[str] = set()
    pending: list[tuple[str, str]] = []  # (call id, function), in call order
    started = False
    for i, m in enumerate(messages):
        if isinstance(m, ChatMessageSystem):
            if started:
                raise ForkRefused(f"message {i}: system message after the conversation started")
            continue
        started = True
        if isinstance(m, ChatMessageTool):
            if not pending:
                raise ForkRefused(f"message {i}: tool result {m.tool_call_id!r} with no pending call")
            call_id, function = pending.pop(0)
            if m.tool_call_id != call_id:
                raise ForkRefused(
                    f"message {i}: result for {m.tool_call_id!r} where {call_id!r} was expected next"
                )
            if m.function != function:
                raise ForkRefused(
                    f"message {i}: result names function {m.function!r}, call was {function!r}"
                )
            continue
        if pending:
            raise ForkRefused(
                f"message {i} ({m.role}) while calls are unanswered: {[c for c, _ in pending]}"
            )
        if isinstance(m, ChatMessageAssistant):
            for call in m.tool_calls or []:
                if call.id in seen:
                    raise ForkRefused(f"message {i}: duplicate tool call id {call.id!r}")
                seen.add(call.id)
                pending.append((call.id, call.function))
        elif not isinstance(m, ChatMessageUser):
            raise ForkRefused(f"message {i} has unsupported type {type(m).__name__}")


def build_fork_prefix(
    recording: Sequence[ChatMessage],
    fork_call_id: str,
    substituted_result: str,
) -> list[ChatMessage]:
    """Return the fork prefix ending in the substituted tool result.

    The recording up to and including the recorded fork result must be a
    valid ordered transcript (see _validate_order). Additionally refuses when:
    - any tool call id appears more than once in the whole recording;
    - the fork call does not appear exactly once in the whole recording;
    - the fork turn makes more than one tool call (sibling results are not modelled);
    - the message right after the fork turn is not the fork call's result
      (not a recorded post-tool boundary);
    - any prefix message has non-text content (thinking blocks included).
    """
    if not isinstance(substituted_result, str):
        raise ForkRefused("substituted result must be a string")

    all_ids = [c.id for m in recording if isinstance(m, ChatMessageAssistant) for c in m.tool_calls or []]
    duplicates = sorted({c for c in all_ids if all_ids.count(c) > 1})
    if duplicates:
        raise ForkRefused(f"duplicate tool call ids in recording: {duplicates}")

    owners = [
        i for i, m in enumerate(recording)
        if isinstance(m, ChatMessageAssistant)
        and any(c.id == fork_call_id for c in m.tool_calls or [])
    ]
    if len(owners) != 1:
        raise ForkRefused(f"fork call {fork_call_id!r} found in {len(owners)} assistant turns, expected 1")
    t = owners[0]
    calls = recording[t].tool_calls or []
    if len(calls) != 1:
        raise ForkRefused(f"fork turn makes {len(calls)} tool calls; only single-call turns are supported")
    call = calls[0]

    boundary = recording[t + 1] if t + 1 < len(recording) else None
    if not (isinstance(boundary, ChatMessageTool) and boundary.tool_call_id == fork_call_id):
        raise ForkRefused(
            f"message after the fork turn is not the result of {fork_call_id!r}; "
            "a fork needs a recorded post-tool boundary"
        )
    _validate_order(recording[: t + 2])

    prefix = list(recording[: t + 1])
    for i, m in enumerate(prefix):
        _check_content(i, m)

    copied = [m.model_copy(deep=True) for m in prefix]
    copied.append(
        ChatMessageTool(content=substituted_result, tool_call_id=call.id, function=call.function)
    )
    return copied
