"""Exact baseline Capability selection for Composer references.

Only implementations with reviewed executable evidence may enter this adapter.
The first supported vertical slice is the existing native image implementation;
other Capability definitions remain discoverable but unavailable.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, canonical_fingerprint
from bowhead_core.agent_capability_catalog import CapabilityDefinition, CapabilityImplementation, default_catalog
from bowhead_core.agent_capability_execution import CapabilityExecutionSelection, ExactCapabilityExecution, apply_capability_execution_conn
from bowhead_core.chat_references import CapabilityResourceRef, InputDocument, ReferenceNode

if TYPE_CHECKING:
    from bowhead_core.chat_reference_execution import ReferencePrimaryInput

CAPABILITY_ADAPTER_ID = "initial-capability"
CAPABILITY_ADAPTER_VERSION = 1
SUPPORTED_IMPLEMENTATIONS = frozenset({"capi_image_generation_native@1"})


@dataclass(frozen=True, slots=True)
class PreparedReferenceCapability:
    node_id: str
    resource: CapabilityResourceRef
    implementation_ref: str
    implementation_digest: str
    output_contract: str
    policy_digest: str


def capability_supported_by_behavior(
    behavior: AgentBehaviorDescriptor | None,
    resource: CapabilityResourceRef,
) -> bool:
    try:
        _resolve_implementation(behavior, resource)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def prepare_reference_capability(
    document: InputDocument,
    behavior: AgentBehaviorDescriptor | None,
) -> PreparedReferenceCapability | None:
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, CapabilityResourceRef)]
    if not nodes:
        return None
    if len(nodes) != 1:
        raise ValueError("reference_capability_selection_invalid")
    node = nodes[0]
    definition, implementation = _resolve_implementation(behavior, node.resource)
    _validate_parameters(definition, node.parameters)
    output_contracts = tuple(item.exact_ref for item in definition.outputs if item.required)
    if output_contracts != ("visual.artifact.v1",):
        raise ValueError("reference_capability_output_unsupported")
    implementation_digest = canonical_fingerprint(
        implementation.model_dump(mode="json"),
    )
    policy_digest = canonical_fingerprint(
        {
            "contract": "reference-capability-admission.v1",
            "resource": node.resource.model_dump(mode="json"),
            "implementation_ref": implementation.exact_ref,
            "implementation_digest": implementation_digest,
            "behavior_fingerprint": behavior.behavior_fingerprint,
            "output_contract": output_contracts[0],
        },
    )
    return PreparedReferenceCapability(
        node_id=node.node_id,
        resource=node.resource,
        implementation_ref=implementation.exact_ref,
        implementation_digest=implementation_digest,
        output_contract=output_contracts[0],
        policy_digest=policy_digest,
    )


def _resolve_implementation(
    behavior: AgentBehaviorDescriptor | None,
    resource: CapabilityResourceRef,
) -> tuple[CapabilityDefinition, CapabilityImplementation]:
    if behavior is None or behavior.verification_state != "reviewed":
        raise ValueError("reference_capability_behavior_unavailable")
    exact_ref = f"{resource.id}@{resource.version}"
    if exact_ref not in behavior.effective_capability_refs:
        raise ValueError("reference_capability_not_in_baseline")
    catalog = default_catalog()
    if catalog is None:
        raise ValueError("reference_capability_catalog_unavailable")
    catalog.validate_reviewed_evidence()
    definition = catalog.get_definition(resource.id, resource.version)
    if definition.lifecycle != "active" or not definition.market_visible or not definition.authoring_selectable:
        raise ValueError("reference_capability_definition_unavailable")
    implementations = tuple(
        item
        for item in catalog.implementations_for(definition)
        if item.lifecycle == "active"
        and item.review_state == "reviewed"
        and item.exact_ref in behavior.implementation_refs
        and item.exact_ref in SUPPORTED_IMPLEMENTATIONS
    )
    if len(implementations) != 1:
        raise ValueError("reference_capability_implementation_unavailable")
    implementation = implementations[0]
    if (
        implementation.effect_class != "internal_artifact_write"
        or implementation.risk_floor != "internal_artifact_write"
        or implementation.required_tools != ("image_generate",)
        or implementation.required_connections
        or implementation.required_skills
    ):
        raise ValueError("reference_capability_implementation_unsupported")
    if not {item.exact_ref for item in definition.outputs} <= set(
        behavior.output_contract_refs,
    ):
        raise ValueError("reference_capability_output_unavailable")
    return definition, implementation


def _validate_parameters(
    definition: CapabilityDefinition,
    parameters: dict,
) -> None:
    schema = definition.parameters_schema
    # The first implementation intentionally has a closed empty schema. Add a
    # real schema validator with the next admitted Capability, not an incomplete
    # subset that could accept unsupported values.
    if (
        schema
        != {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        }
        or parameters
    ):
        raise ValueError("reference_capability_parameters_invalid")


def execution_selection(
    prepared: PreparedReferenceCapability,
    *,
    generation: int,
) -> CapabilityExecutionSelection:
    capability_id, capability_version = prepared.resource.id, int(prepared.resource.version)
    implementation_id, separator, version = prepared.implementation_ref.rpartition("@")
    if not separator:
        raise ValueError("reference_capability_implementation_invalid")
    return CapabilityExecutionSelection(
        composition_node_id=prepared.node_id,
        attempt_generation=generation,
        capability_id=capability_id,
        capability_version=capability_version,
        implementation_id=implementation_id,
        implementation_version=int(version),
        adapter_id=None,
        prompt_module_fingerprint="sha256:" + hashlib.sha256(b"").hexdigest(),
        dependency_fingerprint=prepared.implementation_digest,
        input_artifact_refs=(),
        output_contract=prepared.output_contract,
    )


def _prepared_from_primary(primary: ReferencePrimaryInput) -> PreparedReferenceCapability | None:
    document = primary.source.input_document
    if document is None:
        raise ValueError("reference_capability_source_unavailable")
    prepared = prepare_reference_capability(document, primary.binding.behavior_descriptor)
    if prepared is None:
        return None
    admission = next(
        (
            item
            for item in primary.plan.admissions
            if item.node_id == prepared.node_id and item.resource == prepared.resource and item.operation == "execute"
        ),
        None,
    )
    if admission is None or admission.policy_digest != prepared.policy_digest:
        raise ValueError("reference_capability_admission_changed")
    return prepared


async def start_reference_capability_execution_in_connection(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    generation: int,
) -> ExactCapabilityExecution | None:
    """Fix and start the exact reviewed implementation before runner effects."""
    prepared = _prepared_from_primary(primary)
    if prepared is None:
        return None
    if os.environ.get("FEATURE_CHAT_REFERENCE_CAPABILITY", "0") != "1" and primary.plan.orchestration_contract != "lead-delegation.v1":
        raise ValueError("reference_capability_runtime_unavailable")
    selection = execution_selection(prepared, generation=generation)
    exact = await apply_capability_execution_conn(
        db,
        agent_execution_id=primary.binding.execution_id,
        expected_agent_capability_fingerprint=primary.binding.capability_fingerprint,
        selection=selection,
    )
    if primary.plan.orchestration_contract == "lead-delegation.v1" and primary.binding.status == "admitted":
        if exact.status != "selected":
            raise ValueError("reference_capability_execution_changed")
        return exact
    return await activate_reference_capability_execution_in_connection(db, execution=exact)


async def activate_reference_capability_execution_in_connection(
    db: aiosqlite.Connection, *, execution: ExactCapabilityExecution
) -> ExactCapabilityExecution:
    """Start an already validated selection in the Agent dispatch transaction."""
    if execution.status == "selected":
        updated = await db.execute(
            "UPDATE turn_capability_execution SET status='running',started_at=? WHERE id=? AND status='selected'",
            (datetime.now(UTC).isoformat(), execution.execution_id),
        )
        if updated.rowcount != 1:
            raise ValueError("reference_capability_start_conflict")
        execution = replace(execution, status="running")
    if execution.status != "running":
        raise ValueError("reference_capability_execution_unavailable")
    return execution


async def require_reference_capability_execution_in_connection(
    db: aiosqlite.Connection,
    *,
    primary: ReferencePrimaryInput,
    generation: int,
    read_output: bool = False,
) -> ExactCapabilityExecution | None:
    prepared = _prepared_from_primary(primary)
    if prepared is None:
        return None
    if primary.plan.orchestration_contract == "lead-delegation.v1" and generation > 1:
        from bowhead_core.chat_reference_continuation import require_resume

        # A supported clarification continues the same implementation attempt.
        # Validate its exact card/worker fence before reopening that attempt.
        await require_resume(db, turn_id=primary.source.turn_id, generation=generation)
        generation = 1
    selection = execution_selection(prepared, generation=generation)
    previous_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT * FROM turn_capability_execution WHERE agent_execution_id=? AND composition_node_id=? AND attempt_generation=?",
            (primary.binding.execution_id, selection.composition_node_id, generation),
        ) as cursor:
            rows = await cursor.fetchall()
    finally:
        db.row_factory = previous_factory
    if len(rows) != 1:
        raise ValueError("reference_capability_execution_unavailable")
    row = rows[0]
    expected_status = (
        "selected" if primary.plan.orchestration_contract == "lead-delegation.v1" and primary.binding.status == "admitted" else "running"
    )
    if read_output and primary.binding.status in {"completed", "failed"}:
        expected_status = primary.binding.status
    expected = (
        selection.capability_id,
        selection.capability_version,
        selection.implementation_id,
        selection.implementation_version,
        selection.adapter_id,
        selection.prompt_module_fingerprint,
        selection.dependency_fingerprint,
        "[]",
        selection.output_contract,
        expected_status,
    )
    actual = (
        row["capability_id"],
        row["capability_version"],
        row["implementation_id"],
        row["implementation_version"],
        row["adapter_id"],
        row["prompt_module_fingerprint"],
        row["dependency_fingerprint"],
        row["input_artifact_refs_json"],
        row["output_contract"],
        row["status"],
    )
    if actual != expected:
        raise ValueError("reference_capability_execution_changed")
    return ExactCapabilityExecution(
        execution_id=row["id"],
        team_id=row["team_id"],
        turn_id=row["turn_id"],
        agent_execution_id=row["agent_execution_id"],
        composition_node_id=row["composition_node_id"],
        attempt_generation=row["attempt_generation"],
        capability_id=row["capability_id"],
        capability_version=row["capability_version"],
        implementation_id=row["implementation_id"],
        implementation_version=row["implementation_version"],
        adapter_id=row["adapter_id"],
        prompt_module_fingerprint=row["prompt_module_fingerprint"],
        dependency_fingerprint=row["dependency_fingerprint"],
        input_artifact_refs=(),
        output_contract=row["output_contract"],
        status=expected_status,
    )


async def settle_reference_capability_execution_in_connection(
    db: aiosqlite.Connection,
    *,
    execution: ExactCapabilityExecution,
    completed: bool,
    failure_code: str | None = None,
) -> None:
    ended = datetime.now(UTC).isoformat()
    if (completed and failure_code is not None) or (not completed and failure_code is None):
        raise ValueError("reference_capability_settlement_invalid")
    updated = await db.execute(
        "UPDATE turn_capability_execution SET status=?,ended_at=?,failure_code=? WHERE id=? AND status='running'",
        ("completed" if completed else "failed", ended, failure_code, execution.execution_id),
    )
    if updated.rowcount != 1:
        raise ValueError("reference_capability_settlement_conflict")


__all__ = [
    "CAPABILITY_ADAPTER_ID",
    "CAPABILITY_ADAPTER_VERSION",
    "PreparedReferenceCapability",
    "SUPPORTED_IMPLEMENTATIONS",
    "capability_supported_by_behavior",
    "prepare_reference_capability",
    "execution_selection",
    "require_reference_capability_execution_in_connection",
    "settle_reference_capability_execution_in_connection",
    "start_reference_capability_execution_in_connection",
]
