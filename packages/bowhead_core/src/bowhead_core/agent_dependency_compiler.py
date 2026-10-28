"""Deterministic Agent dependency, risk, and precise Skill-scope compiler.

This module is the M2 publication truth for dependency identity.  It consumes
the existing actor-scoped Tool catalog and exact Skill records; it does not
create another executable registry and it never grants a Tool or Skill by
itself.  M2 release commands persist the hashes/result, while M3 runtime uses
the emitted exact Skill scope as one input to its authorization intersection.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from bowhead_skill.loader import SkillSpec, load_skill
from bowhead_skill.runtime_scope import PRECISE_SKILL_SCOPE_VERSION, ExactSkillRuntimeScope
from bowhead_skill.script_langs import all_entry_extensions

from bowhead_core.agent_runtime_extensions import (
    AgentRuntimeExtensions,
    SkillExtensionSelector,
    extension_policy_summary,
    read_agent_runtime_extensions,
)
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.subagent_authoring_catalog import (
    CATALOG_SCHEMA_VERSION,
    COMPILER_DERIVED_SKILL_TOOLS,
    RISK_REGISTRY_VERSION,
    AuthoringTool,
    SubagentAuthoringCatalogSnapshot,
    native_authoring_tools,
)
from bowhead_skill import SkillCatalog
from bowhead_skill.catalog import builtin_catalog_dir

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bowhead_core.chat_references import SkillResourceRef


AGENT_DEPENDENCY_COMPILER_VERSION = "agent-dependency-v1"
AGENT_RISK_COMPILER_VERSION = "agent-risk-v1"

RiskClass = Literal[
    "read",
    "compute",
    "artifact_write",
    "external_write",
    "destructive",
    "unknown",
]
CompileStatus = Literal["ready", "review_required", "blocked"]
SkillKind = Literal["builtin", "team", "user"]
SkillPermission = Literal["read", "invoke"]
SkillDistribution = Literal["reference", "embedded_agent_scope"]

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_RISK_ORDER: dict[str, int] = {
    "read": 0,
    "compute": 1,
    "artifact_write": 2,
    "external_write": 3,
    "destructive": 4,
    "unknown": 5,
}
_AUDIENCE_ORDER = {"private": 0, "custom": 1, "teamwide": 2}
_AUTOMATION_ORDER = {"interactive": 0, "scheduled": 1, "background": 2}
_ARTIFACT_BUILTIN_SKILLS = frozenset(
    {
        "canvas-design",
        "canvas-docx",
        "canvas-pdf",
        "canvas-pptx",
        "canvas-xlsx",
        "docx",
        "frontend-design",
        "pdf",
        "pptx",
        "theme-factory",
    },
)
_MAX_TOOL_REFS = 128
_MAX_SKILL_GRANTS = 64
_MAX_RISK_REPORT_BYTES = 131_072


class PersonalSkillStore(Protocol):
    async def get_by_id(self, *, user_id: str, skill_id: str) -> Any | None: ...


class TeamSkillStore(Protocol):
    async def get_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        only_enabled: bool = False,
    ) -> Any | None: ...

    async def get_team_skill_by_name(
        self,
        team_id: str,
        name: str,
        *,
        only_enabled: bool = True,
        only_approved: bool = True,
    ) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class AgentDependencyContext:
    """Server-derived publication context; none of these values comes from LLM copy."""

    agent_id: str
    team_id: str
    owner_user_id: str
    audience_mode: Literal["private", "custom", "teamwide"]
    execution_mode: Literal["interactive", "scheduled", "background"] = "interactive"
    # Exact coverage assertions produced by the Audience/resource-grant service.
    # A value is ``<kind>:<id>@sha256:<digest>``.  The compiler never infers
    # custom-audience coverage from a mutable display name.
    reference_coverage: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.agent_id or not self.team_id or not self.owner_user_id:
            raise ValueError("agent_id, team_id, and owner_user_id are required")
        if self.audience_mode not in _AUDIENCE_ORDER:
            raise ValueError("unsupported Agent audience mode")
        if self.execution_mode not in _AUTOMATION_ORDER:
            raise ValueError("unsupported Agent execution mode")
        if len(self.reference_coverage) > 256 or any(
            not isinstance(value, str) or not value or len(value) > 300 for value in self.reference_coverage
        ):
            raise ValueError("reference coverage is invalid or too large")


@dataclass(frozen=True, slots=True)
class SkillReadContext:
    """Resource eligibility without inventing an Agent or execution identity."""

    team_id: str
    owner_user_id: str
    audience_mode: Literal["private", "custom", "teamwide"]
    execution_mode: Literal["interactive", "scheduled", "background"] = "interactive"
    reference_coverage: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.team_id or not self.owner_user_id or self.audience_mode not in _AUDIENCE_ORDER:
            raise ValueError("invalid Skill read context")


@dataclass(frozen=True, slots=True)
class ExactSkillGrant:
    kind: SkillKind
    id: str
    name: str
    permissions: tuple[SkillPermission, ...]
    revision: str
    distribution: SkillDistribution

    def __post_init__(self) -> None:
        if self.kind not in {"builtin", "team", "user"}:
            raise ValueError("unsupported Skill kind")
        if not _SAFE_TOKEN_RE.fullmatch(self.id) or not _SAFE_TOKEN_RE.fullmatch(self.name):
            raise ValueError("Skill identity is invalid")
        if not _HASH_RE.fullmatch(self.revision):
            raise ValueError("Skill revision is invalid")
        if self.distribution not in {"reference", "embedded_agent_scope"}:
            raise ValueError("unsupported Skill distribution")
        if (
            not self.permissions
            or len(self.permissions) > 2
            or tuple(sorted(set(self.permissions))) != self.permissions
            or any(permission not in {"read", "invoke"} for permission in self.permissions)
        ):
            raise ValueError("Skill permissions are invalid")

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def coverage_key(self) -> str:
        return f"{self.identity}@{self.revision}"

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "distribution": self.distribution,
            "permissions": list(self.permissions),
            "revision": self.revision,
            "skill_ref": {"id": self.id, "kind": self.kind, "name": self.name},
        }


@dataclass(frozen=True, slots=True)
class ResolvedSkillDependency:
    grant: ExactSkillGrant
    kind: str
    has_scripts: bool
    risk: RiskClass
    risk_reasons: tuple[str, ...]
    risk_evidence_digest: str | None
    review_required: bool
    available: bool
    reason_codes: tuple[str, ...]

    def fingerprint_dict(self) -> dict[str, Any]:
        # Skills follow their latest approved revision (agent-skill-gates D2): a
        # Skill update must not make a prepared candidate stale, so the exact
        # revision stays out of the dependency fingerprint. Identity,
        # permissions and risk evidence stay in it.
        grant = self.grant.to_canonical_dict()
        grant.pop("revision")
        return {
            **grant,
            "has_scripts": self.has_scripts,
            "kind": self.kind,
            "risk": self.risk,
            "risk_evidence_digest": self.risk_evidence_digest,
            "risk_reasons": list(self.risk_reasons),
        }

    def record_dict(self) -> dict[str, Any]:
        """The fingerprint fields plus the exact revision this compilation resolved."""
        return {**self.fingerprint_dict(), "revision": self.grant.revision}


@dataclass(frozen=True, slots=True)
class ResolvedToolDependency:
    stable_ref: str
    revision: str
    source_kind: str
    risk: RiskClass
    risk_source: str
    approval_required: bool
    capability_classes: tuple[str, ...]
    connection_ref: str | None
    operation_id: str | None
    method: str | None
    available: bool
    reason_codes: tuple[str, ...]
    source_id: str | None = None

    def fingerprint_dict(self) -> dict[str, Any]:
        return {
            "approval_required": self.approval_required,
            "capability_classes": list(self.capability_classes),
            "connection_ref": self.connection_ref,
            "method": self.method,
            "operation_id": self.operation_id,
            "revision": self.revision,
            "risk": self.risk,
            "risk_source": self.risk_source,
            "source_kind": self.source_kind,
            "stable_ref": self.stable_ref,
        }


@dataclass(frozen=True, slots=True)
class AgentRiskAssessment:
    level: Literal["low", "medium", "high", "unknown"]
    data: str
    effect: str
    automation: str
    scope: str
    reversibility: str
    reasons: tuple[str, ...]
    unknown_dependencies: tuple[str, ...]
    approval_required: bool
    runtime_confirmation_required: bool
    schema_version: int = 1
    compiler_version: str = AGENT_RISK_COMPILER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_required": self.approval_required,
            "compiler_version": self.compiler_version,
            "dimensions": {
                "automation": self.automation,
                "data": self.data,
                "effect": self.effect,
                "reversibility": self.reversibility,
                "scope": self.scope,
            },
            "level": self.level,
            "reasons": list(self.reasons),
            "runtime_confirmation_required": self.runtime_confirmation_required,
            "schema_version": self.schema_version,
            "unknown_dependencies": list(self.unknown_dependencies),
        }


@dataclass(frozen=True, slots=True)
class AgentDependencyCompilation:
    status: CompileStatus
    reason_codes: tuple[str, ...]
    dependency_fingerprint: str
    capability_fingerprint: str
    tools: tuple[ResolvedToolDependency, ...]
    skills: tuple[ResolvedSkillDependency, ...]
    skill_scope: ExactSkillRuntimeScope
    risk: AgentRiskAssessment
    schema_version: int = 1
    compiler_version: str = AGENT_DEPENDENCY_COMPILER_VERSION
    runtime_extension_policy: AgentRuntimeExtensions | None = field(default=None, repr=False)
    # One entry per Skill, Tool or definition problem that blocks publication:
    # {"kind": "skill" | "tool" | "definition", "name", "identity", "reason_codes"}.
    blocked_items: tuple[dict[str, Any], ...] = field(default=(), compare=False)

    @property
    def publishable(self) -> bool:
        return self.status != "blocked"

    def rebased_skill_grants(self, definition: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        """The definition's own ``skill_grants`` with each revision rebased to this compilation.

        Returns None when nothing changed. Only grants the definition itself
        declares are rewritten; Plugin-expanded grants are resolved again on
        every expansion and are never written back.
        """
        raw = definition.get("skill_grants")
        if not isinstance(raw, list):
            return None
        resolved = {item.grant.identity: item.grant for item in self.skills}
        changed = False
        rebased: list[dict[str, Any]] = []
        for item in raw:
            grant = _parse_skill_grant(item)
            current = resolved.get(grant.identity) if grant is not None else None
            if grant is None or current is None or current.revision == grant.revision or current.permissions != grant.permissions:
                rebased.append(item)
                continue
            rebased.append({**item, "revision": current.revision})
            changed = True
        return rebased if changed else None

    def dependency_json(self) -> dict[str, Any]:
        value = {
            "compiler_version": self.compiler_version,
            "fingerprint": self.dependency_fingerprint,
            "reason_codes": list(self.reason_codes),
            "schema_version": self.schema_version,
            "skill_scope": self.skill_scope.to_dict(),
            "skills": [item.record_dict() for item in self.skills],
            "status": self.status,
            "tools": [item.fingerprint_dict() for item in self.tools],
        }
        if self.runtime_extension_policy is not None:
            value["runtime_extensions"] = {
                "policy_digest": self.runtime_extension_policy.digest,
                "summary": extension_policy_summary({"runtime_extensions": self.runtime_extension_policy.canonical_value()}),
            }
        return value


@dataclass(frozen=True, slots=True)
class AgentDependencyDiff:
    added_tools: tuple[str, ...]
    removed_tools: tuple[str, ...]
    changed_tools: tuple[str, ...]
    added_skills: tuple[str, ...]
    removed_skills: tuple[str, ...]
    changed_skills: tuple[str, ...]
    audience_expanded: bool
    automation_expanded: bool
    risk_increased: bool
    high_risk_change: bool
    review_required: bool


@dataclass(frozen=True, slots=True)
class _ParsedDefinition:
    tool_refs: tuple[str, ...]
    skill_grants: tuple[ExactSkillGrant, ...]
    model: str | None
    output_contract_hash: str
    reason_codes: tuple[str, ...]
    malformed_digest: str | None


class AgentDependencyCompiler:
    """Compile exact dependencies without changing executable registries or grants."""

    def __init__(
        self,
        *,
        builtin_skills: SkillCatalog | None = None,
        personal_skill_store: PersonalSkillStore | None = None,
        team_skill_store: TeamSkillStore | None = None,
    ) -> None:
        self._builtin_skills = builtin_skills or SkillCatalog(roots=[builtin_catalog_dir()])
        self._personal_skill_store = personal_skill_store
        self._team_skill_store = team_skill_store

    @staticmethod
    def compile_system_capability_dependencies(
        tool_refs: Sequence[str],
        *,
        agent_id: str,
        source_definition_hash: str,
    ) -> AgentDependencyCompilation:
        """Compile a code-owned system Capability's declared native Tool slice.

        The caller selects refs from an exact reviewed Implementation; this
        method remains the single Tool identity/risk compiler.  It does not
        inspect an Agent name or infer which Capability the Agent intended.
        """
        if (
            not agent_id
            or _HASH_RE.fullmatch(source_definition_hash) is None
            or len(tool_refs) > _MAX_TOOL_REFS
            or len(set(tool_refs)) != len(tool_refs)
            or any(not isinstance(ref, str) or _SAFE_TOKEN_RE.fullmatch(ref) is None for ref in tool_refs)
        ):
            raise ValueError("system Agent Capability dependencies are invalid")
        native_by_ref = {tool.stable_ref: tool for tool in native_authoring_tools()}
        tool_dependencies = tuple(_resolve_tool(tool_ref, native_by_ref) for tool_ref in sorted(tool_refs))
        unknown_dependencies = {item.stable_ref for item in tool_dependencies if not item.available or item.risk == "unknown"}
        context = AgentDependencyContext(
            agent_id=agent_id,
            team_id="system",
            owner_user_id="platform",
            audience_mode="private",
        )
        risk = _compile_risk(
            risks=[item.risk for item in tool_dependencies],
            tool_dependencies=tool_dependencies,
            skill_dependencies=(),
            context=context,
            unknown_dependencies=unknown_dependencies,
        )
        reasons = tuple(
            sorted(
                {reason for item in tool_dependencies for reason in item.reason_codes} | set(risk.reasons),
            ),
        )
        dependency_fingerprint = _hash_json(
            {
                "compiler_version": AGENT_DEPENDENCY_COMPILER_VERSION,
                "source_definition_hash": source_definition_hash,
                "source_kind": "system_capability",
                "tools": [item.fingerprint_dict() for item in tool_dependencies],
            },
        )
        return AgentDependencyCompilation(
            status="blocked" if unknown_dependencies else "ready",
            reason_codes=reasons,
            dependency_fingerprint=dependency_fingerprint,
            capability_fingerprint=_hash_json(
                {
                    "dependency_fingerprint": dependency_fingerprint,
                    "source_definition_hash": source_definition_hash,
                },
            ),
            tools=tool_dependencies,
            skills=(),
            skill_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=()),
            risk=risk,
        )

    async def exact_grants_for_legacy_names(
        self,
        names: Sequence[str],
        *,
        team_id: str,
        tool_refs: Sequence[str] = (),
    ) -> tuple[ExactSkillGrant, ...]:
        """Resolve the current Creator's name-only selections into exact refs.

        The conversational Creator remains name-based for usability.  The M2
        writer calls this at commit time so the first immutable revision never
        carries an ambiguous legacy Skill scope.

        Every Skill grants read; a runnable Skill (``kind: tool`` with scripts)
        also grants invoke. ``python_sandbox`` keeps its own rule: it is
        invokable only when the Python sandbox Tool is selected.
        """
        if len(names) > _MAX_SKILL_GRANTS or any(not isinstance(name, str) or not _SAFE_TOKEN_RE.fullmatch(name) for name in names):
            raise ValueError("legacy Skill selection is invalid")
        if len(set(names)) != len(names):
            raise ValueError("legacy Skill selection contains duplicates")
        if not isinstance(tool_refs, Sequence) or isinstance(tool_refs, (str, bytes)) or any(not isinstance(ref, str) for ref in tool_refs):
            raise ValueError("legacy tool selection is invalid")
        # Selecting this authoring capability explicitly requests its bounded
        # runtime Skill. Generic skill_invoke never implies an additional grant.
        python_selected = "python_sandbox" in tool_refs
        if python_selected and self._builtin_skills.get("python_sandbox") is None:
            raise ValueError("selected Python capability is unavailable")
        names = tuple(dict.fromkeys((*names, *(("python_sandbox",) if python_selected else ()))))
        if len(names) > _MAX_SKILL_GRANTS:
            raise ValueError("legacy Skill selection is invalid")
        grants: list[ExactSkillGrant] = []
        for name in names:
            builtin = self._builtin_skills.get(name)
            if builtin is not None:
                revision = _bundle_revision(str(builtin.source_dir))
                if revision is None:
                    raise ValueError("selected Skill bundle is unavailable")
                if name == "python_sandbox":
                    runnable = python_selected
                else:
                    runnable = skill_is_runnable(builtin)
                grants.append(
                    ExactSkillGrant(
                        kind="builtin",
                        id=f"builtin:{name}",
                        name=name,
                        permissions=_permissions_for(runnable=runnable),
                        revision=revision,
                        distribution="reference",
                    ),
                )
                continue
            if self._team_skill_store is None:
                raise ValueError("selected Skill is unavailable")
            record = await self._team_skill_store.get_team_skill_by_name(
                team_id,
                name,
                only_enabled=True,
                only_approved=True,
            )
            source_path = str(getattr(record, "source_path", "") or "") if record is not None else ""
            revision = _bundle_revision(source_path)
            if record is None or str(getattr(record, "visibility", "")) != "team_wide" or revision is None:
                raise ValueError("selected Skill is unavailable")
            approved_revision = _revision_token(getattr(record, "approved_sha256", None))
            if approved_revision is None or approved_revision != revision:
                raise ValueError("selected Skill revision is not approved")
            spec = _load_skill(source_path)
            if spec is None or spec.name != name:
                raise ValueError("selected Skill is unavailable")
            grants.append(
                ExactSkillGrant(
                    kind="team",
                    id=str(getattr(record, "id", "")),
                    name=name,
                    permissions=_permissions_for(runnable=skill_is_runnable(spec)),
                    revision=revision,
                    distribution="reference",
                ),
            )
        return tuple(sorted(grants, key=lambda grant: grant.identity))

    async def exact_grant_for_personal_skill(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
    ) -> ExactSkillGrant:
        """Resolve one Owner-private Skill into a path-free immutable grant.

        Personal Skills are deliberately not accepted by the legacy name-only
        converter: names cannot distinguish them from builtin or team Skills.
        The M2 editor lists them by authenticated Owner and persists this exact
        ID + bundle revision. Publication validation still rejects the grant
        for every non-private Audience.
        """
        if self._personal_skill_store is None or not _SAFE_TOKEN_RE.fullmatch(owner_user_id) or not _SAFE_TOKEN_RE.fullmatch(skill_id):
            raise ValueError("selected personal Skill is unavailable")
        record = await self._personal_skill_store.get_by_id(
            user_id=owner_user_id,
            skill_id=skill_id,
        )
        name = str(getattr(record, "name", "") or "") if record is not None else ""
        source_path = str(getattr(record, "source_path", "") or "") if record is not None else ""
        revision = _bundle_revision(source_path)
        spec = _load_skill(source_path)
        if (
            record is None
            or not bool(getattr(record, "enabled", False))
            or getattr(record, "import_status", None) == "pending"  # skill-import batch 1: pending imports cannot be bound.
            or str(getattr(record, "source_type", "")) not in {"user_upload", "user_generated"}
            or not _SAFE_TOKEN_RE.fullmatch(name)
            or revision is None
            or spec is None
            or spec.name != name
        ):
            raise ValueError("selected personal Skill is unavailable")
        return ExactSkillGrant(
            kind="user",
            id=skill_id,
            name=name,
            permissions=_permissions_for(runnable=skill_is_runnable(spec)),
            revision=revision,
            distribution="reference",
        )

    async def compile(
        self,
        definition: Mapping[str, Any],
        *,
        context: AgentDependencyContext | SkillReadContext,
        tool_catalog: SubagentAuthoringCatalogSnapshot,
    ) -> AgentDependencyCompilation:
        if tool_catalog.team_id != context.team_id or tool_catalog.actor_user_id != context.owner_user_id:
            raise ValueError("Tool catalog is not bound to the Agent Owner and team")
        if tool_catalog.schema_version != CATALOG_SCHEMA_VERSION or tool_catalog.risk_registry_version != RISK_REGISTRY_VERSION:
            raise ValueError("Tool catalog version is not supported by this compiler")

        parsed = _parse_definition(definition)
        reasons = set(parsed.reason_codes)
        review_required = False
        unknown_dependencies: set[str] = set()
        extension_policy = read_agent_runtime_extensions(definition)
        if extension_policy is not None:
            extension_policy.require_private_audience(context.audience_mode)
            if extension_policy.skill_read.enabled:
                for selector in extension_policy.skill_read.resource_allowlist:
                    if not await self.resolve_optional_skill_read(selector, context=context):
                        reasons.add("agent_runtime_extension_source_unavailable")
                        unknown_dependencies.add("runtime_extensions")
        if reasons.intersection(
            {
                "dependency_definition_malformed",
                "dependency_limit_exceeded",
                "legacy_skill_scope_not_publishable",
                "skill_grant_duplicate",
                "skill_grant_malformed",
                "tool_reference_duplicate",
            },
        ):
            unknown_dependencies.add("definition")

        # Per-dependency reasons, kept so a blocked publication can name the
        # Skill or Tool and its reason instead of only the merged reason set.
        items: dict[tuple[str, str], tuple[str, set[str]]] = {}
        if reasons:
            items[("definition", "definition")] = ("definition", set(reasons))

        skill_dependencies: list[ResolvedSkillDependency] = []
        skill_names: dict[str, str] = {}
        for grant in parsed.skill_grants:
            # The dependency carries the grant rebased to the Skill's current
            # approved revision; scope and runtime names come from it.
            dependency = await self._resolve_skill(grant, context=context)
            skill_dependencies.append(dependency)
            reasons.update(dependency.reason_codes)
            items[("skill", grant.identity)] = (grant.name, set(dependency.reason_codes))
            review_required = review_required or dependency.review_required
            if not dependency.available or dependency.risk == "unknown":
                unknown_dependencies.add(grant.identity)
            previous = skill_names.setdefault(grant.name, grant.identity)
            if previous != grant.identity:
                reasons.add("skill_runtime_name_conflict")
                unknown_dependencies.update((previous, grant.identity))
                for identity in (previous, grant.identity):
                    items[("skill", identity)][1].add("skill_runtime_name_conflict")

        resolved_grants = [item.grant for item in skill_dependencies]
        read_names = tuple(
            sorted(
                {grant.name for grant in resolved_grants if "read" in grant.permissions},
            ),
        )
        invoke_names = tuple(
            sorted(
                {grant.name for grant in resolved_grants if "invoke" in grant.permissions and "read" in grant.permissions},
            ),
        )
        scope = ExactSkillRuntimeScope(
            read_names=read_names,
            invoke_names=invoke_names,
        )

        # Skill wrapper Tools are not Owner choices (agent-skill-gates D1):
        # stored refs to them are accepted and ignored, and the Skills' own
        # grants decide which wrappers the Agent gets. That holds only when the
        # definition declares ``skill_grants`` (even an empty list): without the
        # key, the runtime has no exact Skill scope and would run the stored
        # wrappers unscoped, so the definition must be converted first.
        if "skill_grants" not in definition and any(ref in COMPILER_DERIVED_SKILL_TOOLS for ref in parsed.tool_refs):
            reasons.add("precise_skill_scope_required")
            items[("definition", "skill_grants")] = ("skill_grants", {"precise_skill_scope_required"})
        effective_tool_refs = {ref for ref in parsed.tool_refs if ref not in COMPILER_DERIVED_SKILL_TOOLS}
        if read_names:
            effective_tool_refs.add("skill_read")
        if invoke_names:
            effective_tool_refs.add("skill_invoke")
        tool_dependencies: list[ResolvedToolDependency] = []
        for tool_ref in sorted(effective_tool_refs):
            tool_reasons: set[str] = set()
            if tool_ref == "python_sandbox" and "python_sandbox" not in invoke_names:
                tool_reasons.add("precise_skill_scope_required")
                unknown_dependencies.add(tool_ref)
            dependency = _resolve_tool(tool_ref, tool_catalog.tools_by_ref)
            tool_dependencies.append(dependency)
            tool_reasons.update(dependency.reason_codes)
            reasons.update(tool_reasons)
            items[("tool", tool_ref)] = (tool_ref, tool_reasons)
            if not dependency.available or dependency.risk == "unknown":
                unknown_dependencies.add(tool_ref)

        risks: list[RiskClass] = [item.risk for item in tool_dependencies]
        risks.extend(item.risk for item in skill_dependencies)
        risk = _compile_risk(
            risks=risks,
            tool_dependencies=tool_dependencies,
            skill_dependencies=skill_dependencies,
            context=context,
            unknown_dependencies=unknown_dependencies,
        )
        reasons.update(risk.reasons)

        blocking_reasons = {
            "dependency_definition_malformed",
            "dependency_limit_exceeded",
            "embedded_skill_scope_not_supported",
            "legacy_skill_scope_not_publishable",
            "precise_skill_scope_required",
            "skill_approval_hash_missing",
            "skill_bundle_unavailable",
            "skill_disabled",
            "skill_import_pending",
            "skill_grant_duplicate",
            "skill_grant_malformed",
            "skill_invoke_requires_read_permission",
            "skill_not_approved",
            "skill_not_invokable",
            "skill_pending_reapproval",
            "skill_reference_audience_not_covered",
            "skill_risk_report_invalid",
            # Only when the report could not be computed from the bundle either.
            "skill_risk_report_missing",
            "skill_runtime_name_conflict",
            "skill_identity_mismatch",
            "tool_reference_duplicate",
            "tool_revision_unavailable",
            "tool_unavailable",
            "tool_risk_metadata_unknown",
        }
        blocked = bool(reasons.intersection(blocking_reasons)) or bool(unknown_dependencies)
        status: CompileStatus = "blocked" if blocked else ("review_required" if review_required else "ready")
        if "runtime_extensions" in unknown_dependencies:
            items[("definition", "runtime_extensions")] = ("runtime_extensions", {"agent_runtime_extension_source_unavailable"})
        blocked_items = _blocked_items(items, blocking_reasons=blocking_reasons, unknown_dependencies=unknown_dependencies) if blocked else ()

        dependency_payload = {
            "compiler_version": AGENT_DEPENDENCY_COMPILER_VERSION,
            "definition_malformed_digest": parsed.malformed_digest,
            "model_role": parsed.model,
            "output_contract_hash": parsed.output_contract_hash,
            "risk_registry_version": tool_catalog.risk_registry_version,
            "skill_scope": scope.to_dict(),
            "skills": [item.fingerprint_dict() for item in sorted(skill_dependencies, key=lambda item: item.grant.identity)],
            "tools": [item.fingerprint_dict() for item in tool_dependencies],
        }
        if extension_policy is not None:
            # This is optional scope, never an additional baseline grant. An
            # unpinned selector follows a currently eligible version at selection
            # time; do not accidentally pin it via the dependency fingerprint.
            dependency_payload["runtime_extensions"] = {"policy_digest": extension_policy.digest}
        dependency_fingerprint = _hash_json(dependency_payload)
        capability_fingerprint = _hash_json(
            {
                "capability_classes": sorted(
                    {capability for item in tool_dependencies for capability in item.capability_classes},
                ),
                "dependency_fingerprint": dependency_fingerprint,
                "output_contract_hash": parsed.output_contract_hash,
                "schema_version": 1,
            },
        )
        return AgentDependencyCompilation(
            status=status,
            reason_codes=tuple(sorted(reasons)),
            dependency_fingerprint=dependency_fingerprint,
            capability_fingerprint=capability_fingerprint,
            tools=tuple(tool_dependencies),
            skills=tuple(sorted(skill_dependencies, key=lambda item: item.grant.identity)),
            skill_scope=scope,
            risk=risk,
            runtime_extension_policy=extension_policy,
            blocked_items=blocked_items,
        )

    async def resolve_optional_skill_read(
        self, selector: SkillExtensionSelector, *, context: AgentDependencyContext | SkillReadContext
    ) -> SkillResourceRef | None:
        """Resolve an opted-in read-only entry, never a baseline or invocation grant."""
        return await self._resolve_skill_reference(selector, context=context, read_only_entry=True)

    async def resolve_baseline_skill(
        self, selector: SkillExtensionSelector, *, context: AgentDependencyContext | SkillReadContext
    ) -> SkillResourceRef | None:
        """Resolve exact package eligibility for authoring a fixed baseline selection.

        This does not admit an optional runtime read or invocation. Those paths
        retain their separate entry declarations and execution permissions.
        """
        return await self._resolve_skill_reference(selector, context=context, read_only_entry=False)

    async def _resolve_skill_reference(
        self, selector: SkillExtensionSelector, *, context: AgentDependencyContext | SkillReadContext, read_only_entry: bool
    ) -> SkillResourceRef | None:
        from functools import partial

        import anyio

        from bowhead_core.chat_references import SkillResourceRef
        from bowhead_core.skill_package_material import SkillPackageMaterialError, capture_skill_package
        from bowhead_core.skill_read_material import SkillReadMaterialError, prepare_skill_read

        try:
            if selector.source_kind == "builtin":
                if not selector.id.startswith("builtin:"):
                    return None
                name = selector.id.removeprefix("builtin:")
                spec = self._builtin_skills.get(name)
                path = str(spec.source_dir) if spec else ""
            elif selector.source_kind == "team":
                if self._team_skill_store is None:
                    return None
                record = await self._team_skill_store.get_team_skill(context.team_id, selector.id, only_enabled=False)
                name, path = str(getattr(record, "name", "") or ""), str(getattr(record, "source_path", "") or "")
            else:
                if self._personal_skill_store is None:
                    return None
                record = await self._personal_skill_store.get_by_id(user_id=context.owner_user_id, skill_id=selector.id)
                if str(getattr(record, "source_type", "")) not in {"user_upload", "user_generated"}:
                    return None
                name, path = str(getattr(record, "name", "") or ""), str(getattr(record, "source_path", "") or "")
            revision = await anyio.to_thread.run_sync(_bundle_revision, path)
            if revision is None or selector.version not in {None, revision}:
                return None
            key = ExactSkillGrant(
                kind=selector.source_kind, id=selector.id, name=name, permissions=("read",), revision=revision, distribution="reference"
            )
            before = await self._resolve_skill(key, context=context)
            if not before.available:
                return None
            resource = SkillResourceRef(kind="skill", source_kind=key.kind, id=key.id, version=key.revision)
            prepare = partial(prepare_skill_read, require_declaration=True) if read_only_entry else capture_skill_package
            await anyio.to_thread.run_sync(partial(prepare, Path(path), resource=resource, expected_name=name))
            after = await self._resolve_skill(key, context=context)
            return resource if after.available and after == before else None
        except (ValueError, OSError, SkillReadMaterialError, SkillPackageMaterialError):
            return None

    async def _resolve_skill(
        self,
        grant: ExactSkillGrant,
        *,
        context: AgentDependencyContext | SkillReadContext,
    ) -> ResolvedSkillDependency:
        if grant.distribution == "embedded_agent_scope":
            return _missing_skill(
                grant,
                "embedded_skill_scope_not_supported",
            )
        if grant.kind == "builtin":
            return self._resolve_builtin_skill(grant)
        if grant.kind == "team":
            return await self._resolve_team_skill(grant, context=context)
        return await self._resolve_user_skill(grant, context=context)

    def _resolve_builtin_skill(
        self,
        grant: ExactSkillGrant,
    ) -> ResolvedSkillDependency:
        if grant.id != f"builtin:{grant.name}":
            return _missing_skill(grant, "skill_identity_mismatch")
        spec = self._builtin_skills.get(grant.name)
        if spec is None:
            return _missing_skill(grant, "skill_bundle_unavailable")
        builtin_root = builtin_catalog_dir().resolve()
        try:
            if not spec.source_dir.resolve().is_relative_to(builtin_root):
                return _missing_skill(grant, "skill_identity_mismatch")
        except OSError:
            return _missing_skill(grant, "skill_bundle_unavailable")
        return _resolved_skill_from_spec(
            grant,
            spec=spec,
            expected_revision=_bundle_revision(str(spec.source_dir)),
            risk_report=None,
            trusted_builtin=True,
        )

    async def _resolve_team_skill(
        self,
        grant: ExactSkillGrant,
        *,
        context: AgentDependencyContext | SkillReadContext,
    ) -> ResolvedSkillDependency:
        if self._team_skill_store is None:
            return _missing_skill(grant, "skill_bundle_unavailable")
        record = await self._team_skill_store.get_team_skill(
            context.team_id,
            grant.id,
            only_enabled=False,
        )
        if record is None or str(getattr(record, "name", "")) != grant.name:
            return _missing_skill(grant, "skill_identity_mismatch")
        if str(getattr(record, "status", "")) != "enabled":
            return _missing_skill(grant, "skill_disabled")
        if str(getattr(record, "approval", "")) != "approved":
            return _missing_skill(grant, "skill_not_approved")
        source_path = str(getattr(record, "source_path", "") or "")
        if not _bundle_is_safe(source_path):
            return _missing_skill(grant, "skill_bundle_unavailable")
        spec = _load_skill(source_path)
        if spec is None:
            return _missing_skill(grant, "skill_bundle_unavailable")
        if spec.name != grant.name:
            return _missing_skill(grant, "skill_identity_mismatch")
        actual_revision = _bundle_revision(source_path)
        approved_revision = _revision_token(getattr(record, "approved_sha256", None))
        if str(getattr(record, "visibility", "")) == "team_wide" and approved_revision is None:
            return _missing_skill(grant, "skill_approval_hash_missing")
        if approved_revision is not None and actual_revision != approved_revision:
            # The bundle was edited after approval; the team admin has not
            # approved the new content yet, so there is no version to rebase to.
            return _missing_skill(grant, "skill_pending_reapproval")
        source_owner = str(getattr(record, "created_by", "") or "")
        team_wide = str(getattr(record, "visibility", "")) == "team_wide"
        rebased = replace(grant, revision=actual_revision) if actual_revision is not None else grant
        if not any(
            _reference_is_covered(candidate, context=context, source_kind="team", source_owner=source_owner, team_wide=team_wide)
            for candidate in (grant, rebased)
        ):
            return _missing_skill(grant, "skill_reference_audience_not_covered")
        return _resolved_skill_from_spec(
            grant,
            spec=spec,
            expected_revision=actual_revision,
            risk_report=getattr(record, "risk_report_json", None),
            trusted_builtin=False,
        )

    async def _resolve_user_skill(
        self,
        grant: ExactSkillGrant,
        *,
        context: AgentDependencyContext | SkillReadContext,
    ) -> ResolvedSkillDependency:
        if self._personal_skill_store is None:
            return _missing_skill(grant, "skill_bundle_unavailable")
        record = await self._personal_skill_store.get_by_id(
            user_id=context.owner_user_id,
            skill_id=grant.id,
        )
        if record is None or str(getattr(record, "name", "")) != grant.name:
            return _missing_skill(grant, "skill_identity_mismatch")
        if not bool(getattr(record, "enabled", False)):
            return _missing_skill(grant, "skill_disabled")
        if getattr(record, "import_status", None) == "pending":
            # skill-import batch 1: bound pending imports compile as unavailable (lifecycle state, not authorization).
            return _missing_skill(grant, "skill_import_pending")
        # Personal Skill instances have no team Audience/share contract today.
        # A caller-provided coverage token must never invent one.
        if context.audience_mode != "private":
            return _missing_skill(grant, "skill_reference_audience_not_covered")
        source_path = str(getattr(record, "source_path", "") or "")
        if not _bundle_is_safe(source_path):
            return _missing_skill(grant, "skill_bundle_unavailable")
        spec = _load_skill(source_path)
        if spec is None:
            return _missing_skill(grant, "skill_bundle_unavailable")
        if spec.name != grant.name:
            return _missing_skill(grant, "skill_identity_mismatch")
        return _resolved_skill_from_spec(
            grant,
            spec=spec,
            expected_revision=_bundle_revision(source_path),
            risk_report=getattr(record, "risk_report_json", None),
            trusted_builtin=False,
        )


def compare_agent_dependencies(
    base: AgentDependencyCompilation,
    candidate: AgentDependencyCompilation,
    *,
    base_context: AgentDependencyContext,
    candidate_context: AgentDependencyContext,
) -> AgentDependencyDiff:
    """Return a safe, ID-only diff for M2 review and notification policy."""
    if (
        base_context.agent_id != candidate_context.agent_id
        or base_context.team_id != candidate_context.team_id
        or base_context.owner_user_id != candidate_context.owner_user_id
    ):
        raise ValueError(
            "dependency diff contexts must belong to one Agent, owner, and team",
        )
    base_tools = {item.stable_ref: item.fingerprint_dict() for item in base.tools}
    candidate_tools = {item.stable_ref: item.fingerprint_dict() for item in candidate.tools}
    base_skills = {item.grant.identity: item.fingerprint_dict() for item in base.skills}
    candidate_skills = {item.grant.identity: item.fingerprint_dict() for item in candidate.skills}

    added_tools, removed_tools, changed_tools = _mapping_diff(base_tools, candidate_tools)
    added_skills, removed_skills, changed_skills = _mapping_diff(base_skills, candidate_skills)
    audience_expanded = _AUDIENCE_ORDER[candidate_context.audience_mode] > _AUDIENCE_ORDER[base_context.audience_mode]
    automation_expanded = _AUTOMATION_ORDER[candidate_context.execution_mode] > _AUTOMATION_ORDER[base_context.execution_mode]
    risk_increased = _risk_level_order(candidate.risk.level) > _risk_level_order(base.risk.level)
    changed_tool_ids = {*added_tools, *changed_tools}
    changed_skill_ids = {*added_skills, *changed_skills}
    added_or_changed_high_risk = any(
        item.stable_ref in changed_tool_ids
        and (item.risk in {"external_write", "destructive", "unknown"} or item.approval_required or item.source_kind == "team_mcp")
        for item in candidate.tools
    ) or any(item.grant.identity in changed_skill_ids and (item.risk == "unknown" or item.review_required) for item in candidate.skills)
    risk_crossed_high_boundary = risk_increased and candidate.risk.level in {
        "high",
        "unknown",
    }
    high_risk_change = risk_crossed_high_boundary or automation_expanded or added_or_changed_high_risk
    review_required = high_risk_change or base.status != "ready" or candidate.status != "ready" or bool(changed_tools or changed_skills)
    return AgentDependencyDiff(
        added_tools=added_tools,
        removed_tools=removed_tools,
        changed_tools=changed_tools,
        added_skills=added_skills,
        removed_skills=removed_skills,
        changed_skills=changed_skills,
        audience_expanded=audience_expanded,
        automation_expanded=automation_expanded,
        risk_increased=risk_increased,
        high_risk_change=high_risk_change,
        review_required=review_required,
    )


def declared_dependencies(
    definition: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[ExactSkillGrant, ...], tuple[str, ...]]:
    """Tool refs, exact Skill grants and legacy Skill names an Agent definition declares.

    Reads both stored shapes (``tool_refs`` / ``skill_grants`` and the imported ``tool_refs_json`` /
    ``skills_json``) through the same parser the compiler uses; malformed entries are dropped, not guessed.
    """
    parsed = _parse_definition(definition)
    legacy = tuple(name for name in _legacy_skill_names(definition) if name != "<malformed>")
    return parsed.tool_refs, parsed.skill_grants, legacy


def _blocked_items(
    items: Mapping[tuple[str, str], tuple[str, set[str]]],
    *,
    blocking_reasons: set[str],
    unknown_dependencies: set[str],
) -> tuple[dict[str, Any], ...]:
    """One entry per Skill, Tool or definition problem that blocks publication."""
    result: list[dict[str, Any]] = []
    for (kind, identity), (name, item_reasons) in sorted(items.items()):
        blocking = sorted(item_reasons.intersection(blocking_reasons))
        if not blocking and identity in unknown_dependencies:
            # Unavailable without a blocking code of its own (for example an
            # unknown risk class); report what is known about it.
            blocking = sorted(item_reasons) or ["dependency_unavailable"]
        if blocking:
            result.append({"kind": kind, "name": name, "identity": identity, "reason_codes": blocking})
    return tuple(result)


def _parse_definition(definition: Mapping[str, Any]) -> _ParsedDefinition:
    reasons: set[str] = set()
    malformed_values: dict[str, str] = {}

    if "tool_refs" in definition and "tool_refs_json" in definition:
        reasons.add("dependency_definition_malformed")
        malformed_values["tool_refs_conflict"] = _hash_json(
            [definition.get("tool_refs"), definition.get("tool_refs_json")],
        )
    raw_tools = definition.get("tool_refs")
    if raw_tools is None and "tool_refs_json" in definition:
        try:
            raw_tools = json.loads(str(definition["tool_refs_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_tools = None
            reasons.add("dependency_definition_malformed")
            malformed_values["tool_refs_json"] = _hash_json(
                definition.get("tool_refs_json"),
            )
    tool_refs = _parse_tool_refs(raw_tools, reasons=reasons)
    if raw_tools is not None and not tool_refs and raw_tools != []:
        malformed_values["tool_refs"] = _hash_json(raw_tools)

    raw_grants = definition.get("skill_grants")
    grants: list[ExactSkillGrant] = []
    if raw_grants is not None:
        if not isinstance(raw_grants, list) or len(raw_grants) > _MAX_SKILL_GRANTS:
            reasons.add("dependency_limit_exceeded" if isinstance(raw_grants, list) else "dependency_definition_malformed")
            malformed_values["skill_grants"] = _hash_json(raw_grants)
        else:
            seen: set[str] = set()
            for raw_grant in raw_grants:
                grant = _parse_skill_grant(raw_grant)
                if grant is None:
                    reasons.add("skill_grant_malformed")
                    malformed_values.setdefault("skill_grants", _hash_json(raw_grants))
                    continue
                if grant.identity in seen:
                    reasons.add("skill_grant_duplicate")
                    continue
                seen.add(grant.identity)
                grants.append(grant)

    legacy_names = _legacy_skill_names(definition)
    if legacy_names:
        reasons.add("legacy_skill_scope_not_publishable")
        reasons.add("precise_skill_scope_required")
        malformed_values["legacy_skills"] = _hash_json(legacy_names)

    model_value = definition.get("model")
    model = str(model_value) if isinstance(model_value, str) and model_value else None
    output_value = definition.get("output_schema", definition.get("output_schema_json"))
    output_contract_hash = _hash_json(output_value)
    return _ParsedDefinition(
        tool_refs=tool_refs,
        skill_grants=tuple(grants),
        model=model,
        output_contract_hash=output_contract_hash,
        reason_codes=tuple(sorted(reasons)),
        malformed_digest=(_hash_json(malformed_values) if malformed_values else None),
    )


def _parse_tool_refs(raw_tools: Any, *, reasons: set[str]) -> tuple[str, ...]:
    if raw_tools is None:
        return ()
    if not isinstance(raw_tools, list):
        reasons.add("dependency_definition_malformed")
        return ()
    if len(raw_tools) > _MAX_TOOL_REFS:
        reasons.add("dependency_limit_exceeded")
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for value in raw_tools:
        if not isinstance(value, str) or not _SAFE_TOKEN_RE.fullmatch(value):
            reasons.add("dependency_definition_malformed")
            continue
        if value in seen:
            reasons.add("tool_reference_duplicate")
            continue
        seen.add(value)
        values.append(value)
    return tuple(sorted(values))


def parse_exact_skill_grants(raw: object) -> tuple[ExactSkillGrant, ...]:
    """Strict, IO-free reader of a published exact Skill grant list.

    Reuse the author's field parser, but reject the entire projection when a
    grant is malformed, duplicated or lacks the supported read prerequisite.
    This reader does not resolve sources, compile dependencies or grant access.
    """
    try:
        if not isinstance(raw, list) or len(raw) > _MAX_SKILL_GRANTS:
            raise ValueError
        grants = []
        identities: set[str] = set()
        names: set[str] = set()
        for item in raw:
            grant = _parse_skill_grant(item)
            if grant is None or "read" not in grant.permissions or grant.identity in identities or grant.name in names:
                raise ValueError
            identities.add(grant.identity)
            names.add(grant.name)
            grants.append(grant)
        return tuple(sorted(grants, key=lambda grant: grant.identity))
    except (TypeError, ValueError):
        raise ValueError("exact_skill_grants_unavailable") from None


def _parse_skill_grant(raw: Any) -> ExactSkillGrant | None:
    if not isinstance(raw, dict) or set(raw) != {"skill_ref", "permissions", "revision", "distribution"}:
        return None
    ref = raw.get("skill_ref")
    permissions = raw.get("permissions")
    revision = raw.get("revision")
    distribution = raw.get("distribution")
    if not isinstance(ref, dict) or set(ref) != {"kind", "id", "name"}:
        return None
    kind = ref.get("kind")
    skill_id = ref.get("id")
    name = ref.get("name")
    if kind not in {"builtin", "team", "user"}:
        return None
    if not isinstance(skill_id, str) or not _SAFE_TOKEN_RE.fullmatch(skill_id):
        return None
    if not isinstance(name, str) or not _SAFE_TOKEN_RE.fullmatch(name):
        return None
    if not isinstance(revision, str) or not _HASH_RE.fullmatch(revision):
        return None
    if distribution not in {"reference", "embedded_agent_scope"}:
        return None
    if not isinstance(permissions, list) or not permissions or len(permissions) > 2:
        return None
    if any(permission not in {"read", "invoke"} for permission in permissions):
        return None
    normalized_permissions = tuple(sorted(set(permissions)))
    if len(normalized_permissions) != len(permissions):
        return None
    if "invoke" in normalized_permissions and "read" not in normalized_permissions:
        # Invocation without readable instructions produces an opaque script
        # capability and is not a supported publication contract.
        return ExactSkillGrant(
            kind=kind,
            id=skill_id,
            name=name,
            permissions=normalized_permissions,
            revision=revision,
            distribution=distribution,
        )
    return ExactSkillGrant(
        kind=kind,
        id=skill_id,
        name=name,
        permissions=normalized_permissions,
        revision=revision,
        distribution=distribution,
    )


def _legacy_skill_names(definition: Mapping[str, Any]) -> tuple[str, ...]:
    raw = definition.get("skills")
    if raw is None and "skills_json" in definition:
        try:
            raw = json.loads(str(definition["skills_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ("<malformed>",)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        return ("<malformed>",)
    return tuple(sorted(set(raw)))


def _resolve_tool(
    tool_ref: str,
    tools_by_ref: Mapping[str, AuthoringTool],
) -> ResolvedToolDependency:
    tool = tools_by_ref.get(tool_ref)
    if tool is None:
        return ResolvedToolDependency(
            stable_ref=tool_ref,
            revision="unresolved",
            source_kind="unknown",
            risk="unknown",
            risk_source="unknown_default",
            approval_required=True,
            capability_classes=(),
            connection_ref=None,
            operation_id=None,
            method=None,
            available=False,
            reason_codes=("tool_unavailable",),
        )
    reasons: set[str] = set()
    if not tool.dependency_revision:
        reasons.add("tool_revision_unavailable")
    if not tool.selectable:
        reasons.add(tool.locked_reason or "tool_unavailable")
    if tool.risk == "unknown":
        reasons.add("tool_risk_metadata_unknown")
    connection_ref = f"team_tool_source:{tool.source_id}" if tool.source_kind == "team_mcp" and tool.source_id else None
    return ResolvedToolDependency(
        stable_ref=tool.stable_ref,
        revision=tool.dependency_revision,
        source_kind=tool.source_kind,
        risk=tool.risk,
        risk_source=tool.risk_source,
        approval_required=tool.approval_required,
        capability_classes=tuple(sorted(tool.capability_classes)),
        connection_ref=connection_ref,
        operation_id=tool.operation_id,
        method=tool.method,
        available=bool(tool.dependency_revision) and tool.selectable and tool.risk != "unknown",
        reason_codes=tuple(sorted(reasons)),
        source_id=tool.source_id,
    )


def _resolved_skill_from_spec(
    grant: ExactSkillGrant,
    *,
    spec: SkillSpec,
    expected_revision: str | None,
    risk_report: Any,
    trusted_builtin: bool,
) -> ResolvedSkillDependency:
    reasons: set[str] = set()
    risk_reasons: set[str] = set()
    risk_evidence_digest: str | None = None
    review_required = False
    if expected_revision is None:
        return _missing_skill(grant, "skill_bundle_unavailable")
    if grant.revision != expected_revision:
        # Skills follow their latest approved revision (agent-skill-gates D2):
        # the grant is rebased, and the stored definition records the rebased
        # revision when a candidate is frozen (prepare or first release).
        grant = replace(grant, revision=expected_revision)
    has_scripts = skill_has_runnable_scripts(spec)
    if "invoke" in grant.permissions and "read" not in grant.permissions:
        reasons.add("skill_invoke_requires_read_permission")
    if "invoke" in grant.permissions and (spec.kind != "tool" or not has_scripts):
        reasons.add("skill_not_invokable")

    if "invoke" not in grant.permissions:
        risk: RiskClass = "read"
    elif trusted_builtin and spec.name in _ARTIFACT_BUILTIN_SKILLS:
        risk = "artifact_write"
        risk_reasons.add("internal_artifact_write")
    elif trusted_builtin:
        risk = "compute"
    else:
        risk = "artifact_write"
        risk_reasons.add("sandboxed_skill_artifact_write")

    if not trusted_builtin and "invoke" in grant.permissions:
        if risk_report is None or risk_report == "":
            # Skills uploaded while the multi-runtime switch was off carry no
            # report; compute the same report from the bundle (agent-skill-gates D3).
            risk_report = _bundle_risk_report(spec)
        parsed_report, report_reason = _parse_skill_risk_report(risk_report)
        if report_reason is not None:
            reasons.add(report_reason)
            risk = "unknown"
        elif parsed_report is not None:
            risk_evidence_digest = str(parsed_report["digest"])
            if parsed_report["keywords"]:
                # A notice, not a review gate: scripts run in the sandbox without
                # network access, and the Skill itself was already approved.
                risk_reasons.add("skill_static_risk_signal")
            if parsed_report["vendored"]:
                # Bundled node_modules can shadow curated packages and change
                # which code runs, so this keeps requiring review.
                risk_reasons.add("skill_vendored_dependency")
                review_required = True
    available = not reasons
    return ResolvedSkillDependency(
        grant=grant,
        kind=spec.kind,
        has_scripts=has_scripts,
        risk=risk,
        risk_reasons=tuple(sorted(risk_reasons)),
        risk_evidence_digest=risk_evidence_digest,
        review_required=review_required,
        available=available,
        reason_codes=tuple(sorted(reasons)),
    )


def _bundle_risk_report(spec: SkillSpec) -> dict[str, Any] | None:
    from bowhead_core.skill_upload import build_bundle_risk_report

    try:
        return build_bundle_risk_report(Path(spec.source_dir))
    except (OSError, ValueError):
        return None


def _parse_skill_risk_report(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if raw is None or raw == "":
        return None, "skill_risk_report_missing"
    if isinstance(raw, str) and len(raw.encode("utf-8")) > _MAX_RISK_REPORT_BYTES:
        return None, "skill_risk_report_invalid"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "skill_risk_report_invalid"
    if not isinstance(value, dict):
        return None, "skill_risk_report_invalid"
    if not {"langs", "keywords", "shebangs"}.issubset(value) or not set(value).issubset(
        {"langs", "keywords", "shebangs", "vendored_node_modules"},
    ):
        return None, "skill_risk_report_invalid"
    langs = value["langs"]
    keywords = value["keywords"]
    shebangs = value["shebangs"]
    vendored = value.get("vendored_node_modules")
    if (
        not isinstance(langs, dict)
        or len(langs) > 20
        or not all(
            isinstance(key, str) and 0 < len(key) <= 20 and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 2_000
            for key, count in langs.items()
        )
    ):
        return None, "skill_risk_report_invalid"
    if not isinstance(keywords, list) or len(keywords) > 50 or not all(isinstance(item, str) and len(item) <= 200 for item in keywords):
        return None, "skill_risk_report_invalid"
    if not isinstance(shebangs, list) or len(shebangs) > 50 or not all(isinstance(item, str) and len(item) <= 200 for item in shebangs):
        return None, "skill_risk_report_invalid"
    vendored_present = False
    if vendored is not None:
        if not isinstance(vendored, dict) or set(vendored) != {"present", "file_count", "shadows"} or not isinstance(vendored.get("present"), bool):
            return None, "skill_risk_report_invalid"
        shadows = vendored.get("shadows", [])
        file_count = vendored.get("file_count", 0)
        if not isinstance(shadows, list) or len(shadows) > 100 or not all(isinstance(item, str) and len(item) <= 200 for item in shadows):
            return None, "skill_risk_report_invalid"
        if not isinstance(file_count, int) or isinstance(file_count, bool) or not 0 <= file_count <= 100_000:
            return None, "skill_risk_report_invalid"
        vendored_present = bool(vendored["present"])
    return {
        "digest": _hash_json(value),
        "keywords": tuple(sorted(set(keywords))),
        "vendored": vendored_present,
    }, None


def _compile_risk(
    *,
    risks: Sequence[RiskClass],
    tool_dependencies: Sequence[ResolvedToolDependency],
    skill_dependencies: Sequence[ResolvedSkillDependency],
    context: AgentDependencyContext | SkillReadContext,
    unknown_dependencies: set[str],
) -> AgentRiskAssessment:
    effective_risks = [*risks]
    if unknown_dependencies:
        effective_risks.append("unknown")
    highest = max(effective_risks or ("read",), key=lambda value: _RISK_ORDER[value])
    reasons: set[str] = set()
    for item in tool_dependencies:
        if item.risk in {"external_write", "destructive"}:
            reasons.add(item.risk)
    for item in skill_dependencies:
        reasons.update(item.risk_reasons)
    has_team_tool = any(item.source_kind == "team_mcp" for item in tool_dependencies)
    has_data_tool = any(
        capability in {"data_discovery", "data_query", "knowledge_search"} for item in tool_dependencies for capability in item.capability_classes
    )
    if has_team_tool:
        data = "external_unclassified"
        reasons.add("external_data_source")
    elif has_data_tool:
        data = "internal_authorized"
    else:
        data = "none"

    effect = {
        "read": "read_only",
        "compute": "compute",
        "artifact_write": "internal_artifact_write",
        "external_write": "external_write",
        "destructive": "destructive",
        "unknown": "unknown",
    }[highest]
    # skill_static_risk_signal stays a reason (a notice in review data) but no
    # longer raises the level on its own (agent-skill-gates D4).
    elevated_skill_risk = "skill_vendored_dependency" in reasons
    if highest == "unknown":
        level: Literal["low", "medium", "high", "unknown"] = "unknown"
    elif highest in {"external_write", "destructive"} or elevated_skill_risk:
        level = "high"
    elif highest == "artifact_write" or context.execution_mode != "interactive" or has_team_tool:
        level = "medium"
    else:
        level = "low"
    if context.execution_mode != "interactive":
        reasons.add("automated_execution")
    approval_required = context.audience_mode != "private" and (level == "high" or any(item.approval_required for item in tool_dependencies))
    runtime_confirmation_required = highest in {"external_write", "destructive"}
    scope = "external_system" if has_team_tool else ("team_resource" if context.audience_mode != "private" else "current_principal")
    reversibility = {
        "read": "none",
        "compute": "none",
        "artifact_write": "reversible",
        "external_write": "compensatable",
        "destructive": "irreversible",
        "unknown": "unknown",
    }[highest]
    return AgentRiskAssessment(
        level=level,
        data=data,
        effect=effect,
        automation=context.execution_mode,
        scope=scope,
        reversibility=reversibility,
        reasons=tuple(sorted(reasons)),
        unknown_dependencies=tuple(sorted(unknown_dependencies)),
        approval_required=approval_required,
        runtime_confirmation_required=runtime_confirmation_required,
    )


def _reference_is_covered(
    grant: ExactSkillGrant,
    *,
    context: AgentDependencyContext | SkillReadContext,
    source_kind: str,
    source_owner: str,
    team_wide: bool,
) -> bool:
    if source_kind == "team" and team_wide:
        return True
    if context.audience_mode == "private" and source_owner == context.owner_user_id:
        return True
    return grant.coverage_key in context.reference_coverage


def _missing_skill(
    grant: ExactSkillGrant,
    reason: str,
) -> ResolvedSkillDependency:
    return ResolvedSkillDependency(
        grant=grant,
        kind="unknown",
        has_scripts=False,
        risk="unknown",
        risk_reasons=(),
        risk_evidence_digest=None,
        review_required=False,
        available=False,
        reason_codes=(reason,),
    )


def skill_has_runnable_scripts(spec: SkillSpec) -> bool:
    """Whether the bundle carries an entry script in any supported language.

    Independent of ``BOWHEAD_SKILL_MULTIRUNTIME``: an Agent keeps its run
    permission when the deployment switch is off, and the run reports the
    switch instead (agent-skill-gates D1). ``spec.scripts`` follows the switch,
    so it is not used here.
    """
    if spec.scripts:
        return True
    scripts_dir = Path(spec.source_dir) / "scripts"
    extensions = frozenset(all_entry_extensions())
    try:
        return scripts_dir.is_dir() and any(
            path.is_file() and path.name != "__init__.py" and path.suffix in extensions for path in scripts_dir.rglob("*")
        )
    except OSError:
        return False


def skill_is_runnable(spec: SkillSpec) -> bool:
    """A Skill whose grant carries invoke as well as read (``kind: tool`` with scripts)."""
    return spec.kind == "tool" and skill_has_runnable_scripts(spec)


def skill_script_runtime(spec: SkillSpec, *, source_kind: str) -> Literal["none", "enabled", "disabled"]:
    """Whether this deployment runs the Skill's scripts.

    Built-in Skills run from the trusted catalog regardless of the switch;
    uploaded team and personal Skills need ``BOWHEAD_SKILL_MULTIRUNTIME``.
    """
    from bowhead_skill.script_langs import multiruntime_enabled

    if not skill_is_runnable(spec):
        return "none"
    if source_kind == "builtin" or multiruntime_enabled():
        return "enabled"
    return "disabled"


def _permissions_for(*, runnable: bool) -> tuple[SkillPermission, ...]:
    return ("invoke", "read") if runnable else ("read",)


def _load_skill(source_path: str) -> SkillSpec | None:
    if not source_path:
        return None
    try:
        return load_skill(Path(source_path))
    except (OSError, ValueError):
        return None


def _bundle_revision(source_path: str) -> str | None:
    if not source_path:
        return None
    root = Path(source_path)
    if not _bundle_is_safe(source_path):
        return None
    try:
        digest = bundle_sha256(root)
    except OSError:
        return None
    return f"sha256:{digest}"


_PYTHON_SANDBOX = "python_sandbox"


def with_python_sandbox_skill_grant(definition: Mapping[str, Any], *, builtin_skills: SkillCatalog | None = None) -> dict[str, Any]:
    """Add the exact invoke grant that selecting the Python sandbox Tool requests.

    The same rule as ``exact_grants_for_legacy_names(tool_refs=...)`` applies to a
    Tool that a built-in Plugin contributes: the Tool needs an exact invoke grant
    on the built-in ``python_sandbox`` Skill. Expansion calls this, so the grant is
    resolved again on every expansion (draft check, publication, run admission) and
    is never written back to a stored revision. A definition that already grants
    invoke, or whose grants can't take one, is returned unchanged; the compiler
    then reports what is missing.
    """
    expanded = dict(definition)
    raw = definition.get("skill_grants", [])
    if _PYTHON_SANDBOX not in (definition.get("tool_refs") or ()) or not isinstance(raw, list):
        return expanded
    grants = list(raw)
    existing = [index for index, item in enumerate(grants) if isinstance(item, dict) and (item.get("skill_ref") or {}).get("name") == _PYTHON_SANDBOX]
    if existing:
        index = existing[0]
        grant = _parse_skill_grant(grants[index])
        if len(existing) > 1 or grant is None or grant.kind != "builtin" or grant.id != f"builtin:{_PYTHON_SANDBOX}" or "invoke" in grant.permissions:
            return expanded
        grants[index] = {**grants[index], "permissions": list(_permissions_for(runnable=True))}
    else:
        if len(grants) >= _MAX_SKILL_GRANTS:
            return expanded
        builtin = (builtin_skills or _builtin_skill_catalog()).get(_PYTHON_SANDBOX)
        revision = _bundle_revision(str(builtin.source_dir)) if builtin is not None else None
        if revision is None:
            return expanded
        grants.append(
            ExactSkillGrant(
                kind="builtin",
                id=f"builtin:{_PYTHON_SANDBOX}",
                name=_PYTHON_SANDBOX,
                permissions=_permissions_for(runnable=True),
                revision=revision,
                distribution="reference",
            ).to_canonical_dict()
        )
    expanded["skill_grants"] = grants
    return expanded


@lru_cache(maxsize=1)
def _builtin_skill_catalog() -> SkillCatalog:
    # Built-in Skills ship with the image and don't change while it runs.
    return SkillCatalog(roots=[builtin_catalog_dir()])


def skill_bundle_revision(source_path: str) -> str | None:
    """``sha256:<digest>`` of a safe Skill bundle, the revision an exact grant records."""
    return _bundle_revision(source_path)


def approved_revision_token(value: Any) -> str | None:
    """A team Skill's stored ``approved_sha256`` as a grant revision, or None."""
    return _revision_token(value)


def _bundle_is_safe(source_path: str) -> bool:
    if not source_path:
        return False
    root = Path(source_path)
    try:
        if not root.is_dir() or root.is_symlink():
            return False
        return not any(
            (base / name).is_symlink() for base, dirnames, filenames in root.walk(follow_symlinks=False) for name in (*dirnames, *filenames)
        )
    except OSError:
        return False


def _revision_token(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    token = value if value.startswith("sha256:") else f"sha256:{value}"
    return token if _HASH_RE.fullmatch(token) else None


def _mapping_diff(
    base: Mapping[str, dict[str, Any]],
    candidate: Mapping[str, dict[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    base_keys = set(base)
    candidate_keys = set(candidate)
    added = tuple(sorted(candidate_keys - base_keys))
    removed = tuple(sorted(base_keys - candidate_keys))
    changed = tuple(
        sorted(key for key in base_keys.intersection(candidate_keys) if base[key] != candidate[key]),
    )
    return added, removed, changed


def _risk_level_order(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "unknown": 3}.get(value, 3)


def _hash_json(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        payload = json.dumps(
            {"unserializable_type": type(value).__name__},
            separators=(",", ":"),
            sort_keys=True,
        )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "AGENT_DEPENDENCY_COMPILER_VERSION",
    "AGENT_RISK_COMPILER_VERSION",
    "AgentDependencyCompilation",
    "AgentDependencyCompiler",
    "AgentDependencyContext",
    "AgentDependencyDiff",
    "AgentRiskAssessment",
    "ExactSkillGrant",
    "ExactSkillRuntimeScope",
    "PRECISE_SKILL_SCOPE_VERSION",
    "approved_revision_token",
    "compare_agent_dependencies",
    "skill_bundle_revision",
    "skill_has_runnable_scripts",
    "skill_is_runnable",
    "skill_script_runtime",
]
