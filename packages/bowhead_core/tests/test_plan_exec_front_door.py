"""WP-5 正门出口集成测试 —— 直打 _run_plan_execution_turn (planning-mode-audit P2-b/c).

prod 事故形状的诚实性契约:
A. BI 步空产出 → blocked(不再「N 步成功」), 文案带 empty_output 桶。
E/I/ψ. 新式(无终端 writer 步)plan: 有真内容才说「报告已生成」, 零内容 demote 回 blocked。

C/C2. plan 轮画布指针置位与 M2 幻影撤回(lead_orchestrator `_claims_canvas_pointer` 分支),
    与 writer 无关: 2026-09-29 起改用无 writer 步的 kb-analyst + financial-analyst plan。

2026-09-29: 原 B/D 两条专钉老式终端 canvas-writer 步(writer 步 report_empty 降级、writer
拒稿兜底)。canvas-writer 已在 c4d50b03b 移除, 老式 writer 步只会以 unknown_agent 阻塞,
这两条随之删除。

真 PlanStore + 真 LocalTaskRunner + 真 adapter 三查 + 真 AgentRegistry manifests;
只 stub SubagentRunner(按 profile 出结果)与 canvas composer(探针)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _run_plan_execution_turn
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.plan_reify import assign_levels
from bowhead_core.plan_store import PlanStatus, PlanStore, StepStatus, new_step
from bowhead_core.subagent import SubagentResult
from bowhead_core.types import TurnStatus


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._pi_turn_state: dict[str, Any] = {}

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)

    def find_all(self, event_type: str) -> list[Any]:
        return [e for e in self.events if e.event_type == event_type]


@dataclass
class _ProfileStubRunner:
    """按 profile 出 SubagentResult 的 SubagentRunner stub."""

    by_profile: dict[str, SubagentResult] = field(default_factory=dict)

    async def run(self, *, profile_name: str, **_kw: Any) -> SubagentResult:
        r = self.by_profile.get(profile_name)
        assert r is not None, f"no stub for {profile_name}"
        return r


def _sub(profile: str, *, payload: dict | None = None, final_answer: str = "") -> SubagentResult:
    return SubagentResult(
        profile=profile,
        subagent_id=f"sub_{profile[:6]}",
        ok=True,
        final_answer=final_answer,
        payload=payload,
        tool_invocations=[],
        tokens_input=100,
        tokens_output=50,
    )


class _FakeCanvasStore:
    """D6/done-branch 消费面: 报告 section 已落库的假证据(真栈=composer 写库)。
    没有它, done-branch 的 content-based report_kept 判 0 section → M2 reconcile
    会把「报告已生成」撤回 —— 那正是安全网该干的(scenario C 反向验证过)。"""

    def __init__(self, kinds: tuple[str, ...] = ("meta", "narrative", "insight_card")) -> None:
        self.kinds = kinds

    async def list_sections_appended_by_turn(self, _turn_id: str) -> list[Any]:
        return [SimpleNamespace(kind=k) for k in self.kinds]

    async def mark_turn_hidden_for_chat_mode(self, _turn_id: str, _rationale: str) -> list[str]:
        return []


async def _setup_plan(db_path: str, agents: list[str]) -> tuple[PlanStore, Any]:
    store = PlanStore(db_path)
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="w", user_id="u", title="Q1 经营分析")
    steps = [new_step(plan_id=plan.id, seq=i, agent_name=a, objective=f"obj-{a}") for i, a in enumerate(agents)]
    levels = assign_levels(steps)
    for s in steps:
        s.level = levels[s.id]
    await store.add_steps(steps)
    await store.update_plan_status(plan.id, PlanStatus.RUNNING)
    return store, plan


def _runtime(store: PlanStore, runner: _ProfileStubRunner, *, canvas_store: Any = None, router: Any = None) -> LeadRuntime:
    ctx = SimpleNamespace(
        canvas_store=canvas_store,
        artifact_store=None,
        kb_files_store=None,
        clarification_store=None,
        member_uid="",
        language_context=None,
        resolve_datarc_project=lambda: "1",  # Bound → no preflight.
        accessible_datarc_workspaces=(),
        agent_registry=None,
    )
    bus = _FakeBus()
    # Mirror run_turn done-branch wiring (absent when directly calling _run_plan_execution_turn):
    # D6/reconcile reads bus._d6_canvas_store + _d6_is_plan_execution.
    bus._d6_canvas_store = canvas_store  # type: ignore[attr-defined]
    bus._d6_is_plan_execution = True  # type: ignore[attr-defined]
    return LeadRuntime(
        router=router,  # type: ignore[arg-type]
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),  # type: ignore[arg-type]
        parent_bus=bus,  # type: ignore[arg-type]
        parent_ctx=ctx,  # type: ignore[arg-type]
        plan_store=store,
        subagent_runner=runner,  # type: ignore[arg-type]
        multi_agent_registry=AgentRegistry.bootstrap(),
    )


def _sctx(plan_id: str, turn_id: str = "t_fd") -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u",
        workspace_id="w",
        session_id="s",
        turn_id=turn_id,
        role="user",
        plan_execution_id=plan_id,
    )


@pytest.mark.asyncio
async def test_scenario_a_empty_bi_steps_block_honestly(tmp_path) -> None:
    """今天的 prod 形状: BI 步空产出 → FAILED(empty_output) → plan blocked;
    绝无「N 步成功,报告已生成」。Chat turn feedback D3: the failure card states plan_blocked,
    so the turn carries no system answer sentence; the step keeps its empty_output kind."""
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "financial-analyst": _sub("financial-analyst", payload=None, final_answer=""),
        }
    )
    rt = _runtime(store, runner)
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res.status == TurnStatus.FAILED
    assert res.final_answer == ""
    assert res.error == "plan_blocked"
    steps = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert steps["financial-analyst"].error_kind == "empty_output"
    dones = rt.parent_bus.find_all("done")
    assert len(dones) == 1 and dones[0].payload["status"] == "error"
    assert dones[0].payload["error_kind"] == "plan_blocked"


# ── Writing convergence T2: new plan (no terminal canvas-writer step), compose through front door ──


@pytest.mark.asyncio
async def test_scenario_c_real_content_claims_canvas_and_wires_pointer(tmp_path, monkeypatch) -> None:
    """有真内容 → 「报告已生成」+ resp canvas + pointer/reconcile 接电。
    composer 探针走正门(deterministic 路, writer-loop 关)。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    got: dict[str, Any] = {}

    async def _probe(**kw: Any) -> list[str]:
        got["payload"] = kw.get("payload")
        got["invs"] = kw.get("tool_invocations")
        return ["meta", "narrative", "insight_card"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业新闻", "exec_summary": "发现 X"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务分析", "exec_summary": "利润率 12%"}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore())
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res.status == TurnStatus.COMPLETED
    assert "报告已生成在 canvas" in res.final_answer
    dones = rt.parent_bus.find_all("done")
    assert dones[0].payload["response_kind"] == "canvas"
    # Pointer wired (M2 reconcile input); actual content in store (fake supplies D6 evidence) →
    # reconcile does not fire; no "本次未生成画布报告" correction.
    assert getattr(rt.parent_bus, "_canvas_pointer_claimed", False) is True
    assert getattr(rt.parent_bus, "_lead_chat_text", "") == res.final_answer
    fas = rt.parent_bus.find_all("final_answer")
    assert all("本次未生成画布报告" not in str(e.payload) for e in fas)
    # Front door gets deterministic aggregation of expert outputs (no writer; old assertion used removed canvas-writer payload).
    assert "发现 X" in str(got["payload"]) and "利润率 12%" in str(got["payload"])


@pytest.mark.asyncio
async def test_scenario_c2_reconcile_retracts_phantom_when_no_sections(tmp_path, monkeypatch) -> None:
    """M2 reconcile 对 plan turn 真通电(discovery P0-3 双重禁用已解): compose 声称
    有内容但库里 0 非 meta section(drift) → 幻影「报告已生成」被撤回 + 纠正 final_answer。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")

    async def _probe(**kw: Any) -> list[str]:
        return ["meta", "narrative"]  # Compose claims content; store has none (fake below only reports meta).

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业新闻", "exec_summary": "发现 X"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务分析", "exec_summary": "利润率 12%"}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore(kinds=("meta",)))
    await _run_plan_execution_turn(_sctx(plan.id), rt)
    fas = rt.parent_bus.find_all("final_answer")
    # After chrome-i18n P1-2, clause splitting recognizes ASCII punctuation: plan text "计划执行完成:N 步成功,
    # 报告已生成在 canvas。" now splits on ASCII comma; innocent first clause survives as correction
    # under strip-preserve semantics (same as Chinese-comma text), phantom second clause still withdrawn;
    # only wholly phantom sentence uses "本次未生成画布报告" fallback.
    assert fas, "应有 final_answer 事件"
    _last = str(fas[-1].payload)
    assert "计划执行完成" in _last, "纠正 final_answer 应保留真发现"
    assert "报告已生成" not in _last, "幻影指针应被撤回（原始 emit 在流里是预期，纠正在其后）"
    # Persist sync (contract 2): _pi final_answer holds corrected text.
    assert "报告已生成" not in str(rt.parent_bus._pi_turn_state.get("final_answer", ""))


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_scenario_e_writerless_plan_composes_honestly(tmp_path, monkeypatch) -> None:
    """T2 新式 plan: planner 不再出组稿步 → aggregated=确定性合并专家 payload,
    正门照常出报告。三个契约: ①不得出现「报告撰写(canvas-writer)步未完成」的
    误导文案(那专属老式 plan 的 writer 腿真失败——新式 plan 这是正常聚合输入,
    会被 writer-loop 当证据写进正文) ②title 无「(部分)」降级后缀 ③渲染哨兵
    落在最后一步(无 writer 步可挂),重试不重复渲染。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    got: dict[str, Any] = {}

    async def _probe(**kw: Any) -> list[str]:
        got["payload"] = kw.get("payload")
        return ["meta", "narrative", "insight_card"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业扫描", "exec_summary": "发现 X 趋势"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务分析", "exec_summary": "利润率 12%"}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore())
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res.status == TurnStatus.COMPLETED
    assert "报告已生成在 canvas" in res.final_answer
    p = got["payload"]
    assert p is not None
    # ① No misleading copy (scan entire payload).
    assert "未完成" not in str(p)
    assert "canvas-writer" not in str(p)
    # ② No "(部分)" degradation suffix; both expert outputs aggregated (one in exec_summary,
    # other folded into insight: full-fidelity mixed merge, no content lost).
    assert "(部分)" not in str(p.get("title") or "")
    _flat = str(p)
    assert "发现 X 趋势" in _flat and "利润率 12%" in _flat
    # ③ Sentinel on last step (reader _prior_fallback checks any step for sentinel).
    steps = await store.list_steps(plan.id)
    assert any("__plan_fallback_report__" in tuple(s.output_section_ids or ()) for s in steps)


@pytest.mark.asyncio
async def test_scenario_f_writerless_peer_payloads_shape_reaches_multi_writer(tmp_path, monkeypatch) -> None:
    """T1 shape 修复: plan 路 peer_payloads 带 {"profile","payload"} 外壳(普通路
    _m137 bag 同形)→ compose_report 的 substantial 判据(p.get("payload"))能命中,
    multi-writer 在 plan turn 不再结构性死路(audit D2)。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "1")
    got: dict[str, Any] = {}

    async def _probe_report(**kw: Any) -> list[str]:
        got["peer_payloads"] = kw.get("peer_payloads")
        return ["meta", "narrative"]

    import bowhead_core.report_writer_loop as rwl

    monkeypatch.setattr(rwl, "compose_report", _probe_report)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业", "exec_summary": "X", "insights": [{"t": 1}]}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务", "exec_summary": "Y", "kpis": [{"k": 1}]}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore())
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res.status == TurnStatus.COMPLETED
    pps = got["peer_payloads"]
    assert pps and len(pps) == 2
    for p in pps:
        assert set(p.keys()) >= {"profile", "payload"}, f"peer payload 缺外壳: {p}"
        assert isinstance(p["payload"], dict) and p["payload"]
    assert {p["profile"] for p in pps} == {"kb-analyst", "financial-analyst"}


# ── Writing-convergence review fixes: retry (P1 sentinel gate) + empty-report demotion (P1 dead path) ──


@pytest.mark.asyncio
async def test_scenario_g_retry_semantics_new_leg_recomposes(tmp_path, monkeypatch) -> None:
    """review P1(哨兵一次性门): ①重试轮无新产出 → 跳过重渲+文案说先前生成;
    ②重试轮有新 DONE leg → 重新成稿且聚合覆盖全部 leg(含历史轮)。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[dict[str, Any]] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw.get("payload"))
        return ["meta", "narrative", "insight_card"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业", "exec_summary": "X"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务", "exec_summary": "Y"}),
            "business-analyst": _sub("business-analyst", payload={"title": "经营", "exec_summary": "Z"}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore())
    res1 = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res1.status == TurnStatus.COMPLETED and len(calls) == 1

    # ── Retry①: no new output → no rerender, honestly point to prior report ──
    from bowhead_core.plan_store import PlanStatus as _PS

    assert await store.demote_completed_to_blocked(plan.id)
    assert await store.update_plan_status(plan.id, _PS.RUNNING)
    rt2 = _runtime(store, runner, canvas_store=_FakeCanvasStore())
    res2 = await _run_plan_execution_turn(_sctx(plan.id, turn_id="t_fd_retry_noop"), rt2)
    assert len(calls) == 1, "无新 DONE 步不得重渲"
    assert "先前生成" in res2.final_answer

    # ── Retry②: rerun one new leg → recompose, aggregate all 3 legs ──
    from bowhead_core.plan_reify import assign_levels as _al
    from bowhead_core.plan_store import new_step as _ns

    extra = _ns(plan_id=plan.id, seq=2, agent_name="business-analyst", objective="obj-ba")
    lv = _al([extra])
    extra.level = lv[extra.id]
    await store.add_steps([extra])
    assert await store.update_plan_status(plan.id, _PS.RUNNING)
    rt3 = _runtime(store, runner, canvas_store=_FakeCanvasStore())
    res3 = await _run_plan_execution_turn(_sctx(plan.id, turn_id="t_fd_retry_newleg"), rt3)
    assert res3.status == TurnStatus.COMPLETED
    assert len(calls) == 2, "新 DONE leg 必须触发重渲(旧行为: 哨兵一刀切永不再渲)"
    _flat2 = str(calls[1])
    assert all(x in _flat2 for x in ("X", "Y", "Z")), "重渲聚合必须覆盖全部 leg(含历史轮)"


@pytest.mark.asyncio
async def test_scenario_i_writerless_empty_report_demotes_to_blocked(tmp_path, monkeypatch) -> None:
    """review P1(demote 死路): 新式 plan compose 出 0 非 meta 内容 → plan 必须
    demote 回 BLOCKED(重试真实可点),不许留在 COMPLETED 终态说「可在计划卡重试」。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")

    async def _probe(**kw: Any) -> list[str]:
        return ["meta"]  # meta only = no content.

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业", "exec_summary": "X"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务", "exec_summary": "Y"}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore(kinds=("meta",)))
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert "报告内容未能生成" in res.final_answer
    fresh = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert str(fresh.status) == "blocked", "新式 plan 空报告必须 demote(此前恒 COMPLETED=死承诺)"
    blocked_evs = rt.parent_bus.find_all("plan_blocked")
    assert blocked_evs and blocked_evs[-1].payload.get("blocked_step_ids") == []


@pytest.mark.asyncio
async def test_scenario_g_psi_composed_report_counts_as_content(tmp_path, monkeypatch) -> None:
    """★116 dogfood 1.0.93 P1（t_d4be37936054/t_4c9c5997643a 两次复现）：卡片缺省
    代笔后 ψ-E 在步执行期就把报告落库，末轮 composer 被 _psi_compose_done 跳过 →
    kinds 恒空 → 文案谎报「报告内容未能生成」+ completed plan 被降级 BLOCKED。
    修：内容真值以库为准（与终态同源）。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    calls: list[Any] = []

    async def _probe(**kw: Any) -> list[str]:
        calls.append(kw)
        return ["meta", "narrative"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业", "exec_summary": "X", "insights": [{"t": 1}]}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务", "exec_summary": "Y", "kpis": [{"k": 1}]}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore(kinds=("meta", "narrative", "insight_card", "kpi_row")))
    rt.parent_bus._psi_compose_done = True  # ψ-E persisted during step execution.
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res.status == TurnStatus.COMPLETED
    assert "报告内容未能生成" not in res.final_answer, "库里有报告还谎报未生成"
    assert "报告已生成" in res.final_answer
    dones = rt.parent_bus.find_all("done")
    assert dones[0].payload["response_kind"] == "canvas"
    fresh = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert str(fresh.status) != "blocked", "有报告的 completed plan 不许被 demote"
    assert not rt.parent_bus.find_all("plan_blocked")
    assert calls == []  # psi done → final composer skips (avoids double compose without fallback).


@pytest.mark.asyncio
@pytest.mark.parametrize("with_canvas", [True, False])
@pytest.mark.parametrize("planning_v2", ["1", "0"])
async def test_scenario_h_answer_plan_surfaces_leg_answer_not_echo(tmp_path, monkeypatch, with_canvas, planning_v2) -> None:
    """★116 dogfood 1.0.93 P1（t_cde94266cbf4）：ANSWER plan 的短答 payload 被
    DF-04 挡在聚合外 → 文案链落到 user_text 兜底 → 终稿=用户原话回显。
    修：回显时以 done leg 的 exec_summary 为真源。"""
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    monkeypatch.setenv("BOWHEAD_PLANNING_V2", planning_v2)
    from unittest.mock import AsyncMock

    import bowhead_core.plan_doc_title as titles

    title_call = AsyncMock(return_value="Unexpected document title")
    monkeypatch.setattr(titles, "generate_document_title", title_call)
    monkeypatch.setattr(titles, "plan_doc_title_enabled", lambda: True)

    async def _probe(**kw: Any) -> list[str]:
        raise AssertionError("An answer Plan must not compose a report")

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan = await store.create_plan(
        session_id="s",
        workspace_id="w",
        user_id="u",
        title="分两步：①用 BI 查今年利润前三的品牌 ②一句话总结。不要写报告。",
        objective="分两步：①用 BI 查今年利润前三的品牌 ②一句话总结。不要写报告。",
        deliverable_kind="answer",
    )
    steps = [new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="查今年利润前三的品牌")]
    levels = assign_levels(steps)
    for s in steps:
        s.level = levels[s.id]
    await store.add_steps(steps)
    await store.update_plan_status(plan.id, PlanStatus.RUNNING)
    _answer = "今年利润前三：周生生 1.2 亿、天梭 0.9 亿、耐克 0.8 亿。"
    runner = _ProfileStubRunner(
        by_profile={
            "compute": _sub(
                "compute",
                payload={"delivery_surface": "chat", "exec_summary": _answer, "kpis": [{"label": "第一名", "value": "周生生 1.2亿"}]},
            ),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore(kinds=("meta",)) if with_canvas else None)
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert res.status == TurnStatus.COMPLETED
    assert "周生生" in res.final_answer, f"leg 答案没浮出: {res.final_answer!r}"
    assert res.final_answer.strip() != plan.objective, "终稿不许回显用户原话"
    assert "报告内容未能生成" not in res.final_answer
    dones = rt.parent_bus.find_all("done")
    assert dones[0].payload["response_kind"] == "chat"
    fresh = await store.get_plan(plan.id, user_id="u", workspace_id="w")
    assert str(fresh.status) != "blocked"
    title_call.assert_not_awaited()


# ── G2 (decision #6.6 implemented): Lead supplies document title; **both tests pin call site** ──
# Same-family lesson: helper-only tests stay green if call line removed (observed in writer-loop canonical order).
# Call `_run_plan_execution_turn` directly, asserting payload["title"] actually fed
# to composer.


class _TitleRouter:
    """只认 narrative 角色的假 router；记下被调了几次。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[str] = []

    async def invoke(self, agent_type: str, req: Any) -> Any:
        self.calls.append(agent_type)
        return SimpleNamespace(
            content=self.content,
            usage=SimpleNamespace(input_tokens=120, output_tokens=12, cached_tokens=0),
        )


async def _run_titled_plan(tmp_path, monkeypatch, *, router: Any) -> dict[str, Any]:
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    got: dict[str, Any] = {}

    async def _probe(**kw: Any) -> list[str]:
        got["payload"] = kw.get("payload")
        return ["meta", "narrative", "insight_card"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业扫描", "exec_summary": "发现 X 趋势"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务分析", "exec_summary": "利润率 12%"}),
        }
    )
    rt = _runtime(store, runner, canvas_store=_FakeCanvasStore(), router=router)
    res = await _run_plan_execution_turn(_sctx(plan.id), rt)
    got["res"] = res
    return got


@pytest.mark.asyncio
async def test_document_title_comes_from_lead_not_from_a_chapter(tmp_path, monkeypatch) -> None:
    """★封面标题＝Lead 生成的**整份文档**标题，不再是某一章的标题。

    116 真机 t_c9853b490565 的形态：封面 h1 与末章 h1 逐字相同。确定性兜底链的
    天花板就在这儿——任何确定性选择都只能是某一章的标题。
    删掉 `_run_plan_execution_turn` 里那次 `generate_document_title` 调用，本条必红。
    """
    router = _TitleRouter("《区域商超连锁收购尽调报告》")
    got = await _run_titled_plan(tmp_path, monkeypatch, router=router)
    p = got["payload"]
    assert p is not None
    assert p["title"] == "区域商超连锁收购尽调报告", p["title"]
    # No chapter title.
    assert p["title"] not in ("行业扫描", "财务分析")
    # Compose uses narrative role (writing convergence T3: not most expensive lead model).
    assert router.calls == ["narrative"], router.calls
    # Writing convergence T4: bill new LLM call; two legs each 100/50 (total 200/100),
    # plus title 120/12 ⇒320/112. Difference proves title call actually billed.
    assert got["res"].tokens_input == 320, got["res"].tokens_input
    assert got["res"].tokens_output == 112, got["res"].tokens_output


@pytest.mark.asyncio
async def test_document_title_falls_back_to_chapter_title_when_flag_off(tmp_path, monkeypatch) -> None:
    """正对照：回滚绳 `BOWHEAD_PLAN_DOC_TITLE=0` 一拉，就退回既有确定性兜底链，

    且**一次 LLM 都不调**（别在关着的路上偷偷烧钱）。
    """
    monkeypatch.setenv("BOWHEAD_PLAN_DOC_TITLE", "0")
    router = _TitleRouter("《不该被用到的标题》")
    got = await _run_titled_plan(tmp_path, monkeypatch, router=router)
    p = got["payload"]
    assert p is not None
    assert p["title"] != "不该被用到的标题"
    assert router.calls == [], "flag 关着时不许调 LLM"
    # Deterministic fallback = closing-chapter title.
    assert p["title"] == "财务分析", p["title"]


@pytest.mark.asyncio
async def test_document_title_survives_a_dead_router(tmp_path, monkeypatch) -> None:
    """★fail-open：router 炸了，报告照出，标题退回确定性链。

    标题是锦上添花，绝不能让一份跑了十几分钟的报告因为它出不来。
    """

    class _DeadRouter:
        async def invoke(self, agent_type: str, req: Any) -> Any:
            raise RuntimeError("router down")

    got = await _run_titled_plan(tmp_path, monkeypatch, router=_DeadRouter())
    p = got["payload"]
    assert p is not None
    assert p["title"] == "财务分析", p["title"]
    assert got["res"].status == TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_citations_flush_before_terminal_emits(tmp_path) -> None:
    """source-accessibility §1.6 投递半：plan 轮注册过的引用必须在 final_answer/done
    **之前**以 turn.citation_registered 事件发出（普通轮的次序不变量:
    citation events → final_answer，见 _compose_turn_output 的 flush 点注释）。

    此前 plan 轮唯一盖到的 flush 在 run_turn 的 finally——那时 done 已把 bus
    _terminal_frozen，emit 逐条静默 return False（116 实测：turn_citations 2,271 行、
    turn.citation_registered 事件 0 条）。本测试直打正门；FakeBus 无冰封语义，
    故断言**存在 + 次序**——次序对了，真 bus 上事件才活得下来。"""
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业新闻", "exec_summary": "只有一篇评测"}),
        }
    )
    rt = _runtime(store, runner)
    from bowhead_core.citations import ensure_registry

    registry = ensure_registry(rt.parent_ctx)
    registry.register("art_x1", label="bi_query: 测试", kind="data_ref")
    registry.register("sec_y2", label="客单价Top3城市", kind="chart_section")

    await _run_plan_execution_turn(_sctx(plan.id), rt)

    types = [e.event_type for e in rt.parent_bus.events]
    cite_idx = [i for i, t in enumerate(types) if t == "turn.citation_registered"]
    assert len(cite_idx) == 2, f"引用事件缺失: {types}"
    final_idx = types.index("final_answer")
    done_idx = types.index("done")
    assert max(cite_idx) < final_idx < done_idx, f"次序不变量被破坏: {types}"
    # Payload contract: citation_seq (not seq: bus envelope overwrites seq) + label + kind.
    cites = rt.parent_bus.find_all("turn.citation_registered")
    assert [c.payload["citation_seq"] for c in cites] == [1, 2]
    assert cites[1].payload["kind"] == "chart_section"
    assert cites[1].payload["label"] == "客单价Top3城市"


@pytest.mark.asyncio
async def test_auto_run_entry_marks_turn_as_plan_execution(tmp_path) -> None:
    """★116 dogfood 1.2.32（s_bb40f9cf41b6）：Auto 档 auto_run 在提案轮内直接执行，turn 起点按
    sctx.plan_execution_id 设的 `_d6_is_plan_execution` 是 False ⇒ 每条腿 eager 以 chapter_mode=False
    落库，N 条腿 = N 份完整文档（各带 h1/署名/KPI/建议/局限）。plan 执行入口自己置位，两条入口同一状态。"""
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["financial-analyst"])
    runner = _ProfileStubRunner(
        by_profile={
            "financial-analyst": _sub("financial-analyst", payload=None, final_answer=""),
        }
    )
    rt = _runtime(store, runner)
    rt.parent_bus._d6_is_plan_execution = False  # Proposal-turn initial state (fixture previously set True manually for run_turn).
    await _run_plan_execution_turn(_sctx(plan.id), rt)
    assert rt.parent_bus._d6_is_plan_execution is True


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_done_earlier", [False, True])
async def test_old_plan_with_a_retired_writer_step_completes_without_gap_or_duplicate(tmp_path, monkeypatch, writer_done_earlier: bool) -> None:
    """老 plan 带 canvas-writer 终端步：它按 skipped 收口，plan completed，报告里没有它的缺口说明；
    它早先写好的综述（done 于旧轮）也不会被当一章再合并。"""
    import json as _json

    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    got: dict[str, Any] = {}

    async def _probe(**kw: Any) -> list[str]:
        got["payload"] = kw.get("payload")
        return ["meta", "narrative", "insight_card"]

    import bowhead_core.canvas_composer as cc

    monkeypatch.setattr(cc, "compose_canvas_from_payload", _probe)
    store, plan = await _setup_plan(str(tmp_path / "p.db"), ["kb-analyst", "financial-analyst", "canvas-writer"])
    if writer_done_earlier:
        writer = next(s for s in await store.list_steps(plan.id) if s.agent_name == "canvas-writer")
        await store.update_step_status(
            writer.id,
            status=StepStatus.DONE,
            turn_id="t_old",
            payload_json=_json.dumps({"title": "综述", "exec_summary": "writer 当年的综述"}, ensure_ascii=False),
        )
    runner = _ProfileStubRunner(
        by_profile={
            "kb-analyst": _sub("kb-analyst", payload={"title": "行业扫描", "exec_summary": "发现 X 趋势"}),
            "financial-analyst": _sub("financial-analyst", payload={"title": "财务分析", "exec_summary": "利润率 12%"}),
        }
    )
    res = await _run_plan_execution_turn(_sctx(plan.id), _runtime(store, runner, canvas_store=_FakeCanvasStore()))
    assert res.status == TurnStatus.COMPLETED
    assert "报告已生成在 canvas" in res.final_answer
    after = {s.agent_name: s for s in await store.list_steps(plan.id)}
    assert str(after["canvas-writer"].status) == str(StepStatus.DONE if writer_done_earlier else StepStatus.SKIPPED)
    flat = _json.dumps(got["payload"], ensure_ascii=False)
    assert "[plan_gap]" not in flat and "canvas-writer" not in flat
    assert "writer 当年的综述" not in flat
    assert "发现 X 趋势" in flat and "利润率 12%" in flat
