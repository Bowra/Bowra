"""子 agent「上次工作备忘」（`peer_prior_run.py`，flag `BOWHEAD_PEER_PRIOR_RUN_MEMO` 默认 1）。

三层：渲染纯函数 → `SessionStore.load_prior_peer_run` 读真 `turn_events` → spawn handler 接线（备忘进 extra_instructions；flag 关 = 逐字节不变）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from bowhead_core.peer_prior_run import PeerPriorRun, PeerToolCall, render_prior_run_memo


def _run(**kw: Any) -> PeerPriorRun:
    base: dict[str, Any] = {
        "turn_id": "t_prev",
        "turn_seq": 3,
        "user_text": "改成 2026年1-6月的口径",
        "profile": "compute",
        "task": "把画布上这份报告的口径从全年改为 2026年1-6月，就地修订",
        "status": "succeeded",
        "failure_reason": None,
        "tools": (
            PeerToolCall(
                tool="bi_query",
                call_id="call_00_bi",
                args_preview='{"queries": ["2026年1-6月 各品牌 营业额 同比", "2026年1-6月 各省 退款率 同比"]}',
                ok=True,
                error=None,
                result_preview='{"results": [{"ok": true, "total_rows": 1041}]}',
                artifact_ids=("art_q1", "art_q2"),
                rows=1041,
                queries=("2026年1-6月 各品牌 营业额 同比", "2026年1-6月 各省 退款率 同比"),
                query_artifacts=(("2026年1-6月 各品牌 营业额 同比", "art_q1"), ("2026年1-6月 各省 退款率 同比", "art_q2")),
            ),
            PeerToolCall(
                tool="python_sandbox",
                call_id="call_01_py",
                args_preview='{"code": "import pandas as pd\\nrev = pd.read_csv(INPUTS[\\"rev\\"][\\"path\\"])\\nprint(rev.head())"}',
                ok=True,
                error=None,
                result_preview='{"ok": true, "exit_code": 0}',
                artifact_ids=("art_stdout1",),
            ),
            PeerToolCall(
                tool="bi_query",
                call_id="call_02_bi",
                args_preview='{"queries": ["2026年1-6月 各门店 明细"]}',
                ok=False,
                error="BIAsyncTaskTimeout: 上一次行级数据查询 60 秒内未返回；请不要换措辞重试，直接如实告知用户",
                result_preview="",
                queries=("2026年1-6月 各门店 明细",),
            ),
        ),
        "return_rejections": ("ν1 ref validation failed: chart_ref_not_in_allowlist field=insights[0].evidence[0].chart_section_id ref=art_x",),
        "is_previous_turn": True,
    }
    base.update(kw)
    return PeerPriorRun(**base)


# ── Rendering ──


@pytest.mark.unit
def test_memo_carries_four_things_with_scope_caveat_and_is_deterministic() -> None:
    same_scope = "把口径保持 2026年1-6月，再加一节各省对比"
    memo = render_prior_run_memo(_run(), task_now=same_scope)
    assert memo == render_prior_run_memo(_run(), task_now=same_scope)
    assert memo.startswith("## 你上一次在本会话里的记录（上一轮用户说「改成 2026年1-6月的口径」）")
    assert "必须重新取数" in memo and "标了「可复用」" in memo, "与 R6 同源的风险：记录不是可照抄的答案"
    assert "上一次的任务口径：把画布上这份报告的口径从全年改为 2026年1-6月" in memo
    assert "2026年1-6月 各品牌 营业额 同比 → artifact_id=art_q1（口径相同，可复用）" in memo
    assert "跑过代码（python_sandbox）→ 产出 artifact：art_stdout1" in memo and "import pandas" in memo
    assert "撞过" in memo and "BIAsyncTaskTimeout" in memo and "请不要换措辞重试" in memo
    assert "subagent_return 被拒" in memo and "chart_ref_not_in_allowlist" in memo
    assert "第 " not in memo and "ms" not in memo, "不含相对轮号 / 耗时"


@pytest.mark.unit
def test_memo_scope_guard_blocks_artifact_reuse_when_period_changes() -> None:
    """review 修（与 R6 同源）：本轮口径是第二季度，上一次是 1-6 月 ⇒ 查询列出来但**不给可复用的 artifact**。"""
    memo = render_prior_run_memo(_run(), task_now="改成 2026年第二季度的口径", user_text_now="第二季度")
    assert "art_q1" not in memo and "art_q2" not in memo
    assert "2026年1-6月 各品牌 营业额 同比（口径不同或未标明，须重查）" in memo
    # Previous query has no time tokens ⇒ do not mark reusable either.
    run = _run(
        tools=(
            PeerToolCall(
                tool="bi_query", call_id="c", args_preview="", ok=True, error=None, result_preview="", query_artifacts=(("各品牌 营业额", "art_x"),)
            ),
        ),
        return_rejections=(),
    )
    memo2 = render_prior_run_memo(run, task_now="2026年 各品牌营业额")
    assert "art_x" not in memo2 and "须重查" in memo2


@pytest.mark.unit
def test_scope_guard_normalises_periods_and_refuses_coarser_prior_scope() -> None:
    """review 第二轮（P1）：子集判定会把「2026年」（全年）判成可复用于「2026年第二季度」——正是守卫要挡的那种；
    记号又是原始子串，`1-6月` 与 `上半年` 判成不同口径，把「换了个日期重查」那群人挡在外面。"""
    from bowhead_core.peer_prior_run import period_tokens, prior_query_reusable

    assert period_tokens("2026年1-6月") == period_tokens("2026年上半年") == period_tokens("2026 年 1至6月") == period_tokens("2026年1月至6月")
    assert period_tokens("第二季度") == period_tokens("Q2") == period_tokens("二季度") == period_tokens("4-6月")
    assert period_tokens("2026-06") == period_tokens("2026年06月") == {"Y2026", "M6"}
    assert period_tokens("2026年全年") == period_tokens("2026年") == {"Y2026"}
    assert period_tokens("近 3 个月") == {"R3月"} and period_tokens("近30日") == {"R30天"} and "去年同期" in period_tokens("去年同期对比")

    def reusable(q: str, task: str, user: str = "") -> bool:
        return prior_query_reusable(q, task_now=task, user_text_now=user)

    assert not reusable("2026年 各品牌 营业额", "把口径改成 2026年第二季度"), "全年产物不能喂季度报告"
    assert not reusable("2026年6月 各品牌 营业额", "2026年1月至6月 各品牌"), "六月产物不能喂上半年"
    assert not reusable("2026年 各品牌 营业额", "2026年 近30天 趋势")
    assert reusable("2026年1-6月 各品牌 营业额 同比", "把口径保持 2026年上半年，再加一节各省对比"), "同口径不同写法"
    assert reusable("2026年1-6月 各品牌 营业额", "2026年1-6月与2025年1-6月对比"), "本轮只多了一个对比年份 ⇒ 上次的那半仍可用"
    assert reusable(
        "2026年4-6月各品牌营业额", "口径保持 2026年第二季度（4-6月）不变。需新增查询：2026年4-6月各省营业额，以及 2025年4-6月（去年同期）各省营业额"
    ), "116 第四轮 dogfood：任务里的「去年同期」是对比词，不收窄口径"
    assert not reusable("2026年 各品牌 营业额", "2026年 本月 各品牌营业额"), "本月这类相对词仍然收窄"
    assert period_tokens("中位数（median）和上下四分位数（Q1=下四分位/25%分位、Q3=上四分位/75%分位）") == frozenset(), "Q1= / Q3= 是分位数，不是季度"
    assert period_tokens("2026年Q1") == {"Y2026", "M1-3"} and period_tokens("Q3 营业额") == {"M7-9"}
    assert reusable("2026年 各品牌 营业额", "查询 2026 年各品牌的利润数据，计算中位数和上下四分位数（Q1=下四分位、Q3=上四分位）"), (
        "116 第四轮：分位数标签不该挡同口径复用"
    )
    assert reusable("各品牌 营业额 前10", "换成饼图：各品牌 营业额 前10"), "没有时间记号但 Lead 原样重述了这句查询"
    assert not reusable("各品牌 营业额 前10", "2026年 各品牌营业额")


@pytest.mark.unit
def test_memo_surfaces_partial_batch_failures_and_earlier_turn_wording() -> None:
    run = _run(
        tools=(
            PeerToolCall(
                tool="bi_query",
                call_id="c",
                args_preview="",
                ok=True,
                error=None,
                result_preview="",
                queries=("a", "b"),
                failed_queries=(("b", "BIAsyncTaskTimeout: 60 秒内未返回；请不要换措辞重试"),),
            ),
        ),
        return_rejections=(),
        is_previous_turn=False,
    )
    memo = render_prior_run_memo(run, task_now="x")
    assert memo.startswith("## 你上一次在本会话里的记录（本会话早些时候用户说")
    assert "撞过" in memo and "bi_query「b」：BIAsyncTaskTimeout" in memo, "批量 bi_query 整体 ok=True 时的局部失败也要露出来"


@pytest.mark.unit
def test_memo_is_empty_when_prior_run_had_no_tools() -> None:
    assert render_prior_run_memo(_run(tools=(), return_rejections=())) == ""


@pytest.mark.unit
def test_memo_respects_char_cap_and_keeps_head() -> None:
    many = tuple(
        PeerToolCall(
            tool="python_sandbox", call_id=f"c{i}", args_preview="x" * 400, ok=True, error=None, result_preview="", artifact_ids=(f"art_{i}",)
        )
        for i in range(40)
    )
    memo = render_prior_run_memo(_run(tools=many, return_rejections=()), max_chars=1500)
    assert len(memo) <= 1500 and memo.startswith("## 你上一次在本会话里的记录") and "必须重新取数" in memo
    # Review round 2 (P3): if too long, drop queries / code first; prior errors and rejection reasons last (they address 48% error recurrence).
    with_errs = many + (
        PeerToolCall(tool="bi_query", call_id="e", args_preview="", ok=False, error="BIAsyncTaskTimeout: 请不要换措辞重试", result_preview=""),
    )
    memo2 = render_prior_run_memo(_run(tools=with_errs), max_chars=1500)
    assert len(memo2) <= 1500 and "请不要换措辞重试" in memo2 and "chart_ref_not_in_allowlist" in memo2


# ── Reading from storage ──


async def test_load_prior_peer_run_reads_the_latest_same_profile_spawn(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    db = str(tmp_path / "s.db")
    store = SessionStore(db_path=db)
    await store.initialize()
    log = TurnLogStore(db_path=db)
    await log.initialize()
    await store.ensure_session(session_id="s1", workspace_id="ws", user_id="u")
    for i, text in enumerate(["全年报告", "改成上半年", "第二季度"], start=1):
        await store.record_turn(turn_id=f"t{i}", session_id="s1", user_text=text)
        if i < 3:
            await store.mark_completed(turn_id=f"t{i}", intent=None, final_answer=f"答{i}", tokens_input=0, tokens_output=0, cost_usd=0.0)
            await store.finalize_turn(session_id="s1", current_turn_id=f"t{i}", next_turn_id=None)

    async def _ev(turn_id: str, event_type: str, payload: dict) -> None:
        await log.record(turn_id=turn_id, event_type=event_type, payload=payload)

    # t1: compute ran bi_query + python + one rejected return.
    await _ev("t1", "agent_spawned", {"subagent_id": "sub_c1", "profile": "compute", "task": "全年各品牌营业额"})
    await _ev(
        "t1",
        "step_started",
        {"parent_agent_id": "sub_c1", "call_id": "c_bi", "tool_name": "bi_query", "arguments_preview": '{"queries": ["2026年 各品牌 营业额"]}'},
    )
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_bi",
            "tool_name": "bi_query",
            "ok": True,
            "error": None,
            "result_inline": {
                "results": [
                    {"ok": True, "query_text": "2026年 各品牌 营业额", "total_rows": 12},
                    {"ok": False, "query_text": "2026年 各门店 明细", "error_message": "BIAsyncTaskTimeout: 请不要换措辞重试"},
                ]
            },
            "result_meta": {"sub_sources": [{"artifact_id": "art_a", "query": "2026年 各品牌 营业额"}]},
        },
    )
    await _ev(
        "t1",
        "step_started",
        {"parent_agent_id": "sub_c1", "call_id": "c_py", "tool_name": "python_sandbox", "arguments_preview": '{"code": "print(1)"}'},
    )
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_py",
            "tool_name": "python_sandbox",
            "ok": True,
            "error": None,
            "result_preview": "{}",
            "result_meta": {"output_artifacts": [{"artifact_id": "art_out"}]},
        },
    )
    await _ev("t1", "step_started", {"parent_agent_id": "sub_c1", "call_id": "c_ret", "tool_name": "subagent_return", "arguments_preview": "{}"})
    # Production shape: event-level error contains only Python exceptions; rejection reasons are in result dict (review round 2 P2).
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_ret",
            "tool_name": "subagent_return",
            "ok": False,
            "error": None,
            "result_inline": {"ok": False, "error": "ν1 ref validation failed: data_ref_not_in_allowlist ref=xx", "retry_hint": "…"},
        },
    )
    # N=1 flat bi_query (most common): no results[] / sub_sources; artifact is in meta.artifact_id.
    await _ev(
        "t1",
        "step_started",
        {"parent_agent_id": "sub_c1", "call_id": "c_flat", "tool_name": "bi_query", "arguments_preview": '{"query": "2026年 各省 营业额"}'},
    )
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_flat",
            "tool_name": "bi_query",
            "ok": True,
            "error": None,
            "result_inline": {"ok": True, "total_rows": 31, "sample_rows": []},
            "result_meta": {"artifact_id": "art_flat"},
        },
    )
    # Large batch: result_inline over 4 KB is cleared and argument preview truncated ⇒ queries/partial failures fall back to step_done.sub_steps.
    await _ev(
        "t1",
        "step_started",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_big",
            "tool_name": "bi_query",
            "arguments_preview": '{"queries": ["2026年 各品牌 明细", "2026年 各门店 明',
        },
    )
    await _ev(
        "t1",
        "step_done",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_big",
            "tool_name": "bi_query",
            "sub_steps": [
                {"label": "2026年 各品牌 明细", "state": "success", "output_preview": "900 行", "error_kind": None},
                {"label": "2026年 各门店 明细", "state": "failure", "output_preview": None, "error_kind": "bi_async_timeout"},
                {"label": "2026年 各渠道 明细", "state": "success", "output_preview": "40 行", "error_kind": None},
            ],
        },
    )
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_c1",
            "call_id": "c_big",
            "tool_name": "bi_query",
            "ok": True,
            "error": None,
            "result_inline": None,
            "result_preview": '{"results": [{"ok": true, "query_text": "2026年 各品牌 明细", "total_rows": 900, "sample_rows": [{"x": 1}]}, {"ok": false, "query_text": "2026年 各门',
        },
    )
    await _ev(
        "t1",
        "lead.subagent_summary",
        {"spawn_id": "sub_c1", "profile": "compute", "status": "succeeded_with_warnings", "failure_reason": None, "task": "全年各品牌营业额，写报告"},
    )
    # t2: delegated only to canvas-writer (another profile).
    await _ev("t2", "agent_spawned", {"subagent_id": "sub_w1", "profile": "canvas-writer", "task": "收紧措辞"})
    await _ev("t2", "lead.subagent_summary", {"spawn_id": "sub_w1", "profile": "canvas-writer", "status": "succeeded"})

    run = await store.load_prior_peer_run(session_id="s1", profile="compute", before_turn_id="t3")
    assert run is not None and run.turn_id == "t1" and run.user_text == "全年报告" and run.task == "全年各品牌营业额，写报告"
    assert [t.tool for t in run.tools] == ["bi_query", "python_sandbox", "subagent_return", "bi_query", "bi_query"]
    bi, py, ret, flat, big = run.tools
    assert flat.queries == ("2026年 各省 营业额",) and flat.query_artifacts == (("2026年 各省 营业额", "art_flat"),), "扁平 bi_query 也给 artifact"
    assert big.queries == ("2026年 各品牌 明细", "2026年 各门店 明细", "2026年 各渠道 明细"), "inline 被置空 ⇒ 查询从 sub_steps 兜底"
    assert big.failed_queries == (("2026年 各门店 明细", "bi_async_timeout"),)
    assert ret.error == "ν1 ref validation failed: data_ref_not_in_allowlist ref=xx", "被拒原因从结果 dict 取，不是截断的 JSON 前缀"
    assert (
        bi.queries == ("2026年 各品牌 营业额", "2026年 各门店 明细") and bi.query_artifacts == (("2026年 各品牌 营业额", "art_a"),) and bi.rows == 12
    )
    assert bi.failed_queries == (("2026年 各门店 明细", "BIAsyncTaskTimeout: 请不要换措辞重试"),), "整体 ok=True 的批量里局部失败要逐条取出"
    assert py.artifact_ids == ("art_out",) and ret.ok is False
    assert run.return_rejections == ("ν1 ref validation failed: data_ref_not_in_allowlist ref=xx",)
    assert run.is_previous_turn is False, "t1 与当前 t3 中间隔着 t2"
    # Previous same-profile run is canvas-writer in t2 ⇒ canvas-writer matches t2; analyst has none ⇒ None.
    w = await store.load_prior_peer_run(session_id="s1", profile="canvas-writer", before_turn_id="t3")
    assert w is not None and w.turn_id == "t2" and w.tools == () and w.is_previous_turn is True
    assert await store.load_prior_peer_run(session_id="s1", profile="analyst", before_turn_id="t3") is None
    # Current turn excluded (before_turn_id=t1 has no earlier turn).
    assert await store.load_prior_peer_run(session_id="s1", profile="compute", before_turn_id="t1") is None


# ── Wiring: actual spawn handler ──


def _summary_helpers() -> Any:
    path = Path(__file__).with_name("test_subagent_summary_emit.py")
    spec = importlib.util.spec_from_file_location("_summary_emit_helpers", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CapturingRunner:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.kwargs: dict[str, Any] = {}

    async def run(self, **kw: Any) -> Any:
        self.kwargs = kw
        return await self._inner.run(**kw)


class _PriorStore:
    def __init__(self, run: PeerPriorRun | None) -> None:
        self._run = run
        self.calls: list[tuple[str, str, str]] = []

    async def load_prior_peer_run(self, *, session_id: str, profile: str, before_turn_id: str) -> PeerPriorRun | None:
        self.calls.append((session_id, profile, before_turn_id))
        return self._run


async def _spawn(monkeypatch: pytest.MonkeyPatch, *, flag: str, run: PeerPriorRun | None) -> tuple[_CapturingRunner, _PriorStore]:
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult, register_subagent_tool
    from bowhead_core.tool_registry import ToolRegistry

    monkeypatch.setenv("BOWHEAD_PEER_PRIOR_RUN_MEMO", flag)
    h = _summary_helpers()
    added = "compute" not in PROFILES
    if added:
        PROFILES["compute"] = SubagentProfile(
            name="compute", llm_agent_type="compute", tool_whitelist=("bi_query", "subagent_return"), system_prompt="(stub)", max_iterations=4
        )
    try:
        result = SubagentResult(
            profile="compute",
            subagent_id="x",
            ok=True,
            final_answer="ok",
            tokens_input=1,
            tokens_output=1,
            duration_ms=1,
            payload={"title": "T", "exec_summary": "s", "kpis": [], "insights": [], "recommendations": []},
            tool_invocations=[],
        )
        bus = h._FakeBus()
        ctx = h._FakeCtx(bus)
        runner = _CapturingRunner(h._FakeRunner(result))
        ctx.subagent_runner = runner
        store = _PriorStore(run)
        ctx.session_store = store
        reg = ToolRegistry()
        register_subagent_tool(reg)
        out = await reg.execute("subagent_spawn", arguments={"profile": "compute", "task": "改成第二季度", "rationale": "r"}, ctx=ctx)
        assert out.error is None, out.error
        return runner, store
    finally:
        if added:
            PROFILES.pop("compute", None)


@pytest.mark.unit
async def test_handler_injects_memo_into_peer_extra_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, store = await _spawn(monkeypatch, flag="1", run=_run())
    assert store.calls == [("s_test", "compute", "t_w42")]
    extra = runner.kwargs.get("extra_instructions") or ""
    assert "你上一次在本会话里的记录" in extra and "BIAsyncTaskTimeout" in extra
    assert "art_q1" not in extra and "须重查" in extra, "本轮 task「改成第二季度」与上次 1-6 月口径不同 ⇒ 不给可复用 artifact"
    assert extra.index("Sub-task context") < extra.index("你上一次在本会话里的记录"), "review 修：先读本轮任务上下文，再读上次记录"
    assert runner.kwargs.get("task") == "改成第二季度", "task 本体不动"


@pytest.mark.unit
async def test_handler_flag_off_or_no_prior_run_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    off, store_off = await _spawn(monkeypatch, flag="0", run=_run())
    assert store_off.calls == [], "flag 关连库都不查"
    none, _ = await _spawn(monkeypatch, flag="1", run=None)
    assert (off.kwargs.get("extra_instructions") or "") == (none.kwargs.get("extra_instructions") or "")
    assert "你上一次在本会话里的记录" not in (off.kwargs.get("extra_instructions") or "")


async def test_turn_runner_hands_the_real_session_store_to_the_spawn_context(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """review 修：`session_store` 是 ToolExecutionContext 的正式字段，TurnRunner 从 orch 传入；没传时 handler 记 skipped 日志而不是静默。"""
    from bowhead_core.turn_runner import TurnRunner

    spec = importlib.util.spec_from_file_location("_turn_runner_unit_helpers2", Path(__file__).with_name("test_turn_runner_unit.py"))
    assert spec and spec.loader
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    monkeypatch.setenv("BOWHEAD_PEER_PRIOR_RUN_MEMO", "1")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = h._make_orch(subagent_runner=fake_subagent_runner)
    await TurnRunner(orch).run(h._req("上半年营业额"))
    ctx = fake_subagent_runner.invocations[0]["parent_ctx"]
    assert getattr(ctx, "session_store", None) is orch.session_store


async def test_loader_reads_sandbox_traceback_and_falls_back_to_agent_spawned_on_cancelled_turn(tmp_path: Path) -> None:
    """116 第四轮 dogfood 两条：① 沙箱失败的外层 error 只是 "exit_code=1"，traceback 在 result_json.stderr（外层包装 exit 0）；
    ② 用户中途 stop 的轮 peer 被杀，没有 lead.subagent_summary，但它查过的东西恰是下一轮最该知道的 ⇒ 退回 agent_spawned。"""
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    db = str(tmp_path / "s.db")
    store = SessionStore(db_path=db)
    await store.initialize()
    log = TurnLogStore(db_path=db)
    await log.initialize()
    await store.ensure_session(session_id="s1", workspace_id="ws", user_id="u")
    await store.record_turn(turn_id="t1", session_id="s1", user_text="用 polars 算中位数")
    await store.mark_cancelled(turn_id="t1") if hasattr(store, "mark_cancelled") else None
    await store.finalize_turn(session_id="s1", current_turn_id="t1", next_turn_id=None)
    # Without mark_cancelled, change status directly (only to construct a "cancelled" turn).
    import sqlite3

    con = sqlite3.connect(db)
    con.execute("UPDATE turns SET status='cancelled' WHERE id='t1'")
    con.commit()
    con.close()
    await store.record_turn(turn_id="t2", session_id="s1", user_text="再算利润的中位数")

    async def _ev(turn_id: str, event_type: str, payload: dict) -> None:
        await log.record(turn_id=turn_id, event_type=event_type, payload=payload)

    # Dispatch agent_spawned has no status; peer then runs bi_query and a failing sandbox before being killed ⇒ no summary or completed agent_spawned.
    await _ev("t1", "agent_spawned", {"subagent_id": "sub_p1", "profile": "compute", "task": "查营业额后用 polars 算分位数"})
    await _ev(
        "t1",
        "step_started",
        {"parent_agent_id": "sub_p1", "call_id": "c_bi", "tool_name": "bi_query", "arguments_preview": '{"query": "2026年 各品牌 营业额"}'},
    )
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_p1",
            "call_id": "c_bi",
            "tool_name": "bi_query",
            "ok": True,
            "error": None,
            "result_inline": {"ok": True, "total_rows": 1083},
            "result_meta": {"artifact_id": "art_rev"},
        },
    )
    await _ev(
        "t1",
        "step_started",
        {"parent_agent_id": "sub_p1", "call_id": "c_py", "tool_name": "python_sandbox", "arguments_preview": '{"code": "import polars as pl\n..."}'},
    )
    await _ev(
        "t1",
        "tool_result",
        {
            "parent_agent_id": "sub_p1",
            "call_id": "c_py",
            "tool_name": "python_sandbox",
            "ok": False,
            "error": None,
            "result_inline": {
                "ok": False,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "result_json": {
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Traceback (most recent call last):\n  File \"/workdir/user.py\", line 26, in <module>\n    import polars as pl\nModuleNotFoundError: No module named 'polars'\n",
                    "error": "exit_code=1",
                },
                "error": "exit_code=1",
            },
        },
    )
    run = await store.load_prior_peer_run(session_id="s1", profile="compute", before_turn_id="t2")
    assert run is not None and run.turn_id == "t1" and run.status == "cancelled" and run.task == "查营业额后用 polars 算分位数"
    bi, py = run.tools
    assert bi.query_artifacts == (("2026年 各品牌 营业额", "art_rev"),), "被杀之前查到的东西照样带上"
    assert py.ok is False and py.error is not None
    assert "ModuleNotFoundError: No module named 'polars'" in py.error and py.error.startswith("exit_code=1："), py.error
    memo = render_prior_run_memo(run, task_now="2026年 各品牌 利润 中位数")
    assert "No module named 'polars'" in memo and "上一次结果：cancelled" in memo
