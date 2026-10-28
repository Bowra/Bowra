"""Bounded Owner evidence projections; reads never schedule or certify execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_context import digest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError
from bowhead_core.agent_eval_plan_store import _workspace_scope
from bowhead_core.agent_eval_quality import answer_quality
from bowhead_core.agent_version_service import read_agent_version_workbench_conn, release_check_outcome_conn, release_tab_path
from bowhead_core.scrub import scrub
from bowhead_core.scrubber import scrub as scrub_pii
from bowhead_core.secret_redaction import redact_secrets


def _excerpt(value: Any) -> str:
    # Raw outputs, inputs, fixture bytes and trace locations are never selected.
    # The shared scrubbers add best-effort protection to bounded scorer excerpts.
    text = str(value)[:4000]
    text = re.sub(r"https?://[^\s<>\"']+", "[URL omitted]", text)
    return scrub_pii(redact_secrets(scrub(text))[0])[:280]


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return {"content_status": "depth_limit"}
    if isinstance(value, dict):
        return {
            str(key)[:100]: item
            if key in {"case_revision_id", "suite_revision_id"} and isinstance(item, str) and re.fullmatch(r"ae[cv]_[A-Za-z0-9_-]{1,150}", item)
            else _bounded(item, depth=depth + 1)
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return _excerpt(value)
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _json(value: str | None) -> dict:
    parsed = json.loads(value or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# COALESCE: a case without the key is visible; NOT (NULL=1) would hide it.
_HELD_OUT_CASE = "COALESCE(json_extract(c.expected_contract_json,'$.held_out'),0)=1"


async def _held_out_results(db: aiosqlite.Connection, *, run_ids: list[str]) -> dict[str, int]:
    """Counts of held-out attempts in runs: how many there are and how many passed. Nothing else."""
    marks = ",".join("?" for _ in run_ids)
    cursor = await db.execute(
        f"""SELECT count(DISTINCT r.case_revision_id) AS cases,count(*) AS attempts,
                  coalesce(sum(CASE WHEN r.status='passed' THEN 1 ELSE 0 END),0) AS passed
           FROM agent_eval_case_result r JOIN agent_eval_case_revision c ON c.id=r.case_revision_id
           WHERE r.run_id IN ({marks}) AND {_HELD_OUT_CASE}""",
        tuple(run_ids),
    )
    row = await cursor.fetchone()
    return {"cases": int(row["cases"] or 0), "attempts": int(row["attempts"] or 0), "passed_attempts": int(row["passed"] or 0)}


async def _held_out_case_ids(db: aiosqlite.Connection, case_revision_ids: list[str]) -> set[str]:
    if not case_revision_ids:
        return set()
    cursor = await db.execute(
        f"SELECT c.id FROM agent_eval_case_revision c WHERE c.id IN (SELECT value FROM json_each(?)) AND {_HELD_OUT_CASE}",
        (json.dumps(case_revision_ids),),
    )
    return {str(row[0]) for row in await cursor.fetchall()}


def _held_out_classes(comparisons: Any, hidden: set[str]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in comparisons if isinstance(comparisons, list) else []:
        if isinstance(item, dict) and item.get("case_revision_id") in hidden:
            key = str(item.get("classification") or "unknown")[:40]
            counts[key] = counts.get(key, 0) + 1
    return {"cases": len(hidden), "classifications": counts}


def _page_check(expected: str | None, actual: str, offset: int) -> None:
    if (offset and expected is None) or (expected is not None and expected != actual):
        raise AgentChangeProposalError("agent_eval_evidence_page_changed")


@dataclass(frozen=True)
class AgentCoachEvidence:
    db_path: str
    team_id: str
    actor_user_id: str
    authorize_owner: Callable[[aiosqlite.Connection, str], Awaitable[None]]
    reauthorize: Callable[[str], Awaitable[None]]

    async def get(self, *, target: dict, offset: int, limit: int, fingerprint: str | None, include_failure_excerpts: bool) -> dict:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await self.authorize_owner(db, target["agent_id"])
            if target["kind"] == "agent_eval_overview":
                result = await self._overview(db, target, offset, limit, fingerprint)
            elif target["kind"] == "agent_eval_run":
                result = await self._run(db, target, offset, limit, fingerprint, include_failure_excerpts)
            else:
                result = await self._snapshot(db, target, offset, limit, fingerprint)
        await self.reauthorize(target["agent_id"])
        result.update(
            {
                "ok": True,
                "target": target,
                "content_role": "untrusted_evaluation_evidence",
                "executed_by_this_call": False,
                "release_authorized": False,
                "applicability_limit": "Recorded identities and current stored state; release admission rechecks live dependencies, permissions and models.",
            }
        )
        if len(json.dumps(result, ensure_ascii=False).encode()) > 96 * 1024:
            raise AgentChangeProposalError("agent_eval_evidence_page_too_large")
        return result

    async def _overview(self, db, target, offset, limit, expected):
        team_id = self.team_id
        marker = await db.execute(
            """SELECT count(*) AS count,max(id) AS last_id,max(finished_at) AS finished_at,
                      sum(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                      sum(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                      (SELECT count(*) FROM agent_eval_snapshot WHERE team_id=? AND agent_id=?) AS snapshot_count
               FROM agent_eval_run WHERE team_id=? AND agent_id=? AND agent_source_kind='team'""",
            (team_id, target["agent_id"], team_id, target["agent_id"]),
        )
        state = dict(await marker.fetchone())
        token = _fingerprint({"team_id": team_id, "agent_id": target["agent_id"], "state": state})
        _page_check(expected, token, offset)
        cursor = await db.execute(
            """SELECT r.id,r.status,r.suite_revision_id,r.candidate_id,r.release_id,r.created_at,
                      l.execution_id,l.subject_side,s.id AS snapshot_id,s.snapshot_hash
               FROM agent_eval_run r LEFT JOIN agent_eval_comparison_run l ON l.run_id=r.id
               LEFT JOIN agent_eval_snapshot s ON s.execution_id=l.execution_id
               WHERE r.team_id=? AND r.agent_id=? AND r.agent_source_kind='team'
               ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?""",
            (team_id, target["agent_id"], limit + 1, offset),
        )
        rows = await cursor.fetchall()
        workbench = await read_agent_version_workbench_conn(db, team_id=self.team_id, agent_id=target["agent_id"], actor_user_id=self.actor_user_id)
        return {
            "workbench": workbench,
            "release_check": await self._release_check(db, target["agent_id"], workbench=workbench),
            "items": [
                {
                    **dict(row),
                    "target": {"kind": "agent_eval_run", "agent_id": target["agent_id"], "run_id": row["id"]},
                    "snapshot_target": {"kind": "agent_eval_snapshot", "agent_id": target["agent_id"], "snapshot_id": row["snapshot_id"]}
                    if row["snapshot_id"]
                    else None,
                }
                for row in rows[:limit]
            ],
            "fingerprint": token,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    async def _run(self, db, target, offset, limit, expected, excerpts):
        cursor = await db.execute(
            "SELECT * FROM agent_eval_run WHERE id=? AND team_id=? AND agent_id=? AND agent_source_kind='team'",
            (target["run_id"], self.team_id, target["agent_id"]),
        )
        run = await cursor.fetchone()
        if run is None:
            raise AgentChangeProposalError("agent_eval_run_not_found")
        cursor = await db.execute(
            """SELECT count(*) AS recorded_attempts,coalesce(sum(cost_microusd),0) AS known_cost_microusd,
                      sum(CASE WHEN cost_microusd IS NULL THEN 1 ELSE 0 END) AS unknown_cost_attempts,
                      max(created_at) AS last_result_at
               FROM agent_eval_case_result WHERE run_id=?""",
            (run["id"],),
        )
        totals = dict(await cursor.fetchone())
        cursor = await db.execute("SELECT count(*) FROM agent_eval_case_job WHERE run_id=?", (run["id"],))
        planned = int((await cursor.fetchone())[0])
        totals["expected_attempts"] = planned or None
        totals["unfinished_or_unrecorded_attempts"] = max(0, planned - totals["recorded_attempts"])
        totals["unknown_cost_attempts"] = int(totals["unknown_cost_attempts"] or 0)
        totals["total_cost_microusd"] = (
            totals["known_cost_microusd"]
            if planned > 0
            and totals["recorded_attempts"]
            and not totals["unknown_cost_attempts"]
            and not totals["unfinished_or_unrecorded_attempts"]
            and run["status"] not in {"queued", "running"}
            else None
        )
        token = _fingerprint({"projection": "answer_quality_v1", "run_id": run["id"], "status": run["status"], "totals": totals})
        _page_check(expected, token, offset)
        held_out = await _held_out_results(db, run_ids=[run["id"]])
        # Held-out cases reach Coach only as counts (alignment loop A6); their rows are never listed.
        cursor = await db.execute(
            f"""SELECT r.*,c.case_key,c.expected_contract_json FROM agent_eval_case_result r JOIN agent_eval_case_revision c ON c.id=r.case_revision_id
               WHERE r.run_id=? AND NOT ({_HELD_OUT_CASE}) ORDER BY c.case_key,r.case_revision_id,r.attempt LIMIT ? OFFSET ?""",
            (run["id"], limit + 1, offset),
        )
        rows = await cursor.fetchall()
        items = []
        for row in rows[:limit]:
            scores = _json(row["deterministic_json"])
            scorers = []
            for item in scores.get("scorers", [])[:16]:
                if not isinstance(item, dict):
                    continue
                scorer = {key: _bounded(item.get(key)) for key in ("name", "passed", "critical", "score", "advisory", "applicable")}
                if excerpts and item.get("passed") is False:
                    scorer["failure_excerpt"] = _excerpt(item.get("detail", ""))
                scorers.append(scorer)
            items.append(
                {
                    "case_revision_id": row["case_revision_id"],
                    "case_key": row["case_key"],
                    "attempt": row["attempt"],
                    "status": row["status"],
                    "duration_ms": row["duration_ms"],
                    "cost_microusd": row["cost_microusd"],
                    "failure_code": _excerpt(row["failure_code"]) if row["failure_code"] else None,
                    "passed": scores.get("passed"),
                    "critical_failure": scores.get("critical_failure"),
                    "scorers": scorers,
                    "judge": {key: _bounded(_json(row["judge_json"]).get(key)) for key in ("status", "passed", "score")},
                    "quality": _bounded(answer_quality(row["expected_contract_json"], row["judge_json"])),
                    "raw_trace": "not_exposed",
                }
            )
        cursor = await db.execute(
            """SELECT e.state,e.current_run_id,e.stale_reason FROM agent_eval_evidence_state e
               JOIN agent_eval_suite_revision r ON r.suite_id=e.suite_id
               WHERE r.id=? AND e.team_id=? AND e.agent_id=? AND e.agent_source_kind='team'""",
            (run["suite_revision_id"], run["team_id"], run["agent_id"]),
        )
        evidence = await cursor.fetchone()
        # A comparison run is one side of a release comparison; the outcome is in release_check.
        cursor = await db.execute("SELECT execution_id,subject_side FROM agent_eval_comparison_run WHERE run_id=?", (run["id"],))
        link = await cursor.fetchone()
        return {
            "comparison": {"execution_id": link["execution_id"], "subject_side": link["subject_side"]} if link else None,
            "release_check": await self._release_check(db, target["agent_id"]) if link else None,
            "identity": {
                key: run[key]
                for key in (
                    "id",
                    "revision_ref",
                    "release_id",
                    "candidate_id",
                    "suite_revision_id",
                    "subject_fingerprint",
                    "case_set_hash",
                    "model_config_hash",
                    "dependency_fingerprint",
                    "scorer_bundle_ref",
                )
            },
            "status": run["status"],
            "recorded_evidence": dict(evidence) if evidence else None,
            "applicability": "recorded_current"
            if evidence and evidence["current_run_id"] == run["id"] and evidence["state"] == "valid"
            else "historical_or_incomplete",
            "cost": totals,
            "items": items,
            "held_out": held_out,
            "fingerprint": token,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    async def _snapshot(self, db, target, offset, limit, expected):
        cursor = await db.execute(
            """SELECT s.*,c.active_snapshot_id,c.active_plan_id,c.readiness AS current_readiness,c.reason_codes_json,
                      c.active_execution_id,c.stale_subject,p.plan_hash,p.policy_revision_id,p.execution_workspace_id,p.execution_workspace_scope_hash,
                      a.state AS candidate_state,p.candidate_hash AS plan_candidate_hash,a.candidate_hash,
                      (SELECT id FROM agent_eval_policy_revision WHERE team_id=s.team_id AND agent_id=s.agent_id ORDER BY sequence DESC LIMIT 1) AS latest_policy_id
               FROM agent_eval_snapshot s JOIN agent_eval_candidate_control c ON c.candidate_id=s.candidate_id
               JOIN agent_eval_plan p ON p.id=s.plan_id
               JOIN team_subagent_release_candidate a ON a.id=s.candidate_id
               WHERE s.id=? AND s.team_id=? AND s.agent_id=?""",
            (target["snapshot_id"], self.team_id, target["agent_id"]),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AgentChangeProposalError("agent_eval_snapshot_not_found")
        token = row["snapshot_hash"]
        _page_check(expected, token, offset)
        summary = _json(row["summary_json"])
        hidden = await _held_out_case_ids(
            db, [str(item.get("case_revision_id")) for item in summary.get("case_comparisons", []) if isinstance(item, dict)]
        )
        # Held-out cases reach Coach only as counts (alignment loop A6).
        comparisons = [
            item for item in summary.get("case_comparisons", []) if not (isinstance(item, dict) and item.get("case_revision_id") in hidden)
        ]
        held_out = _held_out_classes(summary.get("case_comparisons", []), hidden)
        current = (
            row["active_snapshot_id"] == row["id"]
            and row["active_plan_id"] == row["plan_id"]
            and row["active_execution_id"] is None
            and row["current_readiness"] == row["readiness"]
            and row["stale_subject"] is None
        )
        scope = await _workspace_scope(db, team_id=self.team_id, workspace_id=row["execution_workspace_id"], actor_user_id=self.actor_user_id)
        reasons = []
        if row["candidate_state"] not in {"prepared", "pending", "approved"}:
            reasons.append("candidate_not_active")
        if row["plan_candidate_hash"] != row["candidate_hash"]:
            reasons.append("candidate_changed")
        if row["policy_revision_id"] != row["latest_policy_id"]:
            reasons.append("evaluation_policy_changed")
        if scope is None or digest(scope) != row["execution_workspace_scope_hash"]:
            reasons.append("evaluation_space_changed")
        current = current and not reasons
        cost = await self._comparison_cost(db, row["execution_id"])
        items = []
        for item in comparisons[offset : offset + limit]:
            value = _bounded(item)
            item_cost = await self._comparison_cost(db, row["execution_id"], case_id=item["case_revision_id"])
            value["cost_microusd"] = item_cost["total_cost_microusd"]
            value["known_cost_microusd"] = item_cost["known_cost_microusd"]
            items.append(value)
        metrics = _bounded(summary.get("aggregate_metrics", {}))
        metrics["cost_microusd"] = cost["total_cost_microusd"]
        return {
            "release_check": await self._release_check(db, target["agent_id"]),
            "identity": {
                key: row[key]
                for key in (
                    "id",
                    "candidate_id",
                    "plan_id",
                    "plan_hash",
                    "policy_revision_id",
                    "execution_id",
                    "identity_hash",
                    "snapshot_hash",
                    "created_at",
                )
            },
            "recorded_readiness": row["readiness"],
            "current_readiness": row["current_readiness"],
            "applicability": "recorded_current" if current else "historical_or_stale",
            "current_restrictions": reasons,
            "aggregate_metrics": metrics,
            "cost": cost,
            "coverage_gaps": _bounded(summary.get("coverage_gaps", [])),
            "reason_codes": _bounded(summary.get("reason_codes", [])),
            "items": items,
            "held_out": held_out,
            "case_count": len(comparisons),
            "fingerprint": token,
            "next_offset": offset + limit if offset + limit < len(comparisons) else None,
        }

    async def _release_check(self, db, agent_id: str, *, workbench: dict | None = None) -> dict:
        """The release outcome the Owner sees on the Release tab; Coach reports it as is and points the Owner there."""
        outcome = await release_check_outcome_conn(db, team_id=self.team_id, agent_id=agent_id, actor_user_id=self.actor_user_id, workbench=workbench)
        return {**outcome, "owner_surface": "release_tab", "owner_path": release_tab_path(agent_id)}

    async def _comparison_cost(self, db, execution_id, case_id=None):
        cursor = await db.execute(
            """SELECT count(*) AS recorded_attempts,coalesce(sum(r.cost_microusd),0) AS known_cost_microusd,
                      sum(CASE WHEN r.cost_microusd IS NULL THEN 1 ELSE 0 END) AS unknown_cost_attempts
               FROM agent_eval_case_result r JOIN agent_eval_comparison_run l ON l.run_id=r.run_id
               WHERE l.execution_id=? AND (? IS NULL OR r.case_revision_id=?)""",
            (execution_id, case_id, case_id),
        )
        cost = dict(await cursor.fetchone())
        cursor = await db.execute(
            """SELECT count(*) FROM agent_eval_case_job j JOIN agent_eval_comparison_run l ON l.run_id=j.run_id
               WHERE l.execution_id=? AND (? IS NULL OR j.case_revision_id=?)""",
            (execution_id, case_id, case_id),
        )
        planned = int((await cursor.fetchone())[0])
        cost["expected_attempts"] = planned or None
        cost["unfinished_or_unrecorded_attempts"] = max(0, planned - cost["recorded_attempts"])
        cost["unknown_cost_attempts"] = int(cost["unknown_cost_attempts"] or 0)
        cost["total_cost_microusd"] = (
            cost["known_cost_microusd"]
            if planned > 0 and cost["recorded_attempts"] and not cost["unknown_cost_attempts"] and not cost["unfinished_or_unrecorded_attempts"]
            else None
        )
        return cost
