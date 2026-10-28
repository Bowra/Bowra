"""M2 Agent Safety Check orchestration and immutable report persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentPublicationValidation,
    AgentPublicationValidator,
    AgentReleaseCutoverRequiredError,
    AgentRevisionConflictError,
    AgentRevisionForbiddenError,
    AgentSemanticSafetyResult,
    AgentSemanticSafetyScanner,
    requires_admin_review,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.catalog_localization import materialize_confirmed_agent_localizations_conn
from bowhead_core.resource_dependency_projection import (
    edges_from_compilation,
    replace_subject_projection_conn,
)
from bowhead_core.user_store import UserStore


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AgentSafetyCheckReport:
    id: str
    agent_id: str
    subject_kind: str
    subject_ref: str
    content_hash: str
    dependency_fingerprint: str
    compiler_version: str
    semantic_scanner_version: str
    status: str
    deterministic: dict[str, Any]
    semantic: dict[str, Any]
    safe_summary: dict[str, Any]
    working_copy_row_version: int
    created_at: str
    # Derived per request from the current audience and routing, never persisted.
    # ``None`` means this response did not evaluate it -- which is
    # not the same as "no review needed", so callers must not read it as False.
    requires_admin_review: bool | None = None


class PersistedAgentSemanticEvidenceResolver:
    """Resolve only completed, exact-hash semantic evidence inside a publish tx."""

    async def resolve(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        content_hash: str,
        dependency_fingerprint: str,
        compiler_version: str,
    ) -> AgentSemanticSafetyResult | None:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT semantic_scanner_version,semantic_json
            FROM agent_safety_report
            WHERE team_id=? AND agent_id=? AND content_hash=?
              AND dependency_fingerprint=? AND compiler_version=?
            ORDER BY (semantic_scanner_version!='none') DESC,created_at DESC,id DESC
            LIMIT 1
            """,
            (
                team_id,
                agent_id,
                content_hash,
                dependency_fingerprint,
                compiler_version,
            ),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        value = _decode_object(row["semantic_json"], name="semantic evidence")
        status = value.get("status")
        reasons = value.get("reason_codes")
        if status not in {"pass", "needs_review", "blocked", "error"} or not isinstance(
            reasons,
            list,
        ):
            return None
        try:
            return AgentSemanticSafetyResult(
                status=status,
                reason_codes=tuple(sorted({str(item) for item in reasons})),
                scanner_version=str(row["semantic_scanner_version"]),
            )
        except ValueError:
            return None


class AgentSafetyCheckService:
    def __init__(
        self,
        db_path: str,
        *,
        validator: AgentPublicationValidator,
        semantic_scanner: AgentSemanticSafetyScanner,
        clock: _Clock | None = None,
        eval_gate_installed: bool = False,
    ) -> None:
        self.db_path = str(db_path)
        self._validator = validator
        self._semantic_scanner = semantic_scanner
        self._clock = clock or _SystemClock()
        self._eval_gate_installed = eval_gate_installed

    async def run_working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_working_copy_id: str,
        expected_working_copy_version: int,
    ) -> AgentSafetyCheckReport:
        snapshot = await self._load_snapshot(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            expected_working_copy_id=expected_working_copy_id,
            expected_working_copy_version=expected_working_copy_version,
        )
        async with open_shared_ctx(self.db_path) as db:
            baseline = await self._validator.validate(
                db,
                team_id=team_id,
                agent_id=agent_id,
                owner_user_id=actor_user_id,
                routing_name=snapshot["routing_name"],
                definition=snapshot["definition"],
                listing=snapshot["listing"],
                audience=snapshot["audience"],
            )
            existing = await _matching_report(
                db,
                team_id=team_id,
                agent_id=agent_id,
                subject_ref=expected_working_copy_id,
                content_hash=snapshot["content_hash"],
                dependency_fingerprint=baseline.compilation.dependency_fingerprint,
                compiler_version=baseline.scanner_version,
                semantic_scanner_version=(
                    "none"
                    if baseline.deterministic_reason_codes or baseline.compilation.status == "blocked"
                    else self._semantic_scanner.scanner_version
                ),
            )
        if existing is not None:
            async with open_shared_ctx(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                try:
                    current = await _owned_working_copy(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        actor_user_id=actor_user_id,
                    )
                    _require_working_snapshot(
                        current,
                        snapshot=snapshot,
                        expected_working_copy_id=expected_working_copy_id,
                        expected_working_copy_version=expected_working_copy_version,
                    )
                    await _replace_working_projection_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        working_copy_id=expected_working_copy_id,
                        content_hash=snapshot["content_hash"],
                        validation=baseline,
                    )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            return _report_from_row(
                existing,
                working_copy_row_version=expected_working_copy_version,
                requires_admin_review=requires_admin_review(
                    baseline,
                    audience=snapshot["audience"],
                    eval_gate_installed=self._eval_gate_installed,
                ),
            )

        validation = baseline
        if not baseline.deterministic_reason_codes and baseline.compilation.status != "blocked":
            try:
                semantic = await self._semantic_scanner.scan(
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    definition=snapshot["definition"],
                    listing=snapshot["listing"],
                    audience_mode=snapshot["audience"].mode,
                    compilation=baseline.compilation,
                )
            except Exception:
                semantic = AgentSemanticSafetyResult(
                    status="error",
                    reason_codes=("semantic_scanner_unavailable",),
                    scanner_version=self._semantic_scanner.scanner_version,
                )
            validation = _with_semantic(
                baseline,
                semantic=semantic,
                audience=snapshot["audience"],
            )

        timestamp = self._clock.now().astimezone(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _owned_working_copy(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                _require_working_snapshot(
                    current,
                    snapshot=snapshot,
                    expected_working_copy_id=expected_working_copy_id,
                    expected_working_copy_version=expected_working_copy_version,
                )
                rechecked = await self._validator.validate(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    owner_user_id=actor_user_id,
                    routing_name=snapshot["routing_name"],
                    definition=snapshot["definition"],
                    listing=snapshot["listing"],
                    audience=snapshot["audience"],
                )
                if (
                    rechecked.compilation.dependency_fingerprint != baseline.compilation.dependency_fingerprint
                    or rechecked.compilation.capability_fingerprint != baseline.compilation.capability_fingerprint
                    or rechecked.deterministic_reason_codes != baseline.deterministic_reason_codes
                    or rechecked.behavior_fingerprint != baseline.behavior_fingerprint
                    or rechecked.behavior_status != baseline.behavior_status
                    or rechecked.behavior_reason_codes != baseline.behavior_reason_codes
                ):
                    raise AgentRevisionConflictError("agent_safety_check_input_changed")
                report_id = f"asr_{uuid4().hex}"
                inserted = await db.execute(
                    """
                    INSERT OR IGNORE INTO agent_safety_report(
                      id,team_id,agent_id,subject_kind,subject_ref,content_hash,
                      dependency_fingerprint,compiler_version,semantic_scanner_version,status,
                      deterministic_json,semantic_json,restricted_evidence_ref,
                      safe_summary_json,created_by,created_at
                    ) VALUES (?,?,?,'working_copy',?,?,?,?,?,?,?, ?,NULL,?,?,?)
                    """,
                    (
                        report_id,
                        team_id,
                        agent_id,
                        expected_working_copy_id,
                        snapshot["content_hash"],
                        validation.compilation.dependency_fingerprint,
                        validation.scanner_version,
                        validation.semantic_scanner_version,
                        validation.safety_status,
                        _canonical_json(validation.deterministic_dict()),
                        _canonical_json(validation.semantic_dict()),
                        _canonical_json(validation.safe_summary_dict()),
                        actor_user_id,
                        timestamp,
                    ),
                )
                report = await _matching_report(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    subject_ref=expected_working_copy_id,
                    content_hash=snapshot["content_hash"],
                    dependency_fingerprint=validation.compilation.dependency_fingerprint,
                    compiler_version=validation.scanner_version,
                    semantic_scanner_version=validation.semantic_scanner_version,
                )
                if report is None:
                    raise RuntimeError("Agent Safety report insert failed")
                existing_validation = _decode_object(
                    current["validation_json"],
                    name="working copy validation",
                )
                next_validation = {key: value for key, value in existing_validation.items() if key in {"change_kind", "rollback_source_release_id"}}
                next_validation.update(
                    {
                        "behavior": validation.behavior_dict(),
                        "content_hash": str(report["content_hash"]),
                        "dependency_fingerprint": str(
                            report["dependency_fingerprint"],
                        ),
                        "report_id": str(report["id"]),
                        "status": str(report["status"]),
                    },
                )
                next_version = expected_working_copy_version + 1
                updated = await db.execute(
                    """
                    UPDATE team_subagent_working_copy
                    SET validation_json=?,row_version=?,updated_by=?,updated_at=?
                    WHERE id=? AND row_version=?
                    """,
                    (
                        _canonical_json(next_validation),
                        next_version,
                        actor_user_id,
                        timestamp,
                        expected_working_copy_id,
                        expected_working_copy_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentRevisionConflictError(
                        "agent_working_copy_version_conflict",
                    )
                await _replace_working_projection_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    working_copy_id=expected_working_copy_id,
                    content_hash=snapshot["content_hash"],
                    validation=validation,
                )
                await UserStore(self.db_path).write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action=AuditAction.AGENT_SAFETY_CHECK_RUN,
                    target_kind="team_subagent",
                    target_id=agent_id,
                    target_team_id=team_id,
                    scope="team",
                    payload={
                        "report_id": str(report["id"]),
                        "status": str(report["status"]),
                        "created": inserted.rowcount == 1,
                    },
                    conn=db,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _report_from_row(
            report,
            working_copy_row_version=next_version,
            requires_admin_review=requires_admin_review(
                validation,
                audience=snapshot["audience"],
                eval_gate_installed=self._eval_gate_installed,
            ),
        )

    async def record_semantic_evidence(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_working_copy_id: str,
        expected_working_copy_version: int,
    ) -> None:
        """Persist semantic evidence for a working copy without changing its row version.

        Server-side preparation (CI and Coach) freezes a revision without the
        Owner's interactive Safety Check, so the publication validator found no
        evidence and recorded ``semantic_scanner_unavailable`` although the scanner
        never ran. These callers pin the working-copy version, so unlike
        ``run_working_copy`` this neither bumps it nor rewrites its validation.
        """

        snapshot = await self._load_snapshot(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            expected_working_copy_id=expected_working_copy_id,
            expected_working_copy_version=expected_working_copy_version,
        )
        async with open_shared_ctx(self.db_path) as db:
            baseline = await self._validator.validate(
                db,
                team_id=team_id,
                agent_id=agent_id,
                owner_user_id=actor_user_id,
                routing_name=snapshot["routing_name"],
                definition=snapshot["definition"],
                listing=snapshot["listing"],
                audience=snapshot["audience"],
            )
            if baseline.deterministic_reason_codes or baseline.compilation.status == "blocked":
                # Publication blocks on the deterministic result; the scanner adds nothing.
                return
            existing = await _matching_report(
                db,
                team_id=team_id,
                agent_id=agent_id,
                subject_ref=expected_working_copy_id,
                content_hash=snapshot["content_hash"],
                dependency_fingerprint=baseline.compilation.dependency_fingerprint,
                compiler_version=baseline.scanner_version,
                semantic_scanner_version=self._semantic_scanner.scanner_version,
            )
        if existing is not None:
            return
        try:
            semantic = await self._semantic_scanner.scan(
                team_id=team_id,
                actor_user_id=actor_user_id,
                definition=snapshot["definition"],
                listing=snapshot["listing"],
                audience_mode=snapshot["audience"].mode,
                compilation=baseline.compilation,
            )
        except Exception:
            semantic = AgentSemanticSafetyResult(
                status="error",
                reason_codes=("semantic_scanner_unavailable",),
                scanner_version=self._semantic_scanner.scanner_version,
            )
        validation = _with_semantic(baseline, semantic=semantic, audience=snapshot["audience"])
        timestamp = self._clock.now().astimezone(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _owned_working_copy(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                _require_working_snapshot(
                    current,
                    snapshot=snapshot,
                    expected_working_copy_id=expected_working_copy_id,
                    expected_working_copy_version=expected_working_copy_version,
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO agent_safety_report(
                      id,team_id,agent_id,subject_kind,subject_ref,content_hash,
                      dependency_fingerprint,compiler_version,semantic_scanner_version,status,
                      deterministic_json,semantic_json,restricted_evidence_ref,
                      safe_summary_json,created_by,created_at
                    ) VALUES (?,?,?,'working_copy',?,?,?,?,?,?,?, ?,NULL,?,?,?)
                    """,
                    (
                        f"asr_{uuid4().hex}",
                        team_id,
                        agent_id,
                        expected_working_copy_id,
                        snapshot["content_hash"],
                        validation.compilation.dependency_fingerprint,
                        validation.scanner_version,
                        validation.semantic_scanner_version,
                        validation.safety_status,
                        _canonical_json(validation.deterministic_dict()),
                        _canonical_json(validation.semantic_dict()),
                        _canonical_json(validation.safe_summary_dict()),
                        actor_user_id,
                        timestamp,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def get_report(
        self,
        *,
        team_id: str,
        agent_id: str,
        report_id: str,
        actor_user_id: str,
    ) -> AgentSafetyCheckReport:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _owned_agent(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            async with db.execute(
                """
                SELECT * FROM agent_safety_report
                WHERE team_id=? AND agent_id=? AND id=?
                """,
                (team_id, agent_id, report_id),
            ) as cursor:
                report = await cursor.fetchone()
            if report is None:
                raise AgentRevisionForbiddenError("agent_safety_report_not_found")
            async with db.execute(
                """
                SELECT row_version FROM team_subagent_working_copy
                WHERE team_id=? AND subagent_id=? AND id=?
                """,
                (team_id, agent_id, report["subject_ref"]),
            ) as cursor:
                working = await cursor.fetchone()
        return _report_from_row(
            report,
            working_copy_row_version=(int(working[0]) if working is not None else 0),
        )

    async def _load_snapshot(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_working_copy_id: str,
        expected_working_copy_version: int,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await _owned_working_copy(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            raw_listing_json = str(row["listing_json"])
            listing = _decode_object(raw_listing_json, name="working copy listing")
            listing = await materialize_confirmed_agent_localizations_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                working_copy_id=expected_working_copy_id,
                listing=listing,
            )
        if str(row["id"]) != expected_working_copy_id or int(row["row_version"]) != expected_working_copy_version:
            raise AgentRevisionConflictError("agent_working_copy_version_conflict")
        definition_json = str(row["definition_json"])
        listing_json = _canonical_json(listing)
        return {
            "routing_name": str(row["routing_name"]),
            "definition_json": definition_json,
            "listing_json": listing_json,
            "raw_listing_json": raw_listing_json,
            "audience_json": str(row["audience_json"]),
            "definition": _decode_object(definition_json, name="working copy definition"),
            "listing": listing,
            "audience": AgentAudience.from_mapping(
                _decode_object(row["audience_json"], name="working copy audience"),
            ),
            "content_hash": _hash_json(
                {
                    "definition": json.loads(definition_json),
                    "listing": listing,
                },
            ),
        }


async def _owned_agent(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT * FROM team_subagent
        WHERE team_id=? AND id=? AND definition_mode='revisioned'
          AND lifecycle_state='active' AND COALESCE(owner_user_id,created_by)=?
        """,
        (team_id, agent_id, actor_user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        # Same distinction as agent_release_read_store._owned_aggregate.
        async with db.execute(
            """
            SELECT definition_mode FROM team_subagent
            WHERE team_id=? AND id=? AND lifecycle_state='active'
              AND COALESCE(owner_user_id,created_by)=?
            """,
            (team_id, agent_id, actor_user_id),
        ) as cursor:
            legacy = await cursor.fetchone()
        if legacy is not None and str(legacy["definition_mode"]) == "legacy_mutable":
            raise AgentReleaseCutoverRequiredError("agent_release_cutover_required")
        raise AgentRevisionForbiddenError("agent_not_found")
    return row


async def _owned_working_copy(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> aiosqlite.Row:
    await _owned_agent(
        db,
        team_id=team_id,
        agent_id=agent_id,
        actor_user_id=actor_user_id,
    )
    async with db.execute(
        """
        SELECT w.*,a.name AS routing_name
        FROM team_subagent_working_copy w
        JOIN team_subagent a ON a.team_id=w.team_id AND a.id=w.subagent_id
        WHERE w.team_id=? AND w.subagent_id=?
        """,
        (team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentRevisionConflictError("agent_working_copy_missing")
    return row


async def _matching_report(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    subject_ref: str,
    content_hash: str,
    dependency_fingerprint: str,
    compiler_version: str,
    semantic_scanner_version: str,
) -> aiosqlite.Row | None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT * FROM agent_safety_report
        WHERE team_id=? AND agent_id=? AND subject_kind='working_copy'
          AND subject_ref=? AND content_hash=? AND dependency_fingerprint=?
          AND compiler_version=? AND semantic_scanner_version=?
        """,
        (
            team_id,
            agent_id,
            subject_ref,
            content_hash,
            dependency_fingerprint,
            compiler_version,
            semantic_scanner_version,
        ),
    ) as cursor:
        return await cursor.fetchone()


def _with_semantic(
    baseline: AgentPublicationValidation,
    *,
    semantic: AgentSemanticSafetyResult,
    audience: AgentAudience,
) -> AgentPublicationValidation:
    if semantic.status == "blocked":
        status = "blocked"
    elif semantic.status == "pass":
        status = "pass"
    elif audience.mode == "private":
        status = "warning"
    else:
        status = "review_required"
    return AgentPublicationValidation(
        compilation=baseline.compilation,
        safety_status=status,
        deterministic_reason_codes=baseline.deterministic_reason_codes,
        semantic_status=semantic.status,
        semantic_reason_codes=semantic.reason_codes,
        scanner_version=baseline.scanner_version,
        semantic_scanner_version=("none" if semantic.status == "error" else semantic.scanner_version),
        behavior_status=baseline.behavior_status,
        behavior_fingerprint=baseline.behavior_fingerprint,
        behavior_reason_codes=baseline.behavior_reason_codes,
        behavior_primary_requested=baseline.behavior_primary_requested,
    )


def _report_from_row(
    row: Mapping[str, Any],
    *,
    working_copy_row_version: int,
    requires_admin_review: bool | None = None,
) -> AgentSafetyCheckReport:
    return AgentSafetyCheckReport(
        id=str(row["id"]),
        agent_id=str(row["agent_id"]),
        subject_kind=str(row["subject_kind"]),
        subject_ref=str(row["subject_ref"]),
        content_hash=str(row["content_hash"]),
        dependency_fingerprint=str(row["dependency_fingerprint"]),
        compiler_version=str(row["compiler_version"]),
        semantic_scanner_version=str(row["semantic_scanner_version"]),
        status=str(row["status"]),
        deterministic=_decode_object(row["deterministic_json"], name="deterministic safety"),
        semantic=_decode_object(row["semantic_json"], name="semantic safety"),
        safe_summary=_decode_object(row["safe_summary_json"], name="safety summary"),
        working_copy_row_version=working_copy_row_version,
        created_at=str(row["created_at"]),
        requires_admin_review=requires_admin_review,
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent Safety payload is not canonical JSON") from exc


def _hash_json(value: Any) -> str:
    encoded = _canonical_json(value)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _require_working_snapshot(
    current: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    expected_working_copy_id: str,
    expected_working_copy_version: int,
) -> None:
    if (
        str(current["id"]) != expected_working_copy_id
        or int(current["row_version"]) != expected_working_copy_version
        or str(current["definition_json"]) != snapshot["definition_json"]
        or str(current["listing_json"]) != snapshot.get("raw_listing_json", snapshot["listing_json"])
        or str(current["audience_json"]) != snapshot["audience_json"]
    ):
        raise AgentRevisionConflictError("agent_working_copy_version_conflict")


async def _replace_working_projection_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    working_copy_id: str,
    content_hash: str,
    validation: AgentPublicationValidation,
) -> None:
    projection, edges = edges_from_compilation(
        team_id=team_id,
        subject_kind="agent_working_copy",
        subject_ref=working_copy_id,
        agent_id=agent_id,
        subject_fingerprint=content_hash,
        compilation=validation.compilation,
    )
    await replace_subject_projection_conn(
        db,
        projection=projection,
        edges=edges,
    )


def _decode_object(value: Any, *, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentPublicationBlockedError(
            f"agent_{name.replace(' ', '_')}_malformed",
        ) from exc
    if not isinstance(decoded, dict):
        raise AgentPublicationBlockedError(
            f"agent_{name.replace(' ', '_')}_malformed",
        )
    return decoded


__all__ = [
    "AgentSafetyCheckReport",
    "AgentSafetyCheckService",
    "PersistedAgentSemanticEvidenceResolver",
]
