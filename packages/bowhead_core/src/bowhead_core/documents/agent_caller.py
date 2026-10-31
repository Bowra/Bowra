"""Borrow an interactive Agent's original authority for native document work."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    ExactAgentExecutionBinding,
    _authorized_current_team_snapshot,
    _authorized_system_execution_snapshot,
    _binding_from_rows,
    _execution_by_id,
    _require_execution_assignment,
    _require_turn_scope,
    _team_snapshot_by_execution,
    _validate_team_snapshot,
    team_agent_dispatchable_state_sql,
)
from bowhead_core.document_chat_continuation import DocumentResumeReservation, DocumentTurnOwner
from bowhead_core.documents.continuation import (
    DocumentPlanningFailure,
    DocumentPlanningOutcome,
    DocumentPlanningPause,
    DocumentPlanningReview,
    validate_document_pause_in_tx,
    validate_document_worker_in_tx,
)
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.resource_disable import attribute_system_tools, compute_turn_resources, load_disable_state, team_plugin_provenance

DOCUMENT_TOOL = "template_document_submit"


@dataclass(frozen=True, slots=True)
class DocumentAgentOutcome:
    """Private runtime result; model-authored payloads cannot create this field."""

    execution_id: str
    outcome: DocumentPlanningOutcome


@dataclass(frozen=True, slots=True)
class DocumentAdmissionGuard:
    """Original selected-Tool obligation, bound only to its primary execution."""

    execution_id: str
    check: Callable[[], Awaitable[None]] = field(repr=False)

    async def __call__(self, execution_id: str) -> None:
        if execution_id != self.execution_id:
            raise DocumentRequestError("document_capability_unavailable")
        try:
            await self.check()
        except ValueError as exc:
            if str(exc) != "reference_mcp_required_tool_incomplete":
                raise
            raise DocumentRequestError("document_source_action_pending") from exc


async def read_document_caller_in_tx(
    db, *, path: str | Path, scope: DocumentScope, turn_id: str, execution_id: str, retained: bool = False
) -> ExactAgentExecutionBinding:
    """Recheck the original assignment, pinned revision and resource provenance.

    A completed caller may own a retained document question. That permits only
    the original document worker to continue; it does not restart the Agent.
    """
    previous_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        await _require_turn_scope(db, team_id=scope.team_id, user_id=scope.user_id, turn_id=turn_id)
        execution = await _execution_by_id(db, execution_id)
        if (
            execution is None
            or (execution["team_id"], execution["turn_id"]) != (scope.team_id, turn_id)
            or execution["dispatch_source"] not in {"lead", "explicit", "system"}
            or execution["status"] not in ({"admitted", "running", "completed"} if retained else {"admitted", "running"})
        ):
            raise DocumentRequestError("document_capability_unavailable")
        async with db.execute(
            "SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id "
            "WHERE t.id=? AND s.workspace_id=? AND s.user_id=? AND s.origin='normal' AND s.deleted_at IS NULL",
            (turn_id, scope.workspace_id, scope.user_id),
        ) as cursor:
            if await cursor.fetchone() is None:
                raise DocumentRequestError("document_capability_unavailable")
        if execution["agent_source_kind"] == "system":
            snapshot = await _authorized_system_execution_snapshot(db, execution, user_id=scope.user_id)
        else:
            await _require_execution_assignment(db, execution, user_id=scope.user_id)
            snapshot = await _team_snapshot_by_execution(db, execution)
            _validate_team_snapshot(snapshot)
            async with db.execute(
                f"SELECT * FROM team_subagent WHERE team_id=? AND id=? AND {team_agent_dispatchable_state_sql()}",
                (scope.team_id, execution["agent_id"]),
            ) as cursor:
                aggregate = await cursor.fetchone()
            if aggregate is None:
                raise DocumentRequestError("document_capability_unavailable")
            await _authorized_current_team_snapshot(db, db_path=Path(path), team_id=scope.team_id, user_id=scope.user_id, aggregate=aggregate)
        binding = _binding_from_rows(execution, snapshot)
        tools = binding.definition.get("tool_refs" if binding.agent_source_kind == "team" else "tool_whitelist", [])
        if DOCUMENT_TOOL not in tools:
            raise DocumentRequestError("document_capability_unavailable")
        provenance = (
            await team_plugin_provenance(str(path), binding.authored_definition or binding.definition, binding.definition)
            if binding.agent_source_kind == "team"
            else attribute_system_tools(tools)
        )
        if (
            provenance is None
            or DOCUMENT_TOOL not in compute_turn_resources(provenance, await load_disable_state(str(path), scope.team_id, scope.user_id)).tools
        ):
            raise DocumentRequestError("document_capability_unavailable")
        return binding
    except AgentExecutionBindingError as exc:
        raise DocumentRequestError("document_capability_unavailable") from exc
    finally:
        db.row_factory = previous_factory


async def document_job_caller_in_tx(db, *, scope: DocumentScope, turn_id: str, job_id: str) -> str:
    async with db.execute(
        """SELECT a.id FROM document_jobs j JOIN document_dispatches d ON d.job_id=j.id
        JOIN turn_capability_execution c ON c.id=d.capability_execution_id
        JOIN turn_agent_execution a ON a.id=c.agent_execution_id
        WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=? AND j.origin_turn_id=?
          AND a.turn_id=j.origin_turn_id AND a.team_id=j.team_id AND c.status='completed'
          AND c.output_contract='document.admission.v1'""",
        (job_id, scope.team_id, scope.workspace_id, scope.user_id, turn_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise DocumentRequestError("document_checkpoint_unavailable")
    return row[0]


async def authorize_document_job_caller(path: str | Path, *, scope: DocumentScope, turn_id: str, job_id: str, execution_id: str | None = None):
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        caller = await document_job_caller_in_tx(db, scope=scope, turn_id=turn_id, job_id=job_id)
        if execution_id is not None and caller != execution_id:
            raise DocumentRequestError("document_command_conflict")
        return await read_document_caller_in_tx(db, path=path, scope=scope, turn_id=turn_id, execution_id=caller, retained=True)


async def document_reference_pause_in_tx(db, *, turn_id: str, execution_id: str) -> bool:
    """Recognize only the original native pause of a settled Reference primary."""
    from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, document_pause_retained_in_tx, read_document_card_in_tx

    card_id = await document_card_id_in_tx(db, turn_id=turn_id)
    if card_id is None:
        return False
    try:
        record = await read_document_card_in_tx(db, card_id=card_id)
        if record.card.status not in {"pending", "resolved"} or record.reserved_generation is not None or record.consumed_generation is not None:
            return False
        if not await document_pause_retained_in_tx(db, turn_id=turn_id):
            # The original terminal writer checks the pause before changing
            # the Turn from running to clarification_pending in this transaction.
            material = await validate_document_pause_in_tx(db, worker=record.creation_worker(), checkpoint=record.snapshot.continuation.checkpoint)
            if material.question != record.card.rationale or material.runtime_snapshot_digest != record.snapshot.continuation.runtime_snapshot_digest:
                return False
        return await _document_reference_caller_in_tx(db, record=record, turn_id=turn_id, execution_id=execution_id)
    except ValueError:
        return False


async def document_reference_resume_reserved_in_tx(db, *, turn_id: str, generation: int) -> bool:
    """A validated native reservation permits only its existing worker claim."""
    from bowhead_core.document_clarification_lifecycle import _retained_material, document_answer, document_card_id_in_tx, read_document_card_in_tx

    card_id = await document_card_id_in_tx(db, turn_id=turn_id)
    if card_id is None:
        return False
    try:
        record = await read_document_card_in_tx(db, card_id=card_id)
        if record.reserved_generation != generation or record.consumed_generation is not None or generation <= record.creation_worker().generation:
            return False
        await _retained_material(db, record)
        document_answer(record.card)
        async with db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)) as cursor:
            primary = await cursor.fetchone()
        return primary is not None and await _document_reference_caller_in_tx(db, record=record, turn_id=turn_id, execution_id=primary[0])
    except ValueError:
        return False


async def require_document_reference_resume_entry_in_tx(db, *, worker: DocumentTurnOwner, reservation: DocumentResumeReservation) -> None:
    """Check the original reservation before the shared worker enters its domain.

    This read grants no replay or attempt start; the original consumer remains
    authoritative after normal worker setup and event admission.
    """
    from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, read_document_card_in_tx

    if worker.generation != reservation.generation or await document_card_id_in_tx(db, turn_id=worker.turn_id) != reservation.card_id:
        raise DocumentRequestError("document_execution_fenced")
    record = await read_document_card_in_tx(db, card_id=reservation.card_id)
    if record.snapshot.continuation.checkpoint != reservation.checkpoint or worker.model_dump(
        exclude={"generation", "owner_id"}
    ) != record.creation_worker().model_dump(exclude={"generation", "owner_id"}):
        raise DocumentRequestError("document_execution_fenced")
    await validate_document_worker_in_tx(db, worker=worker, job_id=reservation.checkpoint.job_id)
    async with db.execute("SELECT 1 FROM chat_turn_spawn_dispatch WHERE turn_id=? AND lease_kind='resume'", (worker.turn_id,)) as cursor:
        if await cursor.fetchone() is None:
            raise DocumentRequestError("document_execution_fenced")
    if not await document_reference_resume_reserved_in_tx(db, turn_id=worker.turn_id, generation=worker.generation):
        raise DocumentRequestError("document_execution_fenced")


async def document_reference_resume_heartbeat_in_tx(db, *, turn_id: str, generation: int, owner_id: str, settling: bool) -> bool | None:
    """Retain only the original native resume's lease, never start an attempt.

    None leaves the initial caller and ordinary Reference work on their original
    classifier. Consumed proof permits cleanup, not another execution entry.
    """
    from bowhead_core.document_clarification_lifecycle import document_answer, document_card_id_in_tx, read_document_card_in_tx

    card_id = await document_card_id_in_tx(db, turn_id=turn_id)
    if card_id is None:
        return None
    try:
        record = await read_document_card_in_tx(db, card_id=card_id)
        if generation not in {record.reserved_generation, record.consumed_generation}:
            return None
        creation = record.creation_worker()
        if record.reserved_generation != generation or generation <= creation.generation:
            return False
        if record.consumed_generation is None and not settling:
            return await document_reference_resume_reserved_in_tx(db, turn_id=turn_id, generation=generation)
        if record.consumed_generation is not None and (record.consumed_generation, record.consumed_owner_id) != (generation, owner_id):
            return False
        document_answer(record.card)
        async with db.execute(
            """SELECT 1 FROM document_jobs j JOIN document_turn_dispatches d ON d.job_id=j.id
            JOIN sessions s ON s.id=j.session_id JOIN workspaces w ON w.id=s.workspace_id
            WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=? AND j.session_id=? AND j.origin_turn_id=?
              AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id AND w.team_id=j.team_id
              AND j.user_source_sha256=? AND d.source_hash=j.user_source_sha256 AND d.creation_generation=? AND d.owner_id=?
              AND d.runtime_snapshot_digest=?""",
            (
                record.snapshot.continuation.checkpoint.job_id,
                creation.team_id,
                creation.workspace_id,
                creation.user_id,
                creation.session_id,
                turn_id,
                record.snapshot.continuation.checkpoint.source_hash,
                creation.generation,
                creation.owner_id,
                record.snapshot.continuation.runtime_snapshot_digest,
            ),
        ) as cursor:
            if await cursor.fetchone() is None:
                return False
        if record.consumed_generation is not None:
            async with db.execute(
                "SELECT 1 FROM document_planning_attempts WHERE job_id=? AND attempt_no=2 AND card_id=? AND creation_generation=? AND owner_id=?",
                (record.snapshot.continuation.checkpoint.job_id, card_id, generation, owner_id),
            ) as cursor:
                if await cursor.fetchone() is None:
                    return False
        async with db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)) as cursor:
            primary = await cursor.fetchone()
        return primary is not None and await _document_reference_caller_in_tx(db, record=record, turn_id=turn_id, execution_id=primary[0])
    except ValueError:
        return False


async def _document_reference_lifecycle_in_tx(db, *, turn_id: str) -> Literal["reserved", "consumed"] | None:
    """Recognize retained native work only for its original tail or recovery.

    Callers fence a live or expired dispatch in their existing transaction.
    This identity proof grants no execution entry or replacement attempt.
    """
    from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, read_document_card_in_tx

    if not db.in_transaction:
        raise DocumentRequestError("document_execution_fenced")
    card_id = await document_card_id_in_tx(db, turn_id=turn_id)
    if card_id is None:
        return None
    try:
        record = await read_document_card_in_tx(db, card_id=card_id)
        async with db.execute(
            "SELECT t.execution_generation,d.state,d.owner_id,d.lease_kind,d.lease_execution_generation "
            "FROM turns t JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id WHERE t.id=?",
            (turn_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row[3] != "resume" or row[0] != row[4] or row[0] != record.reserved_generation:
            return None
        if record.consumed_generation is None:
            if row[1] not in {"claimed", "spawned", "drain_pending"}:
                return None
            return "reserved" if await document_reference_resume_reserved_in_tx(db, turn_id=turn_id, generation=row[0]) else None
        if record.consumed_generation != row[0] or not (
            row[1] == "spawned" and row[2] == record.consumed_owner_id or row[1] == "drain_pending" and row[2] is None
        ):
            return None
        if await document_reference_resume_heartbeat_in_tx(db, turn_id=turn_id, generation=row[0], owner_id=record.consumed_owner_id, settling=True):
            return "consumed"
    except ValueError:
        pass
    return None


async def _document_reference_caller_in_tx(db, *, record, turn_id: str, execution_id: str) -> bool:
    worker = record.creation_worker()
    scope = DocumentScope(team_id=worker.team_id, user_id=worker.user_id, workspace_id=worker.workspace_id)
    caller = await document_job_caller_in_tx(db, scope=scope, turn_id=turn_id, job_id=record.snapshot.continuation.checkpoint.job_id)
    if caller != execution_id:
        return False
    async with db.execute(
        """SELECT 1 FROM turn_agent_binding b JOIN turn_agent_execution a ON a.id=b.primary_execution_id
        JOIN chat_reference_evidence e ON e.execution_id=a.id AND e.turn_id=a.turn_id
        WHERE b.turn_id=? AND a.id=? AND a.status='completed' AND e.kind='output_received' AND e.retention_state='available'""",
        (turn_id, execution_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def validate_document_agent_outcome(path: str | Path, *, worker: DocumentTurnOwner, result: DocumentAgentOutcome, execution_id: str):
    """Bind a native result to its real caller and the original shared worker."""
    if not isinstance(result, DocumentAgentOutcome) or result.execution_id != execution_id:
        raise DocumentRequestError("document_checkpoint_unavailable")
    outcome = result.outcome
    if not isinstance(outcome, (DocumentPlanningReview, DocumentPlanningPause, DocumentPlanningFailure)):
        raise DocumentRequestError("document_checkpoint_unavailable")
    scope = DocumentScope(team_id=worker.team_id, workspace_id=worker.workspace_id, user_id=worker.user_id)
    async with open_shared_ctx(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        await validate_document_worker_in_tx(db, worker=worker, job_id=outcome.job_id)
        if await document_job_caller_in_tx(db, scope=scope, turn_id=worker.turn_id, job_id=outcome.job_id) != execution_id:
            raise DocumentRequestError("document_checkpoint_unavailable")
        if isinstance(outcome, DocumentPlanningPause):
            material = await validate_document_pause_in_tx(db, worker=worker, checkpoint=outcome.checkpoint)
            if material.question != outcome.question:
                raise DocumentRequestError("document_checkpoint_unavailable")
        elif isinstance(outcome, DocumentPlanningReview):
            async with db.execute("SELECT state,draft_digest,owner FROM document_executions WHERE job_id=?", (outcome.job_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None or tuple(row) != ("review", outcome.draft_digest, worker.owner_id):
                raise DocumentRequestError("document_checkpoint_unavailable")
        else:
            async with db.execute("SELECT state,error_code,owner FROM document_executions WHERE job_id=?", (outcome.job_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None or row[0] != "failed" or row[1] != outcome.error_code or row[2] != worker.owner_id:
                raise DocumentRequestError("document_checkpoint_unavailable")
    return outcome
