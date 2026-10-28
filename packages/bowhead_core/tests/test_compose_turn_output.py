"""_compose_turn_output characterization 测试 (planning-mode-audit WP-4).

抽取自 _lead_loop 尾部的 compose+文案定稿共享块 —— 这些测试钉住抽取后的行为
契约(gate/文案兜底链/参数化点), 是 WP-5 plan 接入前的等价性防线(FE golden
replay 护不住后端块, review 契约-F5)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import _compose_turn_output

# Review §5.10 R-7: no I/O here (canvas_store object()/stub, monkeypatch throughout),
# yet no marker ⇒ `-m unit` never collected these 23 cases, pinning compose path repeatedly
# changed this sprint. Same as test_plan_report_gaps.py (§5.9 F-4).
pytestmark = pytest.mark.unit


@dataclass
class _FakeBus:
    events: list[Any] = field(default_factory=list)
    _pi_turn_state: dict[str, Any] = field(default_factory=dict)

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


def _mk_runtime(*, canvas_store: Any = None, psi_done: bool = False) -> Any:
    bus = _FakeBus()
    if psi_done:
        bus._psi_compose_done = True  # type: ignore[attr-defined]
    ctx = SimpleNamespace(
        canvas_store=canvas_store,
        artifact_store=None,
        member_uid="",
        language_context=None,
        _m137_subagent_payloads=[],
    )
    return SimpleNamespace(parent_bus=bus, parent_ctx=ctx, router=None)


def _mk_sctx() -> Any:
    return SimpleNamespace(
        turn_id="t_cto",
        session_id="s_cto",
        workspace_id="w",
        user_id="u",
        prior_turn_status=None,
    )


def _mk_result(final_answer: str = "", payload: dict | None = None, intent: Any = None) -> Any:
    return SimpleNamespace(
        final_answer=final_answer,
        payload=payload,
        intent=intent,
        tool_invocations=[],
    )


@pytest.mark.asyncio
async def test_chat_direct_emits_synthetic_delta_matching_final_text() -> None:
    """★批5 review#2 F-3：chat 直达的合成 narrative.delta **发在终答定稿处**，
    且与返回的 text_for_chat **逐字相同**。

    发早了（loop 刚返回时）文本还会被 `_resolve_citations`/deck caveat/ledger 守卫
    继续改写 ⇒ beat 与气泡不同字 ⇒ FE 80 字符双向去重探针两侧都不命中 ⇒ 同文两显，
    且 beat 显示的是**未纠正**的版本。钉住「同源」这个不变量。
    """
    runtime = _mk_runtime()
    result = _mk_result(final_answer="2026 年营业额约 106.62 亿元。")
    result.chat_direct_delivered = True  # type: ignore[attr-defined]
    _kinds, text = await _compose_turn_output(
        runtime=runtime,
        sctx=_mk_sctx(),
        result=result,
        aggregated_payload=None,
        user_text="今年营业额是多少",
        peer_payloads=[],
    )
    deltas = [e for e in runtime.parent_bus.events if getattr(e, "event_type", "") == "narrative.delta"]
    assert len(deltas) == 1, f"应恰好补一条合成 delta，实际 {len(deltas)}"
    # Payload field is `delta`, not `text`: contract from actual _emit_narrative_delta emit.
    assert (deltas[0].payload or {}).get("delta") == text, "合成 delta 必须与终答逐字同源"


@pytest.mark.asyncio
async def test_chat_direct_replaces_prior_narrative_with_final_text() -> None:
    runtime = _mk_runtime()
    runtime.parent_ctx._lead_narrative_text = "An earlier explanation."
    result = _mk_result(final_answer="Revenue: 800\nProfit: 120\nMargin: 15.0%")
    result.chat_direct_delivered = True
    _, text = await _compose_turn_output(
        runtime=runtime,
        sctx=_mk_sctx(),
        result=result,
        aggregated_payload=None,
        user_text="Return exactly three lines.",
        peer_payloads=[],
    )
    assert runtime.parent_ctx._lead_narrative_superseded is True
    deltas = [event.payload["delta"] for event in runtime.parent_bus.events if event.event_type == "narrative.delta"]
    assert deltas == [text] == [result.final_answer]


@pytest.mark.asyncio
async def test_non_chat_direct_turn_emits_no_synthetic_delta() -> None:
    """正对照：普通轮不补 delta（它的 delta 由流式回调发过，补了就是双显）。"""
    runtime = _mk_runtime()
    _kinds, _text = await _compose_turn_output(
        runtime=runtime,
        sctx=_mk_sctx(),
        result=_mk_result(final_answer="Lead 自己写的收尾"),
        aggregated_payload=None,
        user_text="q",
        peer_payloads=[],
    )
    assert not [e for e in runtime.parent_bus.events if getattr(e, "event_type", "") == "narrative.delta"]


@pytest.mark.asyncio
async def test_text_chain_final_answer_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """result.final_answer 优先 + response_kind marker 剥除。"""
    kinds, text = await _compose_turn_output(
        runtime=_mk_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result(final_answer="答案正文<response_kind>chat</response_kind>"),
        aggregated_payload={},
        user_text="q",
        peer_payloads=[],
    )
    assert kinds == []
    assert text == "答案正文"


@pytest.mark.asyncio
async def test_text_chain_payload_fallback_order() -> None:
    """final_answer 空 → exec_summary > title > narrative > fallback_markdown。
    fallback_markdown 是 plan writer 拒稿 prose 的唯一出口(discovery P0-3)。"""
    base = {"fallback_markdown": "FB", "narrative": "NA", "title": "TI", "exec_summary": "ES"}
    for missing, expect in (((), "ES"), (("exec_summary",), "TI"), (("exec_summary", "title"), "NA"), (("exec_summary", "title", "narrative"), "FB")):
        payload = {k: v for k, v in base.items() if k not in missing}
        _kinds, text = await _compose_turn_output(
            runtime=_mk_runtime(),
            sctx=_mk_sctx(),
            result=_mk_result(),
            aggregated_payload=payload,
            user_text="q",
            peer_payloads=[],
        )
        assert text == expect, (missing, text)


@pytest.mark.asyncio
async def test_no_canvas_store_skips_compose() -> None:
    kinds, _text = await _compose_turn_output(
        runtime=_mk_runtime(canvas_store=None),
        sctx=_mk_sctx(),
        result=_mk_result(payload=None),
        aggregated_payload={"exec_summary": "x"},
        user_text="q",
        peer_payloads=[],
        force_canvas=True,
    )
    assert kinds == []


@pytest.mark.asyncio
async def test_peer_chat_declaration_reaches_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 批 5ⓐ review-R1（P0）：peer 的 chat 声明必须真的进得了决定链。

    `peer_payloads` 是给 composer 用的、**已被 `is_short_answer_payload` 滤掉短答**的那份；
    autonomy 下「短答」＝「声明了 chat」——拿它判交付面，peer 档的 chat 半边永远走不到
    （headline 功能在生产路上是死的）。决定必须读未过滤的 `peer_payloads_all`。
    """
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    runtime = _mk_runtime(canvas_store=object())
    chat_payload = {"profile": "compute", "payload": {"delivery_surface": "chat", "exec_summary": "今年 GMV 5.2 亿"}}
    kinds, _text = await _compose_turn_output(
        runtime=runtime,
        sctx=_mk_sctx(),
        result=_mk_result(intent=None),
        aggregated_payload={},
        user_text="今年 GMV 多少",
        peer_payloads=[],  # Composer: short answers filtered (production shape).
        peer_payloads_all=[chat_payload],  # Decision: unfiltered.
    )
    decision = getattr(runtime.parent_bus, "_delivery_decision", None)
    assert decision == {"surface": "chat", "tier": "peer"}, decision
    assert kinds == [], "chat 声明不该成稿"


async def test_decision_falls_back_when_only_filtered_bag_given() -> None:
    """负对照：不传 `peer_payloads_all` 时行为回落到旧签名（不报错、不假装有声明）。"""
    runtime = _mk_runtime(canvas_store=object())
    await _compose_turn_output(
        runtime=runtime,
        sctx=_mk_sctx(),
        result=_mk_result(intent=None),
        aggregated_payload={},
        user_text="q",
        peer_payloads=[],
    )
    decision = getattr(runtime.parent_bus, "_delivery_decision", None)
    assert decision is not None and decision["tier"] != "peer"


def test_live_lead_callsite_passes_the_unfiltered_bag() -> None:
    """漂移闸：生产调用点必须同时传两份 bag。

    只测函数不测调用点，正是 R1 那个 bug 的形状（函数写对了、生产传的是过滤后的）。
    """
    from pathlib import Path as _P

    import bowhead_core.agents.lead_orchestrator as _lo

    src = _P(_lo.__file__).read_text(encoding="utf-8")
    assert "peer_payloads=_peer_bag_for_compose," in src
    assert 'peer_payloads_all=list(getattr(runtime.parent_ctx, "_m137_subagent_payloads", None) or []),' in src


async def test_force_canvas_reaches_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """参数化点②: force_canvas=True 短路 response_kind 分派直达 composer;
    aggregated_payload 为空也进(plan fallback 场景)。writer-loop/author 关闭
    → 走确定性 compose_canvas_from_payload(monkeypatch 探针)。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    called: dict[str, Any] = {}

    async def _probe(**kw: Any) -> list[str]:
        called["payload"] = kw.get("payload")
        called["intent"] = kw.get("intent")
        return ["meta", "narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_runtime(canvas_store=object()),
        sctx=_mk_sctx(),
        result=_mk_result(intent=None),
        aggregated_payload={"exec_summary": "只有摘要"},
        user_text="q",
        peer_payloads=[],
        force_canvas=True,
    )
    assert kinds == ["meta", "narrative"]
    assert called["payload"] == {"exec_summary": "只有摘要"}


@pytest.mark.asyncio
async def test_normal_path_chat_rk_skips_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通路等价性: 无 spawn/无 canvas invocation/无 payload → rk=chat → composer 不跑
    (抽取前行为: _composer_entry_ok False)。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    called = {"n": 0}

    async def _probe(**kw: Any) -> list[str]:
        called["n"] += 1
        return ["meta"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, text = await _compose_turn_output(
        runtime=_mk_runtime(canvas_store=object()),
        sctx=_mk_sctx(),
        result=_mk_result(final_answer="闲聊回答"),
        aggregated_payload={},
        user_text="你好",
        peer_payloads=[],
    )
    assert called["n"] == 0
    assert kinds == []
    assert text == "闲聊回答"


@pytest.mark.asyncio
async def test_psi_eager_compose_dedup_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """ψ-E 去重旗: bus._psi_compose_done → composer 不重跑(抽取保留)。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    called = {"n": 0}

    async def _probe(**kw: Any) -> list[str]:
        called["n"] += 1
        return ["meta"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_runtime(canvas_store=object(), psi_done=True),
        sctx=_mk_sctx(),
        result=_mk_result(),
        aggregated_payload={"exec_summary": "x"},
        user_text="q",
        peer_payloads=[],
        force_canvas=True,
    )
    assert called["n"] == 0
    assert kinds == []


@pytest.mark.asyncio
async def test_peer_payloads_thread_into_writer_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """参数化点①钉子(WP-4 review P2-1): 非空 peer_payloads 必须原样穿线进
    compose_report(multi-writer 判定依据) —— 此前只测 []→[]。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "1")
    got: dict[str, Any] = {}

    async def _probe_report(**kw: Any) -> list[str]:
        got["peer_payloads"] = kw.get("peer_payloads")
        return ["meta", "narrative"]

    import bowhead_core.report_writer_loop as rwl

    monkeypatch.setattr(rwl, "compose_report", _probe_report)
    bag = [{"title": "a"}, {"title": "b"}]
    kinds, _text = await _compose_turn_output(
        runtime=_mk_runtime(canvas_store=object()),
        sctx=_mk_sctx(),
        result=_mk_result(),
        aggregated_payload={"exec_summary": "x"},
        user_text="q",
        peer_payloads=bag,
        force_canvas=True,
    )
    assert kinds == ["meta", "narrative"]
    assert got["peer_payloads"] == bag


# ── 116 production 1.0.79 dogfood P1: no compose for content-free turns ──


def _mk_result_with_tools(payload: dict | None, tools: list[str]) -> Any:
    r = _mk_result(payload=payload)
    r.tool_invocations = [SimpleNamespace(tool_name=t) for t in tools]
    return r


def _mk_canvas_runtime() -> Any:
    """rk=canvas 的 runtime。

    ⚠ 没有这一步，下面几条测试全是**假绿**：本 harness 里 `_compute_response_kind_dispatch`
    对无 spawn/无 canvas_add_* 的 turn 判 rk=chat，composer 压根不会被调用——于是
    「composer 没被调用」这个断言在守卫摘掉后照样成立（实测过）。用 pre_flight_override
    把 rk 钉成 canvas，测试才真的穿过这道闸。
    """
    rt = _mk_runtime(canvas_store=object())
    rt.parent_ctx.response_kind_pre_flight = "canvas"
    return rt


@pytest.mark.asyncio
async def test_export_only_turn_does_not_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 穿过闸本身：`t_5b91c70281c9` 的真实形状进来，composer **一次都不能被调用**。

    修复前：这一轮 compose 出一份「标题 = 用户那句导出指令」的伪报告，把真报告 11 段
    全 supersede，于是下一次导出的 xlsx 只有 5117 字节、一个数据都没有。
    """
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "meta", "narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(
            {
                "title": "",
                "exec_summary": "Word 文档已生成：**2026年7月各省经营分析报告.docx**",
                "kpis": [],
                "insights": [],
                "recommendations": [],
                "sources": [],
            },
            ["skill_read", "skill_invoke", "subagent_return", "subagent_spawn"],
        ),
        aggregated_payload={"title": "", "exec_summary": "Word 文档已生成。", "kpis": [], "insights": [], "recommendations": [], "sources": []},
        user_text="把画布上这份报告导出成 Word 文档（.docx）给我",
        peer_payloads=[],
    )
    assert calls == [], "无内容轮不该进 composer"
    assert kinds == []


@pytest.mark.asyncio
async def test_real_report_turn_still_composes(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 反向闸：有 KPI 的真报告必须照常 compose——守卫收得太紧会把报告整个吃掉。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "kpi_row", "narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result_with_tools({"kpis": [{"label": "营收", "value": "9.80亿元"}]}, ["bi_query", "add_chart"]),
        aggregated_payload={"title": "2026年7月各省经营分析报告", "kpis": [{"label": "营收", "value": "9.80亿元"}]},
        user_text="帮我做一份本月各省营业额与利润的经营分析报告",
        peer_payloads=[],
    )
    assert len(calls) == 1, "真报告被守卫误挡"
    assert kinds == ["h1", "kpi_row", "narrative"]


@pytest.mark.asyncio
async def test_bi_only_contentless_turn_skips_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """delivery-surface C1：payload 无结论、全轮只有 bi 读取 ⇒ **不** compose。

    旧断言保护的「Lead 偷懒 bi-only path」（空 payload 也进 composer 让
    bi-fallback 强行成报告）正是「简单查数被写成画布报告」的通路之一
    （116 t_b816e1879d6d / t_0f32354e8f81，discovery §3.2）——按 tech-design
    §4.4 该形态改为 skip：答案留在 chat，数据留在「数据」。
    真报告不受影响：上一条 test_real_report_not_blocked 是它的正对照。
    """
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "data_table"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result_with_tools({"exec_summary": "查到了"}, ["bi_query"]),
        aggregated_payload={"exec_summary": "查到了"},
        user_text="上个月营收多少",
        peer_payloads=[],
    )
    assert calls == [], "bi-only 无内容轮不该进 composer（C1 准入证据化）"
    assert kinds == []


@pytest.mark.asyncio
async def test_user_no_report_directive_skips_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """delivery-surface C5 消费点①：用户显式「不要写报告」压过一切机械判据。

    即使 payload 有真报告内容 + 调了 add_chart（rule 2 本会判 canvas），
    显式指令仍让本轮不成稿——优先级链最高位（tech-design §1）。
    正对照 = 上面的 test_real_report_turn_still_composes（同 payload 无指令 → compose）。
    """
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "kpi_row"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result_with_tools({"kpis": [{"label": "营收", "value": "9.80亿元"}]}, ["bi_query", "add_chart"]),
        aggregated_payload={"title": "报告", "kpis": [{"label": "营收", "value": "9.80亿元"}]},
        user_text="查一下各省营业额，直接告诉我就行，不要写报告。",
        peer_payloads=[],
    )
    assert calls == [], "用户显式拒报告后仍进 composer（C5 消费点①失效）"
    assert kinds == []


@pytest.mark.asyncio
async def test_payload_only_report_turn_composes(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ live 负对照 E 逮到的 P0（批11）：peer 只返报告级 payload、没画任何图
    （无 canvas_add_* invocation）→ C2 删 rule5 后 tool-shape 判 chat → 写手
    永不被调用 → 报告丢失（卡片模式靠 ψ-E eager compose 幸存，写手模式全灭）。
    修法=优先级链第三档：payload 有报告级内容（证据）→ compose。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "kpi_row", "insight_card", "narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, ["subagent_spawn"]),  # No canvas_add_*.
        aggregated_payload={
            "title": "经营分析报告",
            "kpis": [{"label": "总营收", "value": "3623万"}],
            "insights": [{"title": "集中度", "narrative": "CR3 48.7%"}],
            "recommendations": [{"priority": "P1", "action": "关注 D 店"}],
        },
        user_text="帮我写一份经营分析报告放到画布上",
        peer_payloads=[],
    )
    assert len(calls) == 1, "payload-only 报告轮必须 compose（写手/代笔的存在意义）"
    assert kinds


@pytest.mark.asyncio
async def test_lead_spawn_delivery_chat_wins_over_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级链第二档：Lead spawn 时 delivery='chat' 压过 payload 证据
    （Lead 看得到用户诉求，payload 只是 peer 建议）。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    r = _mk_result_with_tools(None, [])
    r.tool_invocations = [SimpleNamespace(tool_name="subagent_spawn", arguments={"profile": "compute", "task": "t", "delivery": "chat"})]
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=r,
        aggregated_payload={"kpis": [{"label": "x", "value": "1"}], "insights": [{"title": "i"}]},
        user_text="查一下",
        peer_payloads=[],
    )
    assert calls == [], "Lead delivery=chat 必须压过 payload 证据"
    assert kinds == []


@pytest.mark.asyncio
async def test_plan_answer_delivery_override_beats_payload_and_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """review R5-2：plan 判 ANSWER 时 caller 传 delivery_override='chat'——它占
    优先级链第三档，压过 pre_flight（canvas runtime 钉了）与 payload 证据。
    plan 聚合会把各 leg exec_summary 折成 insights（制造证据），没有这一档
    ANSWER plan 必被证据层顶回 canvas。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),  # pre_flight=canvas; override must win.
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, ["subagent_spawn"]),
        aggregated_payload={"insights": [{"title": "折叠自 leg exec_summary"}], "kpis": [{"label": "x", "value": "1"}]},
        user_text="按步骤查三个数",
        peer_payloads=[],
        delivery_override="chat",
    )
    assert calls == [], "ANSWER override 必须压过 payload 证据与 pre_flight"
    assert kinds == []


@pytest.mark.asyncio
async def test_nested_canvas_write_admits_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """review R5-6 证据层①：peer 的 write_report/canvas_add_* 挂在 spawn inv 的
    nested 里（顶层只有 subagent_spawn）——薄 payload 轮也必须进 composer。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    rt = _mk_runtime(canvas_store=object())  # No pre_flight: evidence decides.
    r = _mk_result_with_tools(None, [])
    r.tool_invocations = [
        SimpleNamespace(
            tool_name="subagent_spawn",
            arguments={"profile": "compute", "task": "t"},
            nested=[SimpleNamespace(tool_name="write_report", arguments={})],
        )
    ]
    kinds, _text = await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=r,
        aggregated_payload={"exec_summary": "peer 已亲笔，payload 薄"},
        user_text="分析一下",
        peer_payloads=[],
    )
    assert len(calls) == 1, "嵌套 write_report 是画布写入证据，必须放行 composer"


@pytest.mark.asyncio
async def test_peer_authored_narrative_admits_composer_without_invocations(monkeypatch: pytest.MonkeyPatch) -> None:
    """review R5-6 证据层③ + no-content 闸补口：invocation 记录缺席（截断/澄清
    续跑）但画布上已有 owner=sub_* 的亲笔 narrative → 仍须进 composer 补
    header/Sources。守卫摘除敏感性：证据层③或闸补口任一摘掉本测试即红。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)

    class _StoreWithPeerNarr:
        async def list_sections_appended_by_turn(self, turn_id: str):
            assert turn_id == "t_cto"
            return [SimpleNamespace(kind="narrative", owner="sub_lawyer001")]

    rt = _mk_runtime(canvas_store=_StoreWithPeerNarr())
    kinds, _text = await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, []),  # Zero invocations: evidence①② false.
        aggregated_payload={"exec_summary": "薄 payload"},
        user_text="分析一下",
        peer_payloads=[],
    )
    assert len(calls) == 1, "亲笔 narrative 在库=确凿产出，composer 必须放行"


@pytest.mark.asyncio
async def test_peer_authored_narrative_admits_composer_without_any_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★116 A3 / C5（canvas-elements tech-design-mixed-turn-chrome §2.3）：peer `write_report` 成功后崩、
    没有 subagent_return ⇒ `aggregated_payload is None`。此前准入四项全假 ⇒ 收尾器进不来 ⇒ 画布上那份
    亲笔正文连来源 / 标题都没有（Lead 不再重派后这就是终态）。画布上已有本轮亲笔段 = 确凿产出，按它准入。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)

    class _StoreWithPeerNarr:
        async def list_sections_appended_by_turn(self, turn_id: str):
            assert turn_id == "t_cto"
            return [SimpleNamespace(kind="narrative", owner="sub_a6ce8e0585", payload={"markdown": "## 营业额\n…"})]

    rt = _mk_runtime(canvas_store=_StoreWithPeerNarr())
    await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, ["subagent_spawn"]),
        aggregated_payload=None,  # Peer crashes before subagent_return: empty bag.
        user_text="全面分析一下 2025 年的经营情况",
        peer_payloads=[],
    )
    assert len(calls) == 1, "无 payload 但画布有亲笔正文 ⇒ 收尾器必须进来补外壳"
    assert calls[0].get("payload") == {}


@pytest.mark.asyncio
async def test_no_payload_and_no_peer_narrative_still_skips_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：没有 payload 也没有亲笔段（peer 崩在写正文之前）⇒ 仍不 compose——否则又是伪报告。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)

    class _EmptyStore:
        async def list_sections_appended_by_turn(self, turn_id: str):
            return []

    rt = _mk_runtime(canvas_store=_EmptyStore())
    await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, ["subagent_spawn"]),
        aggregated_payload=None,
        user_text="全面分析一下 2025 年的经营情况",
        peer_payloads=[],
    )
    assert calls == []


@pytest.mark.asyncio
async def test_default_env_report_turn_composes_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省代笔拍板后的**默认接线**（不设任何 env）：笔在（compose_report 入口）
    + 无人亲笔 → 确定性卡片（compose_report 内就地调 compose_canvas_from_payload）。
    钉住 flag 拆分后 prod 默认真走的那条路。"""
    monkeypatch.delenv("BOWHEAD_WRITE_REPORT", raising=False)
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "kpi_row", "insight_card"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, ["subagent_spawn"]),
        aggregated_payload={
            "title": "经营分析报告",
            "kpis": [{"label": "总营收", "value": "3623万"}],
            "insights": [{"title": "集中度", "narrative": "CR3 48.7%"}],
        },
        user_text="帮我写一份经营分析报告",
        peer_payloads=[],
    )
    assert len(calls) == 1, "默认接线必须落到确定性卡片"
    assert "insight_card" in kinds


@pytest.mark.asyncio
async def test_lead_spawn_delivery_report_composes_without_evidence_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级链第二档正向：delivery='report' → compose（即使无 canvas_add_*）。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    r = _mk_result_with_tools(None, [])
    r.tool_invocations = [SimpleNamespace(tool_name="subagent_spawn", arguments={"profile": "compute", "task": "t", "delivery": "report"})]
    kinds, _text = await _compose_turn_output(
        runtime=_mk_canvas_runtime(),
        sctx=_mk_sctx(),
        result=r,
        aggregated_payload={"exec_summary": "结论", "kpis": [{"label": "x", "value": "1"}]},
        user_text="出份报告",
        peer_payloads=[],
    )
    assert len(calls) == 1
    assert kinds == ["h1", "narrative"]


# Delegated results must reach the composer through invocation.nested.
# Dispatch may already have deleted the temporary context collection; a top-level-
# only read would incorrectly treat a delegated turn as having no evidence.


@pytest.mark.asyncio
async def test_delegated_turn_nested_tool_results_reach_the_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """委派轮：顶层只有一个 spawn（result 零数字），真数在 inv.nested —— 必须到得了写手。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "1")
    got: dict[str, Any] = {}

    async def _probe_report(**kw: Any) -> list[str]:
        got["tool_invocations"] = kw.get("tool_invocations")
        return ["h1", "narrative"]

    import bowhead_core.report_writer_loop as rwl

    monkeypatch.setattr(rwl, "compose_report", _probe_report)

    nested = SimpleNamespace(tool_name="bi_query", result={"rows": [{"slope": 82.937}]}, nested=None)
    spawn = SimpleNamespace(tool_name="subagent_spawn", result={"status": "done"}, nested=[nested])
    result = _mk_result(payload={"exec_summary": "x"})
    result.tool_invocations = [spawn]

    await _compose_turn_output(
        runtime=_mk_runtime(canvas_store=object()),
        sctx=_mk_sctx(),
        result=result,
        aggregated_payload={"exec_summary": "x"},
        user_text="q",
        peer_payloads=[],
        force_canvas=True,
    )

    from bowhead_core.analysis_payload import build_observed_numbers

    invs = got.get("tool_invocations") or []
    observed = build_observed_numbers([getattr(i, "result", None) for i in invs])
    assert 82.937 in observed, f"peer 的真实工具结果没到 compose：invocations={len(invs)} observed={sorted(observed)}"


def test_compose_report_wrapper_mirrors_the_impl_signature() -> None:
    """`compose_report` 是 `_compose_report_impl` 的**手抄**封装，参数得靠人对齐。

    薄封装解决了「三条 return 各加一行会漏一条」，但换来一个新漂移面：impl 新增/改名
    一个关键字参数时，封装不会编译错、只会**静默不传**——而生产只调封装。
    这条把两边签名钉死（同 `feedback_default_param_hides_missing_wiring`）。
    """
    import inspect

    from bowhead_core.report_writer_loop import _compose_report_impl, compose_report

    impl = inspect.signature(_compose_report_impl).parameters
    wrap = inspect.signature(compose_report).parameters
    assert list(impl) == list(wrap), f"参数名/顺序漂了：impl={list(impl)} wrapper={list(wrap)}"
    assert {k: v.default for k, v in impl.items()} == {k: v.default for k, v in wrap.items()}, "默认值漂了"


# End-to-end report composition after a rejected cross-turn patch.


@pytest.mark.asyncio
async def test_failed_cross_turn_patch_still_yields_exactly_one_report(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected cross-turn patch still permits one complete report.

    Successful eager composition sets the skip marker; final composition must not
    duplicate cards or treat eager nonchapter sections as a handwritten report.
    """
    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.canvas_revise import mark_turn_committed_to_v4
    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.types import TurnRequest

    monkeypatch.delenv("BOWHEAD_WRITE_REPORT", raising=False)
    store = CanvasStore(tmp_path / "w.db")
    await store.initialize()
    await store.ensure_canvas(session_id="s_cto", workspace_id="w", user_id="u")
    payload = {
        "title": "2026年1-8月品牌经营表现",
        "exec_summary": "整体达成率 101.65%。",
        "kpis": [{"label": "整体销售金额", "value": "68.80亿元", "sentiment": "good"}],
        "insights": [{"title": "头部集中", "thesis": "前三 70%", "narrative": "格华止领跑。", "evidence": []}],
        "recommendations": [{"priority": "P0", "action": "核查拓得康"}],
    }
    rt = _mk_runtime(canvas_store=store)
    rt.parent_ctx.response_kind_pre_flight = "canvas"
    bus = rt.parent_bus
    # ① Peer return: ψ-E eager card (A3: failed write_report no longer suppresses it).
    req = TurnRequest(turn_id="t_cto", session_id="s_cto", workspace_id="w", user_id="u", member_uid="", text="2026年1-8月")
    kinds = await compose_canvas_from_payload(canvas_store=store, request=req, bus=bus, payload=payload, peer_id="sub_p1", emit_lifecycle=False)
    assert "h1" in kinds
    bus._psi_compose_done = True  # type: ignore[attr-defined]
    # ② Lead emits lead.final.payload: skip flag set only after actual eager rendering.
    mark_turn_committed_to_v4(bus)
    # ③ Final compose: spawn nests failed write_report.
    result = _mk_result(payload=payload)
    result.tool_invocations = [
        SimpleNamespace(
            tool_name="subagent_spawn",
            nested=[SimpleNamespace(tool_name="write_report", result={"error": "not_owner", "owner": "sub_prev"}, error=None)],
        )
    ]
    composed, _text = await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=result,
        aggregated_payload=payload,
        user_text="2026年1-8月",
        peer_payloads=[{"subagent_id": "sub_p1", "profile": "compute", "payload": payload}],
    )
    cv = await store.get_canvas_by_session("s_cto")
    secs = await store.list_sections(cv.id)
    assert sum(1 for s in secs if s.kind == "h1") == 1, [s.kind for s in secs]
    assert sum(1 for s in secs if s.kind == "kpi_row") == 1
    # Document projection (default on) recommendations are style=recommendations narrative; kill switch uses recommendations kind.
    # Recommendations section retired (tech-design-document-minimalism §2.2); use title as single-report probe:
    # document identity, exactly one per report.
    assert sum(1 for s in secs if s.kind == "h1") == 1
    assert composed == []  # No final card or finalizer.


@pytest.mark.asyncio
async def test_recommendations_only_payload_reaches_cards_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A3 行为变化清单第 2 条：只有 recommendations 的 payload（eager 门槛不过、
    `_payload_has_report_content` 过）——以前被无条件 skip flag 挡住无段，现在末轮出卡。"""
    monkeypatch.delenv("BOWHEAD_WRITE_REPORT", raising=False)
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["h1", "recommendations"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    from bowhead_core.canvas_revise import mark_turn_committed_to_v4

    rt = _mk_canvas_runtime()
    mark_turn_committed_to_v4(rt.parent_bus)  # No eager ⇒ no skip flag.
    kinds, _ = await _compose_turn_output(
        runtime=rt,
        sctx=_mk_sctx(),
        result=_mk_result_with_tools(None, ["subagent_spawn"]),
        aggregated_payload={"title": "建议清单", "recommendations": [{"priority": "P0", "action": "先核对目标"}]},
        user_text="给我几条建议",
        peer_payloads=[],
    )
    assert len(calls) == 1 and "recommendations" in kinds


# ── Unsent-email fallback (116 1.3.43 t_4eabdf899529) ────────────

_MAIL_TO = "exampleuser@example.com"
_UNVERIFIED_MAIL = {
    "ok": False,
    "error": "email_numbers_unverified",
    "unverified_numbers": ["37.46亿", "37.11%", "15.28亿", "4.87%"],
    "message": "邮件未发送：以下数字在本轮取得的数据中找不到来源。",
}
_SUMMARY = "2026年1-7月营业额前五品类：第一名 37.46亿，占 37.11%；……第五名占 4.87%。"


def _mail_inv(result: dict[str, Any]) -> Any:
    from bowhead_core.types import ToolInvocation

    return ToolInvocation(tool_name="send_email", arguments={"to": _MAIL_TO, "subject": "前五品类", "body": "…"}, result=result)


def _spawn_inv() -> Any:
    from bowhead_core.types import ToolInvocation

    return ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "compute"}, result={"ok": True})


async def _compose_with(
    invocations: list[Any], final_answer: str = _SUMMARY, *, chat_direct: bool = False, lang: str | None = "zh-CN"
) -> tuple[Any, str]:
    from bowhead_core.language_context import LanguageContext

    runtime = _mk_runtime()
    # The fixture conversation is Chinese; `lang=None` models a turn without any language context.
    if lang is not None:
        runtime.parent_ctx.language_context = LanguageContext(ui_language=lang, output_language=lang)
    result = _mk_result(final_answer=final_answer)
    result.tool_invocations = invocations
    if chat_direct:
        result.chat_direct_delivered = True  # type: ignore[attr-defined]
    _kinds, text = await _compose_turn_output(
        runtime=runtime,
        sctx=_mk_sctx(),
        result=result,
        aggregated_payload=None,
        user_text="把这个前五品类的结果整理一下，发邮件给 " + _MAIL_TO,
        peer_payloads=[],
    )
    return runtime, text


@pytest.mark.asyncio
async def test_failed_send_email_then_silent_answer_gets_unsent_notice() -> None:
    """真机形状：send_email 被数字核对拦下 → 派 compute 重算 → 终答不提邮件 ⇒ 终答补「邮件未发送」+原因+下一步。"""
    _runtime, text = await _compose_with([_mail_inv(_UNVERIFIED_MAIL), _spawn_inv()])
    assert text.startswith(_SUMMARY), "原终答必须保留"
    assert "邮件未发送" in text
    assert _MAIL_TO in text
    assert "没能与本轮取得的数据逐一核对上" in text, "原因必须按错误码说人话"
    assert "同一条消息里让我重新取数并发送" in text, "必须给出下一步"


@pytest.mark.asyncio
async def test_unsent_notice_is_the_same_text_on_the_chat_direct_delta() -> None:
    """live 同源：chat 直达轮的合成 delta 与终答逐字相同，都带未发送说明。"""
    runtime, text = await _compose_with([_mail_inv(_UNVERIFIED_MAIL)], chat_direct=True)
    deltas = [e for e in runtime.parent_bus.events if getattr(e, "event_type", "") == "narrative.delta"]
    assert len(deltas) == 1
    assert (deltas[0].payload or {}).get("delta") == text
    assert "邮件未发送" in text


@pytest.mark.asyncio
async def test_unsent_notice_stands_alone_when_answer_is_empty() -> None:
    _runtime, text = await _compose_with([_mail_inv({"ok": False, "error": "email_send_rejected", "message": "…"})], final_answer="")
    assert text.startswith("**邮件未发送**")
    assert "拒绝了这封邮件" in text


@pytest.mark.asyncio
async def test_unsent_notice_uses_english_without_a_language_context() -> None:
    from bowhead_core.send_email_tool import unsent_email_notice

    rejected = _mail_inv({"ok": False, "error": "email_send_rejected", "message": "…"})
    _runtime, text = await _compose_with([rejected], final_answer="", lang=None)
    assert text == unsent_email_notice([rejected], "en-US")
    assert text.startswith(f"**The email to {_MAIL_TO} was not sent**")


@pytest.mark.asyncio
async def test_later_successful_send_email_needs_no_notice() -> None:
    _runtime, text = await _compose_with([_mail_inv(_UNVERIFIED_MAIL), _spawn_inv(), _mail_inv({"ok": True, "message": "邮件已发送。"})])
    assert text == _SUMMARY


@pytest.mark.asyncio
async def test_turn_without_send_email_needs_no_notice() -> None:
    _runtime, text = await _compose_with([_spawn_inv()])
    assert text == _SUMMARY
