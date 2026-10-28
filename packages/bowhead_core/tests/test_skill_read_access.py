"""Real retained material reads under current authority, not generic tool routing."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import traceback
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bowhead_core.agents._lead_context import reset_current_subagent_id, set_current_subagent_id
from bowhead_core.chat_reference_scope import require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceScope
from bowhead_core.skill_read_access import ReferenceSkillReadAccess, SkillReadAccessError
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.tool_registry import ToolExecutionContext
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import facts
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case

claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.fixture
async def access_case(claimed_case):
    from bowhead_core._db import open_shared_ctx

    case = claimed_case
    await claim_reference_skill_primary_for_worker(**case.args)
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        epoch = await require_reference_request_scope(db, case.scope)
        await db.rollback()
    args = {
        "sources": case.fixture.sources,
        "cipher": case.cipher,
        "scope": ReferenceScope(**case.scope.model_dump(), policy_epoch=epoch),
        "turn_id": "t_reference",
        "owner_id": "mixed-worker",
        "generation": 1,
    }
    access = await ReferenceSkillReadAccess.bind(**args)
    ctx = ToolExecutionContext(**case.scope.model_dump(), turn_id="t_reference", member_uid=case.fixture.case.fixture.member_id, datarc=None)
    return SimpleNamespace(case=case, access=access, args=args, ctx=ctx, handle=next(h for h in access.handles if h.resource.id == "builtin:method"))


@contextmanager
def receiver(subagent_id):
    token = set_current_subagent_id(subagent_id)
    try:
        yield
    finally:
        reset_current_subagent_id(token)


async def read(case, handle=None, **kwargs):
    with receiver(case.handle.subagent_id):
        return await case.access.read(handle or case.handle, ctx=case.ctx, **kwargs)


async def test_retained_read_uses_original_files_and_parameters_without_name_resolution(access_case):
    case = access_case
    before = facts(case.case)
    for name in ("method", "support"):
        path = case.case.fixture.root / name / "method.md"
        path.write_text("REPLACEMENT_PACKAGE")
        path.unlink()
    result = await read(case)
    assert result["parameters"] == {"year": 2026}
    assert result["files"] == [{"path": "method.md", "text": "BODY_method", "sha256": "sha256:" + hashlib.sha256(b"BODY_method").hexdigest()}]
    assert result["resource"] == case.handle.resource.model_dump(mode="json")
    assert result["content_hash"] == case.handle.content_hash
    assert result["materialization_hash"] == case.handle.materialization_hash
    assert "NOT_SELECTED_MANIFEST" not in json.dumps(result)
    assert not {"scripts", "source_dir", "body_raw", "allowed_tools"}.intersection(result)
    result["parameters"]["year"] = 2030
    result["files"][0]["text"] = "CHANGED_RESULT"
    assert (await read(case, file="method.md"))["parameters"] == {"year": 2026}
    assert (await read(case))["files"][0]["text"] == "BODY_method"
    assert facts(case.case) == before  # no new evidence, lease renewal or state write


@pytest.mark.parametrize("file", ["SKILL.md", "scripts/run.py", "../method.md", "/method.md", "./method.md", True])
async def test_only_exact_declared_files_are_readable(access_case, file):
    with pytest.raises(SkillReadAccessError, match="file_unavailable"):
        await read(access_case, file=file)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 2),
        ("generation", True),
        ("resolution_seq", 2),
        ("resolution_seq", True),
        ("composition_digest", "sha256:" + "0" * 64),
        ("operation", "invoke"),
        ("content_key", "other"),
        ("execution_id", "other"),
        ("parameter_source", "new-parameters"),
    ],
)
async def test_handle_fields_are_bound_to_the_saved_composition(access_case, field, value):
    with pytest.raises(SkillReadAccessError):
        await read(access_case, replace(access_case.handle, **{field: value}))


@pytest.mark.parametrize("field", ["team_id", "user_id", "workspace_id", "session_id", "turn_id"])
async def test_copied_context_cannot_cross_receiver_scope(access_case, field):
    case = access_case
    case.ctx = copy.copy(case.ctx)
    setattr(case.ctx, field, "other")
    with pytest.raises(SkillReadAccessError, match="receiver_changed"):
        await read(case)


async def test_helper_and_missing_task_identity_cannot_use_primary_handle(access_case):
    case = access_case
    case.ctx._current_subagent_id = case.handle.subagent_id  # shared attribute is not authority
    for subagent_id in (None, "helper"):
        with receiver(subagent_id), pytest.raises(SkillReadAccessError, match="receiver_changed"):
            await case.access.read(case.handle, ctx=copy.copy(case.ctx))
    ready = asyncio.Event()

    async def primary():
        with receiver(case.handle.subagent_id):
            await ready.wait()
            return await case.access.read(case.handle, ctx=case.ctx)

    async def helper():
        with receiver("helper"):
            ready.set()
            with pytest.raises(SkillReadAccessError, match="receiver_changed"):
                await case.access.read(case.handle, ctx=case.ctx)

    first, _ = await asyncio.gather(primary(), helper())
    assert first["files"][0]["text"] == "BODY_method"


@pytest.mark.parametrize("change", ["method", "support", "epoch", "cancel", "lease", "generation", "owner", "purge", "agent"])
async def test_current_state_is_rechecked_after_a_successful_read(access_case, change):
    case = access_case
    assert (await read(case))["files"]
    if change in {"method", "support"}:
        await case.case.fixture.sources.instances.upsert(
            user_id=case.case.scope.user_id, name=change, source_type="builtin", source_path=str(case.case.fixture.root / change), enabled=False
        )
    else:
        with sqlite3.connect(case.case.path) as db:
            if change == "epoch":
                db.execute("UPDATE access_policy_state SET epoch=epoch+1 WHERE team_id=?", (case.case.scope.team_id,))
            elif change == "cancel":
                db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id='t_reference'", (datetime.now(UTC).isoformat(),))
            elif change == "lease":
                db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
                )
            elif change == "generation":
                db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2 WHERE turn_id='t_reference'")
            elif change == "owner":
                db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='different-worker' WHERE turn_id='t_reference'")
            elif change == "purge":
                db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
            else:
                db.execute(
                    "UPDATE turn_agent_execution SET status='completed',ended_at=? WHERE id=?",
                    (datetime.now(UTC).isoformat(), case.handle.execution_id),
                )
    before = facts(case.case)
    with pytest.raises(SkillReadAccessError, match="access_unavailable"):
        await read(case)
    assert facts(case.case) == before


async def test_last_fence_catches_expiry_during_member_revalidation(access_case, monkeypatch):
    case = access_case
    original = type(case.access.sources).revalidate_in_connection

    async def expires(self, db, proof):
        await original(self, db, proof)
        await db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )

    monkeypatch.setattr(type(case.access.sources), "revalidate_in_connection", expires)
    before = facts(case.case)
    with pytest.raises(SkillReadAccessError):
        await read(case)
    assert facts(case.case) == before  # local fault injection rolls back, no lease write


async def test_private_errors_are_finite_and_cancellation_releases_transaction(access_case, monkeypatch):
    case = access_case
    original = type(case.access.sources).revalidate_in_connection

    async def fail(self, db, proof):
        raise ValueError("PRIVATE_METHOD_PARAMETER")

    monkeypatch.setattr(type(case.access.sources), "revalidate_in_connection", fail)
    with pytest.raises(SkillReadAccessError) as error:
        await read(case)
    assert "PRIVATE_METHOD_PARAMETER" not in "".join(traceback.format_exception(error.value))

    async def cancel(self, db, proof):
        raise asyncio.CancelledError()

    monkeypatch.setattr(type(case.access.sources), "revalidate_in_connection", cancel)
    with pytest.raises(asyncio.CancelledError):
        await read(case)
    monkeypatch.setattr(type(case.access.sources), "revalidate_in_connection", original)
    assert (await read(case))["files"]


async def test_waiting_reader_observes_the_new_committed_fence(access_case, monkeypatch):
    import bowhead_core.skill_read_access as module

    case = access_case
    original = module.open_shared_ctx
    attempting = asyncio.Event()

    @asynccontextmanager
    async def observed_open(path):
        async with original(path) as db:
            execute = db.execute

            def observed_execute(sql, *args, **kwargs):
                if sql == "BEGIN IMMEDIATE":
                    attempting.set()
                return execute(sql, *args, **kwargs)

            db.execute = observed_execute
            yield db

    monkeypatch.setattr(module, "open_shared_ctx", observed_open)
    async with original(case.case.path) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        await writer.execute(
            "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id='t_reference'", (datetime.now(UTC).isoformat(),)
        )
        pending = asyncio.create_task(read(case))
        try:
            await asyncio.wait_for(attempting.wait(), 5)
            assert not pending.done()
            await writer.commit()
            with pytest.raises(SkillReadAccessError, match="access_unavailable"):
                await asyncio.wait_for(pending, 5)
        finally:
            await writer.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_changed_cached_handle_list_cannot_replace_durable_identity(access_case):
    case = access_case
    forged = replace(case.handle, content_hash="sha256:" + "0" * 64)
    case.access = replace(case.access, handles=(forged,))
    with pytest.raises(SkillReadAccessError, match="handle_changed"):
        await read(case, forged)
