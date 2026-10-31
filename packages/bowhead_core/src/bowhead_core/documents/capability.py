"""Atomic admission on the original interactive Agent execution."""

import hashlib

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_capability_catalog import default_catalog
from bowhead_core.agent_capability_execution import CapabilityExecutionSelection, apply_capability_execution_conn
from bowhead_core.documents.request import DocumentRequestError


async def admit_capability(db, *, path, runtime, scope, turn_id, inputs, now):
    binding = runtime.binding
    if binding is None or binding.team_id != scope.team_id or binding.turn_id != turn_id:
        raise DocumentRequestError("document_capability_unavailable")
    catalog = default_catalog()
    if catalog is None:
        raise DocumentRequestError("document_capability_unavailable")
    catalog.validate_reviewed_evidence()
    definition = catalog.get_definition("cap_template_document", 1)
    implementation = catalog.get_implementation("capi_template_document_native", 1)
    if definition.lifecycle != "active" or implementation.lifecycle != "active" or implementation.review_state != "reviewed":
        raise DocumentRequestError("document_capability_unavailable")
    tools = binding.definition.get("tool_refs" if binding.agent_source_kind == "team" else "tool_whitelist", [])
    if "template_document_submit" not in tools:
        raise DocumentRequestError("document_capability_unavailable")
    if binding.runtime_profile_name != "lead":
        from bowhead_core.documents.agent_caller import read_document_caller_in_tx

        current = await read_document_caller_in_tx(db, path=path, scope=scope, turn_id=turn_id, execution_id=binding.execution_id)
        if current.capability_fingerprint != binding.capability_fingerprint or runtime.owns_binding:
            raise DocumentRequestError("document_capability_unavailable")
    if runtime.owns_binding:
        await db.execute(
            "UPDATE turn_agent_execution SET status='running',started_at=? WHERE id=? AND status='admitted'", (now, binding.execution_id)
        )
    selection = CapabilityExecutionSelection(
        composition_node_id="document-admission",
        attempt_generation=1,
        capability_id=definition.id,
        capability_version=definition.version,
        implementation_id=implementation.id,
        implementation_version=implementation.version,
        adapter_id=None,
        prompt_module_fingerprint="sha256:" + hashlib.sha256(b"").hexdigest(),
        dependency_fingerprint=canonical_fingerprint(implementation.model_dump(mode="json")),
        input_artifact_refs=tuple(item.artifact_id for item in inputs),
        output_contract="document.admission.v1",
    )
    exact = await apply_capability_execution_conn(
        db,
        agent_execution_id=binding.execution_id,
        expected_agent_capability_fingerprint=binding.capability_fingerprint,
        selection=selection,
    )
    await db.execute("UPDATE turn_capability_execution SET status='running',started_at=? WHERE id=? AND status='selected'", (now, exact.execution_id))
    await db.execute("UPDATE turn_capability_execution SET status='completed',ended_at=? WHERE id=? AND status='running'", (now, exact.execution_id))
    if runtime.owns_binding:
        await db.execute("UPDATE turn_agent_execution SET status='completed',ended_at=? WHERE id=? AND status='running'", (now, binding.execution_id))
    return exact.execution_id
