"""Permission-scoped M2 Agent release read projections.

Reviewer reads are deliberately built from the frozen candidate and its base
release.  They never reuse the Owner manager serializer, expose a mutable
working copy, or return an Audience roster.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor
from bowhead_core.agent_capability_catalog import default_catalog
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentReleaseCutoverRequiredError,
    AgentRevisionForbiddenError,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.catalog_localization import SERVER_DERIVED_AGENT_LISTING_KEYS
from bowhead_core.release_plugin_versions import plugin_updates_conn
from bowhead_core.release_skill_versions import skill_updates_conn
from bowhead_core.user_store import UserStore


class AgentReleaseReadStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)

    async def owned_agent_ids(self, *, team_id: str, actor_user_id: str) -> set[str]:
        """调用者名下（owner，缺省为 creator）的 agent id 集合。

        所有权口径与 ``_owned_aggregate`` / ``owned_agent_working_copy_conn`` 同一条
        ``COALESCE(owner_user_id,created_by)``——建议收件箱按此给资源 owner 开列表
        车道（agent-feedback-loop 批 2b-i，PRD §5 agent_def 行）。
        """
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT id FROM team_subagent
                WHERE team_id=? AND lifecycle_state IN ('active','archived')
                  AND COALESCE(owner_user_id,created_by)=?
                """,
                (team_id, actor_user_id),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return {str(row[0]) for row in rows}

    async def working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            aggregate = await _owned_aggregate(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
                allow_archived=True,
            )
            live = await _live_snapshot(db, aggregate=aggregate)
            async with db.execute(
                """
                SELECT * FROM team_subagent_working_copy
                WHERE team_id=? AND subagent_id=?
                """,
                (team_id, agent_id),
            ) as cursor:
                row = await cursor.fetchone()
            async with db.execute(
                """
                SELECT id,candidate_hash,row_version,submitted_at
                FROM team_subagent_release_candidate
                WHERE team_id=? AND subagent_id=? AND state='pending'
                """,
                (team_id, agent_id),
            ) as cursor:
                pending = await cursor.fetchone()
        return {
            "kind": "agent_working_copy_detail",
            "schema_version": 1,
            "agent_id": agent_id,
            "aggregate_row_version": int(aggregate["row_version"]),
            "aggregate": {
                "name": str(aggregate["name"]),
                "status": str(aggregate["status"]),
                "lifecycle_state": str(aggregate["lifecycle_state"]),
            },
            "live": live,
            "working_copy": _owner_working_copy(row) if row is not None else None,
            "pending_candidate": (
                {
                    "candidate_id": str(pending["id"]),
                    "candidate_hash": str(pending["candidate_hash"]),
                    "candidate_row_version": int(pending["row_version"]),
                    "submitted_at": str(pending["submitted_at"]),
                }
                if pending is not None
                else None
            ),
            "actions": {
                "edit": str(aggregate["lifecycle_state"]) == "active",
                "publish": (row is not None and pending is None and str(aggregate["lifecycle_state"]) == "active"),
                "enable": (str(aggregate["lifecycle_state"]) == "active" and str(aggregate["status"]) == "disabled"),
                "disable": (str(aggregate["lifecycle_state"]) == "active" and str(aggregate["status"]) == "enabled"),
                "archive": str(aggregate["lifecycle_state"]) == "active",
                "restore": str(aggregate["lifecycle_state"]) == "archived",
            },
        }

    async def archived_agents(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return owner-only archived summaries for the management surface."""
        limit = max(1, min(limit, 100))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT a.id,a.name,a.display_name,a.description,a.status,a.lifecycle_state,
                       a.row_version,a.live_release_id,a.archived_at,a.updated_at,
                       rel.release_sequence,
                       EXISTS(
                         SELECT 1 FROM team_subagent_release_candidate pending
                         WHERE pending.team_id=a.team_id AND pending.subagent_id=a.id
                           AND pending.state='pending'
                       ) AS has_pending_candidate
                FROM team_subagent a
                LEFT JOIN team_subagent_release rel
                  ON rel.team_id=a.team_id AND rel.subagent_id=a.id
                 AND rel.id=a.live_release_id
                JOIN users owner_user
                  ON owner_user.id=COALESCE(a.owner_user_id,a.created_by)
                 AND owner_user.status='active'
                JOIN team_members owner_member
                  ON owner_member.team_id=a.team_id AND owner_member.user_id=owner_user.id
                 AND owner_member.status='active'
                WHERE a.team_id=? AND COALESCE(a.owner_user_id,a.created_by)=?
                  AND a.definition_mode='revisioned' AND a.lifecycle_state='archived'
                ORDER BY a.archived_at DESC,a.id
                LIMIT ?
                """,
                (team_id, actor_user_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "kind": "agent_archived_management_item",
                "schema_version": 1,
                "agent_id": str(row["id"]),
                "name": str(row["name"]),
                "display_name": str(row["display_name"] or row["name"]),
                "summary": str(row["description"] or ""),
                "status": str(row["status"]),
                "lifecycle_state": str(row["lifecycle_state"]),
                "aggregate_row_version": int(row["row_version"]),
                "live_release_id": str(row["live_release_id"]),
                "release_sequence": int(row["release_sequence"]),
                "has_pending_candidate": bool(row["has_pending_candidate"]),
                "archived_at": str(row["archived_at"]),
                "updated_at": str(row["updated_at"]),
                "actions": {"restore": True, "view_history": True},
            }
            for row in rows
        ]

    async def inbox_count(
        self,
        *,
        team_id: str,
        actor_user_id: str,
    ) -> int:
        async with open_shared_ctx(self.db_path) as db:
            if not await _qualified_reviewer(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
            ):
                raise AgentRevisionForbiddenError("agent_release_review_forbidden")
            async with db.execute(
                """
                SELECT COUNT(*) FROM team_subagent_release_candidate
                WHERE team_id=? AND state='pending'
                """,
                (team_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def inbox(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if not await _qualified_reviewer(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
            ):
                raise AgentRevisionForbiddenError("agent_release_review_forbidden")
            async with db.execute(
                """
                SELECT c.*,r.revision_kind,r.listing_json,s.safe_summary_json,
                       a.name AS routing_name,a.owner_user_id,a.created_by
                FROM team_subagent_release_candidate c
                JOIN team_subagent_revision r
                  ON r.team_id=c.team_id AND r.subagent_id=c.subagent_id
                 AND r.id=c.revision_id
                JOIN agent_safety_report s
                  ON s.team_id=c.team_id AND s.agent_id=c.subagent_id
                 AND s.id=c.safety_report_id
                JOIN team_subagent a
                  ON a.team_id=c.team_id AND a.id=c.subagent_id
                WHERE c.team_id=? AND c.state='pending'
                ORDER BY c.submitted_at ASC,c.id ASC
                LIMIT ?
                """,
                (team_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_review_inbox_item(row) for row in rows]

    async def candidate_detail(
        self,
        *,
        team_id: str,
        candidate_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                candidate = await _candidate_row(
                    db,
                    team_id=team_id,
                    candidate_id=candidate_id,
                )
                owner_id = str(candidate["owner_user_id"] or candidate["created_by"])
                is_owner = owner_id == actor_user_id
                is_reviewer = await _qualified_reviewer(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                )
                decided_by_actor = str(candidate["decided_by"] or "") == actor_user_id
                if not is_owner and not is_reviewer:
                    raise AgentRevisionForbiddenError("agent_release_candidate_not_found")
                if str(candidate["state"]) != "pending" and not (is_owner or decided_by_actor):
                    raise AgentRevisionForbiddenError("agent_release_candidate_not_found")
                base = await _base_snapshot(db, candidate=candidate)
                candidate_revision = _decode_object(
                    candidate["definition_json"],
                    name="candidate definition",
                )
                candidate_listing = _decode_object(
                    candidate["listing_json"],
                    name="candidate listing",
                )
                target_audience = _decode_object(
                    candidate["target_audience_json"],
                    name="candidate audience",
                )
                plugin_updates = await plugin_updates_conn(db, team_id=team_id, recorded_json=candidate["plugin_versions_json"])
                detail = _candidate_detail(
                    candidate,
                    base=base,
                    definition=candidate_revision,
                    listing=candidate_listing,
                    target_audience=target_audience,
                    can_review=is_reviewer and str(candidate["state"]) == "pending",
                    can_withdraw=is_owner and str(candidate["state"]) == "pending",
                )
                detail["plugin_updates"] = plugin_updates
                # Skills whose approved revision changed since this candidate was frozen.
                detail["skill_updates"] = await skill_updates_conn(db, team_id=team_id, owner_user_id=owner_id, definition=candidate_revision)
                from bowhead_core.agent_resource_summary import plugin_change_items, tool_and_skill_changes

                detail["diff"].update(tool_and_skill_changes(base["definition"] if base else {}, candidate_revision))
                detail["diff"]["plugin_items"] = await plugin_change_items(db, team_id, detail["diff"]["plugin_changes"])
                await UserStore(self.db_path).write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action=AuditAction.AGENT_RELEASE_CANDIDATE_VIEWED,
                    target_kind="team_subagent_release_candidate",
                    target_id=candidate_id,
                    target_team_id=team_id,
                    scope="team",
                    payload={
                        "agent_id": str(candidate["subagent_id"]),
                        "state": str(candidate["state"]),
                        "viewer_role": "owner" if is_owner else "reviewer",
                    },
                    conn=db,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return detail

    async def releases(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _owned_aggregate(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
                allow_archived=True,
            )
            async with db.execute(
                """
                SELECT rel.*,rev.revision_kind,rev.sequence AS revision_sequence,
                       rev.listing_json,s.status AS safety_status,s.safe_summary_json,
                       c.decided_by,c.self_review,c.decision_reason_code
                FROM team_subagent_release rel
                JOIN team_subagent_revision rev
                  ON rev.team_id=rel.team_id AND rev.subagent_id=rel.subagent_id
                 AND rev.id=rel.revision_id
                JOIN agent_safety_report s
                  ON s.team_id=rel.team_id AND s.agent_id=rel.subagent_id
                 AND s.id=rel.safety_report_id
                LEFT JOIN team_subagent_release_candidate c
                  ON c.team_id=rel.team_id AND c.subagent_id=rel.subagent_id
                 AND c.id=rel.source_candidate_id
                WHERE rel.team_id=? AND rel.subagent_id=?
                ORDER BY rel.release_sequence DESC
                LIMIT ?
                """,
                (team_id, agent_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_release_item(row) for row in rows]


async def _owned_aggregate(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    allow_archived: bool = False,
) -> aiosqlite.Row:
    lifecycle = "IN ('active','archived')" if allow_archived else "='active'"
    async with db.execute(
        f"""
        SELECT * FROM team_subagent
        WHERE team_id=? AND id=? AND definition_mode='revisioned'
          AND lifecycle_state {lifecycle}
          AND COALESCE(owner_user_id,created_by)=?
        """,  # noqa: S608 -- lifecycle is a fixed local fragment
        (team_id, agent_id, actor_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        # An Agent the caller owns but which has never been imported into release
        # history is NOT missing.  Answering agent_not_found here sends the owner of
        # a catalog-visible Agent to a manager page claiming it does not exist, and
        # points them away from the only remedy (the baseline import).
        async with db.execute(
            f"""
            SELECT definition_mode FROM team_subagent
            WHERE team_id=? AND id=? AND lifecycle_state {lifecycle}
              AND COALESCE(owner_user_id,created_by)=?
            """,  # noqa: S608 -- lifecycle is a fixed local fragment
            (team_id, agent_id, actor_user_id),
        ) as cursor:
            legacy = await cursor.fetchone()
        if legacy is not None and str(legacy["definition_mode"]) == "legacy_mutable":
            raise AgentReleaseCutoverRequiredError("agent_release_cutover_required")
        raise AgentRevisionForbiddenError("agent_not_found")
    return row


async def _qualified_reviewer(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
) -> bool:
    async with db.execute(
        """
        SELECT 1
        FROM users u
        JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
        JOIN teams t ON t.id=? AND t.status='active'
        WHERE u.id=? AND u.status='active'
          AND (u.id=t.created_by_user_id OR tm.role='team_admin')
        """,
        (team_id, team_id, actor_user_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _candidate_row(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    candidate_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT c.*,r.revision_kind,r.definition_json,r.listing_json,
               r.definition_hash,a.name AS routing_name,a.display_name AS live_display_name,
               a.owner_user_id,a.created_by,s.status AS safety_status,
               s.deterministic_json,s.semantic_json,s.safe_summary_json,
               behavior.descriptor_json AS behavior_descriptor_json,
               behavior.behavior_fingerprint AS behavior_fingerprint,
               control.review_release_note AS review_release_note
        FROM team_subagent_release_candidate c
        JOIN team_subagent_revision r
          ON r.team_id=c.team_id AND r.subagent_id=c.subagent_id AND r.id=c.revision_id
        JOIN team_subagent a
          ON a.team_id=c.team_id AND a.id=c.subagent_id
        JOIN agent_safety_report s
          ON s.team_id=c.team_id AND s.agent_id=c.subagent_id AND s.id=c.safety_report_id
        LEFT JOIN team_subagent_revision_behavior behavior
          ON behavior.revision_id=r.id AND behavior.team_id=r.team_id
        LEFT JOIN agent_eval_candidate_control control ON control.candidate_id=c.id
        WHERE c.team_id=? AND c.id=?
        """,
        (team_id, candidate_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentRevisionForbiddenError("agent_release_candidate_not_found")
    return row


async def _live_snapshot(
    db: aiosqlite.Connection,
    *,
    aggregate: Mapping[str, Any],
) -> dict[str, Any]:
    release_id = aggregate["live_release_id"]
    if not release_id:
        raise AgentPublicationBlockedError("agent_live_release_missing")
    async with db.execute(
        """
        SELECT rel.*,rev.definition_json,rev.listing_json,rev.revision_kind,
               candidate.plugin_versions_json
        FROM team_subagent_release rel
        JOIN team_subagent_revision rev
          ON rev.team_id=rel.team_id AND rev.subagent_id=rel.subagent_id
         AND rev.id=rel.revision_id
        LEFT JOIN team_subagent_release_candidate candidate
          ON candidate.team_id=rel.team_id AND candidate.subagent_id=rel.subagent_id
         AND candidate.id=rel.source_candidate_id
        WHERE rel.team_id=? AND rel.subagent_id=? AND rel.id=?
        """,
        (aggregate["team_id"], aggregate["id"], release_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentPublicationBlockedError("agent_live_release_missing")
    definition = _decode_object(row["definition_json"], name="live definition")
    return {
        "release_id": str(row["id"]),
        "release_sequence": int(row["release_sequence"]),
        "revision_id": str(row["revision_id"]),
        "revision_kind": str(row["revision_kind"]),
        "definition": definition,
        "listing": _decode_object(row["listing_json"], name="live listing"),
        "audience": AgentAudience.from_release_snapshot(_decode_object(row["audience_snapshot_json"], name="live audience")).to_dict(),
        "activated_at": str(row["activated_at"]),
        # Plugins updated since this release (PRD §7.4): the details page suggests re-running evaluation.
        "plugin_updates": await plugin_updates_conn(db, team_id=str(aggregate["team_id"]), recorded_json=row["plugin_versions_json"]),
        # Skills updated since this release (agent-skill-gates D2): runs already use the new revision.
        "skill_updates": await skill_updates_conn(
            db,
            team_id=str(aggregate["team_id"]),
            owner_user_id=str(aggregate["owner_user_id"] or aggregate["created_by"]),
            definition=definition,
        ),
    }


async def _base_snapshot(
    db: aiosqlite.Connection,
    *,
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    release_id = candidate["base_live_release_id"]
    if release_id is None:
        return None
    async with db.execute(
        """
        SELECT rel.*,rev.definition_json,rev.listing_json,
               behavior.descriptor_json AS behavior_descriptor_json,
               behavior.behavior_fingerprint AS behavior_fingerprint
        FROM team_subagent_release rel
        JOIN team_subagent_revision rev
          ON rev.team_id=rel.team_id AND rev.subagent_id=rel.subagent_id
         AND rev.id=rel.revision_id
        LEFT JOIN team_subagent_revision_behavior behavior
          ON behavior.revision_id=rev.id AND behavior.team_id=rev.team_id
        WHERE rel.team_id=? AND rel.subagent_id=? AND rel.id=?
        """,
        (candidate["team_id"], candidate["subagent_id"], release_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentPublicationBlockedError("agent_base_release_missing")
    return {
        "release_id": str(row["id"]),
        "release_sequence": int(row["release_sequence"]),
        "revision_id": str(row["revision_id"]),
        "definition": _decode_object(row["definition_json"], name="base definition"),
        "listing": _decode_object(row["listing_json"], name="base listing"),
        "audience": _decode_object(row["audience_snapshot_json"], name="base audience"),
        "risk": _decode_object(row["risk_json"], name="base risk"),
        "dependency_fingerprint": str(row["dependency_fingerprint"]),
        "capability_fingerprint": str(row["capability_fingerprint"]),
        "behavior_fingerprint": (str(row["behavior_fingerprint"]) if row["behavior_fingerprint"] is not None else None),
        "behavior": _behavior_summary(
            row["behavior_descriptor_json"],
            definition=_decode_object(row["definition_json"], name="base definition"),
        ),
    }


def _owner_working_copy(row: Mapping[str, Any]) -> dict[str, Any]:
    validation = _decode_object(row["validation_json"], name="working copy validation")
    return {
        "id": str(row["id"]),
        "base_release_id": str(row["base_release_id"]),
        "definition": _decode_object(row["definition_json"], name="working copy definition"),
        "listing": _decode_object(row["listing_json"], name="working copy listing"),
        "audience": _decode_object(row["audience_json"], name="working copy audience"),
        "validation": validation,
        "change_kind": str(validation.get("change_kind", "authored")),
        "rollback_source_release_id": validation.get("rollback_source_release_id"),
        "row_version": int(row["row_version"]),
        "updated_at": str(row["updated_at"]),
    }


def _review_inbox_item(row: Mapping[str, Any]) -> dict[str, Any]:
    listing = _decode_object(row["listing_json"], name="candidate listing")
    audience = _decode_object(row["target_audience_json"], name="candidate audience")
    risk = _decode_object(row["risk_json"], name="candidate risk")
    return {
        "kind": "agent_release_inbox_item",
        "schema_version": 1,
        "candidate_id": str(row["id"]),
        "candidate_hash": str(row["candidate_hash"]),
        "candidate_row_version": int(row["row_version"]),
        "agent_id": str(row["subagent_id"]),
        "display_name": str(listing.get("display_name") or row["routing_name"]),
        "owner_user_id": str(row["owner_user_id"] or row["created_by"]),
        "change_kind": str(row["revision_kind"]),
        "audience": _audience_summary(audience),
        "risk": risk,
        "safety": _decode_object(row["safe_summary_json"], name="candidate safety"),
        "submitted_at": str(row["submitted_at"]),
        "detail_url": f"/agents/manage/?release_candidate={row['id']}",
    }


def _candidate_detail(
    row: Mapping[str, Any],
    *,
    base: dict[str, Any] | None,
    definition: dict[str, Any],
    listing: dict[str, Any],
    target_audience: dict[str, Any],
    can_review: bool,
    can_withdraw: bool,
) -> dict[str, Any]:
    candidate_risk = _decode_object(row["risk_json"], name="candidate risk")
    from bowhead_core.agent_knowledge_policy import knowledge_access_expanded
    from bowhead_core.agent_runtime_extensions import extension_policy_summary

    base_definition = base["definition"] if base else {}
    base_listing = base["listing"] if base else {}
    base_audience = base["audience"] if base else None
    diff = {
        "listing_fields": _changed_keys(base_listing, listing),
        "instructions_changed": base_definition.get("system_prompt") != definition.get("system_prompt"),
        "model_changed": base_definition.get("model") != definition.get("model"),
        "output_contract_changed": base_definition.get("output_schema") != definition.get("output_schema"),
        "tool_set_changed": base_definition.get("tool_refs") != definition.get("tool_refs"),
        "skill_set_changed": base_definition.get("skill_grants") != definition.get("skill_grants"),
        "plugin_set_changed": _plugin_selection(base_definition) != _plugin_selection(definition),
        "plugin_changes": _plugin_changes(base_definition, definition),
        "knowledge_access_changed": base_definition.get("knowledge_access") != definition.get("knowledge_access"),
        "knowledge_access_expanded": knowledge_access_expanded(base_definition, definition),
        "runtime_extensions_changed": base_definition.get("runtime_extensions") != definition.get("runtime_extensions"),
        "primary_capability_changed": base_definition.get("primary_capability_ref") != definition.get("primary_capability_ref"),
        "capability_protocol_block_changed": (base_definition.get("capability_protocol_block") is True)
        != (definition.get("capability_protocol_block") is True),
        "behavior_changed": (base is None or base["behavior_fingerprint"] != row["behavior_fingerprint"]),
        "dependency_changed": (base is None or base["dependency_fingerprint"] != row["dependency_fingerprint"]),
        "capability_changed": (base is None or base["capability_fingerprint"] != row["capability_fingerprint"]),
        "risk_changed": base is None or base["risk"] != candidate_risk,
        "audience": {
            "before": _audience_summary(base_audience) if base_audience else None,
            "after": _audience_summary(target_audience),
            "changed": base_audience != target_audience,
            "expanded": _audience_expanded(base_audience, target_audience),
        },
    }
    return {
        "kind": "agent_release_candidate_detail",
        "schema_version": 1,
        "candidate_id": str(row["id"]),
        "candidate_hash": str(row["candidate_hash"]),
        "candidate_row_version": int(row["row_version"]),
        "state": str(row["state"]),
        "agent": {
            "id": str(row["subagent_id"]),
            "display_name": str(listing.get("display_name") or row["routing_name"]),
            "owner_user_id": str(row["owner_user_id"] or row["created_by"]),
        },
        "base": (
            {
                "release_id": base["release_id"],
                "release_sequence": base["release_sequence"],
                "revision_id": base["revision_id"],
            }
            if base
            else None
        ),
        "candidate": {
            "revision_id": str(row["revision_id"]),
            "change_kind": str(row["revision_kind"]),
            "listing": listing,
            "behavior": {
                "model": definition.get("model"),
                "max_iterations": definition.get("max_iterations"),
                "has_output_contract": definition.get("output_schema") is not None,
                "tool_count": _list_size(definition.get("tool_refs")),
                "skill_count": _list_size(definition.get("skill_grants")),
                "runtime_extensions": extension_policy_summary(definition),
                "capability_protocol_block": definition.get("capability_protocol_block") is True,
                **_behavior_summary(
                    row["behavior_descriptor_json"],
                    definition=definition,
                ),
            },
            "audience": _audience_summary(target_audience),
            "risk": candidate_risk,
            "dependency_fingerprint": str(row["dependency_fingerprint"]),
            "capability_fingerprint": str(row["capability_fingerprint"]),
            "behavior_fingerprint": (str(row["behavior_fingerprint"]) if row["behavior_fingerprint"] is not None else None),
            "safety": {
                "status": str(row["safety_status"]),
                "deterministic": _decode_object(
                    row["deterministic_json"],
                    name="candidate deterministic safety",
                ),
                "semantic": _decode_object(
                    row["semantic_json"],
                    name="candidate semantic safety",
                ),
                "summary": _decode_object(
                    row["safe_summary_json"],
                    name="candidate safety summary",
                ),
            },
        },
        "diff": diff,
        # The note the Owner submitted with (outside the candidate hash), else the candidate's own.
        "release_note": row["review_release_note"] if row["review_release_note"] is not None else row["release_note"],
        "submitted_by": str(row["submitted_by"]),
        "submitted_at": str(row["submitted_at"]),
        "decision": (
            {
                "decided_by": str(row["decided_by"]),
                "reason_code": row["decision_reason_code"],
                "self_review": bool(row["self_review"]),
                "decided_at": str(row["decided_at"]),
            }
            if row["decided_at"] is not None
            else None
        ),
        "actions": {
            "approve": can_review,
            "reject": can_review,
            "withdraw": can_withdraw,
        },
    }


def _release_test(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        evidence = json.loads(value)
    except ValueError:
        return None
    test = evidence.get("release_test") if isinstance(evidence, dict) else None
    if not isinstance(test, dict) or not isinstance(test.get("state"), str):
        return None
    reasons = test.get("reason_codes")
    return {
        "state": test["state"],
        "reason_codes": [code for code in reasons if isinstance(code, str)] if isinstance(reasons, list) else [],
    }


def _release_item(row: Mapping[str, Any]) -> dict[str, Any]:
    listing = _decode_object(row["listing_json"], name="release listing")
    audience = _decode_object(row["audience_snapshot_json"], name="release audience")
    return {
        "kind": "agent_release_history_item",
        "schema_version": 1,
        "release_id": str(row["id"]),
        "release_sequence": int(row["release_sequence"]),
        "revision_id": str(row["revision_id"]),
        "revision_sequence": int(row["revision_sequence"]),
        "change_kind": str(row["revision_kind"]),
        "display_name": str(listing.get("display_name") or ""),
        "audience": _audience_summary(audience),
        "risk": _decode_object(row["risk_json"], name="release risk"),
        "safety": {
            "status": str(row["safety_status"]),
            "summary": _decode_object(row["safe_summary_json"], name="release safety"),
        },
        "release_note": row["release_note"],
        # Private releases: what the test said when the Owner released (for example no conclusion, or untested).
        "release_test": _release_test(row["eval_evidence_json"]),
        "activated_by": str(row["activated_by"]),
        "activated_at": str(row["activated_at"]),
        "review": (
            {
                "decided_by": str(row["decided_by"]),
                "reason_code": row["decision_reason_code"],
                "self_review": bool(row["self_review"]),
            }
            if row["decided_by"] is not None
            else None
        ),
    }


def _audience_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    users = value.get("user_ids")
    groups = value.get("group_ids")
    return {
        "mode": str(value.get("effective_mode", "unknown")),
        "user_count": len(users) if isinstance(users, list) else 0,
        "group_count": len(groups) if isinstance(groups, list) else 0,
    }


def _audience_expanded(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
) -> bool:
    """Compare exact rosters without returning their identities to reviewers."""
    if before is None:
        return str(after.get("effective_mode")) != "private"
    rank = {"private": 0, "custom": 1, "teamwide": 2}
    before_mode = str(before.get("effective_mode"))
    after_mode = str(after.get("effective_mode"))
    before_rank = rank.get(before_mode, -1)
    after_rank = rank.get(after_mode, -1)
    if before_rank != after_rank:
        return after_rank > before_rank
    if after_mode != "custom":
        return False
    before_users = set(before.get("user_ids") or ())
    before_groups = set(before.get("group_ids") or ())
    after_users = set(after.get("user_ids") or ())
    after_groups = set(after.get("group_ids") or ())
    return not after_users.issubset(before_users) or not after_groups.issubset(before_groups)


def _plugin_selection(definition: Mapping[str, Any]) -> list[str]:
    """Selected team Plugin ids and built-in Plugin references, in a stable order."""
    selected = [*(definition.get("capability_ids") or []), *(definition.get("builtin_plugin_ids") or [])]
    return sorted(str(item) for item in selected)


def _plugin_changes(base: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, list[str]]:
    before, after = set(_plugin_selection(base)), set(_plugin_selection(definition))
    return {"added": sorted(after - before), "removed": sorted(before - after)}


def _changed_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Which listing fields the *Owner* changed -- the materializer's own keys don't count."""
    candidates = (set(before) | set(after)) - SERVER_DERIVED_AGENT_LISTING_KEYS
    return sorted(key for key in candidates if before.get(key) != after.get(key))


def _list_size(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _behavior_summary(
    value: Any,
    *,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    requested = definition.get("primary_capability_ref")
    requested_ref = requested if isinstance(requested, str) else None
    primary_name, primary_name_en = _primary_capability_names(requested_ref)
    if value is None:
        return {
            "requested_primary_capability_ref": requested_ref,
            "primary_capability_ref": None,
            "primary_capability_name": primary_name,
            "primary_capability_name_en": primary_name_en,
            "verification_state": "blocked",
            "eligible_role_ids": [],
            "output_contract_refs": [],
        }
    try:
        descriptor = AgentBehaviorDescriptor.model_validate_json(str(value))
    except ValueError:
        return {
            "requested_primary_capability_ref": requested_ref,
            "primary_capability_ref": None,
            "primary_capability_name": primary_name,
            "primary_capability_name_en": primary_name_en,
            "verification_state": "blocked",
            "eligible_role_ids": [],
            "output_contract_refs": [],
        }
    return {
        "requested_primary_capability_ref": requested_ref,
        "primary_capability_ref": descriptor.primary_capability_ref,
        "primary_capability_name": primary_name,
        "primary_capability_name_en": primary_name_en,
        "verification_state": descriptor.verification_state,
        "eligible_role_ids": list(descriptor.eligible_lead_role_ids),
        "output_contract_refs": list(descriptor.output_contract_refs),
    }


def _primary_capability_names(exact_ref: str | None) -> tuple[str | None, str | None]:
    if exact_ref is None:
        return None, None
    try:
        capability_id, raw_version = exact_ref.rsplit("@", 1)
        catalog = default_catalog()
        if catalog is None:
            return None, None
        definition = catalog.get_definition(capability_id, int(raw_version))
    except (KeyError, TypeError, ValueError):
        return None, None
    return definition.listing["zh-CN"].name, definition.listing["en-US"].name


def _decode_object(value: Any, *, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentPublicationBlockedError(f"agent_{name.replace(' ', '_')}_malformed") from exc
    if not isinstance(decoded, dict):
        raise AgentPublicationBlockedError(f"agent_{name.replace(' ', '_')}_malformed")
    return decoded


__all__ = ["AgentReleaseReadStore"]
