"""Unproven outcomes do not confer execution or waive real dependency blockers."""

import pytest
from bowhead_core.agent_behavior_contract import behavior_fingerprint, supports_skill_method_input
from bowhead_core.agent_execution_binding import project_system_agent_revision
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.chat_reference_capability import _resolve_implementation
from bowhead_core.chat_references import CapabilityResourceRef
from bowhead_core.subagent import _system_profile_snapshot


@pytest.fixture
def unproven_primary():
    profile = manifest_to_profile(AgentRegistry.bootstrap().get("compute", None))
    definition = _system_profile_snapshot(profile)
    behavior = project_system_agent_revision(team_id="org_method_test", profile_name="compute", definition=definition).behavior_descriptor
    assert behavior.verification_state == "blocked"
    assert behavior.primary_capability_ref is None
    assert set(behavior.reason_codes) == {"composition:no_reviewed_implementation:cap_data_query@1", "primary_capability_unproven:cap_data_query@1"}
    return definition, behavior


def changed(behavior, **fields):
    value = behavior.model_copy(update=fields)
    return value.model_copy(update={"behavior_fingerprint": behavior_fingerprint(value)})


def test_unproven_declared_primary_only_permits_method_input(unproven_primary):
    definition, behavior = unproven_primary
    assert supports_skill_method_input(behavior, allow_unproven_primary=True, declared_primary=definition["primary_capability_ref"])
    assert not supports_skill_method_input(behavior, allow_unproven_primary=False, declared_primary=definition["primary_capability_ref"])
    with pytest.raises(ValueError, match="reference_capability_behavior_unavailable"):
        _resolve_implementation(behavior, CapabilityResourceRef(kind="capability", id="cap_data_query", version=1))


@pytest.mark.parametrize(
    "extra",
    ["dependency_status:blocked", "dependency:skill_bundle_unavailable", "system_tool_contract_malformed", "composition_blocked", "unknown_blocker"],
)
def test_other_blockers_cannot_use_the_method_exception(unproven_primary, extra):
    definition, behavior = unproven_primary
    value = changed(behavior, reason_codes=tuple(sorted((*behavior.reason_codes, extra))))
    assert not supports_skill_method_input(value, allow_unproven_primary=True, declared_primary=definition["primary_capability_ref"])


@pytest.mark.parametrize("declared", [None, "cap_analysis@1", ""])
def test_unproven_reasons_must_match_the_original_declaration(unproven_primary, declared):
    _, behavior = unproven_primary
    assert not supports_skill_method_input(behavior, allow_unproven_primary=True, declared_primary=declared)


@pytest.mark.parametrize(
    "reasons",
    [
        (),
        ("primary_capability_unproven:cap_data_query@1",),
        ("composition:no_reviewed_implementation:cap_analysis@1", "primary_capability_unproven:cap_data_query@1"),
    ],
)
def test_unknown_or_incomplete_blocking_evidence_stays_unavailable(unproven_primary, reasons):
    definition, behavior = unproven_primary
    assert not supports_skill_method_input(
        changed(behavior, reason_codes=reasons), allow_unproven_primary=True, declared_primary=definition["primary_capability_ref"]
    )
