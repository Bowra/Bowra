"""Versioned storage for exact continuation bytes under the original card authority."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.context_segments import CONTEXT_SEGMENT_BYTES, MAX_CONTEXT_BYTES, MAX_CONTEXT_PARTS, SegmentReference

PREFIX = b"bowhead-continuation-segments.v1\n"


class PayloadReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["ordinary", "helper"]
    turn_id: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0, le=MAX_CONTEXT_BYTES)
    segments: tuple[SegmentReference, ...] = Field(min_length=1, max_length=MAX_CONTEXT_PARTS)


def seal_payload(
    content: bytes, *, kind: Literal["ordinary", "helper"], turn_id: str, scope: ReferenceScope, identity: str, purpose: str, cipher: ReferenceCipher
) -> tuple[bytes, tuple[str, ...]]:
    """Seal each bounded segment; the original envelope authenticates its manifest."""
    if not 0 < len(content) <= MAX_CONTEXT_BYTES:
        raise ValueError("continuation_payload_storage_limit")
    references, ciphertexts = [], []
    for ordinal, offset in enumerate(range(0, len(content), CONTEXT_SEGMENT_BYTES)):
        part = content[offset : offset + CONTEXT_SEGMENT_BYTES]
        digest = hashlib.sha256(part).hexdigest()
        references.append(SegmentReference(digest=digest, byte_count=len(part)))
        ciphertexts.append(cipher.seal(scope=scope, purpose=purpose + ".segment.v1", identity=f"{identity}:{ordinal}:{digest}", content=part))
    reference = PayloadReference(
        kind=kind, turn_id=turn_id, digest=hashlib.sha256(content).hexdigest(), byte_count=len(content), segments=tuple(references)
    )
    return PREFIX + reference.model_dump_json().encode(), tuple(ciphertexts)


async def write_payload_in_tx(db, *, manifest: bytes, ciphertexts: tuple[str, ...], scope: ReferenceScope, identity: str) -> None:
    """Join the original capture transaction; deferred parent FKs prohibit orphan commits."""
    if not db.in_transaction:
        raise ValueError("continuation_payload_transaction_required")
    reference = PayloadReference.model_validate_json(manifest[len(PREFIX) :])
    if not manifest.startswith(PREFIX) or len(ciphertexts) != len(reference.segments):
        raise ValueError("continuation_payload_manifest_invalid")
    await db.executemany(
        "INSERT INTO continuation_payload_segments "
        "(kind,owner_id,ordinal,session_id,turn_id,clarification_id,checkpoint_id,digest,byte_count,ciphertext) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                reference.kind,
                identity,
                index,
                scope.session_id,
                reference.turn_id,
                identity if reference.kind == "ordinary" else None,
                identity if reference.kind == "helper" else None,
                segment.digest,
                segment.byte_count,
                ciphertext,
            )
            for index, (segment, ciphertext) in enumerate(zip(reference.segments, ciphertexts, strict=True))
        ],
    )


def open_payload(
    content: bytes,
    *,
    db_path: str | Path | None,
    kind: Literal["ordinary", "helper"],
    turn_id: str,
    scope: ReferenceScope,
    identity: str,
    purpose: str,
    cipher: ReferenceCipher,
) -> bytes:
    """Legacy bytes stay unchanged; segment reads confer no continuation or source authority."""
    if not content.startswith(PREFIX):
        return content
    if db_path is None:
        raise ValueError("continuation_payload_database_required")
    reference = PayloadReference.model_validate_json(content[len(PREFIX) :])
    if reference.kind != kind or reference.turn_id != turn_id or sum(part.byte_count for part in reference.segments) != reference.byte_count:
        raise ValueError("continuation_payload_scope_invalid")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute(
            "SELECT ordinal,digest,byte_count,ciphertext FROM continuation_payload_segments "
            "WHERE kind=? AND owner_id=? AND session_id=? AND turn_id=? ORDER BY ordinal",
            (kind, identity, scope.session_id, turn_id),
        ).fetchall()
    if len(rows) != len(reference.segments):
        raise ValueError("continuation_payload_unavailable")
    parts = []
    for index, (segment, row) in enumerate(zip(reference.segments, rows, strict=True)):
        if row[:3] != (index, segment.digest, segment.byte_count):
            raise ValueError("continuation_payload_segment_changed")
        part = cipher.open(scope=scope, purpose=purpose + ".segment.v1", identity=f"{identity}:{index}:{segment.digest}", ciphertext=row[3])
        if len(part) != segment.byte_count or hashlib.sha256(part).hexdigest() != segment.digest:
            raise ValueError("continuation_payload_segment_changed")
        parts.append(part)
    payload = b"".join(parts)
    if hashlib.sha256(payload).hexdigest() != reference.digest:
        raise ValueError("continuation_payload_digest_changed")
    return payload
