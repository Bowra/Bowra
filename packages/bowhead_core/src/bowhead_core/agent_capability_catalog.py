"""Versioned Capability contracts and read-only legacy drift compilation.

Capability is a product contract, not an execution or authorization layer.  M3-1
loads Bowhead-owned immutable Definition/Implementation manifests and compares
them with today's system/team Agent declarations.  The compiler deliberately
does not select an implementation or change runtime routing.

The public projection contains stable capability identifiers and typed contracts,
but never exposes Agent prompts, raw Tool/Skill references, connection details or
Audience membership.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_core.agent_behavior_contract import LeadRoleId
from bowhead_core.agents.exceptions import ManifestValidationError
from bowhead_core.agents.manifest import AgentManifest
from bowhead_core.agents.registry import _validate_path

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride

CAPABILITY_CATALOG_COMPILER_VERSION = "agent-capability-drift-v1"
CAPABILITY_CATALOG_SCHEMA_VERSION = 1
AGENT_CAPABILITY_REPORT_SCHEMA_VERSION = 2
SUPPORTED_CAPABILITY_LOCALES = ("en-US", "zh-CN")

_PACKAGE_ROOT = Path(__file__).parent
_DEFAULT_MANIFEST_DIR = _PACKAGE_ROOT / "agents" / "manifests" / "capabilities"
_CAPABILITY_ID_PATTERN = r"^cap_[a-z][a-z0-9_]{1,63}$"
_IMPLEMENTATION_ID_PATTERN = r"^capi_[a-z][a-z0-9_]{1,95}$"
_STABLE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_EFFECT_ORDER = {
    "read": 0,
    "compute": 1,
    "internal_artifact_write": 2,
    "external_write": 3,
    "destructive": 4,
}
CapabilityTaskExample = Annotated[str, Field(min_length=1, max_length=240)]

# Conservative evidence shared by drift reports and legacy role routing.
# These hints never authorize execution or establish verified implementations.
_LEGACY_TOOL_CAPABILITY_HINTS: dict[str, tuple[str, ...]] = {
    "cap_template_document": ("template_document_submit",),
    "cap_compute": ("python_exec", "python_sandbox"),
    "cap_data_discovery": ("bi_schema",),
    "cap_data_query": (
        "bi_query",
        "bi_ask_question",
        "bi_fetch_rows",
    ),
    "cap_knowledge_search": ("kb_search", "kb_query_records", "kb_get_record"),
    "cap_report": ("write_report",),
    "cap_visualize": (
        "add_chart",
        "add_image",
        "image_generate",
        "canvas_create_design",
    ),
    "cap_web_research": ("web_search", "web_fetch", "web_extract", "web_map", "web_crawl", "web_deep_research"),
}


def builtin_powerbi_operation(ref: str) -> str | None:
    parts = ref.split(".")
    if len(parts) != 3 or parts[0] != "teammcp" or not parts[1].startswith(("ibt_", "pbi_")):
        return None
    return parts[2] if parts[2] in {"pbi_execute_dax", "pbi_fetch_rows", "pbi_execute_template", "pbi_list_measures", "pbi_list_templates"} else None


class CapabilityLocaleListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    task_examples: tuple[CapabilityTaskExample, ...] = Field(min_length=1, max_length=5)


class CapabilityContractRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)
    required: bool = True

    @property
    def exact_ref(self) -> str:
        return f"{self.contract}.v{self.version}"


class CapabilityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_CAPABILITY_ID_PATTERN)
    version: int = Field(ge=1)

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class VersionedComponentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class SkillRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(pattern=r"^skill://[a-zA-Z0-9_.-]+@sha256:[0-9a-f]{64}$")
    permission: Literal["read", "invoke"]
    optional: bool = False


class ArtifactImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)
    adapter: VersionedComponentRef


class PromptModuleRef(VersionedComponentRef):
    sha256: str = Field(pattern=_SHA256_PATTERN)


class CapabilityDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_CAPABILITY_ID_PATTERN)
    version: int = Field(ge=1)
    compatibility_major: int = Field(ge=1)
    lifecycle: Literal["active", "deprecated", "retired"]
    successor: CapabilityRef | None = None
    category: str = Field(pattern=_STABLE_ID_PATTERN)
    listing_revision: int = Field(ge=1)
    listing: dict[str, CapabilityLocaleListing]
    parameters_schema: dict[str, Any]
    inputs: tuple[CapabilityContractRef, ...]
    outputs: tuple[CapabilityContractRef, ...] = Field(min_length=1)
    compatibility_class: str = Field(pattern=_STABLE_ID_PATTERN)
    market_visible: bool
    authoring_selectable: bool
    system_only: bool = False
    contract_test_family: str = Field(pattern=_STABLE_ID_PATTERN)

    @field_validator("listing")
    @classmethod
    def _supported_locales_are_complete(
        cls,
        value: dict[str, CapabilityLocaleListing],
    ) -> dict[str, CapabilityLocaleListing]:
        if set(value) != set(SUPPORTED_CAPABILITY_LOCALES):
            raise ValueError(f"listing locales must be exactly {SUPPORTED_CAPABILITY_LOCALES}")
        return value

    @field_validator("parameters_schema")
    @classmethod
    def _parameters_are_closed(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object" or value.get("additionalProperties") is not False:
            raise ValueError("parameters_schema must be a closed object schema")
        return value

    @field_validator("inputs", "outputs")
    @classmethod
    def _contracts_are_unique(
        cls,
        value: tuple[CapabilityContractRef, ...],
    ) -> tuple[CapabilityContractRef, ...]:
        refs = [item.exact_ref for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate capability contract")
        return value

    @model_validator(mode="after")
    def _retired_has_successor(self) -> CapabilityDefinition:
        if self.lifecycle == "retired" and self.successor is None:
            raise ValueError("retired definition requires successor")
        return self

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class CapabilityImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_IMPLEMENTATION_ID_PATTERN)
    version: int = Field(ge=1)
    capability: CapabilityRef
    lifecycle: Literal["active", "deprecated", "revoked"]
    replacement: VersionedComponentRef | None = None
    required_tools: tuple[str, ...] = ()
    optional_tools: tuple[str, ...] = ()
    required_skills: tuple[SkillRequirement, ...] = ()
    required_connections: tuple[str, ...] = ()
    artifact: ArtifactImplementation | None = None
    prompt_modules: tuple[PromptModuleRef, ...] = ()
    lead_role_ids: tuple[LeadRoleId, ...] = ()
    output_grant: VersionedComponentRef | None = None
    risk_floor: Literal[
        "read",
        "compute",
        "internal_artifact_write",
        "external_write",
        "destructive",
    ]
    effect_class: Literal[
        "read",
        "compute",
        "internal_artifact_write",
        "external_write",
        "destructive",
    ]
    availability_checks: tuple[str, ...] = Field(min_length=1)
    eval_suite: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    priority: int = Field(ge=1, le=10000)
    source: Literal["bowhead_core"] = "bowhead_core"
    owner: str = Field(pattern=_STABLE_ID_PATTERN)
    review_state: Literal["shadow", "reviewed"] = "shadow"
    reviewed_at: datetime | None = None

    @field_validator(
        "required_tools",
        "optional_tools",
        "required_connections",
        "availability_checks",
    )
    @classmethod
    def _stable_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate dependency")
        if any(re.fullmatch(_STABLE_ID_PATTERN, item) is None for item in value):
            raise ValueError("dependency must use a stable identifier")
        return value

    @field_validator("required_skills")
    @classmethod
    def _skills_are_unique(
        cls,
        value: tuple[SkillRequirement, ...],
    ) -> tuple[SkillRequirement, ...]:
        refs = [(item.ref, item.permission) for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate Skill requirement")
        return value

    @field_validator("prompt_modules")
    @classmethod
    def _prompt_modules_are_unique(
        cls,
        value: tuple[PromptModuleRef, ...],
    ) -> tuple[PromptModuleRef, ...]:
        refs = [(item.id, item.version) for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate Prompt Module requirement")
        return value

    @field_validator("lead_role_ids")
    @classmethod
    def _lead_roles_are_sorted_and_unique(
        cls,
        value: tuple[LeadRoleId, ...],
    ) -> tuple[LeadRoleId, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("lead_role_ids must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _dependency_and_lifecycle_guards(self) -> CapabilityImplementation:
        if set(self.required_tools) & set(self.optional_tools):
            raise ValueError("required_tools and optional_tools must be disjoint")
        if self.lifecycle == "revoked" and self.replacement is None:
            raise ValueError("revoked implementation requires replacement")
        if self.review_state == "reviewed" and self.eval_suite is None:
            raise ValueError("reviewed implementation requires eval_suite")
        if _EFFECT_ORDER[self.risk_floor] < _EFFECT_ORDER[self.effect_class]:
            raise ValueError("risk_floor cannot be lower than effect_class")
        return self

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class CapabilityManifestBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    definitions: tuple[CapabilityDefinition, ...]
    implementations: tuple[CapabilityImplementation, ...]


class CapabilityCatalog:
    """Immutable, exact-version registry loaded from code-owned manifests."""

    def __init__(
        self,
        *,
        definitions: dict[tuple[str, int], CapabilityDefinition],
        implementations: dict[tuple[str, int], CapabilityImplementation],
    ) -> None:
        self._definitions = definitions
        self._implementations = implementations
        self.fingerprint = _sha256(
            {
                "schema_version": CAPABILITY_CATALOG_SCHEMA_VERSION,
                "definitions": [definition.model_dump(mode="json") for _, definition in sorted(definitions.items())],
                "implementations": [
                    implementation.model_dump(
                        mode="json",
                        exclude=({"reviewed_at"} if implementation.reviewed_at is None else None),
                    )
                    for _, implementation in sorted(implementations.items())
                ],
            },
        )

    @classmethod
    def bootstrap(cls, manifest_dir: Path | None = None) -> CapabilityCatalog:
        base = manifest_dir or _DEFAULT_MANIFEST_DIR
        if not base.is_dir():
            raise ManifestValidationError(f"capability manifest dir not found: {base}")
        definitions: dict[tuple[str, int], CapabilityDefinition] = {}
        implementations: dict[tuple[str, int], CapabilityImplementation] = {}
        for path in sorted(base.glob("*.yaml")):
            _validate_path(path, base)
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                bundle = CapabilityManifestBundle.model_validate(raw)
            except Exception as exc:
                raise ManifestValidationError(f"failed to load capability manifest {path}: {exc}") from exc
            for definition in bundle.definitions:
                key = (definition.id, definition.version)
                if key in definitions:
                    raise ManifestValidationError(f"duplicate capability definition: {definition.exact_ref}")
                definitions[key] = definition
            for implementation in bundle.implementations:
                key = (implementation.id, implementation.version)
                if key in implementations:
                    raise ManifestValidationError(f"duplicate capability implementation: {implementation.exact_ref}")
                implementations[key] = implementation
        if not definitions:
            raise ManifestValidationError("capability catalog has no definitions")
        for implementation in implementations.values():
            key = (implementation.capability.id, implementation.capability.version)
            if key not in definitions:
                raise ManifestValidationError(
                    f"{implementation.exact_ref} references unknown definition {implementation.capability.exact_ref}",
                )
            definition = definitions[key]
            if implementation.artifact is not None:
                artifact_ref = f"{implementation.artifact.contract}.v{implementation.artifact.version}"
                if artifact_ref not in {item.exact_ref for item in definition.outputs}:
                    raise ManifestValidationError(
                        f"{implementation.exact_ref} artifact {artifact_ref} is not a definition output",
                    )
        priorities: set[tuple[str, int, int]] = set()
        prompt_module_hashes: dict[tuple[str, int], str] = {}
        for implementation in implementations.values():
            for prompt_module in implementation.prompt_modules:
                prompt_key = (prompt_module.id, prompt_module.version)
                existing_hash = prompt_module_hashes.setdefault(prompt_key, prompt_module.sha256)
                if existing_hash != prompt_module.sha256:
                    raise ManifestValidationError(
                        f"conflicting Prompt Module identity: {prompt_module.exact_ref}",
                    )
            if implementation.lifecycle == "revoked":
                continue
            priority_key = (
                implementation.capability.id,
                implementation.capability.version,
                implementation.priority,
            )
            if priority_key in priorities:
                raise ManifestValidationError(
                    f"ambiguous implementation priority for {implementation.capability.exact_ref}: {implementation.priority}",
                )
            priorities.add(priority_key)
        for definition in definitions.values():
            if definition.successor is not None:
                successor_key = (definition.successor.id, definition.successor.version)
                if successor_key not in definitions or successor_key == (definition.id, definition.version):
                    raise ManifestValidationError(
                        f"{definition.exact_ref} has invalid successor {definition.successor.exact_ref}",
                    )
        for implementation in implementations.values():
            if implementation.replacement is not None:
                replacement_key = (implementation.replacement.id, implementation.replacement.version)
                if replacement_key not in implementations or replacement_key == (
                    implementation.id,
                    implementation.version,
                ):
                    replacement_ref = f"{implementation.replacement.id}@{implementation.replacement.version}"
                    raise ManifestValidationError(
                        f"{implementation.exact_ref} has invalid replacement {replacement_ref}",
                    )
        return cls(definitions=definitions, implementations=implementations)

    def get_definition(self, capability_id: str, version: int) -> CapabilityDefinition:
        try:
            return self._definitions[(capability_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown capability definition: {capability_id}@{version}") from exc

    def latest_definition(self, capability_id: str) -> CapabilityDefinition | None:
        candidates = [
            definition for (item_id, _), definition in self._definitions.items() if item_id == capability_id and definition.lifecycle != "retired"
        ]
        return max(candidates, key=lambda item: item.version, default=None)

    def get_implementation(self, implementation_id: str, version: int) -> CapabilityImplementation:
        try:
            return self._implementations[(implementation_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown capability implementation: {implementation_id}@{version}") from exc

    def implementations_for(self, definition: CapabilityDefinition) -> tuple[CapabilityImplementation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._implementations.values()
                    if item.capability.id == definition.id and item.capability.version == definition.version and item.lifecycle != "revoked"
                ),
                key=lambda item: (-item.priority, item.id, item.version),
            ),
        )

    @property
    def definitions(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(definition for _, definition in sorted(self._definitions.items()))

    @property
    def implementations(self) -> tuple[CapabilityImplementation, ...]:
        return tuple(implementation for _, implementation in sorted(self._implementations.items()))

    def validate_reviewed_evidence(self) -> None:
        """Reject a reviewed Implementation backed only by an unbound label."""
        from bowhead_core.agent_behavior_parity import AGENT_BEHAVIOR_PARITY_SUITES

        for implementation in self.implementations:
            if implementation.review_state != "reviewed":
                continue
            suite = AGENT_BEHAVIOR_PARITY_SUITES.get(implementation.eval_suite or "")
            if suite is None or suite.implementation_ref != implementation.exact_ref:
                raise ManifestValidationError(
                    f"{implementation.exact_ref} has no exact executable parity suite",
                )


def latest_active_capability_ref(capability_id: str) -> str:
    """Pin a legacy manifest Capability ID to the current exact active version."""
    catalog = default_catalog()
    definition = catalog.latest_definition(capability_id) if catalog is not None else None
    if definition is None or definition.lifecycle != "active":
        raise KeyError(f"unknown active capability: {capability_id}")
    return definition.exact_ref


@dataclass(frozen=True, slots=True)
class LegacyCapabilitySource:
    agent_id: str
    source_kind: Literal["system", "team"]
    source_version: str
    listing_capability_ids: tuple[str, ...]
    tool_refs: tuple[str, ...]
    skill_refs: tuple[str, ...]
    declared_outputs: tuple[str, ...] = ()
    prompt_protocols: tuple[str, ...] = ()
    prompt_fingerprint: str | None = None
    implicit_team_tool_overlay: bool = False
    internal_runtime_profile: bool = False


@dataclass(frozen=True, slots=True)
class CompiledCapability:
    capability_id: str
    definition: CapabilityDefinition | None
    implementation: CapabilityImplementation | None
    availability: Literal["degraded", "unknown"]
    evidence_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityReadinessContext:
    """Actor-scoped Agent readiness already resolved by the caller.

    The capability compiler remains a read-only evidence projection.  It may
    narrow an Agent-level positive result when evidence is incomplete, but it
    never reconstructs authorization or Connection health itself.
    """

    agent_runnable: bool
    checked_at: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityDriftReport:
    source: LegacyCapabilitySource
    catalog_fingerprint: str
    capabilities: tuple[CompiledCapability, ...]
    inferred_capability_ids: tuple[str, ...]
    listing_without_evidence: tuple[str, ...]
    dependency_without_listing: tuple[str, ...]
    unverified_outputs: tuple[str, ...]
    evidence_fingerprint: str

    def to_consumer_dict(
        self,
        *,
        locale: str = "en-US",
        readiness: CapabilityReadinessContext | None = None,
        include_management: bool = False,
    ) -> dict[str, Any]:
        selected_locale = locale if locale in SUPPORTED_CAPABILITY_LOCALES else "en-US"
        capability_rows: list[dict[str, Any]] = []
        for item in self.capabilities:
            definition = item.definition
            implementation = item.implementation
            implementation_status = implementation.review_state if implementation is not None else "missing"
            implementation_evidence = {
                "status": implementation_status,
                "reviewed_at": (
                    implementation.reviewed_at.isoformat() if implementation is not None and implementation.reviewed_at is not None else None
                ),
                "evidence_level": ("catalog_reviewed" if implementation_status == "reviewed" else "derived_unverified"),
            }
            capability_readiness = _capability_readiness(
                item,
                readiness=readiness,
            )
            if definition is None:
                row: dict[str, Any] = {
                    "id": item.capability_id,
                    "definition_version": None,
                    "definition_status": "unknown",
                    "definition": {"lifecycle": "unknown", "version": None},
                    "implementation_evidence": implementation_evidence,
                    "readiness": capability_readiness,
                    "verification_status": "derived_unverified",
                    "availability": "unknown",
                    "evidence_reasons": list(item.evidence_reasons),
                }
                if include_management:
                    row["management"] = _management_projection(self.source, implementation)
                capability_rows.append(row)
                continue
            listing = definition.listing[selected_locale]
            row = {
                "id": definition.id,
                "definition_version": definition.version,
                "compatibility_major": definition.compatibility_major,
                "definition_status": definition.lifecycle,
                "definition": {"lifecycle": definition.lifecycle, "version": definition.version},
                "category": definition.category,
                "listing_revision": definition.listing_revision,
                "name": listing.name,
                "description": listing.description,
                "task_examples": list(listing.task_examples),
                "parameters_schema": definition.parameters_schema,
                "inputs": [{"contract": value.exact_ref, "required": value.required} for value in definition.inputs],
                "outputs": [{"contract": value.exact_ref, "required": value.required} for value in definition.outputs],
                "implementation_evidence": implementation_evidence,
                "readiness": capability_readiness,
                "verification_status": "derived_unverified",
                "availability": item.availability,
                "evidence_reasons": list(item.evidence_reasons),
            }
            if include_management:
                row["management"] = _management_projection(self.source, implementation)
            capability_rows.append(row)
        listing_status = "drift" if self.listing_without_evidence or self.dependency_without_listing else "aligned"
        prompt_status = "drift" if self.source.prompt_protocols else "clear"
        overlay_status = "drift" if self.source.implicit_team_tool_overlay else "clear"
        output_status = "drift" if self.unverified_outputs else "clear"
        capability_dependency_drift = bool(self.listing_without_evidence or self.dependency_without_listing)
        capability_dependency_reasons = []
        if self.listing_without_evidence:
            capability_dependency_reasons.append("capability_dependency_evidence_missing")
        if self.dependency_without_listing:
            capability_dependency_reasons.append("dependency_implies_unlisted_capability")
        return {
            "kind": "agent_capability_report",
            "schema_version": AGENT_CAPABILITY_REPORT_SCHEMA_VERSION,
            "compiler_version": CAPABILITY_CATALOG_COMPILER_VERSION,
            "catalog_fingerprint": self.catalog_fingerprint,
            "locale": selected_locale,
            "agent": {
                "id": self.source.agent_id,
                "source_kind": self.source.source_kind,
                "source_version": self.source.source_version,
                "runtime_profile": "internal" if self.source.internal_runtime_profile else "marketplace",
            },
            "verification_status": "derived_unverified",
            "capabilities": capability_rows,
            "legacy_direct_dependencies": {
                "status": "derived_unverified",
                "tool_count": len(self.source.tool_refs),
                "skill_count": len(self.source.skill_refs),
            },
            "drift": {
                "status": (
                    "review_required"
                    if any(status == "drift" for status in (listing_status, prompt_status, overlay_status, output_status))
                    else "legacy_unverified"
                ),
                "listing_vs_manifest": {
                    "status": listing_status,
                    "listing_without_evidence": list(self.listing_without_evidence),
                    "dependency_without_listing": list(self.dependency_without_listing),
                },
                "capability_vs_dependency": {
                    "status": "drift" if capability_dependency_drift else "aligned",
                    "reason_codes": capability_dependency_reasons,
                },
                "prompt_duplication": {
                    "status": prompt_status,
                    "reason_codes": list(self.source.prompt_protocols),
                },
                "implicit_team_tool_overlay": {
                    "status": overlay_status,
                    "reason_codes": (["implicit_team_tool_overlay"] if self.source.implicit_team_tool_overlay else []),
                },
                "unverified_outputs": {
                    "status": output_status,
                    "formats": list(self.unverified_outputs),
                },
            },
        }


@lru_cache(maxsize=1)
def default_catalog() -> CapabilityCatalog | None:
    """进程级 catalog（唯一加载点）。加载失败返 None —— 能力目录不该阻塞 turn 或 boot。

    WP M3-12 引入：此前每个消费方各自 `CapabilityCatalog.bootstrap()`，而 manifest
    字段校验会对 9 份 manifest 各触发一次全量 YAML 读盘。调用方拿到 None 时按
    「判不出能力」处理（岗位解析→无承担者话术；manifest 校验→跳过值域检查），
    而不是抛异常。
    """
    try:
        return CapabilityCatalog.bootstrap()
    except Exception:
        logger.warning("capability_catalog.bootstrap_failed", exc_info=True)
        return None


def reset_default_catalog_cache_for_test() -> None:
    default_catalog.cache_clear()


def infer_capabilities_from_tools(
    tool_ids: Collection[str],
    *,
    catalog: CapabilityCatalog,
    listing_capability_ids: Collection[str] = (),
) -> tuple[tuple[str, str], ...]:
    """工具名集合 → ``((capability_id, reason), ...)``，按 capability_id 稳定排序。

    这是「这个 Agent 具备哪些能力」的**唯一**判定实现，两个消费方：

    - ``CapabilityDriftCompiler.compile``：legacy 声明 vs catalog 的只读对账；
    - ``agents/capability_roles.py``：Lead 提示词的岗位承担者解析（WP M3-12）。

    两条保守推断规则：

    1. 无 exact Skill 依赖且 ``required_tools ⊆ tools`` 的 active Implementation → ``legacy_dependency_match``。
       ``required_tools`` 为空的 Implementation（如 ``capi_analysis_reasoning``）**只在**
       调用方显式声明了该 capability 时才算命中 —— 否则任何 Agent 都会凭空拿到它。
       岗位解析传空 ``listing_capability_ids``，所以走的是保守那一侧。
    2. ``_LEGACY_TOOL_CAPABILITY_HINTS`` 的工具类推断 → ``legacy_tool_class_inference``。

    ⚠ 返回的是**声明面**推断，不是执行授权：Implementation 的执行面在 M3-A 之后仍是
    shadow。调用方只可拿它做路由/对账，不可当成「能跑」的凭据。
    """
    tools = set(tool_ids)
    listing = set(listing_capability_ids)
    # ⚠ A capability can match both rules; retain both reasons. The drift report's
    # `evidence_reasons` uses them to assess evidence strength; deduplicating to one changes report output.
    found: dict[str, set[str]] = {}
    for definition in catalog.definitions:
        for implementation in catalog.implementations_for(definition):
            # A generic Skill dispatcher proves no particular Skill binding.
            # This legacy projection has no exact Skill grants to check here.
            if implementation.required_skills or implementation.lifecycle != "active":
                continue
            if set(implementation.required_tools).issubset(tools):
                # Empty dependency implementations are valid, but legacy Agents
                # still need an explicit listing claim before we infer semantics.
                if implementation.required_tools or definition.id in listing:
                    found.setdefault(definition.id, set()).add("legacy_dependency_match")
    for capability_id, tool_hints in _LEGACY_TOOL_CAPABILITY_HINTS.items():
        if tools.intersection(tool_hints):
            found.setdefault(capability_id, set()).add("legacy_tool_class_inference")
    # Built-in Power BI connections carry a stable instance ID in the exact Tool ref.
    # The old native pbi_* names no longer grant access or routing evidence.
    powerbi_operations = {operation for ref in tools if (operation := builtin_powerbi_operation(ref)) is not None}
    if powerbi_operations.intersection({"pbi_execute_dax", "pbi_fetch_rows", "pbi_execute_template"}):
        found.setdefault("cap_data_query", set()).add("builtin_powerbi_tool_inference")
    if powerbi_operations.intersection({"pbi_list_measures", "pbi_list_templates"}):
        found.setdefault("cap_data_discovery", set()).add("builtin_powerbi_tool_inference")
    return tuple((capability_id, reason) for capability_id in sorted(found) for reason in sorted(found[capability_id]))


class CapabilityDriftCompiler:
    """Compare current Agent claims with the catalog without changing execution."""

    def __init__(self, catalog: CapabilityCatalog) -> None:
        self.catalog = catalog

    def compile(self, source: LegacyCapabilitySource) -> CapabilityDriftReport:
        listing_ids = set(source.listing_capability_ids)
        tool_ids = set(source.tool_refs)
        inferred_reasons: dict[str, set[str]] = {}

        # WP M3-12: extract the two **tool-based** inference rules into module-level `infer_capabilities_from_tools`,
        # sharing the implementation with `agents/capability_roles.py` (Lead role resolution).
        # Separate implementations inevitably drift: the roster claims data retrieval while the prompt says no one can retrieve data.
        # The output / name heuristics below remain here: they read legacy declarations
        # (`produces` and literal tool names), serving as drift-reconciliation evidence rather than runtime role criteria.
        for capability_id, reason in infer_capabilities_from_tools(
            tool_ids,
            catalog=self.catalog,
            listing_capability_ids=listing_ids,
        ):
            inferred_reasons.setdefault(capability_id, set()).add(reason)

        output_capabilities = {
            "pptx": "cap_presentation",
            "docx": "cap_document",
            "pdf": "cap_document",
            "xlsx": "cap_spreadsheet",
        }
        unverified_outputs = set(source.declared_outputs)
        for output in source.declared_outputs:
            capability_id = output_capabilities.get(output.casefold())
            if capability_id:
                inferred_reasons.setdefault(capability_id, set()).add("legacy_output_declaration")

        for raw_ref in (*source.tool_refs, *source.skill_refs):
            normalized = raw_ref.casefold()
            for tokens, capability_id, output_format in (
                (("ppt", "slide", "deck", "presentation"), "cap_presentation", "pptx"),
                (("word", "docx", "document"), "cap_document", "docx"),
                (("xlsx", "excel", "spreadsheet"), "cap_spreadsheet", "xlsx"),
            ):
                if any(token in normalized for token in tokens):
                    inferred_reasons.setdefault(capability_id, set()).add("legacy_name_inference")
                    unverified_outputs.add(output_format)

        inferred_ids = set(inferred_reasons)
        capability_rows: list[CompiledCapability] = []
        for capability_id in sorted(listing_ids):
            definition = self.catalog.latest_definition(capability_id)
            implementation = _best_evidence_implementation(
                self.catalog,
                definition,
                source=source,
            )
            reasons = set(inferred_reasons.get(capability_id, ()))
            reasons.add("no_exact_capability_binding")
            if not inferred_reasons.get(capability_id):
                reasons.add("listing_only")
            capability_rows.append(
                CompiledCapability(
                    capability_id=capability_id,
                    definition=definition,
                    implementation=implementation,
                    availability="degraded" if capability_id in inferred_ids else "unknown",
                    evidence_reasons=tuple(sorted(reasons)),
                ),
            )

        report_payload = {
            "compiler_version": CAPABILITY_CATALOG_COMPILER_VERSION,
            "catalog_fingerprint": self.catalog.fingerprint,
            "source": {
                "agent_id": source.agent_id,
                "source_kind": source.source_kind,
                "source_version": source.source_version,
                "listing_capability_ids": sorted(listing_ids),
                "tool_refs": sorted(source.tool_refs),
                "skill_refs": sorted(source.skill_refs),
                "declared_outputs": sorted(source.declared_outputs),
                "prompt_protocols": sorted(source.prompt_protocols),
                "prompt_fingerprint": source.prompt_fingerprint,
                "implicit_team_tool_overlay": source.implicit_team_tool_overlay,
                "internal_runtime_profile": source.internal_runtime_profile,
            },
            "inferred": sorted(inferred_ids),
        }
        return CapabilityDriftReport(
            source=source,
            catalog_fingerprint=self.catalog.fingerprint,
            capabilities=tuple(capability_rows),
            inferred_capability_ids=tuple(sorted(inferred_ids)),
            listing_without_evidence=tuple(sorted(listing_ids - inferred_ids)),
            dependency_without_listing=tuple(sorted(inferred_ids - listing_ids)),
            unverified_outputs=tuple(sorted(unverified_outputs)),
            evidence_fingerprint=_sha256(report_payload),
        )

    def compile_system(
        self,
        *,
        manifest: AgentManifest,
        listing_capability_ids: tuple[str, ...],
        prompt_text: str = "",
        override: BuiltinSubagentOverride | None = None,
    ) -> CapabilityDriftReport:
        from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
        from bowhead_core.builtin_subagent_override import apply_to_profile, effective_skill_names

        profile = apply_to_profile(manifest_to_profile(manifest, prompt_loader=lambda _: prompt_text), override)
        prompt_bundle = profile.system_prompt
        skill_refs = tuple(dict.fromkeys((*manifest.skills.framework, *manifest.skills.sop_lazy, *effective_skill_names(override))))
        return self.compile(
            LegacyCapabilitySource(
                agent_id=f"system:{manifest.name}",
                source_kind="system",
                source_version=f"{manifest.version}:override:{override.row_version}" if override is not None else str(manifest.version),
                listing_capability_ids=listing_capability_ids,
                tool_refs=tuple(profile.tool_whitelist),
                skill_refs=skill_refs,
                declared_outputs=tuple(manifest.produces),
                prompt_protocols=detect_prompt_protocols(prompt_bundle),
                prompt_fingerprint=capability_prompt_fingerprint(prompt_bundle),
                implicit_team_tool_overlay=manifest.name != "lead",
                internal_runtime_profile=manifest.name == "lead",
            ),
        )


def detect_prompt_protocols(prompt_text: str) -> tuple[str, ...]:
    """Return bounded protocol codes, never prompt excerpts or dependency names."""
    normalized = prompt_text.casefold()
    patterns = {
        "artifact_export_protocol_in_agent_prompt": (
            'skill_invoke(name="canvas-',
            "skill_invoke(name='canvas-",
        ),
        "sandbox_protocol_in_agent_prompt": (
            'skill_invoke(name="python_sandbox"',
            "skill_invoke(name='python_sandbox'",
        ),
        "report_protocol_in_agent_prompt": ("write_report(", "read_report("),
    }
    return tuple(
        sorted(code for code, markers in patterns.items() if any(marker in normalized for marker in markers)),
    )


def capability_prompt_fingerprint(prompt_text: str) -> str:
    """Bind internal evidence to prompt content without returning the content."""
    return _sha256({"prompt": prompt_text})


def _sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _best_evidence_implementation(
    catalog: CapabilityCatalog,
    definition: CapabilityDefinition | None,
    *,
    source: LegacyCapabilitySource,
) -> CapabilityImplementation | None:
    if definition is None:
        return None
    candidates = catalog.implementations_for(definition)
    if not candidates:
        return None
    from bowhead_core.subagent_authoring_catalog import runtime_tool_ref

    # Same names as the missing-dependency report below, or the two disagree on which implementation applies.
    tool_refs = {runtime_tool_ref(value) for value in source.tool_refs}
    skill_refs = set(source.skill_refs)

    def missing_count(implementation: CapabilityImplementation) -> int:
        missing_tools = sum(runtime_tool_ref(value) not in tool_refs for value in implementation.required_tools)
        missing_skills = sum(
            requirement.ref not in skill_refs and _skill_name_from_ref(requirement.ref) not in skill_refs
            for requirement in implementation.required_skills
        )
        return missing_tools + missing_skills

    return min(enumerate(candidates), key=lambda value: (missing_count(value[1]), value[0]))[1]


def _capability_readiness(
    item: CompiledCapability,
    *,
    readiness: CapabilityReadinessContext | None,
) -> dict[str, Any]:
    if readiness is None:
        return {
            "state": "unknown",
            "checked_at": None,
            "reason_codes": ["actor_readiness_not_evaluated"],
        }
    definition = item.definition
    implementation = item.implementation
    if definition is None:
        state = "unknown"
        reasons = ["definition_unknown"]
    elif definition.lifecycle == "retired":
        state = "unavailable"
        reasons = ["definition_retired"]
    elif not readiness.agent_runnable:
        state = "unavailable"
        reasons = list(readiness.reason_codes or ("agent_not_runnable",))
    elif implementation is None:
        state = "unknown"
        reasons = ["implementation_evidence_missing"]
    elif implementation.review_state != "reviewed":
        state = "unknown"
        reasons = ["implementation_not_reviewed"]
    elif "no_exact_capability_binding" in item.evidence_reasons:
        state = "unknown"
        reasons = ["no_exact_capability_binding"]
    elif definition.lifecycle == "deprecated" or implementation.lifecycle == "deprecated":
        state = "degraded"
        reasons = ["capability_or_implementation_deprecated"]
    else:
        state = "ready"
        reasons = []
    return {
        "state": state,
        "checked_at": readiness.checked_at,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _management_projection(
    source: LegacyCapabilitySource,
    implementation: CapabilityImplementation | None,
) -> dict[str, Any]:
    if implementation is None:
        return {
            "implementation_ref": None,
            "implementation_lifecycle": None,
            "missing_dependencies": [],
            "unresolved_dependencies": [],
        }
    from bowhead_core.subagent_authoring_catalog import runtime_tool_ref

    # Compare the names the runtime uses: a contract declaring ``python_exec`` is met by ``python_sandbox``.
    tool_refs = {runtime_tool_ref(value) for value in source.tool_refs}
    skill_refs = set(source.skill_refs)
    # Reported under the name the tools page lists, so the repair link finds it.
    missing: list[dict[str, str]] = [
        {"kind": "tool", "ref": runtime_tool_ref(value)} for value in implementation.required_tools if runtime_tool_ref(value) not in tool_refs
    ]
    unresolved: list[dict[str, str]] = []
    for requirement in implementation.required_skills:
        if requirement.ref in skill_refs:
            continue
        skill_name = _skill_name_from_ref(requirement.ref)
        if skill_name in skill_refs:
            unresolved.append(
                {
                    "kind": "skill",
                    "ref": requirement.ref,
                    "reason_code": "skill_version_not_exact",
                },
            )
        else:
            missing.append({"kind": "skill", "ref": requirement.ref})
    unresolved.extend(
        {
            "kind": "connection",
            "ref": value,
            "reason_code": "connection_readiness_not_evaluated",
        }
        for value in implementation.required_connections
    )
    return {
        "implementation_ref": implementation.exact_ref,
        "implementation_lifecycle": implementation.lifecycle,
        "missing_dependencies": missing,
        "unresolved_dependencies": unresolved,
    }


def _skill_name_from_ref(value: str) -> str:
    return value.removeprefix("skill://").split("@", 1)[0]


__all__ = [
    "AGENT_CAPABILITY_REPORT_SCHEMA_VERSION",
    "CAPABILITY_CATALOG_COMPILER_VERSION",
    "CapabilityCatalog",
    "CapabilityDefinition",
    "CapabilityDriftCompiler",
    "CapabilityDriftReport",
    "CapabilityImplementation",
    "CapabilityReadinessContext",
    "LegacyCapabilitySource",
    "SUPPORTED_CAPABILITY_LOCALES",
    "capability_prompt_fingerprint",
    "default_catalog",
    "detect_prompt_protocols",
    "latest_active_capability_ref",
]
