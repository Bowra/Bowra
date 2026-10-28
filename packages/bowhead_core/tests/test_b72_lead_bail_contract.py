"""Peer clarification handoff.

Scripted model responses exercise real Lead dispatch, peer return, card storage,
and pause events. Boundary cases also cover malformed payloads and store failure
at the legacy peer-bail consumer.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult
from bowhead_core.types import IntentType, TurnStatus

# ── Shared fakes ────────────────────────────────────────────────────────


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def find_all(self, event_type: str) -> list[Any]:
        return [e for e in self.events if e.event_type == event_type]


@dataclass
class _FakeSubagentRunner:
    result: SubagentResult | None = None

    async def run(
        self,
        *,
        profile_name: str,
        task: str,
        parent_turn_id: str,
        parent_bus: Any,
        parent_ctx: Any,
        extra_instructions: str | None = None,
        state: Any = None,
        stream_callback: Any = None,
        stream_on_tool_call: Any = None,
        **_kwargs: Any,
    ) -> SubagentResult:
        if self.result is not None:
            return self.result
        return SubagentResult(
            profile=profile_name,
            subagent_id="sub_test",
            ok=True,
            final_answer="default",
        )


def _make_sctx(turn_id: str = "t_b72_contract") -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u1",
        workspace_id="ws1",
        session_id="s1",
        turn_id=turn_id,
        role="user",
    )


def _make_ctx(store: ClarificationStore | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        clarification_store=store,
        _m137_subagent_payloads=[],
        execution_generation=1,
        execution_owner_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("question_count", [1, 2])
@pytest.mark.parametrize("storage_failure", [None, "put", "update"])
async def test_peer_question_through_real_lead_dispatch(tmp_path, monkeypatch, question_count, storage_failure) -> None:
    """Only the model is scripted: dispatch, return, persistence and card are real."""
    from bowhead_core.subagent import SubagentRunner, register_subagent_tool
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    from bowhead_router import AgentBinding, LLMChunk, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, TokenUsage, ToolCall

    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "1")
    questions = [
        {"question": "分析哪个期间？", "options": ["今年", "去年"], "type": "single_select"},
        {"question": "分析哪些区域？", "options": ["华东", "华南"], "type": "multi_select"},
    ][:question_count]

    class QuestionAdapter(MockAdapter):
        async def stream(self, req):
            response = await self.invoke(req)
            if response.content:
                yield LLMChunk(delta=response.content)
            for call in response.tool_calls:
                yield LLMChunk(tool_call_delta=call)
            yield LLMChunk(finish_reason=response.finish_reason, usage=response.usage)

    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=("subagent_spawn", "clarification_ask"),
            system_prompt="Delegate analysis to compute.",
            max_iterations=4,
        ),
    )
    adapter = QuestionAdapter(provider_name="questions", raise_on_exhaust=True)
    adapter.scripted_responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="spawn", name="subagent_spawn", arguments={"profile": "compute", "task": "分析利润"})],
            usage=TokenUsage(input_tokens=11, output_tokens=7),
        ),
        LLMResponse(
            content="",
            usage=TokenUsage(input_tokens=23, output_tokens=13),
            tool_calls=[
                ToolCall(
                    id="ask",
                    name="subagent_return",
                    arguments={
                        "payload": {
                            "error": {"kind": "needs_user_input", "message": "请确定分析期间"},
                            "clarification_payload": {"questions": questions},
                        }
                    },
                )
            ],
        ),
        LLMResponse(content="请确定分析期间", tool_calls=[]),
    ]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="questions", type="mock")])
    providers._adapters["questions"] = adapter
    router = Router(providers)
    for agent_type in ("lead", "analyst", "compute"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("questions", "mock")))
    registry = build_default_registry()
    register_subagent_tool(registry)
    register_subagent_return_tool(registry)
    runner = SubagentRunner(router=router, tool_registry=registry)
    store = ClarificationStore(db_path=tmp_path / "questions.db")
    await store.initialize()
    original_put = store.put

    async def atomic_question(**kwargs):
        if storage_failure == "put":
            raise OSError("question storage unavailable")
        saved = await original_put(**kwargs)
        persisted = await store.get(saved.request_id)
        assert persisted.resume_context["user_text"] == "分析利润"
        assert persisted.resume_context["parent_turn_id"] == "t_real_peer_question"
        return saved

    monkeypatch.setattr(store, "put", atomic_question)
    updates = []
    if storage_failure == "update":

        async def failed_update(*args, **kwargs):
            updates.append(args)
            raise OSError("later context enrichment failed")

        monkeypatch.setattr(store, "set_resume_context", failed_update)
    bus = _FakeBus()
    sctx = _make_sctx("t_real_peer_question")
    ctx = ToolExecutionContext(
        workspace_id="ws1",
        member_uid="member1",
        user_id="u1",
        turn_id=sctx.turn_id,
        datarc=None,
        clarification_store=store,
        subagent_runner=runner,
        parent_bus=bus,
        user_text="分析利润",
    )
    result = await _lead_loop(
        sctx, "分析利润", LeadRuntime(router=router, tool_registry=registry, parent_bus=bus, parent_ctx=ctx, subagent_runner=runner)
    )

    if storage_failure == "put":
        assert result.status == TurnStatus.COMPLETED
        assert not bus.find_all("clarification_required")
        assert not await store.get_pending_for_turn(sctx.turn_id)
        for question in questions:
            assert question["question"] in result.final_answer
        return

    assert result.status == TurnStatus.CLARIFICATION_PENDING, [(e.event_type, e.payload) for e in bus.events]
    assert not updates  # A new card must not depend on a second context write.
    assert len(bus.find_all("peer_dispatched")) == 1
    cards = bus.find_all("clarification_required")
    assert len(cards) == 1
    assert cards[0].payload["questions"] == questions
    assert cards[0].payload["multi_select"] is (question_count == 2)
    assert adapter._script_index == 2  # Lead must pause without another model call.
    assert (result.tokens_input, result.tokens_output) == (34, 20)
    done = bus.find_all("done")
    assert len(done) == 1
    assert (done[0].payload["tokens_input"], done[0].payload["tokens_output"]) == (34, 20)
    assert not bus.find_all("lead.final.payload")
    pending = await store.get_pending_for_turn(sctx.turn_id)
    assert len(pending) == 1
    assert pending[0].request_id == cards[0].payload["clarification_id"]
    assert pending[0].resume_context["user_text"] == "分析利润"
    assert pending[0].resume_context["parent_turn_id"] == sctx.turn_id


@pytest.fixture(autouse=True)
def _ensure_lead_profile():
    pre = "lead" in PROFILES
    if not pre:
        PROFILES["lead"] = SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=("subagent_return", "clarification_ask"),
            system_prompt="(stub)",
            max_iterations=4,
        )
    try:
        yield
    finally:
        if not pre:
            PROFILES.pop("lead", None)


@pytest.fixture
def _stream_env(monkeypatch):
    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")


# ── Contract: peer bails with needs_user_input + clarification_payload ──


@pytest.mark.asyncio
async def test_b72_contract_needs_user_input_emits_card_and_pending(_stream_env, monkeypatch) -> None:
    """Peer bail with `needs_user_input` kind + B5 clarification_payload:
    Lead emits `clarification_required` SSE with the right shape AND returns
    TurnResult with CLARIFICATION_PENDING. Real contract, not source grep."""
    bus = _FakeBus()
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        original_put = store.put

        async def atomic_question(**kwargs):
            saved = await original_put(**kwargs)
            # Inspect the committed row before Lead can perform another write.
            persisted = await store.get(saved.request_id)
            assert persisted.resume_context["user_text"] == "客单价同比"
            assert persisted.resume_context["parent_turn_id"] == "t_b72_contract_1"
            return saved

        monkeypatch.setattr(store, "put", atomic_question)
        ctx = _make_ctx(store=store)
        sctx = _make_sctx("t_b72_contract_1")

        peer_payload = {
            "error": {
                "kind": "needs_user_input",
                "message": "客单价有 2 种公式候选, 用户得选",
                "clarification_payload": {
                    "questions": [
                        {
                            "question": "客单价 哪种公式?",
                            "options": ["GMV / 订单数", "净销售额 / 订单数"],
                            "type": "single_select",
                        }
                    ],
                },
            },
        }
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_x",
            ok=True,
            final_answer="(unused)",
            tokens_input=10,
            tokens_output=5,
            tool_invocations=[],
            payload=peer_payload,
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        turn_result = await _lead_loop(sctx, "客单价同比", runtime)

        # Contract #1: SSE emitted with proper payload shape
        clar_events = bus.find_all("clarification_required")
        assert len(clar_events) == 1
        payload = clar_events[0].payload
        assert payload["kind"] == "options"
        assert payload["candidates"] == ["GMV / 订单数", "净销售额 / 订单数"]
        assert payload["multi_select"] is False  # single_select → False
        assert payload["fallback_text"] == "客单价 哪种公式?"
        assert "clarification_id" in payload
        assert payload["clarification_id"]  # non-empty (from store.put)

        # Contract #2: TurnResult suspends turn
        assert turn_result.status == TurnStatus.CLARIFICATION_PENDING
        assert turn_result.intent == IntentType.AMBIGUOUS

        # Contract #3: ClarificationStore row persisted (FE /resolve will find it)
        pending = await store.get_pending_for_turn("t_b72_contract_1")
        assert len(pending) == 1
        assert pending[0].kind == "ambiguous_turn"
        assert pending[0].resume_context["user_text"] == "客单价同比"
        assert pending[0].resume_context["session_id"] == "s1"

        # Contract #4: done event with correct status + error_kind
        done_events = bus.find_all("done")
        assert len(done_events) == 1
        assert done_events[0].payload["status"] == "clarification_pending"
        assert done_events[0].payload["error_kind"] == "needs_user_input"


@pytest.mark.asyncio
async def test_b72_contract_multi_select_propagates(_stream_env) -> None:
    """Type=multi_select → multi_select=True in SSE emit + store row."""
    bus = _FakeBus()
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(store=store)
        sctx = _make_sctx("t_b72_contract_2")

        peer_payload = {
            "error": {
                "kind": "needs_user_input",
                "message": "选 ≥1 个想看的维度",
                "clarification_payload": {
                    "questions": [
                        {
                            "question": "选 ≥1 个想看的维度",
                            "options": ["销售", "成本", "库存"],
                            "type": "multi_select",
                        }
                    ],
                },
            },
        }
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_y",
            ok=True,
            final_answer="",
            tool_invocations=[],
            payload=peer_payload,
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        await _lead_loop(sctx, "看什么数据", runtime)

        clar_events = bus.find_all("clarification_required")
        assert len(clar_events) == 1
        assert clar_events[0].payload["multi_select"] is True

        # multi_select on the store row, too
        pending = await store.get_pending_for_turn("t_b72_contract_2")
        assert len(pending) == 1
        assert pending[0].multi_select is True


@pytest.mark.asyncio
async def test_b72_contract_missing_payload_falls_through_to_prose(_stream_env) -> None:
    """Risk-P1-1 fallback: peer says needs_user_input but no clarification_payload
    → Lead emits prose final_answer (not clarification_required). TurnResult
    COMPLETED, not CLARIFICATION_PENDING."""
    bus = _FakeBus()
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(store=store)
        sctx = _make_sctx("t_b72_contract_3")

        peer_payload = {
            "error": {
                "kind": "needs_user_input",
                "message": "peer 想问但没拼好 payload (LLM 没遵守 prompt)",
                # clarification_payload missing
            },
        }
        sub_result = SubagentResult(
            profile="lead",
            subagent_id="sub_z",
            ok=True,
            final_answer="",
            tool_invocations=[],
            payload=peer_payload,
        )
        runner = _FakeSubagentRunner(result=sub_result)
        runtime = LeadRuntime(
            router=SimpleNamespace(),
            tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
            parent_bus=bus,
            parent_ctx=ctx,
            subagent_runner=runner,
        )
        turn_result = await _lead_loop(sctx, "?", runtime)

        # No clarification_required emit (fell through)
        assert bus.find_all("clarification_required") == []
        # Prose final_answer emitted instead
        fa = bus.find_all("final_answer")
        assert len(fa) == 1
        assert "peer 想问" in fa[0].payload["text"]
        # Turn COMPLETED, not pending
        assert turn_result.status == TurnStatus.COMPLETED
        # No row in clarification store
        pending = await store.get_pending_for_turn("t_b72_contract_3")
        assert len(pending) == 0


@pytest.mark.asyncio
async def test_b72_contract_other_bail_kinds_still_emit_prose(_stream_env) -> None:
    """Existing bail kinds (domain_mismatch, ambiguity, transient) unchanged —
    prose path, no clarification card."""
    for bail_kind in ("domain_mismatch", "ambiguity", "transient", "tool_failure"):
        bus = _FakeBus()
        with tempfile.TemporaryDirectory() as td:
            store = ClarificationStore(db_path=Path(td) / "c.db")
            await store.initialize()
            ctx = _make_ctx(store=store)
            sctx = _make_sctx(f"t_b72_contract_kind_{bail_kind}")

            peer_payload = {
                "error": {
                    "kind": bail_kind,
                    "message": f"bail with {bail_kind}",
                },
            }
            sub_result = SubagentResult(
                profile="lead",
                subagent_id="sub_a",
                ok=True,
                final_answer="",
                tool_invocations=[],
                payload=peer_payload,
            )
            runner = _FakeSubagentRunner(result=sub_result)
            runtime = LeadRuntime(
                router=SimpleNamespace(),
                tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
                parent_bus=bus,
                parent_ctx=ctx,
                subagent_runner=runner,
            )
            turn_result = await _lead_loop(sctx, "test", runtime)

            assert bus.find_all("clarification_required") == [], f"bail_kind={bail_kind} should NOT trigger clarification_required emit"
            assert len(bus.find_all("final_answer")) == 1
            assert turn_result.status == TurnStatus.COMPLETED


# ── P9 SRE fix: store-fail fall-through to prose (no orphan UUID emit) ──


@pytest.mark.asyncio
async def test_b72_contract_store_unavailable_falls_through_to_prose(_stream_env) -> None:
    """P9 SRE-P1 fix: if ClarificationStore is None (or put() raises), Lead
    must NOT emit clarification_required with a local UUID — that re-creates
    the A1 P0 prod 404 (FE clicks resolve → gateway 404). Must fall through
    to prose instead.
    """
    bus = _FakeBus()
    # No clarification store on ctx (simulates failure / unwired state)
    ctx = _make_ctx(store=None)
    sctx = _make_sctx("t_b72_contract_store_unavail")

    peer_payload = {
        "error": {
            "kind": "needs_user_input",
            "message": "想问用户",
            "clarification_payload": {
                "questions": [
                    {
                        "question": "选?",
                        "options": ["A", "B"],
                        "type": "single_select",
                    }
                ],
            },
        },
    }
    sub_result = SubagentResult(
        profile="lead",
        subagent_id="sub_n",
        ok=True,
        final_answer="",
        tool_invocations=[],
        payload=peer_payload,
    )
    runner = _FakeSubagentRunner(result=sub_result)
    runtime = LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        subagent_runner=runner,
    )
    turn_result = await _lead_loop(sctx, "?", runtime)

    # No clarification_required emit when store unavailable
    assert bus.find_all("clarification_required") == [], "P9 SRE-P1: must NOT emit orphan-uuid clarification when store unavailable"
    # Fell through to prose
    assert len(bus.find_all("final_answer")) == 1
    assert turn_result.status == TurnStatus.COMPLETED
