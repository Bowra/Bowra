"""Local, opt-in live check for exact Agent + Skill clarification."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.agents.lead_orchestrator import LeadRuntime, run_turn
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner, register_subagent_tool
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import build_default_registry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from test_reference_primary_runtime import _ctx, _rows
from test_skill_read_model_input import Registry
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_start_admission import candidate
from test_skill_start_admission import start_case as _start_case

from bowhead_router import AgentBinding

start_case = _start_case
receiver_case = _receiver_case
pytestmark = [
    pytest.mark.live,
    pytest.mark.slow,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


async def test_live_predispatch_card(receiver_case, monkeypatch):
    key = os.environ.get("ALIYUN_DASHSCOPE_API_KEY")
    env_path = os.environ.get("BOWHEAD_LOCAL_ENV_FILE")
    if not key and env_path:
        key = dotenv_values(Path(env_path)).get("ALIYUN_DASHSCOPE_API_KEY")
    if not key:
        pytest.skip("Set ALIYUN_DASHSCOPE_API_KEY or BOWHEAD_LOCAL_ENV_FILE")

    fixture = receiver_case
    case = fixture.case
    admission = case.args["reference_start"]
    nodes = list(admission.input_document.nodes)
    nodes[-1] = nodes[-1].model_copy(
        update={
            "text": "2026年虚构门店营业收入200、净利润20、总资产100。使用所选杜邦方法分析；平均所有者权益缺失，"
            "请先问我这个数值，等我回答后再继续计算，不要自行假设。"
        }
    )
    document = admission.input_document.model_copy(update={"nodes": nodes})
    case.args["reference_start"] = replace(admission, input_document=document)
    await candidate(fixture, agent_first=True)
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="worker-a",
    )
    runtime = ReferencePrimaryRuntime(
        scope=case.scope, turn_id="t_reference", owner_id="worker-a", bindings=case.bindings, cipher=cipher, skill_sources=fixture.sources
    )
    primary = await runtime.validate()
    ctx = _ctx(case)
    ctx.session_store = case.store
    ctx._reference_runtime = runtime
    store = ClarificationStore(case.fixture.db_path)
    await store.initialize()
    ctx.clarification_store = store
    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=("subagent_spawn", "clarification_ask"),
            system_prompt="Delegate then summarize.",
            max_iterations=4,
        ),
    )
    adapter = OpenAICompatibleAdapter(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=str(key),
        provider_name="primary",
        provider_options={"models": [{"id": "qwen3.6-flash"}]},
    )
    captured_inputs = []
    original_invoke = adapter.invoke
    original_stream = adapter.stream

    async def capture_invoke(request, *args, **kwargs):
        captured_inputs.append(str(request))
        return await original_invoke(request, *args, **kwargs)

    async def capture_stream(request, *args, **kwargs):
        captured_inputs.append(str(request))
        async for chunk in original_stream(request, *args, **kwargs):
            yield chunk

    monkeypatch.setattr(adapter, "invoke", capture_invoke)
    monkeypatch.setattr(adapter, "stream", capture_stream)
    from bowhead_router import Router

    router = Router(Registry(primary=adapter))
    for agent_type in ("lead", "compute", "analyst"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("primary", "qwen3.6-flash")))
    router.set_context_windows({("primary", "qwen3.6-flash"): 1000000})
    registry = build_default_registry()
    register_subagent_tool(registry)
    register_subagent_return_tool(registry)
    register_clarification_tool(registry)
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    log = TurnLogStore(case.fixture.db_path)
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(log), persist_before_live=True)
    ctx.subagent_runner = runner
    ctx.parent_bus = bus

    async def execute(active_runtime, active_ctx, active_bus):
        return await run_turn(
            SerializableTurnContext(**case.scope.identity(), turn_id="t_reference", role="user"),
            primary.source.user_text,
            runtime=LeadRuntime(
                router=router,
                tool_registry=registry,
                parent_bus=active_bus,
                parent_ctx=active_ctx,
                subagent_runner=runner,
                session_store=case.store,
                persist_turn_state=lambda **kwargs: active_runtime.persist_turn_state(store=case.store, **kwargs),
            ),
        )

    try:
        result = await execute(runtime, ctx, bus)
        assert result.status == "clarification_pending"
        cards = _rows(case, "clarification_requests")
        assert len(cards) == 1
        assert _rows(case, "turn_agent_execution")[0]["status"] == "admitted"
        assert (await store.resolve(cards[0]["request_id"], "50")).status == "resolved"
        assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
        assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=cards[0]["request_id"]) == 2
        resumed = replace(runtime, generation=2, owner_id="worker-resume")
        await resumed.claim_bi_resume(clarification_id=cards[0]["request_id"], lease_seconds=300)
        resumed_ctx = _ctx(case)
        resumed_ctx.session_store = case.store
        resumed_ctx._reference_runtime = resumed
        resumed_ctx.clarification_store = store
        resumed_bus = TurnEventBus(log_sink=OrderedTurnLogSink(log), persist_before_live=True)
        resumed_ctx.subagent_runner = runner
        resumed_ctx.parent_bus = resumed_bus
        completed = await execute(resumed, resumed_ctx, resumed_bus)
        assert completed.status == "completed", completed.error
        assert "40%" in completed.final_answer
        assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
        assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
        assert any("BODY_method" in request and "BODY_support" in request for request in captured_inputs)
    finally:
        await adapter.close()
