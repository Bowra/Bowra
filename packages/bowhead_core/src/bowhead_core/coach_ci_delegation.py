"""Internal CI consent, original-target binding and cumulative dispatch authority."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_context import canonical, digest
from bowhead_core.agent_change_proposal_store import _require_owner_conn
from bowhead_core.agent_eval_plan_store import _workspace_scope
from bowhead_core.agent_revision_store import AgentAudience, _working_copy_hash, working_copy_content_hash
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope

Authorize = Callable[[aiosqlite.Connection], Awaitable[None]]


class CoachCIError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Request id of a check the check-after-edit setting starts: one per Coach write, so a repeated request in
# the same Turn returns the same delegation, and supersede can tell these from checks the Owner started.
STANDING_REQUEST_PREFIX = "after-edit:"


def now() -> str:
    return datetime.now(UTC).isoformat()


async def scope_conn(db: aiosqlite.Connection, row: Any) -> dict:
    await _require_owner_conn(db, team_id=row["team_id"], agent_id=row["agent_id"], actor_user_id=row["owner_user_id"])
    try:
        await require_reference_request_scope(
            db,
            ReferenceRequestScope(
                team_id=row["team_id"],
                user_id=row["owner_user_id"],
                workspace_id=row["workspace_id"],
                session_id=row["source_session_id"],
            ),
        )
    except ValueError as exc:
        raise CoachCIError("coach_ci_scope_unavailable") from exc
    scope = await _workspace_scope(db, team_id=row["team_id"], workspace_id=row["workspace_id"], actor_user_id=row["owner_user_id"])
    if scope is None:
        raise CoachCIError("coach_ci_scope_unavailable")
    cursor = await db.execute("SELECT 1 FROM turns WHERE id=? AND session_id=?", (row["source_turn_id"], row["source_session_id"]))
    if await cursor.fetchone() is None:
        raise CoachCIError("coach_ci_origin_unavailable")
    return scope


async def context_conn(db: aiosqlite.Connection, row: Any) -> dict:
    scope = await scope_conn(db, row)
    cursor = await db.execute(
        """SELECT p.id,p.status,p.row_version,p.patch_hash,p.base_release_id,d.selected_group_ids_json,
                  d.result_working_copy_id,d.result_working_copy_version,d.result_working_copy_hash,d.result_content_hash
           FROM agent_change_proposal p JOIN agent_change_proposal_decision d ON d.proposal_id=p.id AND d.decision_kind='patch_accept'
           WHERE p.id=? AND p.team_id=? AND p.agent_id=? AND p.status='accepted' AND p.base_suite_revision_id IS NULL
             AND d.actor_user_id=?""",
        (row["proposal_id"], row["team_id"], row["agent_id"], row["owner_user_id"]),
    )
    proposal = await cursor.fetchone()
    if proposal is None:
        raise CoachCIError("coach_ci_accepted_proposal_required")
    cursor = await db.execute(
        "SELECT * FROM team_subagent_working_copy WHERE id=? AND team_id=? AND subagent_id=?",
        (proposal["result_working_copy_id"], row["team_id"], row["agent_id"]),
    )
    working = await cursor.fetchone()
    if working is None or working["row_version"] < proposal["result_working_copy_version"]:
        raise CoachCIError("coach_ci_accepted_baseline_changed")
    current_hash = _working_copy_hash(
        working=working,
        definition=json.loads(working["definition_json"]),
        listing=json.loads(working["listing_json"]),
        audience=AgentAudience.from_mapping(json.loads(working["audience_json"])),
    )
    if proposal["result_content_hash"] is not None:
        accepted_content_matches = working_copy_content_hash(working) == proposal["result_content_hash"]
    else:
        # Historical decisions cannot prove a later metadata-only transition.
        accepted_content_matches = current_hash == proposal["result_working_copy_hash"]
    if not accepted_content_matches:
        raise CoachCIError("coach_ci_accepted_baseline_changed")
    cursor = await db.execute("SELECT live_release_id FROM team_subagent WHERE id=? AND team_id=?", (row["agent_id"], row["team_id"]))
    live = (await cursor.fetchone())[0]
    if live != working["base_release_id"]:
        raise CoachCIError("coach_ci_live_baseline_changed")
    cursor = await db.execute(
        "SELECT id,policy_hash FROM agent_eval_policy_revision WHERE team_id=? AND agent_id=? ORDER BY sequence DESC LIMIT 1",
        (row["team_id"], row["agent_id"]),
    )
    policy = await cursor.fetchone()
    proposal_value = dict(proposal)
    if proposal_value["result_content_hash"] is None:
        proposal_value.pop("result_content_hash")
    return {
        "schema_version": 1,
        "proposal": proposal_value,
        "working_copy_id": working["id"],
        "working_copy_version": working["row_version"],
        "working_copy_hash": current_hash,
        "live_release_id": live,
        "scope": scope,
        "policy": dict(policy) if policy else None,
        "owner_suites": await owner_suites_conn(db, row),
    }


async def owner_suites_conn(db: aiosqlite.Connection, row: Any) -> list[dict]:
    cursor = await db.execute(
        """SELECT s.id,s.state,r.id AS revision_id,r.definition_hash FROM agent_eval_suite s
           LEFT JOIN agent_eval_suite_revision r ON r.suite_id=s.id AND r.sequence=(SELECT max(sequence) FROM agent_eval_suite_revision WHERE suite_id=s.id)
           WHERE s.team_id=? AND s.agent_id=? AND s.suite_kind='owner_quality' ORDER BY s.id""",
        (row["team_id"], row["agent_id"]),
    )
    return [dict(item) for item in await cursor.fetchall()]


async def delegation_conn(db: aiosqlite.Connection, delegation_id: str) -> aiosqlite.Row:
    cursor = await db.execute("SELECT * FROM coach_ci_delegation WHERE id=?", (delegation_id,))
    row = await cursor.fetchone()
    if row is None:
        raise CoachCIError("coach_ci_not_found")
    return row


async def require_active_conn(
    db: aiosqlite.Connection, delegation_id: str, *, preparing: bool = False, timestamp: str | None = None, verify_context: bool = True
) -> aiosqlite.Row:
    row = await delegation_conn(db, delegation_id)
    if row["state"] != "authorized":
        raise CoachCIError(row["stop_reason"] or "coach_ci_not_authorized")
    if not row["expires_at"] or row["expires_at"] <= (timestamp or now()):
        raise CoachCIError("coach_ci_expired")
    original = json.loads(row["context_json"])
    scope = await scope_conn(db, row)
    if scope != original["scope"]:
        raise CoachCIError("coach_ci_scope_changed")
    if await owner_suites_conn(db, row) != original["owner_suites"]:
        raise CoachCIError("coach_ci_suite_changed")
    if preparing and row["candidate_id"] is None:
        if verify_context and digest(await context_conn(db, row)) != row["context_hash"]:
            raise CoachCIError("coach_ci_context_changed")
    elif row["candidate_id"] is not None:
        cursor = await db.execute(
            """SELECT c.state,c.candidate_hash,c.base_live_release_id,a.live_release_id,p.plan_hash,p.candidate_hash AS plan_candidate_hash,
                      p.policy_revision_id,ctl.active_plan_id,
                      (SELECT id FROM agent_eval_policy_revision WHERE team_id=c.team_id AND agent_id=c.subagent_id ORDER BY sequence DESC LIMIT 1) AS latest_policy
               FROM team_subagent_release_candidate c JOIN team_subagent a ON a.id=c.subagent_id
               JOIN agent_eval_plan p ON p.id=? JOIN agent_eval_candidate_control ctl ON ctl.candidate_id=c.id
               WHERE c.id=? AND c.team_id=? AND c.subagent_id=?""",
            (row["plan_id"], row["candidate_id"], row["team_id"], row["agent_id"]),
        )
        bound = await cursor.fetchone()
        if (
            bound is None
            or bound["state"] not in {"prepared", "pending", "approved"}
            or bound["active_plan_id"] != row["plan_id"]
            or bound["policy_revision_id"] != bound["latest_policy"]
            or bound["live_release_id"] != bound["base_live_release_id"]
            or bound["candidate_hash"] != bound["plan_candidate_hash"]
        ):
            raise CoachCIError("coach_ci_plan_changed")
    else:
        raise CoachCIError("coach_ci_preparation_required")
    # An uncertain send is never a retryable unspent slot. A crashed lease with a
    # pending receipt has the same conservative meaning after worker restart.
    cursor = await db.execute(
        """SELECT 1 FROM coach_ci_invocation i JOIN agent_eval_case_job j ON j.id=i.job_id
           WHERE i.delegation_id=? AND (i.state='unknown' OR (i.state='pending' AND
             (j.lease_generation!=i.lease_generation OR j.status NOT IN ('leased','running') OR j.lease_expires_at<=?))) LIMIT 1""",
        (delegation_id, timestamp or now()),
    )
    if await cursor.fetchone() is not None:
        raise CoachCIError("coach_ci_invocation_unknown")
    return row


async def bind_preparation_conn(db: aiosqlite.Connection, delegation_id: str, result: Any, *, accepted_working_copy_hash: str) -> None:
    row = await require_active_conn(db, delegation_id, preparing=True, verify_context=False)
    original = json.loads(row["context_json"])
    if accepted_working_copy_hash != original["working_copy_hash"]:
        raise CoachCIError("coach_ci_accepted_baseline_changed")
    if original["policy"] is not None and result.policy_revision_id != original["policy"]["id"]:
        raise CoachCIError("coach_ci_context_changed")
    # The existing writer materializes confirmed listing translations. Preserve
    # both the consent baseline and the resulting preparation hash explicitly.
    value = {**asdict(result), "accepted_working_copy_hash": accepted_working_copy_hash}
    if row["candidate_id"] is not None:
        if row["preparation_json"] != canonical(value):
            raise CoachCIError("coach_ci_preparation_conflict")
        return
    await db.execute(
        "UPDATE coach_ci_delegation SET candidate_id=?,plan_id=?,preparation_json=?,row_version=row_version+1 WHERE id=? AND candidate_id IS NULL",
        (result.candidate_id, result.plan_id, canonical(value), delegation_id),
    )


class CoachCIDelegations:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def request(
        self,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        workspace_id: str,
        session_id: str,
        turn_id: str,
        proposal_id: str,
        request_id: str,
        max_provider_calls: int,
        max_comparisons: int,
        duration_seconds: int,
        authorize: Authorize,
    ) -> dict:
        if not 1 <= max_provider_calls <= 200 or not 1 <= max_comparisons <= 5 or not 60 <= duration_seconds <= 3600:
            raise CoachCIError("coach_ci_budget_invalid")
        if not request_id or len(request_id) > 64:
            raise CoachCIError("coach_ci_request_invalid")
        identity = {
            "team_id": team_id,
            "agent_id": agent_id,
            "owner_user_id": owner_user_id,
            "workspace_id": workspace_id,
            "source_session_id": session_id,
            "source_turn_id": turn_id,
            "proposal_id": proposal_id,
        }
        budget = {"max_provider_calls": max_provider_calls, "max_comparisons": max_comparisons, "duration_seconds": duration_seconds}
        request_key = f"{turn_id}:{request_id}"
        request_hash = digest({**identity, **budget})
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await authorize(db)
            await scope_conn(db, identity)
            cursor = await db.execute(
                "SELECT id,request_hash FROM coach_ci_delegation WHERE team_id=? AND owner_user_id=? AND request_key=?",
                (team_id, owner_user_id, request_key),
            )
            old = await cursor.fetchone()
            if old is not None:
                if old["request_hash"] != request_hash:
                    raise CoachCIError("coach_ci_idempotency_conflict")
                return await self._project(db, await delegation_conn(db, old["id"]))
            cursor = await db.execute("SELECT 1 FROM turns WHERE id=? AND session_id=?", (turn_id, session_id))
            if await cursor.fetchone() is None:
                raise CoachCIError("coach_ci_origin_unavailable")
            context = await context_conn(db, identity)
            delegation_id = "acig_" + uuid4().hex
            await db.execute(
                """INSERT INTO coach_ci_delegation(id,team_id,agent_id,owner_user_id,workspace_id,source_session_id,source_turn_id,proposal_id,
                   request_key,request_hash,context_json,context_hash,max_provider_calls,max_comparisons,duration_seconds,state,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'requested',?)""",
                (
                    delegation_id,
                    team_id,
                    agent_id,
                    owner_user_id,
                    workspace_id,
                    session_id,
                    turn_id,
                    proposal_id,
                    request_key,
                    request_hash,
                    canonical(context),
                    digest(context),
                    max_provider_calls,
                    max_comparisons,
                    duration_seconds,
                    now(),
                ),
            )
            await authorize(db)
            await db.commit()
            return await self._project(db, await delegation_conn(db, delegation_id))

    async def authorize(
        self, *, team_id: str, agent_id: str, actor_user_id: str, delegation_id: str, expected_version: int, authorize: Authorize
    ) -> dict:
        """Owner consent: the explicit Owner HTTP action, or the Owner's standing check-after-edit setting
        applied to Coach's own draft write in the same Turn. A model Tool never chooses to call this."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await authorize(db)
            row = await self._owned(db, team_id, agent_id, actor_user_id, delegation_id)
            if row["state"] == "authorized" and row["row_version"] >= expected_version:
                return await self._project(db, row)
            if row["state"] != "requested" or row["row_version"] != expected_version:
                raise CoachCIError("coach_ci_consent_changed")
            if digest(await context_conn(db, row)) != row["context_hash"]:
                raise CoachCIError("coach_ci_context_changed")
            timestamp = now()
            expiry = (datetime.fromisoformat(timestamp) + timedelta(seconds=row["duration_seconds"])).isoformat()
            await db.execute(
                "UPDATE coach_ci_delegation SET state='authorized',authorized_at=?,expires_at=?,row_version=row_version+1 WHERE id=?",
                (timestamp, expiry, delegation_id),
            )
            await db.commit()
            return await self._project(db, await delegation_conn(db, delegation_id))

    async def get(self, *, team_id: str, agent_id: str, actor_user_id: str, delegation_id: str) -> dict:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            return await self._project(db, await self._owned(db, team_id, agent_id, actor_user_id, delegation_id))

    async def list(self, *, team_id: str, agent_id: str, actor_user_id: str, offset: int = 0, limit: int = 10) -> dict:
        if not 0 <= offset <= 10000 or not 1 <= limit <= 10:
            raise CoachCIError("coach_ci_page_invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            cursor = await db.execute(
                "SELECT * FROM coach_ci_delegation WHERE team_id=? AND agent_id=? AND owner_user_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (team_id, agent_id, actor_user_id, limit + 1, offset),
            )
            rows = await cursor.fetchall()
            items = []
            for row in rows[:limit]:
                item = await self._project(db, row)
                context = item.pop("context")
                item["accepted_baseline"] = {key: context[key] for key in ("working_copy_id", "working_copy_version")}
                items.append(item)
            return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    async def revoke(self, *, team_id: str, agent_id: str, actor_user_id: str, delegation_id: str) -> dict:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await self._owned(db, team_id, agent_id, actor_user_id, delegation_id)
            if row["state"] in {"requested", "authorized"}:
                await stop_delegation_conn(db, row, reason="coach_ci_owner_cancelled", state="revoked")
                from bowhead_core.agent_automation_execution import guard_comparison_conn

                cursor = await db.execute(
                    "SELECT * FROM agent_eval_comparison_execution WHERE coach_delegation_id=? AND status IN ('queued','running','finalizing')",
                    (delegation_id,),
                )
                for execution in await cursor.fetchall():
                    await guard_comparison_conn(db, execution=execution, timestamp=now())
            await db.commit()
            return await self._project(db, await delegation_conn(db, delegation_id))

    async def supersede(self, *, team_id: str, agent_id: str, actor_user_id: str, keep_delegation_id: str) -> tuple[str, ...]:
        """Stop the Owner's other open checks that the check-after-edit setting started on this Agent.

        Checks the Owner authorized by hand are never touched. Used requests stay counted.
        """
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            cursor = await db.execute(
                """SELECT id FROM coach_ci_delegation WHERE team_id=? AND agent_id=? AND owner_user_id=? AND id!=?
                   AND state IN ('requested','authorized') AND request_key LIKE ?""",
                (team_id, agent_id, actor_user_id, keep_delegation_id, f"%:{STANDING_REQUEST_PREFIX}%"),
            )
            stopped = tuple(str(row["id"]) for row in await cursor.fetchall())
            await db.rollback()
        for delegation_id in stopped:
            await self.revoke(team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, delegation_id=delegation_id)
        return stopped

    async def _owned(self, db, team_id, agent_id, actor_user_id, delegation_id):
        await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
        row = await delegation_conn(db, delegation_id)
        if (row["team_id"], row["agent_id"], row["owner_user_id"]) != (team_id, agent_id, actor_user_id):
            raise CoachCIError("coach_ci_not_found")
        return row

    async def _project(self, db, row):
        admission = "consent_required" if row["state"] == "requested" else row["stop_reason"]
        if row["state"] == "requested":
            try:
                if digest(await context_conn(db, row)) != row["context_hash"]:
                    admission = "coach_ci_context_changed"
            except (RuntimeError, ValueError) as exc:
                admission = getattr(exc, "code", "coach_ci_scope_unavailable")
        elif row["state"] == "authorized":
            try:
                await require_active_conn(db, row["id"], preparing=True)
                admission = "current_db_authority"
            except (RuntimeError, ValueError) as exc:
                admission = getattr(exc, "code", "coach_ci_scope_unavailable")
        cursor = await db.execute(
            "SELECT state,count(*) AS count,coalesce(sum(tokens_input),0) AS tokens_input,coalesce(sum(tokens_output),0) AS tokens_output FROM coach_ci_invocation WHERE delegation_id=? GROUP BY state",
            (row["id"],),
        )
        usage = [dict(item) for item in await cursor.fetchall()]
        cursor = await db.execute(
            "SELECT id,status,run_kind,plan_id,failure_code,row_version,idempotency_hash AS operation_id FROM agent_eval_comparison_execution WHERE coach_delegation_id=? ORDER BY requested_at,id",
            (row["id"],),
        )
        executions = [dict(item) for item in await cursor.fetchall()]
        cursor = await db.execute(
            "SELECT c.row_version AS candidate_row_version,ctl.row_version AS control_row_version FROM team_subagent_release_candidate c JOIN agent_eval_candidate_control ctl ON ctl.candidate_id=c.id WHERE c.id=?",
            (row["candidate_id"],),
        )
        current_candidate = await cursor.fetchone()
        insights: dict[str, Any] = {}
        from bowhead_core.agent_eval_insights import coach_estimate_conn, execution_progress_conn

        insights["estimate"] = await coach_estimate_conn(db, row, has_execution=bool(executions))
        for execution in executions:
            try:
                execution["progress"] = await execution_progress_conn(db, execution["id"], include_estimate=False)
            except Exception:  # noqa: BLE001 — Progress is optional; the projection must still load.
                execution.pop("progress", None)
        return {
            "delegation_id": row["id"],
            "agent_id": row["agent_id"],
            "workspace_id": row["workspace_id"],
            "source_session_id": row["source_session_id"],
            "source_turn_id": row["source_turn_id"],
            "proposal_id": row["proposal_id"],
            "state": row["state"],
            "row_version": row["row_version"],
            "expires_at": row["expires_at"],
            "stop_reason": row["stop_reason"],
            "context_hash": row["context_hash"],
            "context": json.loads(row["context_json"]),
            "budget": {key: row[key] for key in ("max_provider_calls", "max_comparisons", "duration_seconds")},
            "usage": usage,
            "remaining_provider_calls": max(0, row["max_provider_calls"] - sum(item["count"] for item in usage)),
            "preparation": json.loads(row["preparation_json"]) if row["preparation_json"] else None,
            "current_candidate": dict(current_candidate) if current_candidate else None,
            "executions": executions,
            "origin_kind": "internal_coach",
            "current_admission": admission,
            "runtime_admission_checked": False,
            "provider_currency_cost": "unknown",
            "release_authorized": False,
            **insights,
        }


async def stop_delegation_conn(db: aiosqlite.Connection, row: Any, *, reason: str, state: str) -> None:
    if row["state"] in {"requested", "authorized"}:
        await db.execute(
            "UPDATE coach_ci_delegation SET state=?,stopped_at=?,stop_reason=?,row_version=row_version+1 WHERE id=? AND state IN ('requested','authorized')",
            (state, now(), reason, row["id"]),
        )


async def check_prepare_conn(
    db: aiosqlite.Connection,
    delegation_id: str,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    working_copy_id: str,
    working_copy_version: int,
    policy_revision_id: str | None,
    workspace_id: str,
) -> aiosqlite.Row:
    row = await require_active_conn(db, delegation_id, preparing=True)
    context = json.loads(row["context_json"])
    expected_policy = context["policy"]["id"] if context["policy"] else None
    if (
        row["team_id"],
        row["agent_id"],
        row["owner_user_id"],
        row["workspace_id"],
        context["working_copy_id"],
        context["working_copy_version"],
        expected_policy,
    ) != (team_id, agent_id, actor_user_id, workspace_id, working_copy_id, working_copy_version, policy_revision_id):
        raise CoachCIError("coach_ci_preparation_scope_mismatch")
    return row


async def check_comparison_conn(
    db: aiosqlite.Connection, delegation_id: str, *, team_id: str, agent_id: str, actor_user_id: str, candidate_id: str, plan_id: str, new: bool
) -> aiosqlite.Row:
    row = await require_active_conn(db, delegation_id)
    if (row["team_id"], row["agent_id"], row["owner_user_id"], row["candidate_id"], row["plan_id"]) != (
        team_id,
        agent_id,
        actor_user_id,
        candidate_id,
        plan_id,
    ):
        raise CoachCIError("coach_ci_comparison_scope_mismatch")
    if new:
        cursor = await db.execute("SELECT count(*) FROM agent_eval_comparison_execution WHERE coach_delegation_id=?", (delegation_id,))
        if int((await cursor.fetchone())[0]) >= row["max_comparisons"]:
            raise CoachCIError("coach_ci_comparison_budget_exhausted")
    return row
