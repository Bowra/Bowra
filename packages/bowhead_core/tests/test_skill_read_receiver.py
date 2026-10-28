"""Baseline preparation at canonical reader/source boundaries; no runtime lease.

The synthetic primary is a reader response, not a substitute for release/SQL
admission. Those producers are exercised by the separate integration tests.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, canonical_fingerprint
from bowhead_core.agent_dependency_compiler import ExactSkillGrant, parse_exact_skill_grants
from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
from bowhead_core.chat_reference_execution import ReferencePrimaryInput
from bowhead_core.chat_reference_storage import ReferencePlan
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.skill_read_receiver import SkillReadReceiverError, prepare_primary_baseline_skill_reads
from test_skill_read_closure import SCOPE, Sources, material, ref

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
HASH = "sha256:" + "1" * 64


def grant(resource, *, name=None, permissions=("read",), distribution="reference"):
    return ExactSkillGrant(resource.source_kind, resource.id, name or resource.id, permissions, resource.version, distribution)


def descriptor(definition, *, state="shadow"):
    payload = {
        "schema_version": 1,
        "compiler_version": "agent-behavior-v1",
        "source_definition_hash": canonical_fingerprint(definition),
        "primary_capability_ref": None,
        "effective_capability_refs": (),
        "implementation_refs": (),
        "eligible_lead_role_ids": (),
        "output_contract_refs": (),
        "adapter_refs": (),
        "prompt_module_refs": (),
        "prompt_module_fingerprint": canonical_fingerprint([]),
        "dependency_fingerprint": HASH,
        "capability_fingerprint": HASH,
        "effective_model_role": "analyst",
        "verification_state": state,
        "reason_codes": (),
    }
    return AgentBehaviorDescriptor.model_validate({**payload, "behavior_fingerprint": canonical_fingerprint(payload)})


def primary(grants, *, definition=None):
    definition = definition or {"skill_grants": [item.to_canonical_dict() for item in grants], "system_prompt": "PRIVATE_AGENT_INSTRUCTIONS"}
    binding = ExactAgentExecutionBinding(
        "execution",
        SCOPE.team_id,
        "turn",
        "agent",
        "team",
        "revision",
        "release",
        "runtime-name",
        "legacy-capability-fingerprint",
        HASH,
        definition,
        {},
        "admitted",
        descriptor(definition),
    )
    source = ChatTurnUserSource("turn", SCOPE.session_id, SCOPE.user_id, SCOPE.workspace_id, "Original task", (), (), None)
    plan = ReferencePlan(
        schema_version=1, source_turn_id="turn", logical_source_turn_id="turn", snapshot_owner_turn_id="turn", source_hash=HASH, admissions=[]
    )
    return ReferencePrimaryInput(source, plan, binding)


def environment(records, grants):
    state = SimpleNamespace(primary=primary(grants), reads=0)
    sources = Sources(records)

    async def reader():
        state.reads += 1
        return state.primary

    async def prepare(resources):
        return await prepare_primary_baseline_skill_reads(sources, resources, scope=SCOPE, turn_id="turn", primary_factory=reader)

    state.prepare, state.sources = prepare, sources
    return state


async def test_baseline_intersection_binds_every_dependency_without_invoke_expansion():
    a, b = ref("a"), ref("b")
    state = environment([material(a, (b,)), material(b)], [grant(a, permissions=("invoke", "read")), grant(b)])
    result = await state.prepare([a])
    assert [item.proof.resource for item in result.closure.items] == [b, a]
    assert result.receiver.execution_id == "execution" and result.receiver.revision_ref == "revision"
    assert result.receiver.scope == SCOPE and result.receiver.baseline.verification_state == "shadow"
    # Legacy and behavior Capability fingerprints intentionally have different semantics.
    assert result.receiver.capability_fingerprint != result.receiver.baseline.capability_fingerprint
    assert result.receiver.grants == tuple(sorted([grant(a, permissions=("invoke", "read")), grant(b)], key=lambda item: item.identity))
    assert state.reads == 2 and state.sources.checked == ["b", "a"]
    assert "PRIVATE_AGENT_INSTRUCTIONS" not in repr(result) and "METHOD" not in repr(result)
    state.primary.binding.definition["skill_grants"].clear()
    assert len(result.receiver.grants) == 2


@pytest.mark.parametrize("case", ["absent", "other_origin", "other_version", "invoke_only", "malformed"])
async def test_ownership_or_matching_name_cannot_replace_an_exact_read_grant(case):
    selected = ref("method")
    grants = {
        "absent": [],
        "other_origin": [grant(ref("method", source="team"))],
        "other_version": [grant(ref("method", version="sha256:" + "0" * 64))],
        "invoke_only": [grant(selected, permissions=("invoke",))],
        "malformed": [grant(selected)],
    }[case]
    state = environment([material(selected)], grants)
    if case == "malformed":
        definition = state.primary.binding.definition
        definition["skill_grants"][0]["permissions"] = [{}]
        state.primary = replace(state.primary, binding=replace(state.primary.binding, behavior_descriptor=descriptor(definition)))
    with pytest.raises(SkillReadReceiverError):
        await state.prepare([selected])
    assert not state.sources.reads


async def test_all_selected_roots_are_checked_before_opening_any_material():
    a, b = ref("a"), ref("b")
    state = environment([material(a), material(b)], [grant(a)])
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_baseline_required$"):
        await state.prepare([a, b])
    assert not state.sources.reads


async def test_top_level_baseline_does_not_grant_its_transitive_dependency():
    a, b = ref("a"), ref("b")
    state = environment([material(a, (b,)), material(b)], [grant(a)])
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_baseline_required$"):
        await state.prepare([a])
    assert state.sources.reads == ["a"]


async def test_material_name_must_match_the_exact_published_identity():
    resource = ref("method")
    state = environment([material(resource)], [grant(resource, name="renamed")])
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_baseline_required$"):
        await state.prepare([resource])
    assert state.sources.reads == ["method"]


@pytest.mark.parametrize("field", ["user_id", "workspace_id", "session_id", "turn_id"])
async def test_source_scope_cannot_be_swapped_for_another_canonical_primary(field):
    resource = ref("method")
    state = environment([material(resource)], [grant(resource)])
    state.primary = replace(state.primary, source=replace(state.primary.source, **{field: "other"}))
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_scope_mismatch$"):
        await state.prepare([resource])
    assert not state.sources.reads


@pytest.mark.parametrize(
    "change", ["team", "turn", "terminal", "missing_release", "missing_descriptor", "blocked", "definition_tampered", "legacy_only"]
)
async def test_unavailable_or_inconsistent_receiver_does_not_open_material(change):
    resource = ref("method")
    state = environment([material(resource)], [grant(resource)])
    binding = state.primary.binding
    if change in {"team", "turn", "terminal", "missing_release", "missing_descriptor"}:
        field, value = {
            "team": ("team_id", "other"),
            "turn": ("turn_id", "other"),
            "terminal": ("status", "completed"),
            "missing_release": ("release_id", None),
            "missing_descriptor": ("behavior_descriptor", None),
        }[change]
        binding = replace(binding, **{field: value})
    elif change == "blocked":
        binding = replace(binding, behavior_descriptor=descriptor(binding.definition, state="blocked"))
    elif change == "definition_tampered":
        binding.definition["system_prompt"] = "CHANGED_PRIVATE_INSTRUCTIONS"
    else:
        definition = {"skills": ["method"]}
        binding = replace(binding, definition=definition, behavior_descriptor=descriptor(definition))
    state.primary = replace(state.primary, binding=binding)
    with pytest.raises(SkillReadReceiverError):
        await state.prepare([resource])
    assert not state.sources.reads


@pytest.mark.parametrize("tampered", [False, True])
async def test_plugin_expanded_binding_is_checked_against_its_authored_definition(tampered):
    # A team Agent with a Plugin runs the expanded definition; its descriptor hashes the authored one.
    resource = ref("method")
    state = environment([material(resource)], [grant(resource)])
    binding = state.primary.binding
    authored = {**binding.definition, "builtin_plugin_ids": ["builtin:knowledge-base"]}
    expanded = {**authored, "tool_refs": ["kb_search"], "resolved_builtin_plugins": [{"name": "knowledge-base", "version": 1}]}
    binding = replace(binding, definition=expanded, authored_definition=authored, behavior_descriptor=descriptor(authored))
    if tampered:
        binding = replace(binding, authored_definition={**authored, "system_prompt": "CHANGED_PRIVATE_INSTRUCTIONS"})
    state.primary = replace(state.primary, binding=binding)
    if tampered:
        with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_unavailable$"):
            await state.prepare([resource])
        assert not state.sources.reads
    else:
        result = await state.prepare([resource])
        assert result.receiver.grants == (grant(resource),)


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_id", "replacement"),
        ("revision_ref", "revision2"),
        ("release_id", "release2"),
        ("policy_fingerprint", "sha256:" + "0" * 64),
        ("status", "running"),
    ],
)
async def test_receiver_is_rechecked_after_material_preparation(field, value):
    resource = ref("method")
    state = environment([material(resource)], [grant(resource)])

    def change(_):
        state.primary = replace(state.primary, binding=replace(state.primary.binding, **{field: value}))

    state.sources.after_read = change
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_changed$"):
        await state.prepare([resource])
    assert state.reads == 2


async def test_reader_revocation_or_failure_never_returns_the_partial_closure():
    resource = ref("method")
    sources = Sources([material(resource)])
    original = primary([grant(resource)])
    calls = 0

    async def reader():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("PRIVATE_ACCOUNT /private/source")
        return original

    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_unavailable$"):
        await prepare_primary_baseline_skill_reads(sources, [resource], scope=SCOPE, turn_id="turn", primary_factory=reader)
    assert calls == 2 and sources.reads == ["method"]


@pytest.mark.parametrize("legacy", [{"skills": []}, {"skills_json": "[]"}, {"skills": None}, {"skills_json": "null"}, {}])
async def test_empty_legacy_metadata_does_not_hide_an_explicit_exact_baseline(legacy):
    resource = ref("method")
    state = environment([material(resource)], [grant(resource)])
    definition = {**state.primary.binding.definition, **legacy}
    state.primary = replace(state.primary, binding=replace(state.primary.binding, definition=definition, behavior_descriptor=descriptor(definition)))
    await state.prepare([resource])


@pytest.mark.parametrize("damage", ["missing", "duplicate_id", "duplicate_name", "permission", "invoke_only", "field", "too_many"])
async def test_shared_grant_reader_rejects_the_entire_invalid_projection(damage):
    raw = [grant(ref("a")).to_canonical_dict()]
    if damage == "missing":
        raw = None
    elif damage == "duplicate_id":
        raw.append({**raw[0], "skill_ref": {**raw[0]["skill_ref"], "name": "other_name"}})
    elif damage == "duplicate_name":
        raw.append(grant(ref("b"), name="a").to_canonical_dict())
    elif damage == "permission":
        raw[0]["permissions"] = ["read", "read"]
    elif damage == "invoke_only":
        raw[0]["permissions"] = ["invoke"]
    elif damage == "field":
        raw[0]["unexpected"] = "PRIVATE_FIELD"
    else:
        raw = [grant(ref("method" + str(i))).to_canonical_dict() for i in range(65)]
    with pytest.raises(ValueError, match="^exact_skill_grants_unavailable$"):
        parse_exact_skill_grants(raw)


async def test_read_grants_are_detached_and_keep_distribution_without_becoming_invocations():
    resource = ref("method")
    raw = [grant(resource, distribution="embedded_agent_scope").to_canonical_dict()]
    parsed = parse_exact_skill_grants(raw)
    raw[0]["permissions"].append("invoke")
    assert parsed == (grant(resource, distribution="embedded_agent_scope"),)


async def test_preparation_digest_binds_the_receiver_execution_even_for_identical_material():
    resource = ref("method")
    state = environment([material(resource)], [grant(resource)])
    first = await state.prepare([resource])
    state.primary = replace(state.primary, binding=replace(state.primary.binding, execution_id="another_execution"))
    second = await state.prepare([resource])
    assert first.closure.closure_digest == second.closure.closure_digest
    assert first.preparation_digest != second.preparation_digest
