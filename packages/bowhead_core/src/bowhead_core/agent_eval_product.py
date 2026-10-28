"""M3-11 Agent Eval product projections, release gate, and regression outbox."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_quality import answer_quality, quality_summary
from bowhead_core.agent_eval_store import (
    AgentEvalStore,
    AgentEvalStoreError,
    EvalRunSubject,
)
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
)
from bowhead_core.notification_store import NotificationStore

RegressionStatus = Literal["pending", "leased", "delivered", "skipped"]
RegressionChangeKind = Literal[
    "definition_change",
    "dependency_change",
    "model_change",
]


@dataclass(frozen=True, slots=True)
class EvalSubjectTarget:
    subject: EvalRunSubject
    model_role: str
    target_kind: Literal["candidate", "live", "system"]


@dataclass(frozen=True, slots=True)
class EvalRegressionEvent:
    event_id: str
    team_id: str
    agent_source_kind: Literal["team", "system"]
    agent_id: str
    change_kind: RegressionChangeKind
    change_ref: str
    status: RegressionStatus
    lease_generation: int
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ClaimedEvalRegression:
    event: EvalRegressionEvent
    lease_token: str


@dataclass(frozen=True, slots=True)
class EvalFailureNotification:
    outbox_id: str
    team_id: str
    agent_id: str
    run_id: str
    recipient_user_id: str
    run_status: Literal["failed", "unknown", "incomplete", "cancelled"]
    trigger_kind: str
    critical_failure: bool
    lease_generation: int
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ClaimedEvalFailureNotification:
    notification: EvalFailureNotification
    lease_token: str


def case_on_old_defaults(scorer_config: Mapping[str, Any]) -> bool:
    """A zero Tool-call cap: the Owner-case default before 0f25f1f9b that still fails data Agents.

    The other old default, a critical `no_fabrication`, no longer decides a case (it is advisory),
    so it alone doesn't flag a case; updating a flagged case still drops it.
    """
    return scorer_config.get("max_tool_calls") == 0


class AgentEvalProductStore:
    """Permissioned read models and durable trigger consumption."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.eval_store = AgentEvalStore(self.db_path)

    async def resolve_team_subject(
        self,
        *,
        team_id: str,
        agent_id: str,
        model_config_hash: str,
        target: Literal["candidate", "live", "auto"] = "auto",
    ) -> EvalSubjectTarget:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = None
            if target in {"candidate", "auto"}:
                # Owner versioning evaluates prepared candidates before submission.
                # Keep this target aligned with manager_snapshot; never fall back
                # to Live merely because the candidate has not entered review.
                async with db.execute(
                    """
                    SELECT candidate.id AS candidate_id,NULL AS release_id,
                           candidate.revision_id,candidate.capability_fingerprint,
                           candidate.dependency_fingerprint,revision.definition_json
                    FROM team_subagent_release_candidate candidate
                    JOIN team_subagent_revision revision
                      ON revision.team_id=candidate.team_id
                     AND revision.subagent_id=candidate.subagent_id
                     AND revision.id=candidate.revision_id
                    WHERE candidate.team_id=? AND candidate.subagent_id=?
                      AND candidate.state IN ('prepared','pending','approved')
                    ORDER BY candidate.submitted_at DESC,candidate.id DESC LIMIT 1
                    """,
                    (team_id, agent_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if target == "candidate" and row is None:
                    raise AgentEvalStoreError("agent_eval_candidate_not_found")
            target_kind: Literal["candidate", "live", "system"] = "candidate"
            if row is None:
                async with db.execute(
                    """
                    SELECT NULL AS candidate_id,release.id AS release_id,
                           release.revision_id,release.capability_fingerprint,
                           release.dependency_fingerprint,revision.definition_json
                    FROM team_subagent agent
                    JOIN team_subagent_release release
                      ON release.team_id=agent.team_id
                     AND release.subagent_id=agent.id
                     AND release.id=agent.live_release_id
                    JOIN team_subagent_revision revision
                      ON revision.team_id=release.team_id
                     AND revision.subagent_id=release.subagent_id
                     AND revision.id=release.revision_id
                    WHERE agent.team_id=? AND agent.id=?
                    """,
                    (team_id, agent_id),
                ) as cursor:
                    row = await cursor.fetchone()
                target_kind = "live"
            if row is None:
                raise AgentEvalStoreError("agent_eval_run_subject_unavailable")
        definition = _object(row["definition_json"])
        role = definition.get("model")
        model_role = str(role) if isinstance(role, str) and role else "analyst"
        return EvalSubjectTarget(
            subject=EvalRunSubject(
                team_id=team_id,
                agent_id=agent_id,
                agent_source_kind="team",
                revision_ref=str(row["revision_id"]),
                release_id=(str(row["release_id"]) if row["release_id"] else None),
                candidate_id=(str(row["candidate_id"]) if row["candidate_id"] else None),
                capability_fingerprint=str(row["capability_fingerprint"]),
                dependency_fingerprint=str(row["dependency_fingerprint"]),
                model_config_hash=model_config_hash,
            ),
            model_role=model_role,
            target_kind=target_kind,
        )

    async def manager_snapshot(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_agent_owner(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
        await self.eval_store.ensure_platform_contract_suite(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_agent_owner(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            async with db.execute(
                """
                SELECT suite.*,revision.id AS suite_revision_id,
                       revision.sequence AS suite_revision_sequence,
                       revision.case_count,revision.created_at AS revision_created_at,
                       evidence.current_run_id,evidence.state AS evidence_state,
                       evidence.stale_reason,evidence.updated_at AS evidence_updated_at,
                       run.status AS run_status,run.summary_json,run.created_at AS run_created_at,
                       run.finished_at AS run_finished_at,
                       run.candidate_id AS run_candidate_id,
                       run.revision_ref AS run_revision_ref,
                       run.capability_fingerprint AS run_capability_fingerprint,
                       run.dependency_fingerprint AS run_dependency_fingerprint
                FROM agent_eval_suite suite
                LEFT JOIN agent_eval_suite_revision revision ON revision.id=(
                  SELECT latest.id FROM agent_eval_suite_revision latest
                  WHERE latest.suite_id=suite.id ORDER BY latest.sequence DESC LIMIT 1
                )
                LEFT JOIN agent_eval_evidence_state evidence
                  ON evidence.team_id=suite.team_id
                 AND evidence.agent_source_kind=suite.agent_source_kind
                 AND evidence.agent_id=suite.agent_id AND evidence.suite_id=suite.id
                LEFT JOIN agent_eval_run run ON run.id=evidence.current_run_id
                WHERE suite.team_id=? AND suite.agent_source_kind='team'
                  AND suite.agent_id=? AND suite.state!='archived'
                ORDER BY CASE suite.suite_kind WHEN 'platform_contract' THEN 0 ELSE 1 END,
                         suite.created_at,suite.id
                """,
                (team_id, agent_id),
            ) as cursor:
                suites = await cursor.fetchall()
            suite_items = []
            for suite in suites:
                cases: list[dict[str, Any]] = []
                if suite["suite_revision_id"] is not None:
                    async with db.execute(
                        """
                        SELECT id,case_key,input_json,expected_contract_json,
                               scorer_config_json,effect_policy_json
                        FROM agent_eval_case_revision
                        WHERE suite_revision_id=? ORDER BY case_key,id
                        """,
                        (suite["suite_revision_id"],),
                    ) as cursor:
                        case_rows = await cursor.fetchall()
                    cases = [
                        {
                            "case_key": str(case["case_key"]),
                            # Calibration examples are stored against the exact case revision (slice 5).
                            "case_revision_id": str(case["id"]),
                            "effect_policy": _object(case["effect_policy_json"]),
                            "expected_contract": _object(case["expected_contract_json"]),
                            "input": _object(case["input_json"]),
                            "scorer_config": _object(case["scorer_config_json"]),
                        }
                        for case in case_rows
                    ]
                suite_items.append(
                    {
                        "suite_id": str(suite["id"]),
                        "suite_kind": str(suite["suite_kind"]),
                        "name": str(suite["name"]),
                        "visibility": str(suite["visibility"]),
                        "state": str(suite["state"]),
                        "row_version": int(suite["row_version"]),
                        "platform_managed": bool(suite["platform_managed"]),
                        "revision": (
                            {
                                "suite_revision_id": str(suite["suite_revision_id"]),
                                "sequence": int(suite["suite_revision_sequence"]),
                                "case_count": int(suite["case_count"]),
                                "created_at": str(suite["revision_created_at"]),
                                "cases": cases,
                                # R18: Owner cases saved under the pre-2026-10-15 defaults, which fail
                                # data Agents for querying data. Never rewritten; the Owner may update them.
                                "cases_on_old_defaults": (
                                    [case["case_key"] for case in cases if case_on_old_defaults(case["scorer_config"])]
                                    if suite["suite_kind"] == "owner_quality"
                                    else []
                                ),
                            }
                            if suite["suite_revision_id"] is not None
                            else None
                        ),
                        "evidence": _evidence_projection(suite),
                    }
                )
            for suite_item in suite_items:
                evidence_run = suite_item["evidence"]["run"]
                if evidence_run is not None:
                    evidence_run["quality"] = await _run_quality_conn(db, evidence_run["run_id"])
            async with db.execute(
                """
                SELECT run.*,revision.suite_id
                FROM agent_eval_run run
                JOIN agent_eval_suite_revision revision ON revision.id=run.suite_revision_id
                WHERE run.team_id=? AND run.agent_source_kind='team' AND run.agent_id=?
                ORDER BY run.created_at DESC,run.id DESC LIMIT 30
                """,
                (team_id, agent_id),
            ) as cursor:
                runs = await cursor.fetchall()
            async with db.execute(
                """
                SELECT * FROM agent_eval_trigger_policy
                WHERE team_id=? AND agent_source_kind='team' AND agent_id=?
                """,
                (team_id, agent_id),
            ) as cursor:
                policy = await cursor.fetchone()
            async with db.execute(
                """
                SELECT id,revision_id,capability_fingerprint,dependency_fingerprint
                FROM team_subagent_release_candidate
                WHERE team_id=? AND subagent_id=? AND state IN ('prepared','pending','approved')
                ORDER BY submitted_at DESC,id DESC LIMIT 1
                """,
                (team_id, agent_id),
            ) as cursor:
                candidate = await cursor.fetchone()
            async with db.execute("SELECT live_revision_id FROM team_subagent WHERE team_id=? AND id=?", (team_id, agent_id)) as cursor:
                live = await cursor.fetchone()
        return {
            "kind": "agent_eval_manager",
            "schema_version": 1,
            "agent_id": agent_id,
            "target": (
                {
                    "kind": "candidate",
                    "candidate_id": str(candidate["id"]),
                    "revision_id": str(candidate["revision_id"]),
                }
                if candidate is not None
                else {"kind": "live", "revision_id": live["live_revision_id"] if live else None}
            ),
            "suites": suite_items,
            "runs": [_run_projection(run) for run in runs],
            "trigger_policy": (
                {
                    "enabled": bool(policy["enabled"]),
                    "on_definition_change": bool(policy["on_definition_change"]),
                    "on_dependency_change": bool(policy["on_dependency_change"]),
                    "on_model_change": bool(policy["on_model_change"]),
                    "row_version": int(policy["row_version"]),
                }
                if policy is not None
                else None
            ),
            "release_gate": _release_gate_projection(suite_items, candidate),
        }

    async def release_snapshot(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        """Return only platform-managed evidence safe for release review."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_release_observer(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
        await self.eval_store.ensure_platform_contract_suite(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_release_observer(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            async with db.execute(
                """
                SELECT suite.*,revision.id AS suite_revision_id,
                       revision.sequence AS suite_revision_sequence,
                       revision.case_count,revision.created_at AS revision_created_at,
                       evidence.current_run_id,evidence.state AS evidence_state,
                       evidence.stale_reason,evidence.updated_at AS evidence_updated_at,
                       run.status AS run_status,run.summary_json,run.created_at AS run_created_at,
                       run.finished_at AS run_finished_at,
                       run.candidate_id AS run_candidate_id,
                       run.revision_ref AS run_revision_ref,
                       run.capability_fingerprint AS run_capability_fingerprint,
                       run.dependency_fingerprint AS run_dependency_fingerprint
                FROM agent_eval_suite suite
                LEFT JOIN agent_eval_suite_revision revision ON revision.id=(
                  SELECT latest.id FROM agent_eval_suite_revision latest
                  WHERE latest.suite_id=suite.id ORDER BY latest.sequence DESC LIMIT 1
                )
                LEFT JOIN agent_eval_evidence_state evidence
                  ON evidence.team_id=suite.team_id
                 AND evidence.agent_source_kind=suite.agent_source_kind
                 AND evidence.agent_id=suite.agent_id AND evidence.suite_id=suite.id
                LEFT JOIN agent_eval_run run ON run.id=evidence.current_run_id
                WHERE suite.team_id=? AND suite.agent_source_kind='team'
                  AND suite.agent_id=? AND suite.suite_kind='platform_contract'
                  AND suite.platform_managed=1 AND suite.state='active'
                ORDER BY suite.created_at,suite.id
                """,
                (team_id, agent_id),
            ) as cursor:
                suites = await cursor.fetchall()
            async with db.execute(
                """
                SELECT id,revision_id,capability_fingerprint,dependency_fingerprint
                FROM team_subagent_release_candidate
                WHERE team_id=? AND subagent_id=? AND id=?
                """,
                (team_id, agent_id, candidate_id),
            ) as cursor:
                candidate = await cursor.fetchone()
        if candidate is None:
            raise AgentEvalStoreError("agent_eval_candidate_not_found")
        suite_items = [
            {
                "suite_id": str(suite["id"]),
                "suite_kind": str(suite["suite_kind"]),
                "name": str(suite["name"]),
                "visibility": str(suite["visibility"]),
                "state": str(suite["state"]),
                "row_version": int(suite["row_version"]),
                "platform_managed": True,
                "revision": (
                    {
                        "suite_revision_id": str(suite["suite_revision_id"]),
                        "sequence": int(suite["suite_revision_sequence"]),
                        "case_count": int(suite["case_count"]),
                        "created_at": str(suite["revision_created_at"]),
                        "cases": [],
                    }
                    if suite["suite_revision_id"] is not None
                    else None
                ),
                "evidence": _review_evidence_projection(suite),
            }
            for suite in suites
        ]
        return {
            "kind": "agent_eval_release_review",
            "schema_version": 1,
            "release_gate": _release_gate_projection(suite_items, candidate),
            "suites": suite_items,
        }

    async def manager_run(
        self,
        *,
        team_id: str,
        agent_id: str,
        run_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_agent_owner(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            async with db.execute(
                """
                SELECT run.*,revision.suite_id
                FROM agent_eval_run run
                JOIN agent_eval_suite_revision revision ON revision.id=run.suite_revision_id
                WHERE run.id=? AND run.team_id=? AND run.agent_source_kind='team'
                  AND run.agent_id=?
                """,
                (run_id, team_id, agent_id),
            ) as cursor:
                run = await cursor.fetchone()
            if run is None:
                raise AgentEvalStoreError("agent_eval_run_not_found")
            async with db.execute(
                """
                SELECT result.case_revision_id,case_revision.case_key,result.attempt,
                       result.status,result.deterministic_json,result.judge_json,
                       result.duration_ms,result.cost_microusd,result.failure_code,case_revision.expected_contract_json,
                       case_revision.input_json
                FROM agent_eval_case_result result
                JOIN agent_eval_case_revision case_revision
                  ON case_revision.id=result.case_revision_id
                WHERE result.run_id=?
                ORDER BY case_revision.case_key,result.attempt
                """,
                (run_id,),
            ) as cursor:
                results = await cursor.fetchall()
            quality = await _run_quality_conn(db, run_id)
        value = _run_projection(run)
        value["results"] = [
            {
                "case_revision_id": str(result["case_revision_id"]),
                "case_key": str(result["case_key"]),
                "task": _object(result["input_json"]).get("task"),
                "quality": answer_quality(result["expected_contract_json"], result["judge_json"]),
                "attempt": int(result["attempt"]),
                "status": str(result["status"]),
                "deterministic": _object(result["deterministic_json"]),
                "judge": (_object(result["judge_json"]) if result["judge_json"] else None),
                "duration_ms": result["duration_ms"],
                "cost_microusd": result["cost_microusd"],
                "failure_code": result["failure_code"],
            }
            for result in results
        ]
        value["quality"] = quality
        return value

    async def latest_live_answer(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        task: str,
    ) -> dict[str, Any] | None:
        """The newest settled answer of the Live release to exactly this task, for the Owner only.

        Matching the task text rather than the case key keeps a renumbered or edited case from
        borrowing another question's answer.
        """

        answers = await self.live_answers(team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, task=task, limit=1)
        return answers[0] if answers else None

    async def live_answers(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        task: str | None = None,
        limit: int = 20,
        exclude_held_out: bool = False,
    ) -> list[dict[str, Any]]:
        """The newest settled Live-release answers, newest first, for the Owner only.

        With `task`, only answers to exactly that task. Each item carries the case task it answered,
        so the alignment loop can grade it against the right question (alignment loop §5).
        """

        from bowhead_core.agent_eval_insights import _bounded_answer
        from bowhead_core.agent_eval_point_calibration import attempt_answer_ref

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_agent_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            async with db.execute(
                """
                SELECT result.run_id,result.case_revision_id,result.attempt,result.trace_evidence_ref,
                       result.created_at,turn.final_answer,json_extract(case_revision.input_json,'$.task') AS task,
                       COALESCE(json_extract(case_revision.expected_contract_json,'$.held_out'),0)=1 AS held_out,
                       suite.suite_kind AS suite_kind
                FROM team_subagent agent
                JOIN agent_eval_run run
                  ON run.team_id=agent.team_id AND run.agent_source_kind='team' AND run.agent_id=agent.id
                 AND run.candidate_id IS NULL AND run.release_id=agent.live_release_id
                JOIN agent_eval_case_result result ON result.run_id=run.id
                JOIN agent_eval_case_revision case_revision ON case_revision.id=result.case_revision_id
                JOIN agent_eval_suite_revision suite_revision ON suite_revision.id=case_revision.suite_revision_id
                JOIN agent_eval_suite suite ON suite.id=suite_revision.suite_id
                JOIN agent_eval_scratch_turn scratch
                  ON result.trace_evidence_ref LIKE 'turn:%' AND scratch.turn_id=substr(result.trace_evidence_ref,6)
                 AND scratch.run_id=result.run_id AND scratch.case_revision_id=result.case_revision_id
                 AND scratch.attempt=result.attempt
                JOIN turns turn ON turn.id=scratch.turn_id
                WHERE agent.team_id=? AND agent.id=? AND agent.live_release_id IS NOT NULL
                  AND result.status IN ('passed','failed','judge_unknown')
                  AND (? IS NULL OR json_extract(case_revision.input_json,'$.task')=?)
                  AND (?=0 OR COALESCE(json_extract(case_revision.expected_contract_json,'$.held_out'),0)!=1)
                  AND turn.final_answer IS NOT NULL AND turn.final_answer!=''
                ORDER BY result.created_at DESC,result.attempt DESC
                LIMIT ?
                """,
                (team_id, agent_id, task, task, int(exclude_held_out), max(1, min(limit, 50))),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "run_id": str(row["run_id"]),
                "case_revision_id": str(row["case_revision_id"]),
                "attempt": int(row["attempt"]),
                # Calibration examples address an evaluation answer by its attempt, not by its chat turn.
                "answer_ref": attempt_answer_ref(
                    run_id=str(row["run_id"]), case_revision_id=str(row["case_revision_id"]), attempt=int(row["attempt"])
                ),
                "answered_at": str(row["created_at"]),
                "task": row["task"] if isinstance(row["task"], str) else "",
                "held_out": bool(row["held_out"]),
                # owner_quality: the Owner's own case; platform_contract: the platform's managed contract case.
                "suite_kind": str(row["suite_kind"]),
                **_bounded_answer(row["final_answer"]),
            }
            for row in rows
        ]

    async def consumer_evidence(
        self,
        *,
        team_id: str,
        agent_id: str,
        agent_source_kind: Literal["team", "system"],
    ) -> dict[str, Any]:
        if agent_source_kind == "system":
            # System Agent Eval needs the M4 governance owner and effective-
            # revision change producer. Until both exist, do not present core
            # test rows as current consumer evidence.
            return {
                "kind": "agent_quality_evidence",
                "schema_version": 1,
                "status": "missing",
                "suites": [],
            }
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT suite.id,suite.suite_kind,suite.name,evidence.state,
                       evidence.stale_reason,evidence.updated_at,run.summary_json,
                       run.finished_at,run.revision_ref,run.release_id,run.candidate_id,
                       run.capability_fingerprint,run.dependency_fingerprint
                FROM agent_eval_suite suite
                LEFT JOIN agent_eval_evidence_state evidence
                  ON evidence.team_id=suite.team_id
                 AND evidence.agent_source_kind=suite.agent_source_kind
                 AND evidence.agent_id=suite.agent_id AND evidence.suite_id=suite.id
                LEFT JOIN agent_eval_run run ON run.id=evidence.current_run_id
                WHERE suite.team_id=? AND suite.agent_source_kind=? AND suite.agent_id=?
                  AND suite.state='active' AND suite.visibility='team_evidence'
                ORDER BY CASE suite.suite_kind WHEN 'platform_contract' THEN 0 ELSE 1 END,
                         suite.created_at,suite.id
                """,
                (team_id, agent_source_kind, agent_id),
            ) as cursor:
                rows = await cursor.fetchall()
            live = None
            if agent_source_kind == "team":
                async with db.execute(
                    """
                    SELECT release.id,release.source_candidate_id,release.revision_id,
                           release.capability_fingerprint,release.dependency_fingerprint
                    FROM team_subagent agent
                    JOIN team_subagent_release release ON release.id=agent.live_release_id
                    WHERE agent.team_id=? AND agent.id=?
                    """,
                    (team_id, agent_id),
                ) as cursor:
                    live = await cursor.fetchone()
        items = []
        for row in rows:
            state = str(row["state"] or "missing")
            if state == "valid" and live is not None and not _run_matches_live(row, live):
                state = "stale"
            summary = _object(row["summary_json"]) if row["summary_json"] else {}
            items.append(
                {
                    "suite_kind": str(row["suite_kind"]),
                    "name": str(row["name"]),
                    "state": state,
                    "stale_reason": row["stale_reason"],
                    "sample_size": int(summary.get("case_count") or 0),
                    "attempts": int(summary.get("attempts") or 0),
                    "passed_cases": int(summary.get("pass@k_cases") or 0),
                    "flaky_cases": int(summary.get("flaky_cases") or 0),
                    "evaluated_at": row["finished_at"] or row["updated_at"],
                }
            )
        return {
            "kind": "agent_quality_evidence",
            "schema_version": 1,
            "status": _aggregate_evidence_status(items),
            "suites": items,
        }

    async def claim_regression(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> ClaimedEvalRegression | None:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        skipped_ids: list[str] = []
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Scan forward for the first pending item **not superseded by later changes**.
                #
                # Database triggers write the outbox: for `definition_change`/`dependency_change`,
                # `change_ref` is the then-current `live_release_id`; for `model_change`, it is
                # `'model:'||llm_config_version`. `UNIQUE(...)+INSERT OR IGNORE` prevents only
                # **identical** duplicates, not five queued items from five rapid releases; only the last matters.
                # Previously, this claimed `ORDER BY created_at,id LIMIT 1` directly, so:
                #   · N rapid changes ⇒ N real LLM evaluations, with N-1 spent on superseded versions;
                #   · One admin change to team model config ⇒ one item per subscribed Agent in the team;
                #   · Triggers can queue many obsolete revisions while processing is delayed.
                # The sole criterion: does `change_ref` still match current truth? If not, mark it `skipped`
                # (already supported when migration 0040 created the table), without any model calls.
                row = None
                while True:
                    async with db.execute(
                        """
                        SELECT * FROM agent_eval_regression_outbox
                        WHERE ((status='pending' AND julianday(available_at)<=julianday(?))
                            OR (status='leased' AND julianday(lease_expires_at)<=julianday(?)))
                          AND id NOT IN (SELECT value FROM json_each(?))
                        ORDER BY created_at,id LIMIT 1
                        """,
                        (now.isoformat(), now.isoformat(), json.dumps(skipped_ids)),
                    ) as cursor:
                        candidate = await cursor.fetchone()
                    if candidate is None:
                        break
                    if await _regression_change_is_current(db, candidate):
                        row = candidate
                        break
                    await db.execute(
                        """
                        UPDATE agent_eval_regression_outbox
                        SET status='skipped',outcome_code='superseded_change',
                            delivered_at=?,lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL
                        WHERE id=?
                        """,
                        (now.isoformat(), candidate["id"]),
                    )
                    skipped_ids.append(str(candidate["id"]))
                if row is None:
                    await db.commit()
                    return None
                generation = int(row["lease_generation"]) + 1
                await db.execute(
                    """
                    UPDATE agent_eval_regression_outbox
                    SET status='leased',lease_generation=?,lease_owner=?,lease_token_hash=?,
                        lease_expires_at=?,attempt_count=attempt_count+1,outcome_code=NULL
                    WHERE id=?
                    """,
                    (
                        generation,
                        worker_id,
                        token_hash,
                        (now + timedelta(seconds=lease_seconds)).isoformat(),
                        row["id"],
                    ),
                )
                async with db.execute(
                    "SELECT * FROM agent_eval_regression_outbox WHERE id=?",
                    (row["id"],),
                ) as cursor:
                    claimed = await cursor.fetchone()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if claimed is None:
            return None
        return ClaimedEvalRegression(_regression_from_row(claimed), token)

    async def settle_regression(
        self,
        claim: ClaimedEvalRegression,
        *,
        status: Literal["delivered", "skipped"],
        outcome_code: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            result = await db.execute(
                """
                UPDATE agent_eval_regression_outbox
                SET status=?,outcome_code=?,delivered_at=?
                WHERE id=? AND status='leased' AND lease_generation=?
                  AND lease_token_hash=?
                """,
                (
                    status,
                    outcome_code,
                    now,
                    claim.event.event_id,
                    claim.event.lease_generation,
                    _hash_token(claim.lease_token),
                ),
            )
            await db.commit()
        if result.rowcount != 1:
            raise AgentEvalStoreError("agent_eval_regression_lease_fenced")

    async def retry_regression(
        self,
        claim: ClaimedEvalRegression,
        *,
        outcome_code: str,
        delay_seconds: int,
    ) -> None:
        available_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            result = await db.execute(
                """
                UPDATE agent_eval_regression_outbox
                SET status='pending',outcome_code=?,available_at=?,lease_owner=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL
                WHERE id=? AND status='leased' AND lease_generation=?
                  AND lease_token_hash=?
                """,
                (
                    outcome_code,
                    available_at,
                    claim.event.event_id,
                    claim.event.lease_generation,
                    _hash_token(claim.lease_token),
                ),
            )
            await db.commit()
        if result.rowcount != 1:
            raise AgentEvalStoreError("agent_eval_regression_lease_fenced")

    async def claim_failure_notification(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> ClaimedEvalFailureNotification | None:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT outbox.*,run.status AS run_status,run.trigger_kind,
                           run.summary_json
                    FROM agent_eval_notification_outbox outbox
                    JOIN agent_eval_run run ON run.id=outbox.run_id
                    WHERE (outbox.status='pending'
                           AND julianday(outbox.available_at)<=julianday(?))
                       OR (outbox.status='leased'
                           AND julianday(outbox.lease_expires_at)<=julianday(?))
                    ORDER BY outbox.created_at,outbox.id LIMIT 1
                    """,
                    (now.isoformat(), now.isoformat()),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                generation = int(row["lease_generation"]) + 1
                updated = await db.execute(
                    """
                    UPDATE agent_eval_notification_outbox
                    SET status='leased',lease_generation=?,lease_owner=?,
                        lease_token_hash=?,lease_expires_at=?,
                        attempt_count=attempt_count+1,outcome_code=NULL
                    WHERE id=? AND (
                      (status='pending' AND julianday(available_at)<=julianday(?))
                      OR (status='leased' AND julianday(lease_expires_at)<=julianday(?))
                    )
                    """,
                    (
                        generation,
                        worker_id,
                        token_hash,
                        (now + timedelta(seconds=lease_seconds)).isoformat(),
                        row["id"],
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    await db.commit()
                    return None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        summary = _object(row["summary_json"]) if row["summary_json"] else {}
        return ClaimedEvalFailureNotification(
            notification=EvalFailureNotification(
                outbox_id=str(row["id"]),
                team_id=str(row["team_id"]),
                agent_id=str(row["agent_id"]),
                run_id=str(row["run_id"]),
                recipient_user_id=str(row["recipient_user_id"]),
                run_status=str(row["run_status"]),  # type: ignore[arg-type]
                trigger_kind=str(row["trigger_kind"]),
                critical_failure=summary.get("critical_failure") is True,
                lease_generation=generation,
                attempt_count=int(row["attempt_count"]) + 1,
            ),
            lease_token=token,
        )

    async def project_failure_notification(
        self,
        claim: ClaimedEvalFailureNotification,
        *,
        notification_store: NotificationStore,
    ) -> None:
        notification = claim.notification
        url = f"/agents/manage/?agent_id={notification.agent_id}"
        await notification_store.upsert_item_with_recipients(
            team_id=notification.team_id,
            category="action",
            type="agent.eval.regression_failed",
            priority="p1" if notification.critical_failure else "p2",
            title="Agent regression needs attention",
            body=("A version-bound automatic evaluation did not pass. Review the Agent and its current quality evidence."),
            subject_type="agent_eval_run",
            subject_id=notification.run_id,
            actor_user_id=None,
            action_url=url,
            actions=[{"id": "review", "label": "Review", "url": url}],
            metadata={
                "schema_version": 1,
                "agent_id": notification.agent_id,
                "run_id": notification.run_id,
                "run_status": notification.run_status,
                "trigger_kind": notification.trigger_kind,
                "critical_failure": notification.critical_failure,
            },
            dedupe_key=f"agent.eval.regression_failed:{notification.run_id}",
            recipients=[(notification.recipient_user_id, "owner")],
            reopen_resolved=False,
        )

    async def settle_failure_notification(
        self,
        claim: ClaimedEvalFailureNotification,
        *,
        status: Literal["delivered", "failed"],
        outcome_code: str,
    ) -> None:
        notification = claim.notification
        async with open_shared_ctx(self.db_path) as db:
            result = await db.execute(
                """
                UPDATE agent_eval_notification_outbox
                SET status=?,outcome_code=?,settled_at=?,lease_owner=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL
                WHERE id=? AND status='leased' AND lease_generation=?
                  AND lease_token_hash=?
                """,
                (
                    status,
                    outcome_code,
                    datetime.now(UTC).isoformat(),
                    notification.outbox_id,
                    notification.lease_generation,
                    _hash_token(claim.lease_token),
                ),
            )
            await db.commit()
        if result.rowcount != 1:
            raise AgentEvalStoreError("agent_eval_notification_lease_fenced")

    async def retry_failure_notification(
        self,
        claim: ClaimedEvalFailureNotification,
        *,
        outcome_code: str,
        delay_seconds: int,
    ) -> None:
        notification = claim.notification
        available_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            result = await db.execute(
                """
                UPDATE agent_eval_notification_outbox
                SET status='pending',outcome_code=?,available_at=?,lease_owner=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL
                WHERE id=? AND status='leased' AND lease_generation=?
                  AND lease_token_hash=?
                """,
                (
                    outcome_code,
                    available_at,
                    notification.outbox_id,
                    notification.lease_generation,
                    _hash_token(claim.lease_token),
                ),
            )
            await db.commit()
        if result.rowcount != 1:
            raise AgentEvalStoreError("agent_eval_notification_lease_fenced")

    async def latest_execution_for_suite(
        self,
        *,
        team_id: str,
        agent_id: str,
        suite_id: str,
    ) -> tuple[str, str, dict[str, int]] | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT execution.*,run.created_by AS run_created_by
                FROM agent_eval_run_execution execution
                JOIN agent_eval_run run ON run.id=execution.run_id
                JOIN agent_eval_suite_revision revision ON revision.id=run.suite_revision_id
                WHERE execution.team_id=? AND run.agent_id=? AND revision.suite_id=?
                ORDER BY execution.created_at DESC,execution.run_id DESC LIMIT 1
                """,
                (team_id, agent_id, suite_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return (
            str(row["workspace_id"]),
            str(row["run_created_by"]),
            {
                "attempts": int(row["requested_attempts"]),
                "max_concurrency": int(row["max_concurrency"]),
                "max_duration_ms": int(row["max_duration_ms"]),
                "max_cost_microusd": int(row["max_cost_microusd"]),
            },
        )


class AgentEvalReleaseGate:
    """Server-derived platform-contract gate for non-private activation."""

    async def prepare_for_activation(
        self,
        db_path: str,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> None:
        """Install the platform contract suite before the activation transaction.

        The suite is platform-authored and idempotent, so installing it here costs
        nothing and removes the only blocker an Owner cannot act on: "no suite
        exists" is the platform's job, not theirs.  What still gates publication is
        the real thing -- the suite has to have a passing run for this revision.
        """
        from bowhead_core.agent_eval_store import AgentEvalStore

        await AgentEvalStore(db_path).ensure_platform_contract_suite(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )

    async def evidence_for_activation(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: Mapping[str, Any],
        audience: AgentAudience,
    ) -> Mapping[str, Any] | None:
        if audience.mode == "private":
            return None
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT suite.id AS suite_id,revision.id AS suite_revision_id,
                   evidence.current_run_id,run.id AS matched_run_id,
                   run.status AS run_status,run.finished_at,run.summary_json
            FROM agent_eval_suite suite
            JOIN agent_eval_suite_revision revision ON revision.id=(
              SELECT latest.id FROM agent_eval_suite_revision latest
              WHERE latest.suite_id=suite.id ORDER BY latest.sequence DESC LIMIT 1
            )
            LEFT JOIN agent_eval_evidence_state evidence
              ON evidence.team_id=suite.team_id
             AND evidence.agent_source_kind=suite.agent_source_kind
             AND evidence.agent_id=suite.agent_id AND evidence.suite_id=suite.id
            LEFT JOIN agent_eval_run run
              ON run.id=evidence.current_run_id AND run.status IN ('passed','failed')
             AND run.suite_revision_id=revision.id
             AND run.candidate_id=? AND run.revision_ref=?
             AND run.capability_fingerprint=? AND run.dependency_fingerprint=?
            WHERE suite.team_id=? AND suite.agent_source_kind='team'
              AND suite.agent_id=? AND suite.suite_kind='platform_contract'
              AND suite.platform_managed=1 AND suite.state='active'
            ORDER BY suite.id
            """,
            (
                candidate["id"],
                candidate["revision_id"],
                candidate["capability_fingerprint"],
                candidate["dependency_fingerprint"],
                candidate["team_id"],
                candidate["subagent_id"],
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            raise AgentPublicationBlockedError(
                "agent_eval_platform_suite_missing",
                reason_codes=("platform_contract_missing",),
            )
        if any(row["matched_run_id"] is None or row["summary_json"] is None for row in rows):
            # Say so when the candidate's evidence is from an earlier platform contract: the fix is
            # simply to run the release check again, not to change the Agent.
            async with db.execute(
                """
                SELECT 1 FROM agent_eval_run run
                JOIN agent_eval_suite_revision older ON older.id=run.suite_revision_id
                JOIN agent_eval_suite_revision latest ON latest.suite_id=older.suite_id AND latest.id=?
                WHERE run.candidate_id=? AND run.status IN ('passed','failed') AND older.sequence<latest.sequence
                LIMIT 1
                """,
                (rows[0]["suite_revision_id"], candidate["id"]),
            ) as cursor:
                upgraded = await cursor.fetchone() is not None
            raise AgentPublicationBlockedError(
                "agent_eval_required",
                reason_codes=("platform_contract_upgraded" if upgraded else "platform_contract_not_current",),
            )
        evidence = []
        for row in rows:
            summary = _object(row["summary_json"])
            if summary.get("critical_failure") is not False:
                if summary.get("critical_failure") is True:
                    raise AgentPublicationBlockedError(
                        "agent_eval_critical_failure",
                        reason_codes=("platform_contract_critical_failure",),
                    )
                raise AgentPublicationBlockedError(
                    "agent_eval_required",
                    reason_codes=("platform_contract_summary_invalid",),
                )
            evidence.append(
                {
                    "suite_id": str(row["suite_id"]),
                    "suite_revision_id": str(row["suite_revision_id"]),
                    "run_id": str(row["matched_run_id"]),
                    "run_status": str(row["run_status"]),
                    "finished_at": str(row["finished_at"]),
                }
            )
        return {"kind": "agent_eval_release_evidence", "schema_version": 1, "runs": evidence}


async def _require_agent_owner(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM users actor
        JOIN team_members member
          ON member.user_id=actor.id AND member.team_id=? AND member.status='active'
        JOIN teams team ON team.id=? AND team.status='active'
        JOIN team_subagent agent
          ON agent.team_id=team.id AND agent.id=?
        WHERE actor.id=? AND actor.status='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (team_id, team_id, agent_id, actor_user_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise AgentEvalStoreError("agent_eval_action_denied")


async def _require_release_observer(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM users actor
        JOIN team_members member
          ON member.user_id=actor.id AND member.team_id=? AND member.status='active'
        JOIN teams team ON team.id=? AND team.status='active'
        JOIN team_subagent agent ON agent.team_id=team.id AND agent.id=?
        WHERE actor.id=? AND actor.status='active' AND (
          member.role='team_admin' OR team.created_by_user_id=actor.id
          OR COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        )
        """,
        (team_id, team_id, agent_id, actor_user_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise AgentEvalStoreError("agent_eval_action_denied")


def _run_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    summary = _object(row["summary_json"]) if row["summary_json"] else None
    return {
        "kind": "agent_eval_run",
        "schema_version": 1,
        "run_id": str(row["id"]),
        "suite_id": str(row["suite_id"]),
        "suite_revision_id": str(row["suite_revision_id"]),
        "target_kind": "candidate" if row["candidate_id"] else "live",
        "revision_id": str(row["revision_ref"]),
        "status": str(row["status"]),
        "trigger_kind": str(row["trigger_kind"]),
        "summary": summary,
        "created_at": str(row["created_at"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _evidence_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    summary = _object(row["summary_json"]) if row["summary_json"] else None
    return {
        "state": str(row["evidence_state"] or "missing"),
        "stale_reason": row["stale_reason"],
        "updated_at": row["evidence_updated_at"],
        "run": (
            {
                "run_id": str(row["current_run_id"]),
                "status": str(row["run_status"]),
                "summary": summary,
                "created_at": row["run_created_at"],
                "finished_at": row["run_finished_at"],
                "target": {
                    "candidate_id": row["run_candidate_id"],
                    "revision_id": row["run_revision_ref"],
                    "capability_fingerprint": row["run_capability_fingerprint"],
                    "dependency_fingerprint": row["run_dependency_fingerprint"],
                },
            }
            if row["current_run_id"] is not None
            else None
        ),
    }


def _review_evidence_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _evidence_projection(row)
    run = evidence["run"]
    if run is None or run["summary"] is None:
        return evidence
    summary = run["summary"]
    allowed = (
        "attempts",
        "case_count",
        "completed_results",
        "cost_microusd",
        "critical_failure",
        "duration_ms",
        "flaky_cases",
        "infra_failures",
        "judge_unknown",
        "pass@k_cases",
        "pass^k_cases",
        "schema_version",
        "stop_reason",
        "time_limit_failures",
    )
    run["summary"] = {key: summary[key] for key in allowed if key in summary}
    return evidence


def _release_gate_projection(
    suites: list[dict[str, Any]],
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contracts = [suite for suite in suites if suite["suite_kind"] == "platform_contract" and suite["platform_managed"]]
    current = (
        bool(contracts)
        and candidate is not None
        and all(
            suite["evidence"]["state"] in {"valid", "failed"}
            and suite["evidence"]["run"] is not None
            and suite["evidence"]["run"]["status"] in {"passed", "failed"}
            and suite["evidence"]["run"]["summary"] is not None
            and suite["evidence"]["run"]["summary"].get("critical_failure") is False
            and str(suite["evidence"]["run"]["target"]["candidate_id"] or "") == str(candidate["id"])
            and str(suite["evidence"]["run"]["target"]["revision_id"] or "") == str(candidate["revision_id"])
            and str(suite["evidence"]["run"]["target"]["capability_fingerprint"] or "") == str(candidate["capability_fingerprint"])
            and str(suite["evidence"]["run"]["target"]["dependency_fingerprint"] or "") == str(candidate["dependency_fingerprint"])
            for suite in contracts
        )
    )
    has_candidate = candidate is not None
    return {
        "required": has_candidate,
        "ready": not has_candidate or current,
        "reason": None if not has_candidate or current else "platform_contract_not_current",
    }


def _run_matches_live(run: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    return (
        str(run["revision_ref"]) == str(live["revision_id"])
        and str(run["capability_fingerprint"]) == str(live["capability_fingerprint"])
        and str(run["dependency_fingerprint"]) == str(live["dependency_fingerprint"])
        and (str(run["release_id"] or "") == str(live["id"]) or str(run["candidate_id"] or "") == str(live["source_candidate_id"] or ""))
    )


def _aggregate_evidence_status(items: list[dict[str, Any]]) -> str:
    states = {str(item["state"]) for item in items}
    for state in ("blocked", "failed", "stale", "running", "unknown"):
        if state in states:
            return state
    return "valid" if "valid" in states else "missing"


async def _regression_change_is_current(db: aiosqlite.Connection, row: Mapping[str, Any]) -> bool:
    """这条待办指向的变更还是当前真值吗？

    口径必须与 `0040_agent_eval_product` 里写 outbox 的两个触发器逐字对齐：
      · `definition_change` / `dependency_change` → `change_ref` 是当时的 `live_release_id`
      · `model_change`                            → `change_ref` 是 `'model:'||llm_config_version`
    对不上就说明它已被后来的变更取代，跑它等于对一个不再是 live 的版本烧一次真 LLM 评测。

    ⚠ 判不出来时返回 True（放行）：宁可多跑一次，也不要因为读不到当前值就把待办
    静默丢掉——那会变成「自动回归看起来开着但从不触发」。
    """
    change_kind = str(row["change_kind"])
    change_ref = str(row["change_ref"])
    if change_kind == "model_change":
        async with db.execute(
            "SELECT llm_config_version FROM teams WHERE id=?",
            (str(row["team_id"]),),
        ) as cursor:
            team = await cursor.fetchone()
        if team is None:
            return True
        return change_ref == f"model:{team[0]}"
    if change_kind in {"definition_change", "dependency_change"}:
        if str(row["agent_source_kind"]) != "team":
            return True
        async with db.execute(
            "SELECT live_release_id FROM team_subagent WHERE team_id=? AND id=?",
            (str(row["team_id"]), str(row["agent_id"])),
        ) as cursor:
            agent = await cursor.fetchone()
        if agent is None or agent[0] is None:
            return True
        return change_ref == str(agent[0])
    return True


def _regression_from_row(row: Mapping[str, Any]) -> EvalRegressionEvent:
    return EvalRegressionEvent(
        event_id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_source_kind=str(row["agent_source_kind"]),  # type: ignore[arg-type]
        agent_id=str(row["agent_id"]),
        change_kind=str(row["change_kind"]),  # type: ignore[arg-type]
        change_ref=str(row["change_ref"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        lease_generation=int(row["lease_generation"]),
        attempt_count=int(row["attempt_count"]),
    )


def _object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentEvalStoreError("agent_eval_document_malformed") from exc
    if not isinstance(decoded, dict):
        raise AgentEvalStoreError("agent_eval_document_malformed")
    return decoded


def model_route_hash(*, role: str, binding: Any) -> str:
    return _hash_document(
        {
            "extended_thinking": bool(binding.extended_thinking),
            "fallback": [list(item) for item in binding.fallback],
            "max_output_tokens": binding.max_output_tokens,
            "primary": list(binding.primary),
            "role": role,
            "routing_policy": str(binding.routing_policy),
            "thinking_budget_tokens": int(binding.thinking_budget_tokens),
        }
    )


def _hash_document(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _hash_token(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


__all__ = [
    "AgentEvalProductStore",
    "AgentEvalReleaseGate",
    "ClaimedEvalFailureNotification",
    "ClaimedEvalRegression",
    "EvalFailureNotification",
    "EvalRegressionEvent",
    "EvalSubjectTarget",
    "model_route_hash",
]


async def _run_quality_conn(db: aiosqlite.Connection, run_id: str) -> dict[str, Any]:
    """Include cases without results as ungraded, using the run's frozen suite."""
    async with db.execute(
        """SELECT c.expected_contract_json,r.judge_json FROM agent_eval_run run
           JOIN agent_eval_case_revision c ON c.suite_revision_id=run.suite_revision_id
           LEFT JOIN agent_eval_case_job job ON job.run_id=run.id AND job.case_revision_id=c.id
           LEFT JOIN agent_eval_case_result r ON r.run_id=run.id AND r.case_revision_id=c.id
             AND (job.attempt IS NULL OR r.attempt=job.attempt)
           WHERE run.id=?""",
        (run_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return quality_summary([point for row in rows for point in answer_quality(row["expected_contract_json"], row["judge_json"])["points"]])
