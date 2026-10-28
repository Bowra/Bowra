"""Team charts derived on demand from durable, successful human root executions.

No raw content or member identifiers are copied into ranking storage. Every
read recomputes qualification and current access before publishing a snapshot;
an old snapshot is never a fallback after deletion or revocation.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.team_subagent_access import TeamSubagentAccessDecision, TeamSubagentAccessResolver

RankingKind = Literal["popular", "trending"]
PRIVACY_FLOOR = 5


class AgentRankingStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def collection(
        self,
        *,
        team_id: str,
        user_id: str,
        ranking_kind: RankingKind,
        domain_id: str | None,
        items: list[dict[str, Any]],
        team_row_versions: dict[str, int],
        system_override_versions: dict[str, int],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        end = instant.replace(hour=0, minute=0, second=0, microsecond=0)
        days = 28 if ranking_kind == "popular" else 7
        start = end - timedelta(days=days)
        previous = start - timedelta(days=7) if ranking_kind == "trending" else None
        result: dict[str, Any] = {
            "kind": "agent_ranking_collection",
            "schema_version": 1,
            "ranking_kind": ranking_kind,
            "domain_id": domain_id,
            "state": "insufficient_sample",
            "generated_at": instant.isoformat(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "comparison_window_start": previous.isoformat() if previous else None,
            "comparison_window_end": start.isoformat() if previous else None,
            "rows": [],
        }
        resolver = TeamSubagentAccessResolver(self.db_path)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # A single source view covers qualification, membership, assignment,
            # access and the complete snapshot insert, including concurrent reads.
            await db.execute("BEGIN IMMEDIATE")
            coverage = await (await db.execute("SELECT complete_days_from FROM agent_ranking_coverage WHERE singleton=1")).fetchone()
            coverage_start = datetime.fromisoformat(coverage[0])
            if (previous or start) < coverage_start:
                result["generated_at"] = None
                result["reason"] = "window_incomplete"
                result["available_at"] = (coverage_start + timedelta(days=14 if previous else 28)).isoformat()
                await db.commit()
                return result

            decisions = await resolver.resolve_many_in_connection(db, team_id=team_id, actor_user_id=user_id, now=instant)
            overrides = {
                f"system:{row['name']}": row
                for row in await (
                    await db.execute("SELECT name,enabled,row_version,updated_by FROM team_builtin_subagent_override WHERE team_id=?", (team_id,))
                ).fetchall()
            }
            owner = await (await db.execute("SELECT created_by_user_id FROM teams WHERE id=? AND status='active'", (team_id,))).fetchone()
            members = {
                row["user_id"]
                for row in await (
                    await db.execute(
                        """SELECT member.user_id FROM team_members member JOIN users actor ON actor.id=member.user_id
                       WHERE member.team_id=? AND member.status='active' AND actor.status='active'""",
                        (team_id,),
                    )
                ).fetchall()
            }
            if owner is None or user_id not in members:
                await db.commit()
                return result
            eligible: dict[str, dict[str, Any]] = {}
            maintainers: dict[str, set[str]] = {}
            for item in items:
                agent_id = item["agent_id"]
                availability = item["availability"]
                if not availability.get("discoverable") or not item["actions"].get("view_detail"):
                    continue
                if availability.get("limited") or not (availability.get("hireable") or availability.get("runnable")):
                    continue
                if domain_id and domain_id not in item["facets"]["domains"]:
                    continue
                if item["source"]["kind"] == "team":
                    decision = decisions.get(agent_id)
                    if (
                        decision is None
                        or not decision.resolution.can_consume
                        or decision.facts.audience_mode != "teamwide"
                        or not decision.facts.owner_active
                        or decision.row_version != team_row_versions.get(agent_id)
                        or item["safety"]["status"] != "pass"
                    ):
                        continue
                    if await self._critical_failure(db, team_id, agent_id):
                        continue
                    maintainers[agent_id] = {decision.owner_user_id} if decision.owner_user_id else set()
                else:
                    override = overrides.get(agent_id)
                    if override is not None and not override["enabled"]:
                        continue
                    if (int(override["row_version"]) if override else 0) != system_override_versions.get(agent_id, 0):
                        continue
                    maintainers[agent_id] = {owner[0]}
                    if override and override["updated_by"]:
                        maintainers[agent_id].add(override["updated_by"])
                eligible[agent_id] = item

            adopted: dict[str, set[str]] = defaultdict(set)
            assignments = await (
                await db.execute(
                    """SELECT agent_id,user_id FROM agent_assignment WHERE team_id=?
                   AND personal_state='active' AND access_state='granted'""",
                    (team_id,),
                )
            ).fetchall()
            member_decisions: dict[str, dict[str, TeamSubagentAccessDecision]] = {}
            for assignment in assignments:
                agent_id, actor = assignment["agent_id"], assignment["user_id"]
                if agent_id not in eligible or actor not in members or actor in maintainers[agent_id]:
                    continue
                if actor not in member_decisions:
                    member_decisions[actor] = await resolver.resolve_many_in_connection(
                        db, team_id=team_id, actor_user_id=actor, subagent_ids=team_row_versions, now=instant
                    )
                decision = member_decisions[actor].get(agent_id)
                if decision and decision.resolution.can_consume:
                    adopted[agent_id].add(actor)
            for agent_id, item in eligible.items():
                if item["source"]["kind"] == "system":
                    # System-required Agents have implicit adoption, not fake
                    # personal assignments. Exclude their team maintainers.
                    adopted[agent_id] = members - maintainers[agent_id]

            use: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
            rows = await (
                await db.execute(_QUALIFIED_USAGE_SQL, {"team": team_id, "start": (previous or start).isoformat(), "end": end.isoformat()})
            ).fetchall()
            for row in rows:
                agent_id, actor = row["agent_id"], row["user_id"]
                if agent_id not in eligible or actor in maintainers[agent_id]:
                    continue
                if eligible[agent_id]["source"]["kind"] == "team":
                    if actor not in member_decisions:
                        member_decisions[actor] = await resolver.resolve_many_in_connection(
                            db, team_id=team_id, actor_user_id=actor, subagent_ids=team_row_versions, now=instant
                        )
                    decision = member_decisions[actor].get(agent_id)
                    if decision is None or not decision.resolution.can_consume:
                        continue
                use[agent_id][actor].add(row["usage_day"])
            aggregates = []
            for agent_id, activity in use.items():
                hires = len(adopted[agent_id])
                if hires < PRIVACY_FLOOR:
                    continue
                if ranking_kind == "popular":
                    active = len(activity)
                    if active < PRIVACY_FLOOR:
                        continue
                    repeats = sum(len(active_days) >= 2 for active_days in activity.values())
                    counts = {"agent_id": agent_id, "hire_members_current": hires, "active_members_28d": active}
                    key = (-active, -hires, -repeats, agent_id)
                else:
                    active = sum(any(day >= start.date().isoformat() for day in active_days) for active_days in activity.values())
                    before = sum(any(day < start.date().isoformat() for day in active_days) for active_days in activity.values())
                    if min(active, before) < PRIVACY_FLOOR or active <= before:
                        continue
                    counts = {"agent_id": agent_id, "hire_members_current": hires, "active_members_7d": active, "active_members_previous_7d": before}
                    key = (before - active, -active, agent_id)
                aggregates.append((key, counts))
            ordered = [counts for _, counts in sorted(aggregates)[:100]]
            state = "ready" if ordered else "insufficient_sample"
            payload = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
            watermark = hashlib.sha256(payload.encode()).hexdigest()
            await db.execute(
                """INSERT OR IGNORE INTO agent_ranking_snapshot
                   (id,team_id,ranking_kind,domain_id,window_start,window_end,algorithm_version,
                    privacy_policy_version,input_watermark,state,rows_json,generated_at)
                   VALUES (?,?,?,?,?,?,1,1,?,?,?,?)""",
                (
                    f"ars_{uuid4().hex}",
                    team_id,
                    ranking_kind,
                    domain_id,
                    start.isoformat(),
                    end.isoformat(),
                    watermark,
                    state,
                    payload,
                    instant.isoformat(),
                ),
            )
            # Retention is bounded and runs without a separately maintained job.
            await db.execute("DELETE FROM agent_ranking_snapshot WHERE generated_at<?", ((instant - timedelta(days=35)).isoformat(),))
            await db.commit()
        result["state"] = state
        result["rows"] = [
            {"display_rank": index + 1, "item": eligible[row["agent_id"]], **{key: value for key, value in row.items() if key != "agent_id"}}
            for index, row in enumerate(ordered)
        ]
        return result

    @staticmethod
    async def _critical_failure(db: aiosqlite.Connection, team_id: str, agent_id: str) -> bool:
        row = await (
            await db.execute(
                """SELECT 1 FROM team_subagent agent
               JOIN agent_eval_evidence_state evidence ON evidence.team_id=agent.team_id AND evidence.agent_id=agent.id
               JOIN agent_eval_run run ON run.id=evidence.current_run_id
               WHERE agent.team_id=? AND agent.id=? AND evidence.agent_source_kind='team'
                 AND run.revision_ref=agent.live_revision_id AND run.release_id=agent.live_release_id
                 AND json_extract(run.summary_json,'$.critical_failure')=1 LIMIT 1""",
                (team_id, agent_id),
            )
        ).fetchone()
        return row is not None


_QUALIFIED_USAGE_SQL = """
SELECT DISTINCT execution.agent_id,binding.user_id,date(execution.ended_at) AS usage_day
FROM turn_agent_binding binding
JOIN turn_agent_execution execution ON execution.turn_id=binding.turn_id AND execution.team_id=binding.team_id
  AND execution.agent_id=binding.requested_agent_id AND execution.agent_source_kind=binding.agent_source_kind
  AND execution.revision_ref=binding.revision_ref AND execution.release_id IS binding.release_id
  AND execution.capability_fingerprint=binding.capability_fingerprint AND execution.policy_fingerprint=binding.policy_fingerprint
  AND (binding.primary_execution_id IS NULL OR binding.primary_execution_id=execution.id)
JOIN turns turn ON turn.id=binding.turn_id
JOIN sessions session ON session.id=turn.session_id AND session.user_id=binding.user_id
JOIN workspaces space ON space.id=session.workspace_id AND space.team_id=binding.team_id
JOIN chat_turn_user_sources source ON source.turn_id=turn.id AND source.user_id=binding.user_id
  AND source.session_id=session.id AND source.workspace_id=space.id
JOIN team_members member ON member.team_id=binding.team_id AND member.user_id=binding.user_id
JOIN users actor ON actor.id=member.user_id
LEFT JOIN team_subagent_revision revision ON binding.agent_source_kind='team' AND revision.id=binding.revision_ref
WHERE binding.team_id=:team AND execution.team_id=:team AND execution.parent_execution_id IS NULL
  AND binding.binding_source IN ('marketplace','detail','hr','mention','plan','lead')
  AND execution.dispatch_source IN ('explicit','lead','plan')
  AND session.origin='normal' AND turn.status='completed' AND execution.status='completed'
  AND member.status='active' AND actor.status='active'
  AND (revision.id IS NULL OR revision.created_by!=binding.user_id)
  AND julianday(member.created_at)<=julianday(binding.created_at)
  AND julianday(actor.created_at)<=julianday(binding.created_at)
  AND julianday(execution.ended_at)>=julianday(:start) AND julianday(execution.ended_at)<julianday(:end)
"""
