"""Orchestrator 端到端测试（真 DatarcClient → bi_mock；Router 用 MockAdapter scripted）."""

from __future__ import annotations

import asyncio

import pytest
from bi_mock.main import app as bi_mock_app
from fastapi.testclient import TestClient

from bowhead_bi_bridge import DatarcClient
from bowhead_core import (
    EventBusRegistry,
    Orchestrator,
    OrchestratorConfig,
    TurnRequest,
    TurnStatus,
    build_default_registry,
)
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
)


@pytest.fixture
def bi_mock_client() -> TestClient:
    """启动 in-memory bi_mock app 作为 datarc 的替身."""
    return TestClient(bi_mock_app)


@pytest.fixture
async def datarc_pointing_to_mock(bi_mock_client: TestClient, monkeypatch):
    """Inject a DatarcClient whose HTTPX2 transport targets the bi_mock app."""
    import httpx2

    # Bypass the real network: replace DatarcClient's AsyncClient with a TestClient-style client
    transport = httpx2.ASGITransport(app=bi_mock_app)

    class _MockDatarcClient(DatarcClient):
        def __init__(self) -> None:  # type: ignore[no-untyped-def]
            self._base_url = "http://bi-mock/"
            self._auth_key = "dev"
            self._timeout = 10
            self._http = httpx2.AsyncClient(
                transport=transport,
                base_url=self._base_url,
                headers={
                    "Authorization": "Bearer dev",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=10,
            )

    client = _MockDatarcClient()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def scripted_router() -> tuple[Router, MockAdapter]:
    """返回一个 Router + 可编辑的 MockAdapter."""
    # Fill scripted_responses later in each test
    adapter = MockAdapter(provider_name="p1")
    registry = ProviderRegistry()
    # Use type "mock" to bypass openai_compatible base_url validation
    registry.load([ProviderConfig(name="p1", type="mock")])
    # Replace the default MockAdapter with our preconfigured scripted adapter
    registry._adapters["p1"] = adapter  # type: ignore[attr-defined]

    router = Router(registry)
    for agent_type in ("lead", "modeler", "analyst", "echo", "judge"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))

    return router, adapter


@pytest.mark.integration
@pytest.mark.skip(reason="OBSOLETE: bi_ask_question 已 P13.5 砍, test 用 bi_query 重写 follow-up")
async def test_vertical_slice_lookup_query(
    datarc_pointing_to_mock: DatarcClient,
    scripted_router: tuple[Router, MockAdapter],
) -> None:
    """垂直切片：lookup intent → LLM 决策 tool call → datarc → 最终答案."""
    router, adapter = scripted_router

    # Scripted LLM responses:
    # 1. First: the LLM decides to call bi.ask_question
    # 2. Second: the LLM receives the tool result and generates the final answer
    adapter.scripted_responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="bi_ask_question",
                    arguments={
                        "workspace_id": "1",
                        "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                        "text": "今年 GMV",
                    },
                )
            ],
        ),
        LLMResponse(
            content="根据 BI，今年营业额是 29.3 亿（数据来源：https://datarc.cn/...）",
        ),
    ]

    orchestrator = Orchestrator(
        router=router,
        datarc=datarc_pointing_to_mock,
        tool_registry=build_default_registry(),
        event_registry=EventBusRegistry(),
        config=OrchestratorConfig(max_tool_iterations=3),
    )

    # Subscribe to events
    req = TurnRequest(
        session_id="s_test",
        workspace_id="1",
        user_id="u_alice",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        text="今年 GMV 是多少？",
    )
    bus = await orchestrator.event_registry.get_or_create(req.turn_id)

    events: list = []

    async def collect() -> None:
        async for event in bus.subscribe():
            events.append(event)

    collector_task = asyncio.create_task(collect())

    result = await orchestrator.run_turn(req)

    # Wait for the subscriber to receive the sentinel and exit
    await asyncio.wait_for(collector_task, timeout=5.0)

    # ─── Assertions ──────────────────────────────────────────
    assert result.status == TurnStatus.COMPLETED
    assert "营业额" in result.final_answer or "29.3" in result.final_answer
    assert len(result.tool_invocations) == 1
    inv = result.tool_invocations[0]
    assert inv.tool_name == "bi_ask_question"
    assert inv.error is None
    assert inv.result is not None
    assert inv.result["type"] == "metric"

    # Event sequence (Sprint θ Phase 8c-B adapted):
    # turn_started (turn_runner) → agent_spawned (Lead/SubagentRunner) →
    # tool_call → tool_result → done (turn_runner wrap)
    # intent_classified / pycell.in/out / partial_answer moved into SubagentRunner
    # internal observability and are no longer emitted to the top-level parent_bus.
    event_types = [e.event_type for e in events]
    assert event_types[0] == "turn_started"
    assert "agent_spawned" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"


@pytest.mark.integration
# Sprint θ Phase 8c-B: test_meta_canvas_patch_shortcircuits + test_ambiguous_query_asks_clarification
# DELETED — tested legacy _handle_canvas_side_effect / _handle_ambiguous_hitl shortcuts which are
# unreachable per A2 strict (all turns through lead_orchestrator).
# Lead's clarification_tool sentinel path covers the ambiguous case via
# tests/test_clarification_tool.py (heuristic pre-gate removed on 2026-09-08).


# #71 attachment hard-interception helpers
def test_data_attachment_ids_detects_csv():
    from dataclasses import dataclass

    # Contract update on 2026-07-14: helpers have public names and live in turn_runner (re-exports removed)
    from bowhead_core.turn_runner import data_attachment_ids

    @dataclass
    class _P:
        artifact_id: str
        filename: str
        mime: str

    prepared = [
        _P(artifact_id="art_1", filename="data.csv", mime="text/csv"),
        _P(artifact_id="art_2", filename="doc.pdf", mime="application/pdf"),
        _P(artifact_id="art_3", filename="sheet.xlsx", mime="application/octet-stream"),  # Detect by extension
    ]
    ids = data_attachment_ids(prepared)
    assert "art_1" in ids
    assert "art_3" in ids  # xlsx by extension
    assert "art_2" not in ids  # pdf not data


def test_user_explicitly_wants_bi_bypass():
    from bowhead_core.turn_runner import user_explicitly_wants_bi

    assert user_explicitly_wants_bi("基于附件名单，去 datarc 查他们今年消费") is True
    assert user_explicitly_wants_bi("分析这份附件合规") is False
    assert user_explicitly_wants_bi("from datarc pull last year") is True
