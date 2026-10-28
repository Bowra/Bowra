"""Real full baseline capture, independent selected input and original recovery."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from uuid import uuid4

import pytest
from bowhead_core.chat_references import ReferenceNode
from bowhead_core.skill_baseline_material import (
    SkillBaselineMaterialError,
    prepare_baseline_selected_reads,
    prepare_primary_skill_baseline,
    restore_skill_baseline,
)
from bowhead_core.skill_read_render import render_prepared_skill_reads
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


async def prepare(case):
    return await prepare_primary_skill_baseline(case.sources, scope=case.scope, turn_id="t_reference", primary_factory=case.reader)


@pytest.mark.parametrize("receiver_case", ["unused_read", "selected_invoke", "unused_invoke", "unused_large"], indirect=True)
async def test_complete_baseline_retains_all_grants_without_forcing_extra_methods(receiver_case):
    case = receiver_case
    primary = await case.reader()
    baseline = await prepare(case)
    assert len(baseline.packages) == len(primary.binding.definition["skill_grants"])
    assert [grant.to_canonical_dict() for grant in baseline.receiver.grants] == sorted(
        primary.binding.definition["skill_grants"], key=lambda g: g["skill_ref"]["id"]
    )
    shutil.rmtree(case.root)
    restored = restore_skill_baseline(baseline.content, receiver=baseline.receiver, content_hash=baseline.content_hash)
    assert restored == baseline
    selected = await prepare_baseline_selected_reads(restored, [case.selected])
    node = ReferenceNode(type="reference", node_id=str(uuid4()), resource=case.selected, label="Profit method", parameters={})
    rendered = render_prepared_skill_reads(selected, [node])
    assert {item["resource"]["id"] for item in rendered.document()["skills"]} == {"builtin:method", "builtin:support"}
    assert b"BODY_unused" not in rendered.content
    assert b"fixture script must never run" not in rendered.content
    assert len(restored.packages) == len(baseline.receiver.grants)


@pytest.mark.parametrize("receiver_case", ["unused_invoke"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "duplicate", "permissions", "execution", "package", "scope", "epoch", "schema"])
async def test_baseline_restore_rejects_changed_original_identity_or_material(receiver_case, change):
    baseline = await prepare(receiver_case)
    receiver = baseline.receiver
    value = json.loads(baseline.content)
    if change == "missing":
        value["members"].pop()
    elif change == "duplicate":
        value["members"][-1] = value["members"][0]
    elif change == "permissions":
        grants = tuple(replace(grant, permissions=("read",)) for grant in receiver.grants)
        receiver = receiver.model_copy(update={"grants": grants})
    elif change == "execution":
        receiver = receiver.model_copy(update={"execution_id": "another-execution"})
    elif change == "package":
        value["members"][-1]["package"]["files"].pop()
    elif change == "scope":
        value["members"][-1]["proof"]["scope"]["session_id"] = "other-session"
    elif change == "epoch":
        value["members"][-1]["proof"]["policy_epoch"] += 1
    else:
        value["schema_version"] = True
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(SkillBaselineMaterialError):
        restore_skill_baseline(content, receiver=receiver, content_hash="sha256:" + hashlib.sha256(content).hexdigest())


@pytest.mark.parametrize("receiver_case", ["unused_read"], indirect=True)
async def test_revocation_of_earlier_package_during_full_capture_rejects_candidate(receiver_case, monkeypatch):
    case = receiver_case
    original = case.sources.prepare_package
    seen = []

    async def interleave(resource, **kwargs):
        result = await original(resource, **kwargs)
        seen.append(resource)
        if resource.id == "builtin:unused":
            await case.sources.instances.upsert(
                user_id=case.scope.user_id, name="method", source_type="builtin", source_path=str(case.root / "method"), enabled=False
            )
        return result

    monkeypatch.setattr(case.sources, "prepare_package", interleave)
    with pytest.raises(SkillBaselineMaterialError):
        await prepare(case)
    assert [item.id for item in seen] == ["builtin:method", "builtin:support", "builtin:unused"]


async def test_an_actual_worker_claim_during_preparation_invalidates_the_candidate(receiver_case, monkeypatch):
    from bowhead_core.chat_reference_execution import claim_reference_primary_for_worker

    case = receiver_case
    original = case.sources.prepare_package
    claimed = []

    async def claim_during_capture(resource, **kwargs):
        result = await original(resource, **kwargs)
        if resource.id == "builtin:support":
            claimed.append(
                await claim_reference_primary_for_worker(
                    scope=case.case.scope, turn_id="t_reference", owner_id="another-worker", expected_generation=1, bindings=case.case.bindings
                )
            )
        return result

    monkeypatch.setattr(case.sources, "prepare_package", claim_during_capture)
    with pytest.raises(SkillBaselineMaterialError):
        await prepare(case)
    assert len(claimed) == 1 and claimed[0].outcome == "claimed"
