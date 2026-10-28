"""Compile exact dependency and Capability plans into one behavior descriptor.

This is a pure facade over existing typed compilers.  It does not infer a
Capability from names, grant dependencies, persist rows, or alter routing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from bowhead_core.agent_behavior_contract import (
    AGENT_BEHAVIOR_COMPILER_VERSION,
    AGENT_BEHAVIOR_SCHEMA_VERSION,
    AgentBehaviorDescriptor,
    BehaviorPromptModuleRef,
    canonical_fingerprint,
    prompt_module_fingerprint,
)
from bowhead_core.agent_capability_catalog import CapabilityCatalog, CapabilityImplementation
from bowhead_core.agent_capability_composition import CapabilityPlanCompilation, ImplementationExecutionPlan
from bowhead_core.agent_dependency_compiler import AgentDependencyCompilation

_CAPABILITY_EXACT_REF_PATTERN = r"^cap_[a-z][a-z0-9_]{1,63}@[1-9][0-9]*$"


class AgentBehaviorCompiler:
    """Compose existing compiler outputs without creating new inference rules."""

    def __init__(self, catalog: CapabilityCatalog) -> None:
        self._catalog = catalog

    def compile(
        self,
        *,
        source_definition_hash: str,
        dependency: AgentDependencyCompilation,
        compositions: Sequence[CapabilityPlanCompilation],
        primary_capability_ref: str | None,
        effective_model_role: str,
    ) -> AgentBehaviorDescriptor:
        reasons = {f"dependency:{code}" for code in dependency.reason_codes}
        hard_blocked = dependency.status != "ready"
        if hard_blocked:
            reasons.add(f"dependency_status:{dependency.status}")
        shadow_only = False
        plans: dict[str, ImplementationExecutionPlan] = {}

        if not compositions:
            reasons.add("behavior_composition_missing")
            shadow_only = True
        for composition in compositions:
            reasons.update(f"composition:{code}" for code in composition.reason_codes)
            if composition.status == "blocked":
                if not composition.reason_codes:
                    reasons.add("composition_blocked")
                if composition.reason_codes and all(code.startswith("no_reviewed_implementation:") for code in composition.reason_codes):
                    shadow_only = True
                else:
                    hard_blocked = True
            for binding in composition.bindings:
                for plan in (binding.primary, *binding.fallbacks):
                    plans[plan.implementation_ref] = plan

        implementations: dict[str, CapabilityImplementation] = {}
        for exact_ref in sorted(plans):
            implementation_id, version = _split_exact_ref(exact_ref)
            try:
                implementation = self._catalog.get_implementation(implementation_id, version)
            except KeyError:
                reasons.add(f"implementation_unknown:{exact_ref}")
                hard_blocked = True
                continue
            implementations[exact_ref] = implementation
            if implementation.lifecycle != "active":
                reasons.add(f"implementation_inactive:{exact_ref}")
                hard_blocked = True
            if implementation.review_state != "reviewed":
                reasons.add(f"implementation_not_reviewed:{exact_ref}")
                shadow_only = True

        effective_capability_refs = tuple(
            sorted({implementation.capability.exact_ref for implementation in implementations.values()}),
        )
        eligible_lead_role_ids = tuple(
            sorted(
                {
                    role
                    for implementation in implementations.values()
                    if implementation.lifecycle == "active" and implementation.review_state == "reviewed"
                    for role in implementation.lead_role_ids
                },
            ),
        )
        descriptor_primary_capability_ref = primary_capability_ref
        if primary_capability_ref is not None and re.fullmatch(_CAPABILITY_EXACT_REF_PATTERN, primary_capability_ref) is None:
            reasons.add("primary_capability_malformed")
            hard_blocked = True
            descriptor_primary_capability_ref = None
        elif primary_capability_ref is not None:
            primary_id, primary_version = _split_exact_ref(primary_capability_ref)
            try:
                primary_definition = self._catalog.get_definition(primary_id, primary_version)
            except KeyError:
                reasons.add(f"primary_capability_unknown:{primary_capability_ref}")
                hard_blocked = True
                descriptor_primary_capability_ref = None
            else:
                if primary_definition.lifecycle != "active":
                    reasons.add(f"primary_capability_inactive:{primary_capability_ref}")
                    hard_blocked = True
                    descriptor_primary_capability_ref = None
                primary_implementations = (
                    implementation
                    for implementation in implementations.values()
                    if implementation.capability.exact_ref == primary_capability_ref
                    and implementation.lifecycle == "active"
                    and implementation.review_state == "reviewed"
                )
                if primary_capability_ref not in effective_capability_refs or not any(
                    implementation.lead_role_ids for implementation in primary_implementations
                ):
                    reasons.add(f"primary_capability_unproven:{primary_capability_ref}")
                    hard_blocked = True
                    descriptor_primary_capability_ref = None

        output_contract_refs: set[str] = set()
        adapter_refs: set[str] = set()
        prompt_modules: dict[tuple[str, int], BehaviorPromptModuleRef] = {}
        prompt_module_conflicts: set[tuple[str, int]] = set()
        for exact_ref, implementation in implementations.items():
            definition = self._catalog.get_definition(
                implementation.capability.id,
                implementation.capability.version,
            )
            output_contract_refs.update(output.exact_ref for output in definition.outputs)
            plan = plans[exact_ref]
            if plan.artifact_adapter_ref is not None:
                adapter_refs.add(plan.artifact_adapter_ref)
            for prompt in implementation.prompt_modules:
                ref = BehaviorPromptModuleRef(id=prompt.id, version=prompt.version, sha256=prompt.sha256)
                key = (ref.id, ref.version)
                current = prompt_modules.get(key)
                if current is not None and current.sha256 != ref.sha256:
                    reasons.add(f"prompt_module_identity_conflict:{ref.exact_ref}")
                    prompt_modules.pop(key)
                    prompt_module_conflicts.add(key)
                    hard_blocked = True
                elif key not in prompt_module_conflicts:
                    prompt_modules[key] = ref

        prompt_module_refs = tuple(prompt_modules[key] for key in sorted(prompt_modules))
        implementation_refs = tuple(sorted(implementations))
        capability_fingerprint = canonical_fingerprint(
            {
                "catalog_fingerprint": self._catalog.fingerprint,
                "composition_fingerprints": sorted({item.fingerprint for item in compositions}),
                "effective_capability_refs": effective_capability_refs,
                "implementation_refs": implementation_refs,
            },
        )
        if hard_blocked:
            verification_state = "blocked"
        elif shadow_only or not implementation_refs:
            verification_state = "shadow"
        else:
            verification_state = "reviewed"

        payload = {
            "schema_version": AGENT_BEHAVIOR_SCHEMA_VERSION,
            "compiler_version": AGENT_BEHAVIOR_COMPILER_VERSION,
            "source_definition_hash": source_definition_hash,
            "primary_capability_ref": descriptor_primary_capability_ref,
            "effective_capability_refs": effective_capability_refs,
            "implementation_refs": implementation_refs,
            "eligible_lead_role_ids": eligible_lead_role_ids,
            "output_contract_refs": tuple(sorted(output_contract_refs)),
            "adapter_refs": tuple(sorted(adapter_refs)),
            "prompt_module_refs": tuple(item.model_dump(mode="json") for item in prompt_module_refs),
            "prompt_module_fingerprint": prompt_module_fingerprint(prompt_module_refs),
            "dependency_fingerprint": dependency.dependency_fingerprint,
            "capability_fingerprint": capability_fingerprint,
            "effective_model_role": effective_model_role,
            "verification_state": verification_state,
            "reason_codes": tuple(sorted(reasons)),
        }
        return AgentBehaviorDescriptor.model_validate(
            {
                **payload,
                "behavior_fingerprint": canonical_fingerprint(payload),
            },
        )


def _split_exact_ref(value: str) -> tuple[str, int]:
    try:
        item_id, raw_version = value.rsplit("@", 1)
        version = int(raw_version)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid exact ref: {value}") from exc
    if not item_id or version < 1:
        raise ValueError(f"invalid exact ref: {value}")
    return item_id, version
