"""Typed Capability composition and exact Implementation plan compilation.

M3-A compiles embedded graphs and Workflow Skills into one normalized graph.
The result is publishable evidence for M3-B; this module performs no routing,
Tool invocation, approval, or Artifact mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_core.agent_artifact_capability import ArtifactCapabilityRegistry
from bowhead_core.agent_capability_catalog import (
    CapabilityCatalog,
    CapabilityContractRef,
    CapabilityImplementation,
    CapabilityRef,
)
from bowhead_core.agent_dependency_compiler import ExactSkillGrant, ExactSkillRuntimeScope

CAPABILITY_COMPOSITION_SCHEMA_VERSION = 1
CAPABILITY_COMPOSITION_COMPILER_VERSION = "agent-capability-composition-v1"

_STABLE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
_EXACT_REVISION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$"
_RISK_ORDER = {
    "read": 0,
    "compute": 1,
    "internal_artifact_write": 2,
    "external_write": 3,
    "destructive": 4,
}
_MAX_GRAPH_NODES = 64
_MAX_GRAPH_EDGES = 128


class CompositionPort(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_STABLE_ID_PATTERN)
    contract: CapabilityContractRef


class CompositionEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_input: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    node: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    graph_output: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    port: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{1,159}$")

    @model_validator(mode="after")
    def _one_endpoint_kind(self) -> CompositionEndpoint:
        selected = sum(value is not None for value in (self.graph_input, self.node, self.graph_output))
        if selected != 1:
            raise ValueError("composition endpoint requires exactly one endpoint kind")
        if self.node is not None and self.port is None:
            raise ValueError("node endpoint requires a typed port")
        if self.node is None and self.port is not None:
            raise ValueError("graph endpoint cannot declare node port")
        return self


class CompositionEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: CompositionEndpoint
    target: CompositionEndpoint
    contract: CapabilityContractRef


class CapabilityNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    kind: Literal["capability"]
    capability: CapabilityRef
    parameters: dict[str, Any] = Field(default_factory=dict)


class DynamicZoneNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    kind: Literal["dynamic_zone"]
    allowed_capabilities: tuple[CapabilityRef, ...] = Field(min_length=1, max_length=16)
    max_steps: int = Field(ge=1, le=16)
    max_cost_units: int = Field(ge=1, le=100000)
    max_duration_seconds: int = Field(ge=1, le=3600)

    @field_validator("allowed_capabilities")
    @classmethod
    def _capabilities_are_unique(cls, value: tuple[CapabilityRef, ...]) -> tuple[CapabilityRef, ...]:
        refs = [item.exact_ref for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate dynamic Capability")
        return value


class ApprovalCheckpointNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    kind: Literal["approval_checkpoint"]
    approval_policy: str = Field(pattern=_STABLE_ID_PATTERN)
    inputs: tuple[CapabilityContractRef, ...] = Field(min_length=1)
    outputs: tuple[CapabilityContractRef, ...] = Field(min_length=1)

    @field_validator("inputs", "outputs")
    @classmethod
    def _contracts_are_unique(
        cls,
        value: tuple[CapabilityContractRef, ...],
    ) -> tuple[CapabilityContractRef, ...]:
        refs = [item.exact_ref for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate approval checkpoint contract")
        return value


CompositionNode = Annotated[
    CapabilityNode | DynamicZoneNode | ApprovalCheckpointNode,
    Field(discriminator="kind"),
]


class CapabilityComposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["fixed", "bounded_dynamic", "hybrid"]
    effect_envelope: Literal[
        "read",
        "compute",
        "internal_artifact_write",
        "external_write",
        "destructive",
    ]
    max_steps: int = Field(ge=1, le=_MAX_GRAPH_NODES)
    max_cost_units: int = Field(ge=1, le=1_000_000)
    max_duration_seconds: int = Field(ge=1, le=86_400)
    inputs: tuple[CompositionPort, ...]
    outputs: tuple[CompositionPort, ...] = Field(min_length=1)
    nodes: tuple[CompositionNode, ...] = Field(min_length=1, max_length=_MAX_GRAPH_NODES)
    edges: tuple[CompositionEdge, ...] = Field(max_length=_MAX_GRAPH_EDGES)

    @model_validator(mode="after")
    def _local_uniqueness_and_mode(self) -> CapabilityComposition:
        for values, label in ((self.inputs, "input"), (self.outputs, "output"), (self.nodes, "node")):
            names = [item.name if isinstance(item, CompositionPort) else item.id for item in values]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate composition {label}")
        edge_keys = [json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) for item in self.edges]
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("duplicate composition edge")
        has_dynamic = any(isinstance(item, DynamicZoneNode) for item in self.nodes)
        if self.mode == "fixed" and has_dynamic:
            raise ValueError("fixed composition cannot contain a dynamic zone")
        if self.mode in {"bounded_dynamic", "hybrid"} and not has_dynamic:
            raise ValueError(f"{self.mode} composition requires a dynamic zone")
        if self.mode == "bounded_dynamic" and any(not isinstance(item, DynamicZoneNode) for item in self.nodes):
            raise ValueError("bounded_dynamic composition can contain only dynamic zones")
        if self.mode == "hybrid" and all(isinstance(item, DynamicZoneNode) for item in self.nodes):
            raise ValueError("hybrid composition requires a fixed node")
        for node in self.nodes:
            if isinstance(node, DynamicZoneNode) and (
                node.max_steps > self.max_steps or node.max_cost_units > self.max_cost_units or node.max_duration_seconds > self.max_duration_seconds
            ):
                raise ValueError(f"dynamic zone exceeds the composition budget: {node.id}")
        planned_steps = sum(node.max_steps if isinstance(node, DynamicZoneNode) else 1 for node in self.nodes)
        if planned_steps > self.max_steps:
            raise ValueError("composition nodes exceed the step budget")
        return self


@dataclass(frozen=True, slots=True)
class ExactToolBinding:
    ref: str
    revision: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"^[A-Za-z][A-Za-z0-9_.:-]{0,199}$", self.ref) is None
            or re.fullmatch(
                _EXACT_REVISION_PATTERN,
                self.revision,
            )
            is None
        ):
            raise ValueError("exact Tool binding is invalid")


@dataclass(frozen=True, slots=True)
class ExactConnectionBinding:
    connection_type: str
    connection_id: str
    revision: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(_STABLE_ID_PATTERN, self.connection_type) is None
            or re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$", self.connection_id) is None
            or re.fullmatch(_EXACT_REVISION_PATTERN, self.revision) is None
        ):
            raise ValueError("exact Connection binding is invalid")


@dataclass(frozen=True, slots=True)
class CapabilityCompilationContext:
    tools: tuple[ExactToolBinding, ...]
    skills: tuple[ExactSkillGrant, ...]
    connections: tuple[ExactConnectionBinding, ...]
    available_checks: tuple[str, ...]
    available_output_grants: tuple[str, ...]
    # ⚠ `_conservative_token_count` counts **UTF-8 bytes**, not tokens.
    # English prose averages 4-5 bytes/token; read this number as bytes: 16384B ≈ 3.5k tokens.
    # The original 4096 was set when the catalog contained only one 956B module; builtin-capability-composition
    # registered three actual data-fetch/delivery/body protocols (about 9.4KB total), so that limit no longer sufficed.
    # This raises the **cap**, not usage: actual prompt volume depends on how many modules each Implementation includes.
    prompt_token_budget: int = 16384

    def __post_init__(self) -> None:
        if self.prompt_token_budget < 1:
            raise ValueError("prompt_token_budget must be positive")
        for values, key in (
            (self.tools, lambda item: item.ref),
            (self.skills, lambda item: item.identity),
            (self.connections, lambda item: item.connection_type),
        ):
            keys = [key(item) for item in values]
            if len(keys) != len(set(keys)):
                raise ValueError("Capability compilation context contains ambiguous exact bindings")
        if len(self.available_checks) != len(set(self.available_checks)) or len(self.available_output_grants) != len(
            set(self.available_output_grants),
        ):
            raise ValueError("Capability compilation context contains duplicate availability evidence")
        if any(re.fullmatch(_STABLE_ID_PATTERN, item) is None for item in self.available_checks) or any(
            re.fullmatch(r"^[a-z][a-z0-9_.-]{1,127}@[1-9][0-9]*$", item) is None for item in self.available_output_grants
        ):
            raise ValueError("Capability compilation availability evidence is invalid")


@dataclass(frozen=True, slots=True)
class ImplementationExecutionPlan:
    implementation_ref: str
    capability_ref: str
    risk_floor: str
    effect_class: str
    tools: tuple[ExactToolBinding, ...]
    optional_tools: tuple[ExactToolBinding, ...]
    skills: tuple[ExactSkillGrant, ...]
    skill_scope: ExactSkillRuntimeScope
    connections: tuple[ExactConnectionBinding, ...]
    artifact_adapter_ref: str | None
    artifact_contract_fingerprint: str | None
    output_grant_ref: str | None
    prompt_bundle_fingerprint: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CapabilityNodeBinding:
    node_id: str
    capability_ref: str
    primary: ImplementationExecutionPlan
    fallbacks: tuple[ImplementationExecutionPlan, ...]


@dataclass(frozen=True, slots=True)
class NormalizedCapabilityGraph:
    graph: CapabilityComposition
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CapabilityCompositionCompilation:
    status: Literal["ready", "blocked"]
    reason_codes: tuple[str, ...]
    normalized_graph: NormalizedCapabilityGraph
    bindings: tuple[CapabilityNodeBinding, ...]
    source_kind: Literal["embedded", "workflow_skill"]
    source_revision: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CapabilitySelectionCompilation:
    """One typed Capability selection without manufacturing a workflow graph."""

    status: Literal["ready", "blocked"]
    reason_codes: tuple[str, ...]
    bindings: tuple[CapabilityNodeBinding, ...]
    source_kind: Literal["single"]
    source_revision: str
    fingerprint: str


CapabilityPlanCompilation = CapabilityCompositionCompilation | CapabilitySelectionCompilation


class CapabilityCompositionCompiler:
    def __init__(
        self,
        *,
        catalog: CapabilityCatalog,
        artifacts: ArtifactCapabilityRegistry,
        prompt_token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._catalog = catalog
        self._artifacts = artifacts
        self._prompt_token_counter = prompt_token_counter or _conservative_token_count
        artifacts.validate_capability_catalog(catalog)

    def compile_embedded(
        self,
        graph: CapabilityComposition | dict[str, Any],
        *,
        revision: str,
        context: CapabilityCompilationContext,
    ) -> CapabilityCompositionCompilation:
        return self._compile(
            graph,
            source_kind="embedded",
            source_revision=revision,
            context=context,
        )

    def compile_workflow_skill(
        self,
        graph: CapabilityComposition | dict[str, Any],
        *,
        workflow_skill: ExactSkillGrant,
        context: CapabilityCompilationContext,
    ) -> CapabilityCompositionCompilation:
        if "read" not in workflow_skill.permissions:
            raise ValueError("Workflow Skill requires read permission")
        if not any(item == workflow_skill for item in context.skills):
            raise ValueError("Workflow Skill is not present in the exact compilation scope")
        return self._compile(
            graph,
            source_kind="workflow_skill",
            source_revision=workflow_skill.coverage_key,
            context=context,
        )

    def compile_single(
        self,
        capability: CapabilityRef | dict[str, Any],
        *,
        revision: str,
        effect_envelope: Literal[
            "read",
            "compute",
            "internal_artifact_write",
            "external_write",
            "destructive",
        ],
        context: CapabilityCompilationContext,
    ) -> CapabilitySelectionCompilation:
        """Select one Capability through the same Implementation compiler as graphs."""
        if not revision or len(revision) > 400:
            raise ValueError("Capability selection source revision is invalid")
        ref = capability if isinstance(capability, CapabilityRef) else CapabilityRef.model_validate(capability)
        plans, reasons = self._plans_for(ref, effect_envelope, context)
        reason_set = set(reasons)
        if any(_RISK_ORDER[plan.risk_floor] >= _RISK_ORDER["external_write"] for plan in plans):
            # A graph can prove that every output path crosses an explicit
            # approval checkpoint. A standalone selection has no such path,
            # so it must remain unavailable until authoring supplies one.
            reason_set.add(f"approval_checkpoint_required:primary:{ref.exact_ref}")
        bindings = (
            (
                CapabilityNodeBinding(
                    node_id="primary",
                    capability_ref=ref.exact_ref,
                    primary=plans[0],
                    fallbacks=plans[1:],
                ),
            )
            if plans
            else ()
        )
        payload = {
            "bindings": [
                {
                    "capability_ref": item.capability_ref,
                    "fallbacks": [plan.fingerprint for plan in item.fallbacks],
                    "node_id": item.node_id,
                    "primary": item.primary.fingerprint,
                }
                for item in bindings
            ],
            "compiler_version": CAPABILITY_COMPOSITION_COMPILER_VERSION,
            "effect_envelope": effect_envelope,
            "selected_capability_ref": ref.exact_ref,
            "source_kind": "single",
            "source_revision": revision,
        }
        return CapabilitySelectionCompilation(
            status="blocked" if reason_set else "ready",
            reason_codes=tuple(sorted(reason_set)),
            bindings=bindings,
            source_kind="single",
            source_revision=revision,
            fingerprint=_hash_json(payload),
        )

    def normalize(self, graph: CapabilityComposition | dict[str, Any]) -> NormalizedCapabilityGraph:
        parsed = graph if isinstance(graph, CapabilityComposition) else CapabilityComposition.model_validate(graph)
        canonical = parsed.model_dump(mode="json")
        canonical["inputs"] = sorted(canonical["inputs"], key=lambda item: item["name"])
        canonical["outputs"] = sorted(canonical["outputs"], key=lambda item: item["name"])
        canonical["nodes"] = sorted(canonical["nodes"], key=lambda item: item["id"])
        for node in canonical["nodes"]:
            if node["kind"] == "dynamic_zone":
                node["allowed_capabilities"] = sorted(
                    node["allowed_capabilities"],
                    key=lambda item: (item["id"], item["version"]),
                )
            elif node["kind"] == "approval_checkpoint":
                node["inputs"] = sorted(
                    node["inputs"],
                    key=lambda item: (item["contract"], item["version"]),
                )
                node["outputs"] = sorted(
                    node["outputs"],
                    key=lambda item: (item["contract"], item["version"]),
                )
        canonical["edges"] = sorted(
            canonical["edges"],
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
        normalized = CapabilityComposition.model_validate(canonical)
        self._validate_graph(normalized)
        return NormalizedCapabilityGraph(graph=normalized, fingerprint=_hash_json(canonical))

    def _compile(
        self,
        graph: CapabilityComposition | dict[str, Any],
        *,
        source_kind: Literal["embedded", "workflow_skill"],
        source_revision: str,
        context: CapabilityCompilationContext,
    ) -> CapabilityCompositionCompilation:
        if not source_revision or len(source_revision) > 400:
            raise ValueError("Capability composition source revision is invalid")
        normalized = self.normalize(graph)
        reasons: set[str] = set()
        bindings: list[CapabilityNodeBinding] = []
        for node in normalized.graph.nodes:
            refs: tuple[CapabilityRef, ...]
            if isinstance(node, CapabilityNode):
                refs = (node.capability,)
            elif isinstance(node, DynamicZoneNode):
                refs = node.allowed_capabilities
            else:
                continue
            for ref in refs:
                plans, plan_reasons = self._plans_for(ref, normalized.graph.effect_envelope, context)
                reasons.update(plan_reasons)
                if plans:
                    if any(_RISK_ORDER[plan.risk_floor] >= _RISK_ORDER["external_write"] for plan in plans) and not _all_output_paths_have_approval(
                        normalized.graph,
                        node.id,
                    ):
                        reasons.add(f"approval_checkpoint_required:{node.id}:{ref.exact_ref}")
                    bindings.append(
                        CapabilityNodeBinding(
                            node_id=node.id,
                            capability_ref=ref.exact_ref,
                            primary=plans[0],
                            fallbacks=plans[1:],
                        ),
                    )
        bindings.sort(key=lambda item: (item.node_id, item.capability_ref))
        payload = {
            "bindings": [
                {
                    "capability_ref": item.capability_ref,
                    "fallbacks": [plan.fingerprint for plan in item.fallbacks],
                    "node_id": item.node_id,
                    "primary": item.primary.fingerprint,
                }
                for item in bindings
            ],
            "compiler_version": CAPABILITY_COMPOSITION_COMPILER_VERSION,
            "graph_fingerprint": normalized.fingerprint,
            "source_kind": source_kind,
            "source_revision": source_revision,
        }
        return CapabilityCompositionCompilation(
            status="blocked" if reasons else "ready",
            reason_codes=tuple(sorted(reasons)),
            normalized_graph=normalized,
            bindings=tuple(bindings),
            source_kind=source_kind,
            source_revision=source_revision,
            fingerprint=_hash_json(payload),
        )

    def _plans_for(
        self,
        ref: CapabilityRef,
        effect_envelope: str,
        context: CapabilityCompilationContext,
    ) -> tuple[tuple[ImplementationExecutionPlan, ...], tuple[str, ...]]:
        definition = self._catalog.get_definition(ref.id, ref.version)
        if definition.lifecycle == "retired":
            return (), (f"capability_retired:{ref.exact_ref}",)
        candidates = self._catalog.implementations_for(definition)
        reviewed = [item for item in candidates if item.review_state == "reviewed" and item.lifecycle == "active"]
        if not reviewed:
            return (), (f"no_reviewed_implementation:{ref.exact_ref}",)
        plans: list[ImplementationExecutionPlan] = []
        rejected: set[str] = set()
        for implementation in reviewed:
            plan, reasons = self._compile_implementation(
                implementation,
                effect_envelope=effect_envelope,
                context=context,
            )
            if plan is not None:
                plans.append(plan)
            rejected.update(reasons)
        if plans:
            primary = plans[0]
            safe_fallbacks = tuple(plan for plan in plans[1:] if _is_safe_fallback(primary, plan))
            return (primary, *safe_fallbacks), ()
        rejected.add(f"no_available_implementation:{ref.exact_ref}")
        return (), tuple(sorted(rejected))

    def _compile_implementation(
        self,
        implementation: CapabilityImplementation,
        *,
        effect_envelope: str,
        context: CapabilityCompilationContext,
    ) -> tuple[ImplementationExecutionPlan | None, tuple[str, ...]]:
        reasons: set[str] = set()
        if _RISK_ORDER[implementation.risk_floor] > _RISK_ORDER[effect_envelope]:
            reasons.add(f"effect_envelope_exceeded:{implementation.exact_ref}")
        tools_by_ref = {item.ref: item for item in context.tools}
        required_tool_refs = set(implementation.required_tools)
        adapter_ref: str | None = None
        artifact_contract_fingerprint: str | None = None
        output_grant_ref: str | None = None
        prompt_fingerprint: str | None = None
        if implementation.artifact is not None:
            self._artifacts.validate_implementation(implementation)
            adapter = self._artifacts.get_adapter(
                implementation.artifact.adapter.id,
                implementation.artifact.adapter.version,
            )
            adapter_ref = adapter.exact_ref
            required_tool_refs.update(tool for operation in adapter.operations for tool in operation.delegate_tools)
            output_grant_ref = implementation.output_grant.exact_ref if implementation.output_grant else None
            if output_grant_ref not in context.available_output_grants:
                reasons.add(f"output_grant_unavailable:{implementation.exact_ref}")
            policy = self._artifacts.get_grant_policy(
                implementation.output_grant.id,
                implementation.output_grant.version,
            )
            artifact_contract_fingerprint = _hash_json(
                {
                    "adapter": adapter.model_dump(mode="json"),
                    "grant_policy": policy.model_dump(mode="json"),
                },
            )
            prompt_bundle = self._artifacts.compile_prompt_bundle(
                implementation.prompt_modules,
                max_tokens=context.prompt_token_budget,
                token_counter=self._prompt_token_counter,
            )
            prompt_fingerprint = prompt_bundle.fingerprint
        missing_tools = required_tool_refs - tools_by_ref.keys()
        if missing_tools:
            reasons.add(f"tool_dependency_unavailable:{implementation.exact_ref}")

        selected_skills: dict[str, ExactSkillGrant] = {}
        selected_permissions: dict[str, set[str]] = {}
        for requirement in implementation.required_skills:
            selected = next(
                (
                    grant
                    for grant in context.skills
                    if f"skill://{grant.name}@{grant.revision}" == requirement.ref and requirement.permission in grant.permissions
                ),
                None,
            )
            if selected is None and not requirement.optional:
                reasons.add(f"skill_dependency_unavailable:{implementation.exact_ref}")
            elif selected is not None:
                selected_skills[selected.identity] = selected
                selected_permissions.setdefault(selected.identity, set()).add(requirement.permission)

        connections_by_type = {item.connection_type: item for item in context.connections}
        if set(implementation.required_connections) - connections_by_type.keys():
            reasons.add(f"connection_dependency_unavailable:{implementation.exact_ref}")
        if set(implementation.availability_checks) - set(context.available_checks):
            reasons.add(f"availability_check_failed:{implementation.exact_ref}")
        if reasons:
            return None, tuple(sorted(reasons))

        skills = tuple(
            replace(grant, permissions=tuple(permission for permission in grant.permissions if permission in selected_permissions[identity]))
            for identity, grant in sorted(selected_skills.items())
        )
        read_names = tuple(sorted({item.name for item in skills if "read" in item.permissions}))
        invoke_names = tuple(sorted({item.name for item in skills if "invoke" in item.permissions}))
        skill_scope = ExactSkillRuntimeScope(read_names=read_names, invoke_names=invoke_names)
        tools = tuple(sorted((tools_by_ref[item] for item in required_tool_refs), key=lambda item: item.ref))
        optional_tools = tuple(
            sorted(
                (tools_by_ref[item] for item in implementation.optional_tools if item in tools_by_ref),
                key=lambda item: item.ref,
            ),
        )
        connections = tuple(
            sorted(
                (connections_by_type[item] for item in implementation.required_connections),
                key=lambda item: item.connection_type,
            ),
        )
        payload = {
            "artifact_adapter_ref": adapter_ref,
            "artifact_contract_fingerprint": artifact_contract_fingerprint,
            "capability_ref": implementation.capability.exact_ref,
            "connections": [asdict(item) for item in connections],
            "effect_class": implementation.effect_class,
            "implementation_ref": implementation.exact_ref,
            "optional_tools": [asdict(item) for item in optional_tools],
            "output_grant_ref": output_grant_ref,
            "prompt_bundle_fingerprint": prompt_fingerprint,
            "risk_floor": implementation.risk_floor,
            "skill_scope": skill_scope.to_dict(),
            "skills": [item.to_canonical_dict() for item in skills],
            "tools": [asdict(item) for item in tools],
        }
        return (
            ImplementationExecutionPlan(
                implementation_ref=implementation.exact_ref,
                capability_ref=implementation.capability.exact_ref,
                risk_floor=implementation.risk_floor,
                effect_class=implementation.effect_class,
                tools=tools,
                optional_tools=optional_tools,
                skills=skills,
                skill_scope=skill_scope,
                connections=connections,
                artifact_adapter_ref=adapter_ref,
                artifact_contract_fingerprint=artifact_contract_fingerprint,
                output_grant_ref=output_grant_ref,
                prompt_bundle_fingerprint=prompt_fingerprint,
                fingerprint=_hash_json(payload),
            ),
            (),
        )

    def _validate_graph(self, graph: CapabilityComposition) -> None:
        inputs = {item.name: item.contract for item in graph.inputs}
        outputs = {item.name: item.contract for item in graph.outputs}
        nodes = {item.id: item for item in graph.nodes}
        node_contracts: dict[str, tuple[dict[str, CapabilityContractRef], dict[str, CapabilityContractRef]]] = {}
        for node in graph.nodes:
            if isinstance(node, CapabilityNode):
                definition = self._catalog.get_definition(node.capability.id, node.capability.version)
                _validate_parameters(node.parameters, definition.parameters_schema, node.id)
                node_contracts[node.id] = (
                    {item.exact_ref: item for item in definition.inputs},
                    {item.exact_ref: item for item in definition.outputs},
                )
            elif isinstance(node, DynamicZoneNode):
                contracts = [self._catalog.get_definition(item.id, item.version) for item in node.allowed_capabilities]
                signatures = [
                    (
                        tuple((item.exact_ref, item.required) for item in definition.inputs),
                        tuple((item.exact_ref, item.required) for item in definition.outputs),
                    )
                    for definition in contracts
                ]
                if len(set(signatures)) != 1:
                    raise ValueError(f"dynamic zone contracts are incompatible: {node.id}")
                node_contracts[node.id] = (
                    {item.exact_ref: item for item in contracts[0].inputs},
                    {item.exact_ref: item for item in contracts[0].outputs},
                )
            else:
                node_contracts[node.id] = (
                    {item.exact_ref: item for item in node.inputs},
                    {item.exact_ref: item for item in node.outputs},
                )

        incoming: dict[tuple[str, str], int] = {}
        graph_input_use: dict[str, int] = {}
        graph_output_use: dict[str, int] = {}
        adjacency: dict[str, set[str]] = {item: set() for item in nodes}
        reverse: dict[str, set[str]] = {item: set() for item in nodes}
        seeded: set[str] = set()
        terminal: set[str] = set()
        for edge in graph.edges:
            contract_ref = edge.contract.exact_ref
            if edge.source.graph_output is not None or edge.target.graph_input is not None:
                raise ValueError("composition edge direction is invalid")
            if edge.source.graph_input is not None:
                contract = inputs.get(edge.source.graph_input)
                if contract is None or contract.exact_ref != contract_ref:
                    raise ValueError("composition input contract mismatch")
                graph_input_use[edge.source.graph_input] = graph_input_use.get(edge.source.graph_input, 0) + 1
            else:
                assert edge.source.node is not None and edge.source.port is not None
                if edge.source.node not in nodes or edge.source.port not in node_contracts[edge.source.node][1]:
                    raise ValueError("composition source port is unknown")
                if edge.source.port != contract_ref:
                    raise ValueError("composition source contract mismatch")
            if edge.target.graph_output is not None:
                contract = outputs.get(edge.target.graph_output)
                if contract is None or contract.exact_ref != contract_ref:
                    raise ValueError("composition output contract mismatch")
                graph_output_use[edge.target.graph_output] = graph_output_use.get(edge.target.graph_output, 0) + 1
                if edge.source.node is not None:
                    terminal.add(edge.source.node)
            else:
                assert edge.target.node is not None and edge.target.port is not None
                if edge.target.node not in nodes or edge.target.port not in node_contracts[edge.target.node][0]:
                    raise ValueError("composition target port is unknown")
                if edge.target.port != contract_ref:
                    raise ValueError("composition target contract mismatch")
                key = (edge.target.node, edge.target.port)
                incoming[key] = incoming.get(key, 0) + 1
                if incoming[key] > 1:
                    raise ValueError("composition input port has multiple producers")
                if edge.source.graph_input is not None:
                    seeded.add(edge.target.node)
                elif edge.source.node is not None:
                    adjacency[edge.source.node].add(edge.target.node)
                    reverse[edge.target.node].add(edge.source.node)

        for name, contract in inputs.items():
            if contract.required and graph_input_use.get(name, 0) == 0:
                raise ValueError(f"required composition input is unused: {name}")
        for name, contract in outputs.items():
            count = graph_output_use.get(name, 0)
            if contract.required and count != 1:
                raise ValueError(f"required composition output must have one producer: {name}")
            if count > 1:
                raise ValueError(f"composition output has multiple producers: {name}")
        for node_id, (node_inputs, _) in node_contracts.items():
            if not any(contract.required for contract in node_inputs.values()):
                seeded.add(node_id)
            for port, contract in node_inputs.items():
                if contract.required and incoming.get((node_id, port), 0) != 1:
                    raise ValueError(f"required node input is not connected: {node_id}:{port}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("composition graph must be acyclic")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in adjacency[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in nodes:
            visit(node_id)

        reachable = set(seeded)
        frontier = list(seeded)
        while frontier:
            source = frontier.pop()
            for target in adjacency[source] - reachable:
                reachable.add(target)
                frontier.append(target)
        if reachable != set(nodes):
            raise ValueError("composition graph contains unreachable nodes")

        productive = set(terminal)
        frontier = list(terminal)
        while frontier:
            target = frontier.pop()
            for source in reverse[target] - productive:
                productive.add(source)
                frontier.append(source)
        if productive != set(nodes):
            raise ValueError("composition graph contains nodes that do not contribute to an output")


def _hash_json(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _all_output_paths_have_approval(graph: CapabilityComposition, source_node_id: str) -> bool:
    nodes = {item.id: item for item in graph.nodes}
    adjacency: dict[str, set[str]] = {item: set() for item in nodes}
    reaches_output: set[str] = set()
    for edge in graph.edges:
        if edge.source.node is None:
            continue
        if edge.target.node is not None:
            adjacency[edge.source.node].add(edge.target.node)
        elif edge.target.graph_output is not None:
            reaches_output.add(edge.source.node)

    memo: dict[tuple[str, bool], bool] = {}

    def every_path_is_approved(node_id: str, approved: bool) -> bool:
        approved = approved or isinstance(nodes[node_id], ApprovalCheckpointNode)
        key = (node_id, approved)
        if key in memo:
            return memo[key]
        if node_id in reaches_output and not approved:
            memo[key] = False
            return False
        result = all(every_path_is_approved(target, approved) for target in adjacency[node_id])
        memo[key] = result
        return result

    return every_path_is_approved(source_node_id, False)


def _is_safe_fallback(primary: ImplementationExecutionPlan, fallback: ImplementationExecutionPlan) -> bool:
    primary_tools = {item.ref for item in (*primary.tools, *primary.optional_tools)}
    fallback_tools = {item.ref for item in (*fallback.tools, *fallback.optional_tools)}
    primary_connections = {(item.connection_type, item.connection_id) for item in primary.connections}
    fallback_connections = {(item.connection_type, item.connection_id) for item in fallback.connections}
    return (
        fallback.effect_class == primary.effect_class
        and _RISK_ORDER[fallback.risk_floor] <= _RISK_ORDER[primary.risk_floor]
        and fallback_tools.issubset(primary_tools)
        and set(fallback.skill_scope.read_names).issubset(primary.skill_scope.read_names)
        and set(fallback.skill_scope.invoke_names).issubset(primary.skill_scope.invoke_names)
        and fallback_connections.issubset(primary_connections)
        and fallback.artifact_adapter_ref == primary.artifact_adapter_ref
        and fallback.output_grant_ref == primary.output_grant_ref
    )


def _conservative_token_count(content: str) -> int:
    """Safe default until M3-B injects the selected model's tokenizer."""
    return len(content.encode("utf-8"))


def _validate_parameters(parameters: dict[str, Any], schema: dict[str, Any], node_id: str) -> None:
    try:
        encoded = json.dumps(
            parameters,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Capability parameters are not JSON-safe: {node_id}") from exc
    if len(encoded.encode()) > 65_536:
        raise ValueError(f"Capability parameters exceed the size limit: {node_id}")
    if not _matches_schema(parameters, schema):
        raise ValueError(f"Capability parameters do not match the Definition schema: {node_id}")


def _matches_schema(value: Any, schema: dict[str, Any]) -> bool:
    supported_keywords = {
        "additionalProperties",
        "const",
        "default",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
    if set(schema) - supported_keywords:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in value for key in required):
            return False
        extra_keys = set(value) - set(properties)
        additional = schema.get("additionalProperties", True)
        if additional is False and extra_keys:
            return False
        if not isinstance(additional, bool) and any(not _matches_schema(value[key], additional) for key in extra_keys):
            return False
        return int(schema.get("minProperties", 0)) <= len(value) <= int(schema.get("maxProperties", len(value))) and all(
            key not in properties or _matches_schema(item, properties[key]) for key, item in value.items()
        )
    if schema_type == "array":
        if not isinstance(value, list):
            return False
        if not int(schema.get("minItems", 0)) <= len(value) <= int(schema.get("maxItems", len(value))):
            return False
        if schema.get("uniqueItems"):
            canonical_items = [json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for item in value]
            if len(canonical_items) != len(set(canonical_items)):
                return False
        return all(_matches_schema(item, schema.get("items", {})) for item in value)
    if schema_type == "string":
        if not isinstance(value, str) or not int(schema.get("minLength", 0)) <= len(value) <= int(
            schema.get("maxLength", len(value)),
        ):
            return False
        try:
            return "pattern" not in schema or re.search(str(schema["pattern"]), value) is not None
        except re.error:
            return False
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and _matches_number_bounds(value, schema)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool) and _matches_number_bounds(value, schema)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return schema_type is None


def _matches_number_bounds(value: int | float, schema: dict[str, Any]) -> bool:
    return (
        ("minimum" not in schema or value >= schema["minimum"])
        and ("maximum" not in schema or value <= schema["maximum"])
        and ("exclusiveMinimum" not in schema or value > schema["exclusiveMinimum"])
        and ("exclusiveMaximum" not in schema or value < schema["exclusiveMaximum"])
    )


__all__ = [
    "CAPABILITY_COMPOSITION_COMPILER_VERSION",
    "ApprovalCheckpointNode",
    "CapabilityCompilationContext",
    "CapabilityComposition",
    "CapabilityCompositionCompilation",
    "CapabilityCompositionCompiler",
    "CapabilityNodeBinding",
    "ExactConnectionBinding",
    "ExactToolBinding",
    "ImplementationExecutionPlan",
    "NormalizedCapabilityGraph",
]
