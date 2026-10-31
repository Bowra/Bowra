"""chat-v4 CHANGE-W4.2 — lead.subagent_summary emit per subagent_return.

Tests verify (per tech-design r2 §2 CHANGE-W4.2 + GAP #6):
  - emit AFTER subagent_return, BEFORE tool_result returns to Lead
  - spawn_id matches peer_dispatched.spawn_id (W3.2 cross-event correlation)
  - child_canvas_sections is dict-list ([{id, kind, title}]) NOT titles-only
  - status reflects ok/failed/succeeded_with_warnings (caveats → with_warnings)
  - duration_ms / child_tool_count / output_preview present
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.subagent import (
    PROFILES,
    SubagentProfile,
    SubagentResult,
    register_subagent_tool,
)
from bowhead_core.tool_registry import ToolRegistry

if TYPE_CHECKING:
    from bowhead_core.events import OrchestratorEvent


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[OrchestratorEvent] = []

    async def emit(self, event: OrchestratorEvent) -> None:
        self.events.append(event)


class _FakeRunner:
    """Drop-in for SubagentRunner — returns pre-seeded SubagentResult."""

    def __init__(self, result: SubagentResult) -> None:
        self._result = result

    async def run(
        self,
        *,
        profile_name: str,
        task: str,
        parent_turn_id: str,
        parent_bus: Any,
        parent_ctx: Any,
        extra_instructions: str | None = None,
        pre_minted_subagent_id: str | None = None,
        **_kw: Any,
    ) -> SubagentResult:
        # Mirror real runner — populate subagent_id from pre-mint when supplied.
        if pre_minted_subagent_id:
            object.__setattr__(self._result, "subagent_id", pre_minted_subagent_id)
        return self._result


class _FakeInv:
    def __init__(self, tool_name: str, result: dict | None = None, args: dict | None = None) -> None:
        self.tool_name = tool_name
        self.result = result
        self.arguments = args or {}


class _FakeCtx:
    """Minimal stand-in for ToolExecutionContext + parent_ctx-attached fields."""

    def __init__(self, bus: _FakeBus) -> None:
        self.turn_id = "t_w42"
        self.parent_bus = bus
        self.subagent_runner: Any = None
        # ToolExecutionContext required-ish fields (role None → skip whitelist)
        self.role = None
        self.workspace_id = "w_test"
        self.user_id = "u_test"
        self.member_uid = "m_test"
        self.session_id = "s_test"
        self.project_id = None
        # ws-datarc-decouple P3: non-None sentinel so execute()'s datarc-capability gate passes
        # for the bi_query calls these summary tests drive (a real bi_query turn has a client).
        self.datarc = object()
        # Misc handler-touched attributes (canvas/artifact/etc.) — None safe.
        self.canvas_store = None
        self.artifact_store = None
        # Custom subagent registry — None signals "no user-defined profiles".
        self.custom_subagent_store = None
        # Mirrors ToolExecutionContext.document_turn_state (default None): ToolRegistry.execute
        # reads it directly for the document review gate (0ea29d766).
        self.document_turn_state = None
        self.collaboration_runtime = None
        self.knowledge_scope = None
        self.knowledge_source_uses = []


@pytest.fixture(autouse=True)
def _profiles_setup():
    """Ensure 'compute' profile exists for spawn handler resolution."""
    added = "compute" not in PROFILES
    if added:
        PROFILES["compute"] = SubagentProfile(
            name="compute",
            llm_agent_type="compute",
            tool_whitelist=("bi_query", "subagent_return"),
            system_prompt="(stub compute)",
            max_iterations=4,
        )
    yield
    if added:
        PROFILES.pop("compute", None)


@pytest.mark.unit
async def test_summary_after_return() -> None:
    """Successful subagent_return → emit lead.subagent_summary with all required fields."""
    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="ok prose",
        tokens_input=10,
        tokens_output=20,
        duration_ms=123,
        payload={
            "title": "Report Title",
            "exec_summary": "summary",
            "kpis": [],
            "insights": [{"title": "i1"}],
            "recommendations": [{"action": "do x"}],
        },
        tool_invocations=[
            _FakeInv("bi_query", result={"ok": True}),
            _FakeInv(
                "add_chart",
                result={"section_id": "sec_chart_1", "kind": "chart"},
                args={"title": "Chart Title"},
            ),
            _FakeInv(
                "canvas_add_insight",
                result={"section_id": "sec_ins_1", "kind": "insight_card"},
                args={"title": "Insight Title"},
            ),
        ],
    )

    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)

    registry = ToolRegistry()
    register_subagent_tool(registry)

    out = await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "do x", "rationale": "test rationale", "delivery": "report"},
        ctx=ctx,
    )
    assert out.error is None

    # Find lead.subagent_summary event
    summaries = [e for e in bus.events if e.event_type == "lead.subagent_summary"]
    assert len(summaries) == 1, f"expected exactly 1 summary, got {[e.event_type for e in bus.events]}"
    s = summaries[0].payload
    # Required fields
    assert s["profile"] == "compute"
    assert s["card_id"] == "t_w42"
    assert s["rationale"] == "test rationale"
    # r4: turn-level history reconstructs this spawn as Lead tool_calls; summary carries the actual full task / delivery / canvas_mode.
    assert s["task"] == "do x" and s["delivery"] == "report" and "canvas_mode" in s
    assert s["child_tool_count"] == 3
    assert s["status"] == "succeeded"
    assert s["failure_reason"] is None
    assert isinstance(s["duration_ms"], int)
    # child_canvas_sections is a dict-list, NOT titles-only.
    assert isinstance(s["child_canvas_sections"], list)
    assert len(s["child_canvas_sections"]) == 2
    for cs in s["child_canvas_sections"]:
        assert set(cs.keys()) >= {"id", "kind", "title"}
    kinds = {cs["kind"] for cs in s["child_canvas_sections"]}
    assert "chart" in kinds and "insight_card" in kinds
    # output_preview surfaces payload shape
    assert s["output_preview"]["insights_count"] == 1
    assert s["output_preview"]["recommendations_count"] == 1


@pytest.mark.unit
async def test_short_answer_spawn_result_is_chat_not_canvas_directive() -> None:
    """Simple payload returns exact prose and never tells Lead to render Canvas."""
    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="",
        payload={
            "delivery_surface": "chat",
            "exec_summary": "19 × 29 = 551",
            "kpis": [
                {
                    "label": "19 × 29",
                    "value": "551",
                    "sentiment": "neutral",
                }
            ],
        },
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)
    registry = ToolRegistry()
    register_subagent_tool(registry)

    invocation = await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "calculate", "delegation_role": "primary"},
        ctx=ctx,
    )

    assert invocation.error is None
    assert invocation.result["final_answer"] == "19 × 29 = 551"
    assert invocation.result["payload_summary"]["delivery_surface"] == "chat"
    assert "do not generate a report" in invocation.result["directive"]
    assert "composer" not in invocation.result["directive"]


@pytest.mark.unit
async def test_spawn_id_matches_peer_dispatched() -> None:
    """spawn_id on lead.subagent_summary must match peer_dispatched.spawn_id
    (W3.2 cross-event correlation). Both emit the SAME pre-minted id."""
    result = SubagentResult(
        profile="compute",
        subagent_id="ignored",
        ok=True,
        final_answer="done",
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)

    registry = ToolRegistry()
    register_subagent_tool(registry)

    await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "t"},
        ctx=ctx,
    )

    peer_dispatched = [e for e in bus.events if e.event_type == "peer_dispatched"]
    summaries = [e for e in bus.events if e.event_type == "lead.subagent_summary"]
    assert len(peer_dispatched) == 1
    assert len(summaries) == 1
    pd_spawn = peer_dispatched[0].payload["spawn_id"]
    sm_spawn = summaries[0].payload["spawn_id"]
    assert pd_spawn == sm_spawn
    # subagent_id alias too
    assert summaries[0].payload["subagent_id"] == pd_spawn


@pytest.mark.unit
async def test_summary_status_failed_with_reason() -> None:
    """ok=False → finite failed disclosure without result prose."""
    result = SubagentResult(
        profile="compute",
        subagent_id="x",
        ok=False,
        final_answer="",
        error="subagent_failed: tool error Authorization: Bearer sk-secretvalue123",
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)

    registry = ToolRegistry()
    register_subagent_tool(registry)

    await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "t"},
        ctx=ctx,
    )
    summaries = [e for e in bus.events if e.event_type == "lead.subagent_summary"]
    assert len(summaries) == 1
    s = summaries[0].payload
    assert s["status"] == "failed"
    assert s["failure_reason"] == "peer_execution_failed"
    assert "secretvalue123" not in str(s)
    assert s["diagnostics"]["diagnostic_code"] == "chat_peer_failed"
    assert s["diagnostics"]["turn_id"] == "t_w42"
    failures = [e for e in bus.events if e.event_type == "peer_dispatch_failed"]
    assert len(failures) == 1
    failure = failures[0].payload
    assert failure["card_id"] == "t_w42"
    assert failure["spawn_id"] == s["spawn_id"]
    assert failure["subagent_id"] == s["subagent_id"]
    assert failure["reason_code"] == "peer_execution_failed"
    assert "error_preview" not in failure
    assert failure["diagnostics"] == s["diagnostics"]
    assert "secretvalue123" not in str(failure)


@pytest.mark.unit
async def test_empty_answer_is_failed_in_every_peer_projection() -> None:
    """The handler's empty-result guard must run before peer terminal emits."""
    result = SubagentResult(
        profile="compute",
        subagent_id="x",
        ok=True,
        final_answer="   ",
        payload=None,
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)

    registry = ToolRegistry()
    register_subagent_tool(registry)

    invocation = await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "t"},
        ctx=ctx,
    )

    assert invocation.error is None
    assert invocation.result["ok"] is False
    assert invocation.result["error_code"] == "subagent_empty_answer"
    summary = next(event for event in bus.events if event.event_type == "lead.subagent_summary")
    assert summary.payload["status"] == "failed"
    assert summary.payload["failure_reason"] == "peer_empty_result"
    failure = next(event for event in bus.events if event.event_type == "peer_dispatch_failed")
    assert failure.payload["spawn_id"] == summary.payload["spawn_id"]
    assert failure.payload["reason_code"] == "peer_empty_result"
    assert failure.payload["diagnostics"] == summary.payload["diagnostics"]


@pytest.mark.unit
@pytest.mark.parametrize("valid_selection,peer_ok", [(True, True), (False, True), (True, False)])
async def test_context_selection_preserves_pause_and_usage_without_masking_failure(valid_selection: bool, peer_ok: bool) -> None:
    selection = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {
            "kind": "tool_context_select",
            "tool": "datarc",
            "prompt": "Choose a workspace.",
            "options": [{"value": "1", "label": "Source A"}] if valid_selection else [],
        },
    }
    result = SubagentResult(
        profile="compute",
        subagent_id="x",
        ok=peer_ok,
        final_answer="",
        tool_invocations=[_FakeInv("bi_schema", selection)],
        tokens_input=17,
        tokens_output=9,
        tokens_cached=3,
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)
    registry = ToolRegistry()
    register_subagent_tool(registry)
    invocation = await registry.execute("subagent_spawn", arguments={"profile": "compute", "task": "Inspect sales."}, ctx=ctx)
    assert invocation.error is None
    if valid_selection and peer_ok:
        assert invocation.result["clarification"] is selection["clarification"]
        assert [invocation.result[key] for key in ("tokens_input", "tokens_output", "tokens_cached")] == [17, 9, 3]
        assert invocation.nested[0].result == selection
        assert not any(event.event_type in {"peer_dispatch_failed", "lead.subagent_summary"} for event in bus.events)
    else:
        assert invocation.result["ok"] is False
        assert any(event.event_type == "peer_dispatch_failed" for event in bus.events)


@pytest.mark.unit
async def test_summary_succeeded_with_warnings_on_caveats() -> None:
    """ok=True but payload.caveats non-empty → succeeded_with_warnings."""
    result = SubagentResult(
        profile="compute",
        subagent_id="x",
        ok=True,
        final_answer="done",
        payload={
            "title": "x",
            "exec_summary": "y",
            "kpis": [],
            "insights": [],
            "recommendations": [],
            "caveats": ["one source missing"],
        },
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)

    registry = ToolRegistry()
    register_subagent_tool(registry)

    await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "t"},
        ctx=ctx,
    )
    summaries = [e for e in bus.events if e.event_type == "lead.subagent_summary"]
    assert len(summaries) == 1
    assert summaries[0].payload["status"] == "succeeded_with_warnings"


# ─────────────────────────────────────────────────────────────
# Empty payloads must not claim report delivery: root cause of observed false claims on 116.
# ─────────────────────────────────────────────────────────────
def _empty_payload_result() -> SubagentResult:
    """peer 走完了但什么也没取到：如实写了 exec_summary，三个计数全 0。

    这是 `t_30eb9e8320d6` 的真实形状（三条 bi_query 全失败：1 次 60s 超时 + 2 次
    tool_exception），照抄下来当 fixture。
    """
    return SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="",
        payload={
            "title": "",
            "exec_summary": "数据查询超时，暂未取得可核验结果。",
            "insights": [],
            "kpis": [],
            "recommendations": [],
        },
    )


async def _spawn(result: SubagentResult) -> dict:
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)
    registry = ToolRegistry()
    register_subagent_tool(registry)
    invocation = await registry.execute(
        "subagent_spawn",
        arguments={"profile": "compute", "task": "各城市营业额和利润率对比"},
        ctx=ctx,
    )
    assert invocation.error is None
    return invocation.result


@pytest.mark.unit
async def test_empty_payload_never_claims_a_report_was_delivered() -> None:
    """★★116 生产实测（2026-08-25 `t_30eb9e8320d6`）：接口对 Lead 说了假话。

    peer 三条取数全失败、如实把 exec_summary 写成「数据查询超时」，canvas 也如实渲了；
    可给 Lead 的工具结果照样说「已通过 subagent_return 交了结构化报告 payload，
    composer 会自动渲染 canvas」，还要求「每句带一个本轮的具体数字」。
    手上没数字却被要求给数字 ⇒ Lead 编出「营业额最高的城市与利润率最高的城市并不重合」，
    发给了真实客户。

    这不是模型幻觉，是**同一句 directive 对「有内容」和「全空」两种 payload 都断言交付成功**。
    摘掉 `_delivered_nothing` 分叉这条必红。
    """
    res = await _spawn(_empty_payload_result())
    directive = res["directive"]

    # No delivery assertions may appear.
    assert "已通过 subagent_return 交了结构化报告" not in directive
    assert "composer 会自动渲染 canvas" not in directive
    # Do not require every sentence to contain a concrete number; that instruction forced fabrication.
    assert "每句带一个本轮的具体数字" not in directive
    # Explicitly state no output, forbidding delivery assertions and directional conclusions.
    assert "没有产出任何可交付内容" in directive
    assert "禁止" in directive


@pytest.mark.unit
async def test_lead_can_see_the_peer_own_reason() -> None:
    """★真话本来就在 payload.exec_summary 里，只是没进给 Lead 的那个 dict。

    Lead 要「如实转达」就得看得见它——短答分支一直带着 exec_summary，报告分支漏了，
    是接口不一致。
    """
    res = await _spawn(_empty_payload_result())
    assert res["payload_summary"]["exec_summary"] == "数据查询超时，暂未取得可核验结果。"


@pytest.mark.unit
async def test_non_empty_payload_still_gets_the_delivery_directive() -> None:
    """★负对照：有真内容时话术一个字不变——这条修复不许误伤正常轮。

    没有它，上面两条绿了也可能只是因为把整条 directive 删了。
    """
    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="",
        payload={
            "title": "各城市营业额排名",
            "exec_summary": "静安区居首。",
            "insights": [{"title": "头部三城断层领先", "body": "..."}],
            "kpis": [{"label": "第一名", "value": "1717.56"}],
            "recommendations": [],
        },
    )
    res = await _spawn(result)
    directive = res["directive"]
    assert "已通过 subagent_return 交了结构化报告" in directive
    assert "composer 会自动渲染 canvas" in directive
    assert "没有产出任何可交付内容" not in directive
    # Carry counts and exec_summary truthfully.
    assert res["payload_summary"]["insights_count"] == 1
    assert res["payload_summary"]["kpis_count"] == 1
    assert res["payload_summary"]["exec_summary"] == "静安区居首。"


@pytest.mark.unit
async def test_drawn_charts_count_as_delivered_even_with_empty_payload() -> None:
    """★★反方向的谎报：peer 画了图但 payload 三个计数全 0。

    只看计数就会告诉 Lead「没有产出任何可交付内容、canvas 上不会有图表」——
    而图就在画布上。用户看到图、却被告知什么都没有，比原来的谎报更难自证。
    2026-08-26 多角度 review 逮到，摘掉 `_drew_canvas` 这条必红。
    """
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="",
        payload={"title": "各城市营业额", "exec_summary": "静安区居首。", "insights": [], "kpis": [], "recommendations": []},
        tool_invocations=[
            ToolInvocation(tool_name="add_chart", arguments={}, result={"section_id": "sec_1"}),
        ],
    )
    res = await _spawn(result)
    assert "没有产出任何可交付内容" not in res["directive"], "画了图却被说成什么都没产出"


@pytest.mark.unit
async def test_failed_canvas_call_does_not_count_as_delivered() -> None:
    """★对照：canvas 调用**失败**时不算产出——否则又变回「说有其实没有」。"""
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="",
        payload={"title": "", "exec_summary": "数据查询超时。", "insights": [], "kpis": [], "recommendations": []},
        tool_invocations=[
            ToolInvocation(tool_name="add_chart", arguments={}, result=None, error="boom"),
        ],
    )
    res = await _spawn(result)
    assert "没有产出任何可交付内容" in res["directive"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_result", "error", "delivered"),
    [
        ({"section_id": "sec_1", "rev": 2}, None, "payload"),
        ({"error": "invalid_chart_change"}, None, "none"),
        (None, "unknown outcome", "none"),
        ({}, None, "none"),
    ],
)
async def test_chart_edit_receipt_counts_only_confirmed_writes(tool_result, error, delivered) -> None:
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="peer",
        ok=True,
        final_answer="",
        payload={"exec_summary": "Chart update", "insights": [], "kpis": [], "recommendations": []},
        tool_invocations=[ToolInvocation(tool_name="edit_chart", arguments={}, result=tool_result, error=error)],
    )
    res = await _spawn(result)
    assert res["delivered"] == delivered
    if delivered == "payload":
        assert "confirmed edit receipt" in res["directive"]
        assert "没有产出任何可交付内容" not in res["directive"]
        assert "最关键的发现" not in res["directive"]


def test_chart_edits_do_not_appear_as_new_sections() -> None:
    from bowhead_core.subagent import _canvas_sections_from_invocations
    from bowhead_core.types import ToolInvocation

    edits = [
        ToolInvocation(tool_name="edit_chart", arguments={}, result={"section_id": "sec_1", "rev": 2}),
        ToolInvocation(tool_name="edit_chart", arguments={}, result={"section_id": "sec_1", "error": "invalid_chart_change"}),
    ]
    assert _canvas_sections_from_invocations(edits) == []


@pytest.mark.unit
async def test_kpis_only_payload_counts_as_delivered() -> None:
    """只有 KPI、没有 insight 的 payload 是**真交付**，不能被当成空。

    判空必须三个计数一起看；只看 insights 会把「一张 KPI 卡」误判成没产出。
    """
    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=True,
        final_answer="",
        payload={
            "title": "营业额",
            "exec_summary": "总营业额 108.13 亿元。",
            "insights": [],
            "kpis": [{"label": "总营业额", "value": "108.13亿"}],
            "recommendations": [],
        },
    )
    res = await _spawn(result)
    assert "没有产出任何可交付内容" not in res["directive"]


def _result_with(payload: dict) -> SubagentResult:
    """同 `_empty_payload_result` 的形状，只换 payload —— 给下面三条用。"""
    return SubagentResult(profile="compute", subagent_id="x", ok=True, final_answer="", payload=payload)


@pytest.mark.unit
async def test_empty_payload_facts_do_not_say_success() -> None:
    """★ directive 分叉只是半边——**事实字段也不能说成功**。

    116 三条生产轮次实测：Lead 收到的 `ok=true` / `payload_returned=true` /
    `final_answer="（payload 已交给 composer 渲染）"` / `produced.verified` 全 true，
    **每一个都指向成功**，唯一的失败线索是埋在 summary 里的 `insights_count=0`。
    directive 说「没产出」而事实字段说「成功」时，接口在自相矛盾——模型信哪个都不奇怪。

    `delivered` 与 directive 同源、彼此印证：摘掉它，这条必红。
    """
    res = await _spawn(_empty_payload_result())
    assert res["delivered"] == "none"
    assert "没有产出任何可交付内容" in res["directive"]


@pytest.mark.unit
async def test_empty_payload_does_not_echo_an_assertive_title() -> None:
    """★ `payload_summary.title` 是 peer 写的**意图**标题，不是已达成的事实。

    `t_f81fec522d27` 实测回的是「各品牌退款率排名（前 20）」——而那个排名根本不存在，
    标题本身就是一句交付断言，摆在 `insights_count: 0` 旁边只会盖过它。
    """
    res = await _spawn(
        _result_with(
            {
                "title": "各品牌退款率排名（前 20）",
                "exec_summary": "数据查询超时，暂未取得可核验结果。",
                "insights": [],
                "kpis": [],
                "recommendations": [],
            },
        ),
    )
    assert res["payload_summary"]["title"] == ""
    # Preserve the truth: do not strip the peer's own explanation too.
    assert res["payload_summary"]["exec_summary"]


@pytest.mark.unit
async def test_non_empty_payload_keeps_delivered_and_title() -> None:
    """负对照：有内容的轮次不许被这两条改动波及。"""
    res = await _spawn(
        _result_with(
            {
                "title": "2026年7月各省利润率排名",
                "exec_summary": "上海利润率 18.21% 居首。",
                "insights": [{"title": "i", "thesis": "t", "narrative": "n" * 40, "evidence": []}],
                "kpis": [{"label": "整体利润率", "value": "17.39%"}],
                "recommendations": [],
            },
        ),
    )
    assert res["delivered"] == "payload"
    assert res["payload_summary"]["title"] == "2026年7月各省利润率排名"


@pytest.mark.unit
async def test_empty_payload_final_answer_is_not_a_delivery_claim() -> None:
    """★ 2026-08-26 review：`delivered` 那轮只补了五个「都指向成功」的字段里的一个。

    commit 自己列的清单是 `ok` / `payload_returned` / `final_answer` /
    `produced.verified` / `payload_summary.title`。`ok` 是拍板不动的，但
    `final_answer="（payload 已交给 composer 渲染）"` 是**字面上的交付断言**——
    它和同一个 dict 里的 `delivered:"none"` 直接打架，矛盾只是从
    「directive vs 事实」挪到了「`delivered` vs `final_answer`」。
    """
    res = await _spawn(_empty_payload_result())
    assert res["delivered"] == "none"
    assert res["final_answer"] == ""
    assert "composer" not in res["final_answer"]
    # Preserve the truth: do not lose the peer's own explanation too.
    assert res["payload_summary"]["exec_summary"]


@pytest.mark.unit
async def test_empty_payload_keeps_the_peers_own_prose_if_it_wrote_any() -> None:
    """peer 自己写了散文就照原样带 —— 留空的只是那句**平台造的** placeholder。"""
    r = _empty_payload_result()
    object.__setattr__(r, "final_answer", "三条查询全部超时，本轮没有可核验的数据。")
    res = await _spawn(r)
    assert res["delivered"] == "none"
    assert res["final_answer"] == "三条查询全部超时，本轮没有可核验的数据。"


@pytest.mark.unit
async def test_delivered_payload_still_gets_the_placeholder() -> None:
    """负对照：真交了东西的轮次不受影响（那句 placeholder 在这一支上本来就成立）。"""
    res = await _spawn(
        _result_with(
            {
                "title": "2026年7月各省利润率排名",
                "exec_summary": "上海利润率 18.21% 居首。",
                "insights": [{"title": "i", "thesis": "t", "narrative": "n" * 40, "evidence": []}],
                "kpis": [],
                "recommendations": [],
            },
        ),
    )
    assert res["delivered"] == "payload"
    assert res["final_answer"] == "（payload 已交给 composer 渲染）"


# ── Structured bail envelopes must reach Lead (2026-08-27 dogfood on 116, t_1ee75cbfb876) ──
#
# Peer uses the standard subagent_return(payload={"error":{"kind":...,"message":...}}) path;
# validate_payload's bail branch requires a nonempty message and accepts it. Yet spawn's returned
# payload_summary.exec_summary reads payload["exec_summary"], while the bail reason lives in
# payload["error"]["message"], dropping the peer's entire explanation.
# Observed cost: Lead sees no reason and launches another whole peer to rediscover it (80.8s / 43,358 tokens).
#
# ⚠ It does have a consumer: lead_orchestrator's Sprint µ P0-3 short circuit reads the aggregate payload,
# effective only at turn end. The broken half is within Lead's loop; every test below uses the real spawn handler.

_REAL_BAIL_MESSAGE = (
    "「火星省」不是本工作区「省」维度下的真实值。省维度实际可选值仅 5 个：上海、北京、天津、江苏、河南。请换成其中一个真实省份后重新提问。"
)


def _bail_result(kind: str = "domain_mismatch", message: str = _REAL_BAIL_MESSAGE) -> SubagentResult:
    """t_1ee75cbfb876 里 peer 真发出去的那份 payload（照抄形状）。"""
    return SubagentResult(
        profile="compute",
        subagent_id="x",
        ok=True,
        final_answer="",
        payload={"error": {"kind": kind, "message": message, "candidate_values": ["上海", "北京", "天津", "江苏", "河南"]}},
    )


@pytest.mark.unit
async def test_bail_envelope_reaches_the_lead_in_loop() -> None:
    """★ kind + message 必须出现在 Lead 收到的工具结果里，否则它只能瞎猜或重派。"""
    res = await _spawn(_bail_result())
    assert res["delivered"] == "none"  # Bail really delivered nothing.
    assert res["bail"]["kind"] == "domain_mismatch"
    assert res["bail"]["message"] == _REAL_BAIL_MESSAGE
    # Peer supplies extra candidate values (undeclared by schema); pass through unchanged when present, without selecting them here.
    assert res["bail"]["candidate_values"] == ["上海", "北京", "天津", "江苏", "河南"]


@pytest.mark.unit
async def test_bail_reason_fills_the_field_the_directive_points_at() -> None:
    """★ directive 写着「原因照抄 payload_summary.exec_summary」——bail 时那个字段
    必须真有东西，否则这句指令指向一个空字段（原样复现了 t_1ee75cbfb876）。"""
    res = await _spawn(_bail_result())
    assert res["payload_summary"]["exec_summary"] == _REAL_BAIL_MESSAGE
    assert "原因照抄" in res["directive"]


@pytest.mark.unit
async def test_bail_directive_does_not_misattribute_domain_mismatch_to_a_timeout() -> None:
    """★ 旧文案一律说「通常是取数超时/失败」。`domain_mismatch` 不是取数失败，
    而且换措辞重派一百次也一样（同 `plan_driver._RETRYABLE_KINDS` 的取舍）。"""
    res = await _spawn(_bail_result("domain_mismatch"))
    d = res["directive"]
    assert "取数超时" not in d
    assert "bail.kind=domain_mismatch" in d
    assert "不要**换措辞重派" in d
    # Shared ban on delivery assertions/directional conclusions must survive this branch.
    assert "禁止" in d


@pytest.mark.unit
async def test_transient_bail_keeps_retry_advice() -> None:
    """负对照（同族异 kind）：`transient` 是暂时性失败，不能被劝成「别重试」。"""
    res = await _spawn(_bail_result("transient", "BI 查询连续 3 次超时。"))
    d = res["directive"]
    assert "bail.kind=transient" in d
    assert "可以重试一次" in d
    assert "不要**换措辞重派" not in d


@pytest.mark.unit
async def test_non_bail_payload_is_untouched_by_the_bail_branch() -> None:
    """负对照：正常交付的轮次不许出现 bail 字段，归因文案也不许被改。"""
    res = await _spawn(
        _result_with(
            {
                "title": "2026年7月各省利润率排名",
                "exec_summary": "上海利润率 18.21% 居首。",
                "insights": [{"title": "i", "thesis": "t", "narrative": "n" * 40, "evidence": []}],
                "kpis": [],
                "recommendations": [],
            },
        ),
    )
    assert "bail" not in res
    assert res["delivered"] == "payload"


@pytest.mark.unit
async def test_empty_payload_without_bail_keeps_the_timeout_attribution() -> None:
    """负对照：没有 bail 信封的零产出（真·取数失败那档）文案保持原样。"""
    res = await _spawn(_empty_payload_result())
    assert "bail" not in res
    assert "通常是取数超时/失败" in res["directive"]


@pytest.mark.unit
async def test_needs_user_input_bail_asks_the_lead_to_relay_the_question() -> None:
    """`needs_user_input` 与 `transient` 的处置**相反**：它要的是先问用户，不是重试。
    突变测试发现这一档原本没有用例（把它并进「暂时性」分支，测试全绿）。"""
    res = await _spawn(_bail_result("needs_user_input", "「客单」在本工作区有两个候选指标，请用户选一个。"))
    d = res["directive"]
    assert "bail.kind=needs_user_input" in d
    assert "转达给用户" in d
    assert "可以重试一次" not in d


@pytest.mark.unit
async def test_bail_discriminator_matches_the_convergence_guard() -> None:
    """★ 「什么算 bail」现在有**两处**在判：这里，和 ξ B1 收敛闸
    `LeadDispatchGuard.has_non_bail_payload`。两处必须同判，否则会出现
    「闸认为 peer 已交付、而 Lead 收到 bail」这种自相矛盾。

    判据都是 `error` 是 dict **且** `kind` 非空 —— 缺 kind 的畸形信封不算 bail
    （突变测试逮到：把 kind 这个条件去掉，全部测试仍绿）。
    """
    from bowhead_core.subagent import LeadDispatchGuard

    for payload, is_bail in (
        ({"error": {"kind": "domain_mismatch", "message": "m"}}, True),
        ({"error": {"message": "缺 kind 的畸形信封"}}, False),
        ({"error": {}}, False),
        ({"error": "字符串不是信封"}, False),
    ):
        guard = LeadDispatchGuard(payloads_ref=[{"payload": payload}], after_payload_deny=frozenset())
        # Gate says not a delivered payload ⟺ this side says bail.
        assert guard.has_non_bail_payload() is (not is_bail), payload

    # Malformed envelope without kind does not enter bail: no bail field, unchanged attribution copy.
    res = await _spawn(
        SubagentResult(profile="compute", subagent_id="x", ok=True, final_answer="", payload={"error": {"message": "无 kind"}}),
    )
    assert "bail" not in res
    assert "通常是取数超时/失败" in res["directive"]


@pytest.mark.unit
async def test_domain_mismatch_directive_does_not_contradict_itself() -> None:
    """★ 自相矛盾闸：同一段 directive 不能一边说「做不到、别重派」，一边又给
    通用的「可以建议用户重试」。可行动建议已经在 peer 的 message 里。"""
    d = (await _spawn(_bail_result("domain_mismatch")))["directive"]
    assert "不要**换措辞重派" in d
    assert "建议用户重试" not in d
    # Transient cases differ: preserve retry suggestions.
    d2 = (await _spawn(_bail_result("transient", "BI 连续超时。")))["directive"]
    assert "建议用户重试" in d2
    # Non-bail zero-output cases (actual retrieval failure) also retain them.
    assert "建议用户重试" in (await _spawn(_empty_payload_result()))["directive"]


@pytest.mark.unit
async def test_failed_after_write_report_tells_lead_the_body_is_on_canvas() -> None:
    """★★116 A3 / C5（canvas-elements tech-design-mixed-turn-chrome §1.3）：peer `write_report` 成功落 9 章后，
    下一圈 LLM 调用挂死 ⇒ `ok=False`。此前 directive 一律说「没产出可靠结论、告诉用户 insufficient_data」，
    而同一个 dict 里 `produced.canvas_sections` 是 9——两句打架，Lead 两次都重派了一个 peer 去「补完」，
    第二个 peer 把画布正文复述成 payload ⇒ 同轮两份报告。摘掉 `_failed_after_write` 分叉这条必红。"""
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=False,
        final_answer="",
        error="subagent_internal_error",
        tool_invocations=[
            ToolInvocation(tool_name="bi_query", arguments={}, result={"rows": []}),
            ToolInvocation(
                tool_name="write_report",
                arguments={"kind": "narrative"},
                result={"ok": True, "section_id": "sec_a", "section_ids": ["sec_a", "sec_b", "sec_c"], "chapters": 3},
            ),
        ],
    )
    res = await _spawn(result)
    assert res["ok"] is False, "流程确实没走完，ok 不改语义"
    assert res["error_code"] == "subagent_failed"
    assert res["delivered"] == "canvas"
    assert res["canvas_written"] == {"tool_calls": 1, "section_ids": ["sec_a", "sec_b", "sec_c"]}
    directive = res["directive"]
    assert "正文已落画布之后" in directive
    assert "不要重派" in directive
    assert "未能产出可靠结论" not in directive
    # insufficient_data may appear only within prohibitions on saying it; do not instruct Lead to tell users that.
    assert "直接告诉用户 insufficient_data" not in directive


@pytest.mark.unit
async def test_failed_without_canvas_write_keeps_the_old_failure_directive() -> None:
    """★正对照：崩了且**没有**成功写过画布（write_report 被拒 / 只跑了 bi_query）⇒ 原话一个字不变。
    没有它，上一条绿了也可能只是把整条失败 directive 换掉了。"""
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=False,
        final_answer="",
        error="subagent_internal_error",
        tool_invocations=[
            ToolInvocation(tool_name="bi_query", arguments={}, result={"rows": []}),
            ToolInvocation(tool_name="write_report", arguments={}, result={"error": "not_owner", "owner": "sub_prev"}),
        ],
    )
    res = await _spawn(result)
    assert res["ok"] is False
    assert "delivered" not in res and "canvas_written" not in res
    assert "未能产出可靠结论" in res["directive"]
    assert "直接告诉用户 insufficient_data" in res["directive"]


@pytest.mark.unit
async def test_chart_only_crash_does_not_claim_body_is_on_canvas() -> None:
    """★★review 第 2 条：peer 只画了张图（或只删了段）就崩，`_canvas_write_succeeded` 同样为真。
    此前会告诉 Lead「正文已落画布、用 2-3 句点出正文里的关键发现、每句带数字」——而画布上没有正文，
    正是本模块其它分支拼命在防的编数路径。判据必须窄到 write_report 真成功。"""
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=False,
        final_answer="",
        error="subagent_internal_error",
        tool_invocations=[ToolInvocation(tool_name="add_chart", arguments={}, result={"section_id": "sec_c1"})],
    )
    res = await _spawn(result)
    assert "正文已落画布之后" not in res["directive"], "只画了图却说正文已落"
    assert res.get("delivered") != "canvas"
    assert "未能产出可靠结论" in res["directive"]


@pytest.mark.unit
async def test_buffered_edit_only_crash_does_not_claim_zero_sections() -> None:
    """★同族：缓冲型工具（patch/delete/reorder）成功返回 `{"buffered": True}`，没有 section_ids。
    此前 directive 会渲成「画布上已有它成功写入的 0 段」，`canvas_written.section_ids` 是空数组。"""
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="canvas-writer",
        subagent_id="will_be_overwritten",
        ok=False,
        final_answer="",
        error="subagent_internal_error",
        tool_invocations=[
            ToolInvocation(tool_name="canvas_patch_section", arguments={}, result={"buffered": True, "op": "patch", "section_id": "sec_x"})
        ],
    )
    res = await _spawn(result)
    assert "0 段" not in res["directive"]
    assert "canvas_written" not in res
    assert "未能产出可靠结论" in res["directive"]


@pytest.mark.unit
async def test_kb_spawn_receipt_uses_executed_counts_instead_of_model_claims() -> None:
    from types import SimpleNamespace

    result = SubagentResult(
        profile="kb-analyst",
        subagent_id="sub_kb",
        ok=True,
        final_answer="",
        payload={"delivery_surface": "chat", "exec_summary": "No evidence found after nine searches."},
        tool_invocations=[
            SimpleNamespace(tool_name="kb_search", arguments={"query": f"topic {i}"}, result={"chunks": []}, error=None) for i in range(6)
        ],
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)
    registry = ToolRegistry()
    register_subagent_tool(registry)
    out = await registry.execute("subagent_spawn", arguments={"profile": "kb-analyst", "task": "Find Skill documents", "delivery": "chat"}, ctx=ctx)
    assert out.error is None
    assert out.result["retrieval"] == {"searches_executed": 6, "searches_succeeded": 6}
    assert "actual retrieval receipt" in out.result["directive"]


@pytest.mark.unit
async def test_a_run_stopped_at_a_limit_is_reported_as_partial_not_failed() -> None:
    """Agent run limits: partial work reaches Lead with a directive; events say partial, not failed."""
    result = SubagentResult(
        profile="compute",
        subagent_id="will_be_overwritten",
        ok=False,
        final_answer="Q1 revenue verified.",
        error="run_limit_time_limit",
        completion="partial",
        stop_reason="time_limit",
        partial_done="Q1 revenue verified.",
        partial_remaining="Fetch Q2 and compare.",
    )
    bus = _FakeBus()
    ctx = _FakeCtx(bus)
    ctx.subagent_runner = _FakeRunner(result)
    registry = ToolRegistry()
    register_subagent_tool(registry)

    out = await registry.execute("subagent_spawn", arguments={"profile": "compute", "task": "compare quarters"}, ctx=ctx)

    assert out.result["completion"] == "partial" and out.result["stop_reason"] == "time_limit"
    assert (out.result["done"], out.result["remaining"]) == ("Q1 revenue verified.", "Fetch Q2 and compare.")
    assert "Never present it as complete" in out.result["directive"]
    assert [e for e in bus.events if e.event_type == "peer_dispatch_failed"] == []
    summary = next(e.payload for e in bus.events if e.event_type == "lead.subagent_summary")
    assert summary["status"] == "partial" and summary["failure_reason"] is None and summary["diagnostics"] is None
    assert (summary["stop_reason"], summary["partial_remaining"]) == ("time_limit", "Fetch Q2 and compare.")

    again = await registry.execute("subagent_spawn", arguments={"profile": "compute", "task": "compare quarters"}, ctx=ctx)
    assert again.result["error_kind"] == "run_limit_partial"


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["llm_context_overflow", "llm_unavailable"])
async def test_model_failure_preserves_successful_tool_receipt_counts(reason: str) -> None:
    from bowhead_core.types import ToolInvocation

    result = SubagentResult(
        profile="compute",
        subagent_id="sub_receipts",
        ok=False,
        final_answer="",
        error=reason,
        llm_reason_code=reason,
        tool_invocations=[
            ToolInvocation(tool_name="python_sandbox", arguments={}, result={"ok": True, "stdout": "14535"}),
            ToolInvocation(tool_name="python_sandbox", arguments={}, result={"ok": False, "error": "script_failed"}),
            ToolInvocation(tool_name="web_fetch", arguments={}, error="network_error"),
        ],
    )
    response = await _spawn(result)
    assert response["ok"] is False
    assert response["tool_count"] == 3
    assert response["failure_stage"] == "model_request"
    assert response["llm_reason_code"] == reason
    assert response["tool_execution"] == {
        "recorded": 3,
        "succeeded": 1,
        "failed": 2,
        "non_failure": 0,
        "unconfirmed": 0,
        "by_tool": {
            "python_sandbox": {"succeeded": 1, "failed": 1, "non_failure": 0, "unconfirmed": 0},
            "web_fetch": {"succeeded": 0, "failed": 1, "non_failure": 0, "unconfirmed": 0},
        },
    }
    assert "tool_execution" in response["directive"]
    assert "does not undo" in response["directive"]
    assert "stdout" not in response["tool_execution"]


@pytest.mark.unit
async def test_failure_receipts_do_not_promote_pauses_or_missing_outcomes_to_success() -> None:
    from bowhead_core.types import ToolInvocation

    response = await _spawn(
        SubagentResult(
            profile="compute",
            subagent_id="sub_receipts",
            ok=False,
            final_answer="",
            error="subagent_execution_uncertain",
            ordinary_completion_reason="tool_unknown",
            tool_invocations=[
                ToolInvocation(tool_name="python_sandbox", arguments={}, result={"ok": True}),
                ToolInvocation(tool_name="web_fetch", arguments={}, result={"error": "denied"}),
                ToolInvocation(
                    tool_name="bi_query",
                    arguments={},
                    result={"ok": False, "error_kind": "needs_clarification", "clarification": {"options": ["a", "b"]}},
                ),
                ToolInvocation(tool_name="external_write", arguments={}),
                ToolInvocation(tool_name="subagent_spawn", arguments={}, result={"ok": False, "execution_outcome": "unknown"}),
            ],
        )
    )
    assert response["execution_outcome"] == "unknown"
    assert "failure_stage" not in response
    assert response["tool_execution"]["succeeded"] == 1
    assert response["tool_execution"]["failed"] == 1
    assert response["tool_execution"]["non_failure"] == 1
    assert response["tool_execution"]["unconfirmed"] == 2
    assert response["tool_execution"]["recorded"] == 5
