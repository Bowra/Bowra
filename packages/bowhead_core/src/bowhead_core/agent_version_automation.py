"""Least-privilege automation grants for external pipelines (team OpenAPI keys)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.audit_actions import AuditAction
from bowhead_core.user_store import UserStore

AUTOMATION_SCOPES = frozenset({"proposal:create", "candidate:prepare", "eval:run", "gate:read"})
# Agent version webhooks were removed (design agent-release-beginner-experience §10). Grants stored
# before that may still list this scope in scopes_json; it is accepted on read and grants nothing.
RETIRED_AUTOMATION_SCOPES = frozenset({"webhook:receive"})


class AgentVersionAutomationError(RuntimeError):
    def __init__(self, code: str, *, details: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        # Safe, caller-actionable detail (for example which grant condition failed).
        self.details = dict(details or {})


class AgentVersionAutomationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def create_grant(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        principal_id: str,
        scopes: Sequence[str],
        repositories: Sequence[str],
        refs: Sequence[str],
        expires_at: str,
        evaluation_workspace_id: str | None = None,
        replaces_grant_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a grant; with `replaces_grant_id`, atomically revoke the grant it replaces.

        The replaced grant must belong to the same team and Agent and must not be revoked already;
        anything else is `agent_automation_grant_not_found`. Like a manual revoke, the current Owner
        may replace any grant of the Agent, including one a previous Owner created (which no longer
        authorizes, reason `owner_changed`). Creation, revocation and the audit entry commit together.
        """
        normalized_scopes = tuple(sorted(set(scopes)))
        if not normalized_scopes or set(normalized_scopes) - AUTOMATION_SCOPES:
            raise AgentVersionAutomationError("agent_automation_scope_invalid")
        rules = _repository_rules(repositories, refs)
        expiry = _parse_future(expires_at)
        principal_id = principal_id.strip()
        if not principal_id or len(principal_id) > 200:
            raise AgentVersionAutomationError("agent_automation_principal_invalid")
        timestamp = _now()
        grant_id = f"avag_{uuid4().hex}"
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                await _require_transport_principal_conn(
                    db,
                    team_id=team_id,
                    principal_id=principal_id,
                    write_required=bool(set(normalized_scopes) & {"proposal:create", "candidate:prepare", "eval:run"}),
                )
                from bowhead_core.agent_automation_context import require_workspace_conn

                if evaluation_workspace_id is not None:
                    await require_workspace_conn(db, team_id=team_id, owner_id=actor_user_id, workspace_id=evaluation_workspace_id)
                elif set(normalized_scopes) & {"candidate:prepare", "eval:run"}:
                    raise AgentVersionAutomationError("agent_automation_workspace_required")
                if replaces_grant_id is not None:
                    replaced = await _grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=replaces_grant_id)
                    if replaced is None or replaced["revoked_at"] is not None:
                        raise AgentVersionAutomationError("agent_automation_grant_not_found")
                await db.execute(
                    """
                    INSERT INTO agent_version_automation_grant(
                      id,team_id,agent_id,owner_user_id,principal_id,scopes_json,
                      repository_rules_json,repository_rules_hash,expires_at,row_version,
                      created_at,updated_at,evaluation_workspace_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)
                    """,
                    (
                        grant_id,
                        team_id,
                        agent_id,
                        actor_user_id,
                        principal_id,
                        _canonical(normalized_scopes),
                        _canonical(rules),
                        _hash(_canonical(rules)),
                        expiry,
                        timestamp,
                        timestamp,
                        evaluation_workspace_id,
                    ),
                )
                if replaces_grant_id is not None:
                    await _revoke_grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=replaces_grant_id, timestamp=timestamp)
                    await UserStore(self.db_path).write_admin_audit_log(
                        actor_user_id=actor_user_id,
                        team_id=team_id,
                        action=AuditAction.AGENT_AUTOMATION_GRANT_REPLACED,
                        target_kind="agent_version_automation_grant",
                        target_id=grant_id,
                        target_team_id=team_id,
                        scope="user",
                        payload={"agent_id": agent_id, "grant_id": grant_id, "replaced_grant_id": replaces_grant_id},
                        conn=db,
                    )
                row = await _grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=grant_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        assert row is not None
        return _grant_view(row)

    async def list_grants(self, *, team_id: str, agent_id: str, actor_user_id: str) -> list[dict[str, Any]]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            async with db.execute(
                "SELECT * FROM agent_version_automation_grant WHERE team_id=? AND agent_id=? ORDER BY created_at DESC",
                (team_id, agent_id),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_grant_view(row) for row in rows]

    async def list_transport_principals(self, *, team_id: str, agent_id: str, actor_user_id: str) -> list[dict[str, Any]]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            async with db.execute(
                """SELECT id,name,token_prefix,scopes_json,expires_at
                FROM team_openapi_keys
                WHERE team_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)
                ORDER BY name,id""",
                (team_id, _now()),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "token_prefix": str(row["token_prefix"]),
                "scopes": sorted({"agent_versions:read", "agent_versions:write"} & set(json.loads(str(row["scopes_json"])))),
                "expires_at": str(row["expires_at"]) if row["expires_at"] else None,
            }
            for row in rows
            if {"agent_versions:read", "agent_versions:write"} & set(json.loads(str(row["scopes_json"])))
        ]

    async def revoke_grant(self, *, team_id: str, agent_id: str, grant_id: str, actor_user_id: str, expected_row_version: int) -> dict[str, Any]:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                # Deliberately NOT matched on `expected_row_version`. Revocation is monotonic and
                # de-escalating, and every field an Owner could race on (scopes, repo rules, expiry,
                # principal) is immutable at the DB. The only writes that move row_version are this
                # revoke and `authorize`'s last_used_at bookkeeping — so a CAS here would let the very
                # credential being revoked starve the revocation by calling the API in a loop.
                # `expected_row_version` is still accepted and reported, it just cannot block the revoke.
                await _revoke_grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=grant_id, timestamp=timestamp)
                row = await _grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=grant_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        assert row is not None
        return _grant_view(row)

    async def authorize(
        self,
        *,
        team_id: str,
        agent_id: str,
        grant_id: str,
        principal_id: str,
        required_scope: str,
        repository: str,
        ref: str,
    ) -> dict[str, Any]:
        if required_scope not in AUTOMATION_SCOPES:
            raise AgentVersionAutomationError("agent_automation_grant_denied")
        _require_well_formed_context(repository, ref)
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self.authorize_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    grant_id=grant_id,
                    principal_id=principal_id,
                    required_scope=required_scope,
                    repository=repository,
                    ref=ref,
                )
                updated = await db.execute(
                    """UPDATE agent_version_automation_grant SET last_used_at=?,updated_at=?,row_version=row_version+1
                    WHERE id=? AND row_version=?""",
                    (now, now, grant_id, int(row["row_version"])),
                )
                if updated.rowcount != 1:
                    raise AgentVersionAutomationError("agent_automation_grant_conflict")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "kind": "automation",
            "principal_id": principal_id,
            "effective_owner_user_id": str(row["owner_user_id"]),
            "grant_id": grant_id,
        }

    async def authorize_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        grant_id: str,
        principal_id: str,
        required_scope: str,
        repository: str,
        ref: str,
    ) -> aiosqlite.Row:
        """Revalidate original authority in the caller's snapshot or mutation transaction."""
        if required_scope not in AUTOMATION_SCOPES:
            raise AgentVersionAutomationError("agent_automation_grant_denied")
        _require_well_formed_context(repository, ref)
        now = _now()
        row = await _grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=grant_id)
        if row is None or str(row["principal_id"]) != principal_id:
            raise AgentVersionAutomationError("agent_automation_grant_denied")
        async with db.execute(
            """SELECT COALESCE(owner_user_id,created_by),lifecycle_state
            FROM team_subagent WHERE team_id=? AND id=?""",
            (team_id, agent_id),
        ) as cursor:
            agent = await cursor.fetchone()
        async with db.execute(
            """SELECT scopes_json FROM team_openapi_keys
            WHERE id=? AND team_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)""",
            (principal_id, team_id, now),
        ) as cursor:
            principal = await cursor.fetchone()
        async with db.execute(
            f"""SELECT {_OWNER_STILL_ACTIVE_EXISTS}
            FROM agent_version_automation_grant grant WHERE grant.id=?""",
            (grant_id,),
        ) as cursor:
            owner_row = await cursor.fetchone()
        owner_still_active = bool(owner_row[0]) if owner_row is not None else False
        scopes = set(json.loads(str(row["scopes_json"])))
        principal_scopes = set(json.loads(str(principal["scopes_json"]))) if principal is not None else set()
        rules = json.loads(str(row["repository_rules_json"]))
        transport_scope = (
            "agent_versions:write"
            if required_scope
            in {
                "proposal:create",
                "candidate:prepare",
                "eval:run",
            }
            else "agent_versions:read"
        )
        transport_allowed = transport_scope in principal_scopes or (
            transport_scope == "agent_versions:read" and "agent_versions:write" in principal_scopes
        )
        # The caller has already proven it holds this grant (id + principal), so naming the failed
        # condition leaks nothing and tells CI what to fix. Order: what the integrator can change last.
        denial: dict[str, str] | None = None
        if agent is None or str(agent[1]) != "active":
            denial = {"reason": "agent_inactive"}
        elif not owner_still_active or str(agent[0]) != str(row["owner_user_id"]):
            denial = {"reason": "owner_changed"}
        elif row["revoked_at"] is not None:
            denial = {"reason": "grant_revoked"}
        elif _parse_time(str(row["expires_at"])) <= datetime.now(UTC):
            denial = {"reason": "grant_expired"}
        elif not transport_allowed:
            denial = {"reason": "key_scope_missing", "required_key_scope": transport_scope}
        elif required_scope not in scopes:
            denial = {"reason": "action_not_granted", "required_action": required_scope}
        elif not _matches(repository, rules["repositories"]):
            denial = {"reason": "repository_not_allowed"}
        elif not _matches(ref, rules["refs"]):
            denial = {"reason": "ref_not_allowed"}
        if denial is not None:
            raise AgentVersionAutomationError("agent_automation_grant_denied", details=denial)
        return row

    async def list_dependency_policies(self, *, team_id: str, agent_id: str, actor_user_id: str) -> list[dict[str, Any]]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            async with db.execute(
                """SELECT dependency_kind,dependency_ref,policy,row_version,updated_at
                FROM agent_dependency_update_policy WHERE team_id=? AND agent_id=?
                ORDER BY dependency_kind,dependency_ref""",
                (team_id, agent_id),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def put_dependency_policy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        dependency_kind: str,
        dependency_ref: str,
        policy: str,
        expected_row_version: int,
    ) -> dict[str, Any]:
        dependency_ref = dependency_ref.strip()
        if (
            dependency_kind not in {"skill", "tool", "model", "fork"}
            or policy not in {"pinned", "compatible", "notify_only"}
            or not dependency_ref
            or len(dependency_ref) > 240
        ):
            raise AgentVersionAutomationError("agent_dependency_policy_invalid")
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                async with db.execute(
                    """SELECT row_version FROM agent_dependency_update_policy
                    WHERE team_id=? AND agent_id=? AND dependency_kind=? AND dependency_ref=?""",
                    (team_id, agent_id, dependency_kind, dependency_ref),
                ) as cursor:
                    current = await cursor.fetchone()
                if current is None:
                    if expected_row_version != 0:
                        raise AgentVersionAutomationError("agent_dependency_policy_conflict")
                    await db.execute(
                        """INSERT INTO agent_dependency_update_policy(
                        team_id,agent_id,dependency_kind,dependency_ref,policy,row_version,updated_by,updated_at)
                        VALUES(?,?,?,?,?,1,?,?)""",
                        (team_id, agent_id, dependency_kind, dependency_ref, policy, actor_user_id, timestamp),
                    )
                    row_version = 1
                else:
                    if int(current[0]) != expected_row_version:
                        raise AgentVersionAutomationError("agent_dependency_policy_conflict")
                    await db.execute(
                        """UPDATE agent_dependency_update_policy SET policy=?,row_version=row_version+1,updated_by=?,updated_at=?
                        WHERE team_id=? AND agent_id=? AND dependency_kind=? AND dependency_ref=? AND row_version=?""",
                        (policy, actor_user_id, timestamp, team_id, agent_id, dependency_kind, dependency_ref, expected_row_version),
                    )
                    row_version = expected_row_version + 1
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "dependency_kind": dependency_kind,
            "dependency_ref": dependency_ref,
            "policy": policy,
            "row_version": row_version,
            "updated_at": timestamp,
        }


# One source of truth for "the grant's owner is still a live account holding active membership
# in a live team". `_require_owner_conn` gates the human path on exactly these three; every
# automation path re-checks the same three, otherwise a grant keeps authorizing after its owner
# is offboarded — the credential would outlive the person it was issued to. Correlated on the
# alias `grant`, so each call site must expose `agent_version_automation_grant` under that name.
_OWNER_STILL_ACTIVE_EXISTS = """EXISTS(
  SELECT 1 FROM teams team
  JOIN users owner ON owner.id=grant.owner_user_id AND owner.status='active'
  JOIN team_members member ON member.team_id=team.id AND member.user_id=owner.id AND member.status='active'
  WHERE team.id=grant.team_id AND team.status='active')"""


async def _require_owner_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str) -> None:
    async with db.execute(
        """SELECT 1 FROM team_subagent agent
        JOIN teams team ON team.id=agent.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.status='active'
        JOIN team_members member ON member.team_id=agent.team_id AND member.user_id=actor.id AND member.status='active'
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id""",
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentVersionAutomationError("agent_owner_required")


async def _require_transport_principal_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    principal_id: str,
    write_required: bool,
) -> None:
    async with db.execute(
        """SELECT scopes_json FROM team_openapi_keys
        WHERE id=? AND team_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)""",
        (principal_id, team_id, _now()),
    ) as cursor:
        row = await cursor.fetchone()
    scopes = set(json.loads(str(row["scopes_json"]))) if row is not None else set()
    allowed = "agent_versions:write" in scopes if write_required else bool({"agent_versions:read", "agent_versions:write"} & scopes)
    if not allowed:
        raise AgentVersionAutomationError("agent_automation_principal_invalid")


async def _revoke_grant_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, grant_id: str, timestamp: str) -> None:
    """Revoke a grant; shared by manual revoke and replacement."""
    result = await db.execute(
        """UPDATE agent_version_automation_grant
        SET revoked_at=?,row_version=row_version+1,updated_at=?
        WHERE id=? AND team_id=? AND agent_id=? AND revoked_at IS NULL""",
        (timestamp, timestamp, grant_id, team_id, agent_id),
    )
    if result.rowcount != 1:
        # Revoking twice is success, not an error — it is the state the caller asked for.
        # Only a grant that does not exist at all is a failure.
        if await _grant_conn(db, team_id=team_id, agent_id=agent_id, grant_id=grant_id) is None:
            raise AgentVersionAutomationError("agent_automation_grant_not_found")


async def _grant_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, grant_id: str) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM agent_version_automation_grant WHERE id=? AND team_id=? AND agent_id=?",
        (grant_id, team_id, agent_id),
    ) as cursor:
        return await cursor.fetchone()


async def _require_active_grant_owner_scope_conn(
    db: aiosqlite.Connection,
    *,
    grant: Mapping[str, Any],
    actor_user_id: str,
    required_scope: str,
) -> None:
    scopes = set(json.loads(str(grant["scopes_json"])))
    if (
        str(grant["owner_user_id"]) != actor_user_id
        or grant["revoked_at"] is not None
        or _parse_time(str(grant["expires_at"])) <= datetime.now(UTC)
        or required_scope not in scopes
    ):
        raise AgentVersionAutomationError("agent_automation_grant_denied")
    await _require_transport_principal_conn(
        db,
        team_id=str(grant["team_id"]),
        principal_id=str(grant["principal_id"]),
        write_required=False,
    )


def _grant_view(row: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    revoked_at = str(row["revoked_at"]) if row["revoked_at"] else None
    active = revoked_at is None and _parse_time(str(row["expires_at"])) > now
    # A retired scope stored before webhooks were removed is ignored, not reported.
    scopes = [scope for scope in json.loads(str(row["scopes_json"])) if scope not in RETIRED_AUTOMATION_SCOPES]
    return {
        "id": str(row["id"]),
        "agent_id": str(row["agent_id"]),
        "principal_id": str(row["principal_id"]),
        "evaluation_workspace_id": row["evaluation_workspace_id"],
        "scopes": scopes,
        # Grants created before protocol v2 hold evaluation scopes without a Space; only a replacement can use them.
        "needs_replacement": row["evaluation_workspace_id"] is None and bool(set(scopes) & {"candidate:prepare", "eval:run"}),
        "repository_rules": json.loads(str(row["repository_rules_json"])),
        "expires_at": str(row["expires_at"]),
        "revoked_at": revoked_at,
        "last_used_at": str(row["last_used_at"]) if row["last_used_at"] else None,
        "row_version": int(row["row_version"]),
        "status": "active" if active else ("revoked" if revoked_at else "expired"),
        "created_at": str(row["created_at"]),
    }


def _repository_rules(repositories: Sequence[str], refs: Sequence[str]) -> dict[str, list[str]]:
    repository_rules = sorted({value.strip() for value in repositories if value.strip()})
    ref_rules = sorted({value.strip() for value in refs if value.strip()})
    values = repository_rules + ref_rules
    if not repository_rules or not ref_rules or any(len(value) > 240 or ".." in value for value in values):
        raise AgentVersionAutomationError("agent_automation_repository_rules_invalid")
    return {"repositories": repository_rules, "refs": ref_rules}


def _require_well_formed_context(repository: str, ref: str) -> None:
    """Hold the caller's self-reported repository/ref to the same shape the rules must have.

    These two values are asserted by the caller — nothing proves the request really came from that
    repository at that ref, and nothing can without signed provenance from the forge (OIDC). They
    are therefore a provenance label for the audit trail and a guard against a pipeline pointing at
    the wrong branch by accident — not a security boundary. See the sprint review for why.

    What they must not be is *unreadable*: `_repository_rules` already rejects `..` when the Owner
    writes the rule, but the incoming value was only length-bounded, so a grant could be exercised
    with `acme/agent-x/../../../evil-org/backdoor` and that string is what the audit log kept. Apply
    the same shape rules on the way in, so what gets recorded means what it says.

    Deliberately NOT narrowing `*` to stop at `/`: `refs/heads/feature/foo` is a legal git ref and
    GitLab nests groups, so a pattern that spans separators is a real thing Owners need to write.
    """
    for value in (repository, ref):
        if not value or len(value) > 240 or ".." in value:
            raise AgentVersionAutomationError("agent_automation_context_invalid")
        if value != value.strip() or "//" in value or value.startswith("/") or value.endswith("/"):
            raise AgentVersionAutomationError("agent_automation_context_invalid")
        if any(char < " " or char == "\x7f" for char in value):
            raise AgentVersionAutomationError("agent_automation_context_invalid")


def _matches(value: str, patterns: Sequence[str]) -> bool:
    return bool(value) and any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _parse_future(value: str) -> str:
    parsed = _parse_time(value)
    if parsed <= datetime.now(UTC):
        raise AgentVersionAutomationError("agent_automation_expiry_invalid")
    return parsed.isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AgentVersionAutomationError("agent_automation_expiry_invalid") from exc
    if parsed.tzinfo is None:
        raise AgentVersionAutomationError("agent_automation_expiry_invalid")
    return parsed.astimezone(UTC)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
