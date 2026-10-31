"""Transactional model-view records at the existing SessionStore boundary.

Callers own the core transaction and existing Turn finalizer. This module has no
scheduler, execution ledger, publisher or independent database connection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Literal
from uuid import uuid4

import aiosqlite
import anyio
from bowhead_router.context_policy import CONTEXT_POLICY_VERSION, ContextPolicy, parse_context_policy
from bowhead_router.types import Message
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.context_compaction import CompactionSpan, message_digest
from bowhead_core.context_dependencies import adoption_source_validator
from bowhead_core.context_ownership import ContextOwner, context_scope, require_context_owner_in_tx
from bowhead_core.context_segments import MAX_CONTEXT_PARTS, ContextSnapshot, SealedContextSegment, open_context_messages, seal_context_messages

ContextLane = Literal["ordinary", "coordination"]
SourceValidator = Callable[[list[Message]], Awaitable[None]]


@dataclass(frozen=True)
class PreparedContextMessages:
    """Call-local encrypted bytes prepared before acquiring the database writer."""

    scope: ReferenceScope
    owner: str
    messages: list[Message]
    snapshot: ContextSnapshot
    segments: tuple[SealedContextSegment, ...]


async def prepare_context_messages(
    messages: list[Message], *, worker: ContextOwner, execution_id: str, lane: ContextLane, cipher: ReferenceCipher
) -> PreparedContextMessages:
    values = deepcopy(messages)
    scope, owner = _scope(worker), _owner(execution_id, lane)
    snapshot, segments = await anyio.to_thread.run_sync(partial(seal_context_messages, values, cipher=cipher, scope=scope, owner=owner))
    return PreparedContextMessages(scope, owner, values, snapshot, segments)


class ContextStoreError(ValueError):
    """Finite context availability or compare-and-swap failure."""


class ContextOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    configuration_version: int | None = Field(default=None, ge=0)


class ContextCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation_id: str = Field(pattern=r"^mco_[0-9a-f]{32}$")
    trigger: Literal["threshold", "capacity", "model_change", "manual"]
    input_before: int = Field(ge=0)
    input_after: int = Field(ge=0)
    prompt_version: Literal["context-summary.v1"] = "context-summary.v1"
    coverage: CompactionSpan | None = None
    subagent_id: str | None = Field(default=None, min_length=1, max_length=128)
    profile: str = Field(default="lead", min_length=1, max_length=128)
    projection_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StoredContext:
    generation_id: str
    execution_id: str
    lane: ContextLane
    snapshot: ContextSnapshot
    messages: list[Message]
    policy: ContextPolicy
    origin: ContextOrigin
    source_refs: tuple[str, ...]
    parent_id: str | None
    completion: ContextCompletion | None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


async def _row(db: aiosqlite.Connection, query: str, args: tuple = ()) -> dict | None:
    async with db.execute(query, args) as cursor:
        row = await cursor.fetchone()
        return dict(zip((column[0] for column in cursor.description), row, strict=True)) if row is not None else None


async def _ancestry_rows(db: aiosqlite.Connection, session_id: str, generation_id: str) -> dict[str, dict]:
    """Fetch reachable metadata in one statement, without a history-length limit."""
    async with db.execute(
        "WITH RECURSIVE ancestry(id) AS (VALUES(?) UNION "
        "SELECT ref.value FROM ancestry a JOIN model_context_generations g ON g.id=a.id "
        "JOIN json_each(CASE WHEN json_valid(g.source_refs_json) THEN g.source_refs_json ELSE '[]' END) ref "
        "WHERE g.session_id=? AND ref.type='text') "
        "SELECT g.* FROM ancestry a JOIN model_context_generations g ON g.id=a.id WHERE g.session_id=?",
        (generation_id, session_id, session_id),
    ) as cursor:
        names = [column[0] for column in cursor.description]
        records = [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]
        return {record["id"]: record for record in records}


async def _ancestry_segments(
    db: aiosqlite.Connection, session_id: str, records: list[tuple[dict, ContextSnapshot, list[str]]]
) -> AsyncIterator[tuple[dict, ContextSnapshot, list[str], dict[str, str]]]:
    """Batch encrypted reads within the existing maximum projection byte budget.

    The batch size bounds temporary ciphertext memory and SQL parameters only;
    every reachable generation and its current source authority are still read.
    """
    offset = 0
    while offset < len(records):
        batch = []
        parts = 0
        while offset < len(records):
            record = records[offset]
            count = len(record[1].segments)
            if batch and parts + count > MAX_CONTEXT_PARTS:
                break
            batch.append(record)
            parts += count
            offset += 1
        keys = list(dict.fromkeys((row["execution_id"], row["lane"], segment.digest) for row, snapshot, _ in batch for segment in snapshot.segments))
        values = ",".join("(?,?,?)" for _ in keys)
        async with db.execute(
            f"WITH requested(execution_id,lane,digest) AS (VALUES {values}) "
            "SELECT s.execution_id,s.lane,s.digest,s.ciphertext FROM requested r "
            "JOIN model_context_segments s ON s.execution_id=r.execution_id AND s.lane=r.lane AND s.digest=r.digest WHERE s.session_id=?",
            (*[value for key in keys for value in key], session_id),
        ) as cursor:
            ciphertext = {(row[0], row[1], row[2]): row[3] for row in await cursor.fetchall()}
        for row, snapshot, refs in batch:
            bodies = {}
            for segment in snapshot.segments:
                key = (row["execution_id"], row["lane"], segment.digest)
                if key not in ciphertext:
                    raise ContextStoreError("context_segment_unavailable")
                bodies[segment.digest] = ciphertext[key]
            yield row, snapshot, refs, bodies
        del ciphertext


def _scope(worker: ContextOwner) -> ReferenceScope:
    return ReferenceScope(**context_scope(worker).model_dump(), policy_epoch=0)


def _owner(execution_id: str, lane: ContextLane) -> str:
    if lane not in {"ordinary", "coordination"}:
        raise ContextStoreError("context_lane_invalid")
    return execution_id + ":" + lane


async def _require_owner(db: aiosqlite.Connection, worker: ContextOwner, execution_id: str, *, for_publication: bool = False) -> dict:
    if not db.in_transaction:
        raise ContextStoreError("context_transaction_required")
    await require_context_owner_in_tx(db, worker=worker)
    row = await _row(db, "SELECT * FROM turn_agent_execution WHERE id=? AND turn_id=? AND team_id=?", (execution_id, worker.turn_id, worker.team_id))
    if row is None or row["status"] not in ({"admitted", "running", "completed"} if for_publication else {"admitted", "running"}):
        raise ContextStoreError("context_execution_unavailable")
    return row


def _policy_json(policy: ContextPolicy) -> str:
    # Revalidate even internal callers; malformed settings cannot enable auto compaction.
    parse_context_policy(asdict(policy))
    return _json({"version": CONTEXT_POLICY_VERSION, "policy": asdict(policy)})


def _read_policy(encoded: str) -> ContextPolicy:
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != {"version", "policy"} or value["version"] != CONTEXT_POLICY_VERSION:
        raise ContextStoreError("context_policy_version_unavailable")
    return parse_context_policy(value["policy"])


async def write_context_in_tx(
    db: aiosqlite.Connection,
    *,
    worker: ContextOwner,
    execution_id: str,
    lane: ContextLane,
    expected_generation: str | None,
    messages: list[Message],
    policy: ContextPolicy,
    origin: ContextOrigin,
    cipher: ReferenceCipher,
    validate_sources: SourceValidator,
    source_refs: tuple[str, ...] = (),
    completion: ContextCompletion | None = None,
    for_publication: bool = False,
    prepared_messages: PreparedContextMessages | None = None,
) -> str:
    """Insert an immutable view and move only its exact execution/lane head.

    The caller commits before adopting this ID in memory or emitting completion.
    A compact operation first records its original input with this same writer;
    its result names that checkpoint as the expected generation/source.
    """
    validate_sources = adoption_source_validator(validate_sources, db)
    execution = await _require_owner(db, worker, execution_id, for_publication=for_publication)
    if for_publication and (lane != "ordinary" or execution["runtime_profile_name"] != "lead" or execution["parent_execution_id"] is not None):
        raise ContextStoreError("context_conversation_publisher_unavailable")
    owner = _owner(execution_id, lane)
    origin = ContextOrigin.model_validate(origin.model_dump())
    policy_json = _policy_json(policy)
    references = tuple(dict.fromkeys((*source_refs, *((expected_generation,) if expected_generation else ()))))
    head = await _row(
        db, "SELECT generation_id FROM model_context_heads WHERE session_id=? AND execution_id=? AND lane=?", (worker.session_id, execution_id, lane)
    )
    current = head["generation_id"] if head else None
    if completion is not None:
        completion = ContextCompletion.model_validate_json(completion.model_dump_json())
        repeated = await _row(db, "SELECT * FROM model_context_generations WHERE operation_id=?", (completion.operation_id,))
        if repeated is not None:
            if (
                repeated["id"] == current
                and repeated["parent_id"] == expected_generation
                and repeated["session_id"] == worker.session_id
                and repeated["execution_id"] == execution_id
                and repeated["lane"] == lane
                and repeated["completion_json"] == completion.model_dump_json()
                and ContextSnapshot.model_validate_json(repeated["snapshot_json"]).digest == message_digest(messages)
                and repeated["policy_json"] == policy_json
                and repeated["origin_json"] == origin.model_dump_json()
                and repeated["source_refs_json"] == _json(references)
            ):
                await read_context_generation_in_tx(
                    db,
                    worker=worker,
                    execution_id=execution_id,
                    lane=lane,
                    generation_id=current,
                    cipher=cipher,
                    validate_sources=validate_sources,
                    for_publication=for_publication,
                )
                return repeated["id"]
            raise ContextStoreError("context_operation_changed")
    if current != expected_generation:
        raise ContextStoreError("context_head_changed")
    if completion is not None and expected_generation is None:
        raise ContextStoreError("context_source_checkpoint_required")
    for reference in references:
        if (
            not isinstance(reference, str)
            or await _row(db, "SELECT id FROM model_context_generations WHERE id=? AND session_id=?", (reference, worker.session_id)) is None
        ):
            raise ContextStoreError("context_source_unavailable")
    # Validate originals, including dependencies inherited through earlier summaries.
    for reference in references:
        await read_context_generation_in_tx(
            db,
            worker=worker,
            execution_id=execution_id,
            lane=lane,
            generation_id=reference,
            cipher=cipher,
            validate_sources=validate_sources,
            for_publication=for_publication,
        )
    await validate_sources(messages)
    if (
        prepared_messages is not None
        and prepared_messages.scope == _scope(worker)
        and prepared_messages.owner == owner
        and prepared_messages.messages == messages
    ):
        snapshot, segments = prepared_messages.snapshot, prepared_messages.segments
    else:
        snapshot, segments = seal_context_messages(messages, cipher=cipher, scope=_scope(worker), owner=owner)
    if current is not None and completion is None:
        existing = await _row(db, "SELECT snapshot_json,policy_json,origin_json FROM model_context_generations WHERE id=?", (current,))
        if (
            existing
            and existing["snapshot_json"] == snapshot.model_dump_json()
            and existing["policy_json"] == policy_json
            and existing["origin_json"] == origin.model_dump_json()
        ):
            return current
    if completion is not None and completion.input_after >= completion.input_before:
        raise ContextStoreError("context_summary_not_smaller")
    now = datetime.now(UTC).isoformat()
    for segment in segments:
        prior = await _row(
            db,
            "SELECT byte_count FROM model_context_segments WHERE session_id=? AND execution_id=? AND lane=? AND digest=?",
            (worker.session_id, execution_id, lane, segment.reference.digest),
        )
        if prior is not None:
            if prior["byte_count"] != segment.reference.byte_count:
                raise ContextStoreError("context_segment_conflict")
            continue
        identity = "mcs_" + hashlib.sha256((worker.session_id + ":" + owner + ":" + segment.reference.digest).encode()).hexdigest()
        await db.execute(
            "INSERT INTO model_context_segments(id,session_id,execution_id,lane,digest,byte_count,ciphertext,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (identity, worker.session_id, execution_id, lane, segment.reference.digest, segment.reference.byte_count, segment.ciphertext, now),
        )
    generation_id = "mcg_" + uuid4().hex
    await db.execute(
        "INSERT INTO model_context_generations(id,session_id,execution_id,turn_id,lane,worker_generation,kind,parent_id,source_refs_json,snapshot_json,policy_json,origin_json,operation_id,completion_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            generation_id,
            worker.session_id,
            execution_id,
            worker.turn_id,
            lane,
            worker.generation,
            "compact" if completion else "checkpoint",
            current,
            _json(references),
            snapshot.model_dump_json(),
            policy_json,
            origin.model_dump_json(),
            completion.operation_id if completion else None,
            completion.model_dump_json() if completion else None,
            now,
        ),
    )
    # The original worker fence includes accepted cancellation and lease/generation.
    await _require_owner(db, worker, execution_id, for_publication=for_publication)
    if current is None:
        await db.execute(
            "INSERT INTO model_context_heads(session_id,execution_id,lane,generation_id) VALUES (?,?,?,?)",
            (worker.session_id, execution_id, lane, generation_id),
        )
    else:
        changed = await db.execute(
            "UPDATE model_context_heads SET generation_id=? WHERE session_id=? AND execution_id=? AND lane=? AND generation_id=?",
            (generation_id, worker.session_id, execution_id, lane, current),
        )
        if changed.rowcount != 1:
            raise ContextStoreError("context_head_changed")
    return generation_id


async def read_context_generation_in_tx(
    db: aiosqlite.Connection,
    *,
    worker: ContextOwner,
    execution_id: str,
    lane: ContextLane,
    generation_id: str,
    cipher: ReferenceCipher,
    validate_sources: SourceValidator,
    for_publication: bool = False,
) -> StoredContext:
    """Recheck the whole source ancestry before returning a derived model view."""
    graph = await read_context_graph_in_tx(
        db,
        worker=worker,
        execution_id=execution_id,
        lane=lane,
        generation_id=generation_id,
        cipher=cipher,
        validate_sources=validate_sources,
        for_publication=for_publication,
        projection_only=True,
    )
    return graph[generation_id]


async def read_context_graph_in_tx(
    db: aiosqlite.Connection,
    *,
    worker: ContextOwner,
    execution_id: str | None,
    lane: ContextLane,
    generation_id: str,
    cipher: ReferenceCipher,
    validate_sources: SourceValidator,
    for_publication: bool = False,
    projection_only: bool = False,
) -> dict[str, StoredContext]:
    """Read one authorized ancestry once for restoration or durable adoption."""
    validate_sources = adoption_source_validator(validate_sources, db)
    if execution_id is None:
        if not db.in_transaction:
            raise ContextStoreError("context_transaction_required")
        await require_context_owner_in_tx(db, worker=worker)
        execution = None
    else:
        execution = await _require_owner(db, worker, execution_id, for_publication=for_publication)
        _owner(execution_id, lane)
    pending = [generation_id]
    seen: set[str] = set()
    result: dict[str, StoredContext] = {}
    rows = await _ancestry_rows(db, worker.session_id, generation_id)
    records = []
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        row = rows.get(identity)
        if row is None:
            raise ContextStoreError("context_generation_unavailable")
        if identity == generation_id:
            same_owner = row["execution_id"] == execution_id and row["lane"] == lane
            if not same_owner:
                published = await _row(db, "SELECT generation_id FROM model_context_conversation_heads WHERE session_id=?", (worker.session_id,))
                if (
                    lane != "ordinary"
                    or row["lane"] != "ordinary"
                    or (execution is not None and (execution["runtime_profile_name"] != "lead" or execution["parent_execution_id"] is not None))
                    or published is None
                    or published["generation_id"] != generation_id
                ):
                    raise ContextStoreError("context_owner_mismatch")
        snapshot = ContextSnapshot.model_validate_json(row["snapshot_json"])
        refs = json.loads(row["source_refs_json"])
        if not isinstance(refs, list) or any(not isinstance(value, str) for value in refs) or identity in refs:
            raise ContextStoreError("context_source_references_invalid")
        records.append((row, snapshot, refs))
        pending.extend(refs)
    async for row, snapshot, refs, bodies in _ancestry_segments(db, worker.session_id, records):
        identity = row["id"]
        messages = open_context_messages(snapshot, bodies, cipher=cipher, scope=_scope(worker), owner=_owner(row["execution_id"], row["lane"]))
        await validate_sources(messages)
        # Keep the current projection and its own replacement chain. Foreign
        # ancestry still passes every source check, but must not accumulate a
        # full historical transcript copy for each earlier Turn in memory.
        retain_messages = not projection_only or identity == generation_id or (row["execution_id"] == execution_id and row["lane"] == lane)
        policy = _read_policy(row["policy_json"])
        origin = ContextOrigin.model_validate_json(row["origin_json"])
        result[identity] = StoredContext(
            identity,
            row["execution_id"],
            row["lane"],
            snapshot,
            messages if retain_messages else [],
            policy,
            origin,
            tuple(refs),
            row["parent_id"],
            ContextCompletion.model_validate_json(row["completion_json"]) if row["completion_json"] is not None else None,
        )
    if generation_id not in result:
        raise ContextStoreError("context_generation_unavailable")
    return result


async def publish_conversation_context_in_tx(
    db: aiosqlite.Connection,
    *,
    worker: ContextOwner,
    execution_id: str,
    generation_id: str,
    expected_conversation_generation: str | None,
    cipher: ReferenceCipher,
    validate_sources: SourceValidator,
) -> None:
    """Publish with the original ordinary Lead terminal write in one transaction."""
    execution = await _require_owner(db, worker, execution_id, for_publication=True)
    if execution["runtime_profile_name"] != "lead" or execution["parent_execution_id"] is not None:
        raise ContextStoreError("context_conversation_publisher_unavailable")
    current = await _row(db, "SELECT generation_id FROM model_context_conversation_heads WHERE session_id=?", (worker.session_id,))
    if (current["generation_id"] if current else None) != expected_conversation_generation:
        raise ContextStoreError("context_conversation_head_changed")
    active = await _row(
        db,
        "SELECT generation_id FROM model_context_heads WHERE session_id=? AND execution_id=? AND lane='ordinary'",
        (worker.session_id, execution_id),
    )
    if active is None or active["generation_id"] != generation_id:
        raise ContextStoreError("context_execution_head_changed")
    await read_context_generation_in_tx(
        db,
        worker=worker,
        execution_id=execution_id,
        lane="ordinary",
        generation_id=generation_id,
        cipher=cipher,
        validate_sources=validate_sources,
        for_publication=True,
    )
    turn = await _row(db, "SELECT seq FROM turns WHERE id=? AND session_id=?", (worker.turn_id, worker.session_id))
    await db.execute(
        "INSERT INTO model_context_conversation_heads(session_id,generation_id,published_turn_id,published_turn_seq) VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET generation_id=excluded.generation_id,published_turn_id=excluded.published_turn_id,published_turn_seq=excluded.published_turn_seq",
        (worker.session_id, generation_id, worker.turn_id, turn["seq"]),
    )
