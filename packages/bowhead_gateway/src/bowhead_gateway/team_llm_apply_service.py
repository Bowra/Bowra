"""Atomic V2 team LLM Apply with in-transaction readiness recomputation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.audit_actions import AuditAction
from bowhead_core.llm_config_crypto import (
    decrypt_llm_config,
    encrypt_llm_config,
)
from bowhead_core.team_llm_control_token import (
    ControlTokenError,
    verify_control_token,
)
from bowhead_core.team_llm_evidence import (
    AttemptRecord,
    Capability,
    EvidenceRecord,
    legacy_provider_id,
    project_latest_evidence,
)
from bowhead_core.team_llm_readiness import (
    READINESS_CONTRACT_VERSION,
    ReadinessDecision,
    ReadinessFact,
    RequiredAssignment,
    compute_team_llm_readiness,
    external_readiness_rows,
)
from bowhead_core.team_llm_readiness_store import (
    ROLLOUT_GENERATION,
    acceptance_key_from_row,
    assignment_semantic_hash,
    safe_assignment_snapshot,
)

from bowhead_gateway.team_llm_readiness_service import _safe_assignments

logger = logging.getLogger(__name__)


class TeamLlmApplyError(RuntimeError):
    code = "llm_apply_failed"


class ConfigVersionConflict(TeamLlmApplyError):
    code = "config_version_conflict"


class ReadinessReviewRequired(TeamLlmApplyError):
    code = "readiness_review_required"


class ReadinessReviewStale(TeamLlmApplyError):
    code = "readiness_review_stale"


class ReadinessBlocked(TeamLlmApplyError):
    code = "readiness_blocked"


class RiskAcceptanceRequired(TeamLlmApplyError):
    code = "risk_acceptance_required"


class MigrationAcceptanceRequired(TeamLlmApplyError):
    code = "migration_acceptance_required"


@dataclass(frozen=True, slots=True)
class V2ApplyResult:
    version: int
    snapshot_id: str
    decision: ReadinessDecision


def _now_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _scope(
    row: dict[str, Any],
) -> tuple[str, str, Capability, str]:
    return (
        str(row["provider_id"]),
        str(row["model_id"]),
        str(row["capability"]),  # type: ignore[return-value]
        str(row["config_signature"]),
    )


async def _provider_ids(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    config: dict[str, Any],
    create_legacy: bool,
    now_iso: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for runtime_key in config.get("providers") or {}:
        row = await (
            await db.execute(
                """
                SELECT provider_id
                FROM team_llm_provider_identities
                WHERE team_id = ? AND runtime_key = ?
                  AND state IN ('reserved', 'active')
                LIMIT 1
                """,
                (team_id, str(runtime_key)),
            )
        ).fetchone()
        if row is None and create_legacy:
            provider_id = legacy_provider_id(
                team_id,
                str(runtime_key),
            )
            await db.execute(
                """
                INSERT INTO team_llm_provider_identities(
                    team_id, provider_id, runtime_key, state,
                    creation_source, created_at, activated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    team_id,
                    provider_id,
                    str(runtime_key),
                    "active",
                    "legacy_migration",
                    now_iso,
                    now_iso,
                ),
            )
        elif row is None:
            raise ReadinessBlocked(f"Provider {runtime_key!r} has no reserved identity")
        else:
            provider_id = str(row["provider_id"])
        result[str(runtime_key)] = provider_id
    return result


async def _facts(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    assignments: tuple[RequiredAssignment, ...],
    now: datetime,
) -> dict[tuple[str, str, Capability, str], ReadinessFact]:
    result: dict[
        tuple[str, str, Capability, str],
        ReadinessFact,
    ] = {}
    for assignment in assignments:
        evidence_rows = await (
            await db.execute(
                """
                SELECT id, config_signature, outcome, safe_code,
                       observed_at, fresh_until
                FROM team_llm_capability_evidence
                WHERE team_id = ? AND provider_id = ?
                  AND model_id = ? AND capability = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT 100
                """,
                (
                    team_id,
                    assignment.provider_id,
                    assignment.model_id,
                    assignment.capability,
                ),
            )
        ).fetchall()
        attempt_row = await (
            await db.execute(
                """
                SELECT state, safe_code, completed_at
                FROM team_llm_verification_items
                WHERE team_id = ? AND provider_id = ?
                  AND model_id = ? AND capability = ?
                  AND config_signature = ?
                  AND state IN (
                      'queued', 'verifying', 'errored',
                      'cancelled', 'interrupted'
                  )
                ORDER BY COALESCE(
                    completed_at,
                    request_started_at,
                    lease_until
                ) DESC, id DESC
                LIMIT 1
                """,
                (
                    team_id,
                    assignment.provider_id,
                    assignment.model_id,
                    assignment.capability,
                    assignment.config_signature,
                ),
            )
        ).fetchone()
        attempt = (
            AttemptRecord(
                state=str(attempt_row["state"]),  # type: ignore[arg-type]
                safe_code=(str(attempt_row["safe_code"]) if attempt_row["safe_code"] else None),
                completed_at=(
                    datetime.fromisoformat(
                        str(attempt_row["completed_at"]).replace(
                            "Z",
                            "+00:00",
                        )
                    )
                    if attempt_row["completed_at"]
                    else None
                ),
            )
            if attempt_row is not None
            else None
        )
        projection = project_latest_evidence(
            [
                EvidenceRecord(
                    evidence_id=str(row["id"]),
                    config_signature=str(row["config_signature"]),
                    outcome=str(row["outcome"]),  # type: ignore[arg-type]
                    safe_code=str(row["safe_code"]),
                    observed_at=datetime.fromisoformat(
                        str(row["observed_at"]).replace(
                            "Z",
                            "+00:00",
                        )
                    ),
                    fresh_until=datetime.fromisoformat(
                        str(row["fresh_until"]).replace(
                            "Z",
                            "+00:00",
                        )
                    ),
                )
                for row in evidence_rows
            ],
            current_signature=assignment.config_signature,
            now=now,
            attempt=attempt,
        )
        result[assignment.scope_key] = ReadinessFact(
            state=projection.fact_state,
            evidence_id=projection.evidence_id,
            safe_code=projection.safe_code,
            observed_at=(_now_iso(projection.observed_at) if projection.observed_at else None),
            fresh_until=(_now_iso(projection.fresh_until) if projection.fresh_until else None),
            attempt_state=projection.attempt_state,
            attempt_safe_code=projection.attempt_safe_code,
        )
    return result


async def apply_team_llm_v2(
    *,
    db_path: str,
    team_id: str,
    actor_user_id: str,
    current_config_for_readiness: dict[str, Any],
    config_to_save: dict[str, Any],
    expected_version: int,
    draft_semantic_hash: str | None,
    acceptance_tokens: list[str],
    migration_acceptance_token: str | None,
    audit_ip: str | None,
    now: datetime | None = None,
) -> V2ApplyResult:
    if not draft_semantic_hash:
        raise ReadinessReviewRequired("V2 Apply requires a reviewed readiness decision")
    timestamp = now or datetime.now(UTC)
    now_iso = _now_iso(timestamp)
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            team = await (
                await db.execute(
                    """
                    SELECT llm_config_encrypted, llm_config_version
                    FROM teams WHERE id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
            if team is None or int(team["llm_config_version"]) != (expected_version):
                raise ConfigVersionConflict("team LLM config version changed")
            stored_config = decrypt_llm_config(str(team["llm_config_encrypted"])) if team["llm_config_encrypted"] else {}
            del stored_config
            current_provider_ids = await _provider_ids(
                db,
                team_id=team_id,
                config=current_config_for_readiness,
                create_legacy=True,
                now_iso=now_iso,
            )
            draft_provider_ids = await _provider_ids(
                db,
                team_id=team_id,
                config=config_to_save,
                create_legacy=False,
                now_iso=now_iso,
            )
            current_assignments = _safe_assignments(
                current_config_for_readiness,
                provider_ids=current_provider_ids,
            )
            assignments = _safe_assignments(
                config_to_save,
                provider_ids=draft_provider_ids,
            )
            enrollment_snapshot = safe_assignment_snapshot(current_assignments)
            enrollment_hash = assignment_semantic_hash(current_assignments)
            enrollment_cursor = await db.execute(
                """
                INSERT OR IGNORE INTO team_llm_v2_enrollments(
                    team_id, enrolled_config_version,
                    enrolled_semantic_hash, assignments_json,
                    rollout_generation, enrollment_source, enrolled_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    team_id,
                    expected_version,
                    enrollment_hash,
                    json.dumps(
                        enrollment_snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    ROLLOUT_GENERATION,
                    "first_v2_write",
                    now_iso,
                ),
            )
            if enrollment_cursor.rowcount == 1:
                await db.execute(
                    """
                    INSERT INTO admin_audit_log(
                        id, actor_user_id, team_id, action,
                        target_kind, target_id, scope,
                        payload_json, ts, ip
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"aud_{uuid4().hex}",
                        actor_user_id,
                        team_id,
                        AuditAction.LLM_V2_ENROLLED,
                        "team_llm_config",
                        team_id,
                        "team",
                        json.dumps(
                            {
                                "config_version": expected_version,
                                "semantic_hash": enrollment_hash,
                                "rollout_generation": (ROLLOUT_GENERATION),
                            },
                            separators=(",", ":"),
                        ),
                        now_iso,
                        audit_ip,
                    ),
                )
            enrollment = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_v2_enrollments
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
            assert enrollment is not None
            enrollment_rows = json.loads(str(enrollment["assignments_json"]))

            facts = await _facts(
                db,
                team_id=team_id,
                assignments=assignments,
                now=timestamp,
            )
            acceptance_rows = await (
                await db.execute(
                    """
                    SELECT acceptance.*
                    FROM team_llm_risk_acceptances AS acceptance
                    LEFT JOIN team_llm_risk_acceptance_retirements AS retired
                      ON retired.risk_acceptance_id = acceptance.id
                    WHERE acceptance.team_id = ?
                      AND retired.risk_acceptance_id IS NULL
                    """,
                    (team_id,),
                )
            ).fetchall()
            # ★ Use the same key construction as checks (7 elements including risk_code). Previously an inline
            # 6-tuple never matched acceptance_key_for's 7-tuple, hiding all existing acceptances during apply;
            # status reverted to required → semantic hashes necessarily differed → deterministic endless 409s
            # (reproduced on 116; second cause of the 08-24 customer's '加模型后应用 409' report).
            active_acceptances = frozenset(acceptance_key_from_row(row) for row in acceptance_rows)
            baseline = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_v2_migration_baselines
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
            invalidation = await (
                await db.execute(
                    """
                    SELECT * FROM
                        team_llm_v2_migration_baseline_invalidations
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
            active_grandfather = (
                frozenset(_scope(row) for row in json.loads(str(baseline["assignments_json"])))
                if baseline is not None and invalidation is None
                else frozenset()
            )
            previous_scopes = frozenset(row.scope_key for row in current_assignments)
            draft_scopes = frozenset(row.scope_key for row in assignments)
            migration_eligible = (
                baseline is None
                and str(enrollment["enrolled_semantic_hash"])
                == assignment_semantic_hash(current_assignments)
                == assignment_semantic_hash(assignments)
                and not any(fact.state == "failed" for fact in facts.values())
                and bool(assignments)
            )
            enrollment_scopes = frozenset(_scope(row) for row in enrollment_rows)
            grandfather_scopes = enrollment_scopes if migration_eligible else active_grandfather
            baseline_will_invalidate = bool(active_grandfather and draft_scopes != active_grandfather)
            decision = compute_team_llm_readiness(
                expected_version=expected_version,
                assignments=assignments,
                facts=facts,
                previous_scopes=previous_scopes,
                active_acceptances=active_acceptances,
                grandfather_scopes=grandfather_scopes,
                baseline_will_invalidate=(baseline_will_invalidate),
                external_rows=external_readiness_rows(config_to_save),
            )
            if decision.draft_semantic_hash != draft_semantic_hash:
                # feedback-triage-2026-08-24: customer reported '点应用报 409，重新检查又回到应用
                # 按钮', while errors said only '这份检查结果已经失效', without identifying **which row** changed;
                # investigations required guessing. Log per-row conclusions so operations/support can locate causes directly.
                # (Bookkeeping fields such as evidence_id no longer enter the hash; see team_llm_readiness comments.
                # Reaching here means actual conclusions changed.)
                logger.warning(
                    "team_llm.readiness_review_stale team=%s expected_version=%s reviewed_hash=%s recomputed_hash=%s rows=%s",
                    team_id,
                    expected_version,
                    draft_semantic_hash,
                    decision.draft_semantic_hash,
                    [
                        {
                            "role_key": row.assignment.role_key,
                            "model_id": row.assignment.model_id,
                            "capability": row.assignment.capability,
                            "fact_state": row.fact.state,
                            "status": row.status,
                        }
                        for row in decision.rows
                    ],
                )
                raise ReadinessReviewStale("readiness inputs changed after Review")
            if decision.blocking:
                raise ReadinessBlocked("readiness contains blocking rows")

            uses_migration = any(row.grandfathered for row in decision.rows)
            if uses_migration:
                if not migration_eligible or not migration_acceptance_token:
                    raise MigrationAcceptanceRequired("migration acceptance is required")
                try:
                    migration_payload = verify_control_token(
                        migration_acceptance_token,
                        purpose="readiness-migration-acceptance",
                    )
                except ControlTokenError as exc:
                    raise MigrationAcceptanceRequired("migration acceptance is invalid") from exc
                expected_migration = {
                    "team_id": team_id,
                    "actor_user_id": actor_user_id,
                    "expected_version": expected_version,
                    "draft_semantic_hash": draft_semantic_hash,
                    "enrolled_config_version": int(enrollment["enrolled_config_version"]),
                    "enrolled_semantic_hash": str(enrollment["enrolled_semantic_hash"]),
                    "assignments_semantic_hash": (assignment_semantic_hash(assignments)),
                }
                if any(migration_payload.get(key) != value for key, value in expected_migration.items()):
                    raise MigrationAcceptanceRequired("migration acceptance scope changed")

            supplied_risks: dict[
                tuple[str, str, str, str, str, str],
                dict[str, Any],
            ] = {}
            for token in acceptance_tokens:
                try:
                    payload = verify_control_token(
                        token,
                        purpose="readiness-risk-acceptance",
                    )
                except ControlTokenError as exc:
                    raise RiskAcceptanceRequired("risk acceptance is invalid") from exc
                if (
                    payload.get("team_id") != team_id
                    or payload.get("actor_user_id") != actor_user_id
                    or payload.get("expected_version") != expected_version
                    or payload.get("draft_semantic_hash") != draft_semantic_hash
                ):
                    raise RiskAcceptanceRequired("risk acceptance scope changed")
                # Key includes risk_code, same shape as RequiredAssignment.acceptance_key_for.
                # Accepting untestable risk must not count as accepting tested-but-failed risk: different declarations.
                key = (
                    str(payload.get("provider_id")),
                    str(payload.get("model_id")),
                    str(payload.get("capability")),
                    str(payload.get("role_key")),
                    str(payload.get("config_signature")),
                    str(payload.get("testability_contract_version")),
                    str(payload.get("risk_code")),
                )
                supplied_risks[key] = payload
            for required in decision.required_acceptances:
                key = tuple(str(value) for value in required.assignment.acceptance_key_for(required.risk_code))
                if supplied_risks.get(key) is None:
                    raise RiskAcceptanceRequired("all exact risk acceptances are required")

            new_version = expected_version + 1
            for required in decision.required_acceptances:
                assignment = required.assignment
                await db.execute(
                    """
                    INSERT INTO team_llm_risk_acceptances(
                        id, team_id, provider_id, model_id, capability,
                        role_key, config_signature,
                        testability_contract_version, risk_code,
                        accepted_by, accepted_at, applied_config_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"llmra_{uuid4().hex}",
                        team_id,
                        assignment.provider_id,
                        assignment.model_id,
                        assignment.capability,
                        assignment.role_key,
                        assignment.config_signature,
                        assignment.testability_contract_version,
                        required.risk_code,
                        actor_user_id,
                        now_iso,
                        new_version,
                    ),
                )

            # Retirement intentionally **excludes risk_code**: it checks whether the acceptance's scope remains
            # (scope_removed / signature_changed), independent of risk type. Changed risk types are handled
            # by evaluation's naturally mismatching acceptance_key_for, and must not be recorded here as retirement.
            active_keys = {
                (
                    row.provider_id,
                    row.model_id,
                    row.capability,
                    row.role_key,
                    row.config_signature,
                    row.testability_contract_version,
                )
                for row in assignments
            }
            for row in acceptance_rows:
                key = (
                    str(row["provider_id"]),
                    str(row["model_id"]),
                    str(row["capability"]),
                    str(row["role_key"]),
                    str(row["config_signature"]),
                    str(row["testability_contract_version"]),
                )
                if key in active_keys:
                    continue
                reason = "scope_removed" if not any(assignment.role_key == row["role_key"] for assignment in assignments) else "signature_changed"
                await db.execute(
                    """
                    INSERT OR IGNORE INTO
                        team_llm_risk_acceptance_retirements(
                            risk_acceptance_id, team_id, retired_at,
                            retired_by_config_version, reason
                        ) VALUES (?,?,?,?,?)
                    """,
                    (
                        str(row["id"]),
                        team_id,
                        now_iso,
                        new_version,
                        reason,
                    ),
                )

            if uses_migration:
                await db.execute(
                    """
                    INSERT INTO team_llm_v2_migration_baselines(
                        team_id, enrollment_config_version,
                        enrollment_semantic_hash, baseline_config_version,
                        baseline_semantic_hash, assignments_json,
                        accepted_by, accepted_at, applied_config_version
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        team_id,
                        int(enrollment["enrolled_config_version"]),
                        str(enrollment["enrolled_semantic_hash"]),
                        expected_version,
                        assignment_semantic_hash(assignments),
                        json.dumps(
                            safe_assignment_snapshot(assignments),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        actor_user_id,
                        now_iso,
                        new_version,
                    ),
                )
            if baseline_will_invalidate and baseline is not None:
                changed_roles = sorted({row.role_key for row in assignments if row.scope_key not in active_grandfather})
                await db.execute(
                    """
                    INSERT INTO
                        team_llm_v2_migration_baseline_invalidations(
                            team_id, baseline_config_version,
                            invalidated_at, invalidated_by_config_version,
                            reason, changed_role_keys_json
                        ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        team_id,
                        int(baseline["baseline_config_version"]),
                        now_iso,
                        new_version,
                        "critical_config_changed",
                        json.dumps(changed_roles),
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO admin_audit_log(
                        id, actor_user_id, team_id, action,
                        target_kind, target_id, scope,
                        payload_json, ts, ip
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"aud_{uuid4().hex}",
                        actor_user_id,
                        team_id,
                        AuditAction.LLM_MIGRATION_BASELINE_INVALIDATED,
                        "team_llm_config",
                        team_id,
                        "team",
                        json.dumps(
                            {
                                "baseline_config_version": int(baseline["baseline_config_version"]),
                                "new_version": new_version,
                                "changed_role_keys": changed_roles,
                            },
                            separators=(",", ":"),
                        ),
                        now_iso,
                        audit_ip,
                    ),
                )

            encrypted = encrypt_llm_config(config_to_save)
            if not encrypted.startswith("FERNET:"):
                raise TeamLlmApplyError("BOWHEAD_FERNET_KEY is required for V2 Apply")
            cursor = await db.execute(
                """
                UPDATE teams
                SET llm_config_encrypted = ?,
                    llm_config_version = llm_config_version + 1
                WHERE id = ? AND llm_config_version = ?
                """,
                (encrypted, team_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ConfigVersionConflict("team LLM config version changed")

            draft_runtime_keys = set((config_to_save.get("providers") or {}).keys())
            for runtime_key in draft_runtime_keys:
                await db.execute(
                    """
                    UPDATE team_llm_provider_identities
                    SET state = 'active',
                        activated_at = COALESCE(activated_at, ?),
                        reservation_expires_at = NULL
                    WHERE team_id = ? AND runtime_key = ?
                      AND state = 'reserved'
                    """,
                    (now_iso, team_id, str(runtime_key)),
                )
            if draft_runtime_keys:
                placeholders = ",".join("?" for _ in draft_runtime_keys)
                await db.execute(
                    f"""
                    UPDATE team_llm_provider_identities
                    SET state = 'deleted', deleted_at = ?
                    WHERE team_id = ? AND state = 'active'
                      AND runtime_key NOT IN ({placeholders})
                    """,
                    (
                        now_iso,
                        team_id,
                        *sorted(draft_runtime_keys),
                    ),
                )
            else:
                await db.execute(
                    """
                    UPDATE team_llm_provider_identities
                    SET state = 'deleted', deleted_at = ?
                    WHERE team_id = ? AND state = 'active'
                    """,
                    (now_iso, team_id),
                )

            snapshot_id = f"llmas_{uuid4().hex}"
            decision_json = json.dumps(
                {
                    "rows": [
                        {
                            "role_key": row.assignment.role_key,
                            "provider_id": row.assignment.provider_id,
                            "provider_display_name": (row.assignment.provider_display_name),
                            "model_id": row.assignment.model_id,
                            "capability": row.assignment.capability,
                            "config_signature": (row.assignment.config_signature),
                            "fact_state": row.fact.state,
                            "evidence_id": row.fact.evidence_id,
                            "safe_code": row.fact.safe_code,
                            "attempt_state": row.fact.attempt_state,
                            "attempt_safe_code": (row.fact.attempt_safe_code),
                            "actual_model_id": (row.fact.actual_model_id),
                            "status": row.status,
                        }
                        for row in decision.rows
                    ],
                    "blocking_empty": True,
                    "baseline_will_invalidate": (baseline_will_invalidate),
                    "external_rows": [
                        {
                            "subject_kind": row.subject_kind,
                            "configured": row.configured,
                            "status": row.status,
                        }
                        for row in decision.external_rows
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            await db.execute(
                """
                INSERT INTO team_llm_apply_snapshots(
                    id, team_id, config_version,
                    previous_config_version, actor_user_id,
                    draft_semantic_hash, readiness_contract_version,
                    decision_json, applied_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    team_id,
                    new_version,
                    expected_version,
                    actor_user_id,
                    draft_semantic_hash,
                    READINESS_CONTRACT_VERSION,
                    decision_json,
                    now_iso,
                ),
            )
            await db.execute(
                """
                INSERT INTO admin_audit_log(
                    id, actor_user_id, team_id, action,
                    target_kind, target_id, scope,
                    payload_json, ts, ip
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"aud_{uuid4().hex}",
                    actor_user_id,
                    team_id,
                    AuditAction.LLM_CONFIG_APPLY,
                    "team_llm_config",
                    team_id,
                    "team",
                    json.dumps(
                        {
                            "version": new_version,
                            "snapshot_id": snapshot_id,
                            "risk_acceptance_count": len(decision.required_acceptances),
                            "migration_baseline_created": (uses_migration),
                            "baseline_invalidated": (baseline_will_invalidate),
                        },
                        separators=(",", ":"),
                    ),
                    now_iso,
                    audit_ip,
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return V2ApplyResult(
        version=new_version,
        snapshot_id=snapshot_id,
        decision=decision,
    )
