"""Published-policy intersections using real revisions and current Skill sources.

An ordinary native binding supplies the pin; this tests the policy/preparation
boundary, not reference admission, worker ownership or model injection.
"""

from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_execution import ReferencePrimaryInput
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import ReferencePlan, reference_source_hash
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.skill_baseline_material import prepare_primary_skill_baseline
from bowhead_core.skill_read_extensions import prepare_extended_skill_reads, primary_extension_policy, require_current_extension_policy
from bowhead_core.skill_read_receiver import SkillReadReceiverError, primary_skill_read_receiver
from bowhead_core.skill_read_resolution import build_reference_skill_sources
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from test_agent_revision_store import _definition
from test_agent_runtime_extension_publication import native_binding, save, seed_release, setup
from test_skill_read_extensions import policy_for
from test_skill_read_receiver_integration import package

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def case(tmp_path, request):
    current = setup(tmp_path)
    root = tmp_path / "builtin"
    selected = package(root, "optional_method", executable=True)
    kind = getattr(request, "param", "builtin")
    if kind == "user":
        from bowhead_core.skill_instance_store import SkillInstanceStore

        record = await SkillInstanceStore(current.path).upsert(
            user_id="user_owner", name="optional_method", source_type="user_upload", source_path=str(root / "optional_method"), enabled=True
        )
        selected = selected.model_copy(update={"source_kind": "user", "id": record.id})
    elif kind == "team":
        from bowhead_core.user_store import TeamSkill, UserStore

        record = TeamSkill(
            id="oskill_optional",
            team_id="team_release",
            name="optional_method",
            source_path=str(root / "optional_method"),
            created_by="user_owner",
            approved_sha256=selected.version,
            risk_report_json='{"langs":{".py":1},"keywords":[],"shebangs":[]}',
        )
        await UserStore(current.path).create_team_skill(record)
        selected = selected.model_copy(update={"source_kind": "team", "id": record.id})
    policy = policy_for(selected)
    value = {**_definition("Keep the original business role."), "runtime_extensions": policy.canonical_value()}
    working = await save(current, value)
    released = await seed_release(current, working)
    binding = await native_binding(current)
    scope = ReferenceRequestScope(team_id="team_release", user_id="user_owner", workspace_id="ws_eval", session_id="s_extensions")
    source = ChatTurnUserSource(
        "t_extensions", scope.session_id, scope.user_id, scope.workspace_id, "Use my selected profitability method.", (), (), None
    )
    plan = ReferencePlan(
        schema_version=1,
        source_turn_id=source.turn_id,
        logical_source_turn_id=source.turn_id,
        snapshot_owner_turn_id=source.turn_id,
        source_hash=reference_source_hash(source),
        admissions=[],
    )
    primary = ReferencePrimaryInput(source, plan, binding)
    receiver = primary_skill_read_receiver(primary, scope, source.turn_id)
    async with open_shared_ctx(current.path) as db:
        await db.execute("BEGIN")
        extension = await primary_extension_policy(db, primary, receiver)
    index = build_builtin_skill_metadata(builtin_root=root)
    sources = build_reference_skill_sources(current.path, scope=scope, builtin_index=lambda: index, builtin_root=root)

    async def read():
        return primary

    baseline = await prepare_primary_skill_baseline(sources, scope=scope, turn_id=source.turn_id, primary_factory=read)
    prepared = await prepare_extended_skill_reads(sources, baseline, [selected], extension)
    return SimpleNamespace(current=current, root=root, selected=selected, primary=primary, prepared=prepared, original=released, sources=sources)


async def check(case):
    async with open_shared_ctx(case.current.path) as db:
        await db.execute("BEGIN")
        return await require_current_extension_policy(db, case.prepared.receiver, case.prepared.closure, case.prepared.extension)


@pytest.mark.parametrize("case", ["builtin", "team", "user"], indirect=True)
async def test_unpublished_draft_cannot_disable_a_running_versions_optional_method(case):
    initial = await check(case)
    draft = {**case.primary.binding.definition, "runtime_extensions": policy_for(case.selected, enabled=False).canonical_value()}
    await save(case.current, draft)
    assert await check(case) == initial
    assert initial.current_revision_ref == case.original.revision_id
    assert initial.current_release_id == case.original.release_id
    assert initial.current_extension_policy_digest == case.prepared.extension.policy.digest


@pytest.mark.parametrize("change", ["disabled", "cleared", "removed", "different_version"])
async def test_published_tightening_blocks_original_selection_without_switching_its_agent(case, change):
    replacement = case.selected.model_copy(update={"id": "builtin:another_method"} if change == "removed" else {"version": "sha256:" + "d" * 64})
    value = (
        policy_for(replacement).canonical_value()
        if change in {"removed", "different_version"}
        else (None if change == "cleared" else policy_for(case.selected, enabled=False).canonical_value())
    )
    working = await save(case.current, {**_definition("New role applies only to new executions."), "runtime_extensions": value})
    published = await seed_release(case.current, working)
    assert published.revision_id != case.primary.binding.revision_ref
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        await check(case)
    assert case.primary.binding.definition["system_prompt"] == "Keep the original business role."
    assert case.prepared.receiver.grants == ()


async def test_new_allowlist_can_keep_original_choice_but_cannot_expand_its_frozen_scope(case):
    extra = package(case.root, "later_method")
    value = policy_for(case.selected, extra).canonical_value()
    working = await save(case.current, {**_definition("Changed current role."), "runtime_extensions": value})
    published = await seed_release(case.current, working)
    checked = await check(case)
    assert checked.current_revision_ref == published.revision_id
    assert checked.current_extension_policy_digest != case.prepared.extension.policy.digest
    assert checked.original_extension_digest == case.prepared.extension.digest
    with pytest.raises(SkillReadReceiverError, match="extension_denied"):
        await prepare_extended_skill_reads(case.sources, case.prepared.baseline, [extra], case.prepared.extension)
    assert case.prepared.receiver.revision_ref == case.original.revision_id


async def test_original_policy_reader_rejects_a_modified_receiver(case):
    changed = case.prepared.receiver.model_copy(update={"source_hash": "sha256:" + "f" * 64})
    async with open_shared_ctx(case.current.path) as db:
        await db.execute("BEGIN")
        with pytest.raises(SkillReadReceiverError, match="extension_denied"):
            await primary_extension_policy(db, case.primary, changed)
