"""M3-10 lease-fenced Agent Eval case jobs and scratch-Turn links."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_usage import EvalAttemptUsage

EvalJobStatus = Literal[
    "queued",
    "leased",
    "running",
    "completed",
    "failed",
    "cancelled",
]
EvalSettlementStatus = Literal[
    "passed",
    "failed",
    "judge_unknown",
    "infra_failed",
    "cancelled",
]

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_DOCUMENT_BYTES = 64 * 1024


class AgentEvalJobStoreError(RuntimeError):
    """Finite execution-control failure safe to publish as an error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvalCaseJob:
    job_id: str
    run_id: str
    case_revision_id: str
    attempt: int
    status: EvalJobStatus
    lease_generation: int
    lease_owner: str | None
    lease_expires_at: str | None
    started_at: str | None
    ended_at: str | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class ClaimedEvalCaseJob:
    job: EvalCaseJob
    lease_token: str


@dataclass(frozen=True, slots=True)
class EvalScratchTurn:
    job_id: str
    lease_generation: int
    run_id: str
    case_revision_id: str
    attempt: int
    team_id: str
    revision_ref: str
    session_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class EvalRunExecution:
    run_id: str
    team_id: str
    workspace_id: str
    requested_attempts: int
    max_concurrency: int
    max_duration_ms: int
    max_cost_microusd: int
    cancel_requested_at: str | None
    created_at: str


class AgentEvalJobStore:
    """Durable work queue with an opaque per-lease fencing token.

    Infrastructure retries reuse one quality-attempt row and increment only
    ``lease_generation``.  A stale worker cannot settle after another worker
    reclaims the job because settlement verifies the latest token inside the
    same transaction that appends the immutable result.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def materialize_jobs(
        self,
        *,
        run_id: str,
        workspace_id: str,
        attempts: int,
        max_concurrency: int,
        max_duration_ms: int,
        max_cost_microusd: int,
        case_revision_ids: Collection[str] | None = None,
    ) -> tuple[EvalCaseJob, ...]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                jobs = await self.materialize_jobs_conn(
                    db,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    attempts=attempts,
                    max_concurrency=max_concurrency,
                    max_duration_ms=max_duration_ms,
                    max_cost_microusd=max_cost_microusd,
                    case_revision_ids=case_revision_ids,
                )
                await db.commit()
                return jobs
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise AgentEvalJobStoreError("agent_eval_job_integrity_error") from exc
            except BaseException:
                await db.rollback()
                raise

    async def materialize_jobs_conn(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        workspace_id: str,
        attempts: int,
        max_concurrency: int,
        max_duration_ms: int,
        max_cost_microusd: int,
        created_at: str | None = None,
        case_revision_ids: Collection[str] | None = None,
    ) -> tuple[EvalCaseJob, ...]:
        """Materialize a complete run plan inside a caller-owned transaction."""

        if not 1 <= attempts <= 5:
            raise ValueError("Agent Eval attempts must be between 1 and 5")
        _identity(workspace_id, name="workspace")
        if not 1 <= max_concurrency <= 8:
            raise ValueError("Agent Eval concurrency must be between 1 and 8")
        if not 1_000 <= max_duration_ms <= 3_600_000:
            raise ValueError("Agent Eval duration budget is invalid")
        if not 0 <= max_cost_microusd <= 1_000_000_000:
            raise ValueError("Agent Eval cost budget is invalid")
        db.row_factory = aiosqlite.Row
        run = await _run_row(db, run_id)
        if run is None:
            raise AgentEvalJobStoreError("agent_eval_run_not_found")
        if str(run["status"]) != "queued":
            rows = await _job_rows(db, run_id)
            plan = await _execution_row(db, run_id)
            if _plan_matches(
                plan,
                workspace_id=workspace_id,
                attempts=attempts,
                max_concurrency=max_concurrency,
                max_duration_ms=max_duration_ms,
                max_cost_microusd=max_cost_microusd,
            ) and _jobs_match(rows, attempts=attempts, case_count=await _case_count(db, run_id)):
                return tuple(_job_from_row(row) for row in rows)
            raise AgentEvalJobStoreError("agent_eval_jobs_not_materializable")
        async with db.execute(
            """
            SELECT id FROM agent_eval_case_revision
            WHERE suite_revision_id=? ORDER BY id
            """,
            (run["suite_revision_id"],),
        ) as cursor:
            case_ids = [str(row[0]) for row in await cursor.fetchall()]
        if case_revision_ids is not None:
            selected = set(case_revision_ids)
            if not selected or len(selected) != len(tuple(case_revision_ids)) or not selected.issubset(case_ids):
                raise AgentEvalJobStoreError("agent_eval_diagnostic_selection_invalid")
            case_ids = [case_id for case_id in case_ids if case_id in selected]
        if not case_ids:
            raise AgentEvalJobStoreError("agent_eval_suite_revision_incomplete")
        existing = await _job_rows(db, run_id)
        plan = await _execution_row(db, run_id)
        if existing:
            if not _plan_matches(
                plan,
                workspace_id=workspace_id,
                attempts=attempts,
                max_concurrency=max_concurrency,
                max_duration_ms=max_duration_ms,
                max_cost_microusd=max_cost_microusd,
            ) or not _jobs_match(existing, attempts=attempts, case_count=len(case_ids)):
                raise AgentEvalJobStoreError("agent_eval_job_plan_conflict")
            return tuple(_job_from_row(row) for row in existing)
        timestamp = created_at or _now()
        if plan is None:
            await db.execute(
                """
                INSERT INTO agent_eval_run_execution(
                  run_id,team_id,workspace_id,requested_attempts,max_concurrency,
                  max_duration_ms,max_cost_microusd,cancel_requested_at,created_at
                ) VALUES (?,?,?,?,?,?,?,NULL,?)
                """,
                (
                    run_id,
                    run["team_id"],
                    workspace_id,
                    attempts,
                    max_concurrency,
                    max_duration_ms,
                    max_cost_microusd,
                    timestamp,
                ),
            )
        elif not _plan_matches(
            plan,
            workspace_id=workspace_id,
            attempts=attempts,
            max_concurrency=max_concurrency,
            max_duration_ms=max_duration_ms,
            max_cost_microusd=max_cost_microusd,
        ):
            raise AgentEvalJobStoreError("agent_eval_job_plan_conflict")
        for case_id in case_ids:
            for attempt in range(1, attempts + 1):
                await db.execute(
                    """
                    INSERT INTO agent_eval_case_job(
                      id,run_id,case_revision_id,attempt,status,
                      lease_generation,created_at
                    ) VALUES (?,?,?,?,'queued',0,?)
                    """,
                    (f"aej_{uuid4().hex}", run_id, case_id, attempt, timestamp),
                )
        rows = await _job_rows(db, run_id)
        return tuple(_job_from_row(row) for row in rows)

    async def claim_next_job(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> ClaimedEvalCaseJob | None:
        _identity(worker_id, name="worker")
        if not 10 <= lease_seconds <= 3600:
            raise ValueError("Agent Eval lease must be between 10 and 3600 seconds")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                from bowhead_core.agent_automation_execution import guard_run_conn

                if not await guard_run_conn(db, run_id=run_id):
                    await db.commit()
                    return None
                run = await _run_row(db, run_id)
                if run is None:
                    raise AgentEvalJobStoreError("agent_eval_run_not_found")
                if str(run["status"]) != "running":
                    raise AgentEvalJobStoreError("agent_eval_run_not_running")
                execution = await _execution_row(db, run_id)
                if execution is None:
                    raise AgentEvalJobStoreError("agent_eval_execution_not_found")
                comparison_status = await _comparison_status(db, run_id)
                if comparison_status is not None and comparison_status not in {"queued", "running"}:
                    await db.commit()
                    return None
                if execution["cancel_requested_at"] is not None:
                    await db.commit()
                    return None
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM agent_eval_case_job
                    WHERE run_id=? AND status IN ('leased','running')
                      AND lease_expires_at > ?
                    """,
                    (run_id, now_text),
                ) as cursor:
                    active_row = await cursor.fetchone()
                if active_row is not None and int(active_row[0]) >= int(execution["max_concurrency"]):
                    await db.commit()
                    return None
                async with db.execute(
                    """
                    SELECT * FROM agent_eval_case_job
                    WHERE run_id=? AND (
                      status='queued'
                      OR (status IN ('leased','running') AND lease_expires_at <= ?)
                    )
                    ORDER BY attempt,case_revision_id,id LIMIT 1
                    """,
                    (run_id, now_text),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                next_generation = int(row["lease_generation"]) + 1
                await db.execute(
                    """
                    UPDATE agent_eval_case_job
                    SET status='leased',lease_generation=?,lease_owner=?,
                        lease_token_hash=?,lease_expires_at=?
                    WHERE id=?
                    """,
                    (
                        next_generation,
                        worker_id,
                        token_hash,
                        expires_at,
                        row["id"],
                    ),
                )
                claimed = await _job_row(db, str(row["id"]))
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise AgentEvalJobStoreError("agent_eval_job_claim_conflict") from exc
            except BaseException:
                await db.rollback()
                raise
        if claimed is None:  # pragma: no cover - committed invariant
            raise AgentEvalJobStoreError("agent_eval_job_claim_failed")
        return ClaimedEvalCaseJob(job=_job_from_row(claimed), lease_token=token)

    async def start_scratch_turn(
        self,
        *,
        job_id: str,
        lease_token: str,
        session_id: str,
        turn_id: str,
        user_text: str,
    ) -> EvalScratchTurn:
        for value, name in ((session_id, "session"), (turn_id, "Turn")):
            _identity(value, name=name)
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
            raise ValueError("Agent Eval task text is invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                job = await _require_current_lease(db, job_id=job_id, lease_token=lease_token, status="leased")
                _require_unexpired_lease(job)
                run = await _run_row(db, str(job["run_id"]))
                if run is None or str(run["status"]) != "running":
                    raise AgentEvalJobStoreError("agent_eval_run_not_running")
                execution = await _execution_row(db, str(job["run_id"]))
                if execution is None:
                    raise AgentEvalJobStoreError("agent_eval_execution_not_found")
                created_at = _now()
                await db.execute(
                    """
                    INSERT INTO sessions(
                      id,workspace_id,user_id,title,started_at,last_active_at,origin
                    ) VALUES (?,?,?,'Agent Eval',?,?, 'agent_eval')
                    """,
                    (
                        session_id,
                        execution["workspace_id"],
                        run["created_by"],
                        created_at,
                        created_at,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO turns(id,session_id,seq,user_text,started_at)
                    VALUES (?,?,1,?,?)
                    """,
                    (turn_id, session_id, user_text, created_at),
                )
                await db.execute(
                    "UPDATE sessions SET active_turn_id=? WHERE id=?",
                    (turn_id, session_id),
                )
                await db.execute(
                    """
                    INSERT INTO agent_eval_scratch_turn(
                      job_id,lease_generation,run_id,case_revision_id,attempt,
                      team_id,revision_ref,session_id,turn_id,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        job["lease_generation"],
                        job["run_id"],
                        job["case_revision_id"],
                        job["attempt"],
                        run["team_id"],
                        run["revision_ref"],
                        session_id,
                        turn_id,
                        created_at,
                    ),
                )
                await db.execute(
                    """
                    UPDATE agent_eval_case_job
                    SET status='running',started_at=COALESCE(started_at,?)
                    WHERE id=?
                    """,
                    (created_at, job_id),
                )
                scratch = await _scratch_row(
                    db,
                    job_id=job_id,
                    lease_generation=int(job["lease_generation"]),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise AgentEvalJobStoreError("agent_eval_scratch_turn_scope_mismatch") from exc
            except BaseException:
                await db.rollback()
                raise
        if scratch is None:  # pragma: no cover - committed invariant
            raise AgentEvalJobStoreError("agent_eval_scratch_turn_create_failed")
        return _scratch_from_row(scratch)

    async def renew_lease(
        self,
        *,
        job_id: str,
        lease_token: str,
        lease_seconds: int = 120,
    ) -> EvalCaseJob:
        if not 10 <= lease_seconds <= 3600:
            raise ValueError("Agent Eval lease must be between 10 and 3600 seconds")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                job = await _require_current_lease(db, job_id=job_id, lease_token=lease_token)
                _require_unexpired_lease(job)
                current_expiry = _parse_timestamp(str(job["lease_expires_at"]))
                proposed_expiry = datetime.now(UTC) + timedelta(seconds=lease_seconds)
                if proposed_expiry <= current_expiry:
                    proposed_expiry = current_expiry + timedelta(microseconds=1)
                await db.execute(
                    "UPDATE agent_eval_case_job SET lease_expires_at=? WHERE id=?",
                    (proposed_expiry.isoformat(), job_id),
                )
                updated = await _job_row(db, job_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if updated is None:  # pragma: no cover - committed invariant
            raise AgentEvalJobStoreError("agent_eval_job_not_found")
        return _job_from_row(updated)

    async def settle_job(
        self,
        *,
        job_id: str,
        lease_token: str,
        status: EvalSettlementStatus,
        deterministic: Mapping[str, Any],
        judge: Mapping[str, Any] | None = None,
        trace_evidence_ref: str | None = None,
        duration_ms: int | None = None,
        cost_microusd: int | None = None,
        failure_code: str | None = None,
        subject_provider_ref: str | None = None,
        subject_provider_hash: str | None = None,
        judge_provider_ref: str | None = None,
        judge_provider_hash: str | None = None,
        reproducibility: Literal["exact", "attributed_only", "unknown"] | None = None,
        usage: EvalAttemptUsage | None = None,
    ) -> EvalCaseJob:
        """Settle the leased attempt once.

        `usage` is spent metering, not evidence: it is written with the first
        result row and is not part of the idempotent replay identity.
        """
        if status not in {"passed", "failed", "judge_unknown", "infra_failed", "cancelled"}:
            raise ValueError("Agent Eval settlement status is invalid")
        deterministic_json = _document(deterministic, name="deterministic result")
        judge_json = _document(judge, name="Judge result") if judge is not None else None
        if trace_evidence_ref is not None:
            _identity(trace_evidence_ref, name="trace evidence")
        if failure_code is not None:
            _identity(failure_code, name="failure code")
        for value, name in (
            (subject_provider_ref, "subject provider"),
            (judge_provider_ref, "Judge provider"),
        ):
            if value is not None:
                _identity(value, name=name)
        for value in (subject_provider_hash, judge_provider_hash):
            if value is not None and _HASH_RE.fullmatch(value) is None:
                raise ValueError("Agent Eval provider hash is invalid")
        if (subject_provider_ref is None) != (subject_provider_hash is None):
            raise ValueError("Agent Eval subject provider identity must be pinned together")
        if (judge_provider_ref is None) != (judge_provider_hash is None):
            raise ValueError("Agent Eval Judge provider identity must be pinned together")
        if reproducibility is not None and reproducibility not in {"exact", "attributed_only", "unknown"}:
            raise ValueError("Agent Eval reproducibility is invalid")
        for value in (duration_ms, cost_microusd):
            if value is not None and value < 0:
                raise ValueError("Agent Eval measurement cannot be negative")
        job_status = "failed" if status == "infra_failed" else ("cancelled" if status == "cancelled" else "completed")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                settled = await _job_row(db, job_id)
                if (
                    status == "cancelled"
                    and settled is not None
                    and settled["status"] == "cancelled"
                    and secrets.compare_digest(str(settled["lease_token_hash"] or ""), _hash_token(lease_token))
                ):
                    # Comparison cancellation already fenced and recorded this attempt.
                    # Preserve that immutable result when its worker acknowledges stop.
                    await db.commit()
                    return _job_from_row(settled)
                job = await _require_current_lease(db, job_id=job_id, lease_token=lease_token, status="running")
                _require_unexpired_lease(job)
                comparison_status = await _comparison_status(db, str(job["run_id"]))
                if comparison_status is not None and comparison_status not in {"queued", "running"} and status != "cancelled":
                    raise AgentEvalJobStoreError("agent_eval_comparison_generation_fenced")
                existing = await _result_row(db, job)
                requested = (
                    status,
                    deterministic_json,
                    judge_json,
                    trace_evidence_ref,
                    duration_ms,
                    cost_microusd,
                    failure_code,
                )
                if existing is None:
                    await db.execute(
                        """
                        INSERT INTO agent_eval_case_result(
                          run_id,case_revision_id,attempt,status,deterministic_json,
                          judge_json,trace_evidence_ref,duration_ms,cost_microusd,
                          failure_code,created_at,tokens_input,tokens_output,
                          tokens_cached,model_call_count
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            job["run_id"],
                            job["case_revision_id"],
                            job["attempt"],
                            *requested,
                            _now(),
                            *(
                                (usage.tokens_input, usage.tokens_output, usage.tokens_cached, usage.model_call_count)
                                if usage is not None
                                else (None, None, None, None)
                            ),
                        ),
                    )
                elif _result_identity(existing) != requested:
                    raise AgentEvalJobStoreError("agent_eval_case_result_conflict")
                async with db.execute(
                    "SELECT 1 FROM agent_eval_comparison_run WHERE run_id=?",
                    (job["run_id"],),
                ) as cursor:
                    comparison_linked = await cursor.fetchone() is not None
                if comparison_linked and reproducibility is None:
                    raise AgentEvalJobStoreError("agent_eval_attempt_identity_required")
                if reproducibility is not None:
                    identity = (
                        subject_provider_ref,
                        subject_provider_hash,
                        judge_provider_ref,
                        judge_provider_hash,
                        reproducibility,
                    )
                    async with db.execute(
                        """
                        SELECT subject_provider_ref,subject_provider_hash,
                               judge_provider_ref,judge_provider_hash,reproducibility
                        FROM agent_eval_attempt_identity
                        WHERE run_id=? AND case_revision_id=? AND attempt=?
                        """,
                        (job["run_id"], job["case_revision_id"], job["attempt"]),
                    ) as cursor:
                        existing_identity = await cursor.fetchone()
                    if existing_identity is None:
                        await db.execute(
                            """
                            INSERT INTO agent_eval_attempt_identity(
                              run_id,case_revision_id,attempt,subject_provider_ref,
                              subject_provider_hash,judge_provider_ref,judge_provider_hash,
                              reproducibility,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                job["run_id"],
                                job["case_revision_id"],
                                job["attempt"],
                                *identity,
                                _now(),
                            ),
                        )
                    elif tuple(existing_identity) != identity:
                        raise AgentEvalJobStoreError("agent_eval_attempt_identity_conflict")
                await db.execute(
                    """
                    UPDATE agent_eval_case_job
                    SET status=?,ended_at=?,failure_code=? WHERE id=?
                    """,
                    (job_status, _now(), failure_code, job_id),
                )
                updated = await _job_row(db, job_id)
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise AgentEvalJobStoreError("agent_eval_job_settlement_conflict") from exc
            except BaseException:
                await db.rollback()
                raise
        if updated is None:  # pragma: no cover - committed invariant
            raise AgentEvalJobStoreError("agent_eval_job_not_found")
        return _job_from_row(updated)

    async def list_jobs(self, run_id: str) -> tuple[EvalCaseJob, ...]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return tuple(_job_from_row(row) for row in await _job_rows(db, run_id))

    async def list_scratch_turns(self, run_id: str) -> tuple[EvalScratchTurn, ...]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM agent_eval_scratch_turn
                WHERE run_id=? ORDER BY case_revision_id,attempt,lease_generation
                """,
                (run_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return tuple(_scratch_from_row(row) for row in rows)

    async def read_execution(self, run_id: str) -> EvalRunExecution | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await _execution_row(db, run_id)
        return _execution_from_row(row) if row is not None else None

    async def list_runnable_executions(
        self,
        *,
        limit: int = 20,
    ) -> tuple[EvalRunExecution, ...]:
        """Return durable running plans for crash recovery and local workers."""

        if not 1 <= limit <= 200:
            raise ValueError("Agent Eval execution scan limit is invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT execution.*
                FROM agent_eval_run_execution execution
                JOIN agent_eval_run run ON run.id=execution.run_id
                WHERE run.status='running'
                ORDER BY execution.created_at,execution.run_id
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return tuple(_execution_from_row(row) for row in rows)

    async def request_cancel(self, run_id: str) -> EvalRunExecution:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await _execution_row(db, run_id)
                if row is None:
                    raise AgentEvalJobStoreError("agent_eval_execution_not_found")
                if row["cancel_requested_at"] is None:
                    await db.execute(
                        "UPDATE agent_eval_run_execution SET cancel_requested_at=? WHERE run_id=?",
                        (_now(), run_id),
                    )
                updated = await _execution_row(db, run_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if updated is None:  # pragma: no cover - committed invariant
            raise AgentEvalJobStoreError("agent_eval_execution_not_found")
        return _execution_from_row(updated)


async def _run_row(db: aiosqlite.Connection, run_id: str) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM agent_eval_run WHERE id=?", (run_id,)) as cursor:
        return await cursor.fetchone()


async def _execution_row(db: aiosqlite.Connection, run_id: str) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM agent_eval_run_execution WHERE run_id=?",
        (run_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _comparison_status(db: aiosqlite.Connection, run_id: str) -> str | None:
    async with db.execute(
        """
        SELECT execution.status
        FROM agent_eval_comparison_run link
        JOIN agent_eval_comparison_execution execution ON execution.id=link.execution_id
        WHERE link.run_id=?
        """,
        (run_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return str(row["status"]) if row is not None else None


async def _case_count(db: aiosqlite.Connection, run_id: str) -> int:
    async with db.execute(
        """
        SELECT COUNT(*) FROM agent_eval_case_revision case_revision
        JOIN agent_eval_run run ON run.suite_revision_id=case_revision.suite_revision_id
        WHERE run.id=?
        """,
        (run_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _job_rows(db: aiosqlite.Connection, run_id: str) -> list[aiosqlite.Row]:
    async with db.execute(
        """
        SELECT * FROM agent_eval_case_job
        WHERE run_id=? ORDER BY attempt,case_revision_id,id
        """,
        (run_id,),
    ) as cursor:
        return list(await cursor.fetchall())


async def _job_row(db: aiosqlite.Connection, job_id: str) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM agent_eval_case_job WHERE id=?", (job_id,)) as cursor:
        return await cursor.fetchone()


async def _scratch_row(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    lease_generation: int,
) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM agent_eval_scratch_turn WHERE job_id=? AND lease_generation=?",
        (job_id, lease_generation),
    ) as cursor:
        return await cursor.fetchone()


async def _require_current_lease(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    lease_token: str,
    status: str | None = None,
) -> aiosqlite.Row:
    row = await _job_row(db, job_id)
    if row is None:
        raise AgentEvalJobStoreError("agent_eval_job_not_found")
    if status is not None and str(row["status"]) != status:
        raise AgentEvalJobStoreError("agent_eval_job_state_conflict")
    if str(row["status"]) not in {"leased", "running"}:
        raise AgentEvalJobStoreError("agent_eval_job_not_leased")
    if not secrets.compare_digest(str(row["lease_token_hash"] or ""), _hash_token(lease_token)):
        raise AgentEvalJobStoreError("agent_eval_job_lease_fenced")
    return row


async def _result_row(db: aiosqlite.Connection, job: Mapping[str, Any]) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM agent_eval_case_result
        WHERE run_id=? AND case_revision_id=? AND attempt=?
        """,
        (job["run_id"], job["case_revision_id"], job["attempt"]),
    ) as cursor:
        return await cursor.fetchone()


def _job_from_row(row: Mapping[str, Any]) -> EvalCaseJob:
    return EvalCaseJob(
        job_id=str(row["id"]),
        run_id=str(row["run_id"]),
        case_revision_id=str(row["case_revision_id"]),
        attempt=int(row["attempt"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        lease_generation=int(row["lease_generation"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None),
        started_at=str(row["started_at"]) if row["started_at"] is not None else None,
        ended_at=str(row["ended_at"]) if row["ended_at"] is not None else None,
        failure_code=str(row["failure_code"]) if row["failure_code"] is not None else None,
    )


def _scratch_from_row(row: Mapping[str, Any]) -> EvalScratchTurn:
    return EvalScratchTurn(
        job_id=str(row["job_id"]),
        lease_generation=int(row["lease_generation"]),
        run_id=str(row["run_id"]),
        case_revision_id=str(row["case_revision_id"]),
        attempt=int(row["attempt"]),
        team_id=str(row["team_id"]),
        revision_ref=str(row["revision_ref"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
    )


def _execution_from_row(row: Mapping[str, Any]) -> EvalRunExecution:
    return EvalRunExecution(
        run_id=str(row["run_id"]),
        team_id=str(row["team_id"]),
        workspace_id=str(row["workspace_id"]),
        requested_attempts=int(row["requested_attempts"]),
        max_concurrency=int(row["max_concurrency"]),
        max_duration_ms=int(row["max_duration_ms"]),
        max_cost_microusd=int(row["max_cost_microusd"]),
        cancel_requested_at=(str(row["cancel_requested_at"]) if row["cancel_requested_at"] is not None else None),
        created_at=str(row["created_at"]),
    )


def _jobs_match(rows: list[Mapping[str, Any]], *, attempts: int, case_count: int) -> bool:
    if len(rows) != attempts * case_count:
        return False
    attempts_by_case: dict[str, set[int]] = {}
    for row in rows:
        attempts_by_case.setdefault(str(row["case_revision_id"]), set()).add(int(row["attempt"]))
    expected = set(range(1, attempts + 1))
    return len(attempts_by_case) == case_count and all(values == expected for values in attempts_by_case.values())


def _plan_matches(
    row: Mapping[str, Any] | None,
    *,
    workspace_id: str,
    attempts: int,
    max_concurrency: int,
    max_duration_ms: int,
    max_cost_microusd: int,
) -> bool:
    return row is not None and (
        str(row["workspace_id"]),
        int(row["requested_attempts"]),
        int(row["max_concurrency"]),
        int(row["max_duration_ms"]),
        int(row["max_cost_microusd"]),
    ) == (
        workspace_id,
        attempts,
        max_concurrency,
        max_duration_ms,
        max_cost_microusd,
    )


def _result_identity(row: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        str(row["status"]),
        str(row["deterministic_json"]),
        str(row["judge_json"]) if row["judge_json"] is not None else None,
        str(row["trace_evidence_ref"]) if row["trace_evidence_ref"] is not None else None,
        int(row["duration_ms"]) if row["duration_ms"] is not None else None,
        int(row["cost_microusd"]) if row["cost_microusd"] is not None else None,
        str(row["failure_code"]) if row["failure_code"] is not None else None,
    )


def _document(value: Mapping[str, Any], *, name: str) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Agent Eval {name} is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"Agent Eval {name} is too large")
    return encoded


def _identity(value: str, *, name: str) -> None:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ValueError(f"Agent Eval {name} identity is invalid")


def _hash_token(token: str) -> str:
    if not isinstance(token, str) or len(token) < 20:
        raise ValueError("Agent Eval lease token is invalid")
    value = f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"
    assert _HASH_RE.fullmatch(value) is not None
    return value


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AgentEvalJobStoreError("agent_eval_job_lease_malformed")
    return parsed.astimezone(UTC)


def _require_unexpired_lease(job: Mapping[str, Any]) -> None:
    if _parse_timestamp(str(job["lease_expires_at"])) <= datetime.now(UTC):
        raise AgentEvalJobStoreError("agent_eval_job_lease_expired")


def _now() -> str:
    return datetime.now(UTC).isoformat()
