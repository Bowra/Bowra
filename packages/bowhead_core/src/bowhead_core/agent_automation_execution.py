"""Revalidate original CI authority at worker and physical dispatch boundaries."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiosqlite
from bowhead_router.invocation import PhysicalInvocationSpec
from bowhead_router.types import TokenUsage

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_context import AutomationAuthority, canonical, comparison_origin_conn
from bowhead_core.agent_version_automation import AgentVersionAutomationError


async def guard_comparison_conn(db: aiosqlite.Connection, *, execution: aiosqlite.Row, timestamp: str) -> bool:
    """Settle lost authority atomically; the caller owns the write transaction."""
    internal_id = execution["coach_delegation_id"]
    if execution["actor_kind"] != "automation" and internal_id is None:
        return True
    if execution["status"] in {"completed", "failed", "cancelled"}:
        return False
    authority_reason = "coach_ci_authority_lost" if internal_id else "agent_automation_authority_lost"
    if internal_id:
        from bowhead_core.coach_ci_delegation import delegation_conn, require_active_conn, stop_delegation_conn

        try:
            grant = await require_active_conn(db, internal_id, timestamp=timestamp)
            if (grant["team_id"], grant["agent_id"], grant["owner_user_id"], grant["candidate_id"], grant["plan_id"]) != (
                execution["team_id"],
                execution["agent_id"],
                execution["actor_ref"],
                execution["candidate_id"],
                execution["plan_id"],
            ):
                raise ValueError("coach_ci_origin_mismatch")
            return True
        except (RuntimeError, ValueError) as exc:
            reason = getattr(exc, "code", "coach_ci_scope_unavailable")
            row = await delegation_conn(db, internal_id)
            await stop_delegation_conn(db, row, reason=reason, state="unknown" if reason == "coach_ci_invocation_unknown" else "revoked")
    else:
        try:
            context = json.loads(execution["automation_context_json"] or "null")
            if not isinstance(context, dict):
                raise AgentVersionAutomationError("agent_automation_execution_context_missing")
            authority = AutomationAuthority(
                principal_id=execution["actor_ref"],
                grant_id=execution["automation_grant_id"],
                repository=context["repository"],
                ref=context["ref"],
                effective_owner_user_id=context["effective_owner_user_id"],
            )
            await comparison_origin_conn(db, execution=execution, authority=authority, scope="eval:run", require_executable_space=True)
            return True
        except (AgentVersionAutomationError, KeyError, TypeError, ValueError) as exc:
            reason = getattr(exc, "code", "agent_automation_execution_context_missing")
    stale_environment = reason == "agent_eval_plan_stale"
    if stale_environment:
        reason = "agent_eval_workspace_scope_changed"
    # Import locally: comparison and job stores both call this transaction helper.
    from bowhead_core.agent_eval_comparison import _cancel_run_conn

    async with db.execute(
        "SELECT r.* FROM agent_eval_run r JOIN agent_eval_comparison_run l ON l.run_id=r.id WHERE l.execution_id=?",
        (execution["id"],),
    ) as cursor:
        runs = await cursor.fetchall()
    for run in runs:
        await db.execute(
            "UPDATE agent_eval_run_execution SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE run_id=?", (timestamp, run["id"])
        )
        if run["status"] in {"queued", "running"}:
            await _cancel_run_conn(db, run=run, timestamp=timestamp)
    if execution["status"] == "finalizing":
        await db.execute(
            "UPDATE agent_eval_comparison_execution SET status='failed',finished_at=?,failure_code=?,row_version=row_version+1 WHERE id=? AND status='finalizing'",
            (timestamp, reason, execution["id"]),
        )
    else:
        await db.execute(
            "UPDATE agent_eval_comparison_execution SET status='cancelled',cancelled_at=?,failure_code=?,row_version=row_version+1 WHERE id=? AND status IN ('queued','running')",
            (timestamp, reason, execution["id"]),
        )
    await db.execute(
        """UPDATE agent_eval_candidate_control SET readiness=?,stale_subject=?,
           reason_codes_json=?,active_execution_id=NULL,active_snapshot_id=NULL,active_override_id=NULL,
           row_version=row_version+1,updated_at=?
           WHERE candidate_id=? AND active_plan_id=? AND active_execution_id=?""",
        (
            "stale" if stale_environment else "unknown",
            "evidence" if stale_environment else None,
            canonical([reason] if stale_environment else [authority_reason, reason]),
            timestamp,
            execution["candidate_id"],
            execution["plan_id"],
            execution["id"],
        ),
    )
    return False


async def guard_run_conn(db: aiosqlite.Connection, *, run_id: str) -> bool:
    async with db.execute(
        "SELECT e.* FROM agent_eval_comparison_execution e JOIN agent_eval_comparison_run l ON l.execution_id=e.id WHERE l.run_id=?",
        (run_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is None or await guard_comparison_conn(db, execution=row, timestamp=datetime.now(UTC).isoformat())


class AutomationExecutionGuard:
    def __init__(
        self,
        db_path: str,
        run_id: str,
        *,
        job_id: str | None = None,
        lease_generation: int | None = None,
        runtime_check: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.db_path, self.run_id = db_path, run_id
        self.job_id, self.lease_generation = job_id, lease_generation
        self.runtime_check = runtime_check

    async def internal_delegation(self) -> str | None:
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "SELECT e.coach_delegation_id FROM agent_eval_comparison_execution e JOIN agent_eval_comparison_run l ON l.execution_id=e.id WHERE l.run_id=?",
                (self.run_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def is_automation(self) -> bool:
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT e.actor_kind FROM agent_eval_comparison_execution e JOIN agent_eval_comparison_run l ON l.execution_id=e.id WHERE l.run_id=?",
                (self.run_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
            return row is not None and row[0] == "automation"

    async def requires_physical_guard(self) -> bool:
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "SELECT e.actor_kind,e.coach_delegation_id FROM agent_eval_comparison_execution e JOIN agent_eval_comparison_run l ON l.execution_id=e.id WHERE l.run_id=?",
                (self.run_id,),
            )
            row = await cursor.fetchone()
            return row is not None and (row[0] == "automation" or row[1] is not None)

    async def allowed(self) -> bool:
        internal_id = await self.internal_delegation()
        denied = None
        if internal_id and self.runtime_check is not None:
            try:
                await self.runtime_check(self.run_id)
            except Exception as exc:
                denied = getattr(exc, "code", "coach_ci_runtime_unavailable")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                if denied and internal_id:
                    from bowhead_core.coach_ci_delegation import delegation_conn, stop_delegation_conn

                    await stop_delegation_conn(db, await delegation_conn(db, internal_id), reason=denied, state="revoked")
                result = await guard_run_conn(db, run_id=self.run_id)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def require(self) -> None:
        if not await self.allowed():
            raise AgentVersionAutomationError("agent_automation_authority_lost")

    async def begin(self, spec: PhysicalInvocationSpec):
        if await self.internal_delegation() and self.runtime_check is None:
            from bowhead_core.coach_ci_delegation import CoachCIError

            raise CoachCIError("coach_ci_runtime_gate_required")
        await self.require()
        if not await self.is_automation() and await self.requires_physical_guard():
            from bowhead_core.coach_ci_budget import begin_invocation

            return await begin_invocation(self.db_path, run_id=self.run_id, job_id=self.job_id, lease_generation=self.lease_generation, spec=spec)
        return self

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        # Preserve accounting for an already dispatched response. Every subsequent
        # dispatch and finalization independently revalidates authority.
        return True

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        return await self.allowed()

    async def unknown(self, *, reason_code: str) -> None:
        # Physical usage and outcome ledgers remain owned by the existing observer.
        return None
