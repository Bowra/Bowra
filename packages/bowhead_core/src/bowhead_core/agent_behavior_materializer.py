"""Materialization and offline backfill for revision-bound Agent behavior.

Descriptors are persisted as immutable revision sidecars.  M3-13E may read a
reviewed sidecar for an independently cut-over behavior, while Lead routing and
Tool authorization keep their existing paths until their own explicit cutover.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_artifact_capability import ArtifactCapabilityRegistry
from bowhead_core.agent_behavior_compiler import AgentBehaviorCompiler
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, canonical_fingerprint
from bowhead_core.agent_capability_catalog import CapabilityCatalog, CapabilityRef, default_catalog
from bowhead_core.agent_capability_composition import (
    CapabilityCompilationContext,
    CapabilityCompositionCompiler,
    CapabilityPlanCompilation,
    ExactToolBinding,
)
from bowhead_core.agent_dependency_compiler import (
    AgentDependencyCompilation,
    AgentDependencyCompiler,
    AgentRiskAssessment,
    ExactSkillRuntimeScope,
)
from bowhead_core.agents.exceptions import ManifestValidationError
from bowhead_core.metrics import inc

logger = logging.getLogger(__name__)

BehaviorSourceKind = Literal["team", "system"]
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXACT_CAPABILITY_PATTERN = re.compile(r"^(cap_[a-z][a-z0-9_]{1,63})@([1-9][0-9]*)$")
_EFFECT_ENVELOPES: dict[str, Literal["read", "compute", "internal_artifact_write", "external_write", "destructive"]] = {
    "read": "read",
    "compute": "compute",
    "artifact_write": "internal_artifact_write",
    "internal_artifact_write": "internal_artifact_write",
    "external_write": "external_write",
    "destructive": "destructive",
    # Unknown evidence must not silently widen the selected implementation.
    "unknown": "read",
}
_AVAILABILITY_TOOL_REQUIREMENTS: dict[str, frozenset[str]] = {
    "image_runtime_contract_ready": frozenset({"image_generate"}),
}


class AgentBehaviorMaterializationError(RuntimeError):
    """An immutable behavior row cannot be safely produced or reconciled."""


@dataclass(frozen=True, slots=True)
class AgentBehaviorBackfillResult:
    scanned_count: int
    missing_count: int
    inserted_count: int
    valid_count: int
    invalid_count: int
    blocked_count: int
    shadow_count: int
    reviewed_count: int
    primary_declared_count: int
    primary_effective_count: int
    next_cursor: str | None


class AgentBehaviorMaterializer:
    """Compile one exact revision and persist its canonical sidecar."""

    def __init__(
        self,
        *,
        catalog: CapabilityCatalog | None = None,
        artifacts: ArtifactCapabilityRegistry | None = None,
    ) -> None:
        resolved_catalog = catalog or default_catalog()
        if resolved_catalog is None:
            raise AgentBehaviorMaterializationError("agent_behavior_catalog_unavailable")
        self.catalog = resolved_catalog
        try:
            self.catalog.validate_reviewed_evidence()
        except ManifestValidationError as exc:
            raise AgentBehaviorMaterializationError("agent_behavior_eval_evidence_invalid") from exc
        artifact_registry = artifacts or ArtifactCapabilityRegistry.bootstrap()
        self._capability_compiler = CapabilityCompositionCompiler(
            catalog=self.catalog,
            artifacts=artifact_registry,
        )
        self._behavior_compiler = AgentBehaviorCompiler(self.catalog)

    def compile_team_revision(
        self,
        *,
        revision_id: str,
        definition: dict[str, Any],
        source_definition_hash: str,
        dependency: AgentDependencyCompilation,
        effective_model_role: str = "analyst",
    ) -> AgentBehaviorDescriptor:
        return self._compile(
            revision_id=revision_id,
            definition=definition,
            source_definition_hash=source_definition_hash,
            dependency=dependency,
            effective_model_role=effective_model_role,
        )

    def compile_system_revision(
        self,
        *,
        revision_id: str,
        definition: dict[str, Any],
        source_definition_hash: str,
    ) -> AgentBehaviorDescriptor:
        catalog_fingerprint = definition.get("behavior_catalog_fingerprint")
        if catalog_fingerprint == self.catalog.fingerprint:
            dependency, available_checks = system_dependency_compilation(
                definition=definition,
                source_definition_hash=source_definition_hash,
                catalog=self.catalog,
            )
        else:
            reasons = [
                ("behavior_catalog_identity_missing" if catalog_fingerprint is None else "behavior_catalog_identity_mismatch"),
                "legacy_system_dependency_unverified",
            ]
            if definition.get("primary_capability") and not definition.get("primary_capability_ref"):
                reasons.append("legacy_primary_capability_unpinned")
            dependency = legacy_dependency_compilation(
                source_definition_hash=source_definition_hash,
                reason_codes=tuple(reasons),
            )
            available_checks = ()
        model_role = definition.get("llm_agent_type")
        if not isinstance(model_role, str) or re.fullmatch(r"^[a-z][a-z0-9_.-]{1,63}$", model_role) is None:
            model_role = "analyst"
        return self._compile(
            revision_id=revision_id,
            definition=definition,
            source_definition_hash=source_definition_hash,
            dependency=dependency,
            effective_model_role=model_role,
            available_checks=available_checks,
        )

    def compile_legacy_team_revision(
        self,
        *,
        revision_id: str,
        definition: dict[str, Any],
        source_definition_hash: str,
        dependency_fingerprint: str,
        capability_fingerprint: str,
        risk_json: str | None = None,
    ) -> AgentBehaviorDescriptor:
        reasons = ["historical_dependency_evidence_unavailable"]
        if definition.get("primary_capability") and not definition.get("primary_capability_ref"):
            reasons.append("legacy_primary_capability_unpinned")
        dependency = legacy_dependency_compilation(
            source_definition_hash=source_definition_hash,
            dependency_fingerprint=dependency_fingerprint,
            capability_fingerprint=capability_fingerprint,
            reason_codes=tuple(reasons),
            risk_json=risk_json,
        )
        return self._compile(
            revision_id=revision_id,
            definition=definition,
            source_definition_hash=source_definition_hash,
            dependency=dependency,
            effective_model_role="analyst",
        )

    def _compile(
        self,
        *,
        revision_id: str,
        definition: dict[str, Any],
        source_definition_hash: str,
        dependency: AgentDependencyCompilation,
        effective_model_role: str,
        available_checks: tuple[str, ...] = (),
    ) -> AgentBehaviorDescriptor:
        primary_ref = _primary_capability_ref(definition.get("primary_capability_ref"))
        context = _context_from_dependency(
            dependency,
            available_checks=available_checks or _availability_checks_from_dependency(dependency),
        )
        compositions: tuple[CapabilityPlanCompilation, ...] = ()
        embedded = definition.get("capability_composition")
        try:
            if embedded is not None:
                compositions = (
                    self._capability_compiler.compile_embedded(
                        embedded,
                        revision=revision_id,
                        context=context,
                    ),
                )
            elif primary_ref is not None:
                match = _EXACT_CAPABILITY_PATTERN.fullmatch(primary_ref)
                assert match is not None
                capability = CapabilityRef(id=match.group(1), version=int(match.group(2)))
                try:
                    self.catalog.get_definition(capability.id, capability.version)
                except KeyError:
                    pass
                else:
                    compositions = (
                        self._capability_compiler.compile_single(
                            capability,
                            revision=revision_id,
                            effect_envelope=_effect_envelope(dependency.risk.effect),
                            context=context,
                        ),
                    )
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentBehaviorMaterializationError("agent_behavior_composition_invalid") from exc
        return self._behavior_compiler.compile(
            source_definition_hash=source_definition_hash,
            dependency=dependency,
            compositions=compositions,
            primary_capability_ref=primary_ref,
            effective_model_role=effective_model_role,
        )

    async def persist_team_revision_conn(
        self,
        db: aiosqlite.Connection,
        *,
        revision_id: str,
        team_id: str,
        subagent_id: str,
        descriptor: AgentBehaviorDescriptor,
        created_at: str,
    ) -> None:
        await _persist_descriptor_conn(
            db,
            source_kind="team",
            revision_id=revision_id,
            team_id=team_id,
            agent_id=subagent_id,
            descriptor=descriptor,
            created_at=created_at,
        )

    @staticmethod
    async def persist_system_revision_conn(
        db: aiosqlite.Connection,
        *,
        revision_id: str,
        team_id: str,
        agent_id: str,
        descriptor: AgentBehaviorDescriptor,
        created_at: str,
    ) -> None:
        """Persist prepared behavior without initializing a catalog or compiler."""
        await _persist_descriptor_conn(
            db,
            source_kind="system",
            revision_id=revision_id,
            team_id=team_id,
            agent_id=agent_id,
            descriptor=descriptor,
            created_at=created_at,
        )


@lru_cache(maxsize=1)
def default_agent_behavior_materializer() -> AgentBehaviorMaterializer:
    return AgentBehaviorMaterializer()


class AgentBehaviorBackfillService:
    """Paged dry-run/apply/verify workflow for revisions predating M3-13B."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        materializer: AgentBehaviorMaterializer | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._materializer = materializer or default_agent_behavior_materializer()

    async def run_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        apply: bool = False,
    ) -> AgentBehaviorBackfillResult:
        if not 1 <= limit <= 500:
            raise ValueError("backfill limit must be between 1 and 500")
        cursor_kind, cursor_revision = _decode_cursor(cursor)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if apply:
                await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await _revision_page_conn(
                    db,
                    cursor_kind=cursor_kind,
                    cursor_revision=cursor_revision,
                    limit=limit + 1,
                )
                page = rows[:limit]
                missing = inserted = valid = invalid = 0
                states = {"blocked": 0, "shadow": 0, "reviewed": 0}
                primary_declared = primary_effective = 0
                for row in page:
                    if row["descriptor_json"] is not None:
                        descriptor = _stored_descriptor(row)
                        if descriptor is None:
                            invalid += 1
                            continue
                        valid += 1
                    else:
                        missing += 1
                        descriptor = self._compile_missing(row)
                        if apply:
                            await self._persist_missing(db, row=row, descriptor=descriptor)
                            inserted += 1
                    states[descriptor.verification_state] += 1
                    if _primary_capability_ref(_decode_definition(row["definition_json"]).get("primary_capability_ref")) is not None:
                        primary_declared += 1
                    if descriptor.primary_capability_ref is not None:
                        primary_effective += 1
                if apply:
                    await db.commit()
            except BaseException:
                if apply:
                    await db.rollback()
                raise
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = f"{last['source_kind']}:{last['revision_id']}"
        result = AgentBehaviorBackfillResult(
            scanned_count=len(page),
            missing_count=missing,
            inserted_count=inserted,
            valid_count=valid,
            invalid_count=invalid,
            blocked_count=states["blocked"],
            shadow_count=states["shadow"],
            reviewed_count=states["reviewed"],
            primary_declared_count=primary_declared,
            primary_effective_count=primary_effective,
            next_cursor=next_cursor,
        )
        logger.info(
            "metric=agent_behavior_shadow_diff mode=%s scanned=%d blocked=%d shadow=%d reviewed=%d invalid=%d",
            "apply" if apply else "preview",
            result.scanned_count,
            result.blocked_count,
            result.shadow_count,
            result.reviewed_count,
            result.invalid_count,
        )
        for state, count in states.items():
            if count:
                inc(
                    "agent_behavior_backfill_scan_total",
                    labels={"mode": "apply" if apply else "preview", "result": state},
                    value=float(count),
                )
        if result.invalid_count:
            inc(
                "agent_behavior_backfill_scan_total",
                labels={"mode": "apply" if apply else "preview", "result": "invalid"},
                value=float(result.invalid_count),
            )
        return result

    async def verify(self) -> dict[str, int]:
        cursor: str | None = None
        totals = {
            "scanned": 0,
            "missing": 0,
            "valid": 0,
            "invalid": 0,
            "blocked": 0,
            "shadow": 0,
            "reviewed": 0,
        }
        while True:
            page = await self.run_page(cursor=cursor, limit=500, apply=False)
            totals["scanned"] += page.scanned_count
            totals["missing"] += page.missing_count
            totals["valid"] += page.valid_count
            totals["invalid"] += page.invalid_count
            totals["blocked"] += page.blocked_count
            totals["shadow"] += page.shadow_count
            totals["reviewed"] += page.reviewed_count
            cursor = page.next_cursor
            if cursor is None:
                return totals

    def _compile_missing(self, row: aiosqlite.Row) -> AgentBehaviorDescriptor:
        definition = _decode_definition(row["definition_json"])
        if row["source_kind"] == "team":
            return self._materializer.compile_legacy_team_revision(
                revision_id=str(row["revision_id"]),
                definition=definition,
                source_definition_hash=str(row["definition_hash"]),
                dependency_fingerprint=str(row["dependency_fingerprint"]),
                capability_fingerprint=str(row["legacy_capability_fingerprint"]),
                risk_json=(str(row["risk_json"]) if row["risk_json"] is not None else None),
            )
        return self._materializer.compile_system_revision(
            revision_id=str(row["revision_id"]),
            definition=definition,
            source_definition_hash=str(row["definition_hash"]),
        )

    async def _persist_missing(
        self,
        db: aiosqlite.Connection,
        *,
        row: aiosqlite.Row,
        descriptor: AgentBehaviorDescriptor,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        if row["source_kind"] == "team":
            await self._materializer.persist_team_revision_conn(
                db,
                revision_id=str(row["revision_id"]),
                team_id=str(row["team_id"]),
                subagent_id=str(row["agent_id"]),
                descriptor=descriptor,
                created_at=timestamp,
            )
        else:
            await self._materializer.persist_system_revision_conn(
                db,
                revision_id=str(row["revision_id"]),
                team_id=str(row["team_id"]),
                agent_id=str(row["agent_id"]),
                descriptor=descriptor,
                created_at=timestamp,
            )


def legacy_dependency_compilation(
    *,
    source_definition_hash: str,
    reason_codes: tuple[str, ...],
    dependency_fingerprint: str | None = None,
    capability_fingerprint: str | None = None,
    risk_json: str | None = None,
) -> AgentDependencyCompilation:
    """Produce explicit blocked evidence for a revision lacking exact inputs."""
    dependency_hash = dependency_fingerprint if dependency_fingerprint and _SHA256_PATTERN.fullmatch(dependency_fingerprint) else None
    capability_hash = capability_fingerprint if capability_fingerprint and _SHA256_PATTERN.fullmatch(capability_fingerprint) else None
    dependency_hash = dependency_hash or canonical_fingerprint(
        {"kind": "legacy_dependency", "source_definition_hash": source_definition_hash},
    )
    capability_hash = capability_hash or canonical_fingerprint(
        {"kind": "legacy_capability", "source_definition_hash": source_definition_hash},
    )
    risk = _legacy_risk(risk_json)
    return AgentDependencyCompilation(
        status="review_required",
        reason_codes=tuple(sorted(set(reason_codes))),
        dependency_fingerprint=dependency_hash,
        capability_fingerprint=capability_hash,
        tools=(),
        skills=(),
        skill_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=()),
        risk=risk,
    )


def system_dependency_compilation(
    *,
    definition: dict[str, Any],
    source_definition_hash: str,
    catalog: CapabilityCatalog,
) -> tuple[AgentDependencyCompilation, tuple[str, ...]]:
    """Compile the exact native dependencies needed by a system primary role.

    System manifests are code-owned and can contain broad legacy Tool/Skill
    access.  The behavior sidecar intentionally selects only dependencies of
    the explicitly pinned primary Capability's reviewed Implementations; the
    source definition hash still anchors the complete profile.  This avoids
    treating unrelated legacy Skill access as evidence for the selected role.
    """
    primary_ref = _primary_capability_ref(definition.get("primary_capability_ref"))
    raw_tools = definition.get("tool_whitelist")
    agent_name = definition.get("name")
    if (
        not isinstance(agent_name, str)
        or not agent_name
        or not isinstance(raw_tools, list)
        or not all(isinstance(item, str) and item for item in raw_tools)
    ):
        return (
            legacy_dependency_compilation(
                source_definition_hash=source_definition_hash,
                reason_codes=("system_tool_contract_malformed",),
            ),
            (),
        )
    declared_tools = frozenset(raw_tools)
    implementations = ()
    if primary_ref is not None:
        match = _EXACT_CAPABILITY_PATTERN.fullmatch(primary_ref)
        assert match is not None
        try:
            capability = catalog.get_definition(match.group(1), int(match.group(2)))
        except KeyError:
            capability = None
        if capability is not None:
            implementations = tuple(
                item for item in catalog.implementations_for(capability) if item.lifecycle == "active" and item.review_state == "reviewed"
            )

    dependency_tool_refs = {
        tool_ref for implementation in implementations for tool_ref in (*implementation.required_tools, *implementation.optional_tools)
    } & declared_tools
    dependency = AgentDependencyCompiler.compile_system_capability_dependencies(
        tuple(sorted(dependency_tool_refs)),
        agent_id=f"system:{agent_name}",
        source_definition_hash=source_definition_hash,
    )

    available_checks = tuple(
        sorted(
            {
                check
                for implementation in implementations
                for check in implementation.availability_checks
                if (required := _AVAILABILITY_TOOL_REQUIREMENTS.get(check)) is not None
                and required.issubset(declared_tools)
                and required.issubset(
                    {item.stable_ref for item in dependency.tools if item.available},
                )
            },
        ),
    )
    return dependency, available_checks


def _context_from_dependency(
    dependency: AgentDependencyCompilation,
    *,
    available_checks: tuple[str, ...] = (),
) -> CapabilityCompilationContext:
    return CapabilityCompilationContext(
        tools=tuple(
            sorted(
                (ExactToolBinding(ref=item.stable_ref, revision=item.revision) for item in dependency.tools if item.available),
                key=lambda item: item.ref,
            ),
        ),
        skills=tuple(sorted((item.grant for item in dependency.skills if item.available), key=lambda item: item.identity)),
        # M3-13B has no exact typed Connection/check/output-grant publication
        # evidence yet.  Empty values under-grant and surface the missing seam.
        connections=(),
        available_checks=available_checks,
        available_output_grants=(),
    )


def _availability_checks_from_dependency(
    dependency: AgentDependencyCompilation,
) -> tuple[str, ...]:
    available_tools = {item.stable_ref for item in dependency.tools if item.available}
    return tuple(
        sorted(check for check, required in _AVAILABILITY_TOOL_REQUIREMENTS.items() if required.issubset(available_tools)),
    )


def _primary_capability_ref(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            return CapabilityRef.model_validate(value).exact_ref
        except ValueError as exc:
            raise AgentBehaviorMaterializationError("agent_behavior_primary_capability_malformed") from exc
    if not isinstance(value, str) or _EXACT_CAPABILITY_PATTERN.fullmatch(value) is None:
        raise AgentBehaviorMaterializationError("agent_behavior_primary_capability_malformed")
    return value


def _effect_envelope(value: str) -> Literal["read", "compute", "internal_artifact_write", "external_write", "destructive"]:
    return _EFFECT_ENVELOPES.get(value, "read")


def _legacy_risk(value: str | None) -> AgentRiskAssessment:
    dimensions: dict[str, Any] = {}
    level = "unknown"
    if value:
        try:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                raw_dimensions = decoded.get("dimensions")
                if isinstance(raw_dimensions, dict):
                    dimensions = raw_dimensions
                raw_level = decoded.get("level")
                if raw_level in {"low", "medium", "high", "unknown"}:
                    level = raw_level
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return AgentRiskAssessment(
        level=level,  # type: ignore[arg-type]
        data=str(dimensions.get("data") or "unknown"),
        effect=str(dimensions.get("effect") or "unknown"),
        automation=str(dimensions.get("automation") or "unknown"),
        scope=str(dimensions.get("scope") or "unknown"),
        reversibility=str(dimensions.get("reversibility") or "unknown"),
        reasons=("legacy_dependency_evidence_unavailable",),
        unknown_dependencies=("legacy_revision",),
        approval_required=True,
        runtime_confirmation_required=True,
    )


async def _persist_descriptor_conn(
    db: aiosqlite.Connection,
    *,
    source_kind: BehaviorSourceKind,
    revision_id: str,
    team_id: str,
    agent_id: str,
    descriptor: AgentBehaviorDescriptor,
    created_at: str,
) -> None:
    table = "team_subagent_revision_behavior" if source_kind == "team" else "system_agent_revision_behavior"
    agent_column = "subagent_id" if source_kind == "team" else "agent_id"
    descriptor_json = descriptor.canonical_json()
    await db.execute(
        f"""
        INSERT INTO {table}(
          revision_id,team_id,{agent_column},source_definition_hash,
          descriptor_json,behavior_fingerprint,compiler_version,created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(revision_id) DO NOTHING
        """,  # noqa: S608 -- table and column are selected from a closed server enum.
        (
            revision_id,
            team_id,
            agent_id,
            descriptor.source_definition_hash,
            descriptor_json,
            descriptor.behavior_fingerprint,
            descriptor.compiler_version,
            created_at,
        ),
    )
    async with db.execute(
        f"SELECT * FROM {table} WHERE revision_id=?",  # noqa: S608 -- closed server enum.
        (revision_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentBehaviorMaterializationError("agent_behavior_persist_failed")
    actual = (
        str(row["team_id"]),
        str(row[agent_column]),
        str(row["source_definition_hash"]),
        str(row["descriptor_json"]),
        str(row["behavior_fingerprint"]),
        str(row["compiler_version"]),
    )
    expected = (
        team_id,
        agent_id,
        descriptor.source_definition_hash,
        descriptor_json,
        descriptor.behavior_fingerprint,
        descriptor.compiler_version,
    )
    if actual != expected:
        raise AgentBehaviorMaterializationError("agent_behavior_immutable_conflict")


async def _revision_page_conn(
    db: aiosqlite.Connection,
    *,
    cursor_kind: str,
    cursor_revision: str,
    limit: int,
) -> list[aiosqlite.Row]:
    async with db.execute(
        """
        WITH revisions AS (
          SELECT 'system' AS source_kind,revision.id AS revision_id,
                 revision.team_id,revision.agent_id,
                 revision.definition_json,revision.definition_hash,
                 NULL AS dependency_fingerprint,NULL AS legacy_capability_fingerprint,
                 NULL AS risk_json,behavior.descriptor_json,
                 behavior.source_definition_hash AS stored_source_definition_hash,
                 behavior.behavior_fingerprint AS stored_behavior_fingerprint,
                 behavior.compiler_version AS stored_compiler_version
          FROM system_agent_effective_revision revision
          LEFT JOIN system_agent_revision_behavior behavior ON behavior.revision_id=revision.id
          UNION ALL
          SELECT 'team',revision.id,revision.team_id,revision.subagent_id,
                 revision.definition_json,revision.definition_hash,
                 revision.dependency_fingerprint,revision.capability_fingerprint,
                 revision.risk_json,behavior.descriptor_json,
                 behavior.source_definition_hash,behavior.behavior_fingerprint,
                 behavior.compiler_version
          FROM team_subagent_revision revision
          LEFT JOIN team_subagent_revision_behavior behavior ON behavior.revision_id=revision.id
        )
        SELECT * FROM revisions
        WHERE source_kind>? OR (source_kind=? AND revision_id>?)
        ORDER BY source_kind,revision_id
        LIMIT ?
        """,
        (cursor_kind, cursor_kind, cursor_revision, limit),
    ) as cursor:
        return await cursor.fetchall()


def _stored_descriptor(row: aiosqlite.Row) -> AgentBehaviorDescriptor | None:
    try:
        raw = str(row["descriptor_json"])
        descriptor = AgentBehaviorDescriptor.model_validate_json(raw)
    except (TypeError, ValueError):
        return None
    if (
        descriptor.canonical_json() != raw
        or descriptor.source_definition_hash != str(row["definition_hash"])
        or descriptor.source_definition_hash != str(row["stored_source_definition_hash"])
        or descriptor.behavior_fingerprint != str(row["stored_behavior_fingerprint"])
        or descriptor.compiler_version != str(row["stored_compiler_version"])
    ):
        return None
    return descriptor


def _decode_definition(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentBehaviorMaterializationError("agent_behavior_definition_malformed") from exc
    if not isinstance(decoded, dict):
        raise AgentBehaviorMaterializationError("agent_behavior_definition_malformed")
    return decoded


def _decode_cursor(value: str | None) -> tuple[str, str]:
    if value is None:
        return "", ""
    try:
        source_kind, revision_id = value.split(":", 1)
    except ValueError as exc:
        raise ValueError("invalid behavior backfill cursor") from exc
    if source_kind not in {"system", "team"} or not revision_id.startswith("arv_"):
        raise ValueError("invalid behavior backfill cursor")
    return source_kind, revision_id
