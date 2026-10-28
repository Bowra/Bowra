"""Real source permission and exact package capture, without runtime grants."""

from __future__ import annotations

import asyncio
import shutil

import pytest
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


async def test_package_and_selected_method_use_same_exact_source(receiver_case):
    case = receiver_case
    original = await case.sources.prepare(case.selected, scope=case.scope)
    package = await case.sources.prepare_package(case.selected, scope=case.scope)
    assert package.proof == original.proof
    shutil.rmtree(case.root)
    assert package.material.selected_read() == original.material
    assert package.material.files()["method.md"] == b"BODY_method"


@pytest.mark.parametrize("timing", ["before", "during"])
async def test_current_revocation_blocks_full_package_preparation(receiver_case, monkeypatch, timing):
    case = receiver_case

    async def revoke():
        await case.sources.instances.upsert(
            user_id=case.scope.user_id, name="method", source_type="builtin", source_path=str(case.root / "method"), enabled=False
        )

    if timing == "before":
        await revoke()
    else:
        import bowhead_core.skill_read_resolution as module

        original = module.anyio.to_thread.run_sync

        async def interleave(*args, **kwargs):
            result = await original(*args, **kwargs)
            await revoke()
            return result

        monkeypatch.setattr(module.anyio.to_thread, "run_sync", interleave)
    with pytest.raises(SkillReadResolutionError):
        await case.sources.prepare_package(case.selected, scope=case.scope)


async def test_source_scope_cannot_be_replaced_by_another_team(receiver_case):
    case = receiver_case
    with pytest.raises(SkillReadResolutionError):
        await case.sources.prepare_package(case.selected, scope=case.scope.model_copy(update={"team_id": "another-team"}))


@pytest.mark.parametrize("pending", [False, True])
async def test_personal_package_uses_exact_owned_instance_and_import_status(receiver_case, pending):
    case = receiver_case
    record = await case.sources.instances.upsert(
        user_id=case.scope.user_id, name="method", source_type="user_upload", source_path=str(case.root / "method"), enabled=True
    )
    if pending:
        await case.sources.instances.set_import_status(user_id=case.scope.user_id, name="method", import_status="pending", import_report_json="{}")
    resource = SkillResourceRef(kind="skill", source_kind="user", id=record.id, version=case.selected.version)
    if pending:
        with pytest.raises(SkillReadResolutionError):
            await case.sources.prepare_package(resource, scope=case.scope)
    else:
        result = await case.sources.prepare_package(resource, scope=case.scope)
        assert result.proof.resource.source_kind == "user" and result.proof.resource.id == record.id
        assert result.material.files()["method.md"] == b"BODY_method"
        # Same display name and bytes do not make an unowned instance selectable.
        with pytest.raises(SkillReadResolutionError):
            await case.sources.prepare_package(resource.model_copy(update={"id": "someone-elses-instance"}), scope=case.scope)


async def test_cancelled_source_recheck_does_not_return_a_package(receiver_case, monkeypatch):
    case = receiver_case
    original = case.sources._resolve
    count = 0

    async def cancel_after_capture(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise asyncio.CancelledError()
        return await original(*args, **kwargs)

    monkeypatch.setattr(case.sources, "_resolve", cancel_after_capture)
    with pytest.raises(asyncio.CancelledError):
        await case.sources.prepare_package(case.selected, scope=case.scope)
    assert count == 2
