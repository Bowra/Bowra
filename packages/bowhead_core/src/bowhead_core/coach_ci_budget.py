"""Reserve one durable slot before each internal CI physical provider dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import aiosqlite
from bowhead_router.invocation import PhysicalInvocationSpec
from bowhead_router.types import TokenUsage

from bowhead_core._db import open_shared_ctx
from bowhead_core.coach_ci_delegation import CoachCIError, delegation_conn, now, require_active_conn, stop_delegation_conn


async def begin_invocation(db_path: str, *, run_id: str, job_id: str | None, lease_generation: int | None, spec: PhysicalInvocationSpec):
    if not job_id or lease_generation is None:
        raise CoachCIError("coach_ci_dispatch_identity_required")
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """SELECT e.coach_delegation_id FROM agent_eval_comparison_execution e
               JOIN agent_eval_comparison_run l ON l.execution_id=e.id WHERE l.run_id=?""",
            (run_id,),
        )
        linked = await cursor.fetchone()
        if linked is None or not linked["coach_delegation_id"]:
            raise CoachCIError("coach_ci_not_found")
        grant = await require_active_conn(db, linked["coach_delegation_id"])
        cursor = await db.execute(
            "SELECT 1 FROM agent_eval_case_job WHERE id=? AND run_id=? AND lease_generation=? AND status='running' AND lease_expires_at>?",
            (job_id, run_id, lease_generation, now()),
        )
        if await cursor.fetchone() is None:
            raise CoachCIError("coach_ci_lease_lost")
        cursor = await db.execute("SELECT count(*) FROM coach_ci_invocation WHERE delegation_id=?", (grant["id"],))
        if int((await cursor.fetchone())[0]) >= grant["max_provider_calls"]:
            await stop_delegation_conn(db, grant, reason="coach_ci_provider_budget_exhausted", state="exhausted")
            await db.commit()
            raise CoachCIError("coach_ci_provider_budget_exhausted")
        identity = "acii_" + uuid4().hex
        await db.execute(
            """INSERT INTO coach_ci_invocation(id,delegation_id,run_id,job_id,lease_generation,provider,model,adapter_contract_key,
               request_digest,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
            (
                identity,
                grant["id"],
                run_id,
                job_id,
                lease_generation,
                spec.provider,
                spec.model,
                spec.adapter_contract_key,
                spec.request_digest,
                now(),
            ),
        )
        await db.commit()
        return CoachCIInvocation(db_path, identity, grant["id"])


@dataclass(frozen=True)
class CoachCIInvocation:
    db_path: str
    identity: str
    delegation_id: str

    async def _settle(self, *, state: str, usage: TokenUsage | None = None, response_digest: str | None = None, code: str | None = None) -> bool:
        expected = (
            state,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            usage.cached_tokens if usage else None,
            response_digest,
            code,
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT state,tokens_input,tokens_output,tokens_cached,response_digest,outcome_code FROM coach_ci_invocation WHERE id=? AND delegation_id=?",
                (self.identity, self.delegation_id),
            )
            old = await cursor.fetchone()
            if old is None:
                raise CoachCIError("coach_ci_usage_not_found")
            if old["state"] != "pending":
                if tuple(old) != expected:
                    raise CoachCIError("coach_ci_usage_conflict")
                return True
            await db.execute(
                """UPDATE coach_ci_invocation SET state=?,tokens_input=?,tokens_output=?,tokens_cached=?,response_digest=?,outcome_code=?,settled_at=?
                   WHERE id=? AND state='pending'""",
                (*expected, now(), self.identity),
            )
            if state == "unknown":
                await stop_delegation_conn(db, await delegation_conn(db, self.delegation_id), reason="coach_ci_invocation_unknown", state="unknown")
            await db.commit()
        return True

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        # A known late response remains accounted for after cancellation. Claim and
        # comparison guards independently fence business settlement and green evidence.
        return await self._settle(state="completed", usage=usage, response_digest=response_digest)

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        # Admission attempts consume the finite call budget even when safely rejected.
        return await self._settle(state="not_applied", response_digest=evidence_digest, code=evidence_code)

    async def unknown(self, *, reason_code: str) -> None:
        await self._settle(state="unknown", code=reason_code)
