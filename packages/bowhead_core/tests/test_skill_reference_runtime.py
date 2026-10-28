"""Mixed source -> real primary runner -> native HTTP -> original durable tail.

Only provider HTTP is mocked. These internal scheduler tests do not enable the
public writer or claim adoption/quality from injected method text.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import SubagentResult, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.router import AgentBinding
from test_skill_model_invocation import access_case as _access_case
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_model_invocation import model_case as _model_case
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case

claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
access_case = _access_case
model_case = _model_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


def rows(case, table):
    with sqlite3.connect(case.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE turn_id='t_reference'")]


def expire(case):
    with sqlite3.connect(case.path) as db:
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )


@pytest.fixture
async def runtime_case(claimed_case):
    base = claimed_case
    claim = await claim_reference_skill_primary_for_worker(**base.args)
    assert claim.outcome == "claimed"
    start = base.fixture.case
    runtime = ReferencePrimaryRuntime(
        scope=start.scope,
        turn_id="t_reference",
        owner_id=base.args["owner_id"],
        bindings=start.bindings,
        cipher=base.cipher,
        skill_sources=base.fixture.sources,
    )
    ctx = ToolExecutionContext(
        **runtime.scope.identity(), turn_id=runtime.turn_id, member_uid=runtime.scope.user_id, datarc=None, session_store=start.store
    )
    return SimpleNamespace(base=base, path=base.path, runtime=runtime, store=start.store, users=start.fixture.users, ctx=ctx, primary=claim.primary)


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
async def test_mixed_runtime_uses_original_method_receiver_and_seals_output(runtime_case, family):
    case = runtime_case
    runtime = case.runtime
    adapter = adapter_for(family)
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=case.users)
    parent_access = case.ctx.reference_skill_access
    parent_marker = case.ctx.reference_skill_required

    def respond(request):
        body = json.loads(request.content)
        text = json.dumps(body, ensure_ascii=False).replace("\\n", "\n")
        assert "BODY_method" in text and "BODY_support" in text
        assert case.primary.source.input_document.text_content() in text
        assert "NOT_SELECTED_MANIFEST" not in text
        calls = rows(case, "chat_reference_model_invocations")
        assert len(calls) == 1 and calls[0]["status"] == "pending"
        assert calls[0]["execution_id"] == case.primary.binding.execution_id
        assert [item["kind"] for item in rows(case, "chat_reference_evidence")] == ["delegate_started"]
        return success(family, "invoke")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert result.ok and result.final_answer == "OK" and route.call_count == 1
        assert case.ctx.reference_skill_access is parent_access and case.ctx.reference_skill_required is parent_marker
        evidence = rows(case, "chat_reference_evidence")
        assert [item["kind"] for item in evidence] == ["delegate_started", "output_received"]
        assert all(item["subagent_id"] == result.subagent_id for item in evidence)
        output = json.loads(
            runtime.cipher.open(
                scope=runtime.scope,
                purpose="reference_output",
                identity=f"{runtime.turn_id}/{case.primary.binding.execution_id}",
                ciphertext=evidence[1]["ciphertext"],
            )
        )
        assert output == {"final_answer": "OK", "payload": None}
        assert rows(case, "turn_agent_execution")[0]["status"] == "completed"
        assert rows(case, "chat_reference_model_invocations")[0]["status"] == "completed"
        with pytest.raises(TerminalDurabilityAbort):
            await runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        assert len(rows(case, "chat_reference_model_invocations")) == 1
    finally:
        await adapter.close()


async def test_mixed_runner_reads_exact_file_then_streams_with_a_second_context(runtime_case, monkeypatch):
    from bowhead_core.skill_bridge import register_skill_tools
    from bowhead_core.tool_registry import ToolRegistry

    from bowhead_skill import SkillCatalog, SkillRuntime

    case = runtime_case
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[case.base.fixture.root]), SkillRuntime())
    case.ctx._ξ_skill_cache["method|||''"] = {"body": "FOREIGN_NAME_CACHE"}
    for name in ("method", "support"):
        (case.base.fixture.root / name / "method.md").unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("old name loader used")

    monkeypatch.setattr("bowhead_skill.tools.resolve_skill", forbidden)
    adapter = adapter_for("openai")
    # Both model calls must retain the user's primary-tier choice. Without it,
    # the post-tool stream would fall back to an unconfigured target.
    case.ctx.preferred_provider = "primary"
    case.ctx.preferred_model = "large"
    router = router_for(adapter)
    router.bind(AgentBinding(agent_type="analyst", primary=("unconfigured", "missing")))
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "read-original",
                                        "type": "function",
                                        "function": {"name": "skill_read", "arguments": '{"name":"method","file":"method.md"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        assert body["stream"] is True
        tool = next(message for message in body["messages"] if message["role"] == "tool")
        assert "BODY_method" in tool["content"] and "FOREIGN_NAME_CACHE" not in tool["content"]
        return success("openai", "stream")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.users)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert result.ok and route.call_count == 2
        calls = rows(case, "chat_reference_model_invocations")
        assert len(calls) == 2 and all(call["status"] == "completed" for call in calls)
        assert len({call["context_id"] for call in calls}) == len(rows(case, "chat_reference_model_contexts")) == 2
        assert {call["execution_id"] for call in calls} == {case.primary.binding.execution_id}
    finally:
        await adapter.close()


@pytest.mark.parametrize("failure", ["timeout", "cancel", "missing_usage"])
async def test_unknown_model_cannot_produce_success_or_reinvoke(runtime_case, failure):
    case = runtime_case
    adapter = adapter_for("openai")
    seen = []

    def respond(request):
        seen.append(request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "timeout":
            raise httpx2.ReadTimeout("PRIVATE_PROVIDER", request=request)
        return httpx2.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "unconfirmed"}, "finish_reason": "stop"}]})

    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=case.users)
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else InvocationOutcomeUnknownError):
                await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            with pytest.raises(TerminalDurabilityAbort):
                await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        assert len(seen) == 1
        assert [item["kind"] for item in rows(case, "chat_reference_evidence")] == ["delegate_started"]
        call = rows(case, "chat_reference_model_invocations")[0]
        assert call["status"] == "delivery_unknown" and call["input_tokens"] is None
        expire(case)
        assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
        assert rows(case, "turn_agent_execution")[0]["status"] == "failed"
    finally:
        await adapter.close()


@pytest.mark.parametrize("failure", ["no_receipt", "wrong_receiver"])
async def test_fabricated_runner_success_cannot_become_a_mixed_output(runtime_case, failure):
    case = runtime_case

    async def fabricate(**kwargs):
        return SubagentResult(
            profile=kwargs["profile_name"],
            subagent_id="wrong" if failure == "wrong_receiver" else kwargs["pre_minted_subagent_id"],
            ok=True,
            final_answer="fabricated",
        )

    with pytest.raises((TerminalDurabilityAbort, ValueError)):
        await case.runtime.run(runner=SimpleNamespace(run=fabricate), parent_ctx=case.ctx, parent_bus=TurnEventBus())
    assert [item["kind"] for item in rows(case, "chat_reference_evidence")] == ["delegate_started"]
    assert rows(case, "turn_agent_execution")[0]["status"] == "running"


@pytest.mark.parametrize("failure", ["revoke", "output_insert", "lease"])
async def test_confirmed_model_receipt_does_not_bypass_output_permission_or_durability(runtime_case, failure):
    from bowhead_core.skill_instance_store import SkillInstanceStore

    case = runtime_case
    adapter = adapter_for("openai")

    async def respond(request):
        if failure == "revoke":
            await SkillInstanceStore(case.path).upsert(
                user_id=case.ctx.user_id, name="support", source_type="builtin", source_path=str(case.base.fixture.root / "support"), enabled=False
            )
        elif failure == "lease":
            expire(case)
        else:
            with sqlite3.connect(case.path) as db:
                db.execute(
                    "CREATE TRIGGER fail_mixed_output BEFORE INSERT ON chat_reference_evidence WHEN NEW.kind='output_received' BEGIN SELECT RAISE(ABORT,'private disk failure'); END"
                )
        return success("openai", "invoke")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=case.users)
            with pytest.raises(TerminalDurabilityAbort):
                await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            with pytest.raises(TerminalDurabilityAbort):
                await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert route.call_count == 1
        assert rows(case, "chat_reference_model_invocations")[0]["status"] == "completed"
        assert [item["kind"] for item in rows(case, "chat_reference_evidence")] == ["delegate_started"]
        assert rows(case, "turn_agent_execution")[0]["status"] == "running"
        expire(case)
        assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    finally:
        await adapter.close()


@pytest.mark.parametrize("change", ["missing_adapter", "source_db", "receiver", "owner"])
async def test_invalid_mixed_runtime_never_records_dispatch_or_calls_runner(runtime_case, change, tmp_path):
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    case = runtime_case
    runtime, ctx = case.runtime, case.ctx
    if change == "missing_adapter":
        runtime = replace(runtime, skill_sources=None)
    elif change == "source_db":
        sources = runtime.skill_sources
        runtime = replace(
            runtime,
            skill_sources=SkillReadSourceResolver(
                tmp_path / "other.db", access_factory=sources.access_factory, builtin_index=sources.builtin_index, builtin_root=sources.builtin_root
            ),
        )
    elif change == "receiver":
        ctx.session_id = "another-session"
    else:
        runtime = replace(runtime, owner_id="another-worker")
    seen = []

    async def forbidden(**kwargs):
        seen.append(kwargs)
        raise AssertionError("unexpected dispatch")

    with pytest.raises((TerminalDurabilityAbort, ValueError)):
        await runtime.run(runner=SimpleNamespace(run=forbidden), parent_ctx=ctx, parent_bus=TurnEventBus())
    assert not seen and rows(case, "chat_reference_evidence") == [] and rows(case, "chat_reference_model_invocations") == []
    assert not (tmp_path / "other.db").exists()


async def test_mixed_scope_cannot_replace_existing_physical_owner(runtime_case):
    from bowhead_router.invocation import physical_invocation_scope

    case = runtime_case
    seen = []

    async def forbidden(**kwargs):
        seen.append(kwargs)
        raise AssertionError("unexpected runner")

    with physical_invocation_scope(object()), pytest.raises(LLMInputAdmissionError, match="scope_conflict"):
        await case.runtime.run(runner=SimpleNamespace(run=forbidden), parent_ctx=case.ctx, parent_bus=TurnEventBus())
    assert seen == [] and rows(case, "chat_reference_model_invocations") == []


async def test_model_call_from_an_earlier_internal_scope_cannot_authorize_a_new_primary_dispatch(model_case):
    from test_skill_model_invocation import call, receiver, request, scope

    case = model_case
    base = case.case.case
    access = case.case.access
    adapter = adapter_for("openai")
    seen = []

    async def fabricated(**kwargs):
        seen.append(kwargs)
        return SubagentResult(
            profile=kwargs["profile_name"], subagent_id=kwargs["pre_minted_subagent_id"], ok=True, final_answer="unrelated to earlier response"
        )

    try:
        with respx.mock() as mock, receiver(access.handles[0].subagent_id), scope(case):
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=lambda _: success("openai", "invoke"))
            assert await call(router_for(adapter), request(case)) == "OK" and route.call_count == 1
        runtime = ReferencePrimaryRuntime(
            scope=access.scope,
            turn_id=access.turn_id,
            owner_id=access.owner_id,
            bindings=base.fixture.case.bindings,
            cipher=access.cipher,
            skill_sources=access.sources,
        )
        with pytest.raises(TerminalDurabilityAbort):
            await runtime.run(runner=SimpleNamespace(run=fabricated), parent_ctx=case.case.ctx, parent_bus=TurnEventBus())
        assert not seen and rows(base, "chat_reference_evidence") == []
        assert len(rows(base, "chat_reference_model_invocations")) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("boundary", ["flag", "missing_adapter", "missing_scope", "missing_session", "source_db"])
async def test_gateway_does_not_claim_mixed_work_without_its_exact_adapter(claimed_case, monkeypatch, tmp_path, boundary):
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_core.types import TurnRequest
    from bowhead_gateway.turn_service import admit_legacy_chat_execution, spawn_turn
    from cryptography.fernet import Fernet

    base = claimed_case
    case = base.fixture.case
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0" if boundary == "flag" else "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    events = EventBusRegistry(default_log_sink=OrderedTurnLogSink(TurnLogStore(base.path)))
    orchestrator = Orchestrator(
        router=None,
        datarc=None,
        session_store=case.store,
        tool_registry=build_default_registry(),
        event_registry=events,
        user_store=case.fixture.users,
    )
    orchestrator.dispatch_owner_id = "mixed-worker"
    request = TurnRequest(
        **case.scope.identity(),
        member_uid=case.fixture.member_id,
        turn_id="t_reference",
        text=case.args["user_text"],
        reference_scope=case.scope,
        planning_mode="off",
    )
    sources = base.fixture.sources
    if boundary == "missing_scope":
        request.reference_scope = None
    elif boundary == "missing_session":
        request.session_id = None
    elif boundary == "missing_adapter":
        sources = None
    elif boundary == "source_db":
        sources = SkillReadSourceResolver(tmp_path / "other.db", access_factory=sources.access_factory, builtin_index=sources.builtin_index)
    bus = await admit_legacy_chat_execution(orchestrator, request)
    task = None
    try:
        with pytest.raises(ValueError):
            task = await spawn_turn(orchestrator, request, team_id=request.team_id, admitted_legacy_bus=bus, reference_skill_sources=sources)
        assert rows(base, "chat_reference_evidence") == rows(base, "chat_reference_model_invocations") == []
        assert rows(base, "turn_agent_execution")[0]["status"] == "admitted"
        assert rows(base, "chat_turn_spawn_dispatch")[0]["owner_id"] is None
        assert not (tmp_path / "other.db").exists()
        assert [event async for event in bus.subscribe()] == []
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("claimed_case", ["canonical_input", "agent_baseline"], indirect=True)
async def test_gateway_mixed_scheduler_finishes_original_execution_and_durable_tail(claimed_case, monkeypatch):
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_core.types import TurnRequest
    from bowhead_gateway.turn_service import admit_legacy_chat_execution, spawn_turn

    base = claimed_case
    case = base.fixture.case
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    # The scheduler uses the existing instance root. Preparation and output use
    # that same key; this test's key was generated only for its isolated store.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    adapter = adapter_for("openai")
    router = router_for(adapter)
    registry = build_default_registry()
    events = EventBusRegistry(default_log_sink=OrderedTurnLogSink(TurnLogStore(base.path)))
    orchestrator = Orchestrator(
        router=router, datarc=None, session_store=case.store, tool_registry=registry, event_registry=events, user_store=case.fixture.users
    )
    orchestrator.dispatch_owner_id = "mixed-worker"
    orchestrator.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    # Gateway startup always installs this factory (main.py). Without it, the team-scoped
    # pre-terminal steer materializer refuses and leaves the finished turn drain-pending.
    orchestrator.reference_skill_sources_factory = lambda _scope: base.fixture.sources
    request = TurnRequest(
        **case.scope.identity(),
        member_uid=case.fixture.member_id,
        turn_id="t_reference",
        text=case.args["user_text"],
        reference_scope=case.scope,
        planning_mode="off",
        team_router=router,
    )
    task = monitor = None
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=lambda _: success("openai", "invoke"))
            bus = await admit_legacy_chat_execution(orchestrator, request)
            task = await spawn_turn(
                orchestrator, request, team_id=request.team_id, admitted_legacy_bus=bus, reference_skill_sources=base.fixture.sources
            )
            from bowhead_gateway.routes.chat import _inflight_tasks

            monitor = next(t for t in _inflight_tasks if t.get_name() == "turn_dispatch_lease:t_reference")
            await asyncio.wait_for(asyncio.gather(task, monitor), timeout=30)
            assert route.call_count == 1
        saved = rows(base, "turn_events")
        terminal = [json.loads(item["payload_json"]) for item in saved if item["event_type"] == "done"]
        assert len(terminal) == 1 and terminal[0]["status"] == "completed"
        assert rows(base, "turn_agent_execution")[0]["status"] == "completed"
        assert rows(base, "chat_turn_spawn_dispatch")[0]["state"] == "terminal"
        assert rows(base, "chat_reference_model_invocations")[0]["status"] == "completed"
        replay = [event async for event in bus.subscribe()]
        assert [event.payload["text"] for event in replay if event.event_type == "final_answer"][-1] == "OK"
        with sqlite3.connect(base.path) as db:
            assert db.execute("SELECT status,final_answer FROM turns WHERE id='t_reference'").fetchone() == ("completed", "OK")
            assert db.execute("SELECT active_turn_id FROM sessions WHERE id='s_reference'").fetchone() == (None,)
    finally:
        for pending in (task, monitor):
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await adapter.close()
