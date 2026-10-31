"""Bounded encrypted model-message projections using the existing reference cipher.

Segment identities are internal storage references. They grant no source access,
Turn ownership, Tool execution or continuation authority. Store callers must
validate those authorities before sealing and after opening a context view.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from bowhead_router.types import ImageRef, Message, ToolCall
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.context_compaction import balanced_boundaries

CONTEXT_SEGMENT_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 64 * 1024 * 1024
MAX_CONTEXT_PARTS = MAX_CONTEXT_BYTES // CONTEXT_SEGMENT_BYTES
CONTEXT_SEGMENT_PURPOSE = "model-context-segment.v1"


class ContextSegmentError(ValueError):
    """Finite storage-format rejection, with no decrypted content."""


class SegmentReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0, le=CONTEXT_SEGMENT_BYTES)


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0, le=MAX_CONTEXT_BYTES)
    message_count: int = Field(ge=0)
    segments: tuple[SegmentReference, ...] = Field(min_length=1, max_length=MAX_CONTEXT_PARTS)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("context_version_invalid")
        return value


@dataclass(frozen=True, slots=True)
class SealedContextSegment:
    reference: SegmentReference
    ciphertext: str


def _identity(owner: str, digest: str) -> str:
    if not isinstance(owner, str) or not owner or len(owner) > 512:
        raise ContextSegmentError("context_segment_owner_invalid")
    return owner + ":" + digest


def _reject_nonfinite(_: str) -> None:
    raise ValueError("nonfinite JSON")


def seal_context_messages(
    messages: list[Message], *, cipher: ReferenceCipher, scope: ReferenceScope, owner: str
) -> tuple[ContextSnapshot, tuple[SealedContextSegment, ...]]:
    """Split storage bytes only; never truncate or split model-visible messages."""
    try:
        balanced_boundaries(messages)
        for message in messages:
            _message(asdict(message))
        encoded = json.dumps(
            [asdict(message) for message in messages], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError) as exc:
        raise ContextSegmentError("context_messages_invalid") from exc
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise ContextSegmentError("context_projection_storage_limit")
    parts = []
    for offset in range(0, len(encoded), CONTEXT_SEGMENT_BYTES):
        content = encoded[offset : offset + CONTEXT_SEGMENT_BYTES]
        reference = SegmentReference(digest=hashlib.sha256(content).hexdigest(), byte_count=len(content))
        parts.append(
            SealedContextSegment(
                reference, cipher.seal(scope=scope, purpose=CONTEXT_SEGMENT_PURPOSE, identity=_identity(owner, reference.digest), content=content)
            )
        )
    snapshot = ContextSnapshot(
        digest=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        message_count=len(messages),
        segments=tuple(part.reference for part in parts),
    )
    return snapshot, tuple(parts)


def open_context_messages(
    snapshot: ContextSnapshot,
    ciphertext_by_digest: dict[str, str],
    *,
    cipher: ReferenceCipher,
    scope: ReferenceScope,
    owner: str,
) -> list[Message]:
    """Strictly reopen the exact scoped projection, without a plaintext fallback."""
    snapshot = ContextSnapshot.model_validate(snapshot.model_dump())
    if sum(part.byte_count for part in snapshot.segments) != snapshot.byte_count:
        raise ContextSegmentError("context_projection_size_mismatch")
    parts = []
    try:
        for reference in snapshot.segments:
            content = cipher.open(
                scope=scope,
                purpose=CONTEXT_SEGMENT_PURPOSE,
                identity=_identity(owner, reference.digest),
                ciphertext=ciphertext_by_digest[reference.digest],
            )
            if len(content) != reference.byte_count or hashlib.sha256(content).hexdigest() != reference.digest:
                raise ContextSegmentError("context_segment_digest_mismatch")
            parts.append(content)
        encoded = b"".join(parts)
        if hashlib.sha256(encoded).hexdigest() != snapshot.digest:
            raise ContextSegmentError("context_projection_digest_mismatch")
        rows = json.loads(encoded, parse_constant=_reject_nonfinite)
        if not isinstance(rows, list) or len(rows) != snapshot.message_count:
            raise ContextSegmentError("context_projection_message_count")
        messages = [_message(row) for row in rows]
        balanced_boundaries(messages)
        return messages
    except (KeyError, ValueError, TypeError, RecursionError) as exc:
        raise ContextSegmentError("context_projection_unavailable") from exc


def _message(row: object) -> Message:
    fields = {"role", "content", "name", "tool_call_id", "tool_calls", "images"}
    if not isinstance(row, dict) or set(row) != fields or not isinstance(row["content"], str):
        raise ContextSegmentError("context_message_shape_invalid")
    if any(row[key] is not None and not isinstance(row[key], str) for key in ("name", "tool_call_id")):
        raise ContextSegmentError("context_message_identity_invalid")
    images = row["images"]
    if not isinstance(images, list) or images and row["role"] != "user":
        raise ContextSegmentError("context_message_images_invalid")
    for image in images:
        if (
            not isinstance(image, dict)
            or set(image) != {"base64", "mime"}
            or not isinstance(image["base64"], str)
            or not isinstance(image["mime"], str)
        ):
            raise ContextSegmentError("context_image_shape_invalid")
    calls = row["tool_calls"]
    if calls is not None:
        if not isinstance(calls, list) or not calls:
            raise ContextSegmentError("context_tool_calls_invalid")
        for call in calls:
            if (
                not isinstance(call, dict)
                or set(call) != {"id", "name", "arguments"}
                or not isinstance(call["id"], str)
                or not isinstance(call["name"], str)
            ):
                raise ContextSegmentError("context_tool_call_shape_invalid")
    return Message(
        **{**row, "images": [ImageRef(**image) for image in images], "tool_calls": [ToolCall(**call) for call in calls] if calls else None}
    )
