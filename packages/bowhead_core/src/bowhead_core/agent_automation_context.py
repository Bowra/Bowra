"""Snapshot-based CI context, immutable preparation provenance and exact reads."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_plan_store import _workspace_scope
from bowhead_core.agent_eval_policy_store import AgentEvalPolicyError, AgentEvalPolicyStore, _current_row
from bowhead_core.agent_version_automation import AgentVersionAutomationError, AgentVersionAutomationStore


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def operation_key_hash(server_key: str) -> str:
    return "sha256:" + hashlib.sha256(server_key.encode()).hexdigest()


def comparison_intent_hash(
    *,
    team_id: str,
    agent_id: str,
    authority: AutomationAuthority,
    candidate_id: str,
    candidate_hash: str,
    candidate_row_version: int,
    plan_id: str,
    plan_hash: str,
    expected_control_row_version: int,
    run_kind: str = "release",
    selected_case_revision_ids: tuple[str, ...] = (),
) -> str:
    return digest(
        {
            "schema_version": 1,
            "team_id": team_id,
            "agent_id": agent_id,
            "principal_id": authority.principal_id,
            "grant_id": authority.grant_id,
            "repository": authority.repository,
            "ref": authority.ref,
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hash,
            "candidate_row_version": candidate_row_version,
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "expected_control_row_version": expected_control_row_version,
            "run_kind": run_kind,
            "selected_case_revision_ids": sorted(selected_case_revision_ids),
        }
    )


async def comparison_origin_conn(
    db: aiosqlite.Connection, *, execution: aiosqlite.Row, authority: AutomationAuthority, scope: str, require_executable_space: bool = False
) -> dict[str, Any] | None:
    team_id, agent_id = str(execution["team_id"]), str(execution["agent_id"])
    grant = await authority.require_conn(db, team_id=team_id, agent_id=agent_id, scope=scope)
    if (
        execution["actor_kind"] != "automation"
        or execution["actor_ref"] != authority.principal_id
        or execution["automation_grant_id"] != authority.grant_id
    ):
        raise AgentVersionAutomationError("agent_eval_comparison_not_found")
    context = json.loads(execution["automation_context_json"]) if execution["automation_context_json"] is not None else None
    if context is not None and (context.get("repository"), context.get("ref"), context.get("effective_owner_user_id")) != (
        authority.repository,
        authority.ref,
        authority.effective_owner_user_id,
    ):
        raise AgentVersionAutomationError("agent_automation_origin_mismatch")
    async with db.execute(
        "SELECT execution_workspace_id,execution_workspace_scope_hash FROM agent_eval_plan WHERE id=? AND team_id=? AND agent_id=?",
        (execution["plan_id"], team_id, agent_id),
    ) as cursor:
        plan = await cursor.fetchone()
    if plan is None or (grant["evaluation_workspace_id"] is not None and grant["evaluation_workspace_id"] != plan["execution_workspace_id"]):
        raise AgentVersionAutomationError("agent_automation_workspace_mismatch")
    if context is not None and context.get("evaluation_workspace_id") != grant["evaluation_workspace_id"]:
        raise AgentVersionAutomationError("agent_automation_origin_mismatch")
    if require_executable_space:
        if context is None:
            raise AgentVersionAutomationError("agent_automation_execution_context_missing")
        current = await require_workspace_conn(
            db, team_id=team_id, owner_id=authority.effective_owner_user_id, workspace_id=str(plan["execution_workspace_id"])
        )
        if digest(current) != plan["execution_workspace_scope_hash"]:
            raise AgentVersionAutomationError("agent_eval_plan_stale")
    return context


@dataclass(frozen=True, slots=True)
class AutomationAuthority:
    principal_id: str
    grant_id: str
    repository: str
    ref: str
    effective_owner_user_id: str

    async def require_conn(self, db: aiosqlite.Connection, *, team_id: str, agent_id: str, scope: str) -> aiosqlite.Row:
        grant = await AgentVersionAutomationStore("").authorize_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            grant_id=self.grant_id,
            principal_id=self.principal_id,
            required_scope=scope,
            repository=self.repository,
            ref=self.ref,
        )
        if grant["owner_user_id"] != self.effective_owner_user_id:
            raise AgentVersionAutomationError("agent_automation_grant_denied")
        return grant


@dataclass(frozen=True, slots=True)
class AutomationPreparation:
    authority: AutomationAuthority
    proposal_id: str
    expected_context_hash: str | None
    policy_expectation_provided: bool


async def require_workspace_conn(db: aiosqlite.Connection, *, team_id: str, owner_id: str, workspace_id: str) -> dict[str, Any]:
    scope = await _workspace_scope(db, team_id=team_id, workspace_id=workspace_id, actor_user_id=owner_id)
    if scope is None:
        raise AgentVersionAutomationError("agent_automation_workspace_unavailable")
    return scope


async def require_protocol_schema_conn(db: aiosqlite.Connection) -> None:
    """Startup check: the database carries the protocol v2 columns and triggers."""
    required = {
        "agent_version_automation_grant": {"evaluation_workspace_id"},
        "agent_eval_comparison_execution": {"client_intent_hash", "automation_context_json"},
        "agent_version_automation_preparation": {"operation_key_hash", "initial_plan_id", "context_hash"},
    }
    for table, fields in required.items():
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        if not fields <= columns:
            raise RuntimeError(f"agent automation schema is missing {sorted(fields - columns)} on {table}; run the database upgrade")

    async with db.execute("SELECT name FROM sqlite_master WHERE type='trigger'") as cursor:
        triggers = {row[0] for row in await cursor.fetchall()}
    if (
        not {
            "trg_agent_comparison_origin_insert",
            "trg_agent_comparison_origin_immutable",
            "trg_agent_automation_preparation_insert",
            "trg_agent_automation_workspace_immutable",
        }
        <= triggers
    ):
        raise RuntimeError("agent automation schema is missing protocol triggers; run the database upgrade")


async def context_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, authority: AutomationAuthority, required_scope: str = "gate:read"
) -> dict[str, Any]:
    """Compile identities only; never create a working copy, Policy or suite."""
    grant = await authority.require_conn(db, team_id=team_id, agent_id=agent_id, scope=required_scope)
    async with db.execute(
        "SELECT id,base_release_id,row_version FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?", (team_id, agent_id)
    ) as cursor:
        working = await cursor.fetchone()
    async with db.execute("SELECT live_release_id FROM team_subagent WHERE team_id=? AND id=?", (team_id, agent_id)) as cursor:
        agent = await cursor.fetchone()
    result: dict[str, Any] = {
        "schema_version": 1,
        "agent_id": agent_id,
        "principal_id": authority.principal_id,
        "working_copy": None
        if working is None
        else {"working_copy_id": working["id"], "working_copy_row_version": working["row_version"], "base_release_id": working["base_release_id"]},
        "live_release_id": agent["live_release_id"],
        "evaluation_context": None,
        "preparation_blockers": [],
        "automation_protocol_version": 2,
    }
    if working is None:
        result["preparation_blockers"].append("agent_working_copy_missing")
    workspace_id = grant["evaluation_workspace_id"]
    if not workspace_id:
        result["preparation_blockers"].append("agent_automation_workspace_required")
        return result
    try:
        scope = await require_workspace_conn(db, team_id=team_id, owner_id=authority.effective_owner_user_id, workspace_id=str(workspace_id))
    except AgentVersionAutomationError as exc:
        result["preparation_blockers"].append(exc.code)
        return result
    policy = await _current_row(db, team_id=team_id, agent_id=agent_id)
    if policy is not None:
        policy_identity = {"resolution": "existing", "revision_id": policy["id"], "policy_hash": policy["policy_hash"]}
    else:
        try:
            preview = await AgentEvalPolicyStore("").default_preview_conn(
                db, team_id=team_id, agent_id=agent_id, actor_user_id=authority.effective_owner_user_id
            )
        except AgentEvalPolicyError as exc:
            result["preparation_blockers"].append(exc.code)
            return result
        policy_identity = {"resolution": "create_default", "revision_id": None, "policy_hash": None, "default_policy_hash": digest(preview)}
    async with db.execute(
        "SELECT scopes_json,expires_at FROM team_openapi_keys WHERE id=? AND team_id=?", (authority.principal_id, team_id)
    ) as cursor:
        principal = await cursor.fetchone()
    envelope = {
        "schema_version": 1,
        "compiler_version": 1,
        "team_id": team_id,
        "agent_id": agent_id,
        "authority": asdict(authority),
        "grant_scopes": sorted(json.loads(grant["scopes_json"])),
        "repository_rules_hash": grant["repository_rules_hash"],
        "grant_expires_at": grant["expires_at"],
        "principal_scopes": sorted(json.loads(principal["scopes_json"])),
        "principal_expires_at": principal["expires_at"],
        "working_copy": result["working_copy"],
        "live_release_id": result["live_release_id"],
        "space": scope,
        "policy": policy_identity,
    }
    result["evaluation_context"] = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "workspace_revision": scope["workspace_revision"],
        "access_policy_epoch": scope["policy_epoch"],
        "policy": policy_identity,
        "context_hash": digest(envelope) if working is not None else None,
    }
    return result


async def accepted_proposal_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, authority: AutomationAuthority, proposal_id: str
) -> aiosqlite.Row:
    async with db.execute(
        """SELECT p.status,p.row_version,p.patch_hash,p.provenance_json,p.base_release_id,
                  d.result_working_copy_id,d.result_working_copy_version,d.selected_group_ids_json
           FROM agent_change_proposal p LEFT JOIN agent_change_proposal_decision d
             ON d.proposal_id=p.id AND d.decision_kind='patch_accept'
           WHERE p.id=? AND p.team_id=? AND p.agent_id=? AND p.source_type='git'
             AND p.created_by_id=? AND json_extract(p.provenance_json,'$.automation_grant_id')=?
           ORDER BY d.created_at DESC,d.id DESC LIMIT 1""",
        (proposal_id, team_id, agent_id, authority.principal_id, authority.grant_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentVersionAutomationError("agent_change_proposal_not_found")
    return row


async def check_prepare_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    preparation: AutomationPreparation,
    working_copy_id: str,
    working_copy_version: int,
    workspace_id: str,
    policy_revision_id: str | None,
) -> aiosqlite.Row:
    authority = preparation.authority
    grant = await authority.require_conn(db, team_id=team_id, agent_id=agent_id, scope="candidate:prepare")
    proposal = await accepted_proposal_conn(db, team_id=team_id, agent_id=agent_id, authority=authority, proposal_id=preparation.proposal_id)
    if (
        proposal["status"] != "accepted"
        or proposal["result_working_copy_id"] != working_copy_id
        or proposal["result_working_copy_version"] != working_copy_version
    ):
        raise AgentVersionAutomationError("agent_automation_proposal_not_accepted")
    if grant["evaluation_workspace_id"] is None:
        # Grants created before protocol v2 keep proposal and gate access; preparation needs a replacement grant.
        raise AgentVersionAutomationError("agent_automation_workspace_required")
    if workspace_id != grant["evaluation_workspace_id"]:
        raise AgentVersionAutomationError("agent_automation_workspace_mismatch")
    if not preparation.expected_context_hash or not preparation.policy_expectation_provided:
        raise AgentVersionAutomationError("agent_automation_context_required")
    origin = json.loads(proposal["provenance_json"])
    if (origin.get("repository"), origin.get("ref")) != (authority.repository, authority.ref):
        raise AgentVersionAutomationError("agent_automation_origin_mismatch")
    current = await context_conn(db, team_id=team_id, agent_id=agent_id, authority=authority, required_scope="candidate:prepare")
    context = current["evaluation_context"]
    if current["preparation_blockers"] or not context:
        raise AgentVersionAutomationError(
            "agent_automation_context_conflict",
            details={"preparation_blockers": ",".join(current["preparation_blockers"])} if current["preparation_blockers"] else None,
        )
    # The context hash covers the Policy identity, so the Policy check must run first: otherwise a stale
    # Policy always surfaces as a context conflict without the documented `current_policy_revision_id`.
    if context["policy"]["revision_id"] != policy_revision_id:
        current_policy = context["policy"]["revision_id"]
        raise AgentVersionAutomationError(
            "agent_eval_policy_version_conflict",
            details={"current_policy_revision_id": current_policy} if current_policy else None,
        )
    if context["context_hash"] != preparation.expected_context_hash:
        raise AgentVersionAutomationError("agent_automation_context_conflict")
    async with db.execute(
        "SELECT base_release_id FROM team_subagent_working_copy WHERE id=? AND team_id=? AND subagent_id=?", (working_copy_id, team_id, agent_id)
    ) as cursor:
        working = await cursor.fetchone()
    if working is None or working["base_release_id"] != proposal["base_release_id"]:
        raise AgentVersionAutomationError("agent_working_copy_base_stale")
    return grant


async def preparation_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, authority: AutomationAuthority, key_hash: str
) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM agent_version_automation_preparation WHERE team_id=? AND agent_id=? AND principal_id=? AND operation_key_hash=?",
        (team_id, agent_id, authority.principal_id, key_hash),
    ) as cursor:
        return await cursor.fetchone()


class AgentAutomationContextStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def read_comparison_operation(
        self, *, team_id: str, agent_id: str, candidate_id: str, authority: AutomationAuthority, operation_key_hash: str
    ) -> dict[str, Any]:
        _validate_operation_hash(operation_key_hash)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                await authority.require_conn(db, team_id=team_id, agent_id=agent_id, scope="gate:read")
                async with db.execute(
                    "SELECT * FROM agent_eval_comparison_execution WHERE team_id=? AND agent_id=? AND actor_kind='automation' AND actor_ref=? AND idempotency_hash=?",
                    (team_id, agent_id, authority.principal_id, operation_key_hash),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    return {"schema_version": 1, "execution": None}
                if row["candidate_id"] != candidate_id:
                    raise AgentVersionAutomationError("agent_eval_comparison_not_found")
                context = await comparison_origin_conn(db, execution=row, authority=authority, scope="gate:read")
                return {
                    "schema_version": 1,
                    "execution": _execution_identity(row),
                    "client_intent_verified": row["client_intent_hash"] is not None,
                    "repository_context_verified": context is not None,
                }
            finally:
                await db.rollback()

    async def read_automation_state(
        self,
        *,
        team_id: str,
        agent_id: str,
        proposal_id: str,
        authority: AutomationAuthority,
        operation_key_hash: str,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        _validate_operation_hash(operation_key_hash)
        if not 0 <= offset <= 100000 or not 1 <= limit <= 50:
            raise ValueError("Automation recovery page is invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                grant = await authority.require_conn(db, team_id=team_id, agent_id=agent_id, scope="gate:read")
                proposal = await accepted_proposal_conn(db, team_id=team_id, agent_id=agent_id, authority=authority, proposal_id=proposal_id)
                origin = json.loads(proposal["provenance_json"])
                if (origin.get("repository"), origin.get("ref")) != (authority.repository, authority.ref):
                    raise AgentVersionAutomationError("agent_automation_origin_mismatch")
                result: dict[str, Any] = {
                    "schema_version": 1,
                    "agent_id": agent_id,
                    "proposal": {
                        "proposal_id": proposal_id,
                        "status": proposal["status"],
                        "row_version": proposal["row_version"],
                        "patch_hash": proposal["patch_hash"],
                        "selected_group_ids": json.loads(proposal["selected_group_ids_json"] or "[]"),
                        "accepted_working_copy_id": proposal["result_working_copy_id"],
                        "accepted_working_copy_row_version": proposal["result_working_copy_version"],
                    },
                    "preparation": None,
                    "candidate": None,
                    "executions": [],
                    "next_offset": None,
                    "preparation_identity_verified": False,
                }
                prepared = await preparation_conn(db, team_id=team_id, agent_id=agent_id, authority=authority, key_hash=operation_key_hash)
                if prepared is None:
                    return result
                if prepared["grant_id"] != authority.grant_id or prepared["proposal_id"] != proposal_id:
                    raise AgentVersionAutomationError("agent_automation_preparation_not_found")
                result["preparation_identity_verified"] = True
                result["preparation"] = {
                    key: prepared[key]
                    for key in (
                        "candidate_id",
                        "initial_plan_id",
                        "initial_plan_hash",
                        "context_hash",
                        "protocol_version",
                        "operation_key_hash",
                        "created_at",
                    )
                }
                async with db.execute(
                    """SELECT c.id,c.state,c.row_version,c.candidate_hash,c.activated_release_id,
                    ctl.active_plan_id,ctl.row_version AS control_row_version,ctl.readiness,ctl.reason_codes_json,
                    ctl.active_execution_id,ctl.active_snapshot_id,p.plan_hash,p.execution_workspace_id,p.execution_workspace_scope_hash,snap.execution_id AS snapshot_execution_id
                    FROM team_subagent_release_candidate c JOIN agent_eval_candidate_control ctl ON ctl.candidate_id=c.id
                    JOIN agent_eval_plan p ON p.id=ctl.active_plan_id
                    LEFT JOIN agent_eval_snapshot snap ON snap.id=ctl.active_snapshot_id
                    WHERE c.id=? AND c.team_id=? AND c.subagent_id=?""",
                    (prepared["candidate_id"], team_id, agent_id),
                ) as cursor:
                    candidate = await cursor.fetchone()
                if candidate is None:
                    raise AgentVersionAutomationError("agent_release_candidate_not_found")
                if grant["evaluation_workspace_id"] is not None and grant["evaluation_workspace_id"] != candidate["execution_workspace_id"]:
                    raise AgentVersionAutomationError("agent_automation_workspace_mismatch")
                result["candidate"] = {
                    "candidate_id": candidate["id"],
                    "state": candidate["state"],
                    "candidate_row_version": candidate["row_version"],
                    "candidate_hash": candidate["candidate_hash"],
                    "control_row_version": candidate["control_row_version"],
                    "plan_id": candidate["active_plan_id"],
                    "plan_hash": candidate["plan_hash"],
                    "readiness": candidate["readiness"],
                    "reason_codes": json.loads(candidate["reason_codes_json"]),
                    "active_execution_id": candidate["active_execution_id"],
                    "snapshot_id": candidate["active_snapshot_id"],
                    "snapshot_execution_id": candidate["snapshot_execution_id"],
                    "release_id": candidate["activated_release_id"],
                    "gate_applicable": candidate["state"] in {"prepared", "pending", "approved"},
                }
                current_scope = await _workspace_scope(
                    db, team_id=team_id, workspace_id=candidate["execution_workspace_id"], actor_user_id=authority.effective_owner_user_id
                )
                if current_scope is None or digest(current_scope) != candidate["execution_workspace_scope_hash"]:
                    result["candidate"]["gate_applicable"] = False
                    result["candidate"]["reason_codes"] = [*result["candidate"]["reason_codes"], "agent_eval_workspace_scope_changed"]
                async with db.execute(
                    """SELECT * FROM agent_eval_comparison_execution WHERE team_id=? AND agent_id=?
                    AND candidate_id=? AND actor_kind='automation' AND actor_ref=? AND automation_grant_id=?
                    ORDER BY requested_at,id LIMIT ? OFFSET ?""",
                    (team_id, agent_id, prepared["candidate_id"], authority.principal_id, authority.grant_id, limit + 1, offset),
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows[:limit]:
                    # A preparation's repository identity does not prove an execution's identity.
                    context = json.loads(row["automation_context_json"] or "null")
                    if context and (context.get("repository"), context.get("ref")) != (authority.repository, authority.ref):
                        continue
                    item = _execution_identity(row)
                    item["repository_context_verified"] = context is not None
                    item["client_intent_verified"] = row["client_intent_hash"] is not None
                    result["executions"].append(item)
                if len(rows) > limit:
                    result["next_offset"] = offset + limit
                return result
            finally:
                await db.rollback()

    async def read_context(self, *, team_id: str, agent_id: str, authority: AutomationAuthority) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                return await context_conn(db, team_id=team_id, agent_id=agent_id, authority=authority)
            finally:
                await db.rollback()


def _validate_operation_hash(value: str) -> None:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("Automation operation key hash is invalid")


def _execution_identity(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "id",
            "candidate_id",
            "plan_id",
            "run_kind",
            "status",
            "row_version",
            "failure_code",
            "requested_at",
            "finished_at",
            "client_intent_hash",
        )
    }
