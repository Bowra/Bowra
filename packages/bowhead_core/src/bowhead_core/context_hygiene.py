"""Model-facing history hygiene for the Lead's published conversation view.

Cross-Turn history carries the conversation, its decisions, outcomes and
identifiers. Protocol material used inside one Turn and the bodies of old,
completed Tool results do not have to be read again by every later Turn.

This module only projects the message list that a successful Turn publishes.
It never changes the Runner's native transcript, stored events, the dependency
manifest, source references or what users see.

Design: docs/sprints/lead-history-growth-2026-11-14/tech-design.md
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Literal

from bowhead_router.runtime_notes import (
    OUTPUT_RECOVERY_STATE_NAME,
    REJECTED_TOOL_SYNTAX_HISTORY_NAME,
    RUNTIME_CORRECTION_NOTE_NAME,
    VISUAL_VIEW_NOTE_NAME,
)
from bowhead_router.token_estimator import estimate_tokens
from bowhead_router.types import Message

from bowhead_core.context_compaction import balanced_boundaries

logger = logging.getLogger(__name__)

HistoryPolicy = Literal["turn_local", "conversation", "dependency"]

# H1: every server-authored message kind declares how it enters cross-Turn
# history. ``turn_local`` is used inside its Turn and never published,
# ``conversation`` is part of the published view, ``dependency`` belongs to the
# source-dependency manifest and keeps that contract. A guard test fails when
# code builds a named non-Tool message whose kind is missing here.
MESSAGE_KIND_POLICY: dict[str, HistoryPolicy] = {
    RUNTIME_CORRECTION_NOTE_NAME: "turn_local",
    REJECTED_TOOL_SYNTAX_HISTORY_NAME: "turn_local",
    OUTPUT_RECOVERY_STATE_NAME: "turn_local",
    # A per-request appendix rebuilt from saved events for every model call.
    "bowhead_material_supplement_v1": "turn_local",
    # A connection probe request; it never belongs to a conversation.
    "bowhead_connection_probe": "turn_local",
    # Rendered views of saved files for the next request only (visual self-check).
    VISUAL_VIEW_NOTE_NAME: "turn_local",
    "context_summary": "conversation",
    # A user-selected Skill stays with the request it belongs to.
    "skill_content": "conversation",
    "context_attachment_dependencies_v1": "dependency",
    "context_attachment_dependencies_v2": "dependency",
    "context_material_dependencies_v1": "dependency",
    "context_source_dependencies_v1": "dependency",
    "context_navigation_dependencies_v1": "dependency",
}

# Tools whose whole call/result exchange is closeout protocol for the running
# Turn. The final answer already reflects the review they carried.
TURN_LOCAL_TOOLS = frozenset({"review_delivery"})

# Results that stay verbatim at any age: instructions a later Turn still acts
# on, the user's own replies, and knowledge reads whose provenance contract is
# checked on the result body itself.
NEVER_CLEARED_TOOLS = frozenset(
    {
        "skill_read",
        "clarification_ask",
        "kb_search",
        "kb_list_tables",
        "kb_query_records",
        "kb_get_record",
        "kb_list_sources",
    }
)

# Pure reads of saved material: reading again is the recovery path, and the
# old body describes a version that may since have changed.
REREAD_TOOLS = frozenset({"read_report", "find_results"})

# Result fields that are rebuilt for every request or only instruct the Turn
# that received them. The retained material supplement re-projects references.
REPROJECTED_RESULT_KEYS = frozenset({"material_refs", "material_recovery_guidance", "directive", "instruction", "evidence_guidance"})

# Run statistics of the finished call. They are neither identity nor outcome,
# and usage is recorded elsewhere.
RUN_STATISTIC_RESULT_KEYS = frozenset(
    {"tokens_input", "tokens_output", "tokens_cached", "tool_count", "duration_ms", "tool_execution", "payload_returned"}
)

# Read by source validation at the top level of a result; see
# ``knowledge_source_runtime.validate_source_messages``.
PROVENANCE_RESULT_KEYS = ("knowledge_provenance", "knowledge_navigation_provenance")

CLEARED_RESULT_KIND = "bowhead_cleared_result_v1"
_CLEARED_PREFIX = '{"cleared_result":"' + CLEARED_RESULT_KIND + '"'

KEEP_RECENT_TURNS = 2
# Calibrated on the nine-Turn 116 report session: 20,000 never ran a batch
# there, 10,000 ran one in nine Turns and 5,000 ran two (tech design §6).
DEFAULT_MIN_SAVING_TOKENS = 10_000
# A stub costs a few hundred characters; smaller results stay as they are.
MIN_RESULT_CHARS = 1_200
MAX_KEPT_STRING_CHARS = 300
MAX_KEPT_ITEMS = 30
MAX_KEPT_DEPTH = 4
# A stub must be clearly smaller than its original to be worth a rewrite.
MAX_STUB_SHARE = 0.6

_OPERATION_NOTE = (
    "This earlier Tool result was shortened in conversation history. Identifiers and short fields are kept; long text and "
    "repeated reference lists were removed. The operation already ran: do not repeat it to recover its output. When the "
    "omitted details are needed, read the saved result with a reader that is available now."
)
_REREAD_NOTE = (
    "This earlier read was shortened in conversation history. Its content may have changed since. "
    "Read the item again with the same Tool when its content is needed now."
)


def history_hygiene_enabled() -> bool:
    """On unless the deployment sets ``FEATURE_CONTEXT_HISTORY_HYGIENE=0`` (default on since 2026-11-16, the maintainer's decision)."""
    return os.environ.get("FEATURE_CONTEXT_HISTORY_HYGIENE", "1").strip().lower() not in {"0", "false", "no", "off"}


def min_saving_tokens() -> int:
    """Rollout calibration only; becomes the constant at Release with the feature switch."""
    try:
        value = int(os.environ.get("BOWHEAD_CTX_HYGIENE_MIN_SAVING_TOKENS", ""))
    except ValueError:
        return DEFAULT_MIN_SAVING_TOKENS
    return value if value > 0 else DEFAULT_MIN_SAVING_TOKENS


def is_cleared_result(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(_CLEARED_PREFIX)


def _size(message: Message) -> int:
    calls = message.tool_calls or ()
    return len(message.content or "") + sum(
        len(call.arguments) if isinstance(call.arguments, str) else len(json.dumps(call.arguments, ensure_ascii=False)) for call in calls
    )


def _tool_names(messages: list[Message]) -> dict[str, str]:
    return {call.id: call.name for message in messages for call in message.tool_calls or ()}


@dataclass(frozen=True, slots=True)
class HistoryComposition:
    """Sizes in characters by kind; never content."""

    messages: int
    chars: int
    user: int
    assistant: int
    call_arguments: int
    tool_results: int
    notes: int
    cleared_results: int
    top_tools: tuple[tuple[str, int], ...]

    def top_tools_text(self) -> str:
        return ",".join(f"{name}:{size}" for name, size in self.top_tools) or "-"


def history_composition(messages: list[Message]) -> HistoryComposition:
    names = _tool_names(messages)
    user = assistant = arguments = results = notes = cleared = 0
    by_tool: Counter[str] = Counter()
    for message in messages:
        content = len(message.content or "")
        if message.role == "tool":
            results += content
            by_tool[names.get(message.tool_call_id or "") or message.name or "?"] += content
            cleared += is_cleared_result(message.content)
        elif message.name:
            notes += content
        elif message.role == "user":
            user += content
        else:
            assistant += content
            arguments += _size(message) - content
    return HistoryComposition(
        messages=len(messages),
        chars=user + assistant + arguments + results + notes,
        user=user,
        assistant=assistant,
        call_arguments=arguments,
        tool_results=results,
        notes=notes,
        cleared_results=cleared,
        top_tools=tuple(by_tool.most_common(4)),
    )


def omit_turn_local(messages: list[Message]) -> list[Message]:
    """H2: leave out protocol material that only served its own Turn.

    Removes turn-local message kinds, an assistant answer that a runtime
    correction superseded, and whole exchanges with turn-local Tools. The
    result keeps every remaining call paired with its results; when that
    cannot be shown, the input is returned unchanged.
    """
    drop: set[int] = set()
    for index, message in enumerate(messages):
        if message.role == "tool" or not message.name or MESSAGE_KIND_POLICY.get(message.name) != "turn_local":
            continue
        drop.add(index)
        if message.name == RUNTIME_CORRECTION_NOTE_NAME and index > 0:
            previous = messages[index - 1]
            # The Runner appends the rejected final right before its correction.
            if previous.role == "assistant" and not previous.tool_calls:
                drop.add(index - 1)
    local_calls: set[str] = set()
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls and all(call.name in TURN_LOCAL_TOOLS for call in message.tool_calls):
            drop.add(index)
            local_calls.update(call.id for call in message.tool_calls)
    for index, message in enumerate(messages):
        if message.role == "tool" and message.tool_call_id in local_calls:
            drop.add(index)
    if not drop:
        return messages
    kept = [message for index, message in enumerate(messages) if index not in drop]
    try:
        balanced = balanced_boundaries(kept)[-1] == len(kept)
    except ValueError:
        balanced = False
    if not balanced:
        logger.warning("context_hygiene.omission_unbalanced messages=%d dropped=%d", len(messages), len(drop))
        return messages
    return kept


def _reduce(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_KEPT_STRING_CHARS else f"[{len(value)} chars removed]"
    if isinstance(value, list):
        if depth >= MAX_KEPT_DEPTH:
            return f"[list with {len(value)} items removed]"
        kept = [_reduce(item, depth + 1) for item in value[:MAX_KEPT_ITEMS]]
        if len(value) > MAX_KEPT_ITEMS:
            kept.append(f"[+{len(value) - MAX_KEPT_ITEMS} more]")
        return kept
    if isinstance(value, dict):
        if depth >= MAX_KEPT_DEPTH:
            return f"[object with {len(value)} keys removed]"
        return {key: _reduce(item, depth + 1) for key, item in value.items() if key not in REPROJECTED_RESULT_KEYS}
    return f"[{type(value).__name__} removed]"


def cleared_result(tool: str, content: str) -> str | None:
    """H6: an identifier-keeping stub for one completed Tool result.

    Deterministic, so a stub written once stays byte-identical in every later
    publication. Returns None when the content is already a stub.
    """
    if is_cleared_result(content):
        return None
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    reread = tool in REREAD_TOOLS
    stub: dict[str, Any] = {
        "cleared_result": CLEARED_RESULT_KIND,
        "tool": tool,
        "original_chars": len(content),
        "note": _REREAD_NOTE if reread else _OPERATION_NOTE,
    }
    if isinstance(data, dict):
        kept: dict[str, Any] = {}
        for key, value in data.items():
            if key in REPROJECTED_RESULT_KEYS or key in RUN_STATISTIC_RESULT_KEYS or key in PROVENANCE_RESULT_KEYS:
                continue
            if reread and isinstance(value, (list, dict)) and key != "coverage":
                continue
            kept[key] = _reduce(value, 1)
        # ``kept`` carries what a later Turn needs to address this result's files: the produced
        # count, the delivered files as identifier and name (``produced.delivered_files``) and a
        # revision's targets. Any other file of the conversation is found with ``find_results``.
        stub["kept"] = kept
        # Source validation reads these at the top level of a result, and an
        # older head may record a source nowhere else. They stay verbatim.
        for key in PROVENANCE_RESULT_KEYS:
            if key in data:
                stub[key] = data[key]
    else:
        stub["kept"] = {"preview": content[:MAX_KEPT_STRING_CHARS]}
    return json.dumps(stub, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ClearedBodies:
    messages: list[Message]
    results: int = 0
    chars: int = 0
    tokens: int = 0


def clear_old_tool_bodies(
    messages: list[Message],
    *,
    keep_turns: int = KEEP_RECENT_TURNS,
    min_tokens: int = DEFAULT_MIN_SAVING_TOKENS,
) -> ClearedBodies:
    """H5: replace old completed Tool bodies with stubs, in one batch.

    A batch runs only when it saves at least ``min_tokens``, so the rewritten
    prefix then stays stable for later Turns and the cache miss it causes is
    paid for. Calls, arguments, user and assistant text are never changed.
    """
    starts = [index for index, message in enumerate(messages) if message.role == "user" and not message.name]
    if keep_turns < 1 or len(starts) <= keep_turns:
        return ClearedBodies(messages)
    protected_from = starts[-keep_turns]
    names = _tool_names(messages[:protected_from])
    stubs: dict[int, str] = {}
    chars = tokens = 0
    for index in range(protected_from):
        message = messages[index]
        if message.role != "tool" or not isinstance(message.content, str) or message.images or len(message.content) < MIN_RESULT_CHARS:
            continue
        tool = names.get(message.tool_call_id or "") or message.name or ""
        if not tool or tool in NEVER_CLEARED_TOOLS:
            continue
        stub = cleared_result(tool, message.content)
        if stub is None or len(stub) > len(message.content) * MAX_STUB_SHARE:
            continue
        stubs[index] = stub
        chars += len(message.content) - len(stub)
        tokens += estimate_tokens(message.content) - estimate_tokens(stub)
    if not stubs or tokens < min_tokens:
        return ClearedBodies(messages)
    cleared = [replace(message, content=stubs[index]) if index in stubs else message for index, message in enumerate(messages)]
    return ClearedBodies(cleared, results=len(stubs), chars=chars, tokens=tokens)


@dataclass(frozen=True, slots=True)
class PublishedHistory:
    messages: list[Message]
    enabled: bool
    before: HistoryComposition
    after: HistoryComposition
    omitted_messages: int = 0
    cleared_results: int = 0
    cleared_tokens: int = 0


def published_history(messages: list[Message]) -> PublishedHistory:
    """The view a successful Lead Turn publishes; unchanged while the switch is off."""
    before = history_composition(messages)
    if not history_hygiene_enabled():
        return PublishedHistory(messages, False, before, before)
    kept = omit_turn_local(messages)
    cleared = clear_old_tool_bodies(kept, min_tokens=min_saving_tokens())
    return PublishedHistory(
        cleared.messages,
        True,
        before,
        history_composition(cleared.messages),
        omitted_messages=len(messages) - len(kept),
        cleared_results=cleared.results,
        cleared_tokens=cleared.tokens,
    )


def record_publication(published: PublishedHistory, *, session_id: str, turn_id: str) -> None:
    """H8: one line and a few totals per publication; sizes only, never content."""
    after = published.after
    logger.info(
        "context_publication.composition session=%s turn=%s hygiene=%s messages=%d chars=%d user=%d assistant=%d call_args=%d "
        "tool_results=%d notes=%d stubs=%d top_tools=%s before_chars=%d omitted_messages=%d cleared_results=%d cleared_tokens=%d",
        session_id,
        turn_id,
        "on" if published.enabled else "off",
        after.messages,
        after.chars,
        after.user,
        after.assistant,
        after.call_arguments,
        after.tool_results,
        after.notes,
        after.cleared_results,
        after.top_tools_text(),
        published.before.chars,
        published.omitted_messages,
        published.cleared_results,
        published.cleared_tokens,
    )
    try:  # Metrics never affect publication.
        from bowhead_core import metrics

        labels = {"hygiene": "on" if published.enabled else "off"}
        metrics.observe(
            "context_published_history_chars",
            after.chars,
            labels=labels,
            help="Characters in the Lead conversation view published at Turn completion.",
            buckets=(10_000, 25_000, 50_000, 100_000, 200_000, 400_000, 800_000),
        )
        if published.cleared_results:
            metrics.inc(
                "context_history_cleared_tokens_total",
                value=published.cleared_tokens,
                help="Estimated tokens removed by clearing batches.",
            )
    except Exception:  # noqa: BLE001
        logger.debug("context_hygiene.metrics_failed", exc_info=True)
