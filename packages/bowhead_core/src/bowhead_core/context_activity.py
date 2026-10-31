"""Content-free context activity through the existing ordered Turn event log."""

from __future__ import annotations

import json
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^mco_[0-9a-f]{32}$")
    execution_id: str = Field(min_length=1, max_length=128)
    lane: Literal["ordinary", "coordination"]
    subagent_id: str | None = Field(default=None, min_length=1, max_length=128)
    profile: str = Field(min_length=1, max_length=128)
    phase: Literal["running", "committed", "skipped"]
    trigger: Literal["threshold", "capacity", "model_change", "manual"]
    source_generation: str | None = Field(pattern=r"^mcg_[0-9a-f]{32}$")
    generation_id: str | None = Field(default=None, pattern=r"^mcg_[0-9a-f]{32}$")
    reason: Literal["summary_result_invalid", "summary_reduction_insufficient", "summary_call_failed", "no_change"] | None = None

    @model_validator(mode="after")
    def require_source_for_replacement(self):
        if self.source_generation is None and (self.phase != "skipped" or self.reason != "no_change"):
            raise ValueError("A context replacement requires its original source generation")
        return self

    def event_id(self) -> str:
        return f"evt_{self.operation_id}_{self.phase}"

    def payload(self, turn_id: str) -> dict:
        return {
            **self.model_dump(),
            "card_id": turn_id,
            "turn_id": turn_id,
            **({"parent_agent_id": self.subagent_id} if self.subagent_id is not None else {}),
        }


async def missing_context_completions_in_connection(db, *, turn_id: str, generation: int) -> tuple[ContextActivity, ...]:
    """Read immutable completion evidence for the original Terminal recovery owner."""
    from bowhead_core.context_store import ContextCompletion, ContextStoreError

    async with db.execute("SELECT 1 FROM sqlite_master WHERE name='model_context_generations'") as cursor:
        if await cursor.fetchone() is None:
            return ()
    async with db.execute(
        "SELECT id,execution_id,lane,parent_id,completion_json FROM model_context_generations "
        "WHERE turn_id=? AND worker_generation=? AND kind='compact' ORDER BY created_at,id",
        (turn_id, generation),
    ) as cursor:
        rows = await cursor.fetchall()
    missing = []
    for identity, execution, lane, source, encoded in rows:
        completion = ContextCompletion.model_validate_json(encoded)
        if completion.trigger == "manual":
            # A prepared maintenance checkpoint alone is not conversation success.
            continue
        activity = ContextActivity(
            operation_id=completion.operation_id,
            execution_id=execution,
            lane=lane,
            subagent_id=completion.subagent_id,
            profile=completion.profile,
            phase="committed",
            trigger=completion.trigger,
            source_generation=source,
            generation_id=identity,
        )
        async with db.execute(
            "SELECT payload_json FROM turn_events WHERE turn_id=? AND event_type='agent.context' "
            "AND json_extract(payload_json,'$.operation_id')=? AND json_extract(payload_json,'$.phase')='committed'",
            (turn_id, activity.operation_id),
        ) as cursor:
            published = await cursor.fetchall()
        if published:
            expected = activity.payload(turn_id)
            if len(published) != 1 or any(json.loads(published[0][0]).get(key) != value for key, value in expected.items()):
                raise ContextStoreError("context_publication_conflict")
        else:
            missing.append(activity)
    return tuple(missing)


async def recover_context_publications_in_connection(db: aiosqlite.Connection, *, turn_id: str) -> int:
    """Repair committed completion in the original expired-worker transaction."""
    from bowhead_core.context_store import ContextCompletion, ContextStoreError
    from bowhead_core.turn_log_store import _semantic_payload

    if not db.in_transaction:
        raise ContextStoreError("context_recovery_transaction_required")
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name='model_context_generations'") as cursor:
        if await cursor.fetchone() is None:
            return 0
    async with db.execute(
        "SELECT id,execution_id,lane,parent_id,completion_json,created_at FROM model_context_generations "
        "WHERE turn_id=? AND kind='compact' ORDER BY created_at,id",
        (turn_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    inserted = 0
    for identity, execution, lane, source, encoded, created_at in rows:
        completion = ContextCompletion.model_validate_json(encoded)
        if completion.trigger == "manual":
            async with db.execute("SELECT 1 FROM model_context_conversation_heads WHERE published_turn_id=?", (turn_id,)) as cursor:
                if await cursor.fetchone() is None:
                    continue
        event = ContextActivity(
            operation_id=completion.operation_id,
            execution_id=execution,
            lane=lane,
            subagent_id=completion.subagent_id,
            profile=completion.profile,
            phase="committed",
            trigger=completion.trigger,
            source_generation=source,
            generation_id=identity,
        )
        payload = event.payload(turn_id)
        async with db.execute("SELECT turn_id,event_type,payload_json FROM turn_events WHERE id=?", (event.event_id(),)) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            if existing[0] != turn_id or existing[1] != "agent.context" or _semantic_payload(json.loads(existing[2])) != payload:
                raise ContextStoreError("context_publication_conflict")
            continue
        async with db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?", (turn_id,)) as cursor:
            sequence = (await cursor.fetchone())[0]
        await db.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (event.event_id(), turn_id, sequence, "agent.context", json.dumps({**payload, "seq": sequence, "evt_seq": sequence}), created_at),
        )
        inserted += 1
    return inserted
