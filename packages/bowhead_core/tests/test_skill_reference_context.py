"""Recognize persisted references after real ToolExecutionContext reconstruction.

Detection grants nothing. Approval tests use a separate approval DB and actual
UserStore/SessionStore source data; their effect handler never reaches I/O.
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.approval_executor import ApprovalExecutor
from bowhead_core.approval_store import ApprovalStore
from bowhead_core.skill_bridge import _wrap_skill_read_with_cache, register_skill_tools
from bowhead_core.skill_reference_context import reference_turn_requires_context
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema
from bowhead_skill.tools import build_skill_tools
from test_skill_read_access import access_case as _access_case
from test_skill_read_access import claimed_case as _claimed_case
from test_skill_read_access import receiver
from test_skill_read_access import receiver_case as _receiver_case
from test_skill_read_access import start_case as _start_case

from bowhead_skill import SkillCatalog, SkillRuntime

access_case = _access_case
claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


def rebuilt(case):
    return ToolExecutionContext(
        **case.case.scope.model_dump(),
        turn_id=case.access.turn_id,
        member_uid=case.ctx.member_uid,
        datarc=None,
        session_store=case.case.fixture.case.store,
    )


def tools_for(case):
    catalog = SkillCatalog(roots=[case.case.fixture.root])
    registry = ToolRegistry()
    register_skill_tools(registry, catalog, SkillRuntime())
    raw = {tool.name: tool.handler for tool in build_skill_tools(catalog, SkillRuntime())}
    return registry, raw


async def ordinary_turn(case):
    store = case.case.fixture.case.store
    await store.ensure_session(session_id="s_ordinary", workspace_id=case.ctx.workspace_id, user_id=case.ctx.user_id)
    await store.record_turn(turn_id="t_ordinary", session_id="s_ordinary", user_text="ordinary")


@pytest.mark.parametrize("path", ["registry", "registered", "raw", "cache"])
async def test_rebuilt_context_cannot_use_a_reference_as_legacy_or_cached_body(access_case, path):
    case = access_case
    registry, raw = tools_for(case)
    ctx = rebuilt(case)
    ctx._ξ_skill_cache["method|||''"] = {"body": "PRIVATE_STALE_BODY"}
    assert ctx.reference_skill_required is False and ctx.reference_skill_access is None
    with receiver(case.handle.subagent_id):
        if path == "registry":
            outcome = await registry.execute("skill_read", {"name": "method"}, ctx)
            result = outcome.result
            assert outcome.error == "ToolPermissionError: reference_skill_unavailable"
        else:
            handler = raw["skill_read"] if path == "raw" else registry.get("skill_read").handler
            if path == "cache":
                handler = _wrap_skill_read_with_cache(raw["skill_read"])
            result = await handler({"name": "method"}, ctx)
    assert result["error_kind"] == "reference_skill_unavailable"
    assert ctx.reference_skill_required is True
    assert ctx.reference_skill_access is None  # source recognition does not mint authority
    assert "PRIVATE_STALE_BODY" not in str(result)


@pytest.mark.parametrize(
    "name,args", [("skill_list", {}), ("skill_invoke", {"name": "method", "script": "run.py"}), ("python_exec", {"code": "print(1)"})]
)
async def test_raw_discovery_and_script_entries_also_recognize_lost_context(access_case, name, args):
    case = access_case
    _, raw = tools_for(case)
    ctx = rebuilt(case)
    result = await raw[name](args, ctx)
    assert result["error_kind"] == "reference_skill_unavailable"
    assert ctx.reference_skill_required is True


async def test_durable_probe_does_not_replace_exact_access_or_receiver_checks(access_case):
    case = access_case
    ctx = rebuilt(case)
    ctx.reference_skill_access = case.access
    registry, _ = tools_for(case)
    with receiver(case.handle.subagent_id):
        result = await registry.execute("skill_read", {"name": "method"}, ctx)
    assert result.error is None and result.result["selected_method"]["files"][0]["text"] == "BODY_method"
    with receiver("helper"):
        denied = await registry.execute("skill_read", {"name": "method"}, copy.copy(ctx))
    assert denied.error == "ToolPermissionError: reference_skill_unavailable"


@pytest.mark.parametrize("association", ["access", "explicit"])
async def test_a_valid_access_from_a_different_database_cannot_supply_authority(access_case, tmp_path, association):
    case = access_case
    replica = tmp_path / "other_source.db"
    with sqlite3.connect(case.case.path) as source, sqlite3.connect(replica) as target:
        source.backup(target)
    other_sources = copy.copy(case.access.sources)
    other_sources.db_path = replica
    other_access = replace(case.access, sources=other_sources)
    ctx = rebuilt(case)
    ctx.reference_skill_access = other_access if association == "access" else case.access
    if association == "explicit":
        ctx.reference_source_db_path = str(replica)
    registry, _ = tools_for(case)
    with receiver(case.handle.subagent_id):
        denied = await registry.execute("skill_read", {"name": "method"}, ctx)
        assert denied.error == "ToolPermissionError: reference_skill_unavailable"
        # The second access really can read this independent source. Rejection
        # above is database association, not a broken Protocol or missing file.
        ctx.session_store = SimpleNamespace(db_path=replica)
        ctx.reference_skill_access = other_access
        valid = await registry.execute("skill_read", {"name": "method"}, ctx)
        assert valid.error is None and valid.result["selected_method"]["files"][0]["text"] == "BODY_method"


async def test_context_factory_preserves_the_chat_source_for_tool_dispatch(access_case):
    from bowhead_core.turn_runner import build_tool_exec_ctx

    case = access_case
    registry, _ = tools_for(case)
    orch = SimpleNamespace(tool_registry=registry, session_store=case.case.fixture.case.store)
    request = SimpleNamespace(**case.case.scope.model_dump(), turn_id=case.access.turn_id, team_router=object())
    ctx = build_tool_exec_ctx(orch, request, None)
    result = await registry.execute("skill_read", {"name": "method"}, ctx)
    assert result.error == "ToolPermissionError: reference_skill_unavailable"
    assert ctx.reference_skill_required is True


async def test_ordinary_saved_turn_keeps_normal_skill_and_cache_behavior(access_case):
    case = access_case
    await ordinary_turn(case)
    ctx = rebuilt(case)
    ctx.turn_id = "t_ordinary"
    ctx.session_id = "s_ordinary"
    registry, _ = tools_for(case)
    first = await registry.execute("skill_read", {"name": "method"}, ctx)
    second = await registry.execute("skill_read", {"name": "method"}, ctx)
    assert first.error is None and first.result["body"] == "NOT_SELECTED_MANIFEST"
    assert second.result["_cached"] is True
    assert ctx.reference_skill_required is False


@pytest.mark.parametrize("change", ["missing_db", "missing_turn", "different_store", "different_access"])
async def test_configured_source_failure_does_not_fall_back_or_create_a_database(access_case, tmp_path, change):
    case = access_case
    ctx = rebuilt(case)
    absent = tmp_path / "private_source_missing.db"
    if change == "missing_db":
        ctx.session_store = SimpleNamespace(db_path=absent)
    elif change == "missing_turn":
        ctx.turn_id = "deleted-turn"
    elif change == "different_store":
        ctx.reference_source_db_path = str(absent)
    else:
        ctx.reference_skill_access = SimpleNamespace(source_db_path=str(absent))
    registry, _ = tools_for(case)
    outcome = await registry.execute("skill_read", {"name": "method"}, ctx)
    assert outcome.error == "ToolPermissionError: reference_skill_unavailable"
    assert not absent.exists()


async def test_purged_plan_still_requires_context_when_source_body_is_gone(access_case):
    case = access_case
    with sqlite3.connect(case.case.path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
        db.execute("DELETE FROM chat_turn_user_sources WHERE turn_id='t_reference'")
    assert await reference_turn_requires_context(case.case.path, turn_id="t_reference") is True
    registry, _ = tools_for(case)
    outcome = await registry.execute("skill_read", {"name": "method"}, rebuilt(case))
    assert outcome.error == "ToolPermissionError: reference_skill_unavailable"


@pytest.mark.parametrize("opaque", ['{"schema_version":999,"nodes":[]}', "PRIVATE_UNKNOWN_FORMAT"])
async def test_source_only_probe_never_parses_unknown_formats_into_legacy_permission(access_case, tmp_path, opaque):
    # Deliberately a reader-only future/corrupt-store fixture. It is not a valid
    # writer, authorization, migration, or executable reference fixture.
    path = tmp_path / "opaque_source.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE turns(id TEXT)")
        db.execute("CREATE TABLE chat_turn_user_sources(turn_id TEXT,input_document_json TEXT)")
        db.execute("CREATE TABLE chat_reference_plans(turn_id TEXT)")
        db.execute("INSERT INTO turns VALUES ('opaque')")
        db.execute("INSERT INTO chat_turn_user_sources VALUES ('opaque',?)", (opaque,))
    assert await reference_turn_requires_context(path, turn_id="opaque") is True


async def test_rebuilt_bad_arguments_are_recognized_before_private_parser_logs(access_case, caplog):
    case = access_case
    registry, _ = tools_for(case)
    ctx = rebuilt(case)
    outcome = await registry.execute("skill_read", {"raw_arguments": "<PRIVATE_METHOD_ARGUMENT_VALUE>"}, ctx)
    assert outcome.error == "ToolPermissionError: reference_skill_unavailable"
    assert "PRIVATE_METHOD_ARGUMENT" not in caplog.text


async def test_cancellation_is_not_a_legacy_permission(access_case, monkeypatch):
    import bowhead_core.skill_reference_context as module

    case = access_case
    original = module._requires_reference_execution_in_tx

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "_requires_reference_execution_in_tx", cancel)
    registry, _ = tools_for(case)
    with pytest.raises(asyncio.CancelledError):
        await registry.execute("skill_read", {"name": "method"}, rebuilt(case))
    monkeypatch.setattr(module, "_requires_reference_execution_in_tx", original)
    assert await reference_turn_requires_context(case.case.path, turn_id=case.access.turn_id)


@pytest.mark.parametrize("mode", ["execute", "execute_effect"])
@pytest.mark.parametrize("source", ["reference", "ordinary", "absent"])
async def test_approval_reconstruction_checks_the_core_source_before_any_effect(access_case, tmp_path, mode, source):
    case = access_case
    turn_id = {"reference": "t_reference", "ordinary": "t_ordinary", "absent": "t_gone"}[source]
    session_id = "s_ordinary" if source == "ordinary" else case.case.scope.session_id
    if source == "ordinary":
        await ordinary_turn(case)
    calls = []

    async def effect(arguments, ctx):
        calls.append(ctx)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema("approved_probe", "approved effect", {}), handler=effect))
    approvals = ApprovalStore(str(tmp_path / "separate_approvals.db"))
    await approvals.initialize()
    approval = await approvals.enqueue(
        turn_id=turn_id,
        session_id=session_id,
        workspace_id=case.case.scope.workspace_id,
        member_uid=case.ctx.member_uid,
        user_id=case.case.scope.user_id,
        tool_name="approved_probe",
        arguments={},
        reason="test",
    )
    await approvals.set_decision(approval_id=approval.id, status="approved", decided_by=case.case.scope.user_id)
    executor = ApprovalExecutor(
        approval_store=approvals,
        tool_registry=registry,
        datarc=None,
        user_store=case.case.fixture.case.fixture.users,
        access_control_store=case.access.sources.access,
    )
    if mode == "execute":
        updated = await executor.execute(approval.id)
        ok, error = updated.status == "executed", updated.execution_error
    else:
        ok, _, error = await executor.execute_effect(approval.id, effect_user_id=case.case.scope.user_id, idempotency_key="approved-probe")
    if source == "ordinary":
        assert ok and len(calls) == 1
        assert calls[0].session_id == session_id
        assert calls[0].reference_source_db_path == str(case.case.path)
    else:
        assert not ok and calls == []
        assert error == ("reference_approval_binding_required" if source == "reference" else "reference_approval_source_unavailable")
