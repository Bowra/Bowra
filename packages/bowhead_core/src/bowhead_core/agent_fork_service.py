"""M4-C same-team Agent Fork with exact lineage and dependency re-resolution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationValidation,
    AgentRevisionStore,
    DeterministicAgentPublicationValidator,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver
from bowhead_core.team_subagent_store import (
    TeamSubagentForkAdmissionError,
    TeamSubagentForkCommit,
    TeamSubagentNameConflictError,
    TeamSubagentOwnerInactiveError,
    TeamSubagentStore,
    agent_fork_source_snapshot_hash,
)
from bowhead_core.user_store import TeamSubagent, UserStore

AgentForkPolicyValue = Literal["not_allowed", "recipients", "team_members"]

_POLICIES = frozenset({"not_allowed", "recipients", "team_members"})
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{8,200}$")


class AgentForkError(RuntimeError):
    def __init__(self, code: str, *, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = reason_codes


class AgentForkForbiddenError(AgentForkError):
    pass


class AgentForkConflictError(AgentForkError):
    pass


@dataclass(frozen=True, slots=True)
class AgentForkPolicy:
    policy: AgentForkPolicyValue
    row_version: int
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "row_version": self.row_version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class AgentForkPreview:
    source_agent_id: str
    source_revision_ref: str
    source_release_id: str
    source_display_name: str
    suggested_display_name: str
    copied: Mapping[str, Any]
    dependency_gaps: tuple[Mapping[str, Any], ...]
    excluded: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "agent_fork_preview",
            "schema_version": 1,
            "source": {
                "agent_id": self.source_agent_id,
                "revision_ref": self.source_revision_ref,
                "release_id": self.source_release_id,
                "display_name": self.source_display_name,
            },
            "suggested_display_name": self.suggested_display_name,
            "copied": dict(self.copied),
            "dependency_gaps": [dict(item) for item in self.dependency_gaps],
            "excluded": list(self.excluded),
            "will_start_disabled": bool(self.dependency_gaps),
        }


@dataclass(frozen=True, slots=True)
class AgentForkResult:
    agent_id: str
    revision_id: str
    release_id: str
    lineage_id: str
    dependency_gaps: tuple[Mapping[str, Any], ...]
    status: Literal["enabled", "disabled"]
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "agent_fork_result",
            "schema_version": 1,
            "agent_id": self.agent_id,
            "revision_id": self.revision_id,
            "release_id": self.release_id,
            "lineage_id": self.lineage_id,
            "dependency_gaps": [dict(item) for item in self.dependency_gaps],
            "status": self.status,
            "audience": "private",
            "replayed": self.replayed,
            "manager_url": f"/agents/manage/?agent_id={self.agent_id}",
        }


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    agent_id: str
    owner_user_id: str
    routing_name: str
    display_name: str
    revision_ref: str
    release_id: str
    definition: dict[str, Any]
    listing: dict[str, Any]
    policy: AgentForkPolicy
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class _PreparedFork:
    source: _SourceSnapshot
    definition: dict[str, Any]
    listing: dict[str, Any]
    dependency_gaps: tuple[Mapping[str, Any], ...]


async def read_agent_fork_policies(
    db_path: str | Path,
    *,
    team_id: str,
) -> dict[str, AgentForkPolicy]:
    """Read catalog policy badges without constructing the publication stack."""

    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT source_agent_id,policy,row_version,updated_at
            FROM team_subagent_fork_policy WHERE team_id=?
            """,
            (team_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {
        str(row["source_agent_id"]): AgentForkPolicy(
            policy=str(row["policy"]),  # type: ignore[arg-type]
            row_version=int(row["row_version"]),
            updated_at=str(row["updated_at"]),
        )
        for row in rows
    }


class AgentForkService:
    """Fork immutable live releases without copying grants, secrets, or approval state."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        validator: DeterministicAgentPublicationValidator,
        revision_store: AgentRevisionStore,
    ) -> None:
        self.db_path = str(db_path)
        self._validator = validator
        self._revision_store = revision_store
        self._aggregate_store = TeamSubagentStore(self.db_path)

    async def set_policy(
        self,
        *,
        team_id: str,
        source_agent_id: str,
        actor_user_id: str,
        policy: AgentForkPolicyValue,
        expected_row_version: int,
    ) -> AgentForkPolicy:
        if policy not in _POLICIES or expected_row_version < 0:
            raise ValueError("invalid Agent Fork policy")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._require_owner_conn(
                    db,
                    team_id=team_id,
                    agent_id=source_agent_id,
                    actor_user_id=actor_user_id,
                )
                async with db.execute(
                    """
                    SELECT policy,row_version,updated_at,created_at
                    FROM team_subagent_fork_policy
                    WHERE team_id=? AND source_agent_id=?
                    """,
                    (team_id, source_agent_id),
                ) as cursor:
                    current = await cursor.fetchone()
                current_version = int(current["row_version"]) if current is not None else 0
                if current_version != expected_row_version:
                    raise AgentForkConflictError("agent_fork_policy_version_conflict")
                timestamp = _next_timestamp(current["updated_at"] if current is not None else None)
                if current is None:
                    await db.execute(
                        """
                        INSERT INTO team_subagent_fork_policy(
                          team_id,source_agent_id,policy,row_version,updated_by,created_at,updated_at
                        ) VALUES (?,?,?,1,?,?,?)
                        """,
                        (team_id, source_agent_id, policy, actor_user_id, timestamp, timestamp),
                    )
                    row_version = 1
                    previous_policy = "not_allowed"
                else:
                    updated = await db.execute(
                        """
                        UPDATE team_subagent_fork_policy
                        SET policy=?,row_version=row_version+1,updated_by=?,updated_at=?
                        WHERE team_id=? AND source_agent_id=? AND row_version=?
                        """,
                        (
                            policy,
                            actor_user_id,
                            timestamp,
                            team_id,
                            source_agent_id,
                            expected_row_version,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AgentForkConflictError("agent_fork_policy_version_conflict")
                    row_version = expected_row_version + 1
                    previous_policy = str(current["policy"])
                await UserStore(self.db_path).write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action=AuditAction.AGENT_FORK_POLICY_CHANGED,
                    target_kind="team_subagent",
                    target_id=source_agent_id,
                    target_team_id=team_id,
                    scope="team",
                    payload={"from": previous_policy, "to": policy, "row_version": row_version},
                    conn=db,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentForkPolicy(policy=policy, row_version=row_version, updated_at=timestamp)

    async def settings(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            aggregate = await self._require_owner_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            async with db.execute(
                """
                SELECT policy,row_version,updated_at FROM team_subagent_fork_policy
                WHERE team_id=? AND source_agent_id=?
                """,
                (team_id, agent_id),
            ) as cursor:
                policy_row = await cursor.fetchone()
            async with db.execute(
                """
                SELECT * FROM team_subagent_fork_lineage
                WHERE team_id=? AND fork_agent_id=?
                """,
                (team_id, agent_id),
            ) as cursor:
                lineage = await cursor.fetchone()
            lineage_payload = await self._lineage_payload_conn(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                aggregate=aggregate,
                lineage=lineage,
            )
        policy = (
            AgentForkPolicy(
                policy=str(policy_row["policy"]),  # type: ignore[arg-type]
                row_version=int(policy_row["row_version"]),
                updated_at=str(policy_row["updated_at"]),
            )
            if policy_row is not None
            else AgentForkPolicy("not_allowed", 0, None)
        )
        return {
            "kind": "agent_fork_settings",
            "schema_version": 1,
            "agent_id": agent_id,
            "policy": policy.to_dict(),
            "lineage": lineage_payload,
        }

    async def preview(
        self,
        *,
        team_id: str,
        source_agent_id: str,
        actor_user_id: str,
    ) -> AgentForkPreview:
        prepared = await self._prepare(
            team_id=team_id,
            source_agent_id=source_agent_id,
            actor_user_id=actor_user_id,
            display_name=None,
            target_agent_id=_deterministic_agent_id(team_id, actor_user_id, "preview-only"),
        )
        source = prepared.source
        return AgentForkPreview(
            source_agent_id=source.agent_id,
            source_revision_ref=source.revision_ref,
            source_release_id=source.release_id,
            source_display_name=source.display_name,
            suggested_display_name=_suggested_display_name(source.display_name),
            copied={
                "prompt": True,
                "listing": True,
                "model": prepared.definition.get("model") is not None,
                "output_contract": prepared.definition.get("output_schema") is not None,
                "tool_count": len(prepared.definition.get("tool_refs") or []),
                "skill_count": len(prepared.definition.get("skill_grants") or []),
            },
            dependency_gaps=prepared.dependency_gaps,
            excluded=(
                "audience_and_grants",
                "approval_and_review_history",
                "assignments_and_popularity",
                "credentials_and_connection_secrets",
                "eval_runs_and_notifications",
            ),
        )

    async def fork(
        self,
        *,
        team_id: str,
        source_agent_id: str,
        actor_user_id: str,
        source_revision_ref: str,
        display_name: str,
        idempotency_key: str,
    ) -> AgentForkResult:
        _validate_idempotency_key(idempotency_key)
        normalized_name = " ".join(display_name.strip().split())
        if not normalized_name or len(normalized_name) > 40:
            raise ValueError("Agent Fork display name must contain 1-40 characters")
        idempotency_hash = _hash_text(idempotency_key)
        request_hash = _hash_json(
            {
                "display_name": normalized_name,
                "source_agent_id": source_agent_id,
                "source_revision_ref": source_revision_ref,
            },
        )
        replay = await self._replay(
            team_id=team_id,
            actor_user_id=actor_user_id,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay

        new_agent_id = _deterministic_agent_id(team_id, actor_user_id, idempotency_hash)
        prepared = await self._prepare(
            team_id=team_id,
            source_agent_id=source_agent_id,
            actor_user_id=actor_user_id,
            display_name=normalized_name,
            target_agent_id=new_agent_id,
        )
        if prepared.source.revision_ref != source_revision_ref:
            raise AgentForkConflictError("agent_fork_source_changed")
        timestamp = datetime.now(UTC).isoformat()
        definition = prepared.definition
        listing = prepared.listing
        subagent = TeamSubagent(
            id=new_agent_id,
            team_id=team_id,
            name=_routing_name(idempotency_hash),
            display_name=normalized_name,
            description=str(listing.get("description", listing.get("summary", "")))[:200],
            system_prompt=str(definition.get("system_prompt", "")),
            tool_refs=list(definition.get("tool_refs") or []),
            model=definition.get("model"),
            max_iterations=int(definition.get("max_iterations") or 10),
            skills=[
                str(item.get("skill_ref", {}).get("name"))
                for item in definition.get("skill_grants") or []
                if isinstance(item, dict) and isinstance(item.get("skill_ref"), dict)
            ],
            output_schema=definition.get("output_schema"),
            visibility="creator_only",
            created_by=actor_user_id,
            status="enabled",
            created_at=timestamp,
            updated_at=timestamp,
        )
        lineage_seed = "\0".join((team_id, actor_user_id, idempotency_hash))
        lineage_id = f"afk_{hashlib.sha256(lineage_seed.encode()).hexdigest()[:32]}"
        commit = TeamSubagentForkCommit(
            lineage_id=lineage_id,
            source_agent_id=source_agent_id,
            source_revision_ref=prepared.source.revision_ref,
            source_release_id=prepared.source.release_id,
            source_owner_user_id=prepared.source.owner_user_id,
            source_display_name=prepared.source.display_name,
            source_snapshot_hash=prepared.source.snapshot_hash,
            policy=prepared.source.policy.policy,
            policy_row_version=prepared.source.policy.row_version,
            dependency_gaps=prepared.dependency_gaps,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
        )
        try:
            await self._aggregate_store.create_team_subagent(
                subagent,
                actor_user_id=actor_user_id,
                audit_action=AuditAction.AGENT_FORKED,
                audit_payload={
                    "source_agent_id": source_agent_id,
                    "source_revision_ref": prepared.source.revision_ref,
                    "dependency_gap_count": len(prepared.dependency_gaps),
                },
                require_active_owner=True,
                revision_store=self._revision_store,
                revision_definition=definition,
                revision_listing=listing,
                revision_target_audience=AgentAudience("private"),
                fork_commit=commit,
            )
        except (aiosqlite.IntegrityError, TeamSubagentNameConflictError) as exc:
            replay = await self._replay(
                team_id=team_id,
                actor_user_id=actor_user_id,
                idempotency_hash=idempotency_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            code = exc.code if isinstance(exc, TeamSubagentNameConflictError) else "agent_fork_conflict"
            raise AgentForkConflictError(code) from exc
        except TeamSubagentOwnerInactiveError as exc:
            raise AgentForkForbiddenError("agent_fork_access_revoked") from exc
        except TeamSubagentForkAdmissionError as exc:
            if exc.code in {"agent_fork_source_changed", "agent_fork_policy_changed"}:
                raise AgentForkConflictError(exc.code) from exc
            raise AgentForkForbiddenError(exc.code) from exc
        result = await self._result_for_agent(
            team_id=team_id,
            agent_id=new_agent_id,
            actor_user_id=actor_user_id,
            replayed=False,
        )
        if result is None:
            raise AgentForkConflictError("agent_fork_commit_missing")
        return result

    async def _prepare(
        self,
        *,
        team_id: str,
        source_agent_id: str,
        actor_user_id: str,
        display_name: str | None,
        target_agent_id: str,
    ) -> _PreparedFork:
        source = await self._source_snapshot(
            team_id=team_id,
            source_agent_id=source_agent_id,
            actor_user_id=actor_user_id,
        )
        definition = json.loads(json.dumps(source.definition))
        listing = json.loads(json.dumps(source.listing))
        if display_name is not None:
            listing["display_name"] = display_name
        validation = await self._validate(
            team_id=team_id,
            actor_user_id=actor_user_id,
            target_agent_id=target_agent_id,
            routing_name=_routing_name(_hash_text(target_agent_id)),
            definition=definition,
            listing=listing,
        )
        if validation.deterministic_reason_codes:
            raise AgentForkForbiddenError(
                "agent_fork_source_safety_blocked",
                reason_codes=validation.deterministic_reason_codes,
            )
        definition, gaps = _drop_unavailable_dependencies(
            definition,
            validation=validation,
        )
        final_validation = await self._validate(
            team_id=team_id,
            actor_user_id=actor_user_id,
            target_agent_id=target_agent_id,
            routing_name=_routing_name(_hash_text(target_agent_id)),
            definition=definition,
            listing=listing,
        )
        if not final_validation.compilation.publishable or final_validation.safety_status not in {"pass", "warning"}:
            reasons = tuple(
                sorted(
                    set(final_validation.compilation.reason_codes)
                    | set(final_validation.deterministic_reason_codes)
                    | set(final_validation.semantic_reason_codes),
                ),
            )
            raise AgentForkForbiddenError("agent_fork_source_incompatible", reason_codes=reasons)
        return _PreparedFork(
            source=source,
            definition=definition,
            listing=listing,
            dependency_gaps=gaps,
        )

    async def _validate(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        target_agent_id: str,
        routing_name: str,
        definition: Mapping[str, Any],
        listing: Mapping[str, Any],
    ) -> AgentPublicationValidation:
        async with open_shared_ctx(self.db_path) as db:
            return await self._validator.validate(
                db,
                team_id=team_id,
                agent_id=target_agent_id,
                owner_user_id=actor_user_id,
                routing_name=routing_name,
                definition=definition,
                listing=listing,
                audience=AgentAudience("private"),
            )

    async def _source_snapshot(
        self,
        *,
        team_id: str,
        source_agent_id: str,
        actor_user_id: str,
    ) -> _SourceSnapshot:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            decision = (
                await TeamSubagentAccessResolver(self.db_path).resolve_many_in_connection(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    subagent_id=source_agent_id,
                )
            ).get(source_agent_id)
            if decision is None or not decision.resolution.can_consume:
                raise AgentForkForbiddenError("agent_fork_source_not_found")
            if decision.resolution.can_manage:
                raise AgentForkForbiddenError("agent_fork_use_duplicate")
            async with db.execute(
                """
                SELECT a.name,a.display_name,a.status,a.lifecycle_state,
                       a.owner_user_id,a.created_by,a.live_revision_id,a.live_release_id,
                       revision.definition_json,revision.listing_json,
                       policy.policy,policy.row_version AS policy_row_version,
                       policy.updated_at AS policy_updated_at
                FROM team_subagent a
                JOIN team_subagent_release release
                  ON release.team_id=a.team_id AND release.subagent_id=a.id
                 AND release.id=a.live_release_id
                JOIN team_subagent_revision revision
                  ON revision.team_id=a.team_id AND revision.subagent_id=a.id
                 AND revision.id=release.revision_id
                LEFT JOIN team_subagent_fork_policy policy
                  ON policy.team_id=a.team_id AND policy.source_agent_id=a.id
                WHERE a.team_id=? AND a.id=?
                """,
                (team_id, source_agent_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or str(row["status"]) != "enabled" or str(row["lifecycle_state"] or "active") != "active":
            raise AgentForkForbiddenError("agent_fork_source_not_found")
        policy_value = str(row["policy"] or "not_allowed")
        if policy_value not in {"recipients", "team_members"}:
            raise AgentForkForbiddenError("agent_fork_not_allowed")
        try:
            definition = json.loads(str(row["definition_json"]))
            listing = json.loads(str(row["listing_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentForkForbiddenError("agent_fork_source_incompatible") from exc
        if not isinstance(definition, dict) or not isinstance(listing, dict):
            raise AgentForkForbiddenError("agent_fork_source_incompatible")
        revision_ref = str(row["live_revision_id"] or "")
        release_id = str(row["live_release_id"] or "")
        owner_user_id = str(row["owner_user_id"] or row["created_by"] or "")
        display_name = str(listing.get("display_name") or row["display_name"] or row["name"])
        return _SourceSnapshot(
            agent_id=source_agent_id,
            owner_user_id=owner_user_id,
            routing_name=str(row["name"]),
            display_name=display_name,
            revision_ref=revision_ref,
            release_id=release_id,
            definition=definition,
            listing=listing,
            policy=AgentForkPolicy(
                policy=policy_value,  # type: ignore[arg-type]
                row_version=int(row["policy_row_version"] or 0),
                updated_at=(str(row["policy_updated_at"]) if row["policy_updated_at"] else None),
            ),
            snapshot_hash=agent_fork_source_snapshot_hash(
                source_agent_id=source_agent_id,
                source_revision_ref=revision_ref,
                source_release_id=release_id,
                definition=definition,
                listing=listing,
            ),
        )

    async def _require_owner_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> aiosqlite.Row:
        async with db.execute(
            """
            SELECT a.* FROM team_subagent a
            JOIN users actor ON actor.id=? AND actor.status='active'
            JOIN team_members member
              ON member.team_id=a.team_id AND member.user_id=actor.id
             AND member.status='active'
            WHERE a.team_id=? AND a.id=?
              AND COALESCE(a.owner_user_id,a.created_by)=actor.id
            """,
            (actor_user_id, team_id, agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentForkForbiddenError("agent_fork_settings_not_found")
        return row

    async def _lineage_payload_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        aggregate: aiosqlite.Row,
        lineage: aiosqlite.Row | None,
    ) -> dict[str, Any] | None:
        if lineage is None:
            return None
        source_id = str(lineage["source_agent_id"])
        decision = (
            await TeamSubagentAccessResolver(self.db_path).resolve_many_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                subagent_id=source_id,
            )
        ).get(source_id)
        source_accessible = bool(decision and decision.resolution.catalog_visible)
        current_source: aiosqlite.Row | None = None
        if source_accessible:
            async with db.execute(
                """
                SELECT a.live_revision_id,a.live_release_id,revision.definition_json,
                       revision.listing_json
                FROM team_subagent a
                LEFT JOIN team_subagent_revision revision
                  ON revision.team_id=a.team_id AND revision.subagent_id=a.id
                 AND revision.id=a.live_revision_id
                WHERE a.team_id=? AND a.id=?
                """,
                (team_id, source_id),
            ) as cursor:
                current_source = await cursor.fetchone()
        current_revision = (
            str(current_source["live_revision_id"]) if current_source is not None and current_source["live_revision_id"] is not None else None
        )
        update_available = bool(source_accessible and current_revision is not None and current_revision != str(lineage["source_revision_ref"]))
        comparison = None
        if update_available and current_source is not None:
            comparison = await self._upstream_comparison_conn(
                db,
                team_id=team_id,
                fork_agent_id=str(aggregate["id"]),
                lineage=lineage,
                current_source=current_source,
            )
        return {
            "lineage_id": str(lineage["id"]),
            "source_kind": str(lineage["source_kind"]),
            "source_agent_id": source_id if source_accessible else None,
            "source_display_name": str(lineage["source_display_name"]),
            "source_revision_ref": str(lineage["source_revision_ref"]),
            "fork_revision_ref": str(lineage["fork_revision_ref"]),
            "source_accessible": source_accessible,
            "upstream": {
                "current_revision_ref": current_revision if source_accessible else None,
                "update_available": update_available,
                "applied_automatically": False,
                "comparison": comparison,
            },
            "dependency_gaps": json.loads(str(lineage["dependency_gaps_json"])),
            "created_at": str(lineage["created_at"]),
        }

    async def _upstream_comparison_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        fork_agent_id: str,
        lineage: aiosqlite.Row,
        current_source: aiosqlite.Row,
    ) -> dict[str, Any] | None:
        async with db.execute(
            """
            SELECT definition_json,listing_json FROM team_subagent_revision
            WHERE team_id=? AND subagent_id=? AND id=?
            """,
            (team_id, str(lineage["source_agent_id"]), str(lineage["source_revision_ref"])),
        ) as cursor:
            old_source = await cursor.fetchone()
        async with db.execute(
            """
            SELECT live_revision_id FROM team_subagent
            WHERE team_id=? AND id=?
            """,
            (team_id, fork_agent_id),
        ) as cursor:
            local = await cursor.fetchone()
        if old_source is None or local is None:
            return None
        try:
            old_definition = json.loads(str(old_source["definition_json"]))
            new_definition = json.loads(str(current_source["definition_json"]))
            old_listing = json.loads(str(old_source["listing_json"]))
            new_listing = json.loads(str(current_source["listing_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return {
            "upstream_changes": _safe_change_summary(
                old_definition,
                old_listing,
                new_definition,
                new_listing,
            ),
            "local_changed_since_fork": str(local["live_revision_id"]) != str(lineage["fork_revision_ref"]),
        }

    async def _replay(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        idempotency_hash: str,
        request_hash: str,
    ) -> AgentForkResult | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT fork_agent_id,request_hash FROM team_subagent_fork_lineage
                WHERE team_id=? AND created_by=? AND idempotency_hash=?
                """,
                (team_id, actor_user_id, idempotency_hash),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise AgentForkConflictError("agent_fork_idempotency_conflict")
        return await self._result_for_agent(
            team_id=team_id,
            agent_id=str(row["fork_agent_id"]),
            actor_user_id=actor_user_id,
            replayed=True,
        )

    async def _result_for_agent(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        replayed: bool,
    ) -> AgentForkResult | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT lineage.id,lineage.fork_revision_ref,lineage.dependency_gaps_json,
                       agent.live_release_id,agent.status
                FROM team_subagent_fork_lineage lineage
                JOIN team_subagent agent
                  ON agent.team_id=lineage.team_id AND agent.id=lineage.fork_agent_id
                WHERE lineage.team_id=? AND lineage.fork_agent_id=?
                  AND lineage.created_by=?
                """,
                (team_id, agent_id, actor_user_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return AgentForkResult(
            agent_id=agent_id,
            revision_id=str(row["fork_revision_ref"]),
            release_id=str(row["live_release_id"]),
            lineage_id=str(row["id"]),
            dependency_gaps=tuple(json.loads(str(row["dependency_gaps_json"]))),
            status=str(row["status"]),  # type: ignore[arg-type]
            replayed=replayed,
        )


def _drop_unavailable_dependencies(
    definition: dict[str, Any],
    *,
    validation: AgentPublicationValidation,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    unavailable_tools = {item.stable_ref: item for item in validation.compilation.tools if not item.available}
    unavailable_skills = {item.grant.identity: item for item in validation.compilation.skills if not item.available}
    raw_tools = definition.get("tool_refs")
    if isinstance(raw_tools, list):
        definition["tool_refs"] = [item for item in raw_tools if isinstance(item, str) and item not in unavailable_tools]
    raw_grants = definition.get("skill_grants")
    if isinstance(raw_grants, list):
        definition["skill_grants"] = [item for item in raw_grants if isinstance(item, dict) and _skill_identity(item) not in unavailable_skills]
    gaps: list[Mapping[str, Any]] = []
    for stable_ref, item in sorted(unavailable_tools.items()):
        if stable_ref in {"skill_read", "skill_invoke"}:
            continue
        gaps.append(
            {
                "kind": "tool",
                "source_ref_digest": _hash_text(f"tool\0{stable_ref}"),
                "reason_codes": list(item.reason_codes or ("tool_unavailable",)),
            },
        )
    for identity, item in sorted(unavailable_skills.items()):
        gaps.append(
            {
                "kind": "skill",
                "source_ref_digest": _hash_text(f"skill\0{identity}"),
                "reason_codes": list(item.reason_codes or ("skill_unavailable",)),
            },
        )
    return definition, tuple(gaps)


def _skill_identity(value: Mapping[str, Any]) -> str:
    ref = value.get("skill_ref")
    if not isinstance(ref, Mapping):
        return ""
    return f"{ref.get('kind', '')}:{ref.get('id', '')}"


def _safe_change_summary(
    old_definition: Mapping[str, Any],
    old_listing: Mapping[str, Any],
    new_definition: Mapping[str, Any],
    new_listing: Mapping[str, Any],
) -> dict[str, Any]:
    old_tools = set(old_definition.get("tool_refs") or [])
    new_tools = set(new_definition.get("tool_refs") or [])
    old_skills = {_skill_identity(item) for item in old_definition.get("skill_grants") or [] if isinstance(item, Mapping)}
    new_skills = {_skill_identity(item) for item in new_definition.get("skill_grants") or [] if isinstance(item, Mapping)}
    return {
        "prompt_changed": old_definition.get("system_prompt") != new_definition.get("system_prompt"),
        "model_changed": old_definition.get("model") != new_definition.get("model"),
        "output_contract_changed": old_definition.get("output_schema") != new_definition.get("output_schema"),
        "listing_changed": old_listing != new_listing,
        "tools_added": len(new_tools - old_tools),
        "tools_removed": len(old_tools - new_tools),
        "skills_added": len(new_skills - old_skills),
        "skills_removed": len(old_skills - new_skills),
    }


def _suggested_display_name(source_display_name: str) -> str:
    suffix = " · Copy"
    return f"{source_display_name[: max(1, 40 - len(suffix))]}{suffix}"


def _routing_name(value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"fork-{digest[:24]}"


def _deterministic_agent_id(team_id: str, actor_user_id: str, value: str) -> str:
    digest = hashlib.sha256(f"{team_id}\0{actor_user_id}\0{value}".encode()).hexdigest()
    return f"osa_{digest[:20]}"


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
        raise ValueError("invalid Agent Fork idempotency key")


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _hash_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _hash_text(payload)


def _next_timestamp(previous: Any) -> str:
    now = datetime.now(UTC)
    if previous:
        try:
            old = datetime.fromisoformat(str(previous).replace("Z", "+00:00"))
        except ValueError:
            old = now - timedelta(microseconds=1)
        if old.tzinfo is None:
            old = old.replace(tzinfo=UTC)
        if now <= old:
            now = old + timedelta(microseconds=1)
    return now.isoformat()


__all__ = [
    "AgentForkConflictError",
    "AgentForkError",
    "AgentForkForbiddenError",
    "AgentForkPolicy",
    "AgentForkPreview",
    "AgentForkResult",
    "AgentForkService",
    "read_agent_fork_policies",
]
