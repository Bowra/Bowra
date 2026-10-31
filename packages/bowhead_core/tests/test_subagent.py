"""Subagent profile + runner + spawn tool 集成.

测试同时跑 ``legacy`` / ``langgraph`` 两个 backend —— 保证 Path B+ 迁移过程
双轨等价。LangGraph 路径比 legacy 多一次 LLM 调用（`find_sop` 选 SOP 名），
所以 scripted responses 需要用 ``_scripted_for`` helper 在 langgraph 下预置
一条 "analyst-sop-business-review"。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import bowhead_core.subagent as subagent_mod
import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_core.agent_execution_binding import AgentExecutionBindingError
from bowhead_core.agent_role_candidates import (
    LeadRoleCandidateRoster,
    new_turn_candidate,
)
from bowhead_core.agents.lead_orchestrator import _emit_event
from bowhead_core.attachments_injector import PreparedAttachment
from bowhead_core.events import (
    OrchestratorEvent,
    TerminalDurabilityAbort,
    TurnEventBus,
)
from bowhead_core.subagent import (
    PROFILES,
    SubagentBackend,
    SubagentProfile,
    SubagentResult,
    SubagentRunner,
    register_subagent_tool,
)
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)
from bowhead_router.types import ImageRef, ToolSchema

from bowhead_bi_bridge import DatarcClient
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
    ToolCall,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _router(*rs: LLMResponse) -> Router:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = list(rs)
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "modeler", "analyst", "echo", "judge"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


def _scripted_for(backend: SubagentBackend, *step_rs: LLMResponse) -> Router:
    """组装 scripted router：step_rs 就是主循环的 LLM 响应。

    这里曾按 backend 分叉（langgraph 那支要在最前面多注入一条 find_sop 响应）。
    LangGraph backend 已于 2026-08 下线（Agent SDK 调研 A2），只剩 legacy 一支。
    """
    return _router(*step_rs)


@pytest.fixture(params=["legacy"])
def backend(request) -> SubagentBackend:
    """曾经参数化 legacy/langgraph 两个 backend。langgraph 下线后只剩一档；
    保留 fixture 形状是为了不去改动几十个用例的签名（改动面换零收益）。"""
    return request.param  # type: ignore[no-any-return]


@pytest.fixture
async def dc():
    import httpx2

    class _M(DatarcClient):
        def __init__(self) -> None:
            # ⚠ Use the real __init__: this stub formerly hand-built four attributes without super(),
            # so every new client instance attribute (such as the 2026-08 measure-format cache)
            # silently broke it: list_measures raised AttributeError and the BI tools failed in a way
            # unrelated to the code under test. Replace only transport; do not recreate construction.
            super().__init__("http://bi-mock/", "dev", timeout_sec=10)
            self._http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=bi_mock_app),
                base_url="http://bi-mock/",
                headers={"Authorization": "Bearer dev"},
                timeout=10,
            )

    c = _M()
    try:
        yield c
    finally:
        await c.aclose()


# ─────────────────────────────────────────────────────────────
# Original six tests: parameterized legacy / langgraph
# ─────────────────────────────────────────────────────────────
def test_profiles_registered() -> None:
    assert "compute" in PROFILES
    # modeler removed on 2026-07-21 with all four BI write tools.
    assert "modeler" not in PROFILES
    # compute must have bi.query (unified entry replacing ask_question/fetch_rows/breakdown).
    assert "bi_query" in PROFILES["compute"].tool_whitelist
    assert "bi_schema" in PROFILES["compute"].tool_whitelist


def test_every_profile_still_advertises_at_least_one_tool() -> None:
    """每个 profile 被 spawn 后必须至少还看得见 1 个工具。

    2026-07-21 事故：给 4 个 BI 写 stub 加 `visible_to_llm=False` 时，`modeler`
    的 6 个白名单工具里另外 2 个本就 hidden → 它的可见工具集**从 4 掉到 0**，
    等于把 profile 改废了；而 `subagent.py` 对此只有一条
    `subagent.no_tools_whitelisted` **warning，不是闸** —— 于是这个回归穿过了
    全部 8000+ 测试。本守卫把那条 warning 变成闸。

    判据刻意保守（只用 base + canvas registry，不拉 gateway 的 14 个 registrar）：
    只有当某 profile「引用了确实注册着的工具、却一个都不可见」时才红 —— 这正是
    上述事故形状；whitelist 全靠 gateway 侧注册的 profile 无法判断，跳过而非误报。
    """
    from bowhead_core.capability_resolver import advertised_tool_schemas

    reg = build_default_registry()
    for mod, fn in (("bowhead_core.canvas_tools", "register_canvas_tools"),):
        with contextlib.suppress(Exception):
            getattr(__import__(mod, fromlist=[fn]), fn)(reg)

    for name, profile in PROFILES.items():
        known = [t for t in profile.tool_whitelist if t in reg._specs]
        if not known:
            continue  # Entire allowlist lives in the gateway registrar; this registry cannot judge it.
        visible = advertised_tool_schemas(reg, profile.tool_whitelist, profile_name=name, role=None)
        assert visible, f"profile {name!r} 引用了 {len(known)} 个已注册工具但一个都不可见（全被 visible_to_llm 过滤）——该 profile 已被改废"


async def test_subagent_direct_answer(backend, dc) -> None:
    """Subagent LLM 第一步就直答（不调 tool）."""
    router = _scripted_for(backend, LLMResponse(content="Q1 GMV growth rate = 12.3%"))
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend=backend)
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    result = await runner.run(
        profile_name="compute",
        task="算 Q1 GMV 同比增速",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    assert result.ok
    assert "12.3%" in result.final_answer
    assert result.subagent_id.startswith("sub_")


@pytest.mark.parametrize("tool_available", [True, False])
async def test_known_url_answer_requires_successful_page_read(backend, dc, monkeypatch, tool_available: bool) -> None:
    profile = SubagentProfile(
        name="web-source-test",
        llm_agent_type="analyst",
        tool_whitelist=("web_fetch",) if tool_available else ("web_search",),
        system_prompt="Read the supplied page before reporting its contents.",
        max_iterations=2,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    registry = ToolRegistry()

    async def _fetch(_args, _ctx):  # type: ignore[no-untyped-def]
        return {"url": "https://example.com", "content": "Example Domain"}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="web_fetch", description="Read page", input_schema={"type": "object"}),
            handler=_fetch,
        )
    )
    router = _scripted_for(
        backend,
        LLMResponse(content="The page says Example Domain."),
        LLMResponse(content="The page says Example Domain."),
    )
    bus = TurnEventBus()
    result = await SubagentRunner(router=router, tool_registry=registry, backend=backend).run(
        profile_name=profile.name,
        task="核对 https://example.com 的公告内容",
        parent_turn_id="t_url",
        parent_bus=bus,
        parent_ctx=ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t_url", datarc=dc),
    )

    assert result.ok is False
    assert result.error == "subagent_source_unverified"
    assert result.final_answer == ""
    events = await _collect_events(bus)
    assert any(event.event_type == "agent_spawned" and event.payload.get("status") == "failed" for event in events)
    assert not any(event.event_type == "agent_spawned" and event.payload.get("status") == "completed" for event in events)
    assert not any(event.event_type == "partial_answer" for event in events)


@pytest.mark.parametrize("fetch_ok", [True, False])
@pytest.mark.parametrize("task", ["核对 https://example.com 的公告内容", "请读取 https://example.com/，只列页面标题和事实"])
async def test_known_url_fetch_allows_answer_only_on_success(backend, dc, monkeypatch, fetch_ok: bool, task: str) -> None:
    profile = SubagentProfile(
        name="web-source-test",
        llm_agent_type="analyst",
        tool_whitelist=("web_fetch",),
        system_prompt="Read the supplied page before reporting its contents.",
        max_iterations=2,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    registry = ToolRegistry()

    async def _fetch(_args, _ctx):  # type: ignore[no-untyped-def]
        return {"url": "https://example.com", "content": "Example Domain"} if fetch_ok else {"error": "http 503"}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="web_fetch", description="Read page", input_schema={"type": "object"}),
            handler=_fetch,
        )
    )
    router = _scripted_for(
        backend,
        LLMResponse(content="", tool_calls=[ToolCall(id="c_fetch", name="web_fetch", arguments={"url": "https://example.com"})]),
        LLMResponse(content="The page says Example Domain."),
    )
    bus = TurnEventBus()
    result = await SubagentRunner(router=router, tool_registry=registry, backend=backend).run(
        profile_name=profile.name,
        task=task,
        parent_turn_id="t_url",
        parent_bus=bus,
        parent_ctx=ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t_url", datarc=dc),
    )

    assert result.ok is fetch_ok
    assert result.final_answer.strip() == ("The page says Example Domain." if fetch_ok else "")
    assert [inv.tool_name for inv in result.tool_invocations] == ["web_fetch"]


async def test_lead_receives_prepared_vision_images(backend, dc) -> None:
    """Both runtimes must forward authorized image bytes, not only attachment XML."""

    class _CapturingRouter:
        def __init__(self) -> None:
            self.requests = []

        async def invoke(self, _agent_type, request, **_kwargs):
            self.requests.append(request)
            if request.trace_id.endswith(":find_sop"):
                return LLMResponse(content="analyst-sop-business-review")
            return LLMResponse(content="vision-ok")

    router = _CapturingRouter()
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t_vision",
        datarc=dc,
    )
    ctx._psi_parsed_results = [
        PreparedAttachment(
            artifact_id="art_vision",
            filename="chart.png",
            mime="image/png",
            size=3,
            extracted_text="",
            truncated=False,
            error=None,
            summary_only=True,
            image_base64="YWJj",
        )
    ]

    result = await SubagentRunner(
        router=router,  # type: ignore[arg-type]
        tool_registry=build_default_registry(),
        backend=backend,
    ).run(
        profile_name="lead",
        task="read the chart",
        parent_turn_id="t_vision",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )

    assert result.ok
    vision_requests = [request for request in router.requests if any(message.images for message in request.messages)]
    assert len(vision_requests) == 1
    user_message = next(message for message in vision_requests[0].messages if message.images)
    image = user_message.images[0]
    assert image.mime == "image/png"
    assert image.base64 == "YWJj"


async def test_peer_receives_only_explicit_image_subset(backend, dc) -> None:
    class CapturingRouter:
        def __init__(self):
            self.requests = []

        async def invoke(self, _agent_type, request, **_kwargs):
            self.requests.append(request)
            return LLMResponse(content="analyzed")

    router = CapturingRouter()
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t_peer_image", datarc=dc)
    ctx._psi_parsed_results = [PreparedAttachment("not_selected", "private.png", "image/png", 3, "", False, None, True, image_base64="cHJpdmF0ZQ==")]
    selected = ImageRef(base64="YWJj", mime="image/png")
    result = await SubagentRunner(router=router, tool_registry=build_default_registry(), backend=backend).run(
        profile_name="compute",
        task="read the chart",
        parent_turn_id="t_peer_image",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
        message_images=[selected],
    )
    assert result.ok
    assert [image for request in router.requests for message in request.messages for image in message.images] == [selected]


async def test_lead_llm_exception_does_not_publish_raw_text(dc) -> None:
    """Lead's SubagentRunner fallback emits only a stable failure code."""

    secret = "api_key=terminal-producer-secret"

    class _FailingRouter:
        async def invoke(self, *_args, **_kwargs):
            raise RuntimeError(secret)

    runner = SubagentRunner(
        router=_FailingRouter(),  # type: ignore[arg-type]
        tool_registry=build_default_registry(),
        backend="legacy",
    )
    bus = TurnEventBus()
    persisted_terminal: dict = {}

    async def _persist_terminal(**kwargs) -> bool:
        persisted_terminal.update(kwargs)
        return True

    bus._lead_emit_event = _emit_event  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = _persist_terminal  # type: ignore[attr-defined]
    bus._pi_turn_state = {}  # type: ignore[attr-defined]
    result = await runner.run(
        profile_name="lead",
        task="触发失败",
        parent_turn_id="t_terminal_security",
        parent_bus=bus,
        parent_ctx=ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t_terminal_security",
            datarc=dc,
        ),
    )

    assert not result.ok
    assert result.error == "lead_internal_error"
    events = await _collect_events(bus)
    assert events
    assert all(secret not in repr(event.payload) for event in events)
    assert all(event.event_type != "error" for event in events)
    failed = next(event for event in events if event.event_type == "turn_failed")
    done = next(event for event in events if event.event_type == "done")
    assert failed.payload["reason"] == "lead_internal_error"
    assert done.payload["error"] == "lead_internal_error"
    # Chat turn feedback D3 (fe5f5c2c3): a failure with a public reason publishes and
    # persists no system answer prose; the failure card speaks from reason_code.
    assert all(event.event_type != "final_answer" for event in events)
    assert persisted_terminal["final_answer"] == ""


def _semantic_event_types(bus) -> list[str]:
    """事件类型，**剔除纯遥测**。

    ★ 这两条测试的真判据是「并发终态赢家压掉每一个 Lead **失败面**」，
    而原断言写成了「事件列表精确等于 [...]」——那是判据的**代理**，不是判据本身。
    turn-latency spike 2026-08-21 给 Lead 解除逐圈遥测排除后，`peer.iter`
    （无内容、FE 不听、成功失败两条路都发）就把这个代理打红了，而真判据毫发无损。
    ⚠ 剔除范围**只限已知纯遥测**，其余仍逐字精确匹配——真冒出一个失败面照样红。
    """
    # Since e027f2296, every model call (including failures) emits llm.call_timing in finally; like peer.iter,
    # it is internal telemetry (events.py declares FE does not listen), not Lead failure output.
    _PURE_TELEMETRY = {"peer.iter", "llm.call_timing"}
    return [e.event_type for e in bus.snapshot() if e.event_type not in _PURE_TELEMETRY]


async def test_lead_llm_exception_lost_terminal_cas_publishes_no_false_terminal(dc) -> None:
    """A concurrent terminal winner suppresses every Lead failure surface."""

    class _FailingRouter:
        async def invoke(self, *_args, **_kwargs):
            raise RuntimeError("late lead failure")

    persist_calls = 0

    async def _lost_terminal_cas(**_kwargs) -> bool:
        nonlocal persist_calls
        persist_calls += 1
        return False

    bus = TurnEventBus()
    bus._lead_emit_event = _emit_event  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = _lost_terminal_cas  # type: ignore[attr-defined]
    bus._pi_turn_state = {}  # type: ignore[attr-defined]

    result = await SubagentRunner(
        router=_FailingRouter(),  # type: ignore[arg-type]
        tool_registry=build_default_registry(),
        backend="legacy",
    ).run(
        profile_name="lead",
        task="trigger a late failure",
        parent_turn_id="t_lost_terminal_cas",
        parent_bus=bus,
        parent_ctx=ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t_lost_terminal_cas",
            datarc=dc,
        ),
    )

    assert not result.ok
    assert persist_calls == 1
    assert _semantic_event_types(bus) == ["agent_spawned"]
    assert bus._closed is False


async def test_subagent_llm_cancel_defers_terminal_to_outer_turn(dc) -> None:
    """Nested cancellation must not close the turn bus ahead of the abort CAS."""

    class _CancelledRouter:
        async def invoke(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    bus = TurnEventBus()
    with pytest.raises(asyncio.CancelledError):
        await SubagentRunner(
            router=_CancelledRouter(),  # type: ignore[arg-type]
            tool_registry=build_default_registry(),
            backend="legacy",
        ).run(
            profile_name="lead",
            task="cancel nested LLM",
            parent_turn_id="t_nested_cancel",
            parent_bus=bus,
            parent_ctx=ToolExecutionContext(
                workspace_id="w",
                member_uid="m",
                user_id="u",
                turn_id="t_nested_cancel",
                datarc=dc,
            ),
        )

    assert _semantic_event_types(bus) == ["agent_spawned"]
    assert bus._closed is False


async def test_lead_subagent_creator_receipt_does_not_require_second_llm_call(dc, monkeypatch) -> None:
    """开卡成功就是终态；后续 LLM 不可用也不能把成功动作覆盖成整轮失败。"""

    class _OneShotRouter:
        calls = 0

        async def invoke(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="create-subagent",
                            name="subagent_creator",
                            # 1662c150a made purpose collection unconditional: an empty call now asks
                            # for the purpose (not terminal). Opening the card is the explicit
                            # manual action.
                            arguments={"action": "manual"},
                        )
                    ],
                )
            raise TimeoutError("post-tool LLM should not be called")

    from dataclasses import replace

    from bowhead_core.subagent_creator_trigger import (
        register_subagent_creator_trigger,
    )

    monkeypatch.setitem(
        PROFILES,
        "lead",
        replace(
            PROFILES["lead"],
            tool_whitelist=(*PROFILES["lead"].tool_whitelist, "subagent_creator"),
        ),
    )

    router = _OneShotRouter()
    registry = build_default_registry()
    register_subagent_creator_trigger(registry)
    bus = TurnEventBus()
    result = await SubagentRunner(
        router=router,  # type: ignore[arg-type]
        tool_registry=registry,
        backend="legacy",
    ).run(
        profile_name="lead",
        task="我要创建一个subagent",
        parent_turn_id="t_create_subagent",
        parent_bus=bus,
        parent_ctx=ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t_create_subagent",
            datarc=dc,
            parent_bus=bus,
            role="lead",
        ),
    )

    assert router.calls == 1, [(inv.tool_name, inv.error, inv.result) for inv in result.tool_invocations]
    assert result.ok
    assert "已打开「新建 Agent」卡片" in result.final_answer
    assert [inv.tool_name for inv in result.tool_invocations] == ["subagent_creator"]
    events = await _collect_events(bus)
    assert any(event.event_type == "cca.subagent_creator_open" for event in events)
    assert all(event.event_type not in {"turn_failed", "error"} for event in events)


async def test_legacy_peer_reports_cached_tokens(dc) -> None:
    """perf WP-1 P1-b: 缓存命中量流进 SubagentResult + agent_spawned(completed) 事件.

    只测 legacy backend —— prod 默认路径（116 无 BOWHEAD_SUBAGENT_BACKEND 覆盖）。
    langgraph 路径的 cached 归账是 follow-up。
    """
    router = _router(
        LLMResponse(
            content="分析完成: GMV 增长 12.3%",
            usage=TokenUsage(input_tokens=120, output_tokens=40, cached_tokens=900),
        )
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    result = await runner.run(
        profile_name="compute",
        task="缓存计量测试",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    assert result.ok
    assert result.tokens_cached == 900  # SubagentResult carries the hit count.
    events = await _collect_events(bus)
    completed = [e for e in events if e.event_type == "agent_spawned" and e.payload.get("status") == "completed"]
    assert completed, "completed agent_spawned 事件缺失"
    assert completed[-1].payload["tokens_cached"] == 900  # Event payload also carries it for observation consumers.


async def test_subagent_tool_call_whitelist(backend, dc) -> None:
    """Subagent 调 bi.query → 允许；调 bi.update_measures → 不在白名单，被过滤."""
    router = _scripted_for(
        backend,
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="bi_query", arguments={"text": "今年 GMV"}),
                ToolCall(id="c2", name="bi_update_measures", arguments={"payload": {}, "reason": "x"}),
            ],
        ),
        LLMResponse(content="done"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend=backend)
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    result = await runner.run(
        profile_name="compute",
        task="取今年 GMV",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    assert result.ok
    # Only bi.query enters invocations.
    assert [inv.tool_name for inv in result.tool_invocations] == ["bi_query"]


@pytest.mark.parametrize("latch_before_auto_chart", [False, True])
async def test_short_chat_payload_suppresses_peer_auto_chart(
    dc,
    monkeypatch,
    latch_before_auto_chart: bool,
) -> None:
    """短答不升级；已锁存持久化失败也不得执行 auto-chart 写副作用。"""
    chart_calls: list[dict[str, object]] = []

    async def _persist(_event):
        return True

    bus = TurnEventBus(log_sink=_persist, persist_before_live=True)

    async def _sandbox(_args, _ctx):  # type: ignore[no-untyped-def]
        return {
            "ok": True,
            "outputs": [
                {
                    "artifact_id": "art_small_comparison",
                    "name": "comparison.csv",
                    "mime": "text/csv",
                    "preview": {
                        "columns": ["period", "value"],
                        "rows": [["Q1", 10], ["Q2", 12], ["Q3", 11]],
                    },
                }
            ],
        }

    async def _chart(args, _ctx):  # type: ignore[no-untyped-def]
        chart_calls.append(args)
        return {"section_id": "sec_auto", "kind": "chart"}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="skill_invoke",
                description="invoke sandbox",
                input_schema={"type": "object"},
            ),
            handler=_sandbox,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="add_chart",
                description="add chart",
                input_schema={"type": "object"},
            ),
            handler=_chart,
        ),
    )
    register_subagent_return_tool(registry)
    if latch_before_auto_chart:
        from bowhead_core.canvas_composers import auto_chart

        monkeypatch.setattr(
            subagent_mod,
            "is_short_answer_payload",
            lambda _payload: False,
        )

        def _latch_then_return_candidate(_invocations):
            bus.latch_fatal_error(RuntimeError("injected sibling persistence failure"))
            return [{"data_artifact_id": "art_small_comparison"}]

        monkeypatch.setattr(
            auto_chart,
            "collect_auto_chart_candidates",
            _latch_then_return_candidate,
        )
    router = _scripted_for(
        "legacy",
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="sandbox",
                    name="skill_invoke",
                    arguments={"name": "python_sandbox", "script": "compute.py"},
                ),
                ToolCall(
                    id="return",
                    name="subagent_return",
                    arguments={
                        "payload": {
                            "delivery_surface": "chat",
                            "exec_summary": "Q2 为 12，较 Q1 高 2。",
                            "kpis": [
                                {
                                    "label": "Q2",
                                    "value": "12",
                                    "sentiment": "neutral",
                                }
                            ],
                        }
                    },
                ),
            ],
        ),
    )
    run = SubagentRunner(
        router=router,
        tool_registry=registry,
        backend="legacy",
    )
    kwargs = {
        "profile_name": "compute",
        "task": "比较三个季度",
        "parent_turn_id": "t_short_auto_chart",
        "parent_bus": bus,
        "parent_ctx": ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t_short_auto_chart",
            datarc=dc,
        ),
    }
    if latch_before_auto_chart:
        with pytest.raises(TerminalDurabilityAbort, match="durable event"):
            await run.run(**kwargs)
        assert chart_calls == []
        return

    result = await run.run(**kwargs)

    assert result.ok
    returned = [inv for inv in result.tool_invocations if inv.tool_name == "subagent_return"]
    assert len(returned) == 1
    assert returned[0].result["ok"] is True
    assert result.payload is not None
    assert result.payload["delivery_surface"] == "chat"
    assert chart_calls == []


@pytest.mark.parametrize("chart_access", ["allowed", "profile_omitted", "run_denied", "unconfigured"])
async def test_peer_auto_chart_runs_through_the_guarded_tool_path(dc, monkeypatch, chart_access) -> None:
    """Backend charts require current tool access, then pass per-call guards."""
    from dataclasses import replace

    from bowhead_core.canvas_composers import auto_chart

    chart_calls: list[dict[str, object]] = []
    gated: list[tuple[str, str | None]] = []
    events: list[OrchestratorEvent] = []

    async def _persist(event):  # type: ignore[no-untyped-def]
        events.append(event)
        return True

    bus = TurnEventBus(log_sink=_persist, persist_before_live=True)

    async def _chart(args, _ctx):  # type: ignore[no-untyped-def]
        chart_calls.append(args)
        return {"section_id": "sec_auto", "kind": "chart"}

    async def _maybe_gate(**kwargs):  # type: ignore[no-untyped-def]
        gated.append((kwargs["tool_name"], kwargs["tool_call_id"]))
        return

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="add_chart", description="add chart", input_schema={"type": "object"}), handler=_chart))
    register_subagent_return_tool(registry)
    monkeypatch.setattr(subagent_mod, "is_short_answer_payload", lambda _payload: False)
    monkeypatch.setattr(auto_chart, "collect_auto_chart_candidates", lambda _invocations: [{"data_artifact_id": "art_auto"}])
    router = _scripted_for(
        "legacy",
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="return",
                    name="subagent_return",
                    arguments={"payload": {"delivery_surface": "chat", "exec_summary": "Q2 为 12。"}},
                ),
            ],
        ),
    )
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t_auto_chart", datarc=dc)
    ctx.approval_gate = SimpleNamespace(maybe_gate=_maybe_gate)
    if chart_access == "profile_omitted":
        profile = subagent_mod.PROFILES["compute"]
        monkeypatch.setitem(
            subagent_mod.PROFILES, "compute", replace(profile, tool_whitelist=tuple(name for name in profile.tool_whitelist if name != "add_chart"))
        )
    if chart_access == "unconfigured":
        ctx.unconfigured_capability_tools = frozenset({"add_chart"})

    result = await SubagentRunner(router=router, tool_registry=registry, backend="legacy").run(
        profile_name="compute",
        task="比较三个季度",
        parent_turn_id="t_auto_chart",
        parent_bus=bus,
        parent_ctx=ctx,
        tool_deny=frozenset({"add_chart"}) if chart_access == "run_denied" else frozenset(),
    )

    assert result.ok
    if chart_access != "allowed":
        assert chart_calls == []
        assert not any(inv.tool_name == "add_chart" for inv in result.tool_invocations)
        assert not any(e.event_type == "step_started" and e.payload.get("tool_name") == "add_chart" for e in events)
        return
    assert chart_calls == [{"data_artifact_id": "art_auto"}]
    chart_inv = next(inv for inv in result.tool_invocations if inv.tool_name == "add_chart")
    assert chart_inv.call_id is not None and chart_inv.call_id.startswith("auto_chart_")
    assert ("add_chart", chart_inv.call_id) in gated
    started = [e for e in events if e.event_type == "step_started" and e.payload.get("tool_name") == "add_chart"]
    assert [e.payload["call_id"] for e in started] == [chart_inv.call_id]


async def test_python_only_subagent_rejects_other_skill_names(
    backend,
    dc,
    monkeypatch,
) -> None:
    """Both runtimes hard-enforce the argument-level Python-only contract."""
    calls: list[str] = []

    async def _invoke(args, _ctx):  # type: ignore[no-untyped-def]
        calls.append(str(args.get("name") or ""))
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="skill_invoke",
                description="invoke",
                input_schema={"type": "object"},
            ),
            handler=_invoke,
        ),
    )
    profile = SubagentProfile(
        name="python-only-test",
        llm_agent_type="analyst",
        tool_whitelist=("skill_invoke",),
        system_prompt="Use Python.",
        max_iterations=3,
        skill_invoke_allowlist=("python_sandbox",),
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    router = _scripted_for(
        backend,
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="allowed",
                    name="skill_invoke",
                    arguments={"name": "python_sandbox"},
                ),
                ToolCall(
                    id="allowed-wrapped",
                    name="skill_invoke",
                    arguments={
                        "raw_arguments": ('{"name":"python_sandbox","script":"runner.py"}'),
                    },
                ),
                ToolCall(
                    id="blocked-direct",
                    name="skill_invoke",
                    arguments={"name": "canvas-pptx"},
                ),
                ToolCall(
                    id="blocked-raw-arguments",
                    name="skill_invoke",
                    arguments={
                        "raw_arguments": ('{"name":"canvas-pptx","script":"render_from_canvas.py"}'),
                    },
                ),
                ToolCall(
                    id="blocked-raw",
                    name="skill_invoke",
                    arguments={
                        "_raw": ('{"name":"canvas-pptx","script":"render_from_canvas.py"}'),
                    },
                ),
                ToolCall(
                    id="blocked-arguments",
                    name="skill_invoke",
                    arguments={
                        "arguments": ('{"name":"canvas-pptx","script":"render_from_canvas.py"}'),
                    },
                ),
            ],
        ),
        LLMResponse(content="done"),
    )

    result = await SubagentRunner(
        router=router,
        tool_registry=registry,
        backend=backend,
    ).run(
        profile_name=profile.name,
        task="calculate",
        parent_turn_id="t_python_scope",
        parent_bus=TurnEventBus(),
        parent_ctx=ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t_python_scope",
            datarc=dc,
        ),
    )

    assert result.ok
    assert calls == ["python_sandbox", "python_sandbox"]
    assert [inv.arguments["name"] for inv in result.tool_invocations] == [
        "python_sandbox",
        "python_sandbox",
    ]


async def test_subagent_unknown_profile_fails(backend, dc) -> None:
    router = _router()  # No response is fine: profile validation precedes LLM calls.
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend=backend,
    )
    result = await runner.run(
        profile_name="nope",
        task="x",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t",
            datarc=dc,
        ),
    )
    assert not result.ok
    assert "unknown profile" in (result.error or "")


async def test_subagent_spawn_tool_registration(backend, dc) -> None:
    """subagent.spawn tool 注册后可通过 registry.execute 调用."""
    router = _scripted_for(backend, LLMResponse(content="result = 42"))
    tr = build_default_registry()
    register_subagent_tool(tr)
    assert tr.has("subagent_spawn")

    runner = SubagentRunner(router=router, tool_registry=tr, backend=backend)
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=bus,
    )

    inv = await tr.execute(
        "subagent_spawn",
        {"profile": "compute", "task": "dummy"},
        ctx,
    )
    assert inv.error is None, inv.error
    assert inv.result["ok"] is True
    assert inv.result["profile"] == "compute"
    assert inv.result["final_answer"] == "result = 42"


async def test_subagent_spawn_missing_runner(dc) -> None:
    """handler 在 ctx 无 runner 时返回 error（不依赖 backend）."""
    tr = build_default_registry()
    register_subagent_tool(tr)
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    inv = await tr.execute(
        "subagent_spawn",
        {"profile": "compute", "task": "x"},
        ctx,
    )
    assert inv.error is None  # Handler returns a dict with error rather than raising.
    assert "subagent_runner/bus not configured" in inv.result["error"]


async def test_subagent_spawn_blocks_before_peer_event_or_runner_on_admission_denial(
    dc,
) -> None:
    class _DeniedRunner:
        called = False

        async def admit_execution_binding(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AgentExecutionBindingError("agent_assignment_inactive")

        async def run(self, **_kwargs):  # type: ignore[no-untyped-def]
            self.called = True
            raise AssertionError("denied Agent must not run")

    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _DeniedRunner()
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t_denied",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=bus,
    )

    invocation = await registry.execute(
        "subagent_spawn",
        {
            "profile": "compute",
            "task": "must not run",
            "delivery": "report",
            "language": "en-US",
        },
        ctx,
    )

    assert invocation.error is None
    assert invocation.result["error_code"] == "agent_assignment_inactive"
    assert invocation.result["error_kind"] == "agent_admission_blocked"
    assert runner.called is False
    assert bus.snapshot() == []
    assert not hasattr(bus, "_lead_delivery_spawn")
    assert not hasattr(bus, "_early_surface_decision")


async def test_subagent_spawn_validates_exact_behavior_candidate_before_admission(
    dc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")

    class _CandidateRunner:
        admitted = None

        async def admit_execution_binding(self, **kwargs):  # type: ignore[no-untyped-def]
            self.admitted = kwargs
            return

        async def run(self, **kwargs):  # type: ignore[no-untyped-def]
            return SubagentResult(
                profile=kwargs["profile_name"],
                subagent_id="sub_candidate",
                ok=True,
                final_answer="done",
            )

    candidate = new_turn_candidate(
        role_id="image",
        agent_id="system:visual-maker",
        runtime_profile_name="visual-maker",
        assignment_id=None,
        revision_ref="arv_" + "1" * 32,
        release_id=None,
        behavior_fingerprint="sha256:" + "2" * 64,
        is_primary=True,
        is_preferred=False,
        display_name="Visual Maker",
        description="Creates images",
    )
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CandidateRunner()
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t_candidate",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=TurnEventBus(),
    )
    ctx._lead_role_candidate_roster = LeadRoleCandidateRoster.build(
        {"image": (candidate,)},
    )
    ctx._lead_behavior_candidate_profiles = frozenset({"visual-maker"})

    rejected = await registry.execute(
        "subagent_spawn",
        {"profile": "visual-maker", "task": "create a poster"},
        ctx,
    )
    assert rejected.result["error_code"] == "candidate_choice_invalid"
    assert runner.admitted is None

    accepted = await registry.execute(
        "subagent_spawn",
        {
            "profile": "visual-maker",
            "task": "create a poster",
            "role_id": "image",
            "candidate_id": candidate.candidate_id,
            "reason_code": "task_match",
        },
        ctx,
    )
    assert accepted.result["ok"] is True
    assert runner.admitted["profile_name"] == "visual-maker"
    assert runner.admitted["agent_id"] == "system:visual-maker"
    assert runner.admitted["expected"].revision_ref == candidate.revision_ref
    assert runner.admitted["expected"].behavior_fingerprint == candidate.behavior_fingerprint


# ─────────────────────────────────────────────────────────────
# Added: LangGraph-specific graph behavior tests (four)
# ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _pin_fanout_on(monkeypatch):
    # fix/sse-fanout-bus: _collect_events uses snapshot() (fanout only);
    # pin ON to isolate external env (BOWHEAD_SSE_FANOUT=0 rollback drills must not affect this file).
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")


async def _collect_events(bus: TurnEventBus) -> list[OrchestratorEvent]:
    """读 bus ring 快照（测试用；非破坏性）."""
    return bus.snapshot()


# LangGraph backend retired (Agent SDK evaluation A2, 2026-08). Formerly three tests here covered only that graph:
# node order / SOP lookup fallback / stream-bridge events. They tested removed capabilities,
# not regressions. Moved capabilities need tests at their new location; retired capabilities lose their tests too.


# ─────────────────────────────────────────────────────────────
# Retired profile guard
# ─────────────────────────────────────────────────────────────
async def test_debate_profile_is_not_runnable(backend, dc) -> None:
    runner = SubagentRunner(
        router=_scripted_for(backend, LLMResponse(content="must not run")),
        tool_registry=build_default_registry(),
        backend=backend,
    )
    result = await runner.run(
        profile_name="debate",
        task="旧调用不得恢复退役能力",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ToolExecutionContext(
            workspace_id="w",
            member_uid="m",
            user_id="u",
            turn_id="t",
            datarc=dc,
        ),
    )

    assert "debate" not in PROFILES
    assert not result.ok
    assert "unknown profile" in (result.error or "")


async def test_subagent_spawn_schema_does_not_advertise_debate() -> None:
    registry = build_default_registry()
    register_subagent_tool(registry)

    profile_schema = registry.get("subagent_spawn").schema.input_schema["properties"]["profile"]
    assert "debate" not in profile_schema["enum"]
    assert registry.get("subagent_spawn").schema.input_schema["properties"]["agent_id"]["type"] == "string"


# ─────────────────────────────────────────────────────────────
# pycell events (#49: visible Python sandbox code)
# ─────────────────────────────────────────────────────────────
async def test_subagent_emits_pycell_events_with_counter_ref(backend, dc) -> None:
    """subagent 的 tool 调用也要 emit pycell.in / pycell.out，使用共享 counter_ref."""
    router = _scripted_for(
        backend,
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="bi_query", arguments={"text": "今年 GMV"}),
                ToolCall(id="c2", name="bi_query", arguments={"text": "今年利润"}),
            ],
        ),
        LLMResponse(content="done"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend=backend)
    bus = TurnEventBus()
    # Simulate orchestrator having emitted five parent pycells; next should be six.
    counter_ref: list[int] = [5]
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
        pycell_counter_ref=counter_ref,
    )
    events: list[OrchestratorEvent] = []

    async def _sink(e: OrchestratorEvent) -> None:
        events.append(e)

    bus._log_sink = _sink  # type: ignore[attr-defined]

    await runner.run(
        profile_name="compute",
        task="取 GMV 和利润",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )

    pycell_ins = [e for e in events if e.event_type == "pycell.in"]
    pycell_outs = [e for e in events if e.event_type == "pycell.out"]
    # Two tool calls → two pycell.in/out pairs.
    assert len(pycell_ins) == 2
    assert len(pycell_outs) == 2
    # n increments from the parent's six; pass subagent_id through.
    assert pycell_ins[0].payload["n"] == 6
    assert pycell_ins[1].payload["n"] == 7
    assert pycell_ins[0].payload["subagent_id"]
    assert pycell_ins[0].payload["code"].startswith("bi_query(")
    # ctx's counter_ref mutates to seven.
    assert counter_ref[0] == 7
    # Output n matches input n.
    assert pycell_outs[0].payload["n"] == 6
    assert pycell_outs[1].payload["n"] == 7


async def test_subagent_no_pycell_when_ref_absent(backend, dc) -> None:
    """兼容老路径：ctx 没传 pycell_counter_ref → subagent 不 emit pycell 事件."""
    router = _scripted_for(
        backend,
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"text": "今年 GMV"})],
        ),
        LLMResponse(content="done"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend=backend)
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
        # Deliberately omit pycell_counter_ref.
    )
    events: list[OrchestratorEvent] = []

    async def _sink(e: OrchestratorEvent) -> None:
        events.append(e)

    bus._log_sink = _sink  # type: ignore[attr-defined]

    await runner.run(
        profile_name="compute",
        task="x",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )

    assert [e for e in events if e.event_type in ("pycell.in", "pycell.out")] == []


def test_query_fingerprint_preserves_order_semantic() -> None:
    """CTO review: 否定词顺序有语义，指纹必须**保序**.

    "买 A 没买 B" 和 "没买 A 买 B" 不能同指纹.
    """
    from bowhead_core.subagent import _query_fingerprint

    neg_a = _query_fingerprint("bi_query", {"text": "买 A 没买 B"})
    neg_b = _query_fingerprint("bi_query", {"text": "没买 A 买 B"})
    assert neg_a != neg_b


def test_query_fingerprint_normalizes_whitespace_and_punct() -> None:
    """标点 + 空白归一化."""
    from bowhead_core.subagent import _query_fingerprint

    a = _query_fingerprint("bi_query", {"text": "今年 GMV, 同比"})
    b = _query_fingerprint("bi_query", {"text": "今年 GMV 同比"})
    c = _query_fingerprint("bi_query", {"text": "今年  GMV   同比"})
    assert a == b == c


def test_query_fingerprint_punctuation_stripped() -> None:
    from bowhead_core.subagent import _query_fingerprint

    a = _query_fingerprint("bi_query", {"text": "今年 GMV, 同比"})
    b = _query_fingerprint("bi_query", {"text": "今年 GMV 同比"})
    assert a == b


def test_query_fingerprint_different_tools_not_same() -> None:
    """不同 tool name 即使 args 相同, fingerprint 也应不同."""
    from bowhead_core.subagent import _query_fingerprint

    a = _query_fingerprint("bi_query", {"text": "GMV"})
    b = _query_fingerprint("bi_attribute", {"text": "GMV"})
    assert a != b


async def test_subagent_pycell_code_rendering_python_sandbox(backend, dc) -> None:
    """python_sandbox 的 code 要原样塞进 pycell.in 的 code 字段（这是用户痛点）."""
    sandbox_code = "import pandas as pd\ndf = pd.read_csv(INPUTS['rows']['path'])\nprint(df.describe())"
    router = _scripted_for(
        backend,
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="skill_invoke",
                    arguments={
                        "name": "python_sandbox",
                        "input": {"code": sandbox_code, "timeout_sec": 30},
                    },
                )
            ],
        ),
        LLMResponse(content="done"),
    )
    tr = build_default_registry()
    runner = SubagentRunner(router=router, tool_registry=tr, backend=backend)
    bus = TurnEventBus()
    counter_ref: list[int] = [0]
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
        pycell_counter_ref=counter_ref,
    )
    events: list[OrchestratorEvent] = []

    async def _sink(e: OrchestratorEvent) -> None:
        events.append(e)

    bus._log_sink = _sink  # type: ignore[attr-defined]

    await runner.run(
        profile_name="compute",
        task="统计",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )

    pycell_ins = [e for e in events if e.event_type == "pycell.in"]
    assert len(pycell_ins) >= 1
    # Find skill.invoke's pycell.
    sbx_cell = next(e for e in pycell_ins if e.payload["name"] == "skill_invoke")
    # Full sandbox code belongs in code (render_code's special case inserts it).
    assert sandbox_code in sbx_cell.payload["code"]
    assert "# python_sandbox" in sbx_cell.payload["code"]


# ── ★ Rule 8 fingerprint degradation (tool-guardrail-fingerprint-2026-08-12) ────────────
#
# _query_fingerprint recognizes only text/question/query/name. Structured-argument tools never match,
# making fingerprints always (tool, "") and degrading the guard to permanently blocking this tool in this step after two failures.
# Evidence from 116: 207 of 690 subagent_return calls (30%) failed validation; agents in two turns independently
# diagnosed this and abandoned structured delivery for prose. Of the latest 120 steps, 11 retained only fallback_markdown.
#
# All three assertion groups are necessary:
#   ① Regression: bi_query family remains byte-identical (breaking it silently weakens existing guards).
#   ② Guard preserved: identical arguments still yield the same key and block the third attempt.
#   ③ Bug fixed: different arguments yield different keys.
_STRUCT_ARG_TOOLS = (
    ("subagent_return", {"payload": {"title": "A"}}, {"payload": {"title": "B"}}),
    ("python_sandbox", {"code": "a=1"}, {"code": "b=2"}),
    ("canvas_add_insight", {"insight": {"t": "x"}}, {"insight": {"t": "y"}}),
    ("web_fetch", {"url": "http://a"}, {"url": "http://b"}),
)


@pytest.mark.parametrize("text", ["今年 GMV, 同比", "买 A 没买 B", "今年  GMV   同比"])
def test_query_fingerprint_text_tools_unchanged(text: str) -> None:
    """★回归对照：有自然 query 字段的工具，指纹不得掺入摘要（不带 '#'）。"""
    from bowhead_core.subagent import _query_fingerprint

    tool, q = _query_fingerprint("bi_query", {"text": text})
    assert tool == "bi_query"
    assert not q.startswith("#"), f"bi_query 不该走摘要分支: {q!r}"
    assert q  # Nonempty means the original path was taken.


@pytest.mark.parametrize(("tool", "a", "b"), _STRUCT_ARG_TOOLS)
def test_query_fingerprint_distinguishes_structured_args(tool: str, a: dict, b: dict) -> None:
    """参数不同 → 不同 key。旧实现下这两个恒相等（= 连坐封禁的根因）。"""
    from bowhead_core.subagent import _query_fingerprint

    assert _query_fingerprint(tool, a) != _query_fingerprint(tool, b)


@pytest.mark.parametrize(("tool", "a", "_b"), _STRUCT_ARG_TOOLS)
def test_query_fingerprint_same_args_still_collide(tool: str, a: dict, _b: dict) -> None:
    """★护栏必须保留：逐字相同的重发仍是同一 key，铁律 8 照常在第 3 次拦下。"""
    from bowhead_core.subagent import _query_fingerprint

    assert _query_fingerprint(tool, dict(a)) == _query_fingerprint(tool, dict(a))


def test_query_fingerprint_key_order_does_not_split_the_key() -> None:
    """键序无关——否则 LLM 换个键序就能绕过铁律 8，护栏形同虚设。"""
    from bowhead_core.subagent import _query_fingerprint

    assert _query_fingerprint("python_sandbox", {"code": "x", "lang": "py"}) == _query_fingerprint("python_sandbox", {"lang": "py", "code": "x"})


def test_query_fingerprint_survives_unserializable_args() -> None:
    """不可序列化不得抛——护栏在主循环热路径上，抛了就是整步炸。"""
    from bowhead_core.subagent import _query_fingerprint

    tool, q = _query_fingerprint("t", {"o": object()})
    assert tool == "t" and isinstance(q, str)


def test_query_fingerprint_empty_args_stays_constant() -> None:
    from bowhead_core.subagent import _query_fingerprint

    assert _query_fingerprint("t", {}) == ("t", "")


async def test_run_wires_figure_anchor_resolution_with_the_run_local_allowlist(backend, dc, monkeypatch) -> None:
    """行内图批 5 接线 —— 只测 `resolve_run_figure_anchors` 本身会漏掉「没接上」。

    ★ 判据是**传下去的表是本 run 自己那份**：Lead 与 peer 各建一份 `RefAllowlist`，
    同一张图编号不同；拿错表会安静地把锚点绑到另一张图上。
    """
    from bowhead_core._ref_allowlist import RefAllowlist

    from bowhead_core import chat_figure_anchors as _cfa

    seen: dict[str, object] = {}
    _orig = _cfa.resolve_run_figure_anchors

    def _spy(text, *, ref_allowlist, invocations):
        seen["allowlist"] = ref_allowlist
        seen["text"] = text
        seen["invocations"] = list(invocations or [])
        return _orig(text, ref_allowlist=ref_allowlist, invocations=invocations)

    monkeypatch.setattr(_cfa, "resolve_run_figure_anchors", _spy)
    monkeypatch.setenv("BOWHEAD_REF_ALLOWLIST_INJECT", "1")

    router = _scripted_for(backend, LLMResponse(content="结论。\n[figure:7]\n后续。"))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend=backend)
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=dc)
    result = await runner.run(
        profile_name="compute",
        task="随便问点什么",
        parent_turn_id="t",
        parent_ctx=ctx,
        parent_bus=TurnEventBus(),
    )

    assert result.ok
    assert "allowlist" in seen, "收口没被调用 —— 接线断了"
    assert isinstance(seen["allowlist"], RefAllowlist), f"传下去的不是本 run 的表：{seen['allowlist']!r}"
    # No evidence (no chart created this run, none in the allowlist) ⇒ fail open, preserving anchors unchanged.
    assert "[figure:7]" in (result.final_answer or "")
