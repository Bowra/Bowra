"""Keep what an unfinished Turn did, for the next Turn's history.

A Turn that fails, is aborted or is stopped by the user ends with native calls that have
no result. Providers reject that transcript and the conversation head refuses it. This
module pairs each such call with a result, so the Lead's transcript can be published, and
records what a helper that is still running has run into.

Design: docs/sprints/failed-turn-history-2026-11-20/tech-design.md.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from bowhead_router.types import Message, ToolCall

from bowhead_core.context_assembly import TOOL_FAILURES_MAX_BYTES, render_spawn_result

# The call may have taken effect, so the text never says it did not run.
PENDING_RESULT = "The Turn ended before this call's result was recorded. The call may have run; check the current state before repeating it."


def returned_error_messages(result: Any) -> list[str]:
    """The error messages a Tool put in its own result: each failed item's, else the result's own."""
    if not isinstance(result, dict):
        return []
    texts = [
        str(item["error_message"])
        for item in result.get("results") or []
        if isinstance(item, dict) and item.get("ok") is False and item.get("error_message")
    ]
    if not texts and result.get("error_message"):
        texts.append(str(result["error_message"]))
    return list(dict.fromkeys(texts))


@dataclass
class OpenDelegation:
    """A ``subagent_spawn`` call whose helper has not returned."""

    profile: str
    subagent_id: str
    tool_count: int = 0
    # (tool, text): the text the helper's model received for a failed call.
    failures: list[tuple[str, str]] = field(default_factory=list)


class OpenDelegations:
    """The helpers running in one Turn, by the call that dispatched them.

    It lives on the Turn's event bus, which the Lead and its helpers share. The text it
    holds stays in memory and in the encrypted conversation head; it never enters events.
    """

    def __init__(self) -> None:
        self._by_call: dict[str, OpenDelegation] = {}

    def open(self, call_id: str, *, profile: str, subagent_id: str) -> None:
        if call_id and subagent_id:
            self._by_call[call_id] = OpenDelegation(profile=profile, subagent_id=subagent_id)

    def close(self, call_id: str) -> None:
        """The helper returned: its real result now speaks for it."""
        self._by_call.pop(call_id, None)

    def observe(self, subagent_id: str, tool_name: str, failures: Sequence[str] | None) -> None:
        """Count one Tool call of a running helper; ``failures`` holds its error text when it failed."""
        for record in self._by_call.values():
            if record.subagent_id != subagent_id:
                continue
            record.tool_count += 1
            for text in failures or ():
                clipped = text.encode()[:TOOL_FAILURES_MAX_BYTES].decode(errors="ignore")
                record.failures.append((tool_name, clipped))
            return

    def result(self, call_id: str, status: str) -> str | None:
        """The delegation result history shows for a helper that never returned."""
        record = self._by_call.get(call_id)
        if record is None:
            return None
        return render_spawn_result(
            SimpleNamespace(status=status, child_tool_count=record.tool_count, synthesized=True, tool_failures=tuple(record.failures))
        )


def open_delegations(bus: Any) -> OpenDelegations:
    """The Turn's record of running helpers. A bus that cannot hold it gets a detached one."""
    registry = getattr(bus, "_open_delegations", None)
    if not isinstance(registry, OpenDelegations):
        registry = OpenDelegations()
        with contextlib.suppress(AttributeError, TypeError):
            bus._open_delegations = registry
    return registry


def repair_pending_tool_calls(messages: Sequence[Message], result_for: Callable[[ToolCall], str | None]) -> list[Message]:
    """Pair every native call that has no result with one, in call order.

    ``result_for`` supplies what is known about a call: a finished sibling's real result or
    a running helper's record. A call it knows nothing about gets ``PENDING_RESULT``.
    """
    answered = {message.tool_call_id for message in messages if message.role == "tool" and message.tool_call_id}
    repaired = list(messages)
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if call.id in answered:
                continue
            repaired.append(Message(role="tool", name=call.name, tool_call_id=call.id, content=result_for(call) or PENDING_RESULT))
            answered.add(call.id)
    return repaired
