"""Gateway orchestration for server-authoritative team LLM readiness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bowhead_core.team_llm_control_token import issue_control_token
from bowhead_core.team_llm_evidence import Capability
from bowhead_core.team_llm_readiness import (
    ReadinessDecision,
    ReadinessFact,
    RequiredAssignment,
    build_catalog_assignments,
    build_required_assignments,
    compute_team_llm_readiness,
    external_readiness_rows,
)
from bowhead_core.team_llm_readiness_store import (
    TeamLlmReadinessStore,
    V2Enrollment,
    assignment_semantic_hash,
)
from bowhead_core.team_llm_verification_store import (
    TeamLlmVerificationStore,
)


@dataclass(frozen=True, slots=True)
class ReadinessEvaluation:
    decision: ReadinessDecision
    enrollment: V2Enrollment
    migration_eligible: bool
    assignments: tuple[RequiredAssignment, ...]
    provider_ids: dict[str, str]


class ProviderIdentityRequiredError(ValueError):
    """A draft Provider has not completed the stable-identity precheck."""

    def __init__(self, runtime_key: str) -> None:
        super().__init__("Provider identity precheck is required")
        self.runtime_key = runtime_key


async def provider_ids_for_config(
    *,
    team_id: str,
    config: dict[str, Any],
    verification_store: TeamLlmVerificationStore,
    create_legacy_if_missing: bool,
) -> dict[str, str]:
    provider_ids: dict[str, str] = {}
    for runtime_key in config.get("providers") or {}:
        identity = await verification_store.get_provider_identity_by_runtime_key(
            team_id=team_id,
            runtime_key=str(runtime_key),
        )
        if identity is None and create_legacy_if_missing:
            identity = await verification_store.ensure_legacy_provider_identity(
                team_id=team_id,
                runtime_key=str(runtime_key),
            )
        if identity is None:
            raise ProviderIdentityRequiredError(str(runtime_key))
        provider_ids[str(runtime_key)] = identity.provider_id
    return provider_ids


def _safe_assignments(
    config: dict[str, Any],
    *,
    provider_ids: dict[str, str],
) -> tuple[RequiredAssignment, ...]:
    if not config.get("providers"):
        return ()
    tiers = config.get("tiers")
    if isinstance(tiers, dict) and not tiers.get("primary"):
        # Saved connection setup is visible to administrators but not runnable.
        return ()
    return build_required_assignments(
        config,
        provider_ids=provider_ids,
    )


async def evaluate_team_llm_readiness(
    *,
    db_path: str,
    team_id: str,
    actor_user_id: str,
    current_config: dict[str, Any],
    draft_config: dict[str, Any],
    expected_version: int,
    enrollment_source: str,
    now: datetime | None = None,
) -> ReadinessEvaluation:
    timestamp = now or datetime.now(UTC)
    verification_store = TeamLlmVerificationStore(db_path)
    readiness_store = TeamLlmReadinessStore(db_path)
    current_provider_ids = await provider_ids_for_config(
        team_id=team_id,
        config=current_config,
        verification_store=verification_store,
        create_legacy_if_missing=True,
    )
    draft_provider_ids = await provider_ids_for_config(
        team_id=team_id,
        config=draft_config,
        verification_store=verification_store,
        create_legacy_if_missing=False,
    )
    current_assignments = _safe_assignments(
        current_config,
        provider_ids=current_provider_ids,
    )
    assignments = _safe_assignments(
        draft_config,
        provider_ids=draft_provider_ids,
    )
    enrollment, _created = await readiness_store.ensure_enrollment(
        team_id=team_id,
        config_version=expected_version,
        assignments=current_assignments,
        source=enrollment_source,
        actor_user_id=actor_user_id,
        now=timestamp,
    )

    facts: dict[
        tuple[str, str, Capability, str],
        ReadinessFact,
    ] = {}
    for assignment in assignments:
        projection = await verification_store.project_evidence(
            team_id=team_id,
            provider_id=assignment.provider_id,
            model_id=assignment.model_id,
            capability=assignment.capability,
            current_signature=assignment.config_signature,
            now=timestamp,
        )
        facts[assignment.scope_key] = ReadinessFact(
            state=projection.fact_state,
            evidence_id=projection.evidence_id,
            safe_code=projection.safe_code,
            observed_at=(
                projection.observed_at.isoformat().replace(
                    "+00:00",
                    "Z",
                )
                if projection.observed_at
                else None
            ),
            fresh_until=(
                projection.fresh_until.isoformat().replace(
                    "+00:00",
                    "Z",
                )
                if projection.fresh_until
                else None
            ),
            attempt_state=projection.attempt_state,
            attempt_safe_code=projection.attempt_safe_code,
            actual_model_id=projection.actual_model_id,
        )

    previous_scopes = frozenset(assignment.scope_key for assignment in current_assignments)
    active_acceptances = await readiness_store.active_acceptance_keys(team_id=team_id)
    active_grandfather_scopes = await readiness_store.active_grandfather_scopes(team_id=team_id)
    draft_scopes = frozenset(assignment.scope_key for assignment in assignments)
    enrollment_matches_current = enrollment.semantic_hash == assignment_semantic_hash(current_assignments)
    draft_matches_enrollment = enrollment.semantic_hash == assignment_semantic_hash(assignments)
    has_baseline = await readiness_store.has_migration_baseline(team_id=team_id)
    migration_eligible = (
        not has_baseline
        and enrollment_matches_current
        and draft_matches_enrollment
        and not any(fact.state == "failed" for fact in facts.values())
        and bool(assignments)
    )
    enrollment_scopes = frozenset(
        (
            str(row["provider_id"]),
            str(row["model_id"]),
            str(row["capability"]),
            str(row["config_signature"]),
        )
        for row in enrollment.assignments
    )
    grandfather_scopes = enrollment_scopes if migration_eligible else active_grandfather_scopes
    baseline_will_invalidate = bool(active_grandfather_scopes and draft_scopes != active_grandfather_scopes)
    decision = compute_team_llm_readiness(
        expected_version=expected_version,
        assignments=assignments,
        facts=facts,
        previous_scopes=previous_scopes,
        active_acceptances=active_acceptances,
        grandfather_scopes=grandfather_scopes,
        baseline_will_invalidate=baseline_will_invalidate,
        external_rows=external_readiness_rows(draft_config),
    )
    return ReadinessEvaluation(
        decision=decision,
        enrollment=enrollment,
        migration_eligible=migration_eligible,
        assignments=assignments,
        provider_ids=draft_provider_ids,
    )


def readiness_response(
    evaluation: ReadinessEvaluation,
    *,
    team_id: str | None = None,
    actor_user_id: str | None = None,
    expected_version: int | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    decision = evaluation.decision
    timestamp = as_of or datetime.now(UTC)
    expires_at = int((timestamp + timedelta(minutes=15)).timestamp())
    acceptance_rows: list[dict[str, Any]] = []
    if team_id is not None and actor_user_id is not None and expected_version is not None:
        for request in decision.required_acceptances:
            assignment = request.assignment
            acceptance_rows.append(
                {
                    "acceptance_token": issue_control_token(
                        {
                            "team_id": team_id,
                            "actor_user_id": actor_user_id,
                            "expected_version": expected_version,
                            "draft_semantic_hash": (decision.draft_semantic_hash),
                            "provider_id": assignment.provider_id,
                            "model_id": assignment.model_id,
                            "capability": assignment.capability,
                            "role_key": assignment.role_key,
                            "config_signature": (assignment.config_signature),
                            "testability_contract_version": (assignment.testability_contract_version),
                            "risk_code": request.risk_code,
                        },
                        purpose="readiness-risk-acceptance",
                        expires_at_epoch=expires_at,
                    ),
                    "risk_code": request.risk_code,
                    "provider_id": assignment.provider_id,
                    "model_id": assignment.model_id,
                    "capability": assignment.capability,
                    "role_key": assignment.role_key,
                }
            )
    migration_acceptance: dict[str, Any] | None = None
    if evaluation.migration_eligible and team_id is not None and actor_user_id is not None and expected_version is not None:
        migration_acceptance = {
            "acceptance_token": issue_control_token(
                {
                    "team_id": team_id,
                    "actor_user_id": actor_user_id,
                    "expected_version": expected_version,
                    "draft_semantic_hash": decision.draft_semantic_hash,
                    "enrolled_config_version": (evaluation.enrollment.config_version),
                    "enrolled_semantic_hash": (evaluation.enrollment.semantic_hash),
                    "assignments_semantic_hash": (assignment_semantic_hash(evaluation.assignments)),
                },
                purpose="readiness-migration-acceptance",
                expires_at_epoch=expires_at,
            ),
            "risk_code": "pre_v2_configuration_unverified",
        }
    return {
        "as_of": timestamp.isoformat().replace("+00:00", "Z"),
        "draft_semantic_hash": decision.draft_semantic_hash,
        "may_apply": decision.may_apply,
        "rows": [
            {
                "role_key": row.assignment.role_key,
                "provider_id": row.assignment.provider_id,
                "provider_display_name": (row.assignment.provider_display_name),
                "model_id": row.assignment.model_id,
                "capability": row.assignment.capability,
                "routing_critical": row.assignment.routing_critical,
                "declared": row.assignment.declared,
                "fact_state": row.fact.state,
                "evidence_id": row.fact.evidence_id,
                "observed_at": row.fact.observed_at,
                "fresh_until": row.fact.fresh_until,
                "safe_code": row.fact.safe_code,
                "attempt_state": row.fact.attempt_state,
                "attempt_safe_code": row.fact.attempt_safe_code,
                "actual_model_id": row.fact.actual_model_id,
                "status": row.status,
                "changed": row.changed,
                "issue_code": row.issue_code,
            }
            for row in decision.rows
        ],
        "blocking": [
            {
                "code": issue.code,
                "field_path": issue.field_path,
                "role_key": issue.role_key,
                "provider_id": issue.provider_id,
                "model_id": issue.model_id,
                "capability": issue.capability,
                "action_code": issue.action_code,
            }
            for issue in decision.blocking
        ],
        "warnings": [
            {
                "code": issue.code,
                "field_path": issue.field_path,
                "role_key": issue.role_key,
                "provider_id": issue.provider_id,
                "model_id": issue.model_id,
                "capability": issue.capability,
                "action_code": issue.action_code,
            }
            for issue in decision.warnings
        ],
        "required_acceptances": acceptance_rows,
        "migration_acceptance": migration_acceptance,
        "external_rows": [
            {
                "subject_kind": row.subject_kind,
                "configured": row.configured,
                "status": row.status,
            }
            for row in decision.external_rows
        ],
        "baseline_will_invalidate": (decision.baseline_will_invalidate),
    }


async def catalog_verification_entries(
    *,
    db_path: str,
    team_id: str,
    config: dict[str, Any],
    provider_ids: dict[str, str],
) -> list[dict[str, Any]]:
    """Use the same transport signatures as production for the full model catalog."""
    store = TeamLlmVerificationStore(db_path)
    entries = []
    for assignment in build_catalog_assignments(config, provider_ids=provider_ids):
        projection = await store.project_evidence(
            team_id=team_id,
            provider_id=assignment.provider_id,
            model_id=assignment.model_id,
            capability=assignment.capability,
            current_signature=assignment.config_signature,
        )
        entries.append(
            {
                "provider_id": assignment.provider_id,
                "model_id": assignment.model_id,
                "capability": assignment.capability,
                "fact_state": projection.fact_state,
                "attempt_state": projection.attempt_state,
                "evidence_id": projection.evidence_id,
                "observed_at": projection.observed_at.isoformat() if projection.observed_at else None,
                "fresh_until": projection.fresh_until.isoformat() if projection.fresh_until else None,
                "safe_code": projection.safe_code,
                "actual_model_id": projection.actual_model_id,
            }
        )
    return entries
