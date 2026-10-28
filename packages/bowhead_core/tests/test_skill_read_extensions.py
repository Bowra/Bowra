"""Real readonly packages with unchanged baselines; preparation alone grants no run."""

from dataclasses import replace

import pytest
from bowhead_core.agent_runtime_extensions import read_agent_runtime_extensions
from bowhead_core.skill_baseline_material import prepare_primary_skill_baseline
from bowhead_core.skill_read_extensions import (
    SkillReadExtensionPolicy,
    prepare_extended_skill_reads,
    require_extension_member_name,
    require_extension_roots,
    skill_read_provenance,
)
from bowhead_core.skill_read_receiver import SkillReadReceiverError
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from test_skill_read_receiver_integration import package
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


def policy_for(*resources, max_selected=3, enabled=True):
    return read_agent_runtime_extensions(
        {
            "runtime_extensions": {
                "schema_version": 1,
                "skill_read": {
                    "enabled": enabled,
                    "selection_sources": ["user_explicit"],
                    "allowed_sources": sorted({item.source_kind for item in resources}),
                    "resource_allowlist": [{"source_kind": item.source_kind, "id": item.id, "version": item.version} for item in resources],
                    "require_read_only_contract": True,
                    "max_selected": max_selected,
                },
            }
        }
    )


async def extension_case(case, *, extra_dependency=False):
    optional_child = package(case.root, "optional_support") if extra_dependency else case.selected
    extra = package(case.root, "optional_method", (optional_child,), executable=True)
    index = build_builtin_skill_metadata(builtin_root=case.root)
    case.sources.builtin_index = lambda: index
    baseline = await prepare_primary_skill_baseline(case.sources, scope=case.scope, turn_id="t_reference", primary_factory=case.reader)
    policy = policy_for(extra, *([optional_child] if extra_dependency else []))
    extension = SkillReadExtensionPolicy(receiver_digest=baseline.receiver.digest, policy=policy)
    return baseline, extra, optional_child, extension


@pytest.mark.parametrize("receiver_case", ["unused_invoke", "unused_sop"], indirect=True)
async def test_extension_closure_reuses_original_baseline_and_keeps_extra_scripts_out(receiver_case):
    baseline, extra, _, extension = await extension_case(receiver_case)
    prepared = await prepare_extended_skill_reads(receiver_case.sources, baseline, [extra], extension)
    assert prepared.receiver == baseline.receiver and prepared.baseline == baseline
    assert prepared.extension == extension
    assert {item.proof.resource.id for item in prepared.closure.items} == {extra.id, receiver_case.selected.id, receiver_case.child.id}
    assert extra.id not in {grant.id for grant in prepared.receiver.grants}
    assert b"fixture script must never run" not in b"".join(item.material.content for item in prepared.closure.items)
    assert [grant.permissions for grant in prepared.receiver.grants] == [grant.permissions for grant in baseline.receiver.grants]
    assert prepared.preparation_digest != replace(prepared, extension=None).preparation_digest


async def test_implicit_optional_dependencies_require_their_own_finite_selector(receiver_case):
    baseline, extra, child, extension = await extension_case(receiver_case, extra_dependency=True)
    denied = extension.model_copy(update={"policy": policy_for(extra)})
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        await prepare_extended_skill_reads(receiver_case.sources, baseline, [extra], denied)
    prepared = await prepare_extended_skill_reads(receiver_case.sources, baseline, [extra], extension)
    assert {item.proof.resource for item in prepared.closure.items} == {extra, child}
    assert prepared.receiver.grants == baseline.receiver.grants


async def test_policy_cannot_override_a_baseline_revision_or_name(receiver_case):
    baseline, extra, _, extension = await extension_case(receiver_case)
    selected = receiver_case.selected
    changed = selected.model_copy(update={"version": extra.version})
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        skill_read_provenance(baseline.receiver.grants, changed, policy_for(changed))
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        require_extension_member_name(baseline.receiver.grants, extra, "method")
    assert skill_read_provenance(baseline.receiver.grants, selected, None) == "baseline"
    assert skill_read_provenance(baseline.receiver.grants, extra, extension.policy) == "readonly_extension"


async def test_only_selected_extra_roots_consume_the_optional_selection_budget(receiver_case):
    baseline, extra, _, _ = await extension_case(receiver_case)
    another = extra.model_copy(update={"id": "builtin:another"})
    policy = policy_for(extra, another, max_selected=1)
    assert require_extension_roots(baseline.receiver.grants, [receiver_case.selected, extra], policy)
    with pytest.raises(ValueError, match="extension_denied"):
        require_extension_roots(baseline.receiver.grants, [extra, another], policy)
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        require_extension_roots(baseline.receiver.grants, [extra], policy_for(extra, enabled=False))


async def test_extension_scope_cannot_be_rebound_to_another_execution(receiver_case):
    baseline, extra, _, extension = await extension_case(receiver_case)
    changed = replace(baseline, receiver=baseline.receiver.model_copy(update={"execution_id": "another_execution"}))
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        await prepare_extended_skill_reads(receiver_case.sources, changed, [extra], extension)


async def test_plain_manifest_does_not_satisfy_an_explicit_readonly_extension_policy(receiver_case):
    import yaml
    from bowhead_core.skill_read_material import SkillReadMaterialError
    from bowhead_core.skill_upload import bundle_sha256
    from bowhead_skill.loader import split_skill_markdown

    case = receiver_case
    baseline, extra, _, _ = await extension_case(case)
    path = case.root / "optional_method" / "SKILL.md"
    header, body = split_skill_markdown(path.read_text())
    data = yaml.safe_load(header)
    del data["read_only_entry"]
    path.write_text("---\n" + yaml.safe_dump(data) + "---\n" + body)
    extra = extra.model_copy(update={"version": "sha256:" + bundle_sha256(path.parent)})
    index = build_builtin_skill_metadata(builtin_root=case.root)
    case.sources.builtin_index = lambda: index
    extension = SkillReadExtensionPolicy(receiver_digest=baseline.receiver.digest, policy=policy_for(extra))
    with pytest.raises(SkillReadMaterialError):
        await prepare_extended_skill_reads(case.sources, baseline, [extra], extension)
