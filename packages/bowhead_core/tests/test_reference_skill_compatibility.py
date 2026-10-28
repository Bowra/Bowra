"""Finite compatibility graphs; source access and execution are separate owners."""

import json
from dataclasses import replace

import pytest
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata
from bowhead_core.reference_skill_compatibility import ReferenceSkillMetadata, reference_skill_modes
from test_skill_read_extensions import policy_for

pytestmark = pytest.mark.unit


def resource(name, revision="a"):
    return SkillResourceRef(kind="skill", source_kind="builtin", id="builtin:" + name, version="sha256:" + revision * 64)


def method(name, children=(), required=False):
    return ReferenceSkillMetadata(
        name,
        json.dumps(
            {
                "schema_version": 1,
                "entry_file": "method.md",
                "required_files": [],
                "parameter_schema": {
                    "type": "object",
                    "properties": {"company": {"type": "string"}},
                    "additionalProperties": False,
                    "required": ["company"] if required else [],
                },
                "required_skill_refs": [child.model_dump(mode="json") for child in children],
                "requires_script": False,
                "requires_new_tools": False,
                "requires_new_connections": False,
            }
        ),
        True,
        True,
    )


def test_baseline_and_optional_graph_preserve_exact_versions_and_full_grants():
    child, root, extra = resource("support"), resource("method"), resource("extra")
    grants = tuple(
        ExactSkillGrant("builtin", item.id, name, ("read",), item.version, "reference") for item, name in [(child, "support"), (root, "method")]
    )
    receiver = ReferenceReceiverMetadata(grants, policy_for(extra))
    skills = {child: method("support"), root: method("method", [child], required=True), extra: method("extra", [child])}
    assert reference_skill_modes(receiver, [root], skills) == ("baseline",)
    assert reference_skill_modes(receiver, [extra], skills) == ("baseline", "readonly_extension")
    assert reference_skill_modes(receiver, [root, extra], skills) == ("baseline", "readonly_extension")
    assert reference_skill_modes(receiver, [child], {**skills, child: replace(skills[child], entry_json=None)}) == ("baseline",)
    assert reference_skill_modes(receiver, [extra], {**skills, extra: replace(skills[extra], entry_json=None)}) is None
    assert receiver.grants == grants
    another = resource("another")
    assert (
        reference_skill_modes(
            replace(receiver, policy=policy_for(extra, another, max_selected=1)), [extra, another], {**skills, another: method("another")}
        )
        is None
    )
    for changed in [
        {**skills, child: replace(skills[child], available=False)},
        {**skills, child: method("support", required=True)},
        {**skills, child: method("support", [root])},
        {**skills, extra: method("method", [child])},
        {**skills, extra: method("extra", [resource("support", "b")])},
    ]:
        assert reference_skill_modes(receiver, [extra], changed) is None
    assert reference_skill_modes(replace(receiver, policy=policy_for(extra, enabled=False)), [extra], skills) is None
    assert reference_skill_modes(receiver, [root, root], skills) is None
    assert reference_skill_modes(receiver, [extra], {**skills, child: replace(skills[child], strict_access=False)}) is None
    # If the dependent method is itself explicitly selected, its required values can be edited later.
    required = {**skills, child: method("support", required=True)}
    assert reference_skill_modes(receiver, [root, child], required) == ("baseline",)


def test_graph_depth_limits_apply_to_shared_dependencies_and_selected_roots():
    refs = [resource("n" + str(index)) for index in range(9)]
    receiver = ReferenceReceiverMetadata((), policy_for(*refs))
    skills = {ref: method("n" + str(i), refs[i + 1 : i + 2]) for i, ref in enumerate(refs)}
    assert reference_skill_modes(receiver, [refs[1]], skills) == ("readonly_extension",)
    assert reference_skill_modes(receiver, [refs[0]], skills) is None
    assert reference_skill_modes(receiver, [refs[-1], refs[0]], skills) is None


def test_agent_without_selected_methods_checks_its_full_baseline():
    skill = resource("script")
    grant = ExactSkillGrant("builtin", skill.id, "script", ("invoke", "read"), skill.version, "reference")
    receiver = ReferenceReceiverMetadata((grant,))
    metadata = ReferenceSkillMetadata("script", None, True)
    assert reference_skill_modes(receiver, (), {skill: metadata}) == ()
    assert reference_skill_modes(receiver, (), {}) is None
    assert reference_skill_modes(receiver, (), {resource("script", "b"): metadata}) is None
    assert reference_skill_modes(receiver, (), {skill: replace(metadata, available=False)}) is None
    assert reference_skill_modes(receiver, (), {skill: replace(metadata, name="other")}) is None
