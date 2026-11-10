"""Provider-native assistant reasoning kept for thinking-mode tool continuation.

Anthropic Messages and DeepSeek's Anthropic-compatible API require an assistant's
``thinking``/``redacted_thinking`` blocks to be sent back unchanged when a tool call
continues with thinking enabled. The envelope records which Team connection and model
produced the blocks so they are only ever returned to that same target. See
docs/design/thinking-tool-continuation-tech-design.md.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_router.types import Message

ANTHROPIC_MESSAGES = "anthropic_messages"
REASONING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
# Stored message records written before provider_reasoning existed have exactly these keys.
MESSAGE_RECORD_KEYS = frozenset({"role", "content", "name", "tool_call_id", "tool_calls", "images"})
REASONING_RECORD_KEY = "provider_reasoning"


def _valid_block(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") not in REASONING_BLOCK_TYPES:
        return False
    if block["type"] == "thinking":
        return isinstance(block.get("thinking"), str) and isinstance(block.get("signature", ""), str)
    return isinstance(block.get("data"), str) and bool(block["data"])


def anthropic_reasoning(blocks: list[Any], *, provider: str, model: str) -> dict[str, Any] | None:
    """Envelope the exact reasoning blocks of one Anthropic Messages response, if any."""
    kept = [copy.deepcopy(block) for block in blocks if _valid_block(block)]
    if not kept or not provider or not model:
        return None
    return {"version": 1, "protocol": ANTHROPIC_MESSAGES, "provider": provider, "model": model, "blocks": kept}


def valid_reasoning(envelope: Any) -> bool:
    return (
        isinstance(envelope, dict)
        and set(envelope) == {"version", "protocol", "provider", "model", "blocks"}
        and envelope["version"] == 1
        and envelope["protocol"] == ANTHROPIC_MESSAGES
        and isinstance(envelope["provider"], str)
        and isinstance(envelope["model"], str)
        and isinstance(envelope["blocks"], list)
        and bool(envelope["blocks"])
        and all(_valid_block(block) for block in envelope["blocks"])
    )


def matching_blocks(envelope: Any, *, provider: str, model: str) -> list[dict[str, Any]] | None:
    """Return copies of the blocks only for the same protocol, connection and model."""
    if not valid_reasoning(envelope) or envelope["provider"] != provider or envelope["model"] != model:
        return None
    return copy.deepcopy(envelope["blocks"])


def message_record(message: Message, *, keep_reasoning: bool = True) -> dict[str, Any]:
    """Serialize a Message; an absent envelope keeps the pre-existing record shape and digest."""
    record = asdict(message)
    if record.get(REASONING_RECORD_KEY) is None or not keep_reasoning:
        record.pop(REASONING_RECORD_KEY, None)
    return record


def record_reasoning(record: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the optional envelope of a stored record; raise ValueError when malformed."""
    envelope = record.get(REASONING_RECORD_KEY)
    if envelope is None:
        # Absent and explicit null (a plain dataclasses.asdict record) both mean no reasoning.
        return None
    if not valid_reasoning(envelope):
        raise ValueError("Invalid stored provider reasoning")
    return copy.deepcopy(envelope)


def valid_record_keys(record: dict[str, Any], base: frozenset[str] = MESSAGE_RECORD_KEYS) -> bool:
    keys = set(record)
    return keys == base or keys == base | {REASONING_RECORD_KEY}
