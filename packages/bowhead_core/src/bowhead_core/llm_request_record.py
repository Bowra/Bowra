"""Store and rebuild the diagnostic record of actual model sends.

The router hands over the exact canonical wire body (the bytes hashed into
`request_digest`). It is split into ordered chunks: a header (the body without its
large fields), `system`, `tools`, and one chunk per element of `messages` or
`input`. Chunks are keyed by Turn and SHA-256, so a Turn's later steps store only
their new messages and nothing is shared across Turns, users or Teams. Chunks over
`OMIT_CHUNK_BYTES` (mostly inline images) keep only their hash and size.

See docs/design/llm-request-record-tech-design.md. Operator-only diagnostics:
nothing here feeds model input, events or the product UI.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import aiosqlite
from bowhead_router.invocation import canonical_transport_bytes
from bowhead_router.request_record import RequestRecordEntry

from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope

RETENTION = timedelta(days=14)
OMIT_CHUNK_BYTES = 256 * 1024
CHUNK_PURPOSE = "llm-request-chunk"
CHANGE_PURPOSE = "llm-request-change"
HEADER_FIELD = "_header"
SINGLE_FIELDS = ("system", "tools")
LIST_FIELDS = ("messages", "input")


@dataclass(frozen=True, slots=True)
class BodyChunk:
    field: str
    data: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def split_body(canonical_body: bytes) -> list[BodyChunk]:
    """Split a canonical wire body into ordered chunks that `rebuild_body` reverses exactly."""
    header = json.loads(canonical_body)
    if not isinstance(header, dict):
        raise ValueError("request body must be a JSON object")
    chunks: list[BodyChunk] = []
    for name in SINGLE_FIELDS:
        if name in header:
            chunks.append(BodyChunk(name, canonical_transport_bytes(header.pop(name))))
    for name in LIST_FIELDS:
        value = header.get(name)
        # An empty list stays in the header so the rebuilt body keeps the key.
        if isinstance(value, list) and value:
            header.pop(name)
            chunks.extend(BodyChunk(name, canonical_transport_bytes(item)) for item in value)
    return [BodyChunk(HEADER_FIELD, canonical_transport_bytes(header)), *chunks]


def rebuild_body(chunks: Iterable[tuple[str, object]]) -> bytes:
    """Reassemble decoded chunk values, in stored order, into the canonical wire body."""
    body: dict[str, object] = {}
    for name, value in chunks:
        if name == HEADER_FIELD:
            if not isinstance(value, dict):
                raise ValueError("request header chunk must be an object")
            body.update(value)
        elif name in LIST_FIELDS:
            items = body.setdefault(name, [])
            if not isinstance(items, list):
                raise ValueError(f"request field {name} is not a list")
            items.append(value)
        elif name in SINGLE_FIELDS:
            body[name] = value
        else:
            raise ValueError(f"unknown request chunk field {name}")
    return canonical_transport_bytes(body)


async def purge_expired_request_records(db: aiosqlite.Connection, now: datetime, *, limit: int) -> int:
    """Delete up to `limit` expired records in the caller's transaction; returns how many.

    The record delete trigger removes their change records. A Turn's chunks are
    deleted once none of its records remain; chunks are only ever written in the
    same transaction as a record, so this leaves no orphaned chunk behind.
    """
    async with db.execute(
        "SELECT id, turn_id FROM llm_request_records WHERE expires_at <= ? ORDER BY expires_at LIMIT ?", (now.isoformat(), limit)
    ) as cursor:
        expired = await cursor.fetchall()
    if not expired:
        return 0
    await db.executemany("DELETE FROM llm_request_records WHERE id=?", [(record_id,) for record_id, _turn_id in expired])
    await db.executemany(
        "DELETE FROM llm_request_chunks WHERE turn_id=? AND NOT EXISTS (SELECT 1 FROM llm_request_records WHERE turn_id=?)",
        [(turn_id, turn_id) for turn_id in {turn_id for _record_id, turn_id in expired}],
    )
    return len(expired)


async def write_request_records(db: aiosqlite.Connection, cipher: ReferenceCipher, entries: Iterable[RequestRecordEntry]) -> int:
    """Insert records, their new chunks and change records in the caller's transaction.

    Entries whose Turn no longer exists (deleted before the flush) are skipped.
    Returns the number of records written.
    """
    sessions: dict[str, str | None] = {}
    written = 0
    for entry in entries:
        usage = entry.usage
        turn_id = usage.turn_id
        if turn_id is None or usage.team_id is None or usage.user_id is None or usage.workspace_id is None:
            continue
        if turn_id not in sessions:
            async with db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)) as cursor:
                row = await cursor.fetchone()
            sessions[turn_id] = row[0] if row is not None else None
        session_id = sessions[turn_id]
        if session_id is None:
            continue
        scope = ReferenceScope(team_id=usage.team_id, user_id=usage.user_id, workspace_id=usage.workspace_id, session_id=session_id, policy_epoch=0)
        created_at = entry.created_at.isoformat()
        chunk_refs: list[dict[str, str]] = []
        for chunk in split_body(entry.canonical_body):
            digest = chunk.digest
            chunk_refs.append({"f": chunk.field, "h": digest})
            omitted = len(chunk.data) > OMIT_CHUNK_BYTES
            ciphertext = None if omitted else cipher.seal(scope=scope, purpose=CHUNK_PURPOSE, identity=f"{turn_id}:{digest}", content=chunk.data)
            await db.execute(
                "INSERT OR IGNORE INTO llm_request_chunks(turn_id,chunk_hash,ciphertext,byte_size,omitted,created_at) VALUES(?,?,?,?,?,?)",
                (turn_id, digest, ciphertext, len(chunk.data), int(omitted), created_at),
            )
        record_id = "lrq_" + uuid4().hex
        await db.execute(
            "INSERT INTO llm_request_records(id,turn_id,session_id,team_id,user_id,workspace_id,reason,usage_event_id,subagent_id,"
            "provider,model,family,mode,request_digest,chunk_hashes_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                turn_id,
                session_id,
                usage.team_id,
                usage.user_id,
                usage.workspace_id,
                usage.reason,
                entry.usage_event_id,
                entry.subagent_id,
                entry.provider,
                entry.model,
                entry.family,
                entry.mode,
                entry.request_digest,
                json.dumps(chunk_refs, separators=(",", ":")),
                created_at,
                (entry.created_at + RETENTION).isoformat(),
            ),
        )
        for change in entry.changes:
            change_id = "lrc_" + uuid4().hex
            detail = canonical_transport_bytes(dict(change.detail))
            await db.execute(
                "INSERT INTO llm_request_changes(id,record_id,kind,detail_ciphertext,created_at) VALUES(?,?,?,?,?)",
                (change_id, record_id, change.kind, cipher.seal(scope=scope, purpose=CHANGE_PURPOSE, identity=change_id, content=detail), created_at),
            )
        written += 1
    return written


@dataclass(frozen=True, slots=True)
class RebuiltRequest:
    record: dict[str, object]
    #: Canonical body; an omitted chunk is replaced by an `_omitted_chunk` placeholder.
    body: bytes
    omitted_chunks: tuple[str, ...]
    #: True or False when every chunk was stored; None when a chunk was omitted.
    digest_matches: bool | None
    #: This run's change records up to and including this send, oldest first.
    changes: tuple[dict[str, object], ...]


_RECORD_COLUMNS = (
    "id",
    "turn_id",
    "session_id",
    "team_id",
    "user_id",
    "workspace_id",
    "reason",
    "usage_event_id",
    "subagent_id",
    "provider",
    "model",
    "family",
    "mode",
    "request_digest",
    "created_at",
    "expires_at",
)


async def find_request_record(db: aiosqlite.Connection, ref: str) -> dict[str, object] | None:
    """Look a record up by its id or by the billing receipt (usage event) id it links to."""
    async with db.execute(
        f"SELECT {','.join(_RECORD_COLUMNS)},chunk_hashes_json FROM llm_request_records WHERE id=? OR usage_event_id=? ORDER BY created_at LIMIT 1",
        (ref, ref),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(zip((*_RECORD_COLUMNS, "chunk_hashes_json"), row, strict=True)) if row is not None else None


async def list_turn_request_records(db: aiosqlite.Connection, turn_id: str) -> list[dict[str, object]]:
    """A Turn's records in send order, with change counts; nothing is decrypted."""
    async with db.execute(
        f"SELECT {','.join('r.' + column for column in _RECORD_COLUMNS)},"
        "(SELECT count(*) FROM llm_request_changes c WHERE c.record_id=r.id) "
        "FROM llm_request_records r WHERE r.turn_id=? ORDER BY r.created_at, r.rowid",
        (turn_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(zip((*_RECORD_COLUMNS, "change_count"), row, strict=True)) for row in rows]


async def rebuild_request(db: aiosqlite.Connection, cipher: ReferenceCipher, record: dict[str, object]) -> RebuiltRequest:
    """Decrypt a record's chunks and change records and check the body against its digest."""
    turn_id = str(record["turn_id"])
    scope = ReferenceScope(
        team_id=str(record["team_id"]),
        user_id=str(record["user_id"]),
        workspace_id=str(record["workspace_id"]),
        session_id=str(record["session_id"]),
        policy_epoch=0,
    )
    refs = json.loads(str(record["chunk_hashes_json"]))
    async with db.execute(
        "SELECT chunk_hash, ciphertext, byte_size FROM llm_request_chunks WHERE turn_id=? AND chunk_hash IN (SELECT json_extract(value, '$.h') FROM json_each(?))",
        (turn_id, record["chunk_hashes_json"]),
    ) as cursor:
        stored = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
    values: list[tuple[str, object]] = []
    omitted: list[str] = []
    for ref in refs:
        digest = ref["h"]
        if digest not in stored:
            raise ValueError(f"request chunk {digest} is missing")
        ciphertext, byte_size = stored[digest]
        if ciphertext is None:
            omitted.append(digest)
            values.append((ref["f"], {"_omitted_chunk": digest, "bytes": byte_size}))
            continue
        values.append((ref["f"], json.loads(cipher.open(scope=scope, purpose=CHUNK_PURPOSE, identity=f"{turn_id}:{digest}", ciphertext=ciphertext))))
    body = rebuild_body(values)
    matches = None if omitted else hashlib.sha256(body).hexdigest() == record["request_digest"]

    async with db.execute(
        "SELECT c.id, c.record_id, c.kind, c.detail_ciphertext, c.created_at FROM llm_request_changes c "
        "JOIN llm_request_records r ON r.id=c.record_id "
        "WHERE r.turn_id=? AND r.subagent_id IS ? AND r.created_at<=? ORDER BY r.created_at, c.rowid",
        (turn_id, record["subagent_id"], record["created_at"]),
    ) as cursor:
        change_rows = await cursor.fetchall()
    changes = tuple(
        {
            "id": change_id,
            "record_id": record_id,
            "kind": kind,
            "created_at": created_at,
            "detail": json.loads(cipher.open(scope=scope, purpose=CHANGE_PURPOSE, identity=change_id, ciphertext=ciphertext)),
        }
        for change_id, record_id, kind, ciphertext, created_at in change_rows
    )
    public = {key: value for key, value in record.items() if key != "chunk_hashes_json"}
    return RebuiltRequest(record=public, body=body, omitted_chunks=tuple(omitted), digest_matches=matches, changes=changes)
