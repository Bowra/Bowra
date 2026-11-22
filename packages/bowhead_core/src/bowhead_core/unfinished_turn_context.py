"""Keep what an unfinished Turn did, for the next Turn's history.

A Turn that fails, is aborted or is stopped by the user ends with native calls that have
no result. Providers reject that transcript and the conversation head refuses it. This
module pairs each such call with a result, so the Lead's transcript can be published, and
records what a helper that is still running has run into.

Design: docs/sprints/failed-turn-history-2026-11-20/tech-design.md.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from bowhead_router.types import Message, ToolCall

from bowhead_core.context_assembly import TOOL_FAILURES_MAX_BYTES, render_spawn_result

logger = logging.getLogger(__name__)

# The call may have taken effect, so the text never says it did not run.
PENDING_RESULT = "The Turn ended before this call's result was recorded. The call may have run; check the current state before repeating it."


# A failed run's error output ends with the exception; what precedes it is the call stack.
ERROR_OUTPUT_TAIL_BYTES = 1024


def _stated_error(result: dict[str, Any]) -> str:
    """What one result says went wrong: its message, else its error, then the end of the error output it kept."""
    stated = next((value.strip() for value in (result.get("error_message"), result.get("error")) if isinstance(value, str) and value.strip()), "")
    run = result.get("result_json")
    output = next(
        (value for value in (result.get("stderr"), run.get("stderr") if isinstance(run, dict) else None) if isinstance(value, str) and value.strip()),
        "",
    )
    tail = output.encode()[-ERROR_OUTPUT_TAIL_BYTES:].decode(errors="ignore").strip()
    return f"{stated}: {tail}" if stated and tail else stated or tail


def returned_error_messages(result: Any) -> list[str]:
    """What a Tool said went wrong in its own result: each failed item's text, else the result's own."""
    if not isinstance(result, dict):
        return []
    items = result.get("results")
    texts = [_stated_error(item) for item in (items if isinstance(items, list) else []) if isinstance(item, dict) and item.get("ok") is False]
    texts = [text for text in texts if text] or [text for text in [_stated_error(result)] if text]
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


def unfinished_reply_from_events(events: Iterable[tuple[str, Mapping[str, Any]]]) -> str:
    """What a Turn had shown of the Lead reply its ending cut off, from its events in order.

    ``events`` holds ``narrative.delta``, ``llm.call_timing`` and ``llm.stream_restarted`` as
    ``(event_type, payload)``. A reply that made Tool calls joined the transcript, so its text
    is not returned. A reply without them stands until another reply follows it.
    """
    streamed = reply = ""
    for kind, payload in events:
        if kind == "narrative.delta":
            streamed += str(payload.get("delta") or "")
        elif payload.get("profile") != "lead":
            continue
        elif kind == "llm.stream_restarted":
            withdrawn = payload.get("discarded_narrative_chars")
            streamed = streamed[: max(0, len(streamed) - withdrawn)] if isinstance(withdrawn, int) and withdrawn > 0 else streamed
        elif kind == "llm.call_timing":
            reply = "" if payload.get("ok") and payload.get("tool_call_count") else streamed
            streamed = ""
    return (streamed or reply).strip()


async def publish_stopped_turn(bus: Any, *, store: Any, turn_id: str, worker: Any = None, ctx: Any = None) -> bool:
    """Publish the Lead's transcript of a Turn the user stopped, once, as its stopped owner.

    The Stop request has already committed the cancelled status. This runs where the worker
    first learns of it, before the cancellation leaves the Lead run: a task group further out
    can wrap that cancellation in an exception group, and the Turn runner's cancelled finalizer
    then never runs (a reproduced task). The finalizer calls this too; the second call is a
    no-op. A failure leaves the Turn stopped and rebuilt from events.

    ``worker`` is the Turn's original owner; the Lead run passes its ``ctx`` instead.
    """
    from bowhead_core.context_ownership import StoppedTurnOwner, context_owner
    from bowhead_core.document_chat_continuation import DocumentTurnOwner

    state = getattr(bus, "__dict__", None)
    if state is None or state.get("_stopped_turn_publication_attempted"):
        return False
    build = state.get("_unfinished_context_publication")
    publish = getattr(store, "publish_stopped_turn_context", None)
    if build is None or not callable(publish):
        return False
    try:
        if worker is None:
            worker = context_owner(ctx)
        if not isinstance(worker, DocumentTurnOwner):
            return False
        bus._stopped_turn_publication_attempted = True
        publication = await build("cancelled", owner=StoppedTurnOwner(**worker.model_dump()))
        return publication is not None and bool(await publish(turn_id=turn_id, context_publication=publication))
    except Exception as failure:  # noqa: BLE001 - a stopped Turn stays stopped without it
        logger.warning(
            "context_publication.unfinished_turn_unavailable turn=%s status=cancelled condition=%s",
            turn_id,
            str(failure)[:120] or type(failure).__name__,
        )
        return False
