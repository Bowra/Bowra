"""Owner version gate, workbench projection, and Reviewer-safe evidence flow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlencode
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_insights import comparison_overview, read_snapshot_failure_summary_conn
from bowhead_core.agent_eval_plan_store import plan_behind_details_conn
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentPublicationResult,
    AgentReleaseStaleError,
    AgentRevisionConflictError,
    AgentRevisionForbiddenError,
    AgentRevisionStore,
    next_release_sequence,
    validated_release_note,
)
from bowhead_core.audit_actions import AuditAction

_SOFT_RISK_REASONS = frozenset(
    {
        "compatibility_unknown",
        "non_critical_regression",
        "owner_quality_skipped",
        # Failures that only model-graded points produced; the Owner may release past them (PRD E11).
        # Soft here for every policy, since saved policies list `override_reason_codes` explicitly.
        "point_regression",
        "point_unmet",
        "provider_identity_attributed_only",
    },
)
_MAX_REASON_NOTE = 2_000
_MAX_SELECTION_ITEMS = 100


class AgentVersionError(RuntimeError):
    """Stable version-workflow failure for HTTP adapters."""

    def __init__(self, code: str, *, reason_codes: Sequence[str] = (), details: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = tuple(reason_codes)
        # Safe identifiers the Owner needs to act on the failure, such as the candidate whose plan is behind.
        self.details = dict(details or {})


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AgentEvalOverride:
    id: str
    candidate_id: str
    plan_id: str
    snapshot_id: str
    override_hash: str
    reason_overrides: tuple[dict[str, str], ...]
    team_evidence: dict[str, Any]
    created_at: str
    control_row_version: int


@dataclass(frozen=True, slots=True)
class _Evidence:
    candidate: aiosqlite.Row
    control: aiosqlite.Row
    plan: aiosqlite.Row
    snapshot: aiosqlite.Row
    override: aiosqlite.Row | None

    @property
    def envelope(self) -> dict[str, Any]:
        return {
            "readiness": str(self.snapshot["readiness"]),
            "schema_version": 2,
            "snapshot_hash": str(self.snapshot["snapshot_hash"]),
            "snapshot_id": str(self.snapshot["id"]),
        }


class AgentVersionService:
    """One transactional command boundary for the Owner version lifecycle."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        revision_store: AgentRevisionStore,
        clock: _Clock | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._revision_store = revision_store
        self._clock = clock or _SystemClock()

    async def create_override(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        expected_control_row_version: int,
        plan_id: str,
        plan_hash: str,
        snapshot_id: str,
        snapshot_hash: str,
        reason_overrides: Sequence[Mapping[str, str]],
        team_evidence_selection: Mapping[str, Any],
    ) -> AgentEvalOverride:
        normalized_reasons = _normalize_reason_overrides(reason_overrides)
        selection = _normalize_selection(team_evidence_selection)
        timestamp = self._timestamp()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                evidence = await _exact_evidence(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    candidate_id=candidate_id,
                    candidate_row_version=candidate_row_version,
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    snapshot_id=snapshot_id,
                    snapshot_hash=snapshot_hash,
                    expected_state="prepared",
                )
                if int(evidence.control["row_version"]) != expected_control_row_version:
                    raise AgentVersionError("agent_eval_control_version_conflict")
                # Accepting risks only leads to a release, which a plan behind the saved cases can't reach.
                await _require_current_plan(db, team_id=team_id, agent_id=agent_id, evidence=evidence)
                if str(evidence.snapshot["readiness"]) != "ready_with_risks":
                    raise AgentVersionError("agent_eval_override_not_applicable")
                snapshot_summary = _object(evidence.snapshot["summary_json"], code="agent_eval_snapshot_malformed")
                snapshot_reasons = _string_set(snapshot_summary.get("reason_codes"), code="agent_eval_snapshot_malformed")
                if snapshot_reasons - _SOFT_RISK_REASONS:
                    raise AgentVersionError("agent_eval_override_hard_reason")
                provided_reasons = {item["reason_code"] for item in normalized_reasons}
                if provided_reasons != snapshot_reasons:
                    raise AgentVersionError("agent_eval_override_reason_mismatch")
                reviewer = _object(evidence.snapshot["reviewer_projection_json"], code="agent_eval_snapshot_malformed")
                team_evidence = _project_team_evidence(reviewer, selection=selection)
                reason_json = _canonical_json(list(normalized_reasons))
                selection_json = _canonical_json(selection)
                evidence_json = _canonical_json(team_evidence)
                override_hash = _hash_document(
                    {
                        "candidate_id": candidate_id,
                        "plan_id": plan_id,
                        "reason_overrides": list(normalized_reasons),
                        "schema_version": 1,
                        "snapshot_id": snapshot_id,
                        "team_evidence": team_evidence,
                        "team_evidence_selection": selection,
                    },
                )
                async with db.execute(
                    "SELECT * FROM agent_eval_override WHERE override_hash=?",
                    (override_hash,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    if str(evidence.control["active_override_id"] or "") != str(existing["id"]):
                        raise AgentVersionError("agent_eval_control_version_conflict")
                    await db.commit()
                    return _override_from_row(existing, control_row_version=int(evidence.control["row_version"]))
                override_id = f"aeov_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO agent_eval_override(
                      id,team_id,agent_id,candidate_id,plan_id,snapshot_id,override_hash,
                      reason_overrides_json,reason_overrides_hash,
                      team_evidence_selection_json,team_evidence_selection_hash,
                      team_evidence_json,team_evidence_hash,created_by,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        override_id,
                        team_id,
                        agent_id,
                        candidate_id,
                        plan_id,
                        snapshot_id,
                        override_hash,
                        reason_json,
                        _hash_text(reason_json),
                        selection_json,
                        _hash_text(selection_json),
                        evidence_json,
                        _hash_text(evidence_json),
                        actor_user_id,
                        timestamp,
                    ),
                )
                updated = await db.execute(
                    """
                    UPDATE agent_eval_candidate_control
                    SET active_override_id=?,row_version=row_version+1,updated_at=?
                    WHERE candidate_id=? AND row_version=? AND active_plan_id=?
                      AND active_snapshot_id=? AND readiness='ready_with_risks'
                    """,
                    (
                        override_id,
                        timestamp,
                        candidate_id,
                        expected_control_row_version,
                        plan_id,
                        snapshot_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentVersionError("agent_eval_control_version_conflict")
                await self._revision_store._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_EVAL_OVERRIDE_CREATED,
                    agent_id=agent_id,
                    payload={
                        "candidate_id": candidate_id,
                        "override_id": override_id,
                        "plan_id": plan_id,
                        "snapshot_id": snapshot_id,
                    },
                )
                async with db.execute("SELECT * FROM agent_eval_override WHERE id=?", (override_id,)) as cursor:
                    created = await cursor.fetchone()
                if created is None:
                    raise AgentVersionError("agent_eval_override_create_failed")
                await db.commit()
                return _override_from_row(created, control_row_version=expected_control_row_version + 1)
            except BaseException:
                await db.rollback()
                raise

    async def activate_private(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        candidate_hash: str,
        plan_id: str | None,
        plan_hash: str | None,
        snapshot_id: str | None,
        snapshot_hash: str | None,
        override_id: str | None,
        override_hash: str | None,
        idempotency_key: str,
        release_note: str | None = None,
        expected_test_state: str | None = None,
    ) -> AgentPublicationResult:
        """Release a private Agent's candidate as the next version.

        For a private Agent the test is information, never a gate (agent-release-beginner-experience.md §4):
        the candidate may be untested (no plan or snapshot given), without a conclusion, failing or with
        risks that have no recorded reason. The test state at release is recorded on the release. When
        `expected_test_state` is given (the state the release dialog showed), a different current state is
        refused with `agent_release_test_state_changed`, so the Owner never releases past a warning they
        didn't see. Ownership, audience, candidate and evidence identities, blocked Skills and Tools, drift
        and a test still running are checked exactly as before.
        """
        return await self._activate(
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            candidate_row_version=candidate_row_version,
            candidate_hash=candidate_hash,
            plan_id=plan_id,
            plan_hash=plan_hash,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            override_id=override_id,
            override_hash=override_hash,
            idempotency_key=idempotency_key,
            reviewer=False,
            release_note=release_note,
            expected_test_state=expected_test_state,
        )

    async def submit_for_review(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        candidate_hash: str,
        plan_id: str,
        plan_hash: str,
        snapshot_id: str,
        snapshot_hash: str,
        override_id: str | None,
        override_hash: str | None,
        idempotency_key: str,
        release_note: str | None = None,
    ) -> AgentPublicationResult:
        """Submit the tested candidate for review.

        `release_note` is the note the Owner writes in "Submit for review (as v2)". It is stored on the
        submission (`agent_eval_candidate_control.review_release_note`), outside the candidate hash the
        evidence pins; reviewers read it and approval copies it onto the release.
        """
        _validate_idempotency_key(idempotency_key)
        release_note = validated_release_note(release_note)
        request_hash = _hash_document(
            {
                **({"release_note": release_note} if release_note is not None else {}),
                "candidate_hash": candidate_hash,
                "candidate_id": candidate_id,
                "candidate_row_version": candidate_row_version,
                "override_hash": override_hash,
                "override_id": override_id,
                "plan_hash": plan_hash,
                "plan_id": plan_id,
                "snapshot_hash": snapshot_hash,
                "snapshot_id": snapshot_id,
            },
        )
        timestamp = self._timestamp()
        scope = f"agent.version.submit:{agent_id}"
        stale_error: AgentReleaseStaleError | None = None
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aggregate = await self._revision_store._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                replay = await self._revision_store._receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    await db.commit()
                    return replay
                # Same gate as private activation and reviewer approval: an archived or
                # non-revisioned Agent cannot be submitted, otherwise reviewers get notified about
                # a candidate they can never approve.
                await self._revision_store._require_revisioned_active(aggregate)
                evidence = await _exact_evidence(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    candidate_id=candidate_id,
                    candidate_row_version=candidate_row_version,
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    snapshot_id=snapshot_id,
                    snapshot_hash=snapshot_hash,
                    expected_state="prepared",
                    override_id=override_id,
                    override_hash=override_hash,
                )
                _require_candidate_hash(evidence.candidate, candidate_hash)
                audience = AgentAudience.from_mapping(_object(evidence.candidate["target_audience_json"], code="agent_audience_malformed"))
                if audience.mode == "private":
                    raise AgentVersionError("agent_release_private_review_not_required")
                if aggregate["live_release_id"] != evidence.candidate["base_live_release_id"]:
                    stale_error = await self._revision_store._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=evidence.candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("base_release_changed",),
                        timestamp=timestamp,
                    )
                elif self._revision_store._owner_id(aggregate) != str(evidence.candidate["submitted_by"]):
                    stale_error = await self._revision_store._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=evidence.candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("agent_owner_changed",),
                        timestamp=timestamp,
                    )
                else:
                    revision = await self._revision_store._revision_conn(db, candidate=evidence.candidate)
                    if _hash_text(str(revision["definition_json"])) != str(revision["definition_hash"]):
                        raise AgentVersionError("agent_revision_integrity_failed")
                    definition = _object(revision["definition_json"], code="agent_revision_malformed")
                    listing = _object(revision["listing_json"], code="agent_revision_malformed")
                    await self._revision_store._validate_audience_conn(db, team_id=team_id, audience=audience)
                    validation = await self._revision_store._validator.validate(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        owner_user_id=self._revision_store._owner_id(aggregate),
                        routing_name=str(aggregate["name"]),
                        definition=definition,
                        listing=listing,
                        audience=audience,
                    )
                    drift = await self._revision_store._candidate_drift_reasons_conn(
                        db,
                        candidate=evidence.candidate,
                        definition=definition,
                        listing=listing,
                        audience=audience,
                        validation=validation,
                    )
                    if drift:
                        stale_error = await self._revision_store._mark_stale_conn(
                            db,
                            aggregate=aggregate,
                            candidate=evidence.candidate,
                            actor_user_id=actor_user_id,
                            reason_codes=drift,
                            timestamp=timestamp,
                        )
                if stale_error is not None:
                    await db.commit()
                    raise stale_error
                self._revision_store._require_publishable(validation, audience=audience)
                await _require_current_plan(db, team_id=team_id, agent_id=agent_id, evidence=evidence)
                _require_releasable_evidence(evidence)
                if not await self._revision_store._has_qualified_reviewer_conn(db, team_id=team_id):
                    raise AgentPublicationBlockedError("qualified_reviewer_unavailable")
                updated = await db.execute(
                    """
                    UPDATE team_subagent_release_candidate
                    SET state='pending',row_version=row_version+1,updated_at=?
                    WHERE id=? AND state='prepared' AND row_version=?
                    """,
                    (timestamp, candidate_id, candidate_row_version),
                )
                if updated.rowcount != 1:
                    raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
                control_version = int(evidence.control["row_version"])
                updated_control = await db.execute(
                    """
                    UPDATE agent_eval_candidate_control
                    SET review_submitted_by=?,review_submitted_at=?,review_plan_id=?,review_plan_hash=?,
                        review_snapshot_id=?,review_snapshot_hash=?,review_override_id=?,review_override_hash=?,
                        review_release_note=?,row_version=row_version+1,updated_at=?
                    WHERE candidate_id=? AND row_version=?
                    """,
                    (
                        actor_user_id,
                        timestamp,
                        plan_id,
                        plan_hash,
                        snapshot_id,
                        snapshot_hash,
                        override_id,
                        override_hash,
                        release_note,
                        timestamp,
                        candidate_id,
                        control_version,
                    ),
                )
                if updated_control.rowcount != 1:
                    raise AgentVersionError("agent_eval_control_version_conflict")
                event_id = await self._revision_store._insert_event_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    candidate_id=candidate_id,
                    release_id=None,
                    event_type="candidate.submitted",
                    actor_user_id=actor_user_id,
                    payload={
                        "candidate_hash": candidate_hash,
                        "plan_hash": plan_hash,
                        "readiness": str(evidence.snapshot["readiness"]),
                        "snapshot_hash": snapshot_hash,
                    },
                    timestamp=timestamp,
                )
                await self._revision_store._enqueue_reviewers_conn(
                    db,
                    team_id=team_id,
                    event_id=event_id,
                    candidate_id=candidate_id,
                    timestamp=timestamp,
                )
                await _notify_review_submitted(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    owner_user_id=actor_user_id,
                    candidate_id=candidate_id,
                    plan_id=plan_id,
                    snapshot_id=snapshot_id,
                    timestamp=timestamp,
                )
                await self._revision_store._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_RELEASE_CANDIDATE_SUBMITTED,
                    agent_id=agent_id,
                    payload={
                        "candidate_id": candidate_id,
                        "plan_id": plan_id,
                        "snapshot_id": snapshot_id,
                    },
                )
                result = AgentPublicationResult(
                    state="pending",
                    agent_id=agent_id,
                    candidate_id=candidate_id,
                    revision_id=str(evidence.candidate["revision_id"]),
                    release_id=None,
                    candidate_row_version=candidate_row_version + 1,
                    aggregate_row_version=int(aggregate["row_version"]),
                )
                await self._revision_store._store_receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result=result,
                    timestamp=timestamp,
                )
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def reviewer_projection(
        self,
        *,
        team_id: str,
        candidate_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if not await self._revision_store._qualified_reviewer_conn(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
            ):
                raise AgentRevisionForbiddenError("agent_release_review_forbidden")
            row = await _review_context(db, team_id=team_id, candidate_id=candidate_id)
            candidate_state = str(row["candidate_state"])
            identity_current = (
                candidate_state == "pending"
                and row["live_release_id"] == row["base_live_release_id"]
                and str(row["candidate_hash"]) == str(row["plan_candidate_hash"])
                and str(row["review_plan_hash"] or "") == str(row["plan_hash"] or "")
                and str(row["review_snapshot_hash"] or "") == str(row["snapshot_hash"] or "")
                and str(row["review_override_hash"] or "") == str(row["override_hash"] or "")
            )
            disabled = [] if identity_current else ["review_identity_stale"]
            reviewer = _object(row["reviewer_projection_json"], code="agent_eval_snapshot_malformed")
            override = None
            if row["override_id"] is not None:
                override = {
                    "id": str(row["override_id"]),
                    "hash": str(row["override_hash"]),
                    "reason_overrides": _array(row["reason_overrides_json"], code="agent_eval_override_malformed"),
                    "team_evidence": _object(row["team_evidence_json"], code="agent_eval_override_malformed"),
                }
            return {
                "actor_overlap": str(row["owner_user_id"]) == actor_user_id,
                "candidate_hash": str(row["candidate_hash"]),
                "candidate_id": candidate_id,
                "candidate_label": f"拟发布 v{next_release_sequence(int(row['release_sequence']))}",
                "decision_availability": {
                    "approve": identity_current,
                    "disabled_reason_codes": disabled,
                    "reject": identity_current,
                },
                "override": override,
                "plan_hash": str(row["review_plan_hash"]),
                "plan_id": str(row["review_plan_id"]),
                "readiness": str(row["readiness"]),
                "reviewer_evidence": reviewer,
                "schema_version": 2,
                "snapshot_created_at": str(row["snapshot_created_at"]),
                "snapshot_hash": str(row["review_snapshot_hash"]),
                "snapshot_id": str(row["review_snapshot_id"]),
                "submitted_at": str(row["review_submitted_at"]),
            }

    async def approve_shared(
        self,
        *,
        team_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        candidate_hash: str,
        plan_id: str,
        plan_hash: str,
        snapshot_id: str,
        snapshot_hash: str,
        override_id: str | None,
        override_hash: str | None,
        idempotency_key: str,
    ) -> AgentPublicationResult:
        return await self._activate(
            team_id=team_id,
            agent_id=None,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            candidate_row_version=candidate_row_version,
            candidate_hash=candidate_hash,
            plan_id=plan_id,
            plan_hash=plan_hash,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            override_id=override_id,
            override_hash=override_hash,
            idempotency_key=idempotency_key,
            reviewer=True,
        )

    async def workbench(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await read_agent_version_workbench_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)

    async def _activate(
        self,
        *,
        team_id: str,
        agent_id: str | None,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        candidate_hash: str,
        plan_id: str | None,
        plan_hash: str | None,
        snapshot_id: str | None,
        snapshot_hash: str | None,
        override_id: str | None,
        override_hash: str | None,
        idempotency_key: str,
        reviewer: bool,
        release_note: str | None = None,
        expected_test_state: str | None = None,
    ) -> AgentPublicationResult:
        _validate_idempotency_key(idempotency_key)
        release_note = validated_release_note(release_note)
        if reviewer and (plan_id is None or plan_hash is None or snapshot_id is None or snapshot_hash is None):
            # A reviewer approves exactly the evidence the Owner submitted.
            raise AgentVersionError("agent_eval_snapshot_hash_conflict")
        if (
            (plan_id is None) != (plan_hash is None)
            or (snapshot_id is None) != (snapshot_hash is None)
            or (snapshot_id is not None and plan_id is None)
        ):
            raise AgentVersionError("agent_eval_snapshot_hash_conflict")
        if expected_test_state is not None and expected_test_state not in PRIVATE_RELEASE_TEST_STATES:
            raise AgentVersionError("agent_release_test_state_invalid")
        request_hash = _hash_document(
            {
                **({"expected_test_state": expected_test_state} if expected_test_state is not None else {}),
                "candidate_hash": candidate_hash,
                "candidate_id": candidate_id,
                "candidate_row_version": candidate_row_version,
                "override_hash": override_hash,
                "override_id": override_id,
                "plan_hash": plan_hash,
                "plan_id": plan_id,
                # Only a note given at release is part of the request; older requests keep their hash.
                **({"release_note": release_note} if release_note is not None else {}),
                "reviewer": reviewer,
                "snapshot_hash": snapshot_hash,
                "snapshot_id": snapshot_id,
            },
        )
        timestamp = self._timestamp()
        scope = f"agent.version.{'approve' if reviewer else 'activate'}:{candidate_id}"
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            stale_error: AgentReleaseStaleError | None = None
            try:
                candidate = await self._revision_store._candidate_conn(db, team_id=team_id, candidate_id=candidate_id)
                resolved_agent_id = str(candidate["subagent_id"])
                if agent_id is not None and agent_id != resolved_agent_id:
                    raise AgentRevisionForbiddenError("agent_release_candidate_not_found")
                if reviewer:
                    if not await self._revision_store._qualified_reviewer_conn(
                        db,
                        team_id=team_id,
                        actor_user_id=actor_user_id,
                    ):
                        raise AgentRevisionForbiddenError("agent_release_review_forbidden")
                    aggregate = await self._revision_store._aggregate_conn(
                        db,
                        team_id=team_id,
                        agent_id=resolved_agent_id,
                    )
                    expected_state: Literal["prepared", "pending"] = "pending"
                else:
                    aggregate = await self._revision_store._owned_aggregate_conn(
                        db,
                        team_id=team_id,
                        agent_id=resolved_agent_id,
                        actor_user_id=actor_user_id,
                    )
                    expected_state = "prepared"
                replay = await self._revision_store._receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    await db.commit()
                    return replay
                # Same checks as AgentRevisionStore.approve_candidate: an archived or non-revisioned
                # Agent cannot have its live release rewritten (owner and reviewer paths alike).
                # After an owner change, a candidate prepared by the previous owner is stale on both
                # paths (tech-design section 6.8: an owner change marks prepared / pending candidates
                # stale). The new owner must prepare again from their own working copy instead of
                # activating the predecessor's private candidate. The reviewer path additionally
                # checks that the owner is still an active member.
                await self._revision_store._require_revisioned_active(aggregate)
                evidence: _Evidence | None = None
                if snapshot_id is not None:
                    evidence = await _exact_evidence(
                        db,
                        team_id=team_id,
                        agent_id=resolved_agent_id,
                        candidate_id=candidate_id,
                        candidate_row_version=candidate_row_version,
                        plan_id=str(plan_id),
                        plan_hash=str(plan_hash),
                        snapshot_id=snapshot_id,
                        snapshot_hash=str(snapshot_hash),
                        expected_state=expected_state,
                        override_id=override_id,
                        override_hash=override_hash,
                        review_frozen=reviewer,
                    )
                    candidate = evidence.candidate
                else:
                    # Private only (reviewers always name evidence): a candidate released without a finished test.
                    candidate = await _untested_candidate(
                        db,
                        candidate=candidate,
                        candidate_row_version=candidate_row_version,
                        plan_id=plan_id,
                        plan_hash=plan_hash,
                        override_id=override_id,
                    )
                _require_candidate_hash(candidate, candidate_hash)
                audience = AgentAudience.from_mapping(_object(candidate["target_audience_json"], code="agent_audience_malformed"))
                if (reviewer and audience.mode == "private") or (not reviewer and audience.mode != "private"):
                    raise AgentVersionError("agent_release_audience_action_mismatch")
                if aggregate["live_release_id"] != candidate["base_live_release_id"]:
                    stale_error = await self._revision_store._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("base_release_changed",),
                        timestamp=timestamp,
                    )
                elif self._revision_store._owner_id(aggregate) != str(candidate["submitted_by"]):
                    stale_error = await self._revision_store._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("agent_owner_changed",),
                        timestamp=timestamp,
                    )
                else:
                    if reviewer:
                        await self._revision_store._require_owner_active_conn(db, aggregate=aggregate)
                    revision = await self._revision_store._revision_conn(db, candidate=candidate)
                    if _hash_text(str(revision["definition_json"])) != str(revision["definition_hash"]):
                        raise AgentVersionError("agent_revision_integrity_failed")
                    definition = _object(revision["definition_json"], code="agent_revision_malformed")
                    listing = _object(revision["listing_json"], code="agent_revision_malformed")
                    await self._revision_store._validate_audience_conn(db, team_id=team_id, audience=audience)
                    validation = await self._revision_store._validator.validate(
                        db,
                        team_id=team_id,
                        agent_id=resolved_agent_id,
                        owner_user_id=self._revision_store._owner_id(aggregate),
                        routing_name=str(aggregate["name"]),
                        definition=definition,
                        listing=listing,
                        audience=audience,
                    )
                    drift = await self._revision_store._candidate_drift_reasons_conn(
                        db,
                        candidate=candidate,
                        definition=definition,
                        listing=listing,
                        audience=audience,
                        validation=validation,
                    )
                    if drift:
                        stale_error = await self._revision_store._mark_stale_conn(
                            db,
                            aggregate=aggregate,
                            candidate=candidate,
                            actor_user_id=actor_user_id,
                            reason_codes=drift,
                            timestamp=timestamp,
                        )
                    else:
                        self._revision_store._require_publishable(validation, audience=audience)
                        if reviewer:
                            # Review freezes the submitted plan (agent_eval_plan_frozen_for_review): the Reviewer
                            # decides on the evidence the Owner submitted, and cases saved meanwhile apply to the
                            # next candidate. Submission refuses a plan that was already behind.
                            assert evidence is not None
                            _require_releasable_evidence(evidence)
                            envelope = evidence.envelope
                        else:
                            # A private Agent: the test is information, not a gate. Record what it said.
                            envelope = await _private_release_envelope_conn(
                                db,
                                team_id=team_id,
                                agent_id=resolved_agent_id,
                                actor_user_id=actor_user_id,
                                candidate_id=candidate_id,
                                evidence=evidence,
                                expected_test_state=expected_test_state,
                            )
                        result = await self._revision_store._activate_conn(
                            db,
                            aggregate=aggregate,
                            candidate=candidate,
                            definition=definition,
                            listing=listing,
                            audience=audience,
                            validation=validation,
                            actor_user_id=actor_user_id,
                            self_review=self._revision_store._owner_id(aggregate) == actor_user_id,
                            decision_reason="reviewer_approved" if reviewer else "owner_private_activation",
                            timestamp=timestamp,
                            candidate_from_state=expected_state,
                            eval_evidence_override=envelope,
                            # A note given at release wins; on approval it is the one the Owner submitted with.
                            release_note=release_note
                            if release_note is not None
                            else (evidence.control["review_release_note"] if evidence is not None else None),
                        )
                        if result.release_id is None:
                            raise AgentVersionError("agent_release_activation_failed")
                        await _insert_release_evidence(
                            db,
                            release_id=result.release_id,
                            team_id=team_id,
                            agent_id=resolved_agent_id,
                            evidence=evidence,
                            timestamp=timestamp,
                        )
                        await self._revision_store._store_receipt_conn(
                            db,
                            team_id=team_id,
                            actor_user_id=actor_user_id,
                            scope=scope,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            result=result,
                            timestamp=timestamp,
                        )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if stale_error is not None:
            raise stale_error
        return result

    def _timestamp(self) -> str:
        return self._clock.now().astimezone(UTC).isoformat()


async def read_agent_version_workbench_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str) -> dict[str, Any]:
    """Read the existing workbench projection in the caller's database snapshot."""
    await _require_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    async with db.execute(
        """
        SELECT agent.live_release_id,agent.live_revision_id,agent.release_sequence,
               (SELECT policy.id FROM agent_eval_policy_revision policy
                WHERE policy.team_id=agent.team_id AND policy.agent_id=agent.id
                ORDER BY policy.sequence DESC LIMIT 1) AS current_eval_policy_revision_id,
               working.id AS working_id,working.base_release_id AS working_base_release_id,
               working.row_version AS working_row_version,
               json_extract(working.audience_json,'$.effective_mode') AS working_audience_mode,
               candidate.id AS candidate_id,candidate.revision_id AS candidate_revision_id,
               candidate.candidate_hash,candidate.state AS candidate_state,
               candidate.row_version AS candidate_row_version,
               candidate.base_live_release_id AS candidate_base_release_id,
               json_extract(candidate.target_audience_json,'$.effective_mode') AS audience_mode,
               control.active_plan_id,control.readiness,control.stale_subject,
               control.reason_codes_json,control.active_execution_id,
               control.active_snapshot_id,control.active_override_id,control.row_version AS control_row_version,
               control.source_working_copy_id,control.source_working_copy_version,
               plan.plan_hash,plan.policy_revision_id AS plan_policy_revision_id,
               plan.execution_workspace_id AS plan_workspace_id,snapshot.snapshot_hash,snapshot.created_at AS snapshot_created_at,
               snapshot.execution_id AS snapshot_execution_id,snapshot.summary_json AS snapshot_summary_json,
               override.override_hash,
               execution.id AS execution_id,execution.status AS execution_status,
               execution.row_version AS execution_row_version,execution.requested_at,execution.started_at,
               execution.finished_at,execution.failure_code,execution.coach_delegation_id AS execution_coach_delegation_id,
               execution.actor_kind AS execution_actor_kind
        FROM team_subagent agent
        LEFT JOIN team_subagent_working_copy working
          ON working.team_id=agent.team_id AND working.subagent_id=agent.id
        LEFT JOIN team_subagent_release_candidate candidate
          ON candidate.team_id=agent.team_id AND candidate.subagent_id=agent.id
         AND candidate.state IN ('prepared','pending','approved')
        LEFT JOIN agent_eval_candidate_control control ON control.candidate_id=candidate.id
        LEFT JOIN agent_eval_plan plan ON plan.id=control.active_plan_id
        LEFT JOIN agent_eval_snapshot snapshot ON snapshot.id=control.active_snapshot_id
        LEFT JOIN agent_eval_override override ON override.id=control.active_override_id
        LEFT JOIN agent_eval_comparison_execution execution ON execution.id=control.active_execution_id
        WHERE agent.team_id=? AND agent.id=?
        """,
        (team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentRevisionForbiddenError("agent_not_found")
    return _workbench_projection(row, facts=await _snapshot_facts_conn(db, row["snapshot_summary_json"]))


def release_tab_path(agent_id: str) -> str:
    """The Owner's Release tab, where the release outcome is shown; other surfaces link here."""
    return "/agents/manage/?" + urlencode({"agent_id": agent_id, "section": "release"})


def release_check_outcome(workbench: Mapping[str, Any], *, failure_counts: Mapping[str, Any] | None) -> dict[str, Any]:
    """The release outcome exactly as the Owner's Release tab derives it, for every other reader.

    The Team API gate and Coach's evidence read this instead of re-deriving it from runs, so the
    three surfaces can't disagree: readiness and reasons come from the candidate's control row (a
    candidate built on a release that is no longer live reads `stale`), and the failing required
    cases are counted from the active snapshot with the same D3 labels the tab shows.
    """
    candidate = workbench.get("candidate_identity") or None
    live = workbench.get("live_identity") or {}
    snapshot = workbench.get("active_snapshot_identity") or None
    base_release_id = candidate.get("base_release_id") if isinstance(candidate, Mapping) else None
    # Built on a release that is no longer live: activation would reject it (base_release_changed),
    # so its earlier evidence must not read as a clear result.
    outdated = bool(base_release_id) and base_release_id != live.get("release_id")
    return {
        "candidate_id": candidate.get("candidate_id") if isinstance(candidate, Mapping) else None,
        "snapshot_id": snapshot.get("id") if isinstance(snapshot, Mapping) else None,
        "readiness": "stale" if outdated else workbench.get("readiness"),
        "stale_subject": "candidate" if outdated else workbench.get("stale_subject"),
        "display_state": workbench.get("display_state"),
        "reason_codes": list(workbench.get("reason_codes") or []),
        "next_action": (workbench.get("primary_action") or {}).get("kind"),
        "evaluation_plan_behind": bool(workbench.get("evaluation_plan_behind")),
        "failure_summary": dict(failure_counts) if failure_counts is not None else None,
        # Private Agents: releasing is always offered; this says what the release would record about the test.
        "private_release": workbench.get("private_release"),
    }


async def release_check_outcome_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    workbench: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`release_check_outcome` read in the caller's snapshot; pass an already read `workbench` to reuse it."""
    current = (
        workbench
        if workbench is not None
        else await read_agent_version_workbench_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    )
    candidate = current.get("candidate_identity") or None
    snapshot = current.get("active_snapshot_identity") or None
    counts = None
    if isinstance(candidate, Mapping) and isinstance(snapshot, Mapping) and snapshot.get("id"):
        counts = await read_snapshot_failure_summary_conn(
            db, team_id=team_id, agent_id=agent_id, candidate_id=str(candidate["candidate_id"]), snapshot_id=str(snapshot["id"])
        )
    return release_check_outcome(current, failure_counts=counts)


async def _require_owner(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM team_subagent agent
        JOIN users actor ON actor.id=? AND actor.team_id=agent.team_id AND actor.status='active'
        JOIN team_members member
          ON member.team_id=agent.team_id AND member.user_id=actor.id AND member.status='active'
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentRevisionForbiddenError("agent_not_found")


async def _exact_evidence(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    candidate_row_version: int,
    plan_id: str,
    plan_hash: str,
    snapshot_id: str,
    snapshot_hash: str,
    expected_state: Literal["prepared", "pending"],
    override_id: str | None = None,
    override_hash: str | None = None,
    review_frozen: bool = False,
) -> _Evidence:
    async with db.execute(
        """
        SELECT * FROM team_subagent_release_candidate
        WHERE team_id=? AND subagent_id=? AND id=?
        """,
        (team_id, agent_id, candidate_id),
    ) as cursor:
        candidate = await cursor.fetchone()
    if candidate is None:
        raise AgentRevisionForbiddenError("agent_release_candidate_not_found")
    if str(candidate["state"]) != expected_state:
        raise AgentVersionError("agent_release_candidate_state_conflict")
    if int(candidate["row_version"]) != candidate_row_version:
        raise AgentVersionError("agent_release_candidate_version_conflict")
    async with db.execute(
        "SELECT * FROM agent_eval_candidate_control WHERE candidate_id=?",
        (candidate_id,),
    ) as cursor:
        control = await cursor.fetchone()
    if control is None:
        raise AgentVersionError("agent_eval_control_not_found")
    actual_plan_id = control["review_plan_id"] if review_frozen else control["active_plan_id"]
    actual_snapshot_id = control["review_snapshot_id"] if review_frozen else control["active_snapshot_id"]
    actual_override_id = control["review_override_id"] if review_frozen else control["active_override_id"]
    if str(actual_plan_id or "") != plan_id:
        raise AgentVersionError("agent_eval_plan_hash_conflict")
    if str(actual_snapshot_id or "") != snapshot_id:
        raise AgentVersionError("agent_eval_snapshot_hash_conflict")
    if (str(actual_override_id) if actual_override_id is not None else None) != override_id:
        raise AgentVersionError("agent_eval_override_hash_conflict")
    async with db.execute(
        "SELECT * FROM agent_eval_plan WHERE id=? AND team_id=? AND agent_id=? AND candidate_id=?",
        (plan_id, team_id, agent_id, candidate_id),
    ) as cursor:
        plan = await cursor.fetchone()
    if plan is None or str(plan["plan_hash"]) != plan_hash:
        raise AgentVersionError("agent_eval_plan_hash_conflict")
    if str(plan["candidate_hash"]) != str(candidate["candidate_hash"]):
        raise AgentVersionError("agent_eval_candidate_stale")
    async with db.execute(
        "SELECT * FROM agent_eval_snapshot WHERE id=? AND team_id=? AND agent_id=? AND candidate_id=? AND plan_id=?",
        (snapshot_id, team_id, agent_id, candidate_id, plan_id),
    ) as cursor:
        snapshot = await cursor.fetchone()
    if snapshot is None or str(snapshot["snapshot_hash"]) != snapshot_hash:
        raise AgentVersionError("agent_eval_snapshot_hash_conflict")
    override = None
    if override_id is not None:
        if override_hash is None:
            raise AgentVersionError("agent_eval_override_hash_conflict")
        async with db.execute(
            """
            SELECT * FROM agent_eval_override
            WHERE id=? AND team_id=? AND agent_id=? AND candidate_id=? AND plan_id=? AND snapshot_id=?
            """,
            (override_id, team_id, agent_id, candidate_id, plan_id, snapshot_id),
        ) as cursor:
            override = await cursor.fetchone()
        if override is None or str(override["override_hash"]) != override_hash:
            raise AgentVersionError("agent_eval_override_hash_conflict")
    elif override_hash is not None:
        raise AgentVersionError("agent_eval_override_hash_conflict")
    if review_frozen:
        if str(control["review_plan_hash"] or "") != plan_hash:
            raise AgentVersionError("agent_eval_plan_hash_conflict")
        if str(control["review_snapshot_hash"] or "") != snapshot_hash:
            raise AgentVersionError("agent_eval_snapshot_hash_conflict")
        if (str(control["review_override_hash"]) if control["review_override_hash"] is not None else None) != override_hash:
            raise AgentVersionError("agent_eval_override_hash_conflict")
    return _Evidence(candidate=candidate, control=control, plan=plan, snapshot=snapshot, override=override)


async def _require_current_plan(db: aiosqlite.Connection, *, team_id: str, agent_id: str, evidence: _Evidence) -> None:
    """Refuse when the prepared candidate's plan predates the latest saved test cases (update the plan first)."""
    details = await plan_behind_details_conn(
        db,
        team_id=team_id,
        agent_id=agent_id,
        candidate_id=str(evidence.candidate["id"]),
        plan=evidence.plan,
    )
    if details is not None:
        raise AgentVersionError("agent_eval_plan_behind", details=details)


def _require_releasable_evidence(evidence: _Evidence) -> None:
    if evidence.control["active_execution_id"] is not None:
        raise AgentVersionError("agent_eval_release_run_incomplete")
    readiness = str(evidence.snapshot["readiness"])
    if str(evidence.control["readiness"]) != readiness:
        raise AgentVersionError("agent_eval_evidence_stale")
    if readiness == "ready":
        if evidence.override is not None:
            raise AgentVersionError("agent_eval_override_not_applicable")
        return
    if readiness == "ready_with_risks":
        if evidence.override is None:
            raise AgentVersionError("agent_eval_override_required")
        return
    if readiness in {"blocked", "unknown"}:
        raise AgentVersionError("agent_eval_release_blocked", reason_codes=_control_reasons(evidence.control))
    raise AgentVersionError("agent_eval_evidence_stale")


def _require_candidate_hash(candidate: Mapping[str, Any], expected: str) -> None:
    if str(candidate["candidate_hash"]) != expected:
        raise AgentVersionError("agent_eval_candidate_stale")


async def _untested_candidate(
    db: aiosqlite.Connection,
    *,
    candidate: aiosqlite.Row,
    candidate_row_version: int,
    plan_id: str | None,
    plan_hash: str | None,
    override_id: str | None,
) -> aiosqlite.Row:
    """Check a private release that names no finished test: the candidate is current and has none."""
    if str(candidate["state"]) != "prepared":
        raise AgentVersionError("agent_release_candidate_state_conflict")
    if int(candidate["row_version"]) != candidate_row_version:
        raise AgentVersionError("agent_release_candidate_version_conflict")
    if override_id is not None:
        raise AgentVersionError("agent_eval_override_hash_conflict")
    async with db.execute("SELECT * FROM agent_eval_candidate_control WHERE candidate_id=?", (str(candidate["id"]),)) as cursor:
        control = await cursor.fetchone()
    if control is not None and control["active_snapshot_id"] is not None:
        # A test finished after the release dialog was opened: its result must be shown first.
        raise AgentVersionError("agent_release_test_state_changed")
    if plan_id is not None:
        async with db.execute("SELECT plan_hash FROM agent_eval_plan WHERE id=? AND candidate_id=?", (plan_id, str(candidate["id"]))) as cursor:
            plan = await cursor.fetchone()
        if control is None or str(control["active_plan_id"] or "") != plan_id or plan is None or str(plan["plan_hash"]) != plan_hash:
            raise AgentVersionError("agent_eval_plan_hash_conflict")
    return candidate


async def _private_release_envelope_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    candidate_id: str,
    evidence: _Evidence | None,
    expected_test_state: str | None,
) -> dict[str, Any]:
    """The test state a private release records, read in the activation transaction."""
    workbench = await read_agent_version_workbench_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    identity = workbench.get("candidate_identity") or {}
    if identity.get("candidate_id") != candidate_id:
        raise AgentVersionError("agent_release_candidate_state_conflict")
    execution = workbench.get("active_execution_summary") or {}
    if execution.get("status") in _RUNNING_EXECUTIONS:
        # Releasing never races a test of the same change: stop it first, or wait for its result.
        raise AgentVersionError("agent_eval_release_run_incomplete")
    facts = await _snapshot_facts_conn(db, evidence.snapshot["summary_json"]) if evidence is not None else _NO_FACTS
    reasons = list(workbench.get("reason_codes") or [])
    state = candidate_test_state(
        readiness=workbench.get("readiness"),
        reason_codes=reasons,
        execution_status=execution.get("status"),
        has_snapshot=evidence is not None,
        evidence_behind=bool(workbench.get("evaluation_plan_behind")) or workbench.get("stale_subject") == "evidence",
        facts=facts,
    )
    if expected_test_state is not None and expected_test_state != state:
        raise AgentVersionError("agent_release_test_state_changed", details={"test_state": state})
    return {
        **(evidence.envelope if evidence is not None else {"readiness": None, "schema_version": 2, "snapshot_hash": None, "snapshot_id": None}),
        # What the Owner was told about the test when releasing; history shows it (for example "released without a
        # conclusive test"). Present only on private releases.
        "release_test": {
            "reason_codes": sorted(set(reasons)) if state not in {"passed", "not_tested"} else [],
            "state": state,
        },
    }


async def _insert_release_evidence(
    db: aiosqlite.Connection,
    *,
    release_id: str,
    team_id: str,
    agent_id: str,
    evidence: _Evidence | None,
    timestamp: str,
) -> None:
    # The evidence table holds releasable test results only; a private release past an untested, failing or
    # inconclusive test records its state on the release itself (`eval_evidence_json.release_test`).
    if evidence is None or str(evidence.snapshot["readiness"]) not in {"ready", "ready_with_risks"}:
        return
    await db.execute(
        """
        INSERT INTO agent_release_eval_evidence(
          release_id,team_id,agent_id,candidate_id,plan_id,snapshot_id,override_id,
          candidate_hash,plan_hash,snapshot_hash,override_hash,readiness,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            release_id,
            team_id,
            agent_id,
            evidence.candidate["id"],
            evidence.plan["id"],
            evidence.snapshot["id"],
            evidence.override["id"] if evidence.override is not None else None,
            evidence.candidate["candidate_hash"],
            evidence.plan["plan_hash"],
            evidence.snapshot["snapshot_hash"],
            evidence.override["override_hash"] if evidence.override is not None else None,
            evidence.snapshot["readiness"],
            timestamp,
        ),
    )


async def _review_context(db: aiosqlite.Connection, *, team_id: str, candidate_id: str) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT candidate.state AS candidate_state,candidate.candidate_hash,
               candidate.base_live_release_id,candidate.row_version AS candidate_row_version,
               agent.live_release_id,agent.release_sequence,
               COALESCE(agent.owner_user_id,agent.created_by) AS owner_user_id,
               control.review_submitted_at,control.review_plan_id,control.review_plan_hash,
               control.review_snapshot_id,control.review_snapshot_hash,
               control.review_override_id,control.review_override_hash,
               plan.plan_hash,plan.candidate_hash AS plan_candidate_hash,
               snapshot.snapshot_hash,snapshot.readiness,
               snapshot.reviewer_projection_json,snapshot.created_at AS snapshot_created_at,
               override.id AS override_id,override.override_hash,
               override.reason_overrides_json,override.team_evidence_json
        FROM team_subagent_release_candidate candidate
        JOIN team_subagent agent
          ON agent.team_id=candidate.team_id AND agent.id=candidate.subagent_id
        JOIN agent_eval_candidate_control control ON control.candidate_id=candidate.id
        LEFT JOIN agent_eval_plan plan ON plan.id=control.review_plan_id
        LEFT JOIN agent_eval_snapshot snapshot ON snapshot.id=control.review_snapshot_id
        LEFT JOIN agent_eval_override override ON override.id=control.review_override_id
        WHERE candidate.team_id=? AND candidate.id=?
        """,
        (team_id, candidate_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row["review_plan_id"] is None:
        raise AgentRevisionForbiddenError("agent_release_candidate_not_found")
    return row


async def _notify_review_submitted(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    owner_user_id: str,
    candidate_id: str,
    plan_id: str,
    snapshot_id: str,
    timestamp: str,
) -> None:
    await db.execute(
        """
        INSERT OR IGNORE INTO agent_version_notification_outbox(
          id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,
          snapshot_id,release_id,dedupe_key,safe_payload_json,status,
          available_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,'pending',?,?)
        """,
        (
            f"aevn_{uuid4().hex}",
            team_id,
            agent_id,
            owner_user_id,
            "review.submitted",
            candidate_id,
            plan_id,
            snapshot_id,
            f"review.submitted:{candidate_id}:{snapshot_id}",
            _canonical_json({"candidate_id": candidate_id, "schema_version": 1}),
            timestamp,
            timestamp,
        ),
    )


@dataclass(frozen=True, slots=True)
class SnapshotTestFacts:
    """What a finished test found that the readiness verdict alone doesn't say."""

    # A required case passed on the released version and fails on the change.
    required_regression: bool = False
    # A case of the platform's safety contract suite fails on the change.
    contract_failed: bool = False


_NO_FACTS = SnapshotTestFacts()

# What the Owner of a private Agent is told about the test when releasing (agent-release-beginner-experience.md
# §4). For a private Agent the test is information, never a gate: every state can be released. Only a real
# regression or a safety-contract failure is a warning, and then the button reads "Release as vN anyway".
PrivateReleaseTestState = Literal[
    "passed",
    "passed_with_risks",
    "regressed",
    "safety_failed",
    "failed",
    "no_conclusion",
    "could_not_run",
    "outdated",
    "not_tested",
]
PRIVATE_RELEASE_TEST_STATES: frozenset[str] = frozenset(PrivateReleaseTestState.__args__)  # type: ignore[attr-defined]
PRIVATE_RELEASE_WARNING_STATES = frozenset({"regressed", "safety_failed"})
_RUNNING_EXECUTIONS = frozenset({"queued", "running", "finalizing"})


async def _snapshot_facts_conn(db: aiosqlite.Connection, summary_json: object) -> SnapshotTestFacts:
    """Read the regression and safety-contract facts of a finished test from its frozen case comparisons."""
    if not isinstance(summary_json, str) or not summary_json:
        return _NO_FACTS
    try:
        summary = json.loads(summary_json)
    except ValueError:
        return _NO_FACTS
    comparisons = summary.get("case_comparisons") if isinstance(summary, dict) else None
    if not isinstance(comparisons, list):
        return _NO_FACTS
    rows = [item for item in comparisons if isinstance(item, dict)]
    required_regression = any(item.get("classification") == "regressed" and item.get("required") is True for item in rows)
    failing_suites = sorted(
        {str(item.get("suite_revision_id")) for item in rows if item.get("classification") in {"unchanged_fail", "regressed"}},
    )
    contract_failed = False
    if failing_suites:
        async with db.execute(
            """
            SELECT 1 FROM agent_eval_suite_revision revision
            JOIN agent_eval_suite suite ON suite.id=revision.suite_id
            WHERE revision.id IN (SELECT value FROM json_each(?)) AND suite.suite_kind='platform_contract'
            LIMIT 1
            """,
            (json.dumps(failing_suites),),
        ) as cursor:
            contract_failed = await cursor.fetchone() is not None
    return SnapshotTestFacts(required_regression=required_regression, contract_failed=contract_failed)


def candidate_test_state(
    *,
    readiness: str | None,
    reason_codes: Sequence[str],
    execution_status: str | None,
    has_snapshot: bool,
    evidence_behind: bool,
    facts: SnapshotTestFacts,
) -> PrivateReleaseTestState:
    """The test state of one prepared candidate, as a private release records and shows it."""
    reasons = set(reason_codes)
    if not has_snapshot or readiness in {None, "missing"}:
        # A test that ended without results (stopped, failed to start its runs) measured nothing.
        return "could_not_run" if execution_status in {"failed", "cancelled"} else "not_tested"
    if evidence_behind or readiness == "stale":
        # Cases were saved, or the evidence was marked stale, after this test: it doesn't cover them.
        return "outdated"
    if "agent_safety_blocked" in reasons or facts.contract_failed:
        return "safety_failed"
    if facts.required_regression or "critical_regression" in reasons:
        return "regressed"
    if readiness == "blocked":
        return "failed"
    if readiness == "unknown":
        return "no_conclusion"
    if readiness == "ready_with_risks":
        return "passed_with_risks"
    return "passed"


def _workbench_projection(row: Mapping[str, Any], *, facts: SnapshotTestFacts = _NO_FACTS) -> dict[str, Any]:
    candidate_state = str(row["candidate_state"]) if row["candidate_state"] is not None else None
    readiness = str(row["readiness"]) if row["readiness"] is not None else None
    reasons = _control_reasons(row) if row["reason_codes_json"] is not None else ()
    audience = str(row["audience_mode"]) if row["audience_mode"] is not None else None
    execution_status = str(row["execution_status"]) if row["execution_status"] is not None else None
    has_override = row["active_override_id"] is not None
    draft_rebase_state = "current" if row["working_id"] is None or row["working_base_release_id"] == row["live_release_id"] else "required"
    # Live moved after the candidate was prepared (e.g. an audience contraction) but nothing has marked
    # it stale yet: activation and review would reject it with base_release_changed, so never offer them.
    base_outdated = (
        candidate_state is not None and row["candidate_base_release_id"] is not None and row["candidate_base_release_id"] != row["live_release_id"]
    )
    if base_outdated and "base_release_changed" not in reasons:
        reasons = (*reasons, "base_release_changed")
    if candidate_state is None:
        action = "prepare_candidate" if draft_rebase_state == "current" else "rebase_draft"
        disabled: list[str] = []
        display_state = "draft"
    elif base_outdated:
        action = "prepare_candidate" if draft_rebase_state == "current" else "rebase_draft"
        disabled, display_state = [], "candidate_stale"
    elif candidate_state == "pending":
        action, disabled, display_state = "wait_for_review", [], "pending_review"
    elif execution_status in {"queued", "running", "finalizing"}:
        action, disabled, display_state = "view_comparison", [], "comparison_running"
    elif readiness == "missing":
        action, disabled, display_state = "run_release_comparison", [], "prepared"
    elif readiness == "ready":
        action = "activate_candidate" if audience == "private" else "submit_for_review"
        disabled, display_state = [], "ready"
    elif readiness == "ready_with_risks":
        action = ("activate_candidate" if audience == "private" else "submit_for_review") if has_override else "resolve_risks"
        disabled, display_state = [], "ready_with_risks"
    elif readiness == "blocked":
        action, disabled, display_state = "fix_next_draft", list(reasons), "blocked"
    elif readiness == "unknown":
        # No verdict (an environment fault, a time limit, an incomparable result): running the
        # comparison again is how it gets one, so the reasons explain the state and never disable it.
        action, disabled, display_state = "retry_after_environment_fix", [], "unknown"
    elif readiness == "stale" and str(row["stale_subject"] or "") == "evidence":
        # Stale evidence is cured by running the comparison again on the active plan (for example after
        # the plan was updated), so the reasons explain the state and never disable that run.
        action, disabled, display_state = "run_release_comparison", [], "evidence_stale"
    else:
        action = "prepare_candidate" if draft_rebase_state == "current" else "rebase_draft"
        disabled, display_state = list(reasons), "candidate_stale"
    # The evaluation policy follows the latest saved suites, while a prepared candidate keeps the plan it
    # was compiled with. Same rule as Coach checks (coach_ci_plan_changed): the plan's policy revision is
    # not the newest one. Candidates frozen for review keep their plan, and a candidate that must be
    # prepared again gets the current policy then, so neither reports a plan to update.
    plan_behind = (
        candidate_state == "prepared"
        and display_state != "candidate_stale"
        and row["active_plan_id"] is not None
        and row["current_eval_policy_revision_id"] is not None
        and row["plan_policy_revision_id"] != row["current_eval_policy_revision_id"]
    )
    if plan_behind and action in {"retry_after_environment_fix", "resolve_risks", "activate_candidate", "submit_for_review"}:
        # Releasing would rely on results without the newer cases (the server refuses with
        # agent_eval_plan_behind). The next step is a comparison: the Owner's run recompiles the plan
        # from the latest saved cases first, recorded as a new plan, so there is no separate update step.
        action, disabled = "run_release_comparison", []
    running = execution_status in {"queued", "running", "finalizing"}
    # The Owner kept editing after preparing: the draft no longer matches what the candidate froze.
    # "Prepare from latest draft" replaces the candidate in one step (withdraw + prepare, server-side).
    draft_ahead = (
        candidate_state == "prepared"
        and display_state != "candidate_stale"
        and draft_rebase_state == "current"
        and row["working_id"] is not None
        and row["source_working_copy_id"] is not None
        and (str(row["working_id"]), int(row["working_row_version"])) != (str(row["source_working_copy_id"]), int(row["source_working_copy_version"]))
    )
    secondary: list[dict[str, Any]] = []
    if draft_ahead:
        # Replacing needs no comparison in flight; cancel it first.
        secondary.append({"enabled": not running, "kind": "prepare_from_latest_draft"})
    if candidate_state in {"prepared", "pending"}:
        secondary.append({"enabled": True, "kind": "withdraw_candidate"})
    if row["active_snapshot_id"] is not None:
        secondary.append({"enabled": True, "kind": "view_evidence"})
    if execution_status in {"queued", "running"}:
        secondary.append({"enabled": True, "kind": "cancel_comparison"})
    private_release = _private_release_projection(
        row,
        candidate_state=candidate_state,
        candidate_usable=candidate_state == "prepared" and display_state != "candidate_stale",
        draft_ahead=draft_ahead,
        draft_rebase_state=draft_rebase_state,
        readiness=readiness,
        reasons=reasons,
        execution_status=execution_status,
        plan_behind=plan_behind,
        facts=facts,
    )
    return {
        "private_release": private_release,
        "active_execution_summary": (
            {
                "can_cancel": execution_status in {"queued", "running"},
                "execution_id": str(row["execution_id"]),
                "failure_code": row["failure_code"],
                "finished_at": row["finished_at"],
                # Who started it, so the page can say a test began after Coach's edit (same rule as comparisons).
                "origin_kind": "internal_coach" if row["execution_coach_delegation_id"] else str(row["execution_actor_kind"]),
                "row_version": int(row["execution_row_version"]),
                "started_at": row["started_at"],
                "status": execution_status,
            }
            if execution_status is not None
            else None
        ),
        "active_plan_identity": (
            {
                # The Space the plan runs in; updating the plan keeps it.
                "execution_workspace_id": str(row["plan_workspace_id"]),
                "hash": str(row["plan_hash"]),
                "id": str(row["active_plan_id"]),
                "policy_revision_id": str(row["plan_policy_revision_id"]),
            }
            if row["active_plan_id"] is not None
            else None
        ),
        "active_snapshot_identity": (
            {
                "created_at": str(row["snapshot_created_at"]),
                # The comparison run that produced this snapshot; stays set after the run finishes,
                # unlike active_execution_summary, so failing cases can be loaded from it.
                "execution_id": str(row["snapshot_execution_id"]),
                "hash": str(row["snapshot_hash"]),
                "id": str(row["active_snapshot_id"]),
            }
            if row["active_snapshot_id"] is not None
            else None
        ),
        "active_override_identity": (
            {"hash": str(row["override_hash"]), "id": str(row["active_override_id"])} if row["active_override_id"] is not None else None
        ),
        # PRD R15: content-free batch overview of the active snapshot, also read by CI under gate:read.
        "comparison_overview": (
            comparison_overview(json.loads(str(row["snapshot_summary_json"])))
            if row["active_snapshot_id"] is not None and row["snapshot_summary_json"] is not None
            else None
        ),
        "candidate_identity": (
            {
                "audience_mode": audience,
                # The live release this candidate was prepared on; the UI marks the candidate
                # outdated when it differs from live_identity.release_id.
                "base_release_id": (str(row["candidate_base_release_id"]) if row["candidate_base_release_id"] is not None else None),
                "candidate_hash": str(row["candidate_hash"]),
                "candidate_id": str(row["candidate_id"]),
                "revision_id": str(row["candidate_revision_id"]),
                "release_sequence_if_activated": next_release_sequence(int(row["release_sequence"])),
                "row_version": int(row["candidate_row_version"]),
                "state": candidate_state,
            }
            if candidate_state is not None
            else None
        ),
        "control_row_version": int(row["control_row_version"]) if row["control_row_version"] is not None else None,
        "current_eval_policy_revision_id": row["current_eval_policy_revision_id"],
        "display_state": display_state,
        "draft_ahead_of_candidate": draft_ahead,
        "draft_rebase_state": draft_rebase_state,
        "evaluation_plan_behind": plan_behind,
        "live_identity": {
            "release_id": str(row["live_release_id"]),
            "release_sequence": int(row["release_sequence"]),
            "revision_id": str(row["live_revision_id"]),
        },
        "next_draft_identity": (
            {
                "base_release_id": str(row["working_base_release_id"]),
                "id": str(row["working_id"]),
                "row_version": int(row["working_row_version"]),
            }
            if row["working_id"] is not None
            else None
        ),
        "primary_action": {"disabled_reason_codes": disabled, "enabled": not disabled, "kind": action},
        "readiness": readiness,
        "reason_codes": list(reasons),
        "schema_version": 1,
        "secondary_actions": secondary,
        "stale_subject": str(row["stale_subject"]) if row["stale_subject"] is not None else None,
    }


def _private_release_projection(
    row: Mapping[str, Any],
    *,
    candidate_state: str | None,
    candidate_usable: bool,
    draft_ahead: bool,
    draft_rebase_state: str,
    readiness: str | None,
    reasons: Sequence[str],
    execution_status: str | None,
    plan_behind: bool,
    facts: SnapshotTestFacts,
) -> dict[str, Any] | None:
    """Release as vN for a private Agent: what releasing the latest saved draft would record, or None.

    The release takes the latest saved draft. A candidate that already covers it keeps its test result;
    otherwise the draft is untested (the server prepares it on release). A test in flight is stopped first.
    """
    if candidate_state in {"pending", "approved"}:
        return None
    covers_draft = candidate_usable and not draft_ahead
    if not covers_draft and row["working_id"] is None:
        return None
    # The audience the release would get: the candidate's when it is what would ship, else the saved draft's.
    audience = row["audience_mode"] if covers_draft else row["working_audience_mode"]
    if audience != "private":
        return None
    running = covers_draft and execution_status in _RUNNING_EXECUTIONS
    state: PrivateReleaseTestState = (
        candidate_test_state(
            readiness=readiness,
            reason_codes=reasons,
            execution_status=execution_status,
            has_snapshot=row["active_snapshot_id"] is not None,
            evidence_behind=plan_behind or str(row["stale_subject"] or "") == "evidence",
            facts=facts,
        )
        if covers_draft
        else "not_tested"
    )
    blocked = "agent_release_test_running" if running else "agent_version_test_rebase_required" if draft_rebase_state == "required" else None
    return {
        "available": blocked is None,
        "blocked_reason_code": blocked,
        # The current candidate already freezes the saved draft; otherwise releasing prepares it first.
        "candidate_covers_draft": covers_draft,
        "release_sequence": next_release_sequence(int(row["release_sequence"])),
        "test_state": state,
        "warning": state in PRIVATE_RELEASE_WARNING_STATES,
        # The reasons behind the state, only when the state comes from this draft's own test.
        "reason_codes": list(reasons) if covers_draft and state not in {"passed", "not_tested"} else [],
    }


def _normalize_reason_overrides(values: Sequence[Mapping[str, str]]) -> tuple[dict[str, str], ...]:
    if not values:
        raise AgentVersionError("agent_eval_override_reason_required")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        if set(item) != {"reason_code", "reason_note"}:
            raise AgentVersionError("agent_eval_override_reason_invalid")
        code = item.get("reason_code")
        note = item.get("reason_note")
        if not isinstance(code, str) or code not in _SOFT_RISK_REASONS or code in seen:
            raise AgentVersionError("agent_eval_override_reason_invalid")
        if not isinstance(note, str) or not note.strip() or len(note.strip()) > _MAX_REASON_NOTE:
            raise AgentVersionError("agent_eval_override_reason_note_invalid")
        seen.add(code)
        normalized.append({"reason_code": code, "reason_note": note.strip()})
    return tuple(sorted(normalized, key=lambda item: item["reason_code"]))


def _normalize_selection(value: Mapping[str, Any]) -> dict[str, list[str]]:
    if set(value) != {"regression_category_ids", "suite_revision_ids"}:
        raise AgentVersionError("agent_eval_team_evidence_selection_invalid")
    normalized: dict[str, list[str]] = {}
    for key in ("suite_revision_ids", "regression_category_ids"):
        items = value.get(key)
        if not isinstance(items, list) or len(items) > _MAX_SELECTION_ITEMS or any(not isinstance(item, str) or not item for item in items):
            raise AgentVersionError("agent_eval_team_evidence_selection_invalid")
        if items != sorted(set(items)):
            raise AgentVersionError("agent_eval_team_evidence_selection_invalid")
        normalized[key] = items
    return normalized


def _project_team_evidence(reviewer: Mapping[str, Any], *, selection: Mapping[str, list[str]]) -> dict[str, Any]:
    summaries = reviewer.get("case_summaries")
    if not isinstance(summaries, list):
        raise AgentVersionError("agent_eval_snapshot_malformed")
    suite_ids = set(selection["suite_revision_ids"])
    available_suites = {str(item.get("suite_revision_id")) for item in summaries if isinstance(item, dict)}
    if not suite_ids <= available_suites:
        raise AgentVersionError("agent_eval_team_evidence_selection_invalid")
    available_categories = {
        str(value)
        for item in summaries
        if isinstance(item, dict)
        for value in [item.get("classification"), *(item.get("reason_codes") or [])]
        if isinstance(value, str)
    }
    categories = set(selection["regression_category_ids"])
    if not categories <= available_categories:
        raise AgentVersionError("agent_eval_team_evidence_selection_invalid")
    evidence: dict[str, Any] = {
        "case_summaries": [item for item in summaries if isinstance(item, dict) and str(item.get("suite_revision_id")) in suite_ids],
        "regression_categories": sorted(categories),
        "schema_version": 1,
    }
    # The record names every point the Owner released past, whatever evidence they chose to share.
    released_past = [
        {
            "case_revision_id": str(item.get("case_revision_id")),
            "point_ids": [str(point) for point in item["unmet_point_ids"] if isinstance(point, str)],
            "suite_revision_id": str(item.get("suite_revision_id")),
        }
        for item in summaries
        if isinstance(item, dict) and item.get("point_only") is True and isinstance(item.get("unmet_point_ids"), list) and item["unmet_point_ids"]
    ]
    if released_past:
        evidence["released_past_points"] = released_past
    return evidence


def _override_from_row(row: Mapping[str, Any], *, control_row_version: int) -> AgentEvalOverride:
    reasons = _array(row["reason_overrides_json"], code="agent_eval_override_malformed")
    if any(not isinstance(item, dict) for item in reasons):
        raise AgentVersionError("agent_eval_override_malformed")
    return AgentEvalOverride(
        id=str(row["id"]),
        candidate_id=str(row["candidate_id"]),
        plan_id=str(row["plan_id"]),
        snapshot_id=str(row["snapshot_id"]),
        override_hash=str(row["override_hash"]),
        reason_overrides=tuple({"reason_code": str(item["reason_code"]), "reason_note": str(item["reason_note"])} for item in reasons),
        team_evidence=_object(row["team_evidence_json"], code="agent_eval_override_malformed"),
        created_at=str(row["created_at"]),
        control_row_version=control_row_version,
    )


def _control_reasons(row: Mapping[str, Any]) -> tuple[str, ...]:
    values = _array(row["reason_codes_json"], code="agent_eval_control_malformed")
    if any(not isinstance(value, str) for value in values):
        raise AgentVersionError("agent_eval_control_malformed")
    return tuple(str(value) for value in values)


def _object(value: object, *, code: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentVersionError(code) from exc
    if not isinstance(decoded, dict):
        raise AgentVersionError(code)
    return decoded


def _array(value: object, *, code: str) -> list[Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentVersionError(code) from exc
    if not isinstance(decoded, list):
        raise AgentVersionError(code)
    return decoded


def _string_set(value: object, *, code: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentVersionError(code)
    return set(value)


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise AgentVersionError("agent_version_idempotency_key_invalid")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _hash_document(value: object) -> str:
    return _hash_text(_canonical_json(value))
