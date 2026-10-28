"""Read initial reference model receipts without restoring executable material.

A fenced worker's pending reservation is an unknown outcome, not a failed or
free request. Keep the original row pending so its actual late callback can
still settle it; observing/recovering a Turn never dispatches another request.
This internal aggregate is not the per-resource UI or billing projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from bowhead_core.chat_reference_retention import reference_material_available_in_connection
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceScope, _row, _transaction


@dataclass(frozen=True, slots=True)
class ReferenceModelStatus:
    state: Literal["not_started", "prepared", "pending", "settled", "delivery_unknown"]
    material_available: bool
    context_count: int
    invocation_count: int
    completed_count: int
    not_applied_count: int
    pending_count: int
    delivery_unknown_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None


async def live_reference_model_execution_in_connection(db: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str) -> tuple[str, int] | None:
    """Read the current receipt owner through the execution path's own fence.

    This projects progress only; it neither restores material nor starts work.
    A pending row from another generation remains unknown even if a newer
    worker is live. Original callbacks may still settle their own receipts.
    """
    from bowhead_core.chat_reference_runtime_store import require_reference_worker_in_connection

    _transaction(db)
    row = await _row(
        db,
        "SELECT e.id,d.owner_id,d.lease_execution_generation FROM turn_agent_binding b "
        "JOIN turn_agent_execution e ON e.id=b.primary_execution_id AND e.turn_id=b.turn_id "
        "JOIN chat_turn_spawn_dispatch d ON d.turn_id=b.turn_id WHERE b.turn_id=? AND e.status='running'",
        (turn_id,),
    )
    if row is None:
        return None
    try:
        await require_reference_worker_in_connection(
            db, scope=scope, turn_id=turn_id, owner_id=row["owner_id"], generation=row["lease_execution_generation"]
        )
    except ValueError:
        return None
    return row["id"], row["lease_execution_generation"]


async def live_reference_model_executions_in_connection(db, *, scope, turn_id):
    """Original primary or the currently leased Plan steps, never old attempts."""
    live = await live_reference_model_execution_in_connection(db, scope=scope, turn_id=turn_id)
    result = {live} if live is not None else set()
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='view' AND name='chat_reference_plan_step_scope'", ()) is not None:
        async with db.execute(
            "SELECT execution_id,generation FROM chat_reference_plan_step_scope WHERE source_turn_id=? AND session_id=? AND user_id=? AND workspace_id=? AND team_id=?",
            (turn_id, scope.session_id, scope.user_id, scope.workspace_id, scope.team_id),
        ) as cursor:
            result.update((row[0], row[1]) for row in await cursor.fetchall())
    return result


async def require_reference_model_receipts_complete_in_connection(db: aiosqlite.Connection, *, turn_id: str) -> None:
    """Completion must not hide unresolved requests even after Agent settlement.

    Single-@ compositions predate the model receipt table and keep their existing
    contract. A mixed composition requires its installed receipt reader.
    """
    _transaction(db)
    mixed = await _row(
        db,
        "SELECT 1 FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1 "
        "AND json_extract(composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v1','initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-baseline.v1','initial-primary-skill-extension.v1')",
        (turn_id,),
    )
    if mixed and await _row(
        db,
        "SELECT 1 FROM chat_reference_model_invocations i WHERE i.turn_id=? AND i.status IN ('pending','delivery_unknown')",
        (turn_id,),
    ):
        raise ValueError("reference_model_invocation_unresolved")


async def read_reference_model_status_in_connection(db: aiosqlite.Connection, *, scope: ReferenceRequestScope, turn_id: str) -> ReferenceModelStatus:
    """One read snapshot, current owned session; no resource names or contents."""
    _transaction(db)
    scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
    if scope.session_id is None or type(turn_id) is not str or not turn_id:
        raise ValueError("reference_model_status_unavailable")
    epoch = await require_reference_request_scope(db, scope)
    parent = await _row(
        db,
        "SELECT r.execution_id,r.created_generation FROM turns t "
        "JOIN sessions s ON s.id=t.session_id JOIN chat_reference_resolutions r ON r.turn_id=t.id AND r.resolution_seq=1 "
        "JOIN chat_reference_plans p ON p.turn_id=t.id JOIN turn_agent_execution e ON e.id=r.execution_id AND e.turn_id=t.id "
        "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id "
        "WHERE t.id=? AND t.session_id=? AND s.user_id=? AND s.workspace_id=? "
        "AND json_extract(r.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v1','initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-baseline.v1','initial-primary-skill-extension.v1')",
        (turn_id, scope.session_id, scope.user_id, scope.workspace_id),
    )
    if parent is None or parent["created_generation"] != 1:
        raise ValueError("reference_model_status_unavailable")
    live = await live_reference_model_executions_in_connection(db, scope=ReferenceScope(**scope.model_dump(), policy_epoch=epoch), turn_id=turn_id)
    contexts = await _row(db, "SELECT COUNT(*) AS n FROM chat_reference_model_contexts WHERE turn_id=?", (turn_id,))
    live_sql = " OR ".join("(execution_id=? AND created_generation=?)" for _ in live) or "0"
    live_values = tuple(value for pair in sorted(live) for value in pair)
    counts = await _row(
        db,
        "SELECT COUNT(*) AS n,COALESCE(SUM(status='completed'),0) AS completed,COALESCE(SUM(status='not_applied'),0) AS not_applied,"
        "COALESCE(SUM(status='pending'),0) AS pending,COALESCE(SUM(status='delivery_unknown'),0) AS unknown,"
        f"COALESCE(SUM(status='pending' AND ({live_sql})),0) AS live_pending,"
        "SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(cached_tokens) AS cached_tokens "
        "FROM chat_reference_model_invocations WHERE turn_id=?",
        (*live_values, turn_id),
    )
    now = datetime.now(UTC)
    # Counts are durable states. The aggregate state additionally expresses
    # pending reservations whose worker can no longer confirm progress.
    if counts["unknown"] or counts["pending"] > counts["live_pending"]:
        state = "delivery_unknown"
    elif counts["pending"]:
        state = "pending"
    elif counts["n"]:
        state = "settled"
    else:
        state = "prepared" if contexts["n"] else "not_started"
    complete = bool(counts["n"]) and not (counts["pending"] or counts["unknown"])
    return ReferenceModelStatus(
        state=state,
        material_available=await reference_material_available_in_connection(db, turn_id=turn_id, now=now),
        context_count=contexts["n"],
        invocation_count=counts["n"],
        completed_count=counts["completed"],
        not_applied_count=counts["not_applied"],
        pending_count=counts["pending"],
        delivery_unknown_count=counts["unknown"],
        input_tokens=counts["input_tokens"] if complete else None,
        output_tokens=counts["output_tokens"] if complete else None,
        cached_tokens=counts["cached_tokens"] if complete else None,
    )


async def read_reference_model_status(*, db_path: str | Path, scope: ReferenceRequestScope, turn_id: str) -> ReferenceModelStatus:
    """Read-only internal entry; missing schema/file is unavailable, not empty."""
    try:
        if not isinstance(db_path, (str, Path)) or not str(db_path):
            raise ValueError()
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                return await read_reference_model_status_in_connection(db, scope=scope, turn_id=turn_id)
            finally:
                await db.rollback()
    except Exception:
        raise ValueError("reference_model_status_unavailable") from None
