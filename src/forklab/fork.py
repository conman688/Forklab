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


def build_fork_prefix(
    recording: Sequence[ChatMessage],
    fork_call_id: str,
    substituted_result: str,
) -> list[ChatMessage]:
    """Return the fork prefix ending in the substituted tool result.

    Refuses when:
    - the fork call does not appear exactly once, in an assistant turn;
    - that turn makes more than one tool call (sibling results are not modelled);
    - the recording has no tool result for the fork call (not a post-tool boundary);
    - any prefix message has non-text content (thinking blocks included);
    - any prefix message has an unsupported role, a tool message in the
      prefix answers a call the prefix never made, or an earlier call does
      not have exactly one result.
    """
    if not isinstance(substituted_result, str):
        raise ForkRefused("substituted result must be a string")

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

    recorded = [
        m for m in recording[t + 1:]
        if isinstance(m, ChatMessageTool) and m.tool_call_id == fork_call_id
    ]
    if len(recorded) != 1:
        raise ForkRefused(
            f"recording has {len(recorded)} results for {fork_call_id!r}; "
            "a fork needs exactly one recorded post-tool boundary"
        )

    prefix = list(recording[: t + 1])
    made_calls: set[str] = set()
    answered: dict[str, int] = {}
    for i, m in enumerate(prefix):
        if not isinstance(m, (ChatMessageSystem, ChatMessageUser, ChatMessageAssistant, ChatMessageTool)):
            raise ForkRefused(f"message {i} has unsupported type {type(m).__name__}")
        _check_content(i, m)
        if isinstance(m, ChatMessageAssistant):
            made_calls.update(c.id for c in m.tool_calls or [])
        if isinstance(m, ChatMessageTool):
            if m.tool_call_id not in made_calls:
                raise ForkRefused(f"message {i} answers unknown tool call {m.tool_call_id!r}")
            answered[m.tool_call_id] = answered.get(m.tool_call_id, 0) + 1
    earlier = made_calls - {fork_call_id}
    unanswered = sorted(c for c in earlier if answered.get(c) != 1)
    if unanswered:
        raise ForkRefused(f"earlier tool calls without exactly one result: {unanswered}")

    copied = [m.model_copy(deep=True) for m in prefix]
    copied.append(
        ChatMessageTool(content=substituted_result, tool_call_id=call.id, function=call.function)
    )
    return copied
