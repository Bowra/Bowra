from __future__ import annotations

import pytest
from bowhead_core.agent_behavior_materializer import AgentBehaviorMaterializer
from bowhead_core.chat_reference_capability import (
    capability_supported_by_behavior,
    execution_selection,
    prepare_reference_capability,
)
from bowhead_core.chat_reference_catalog import initial_reference_entry_supported
from bowhead_core.chat_references import CapabilityResourceRef, InputDocument, ReferenceNode

_HASH = "sha256:" + "a" * 64


def _descriptor():
    materializer = AgentBehaviorMaterializer()
    return materializer.compile_system_revision(
        revision_id="arv_reference_image",
        definition={
            "behavior_catalog_fingerprint": materializer.catalog.fingerprint,
            "llm_agent_type": "analyst",
            "name": "image-agent",
            "primary_capability_ref": "cap_visualize@1",
            "tool_whitelist": ["image_generate", "subagent_return"],
        },
        source_definition_hash=_HASH,
    )


def _document(parameters=None):
    return InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000001",
                    "resource": {"kind": "capability", "id": "cap_visualize", "version": 1},
                    "label": "Create visuals",
                    "parameters": parameters or {},
                },
                {"type": "text", "text": "Create a launch image"},
            ],
        }
    )


def test_exact_reviewed_image_capability_prepares_a_durable_selection() -> None:
    descriptor = _descriptor()
    resource = CapabilityResourceRef(kind="capability", id="cap_visualize", version=1)
    assert capability_supported_by_behavior(descriptor, resource)
    prepared = prepare_reference_capability(_document(), descriptor)
    assert prepared is not None
    assert prepared.implementation_ref == "capi_image_generation_native@1"
    assert prepared.output_contract == "visual.artifact.v1"
    selection = execution_selection(prepared, generation=1)
    assert (selection.capability_id, selection.capability_version) == ("cap_visualize", 1)
    assert (selection.implementation_id, selection.implementation_version) == ("capi_image_generation_native", 1)
    assert selection.output_contract == "visual.artifact.v1"


def test_exact_reviewed_image_capability_is_released_for_steer() -> None:
    document = _document()
    document.nodes.insert(
        0,
        ReferenceNode.model_validate(
            {
                "type": "reference",
                "node_id": "00000000-0000-4000-8000-000000000002",
                "resource": {
                    "kind": "agent",
                    "source_kind": "team",
                    "id": "osa_visual",
                    "version": "arv_visual",
                    "assignment_id": "asg_visual",
                    "release_id": "rel_visual",
                },
                "label": "Visual analyst",
                "parameters": {},
            }
        ),
    )
    document = InputDocument.model_validate(document.model_dump(mode="python"))
    assert initial_reference_entry_supported(
        document,
        entry_mode="steer",
        required_skill_modes=(),
    )


def test_released_entry_requires_both_capability_and_skill_cells_for_a_mixed_selection() -> None:
    document = _document().model_copy(deep=True)
    document.nodes.insert(
        1,
        ReferenceNode.model_validate(
            {
                "type": "reference",
                "node_id": "00000000-0000-4000-8000-000000000002",
                "resource": {"kind": "skill", "source_kind": "builtin", "id": "builtin:method", "version": _HASH},
                "label": "Financial method",
                "parameters": {},
            }
        ),
    )
    document = InputDocument.model_validate(document.model_dump(mode="python"))
    assert initial_reference_entry_supported(document, required_skill_modes=("baseline",))
    assert initial_reference_entry_supported(
        document,
        entry_mode="steer",
        required_skill_modes=("baseline",),
    )
    assert not initial_reference_entry_supported(document, required_skill_modes=())


@pytest.mark.parametrize("change", ["behavior", "review", "parameters", "resource"])
def test_unreviewed_or_changed_capability_fails_closed(change: str) -> None:
    descriptor = _descriptor()
    document = _document({"style": "photo"} if change == "parameters" else None)
    if change == "behavior":
        descriptor = None
    elif change == "review":
        descriptor = descriptor.model_copy(update={"verification_state": "shadow"})
    elif change == "resource":
        document.nodes[0] = document.nodes[0].model_copy(update={"resource": CapabilityResourceRef(kind="capability", id="cap_analysis", version=1)})
    with pytest.raises(ValueError):
        prepare_reference_capability(document, descriptor)
