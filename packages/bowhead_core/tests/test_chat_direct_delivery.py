"""lead-analyst-logic 批5 — peer chat 直达交付（2026-08-21 转正，无 flag）。

把 DF-04 短答的「Lead 复读一整轮」换成确定性提升：spawn handler 打哨兵 →
Lead loop 第三哨兵家族短路 → final_answer = peer 原文（调用方补合成 delta）。

覆盖（tech-design-batch5 §6）：
1. 谓词与资格判据（结构证据、四条判据成对）；
2. loop 级：**router 调用计数少一轮**（直接断言「没有第二轮 LLM」，不数散文）
   + 无哨兵正对照多一轮——这是本批的核心收益，也是最容易假绿的地方；
3. 短路只对 lead（peer 内 spawn 不短路，I5 纪律同 clarification break）；
4. **只认 `subagent_spawn` 打的哨兵**——转正前这道防线由 flag 兼任（flag OFF 时
   消费侧不认哨兵），flag 删除后必须由「工具名」这个真判据顶上，否则任何返回
   `_chat_direct_delivery{text}` 的工具都能劫持终答。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import (
    PROFILES,
    SubagentProfile,
    SubagentResult,
    SubagentRunner,
    _chat_direct_eligible,
    is_chat_direct_delivery_sentinel,
    register_subagent_tool,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
)

SHORT_ANSWER = "2026 年营业额约 106.62 亿元。"

# ─────────────────────────────────────────────────────────────
# 1. Predicate + eligibility criteria
# ─────────────────────────────────────────────────────────────


def test_sentinel_requires_structural_evidence() -> None:
    """魔法键不够——text 必须是非空字符串（同 needs_clarification 要 options 的思路）。"""
    assert is_chat_direct_delivery_sentinel({"_chat_direct_delivery": {"text": SHORT_ANSWER}}) == (True, SHORT_ANSWER)
    assert is_chat_direct_delivery_sentinel({"_chat_direct_delivery": {"text": "   "}})[0] is False
    assert is_chat_direct_delivery_sentinel({"_chat_direct_delivery": True})[0] is False
    assert is_chat_direct_delivery_sentinel({"_chat_direct_delivery": {}})[0] is False
    assert is_chat_direct_delivery_sentinel({"ok": True})[0] is False
    assert is_chat_direct_delivery_sentinel(None)[0] is False


class _Ctx:
    """review F-1 后：判据读**真实 spawn 计数**，不再读 payload 袋。"""

    def __init__(self, spawn_count: int = 1) -> None:
        self._chat_direct_spawn_count = spawn_count


def test_eligible_happy_path() -> None:
    assert _chat_direct_eligible(_Ctx(), SHORT_ANSWER, primary=True) is True


def test_short_answer_alone_does_not_establish_completion() -> None:
    assert _chat_direct_eligible(_Ctx(), SHORT_ANSWER) is False


def test_ineligible_on_multi_peer_turn() -> None:
    """多 peer 轮的整合是 Lead 真活——本 turn spawn 过 ≥2 次就不直达。"""
    assert _chat_direct_eligible(_Ctx(2), SHORT_ANSWER, primary=True) is False


def test_ineligible_when_overclaim_directive_present() -> None:
    """review F-8：peer 声称产了文件而账单为 0 时的纠偏 directive 指望 Lead 改口，
    短路会把它连同谎报一起放行。"""
    assert _chat_direct_eligible(_Ctx(), SHORT_ANSWER, primary=True, no_file_directive="⚠ 本轮没有产出文件…") is False


def test_ineligible_on_empty_text() -> None:
    assert _chat_direct_eligible(_Ctx(), "   ", primary=True) is False


# ─────────────────────────────────────────────────────────────
# 2. spawn handler sets the sentinel (actually through registry.execute)
# ─────────────────────────────────────────────────────────────


class _ShortAnswerRunner:
    """假 runner：返回 DF-04 短答 payload（delivery_surface=chat + exec_summary）。"""

    async def run(self, **kwargs: Any) -> SubagentResult:
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_chat1",
            ok=True,
            final_answer=SHORT_ANSWER,
            payload={"delivery_surface": "chat", "exec_summary": SHORT_ANSWER},
        )


class _FakeBus:
    async def emit(self, ev: Any) -> None:
        return None


async def _spawn_once(spawn_count: int | None = None, *, role: str | None = "primary") -> Any:
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_cd",
        datarc=None,
        subagent_runner=_ShortAnswerRunner(),
        parent_bus=_FakeBus(),
    )
    if spawn_count is not None:
        ctx._chat_direct_spawn_count = spawn_count  # type: ignore[attr-defined]
    args = {"profile": "compute", "task": "Read revenue."}
    if role is not None:
        args["delegation_role"] = role
    inv = await registry.execute("subagent_spawn", args, ctx)
    assert inv.error is None, inv.error
    return inv


async def test_spawn_marks_sentinel() -> None:
    inv = await _spawn_once()
    hit, text = is_chat_direct_delivery_sentinel(inv.result)
    assert hit is True
    assert text == SHORT_ANSWER
    # Keep legacy fields too (existing FE/Lead consumers stay unchanged).
    assert inv.result["ok"] is True
    assert inv.result["final_answer"] == SHORT_ANSWER


@pytest.mark.parametrize("role", [None, "helper", "unknown"])
async def test_partial_or_unspecified_delegation_returns_evidence_to_lead(role: str | None) -> None:
    inv = await _spawn_once(role=role)
    assert not is_chat_direct_delivery_sentinel(inv.result)[0]
    assert inv.result["final_answer"] == SHORT_ANSWER
    assert "remaining work" in inv.result["directive"]


class _ProseRunner:
    """假 runner：peer 用 prose final_answer 收尾（**不调 subagent_return**）。
    生产实测这条路占 spawn 的 12.7%——它**不进 payload 袋**，正是 F-1 的关键。"""

    async def run(self, **kwargs: Any) -> SubagentResult:
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_prose",
            ok=True,
            final_answer="第一个 peer 的结论：客流量 1,516 万人次。",
        )


@pytest.mark.parametrize(
    "case,eligible",
    [
        ("plain", True),
        ("verified_tool", True),
        ("ordinary_peer", False),
        ("system", False),
        ("schema", False),
        ("multiple_peers", False),
        ("failed", False),
        ("canvas_write", False),
        ("file", False),
        ("canvas_section", False),
        ("unknown_file_ledger", False),
        ("unknown_canvas_ledger", False),
        ("overclaim", False),
    ],
)
async def test_selected_team_plaintext_delivery_keeps_existing_boundaries(monkeypatch, case, eligible) -> None:
    from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
    from bowhead_core.deliverable_ledger import DeliverableLedger
    from bowhead_core.types import ToolInvocation

    binding = ExactAgentExecutionBinding(
        execution_id="aex_selected",
        team_id="org_test",
        turn_id="t_selected",
        agent_id="osa_selected",
        agent_source_kind="system" if case == "system" else "team",
        revision_ref="arv_selected",
        release_id="arl_selected",
        runtime_profile_name="compute",
        capability_fingerprint="capability",
        policy_fingerprint="policy",
        definition={"output_schema": {"type": "object"}} if case == "schema" else {},
        listing={},
        status="admitted",
    )
    tool_cases = {"verified_tool", "unknown_file_ledger", "unknown_canvas_ledger", "canvas_write"}
    peer = SubagentResult(
        profile="compute",
        subagent_id="sub_selected",
        ok=case != "failed",
        error="peer_failed" if case == "failed" else None,
        final_answer="PDF 已生成，可直接下载。" if case == "overclaim" else "Revenue: 800\nProfit: 120\nMargin: 15.0%",
        tool_invocations=[ToolInvocation(tool_name="write_report" if case == "canvas_write" else "python_sandbox", arguments={}, result={"ok": True})]
        if case in tool_cases
        else [],
    )

    class SelectedRuntime:
        async def binding_for_delegation(self, **_kwargs):
            return None if case == "ordinary_peer" else binding

        async def run(self, **_kwargs):
            return peer

    class Runner:
        async def run(self, **_kwargs):
            return peer

    async def ledger(**_kwargs):
        return DeliverableLedger(
            files=int(case == "file"),
            sections=int(case == "canvas_section"),
            files_verified=case != "unknown_file_ledger",
            sections_verified=case != "unknown_canvas_ledger",
        )

    monkeypatch.setattr("bowhead_core.deliverable_ledger.build_deliverable_ledger", ledger)
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m",
        user_id="u",
        turn_id="t_selected",
        datarc=None,
        subagent_runner=Runner(),
        parent_bus=_FakeBus(),
    )
    ctx._reference_lead_runtime = SelectedRuntime()
    ctx._chat_direct_spawn_count = 2 if case == "multiple_peers" else 1
    invocation = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Calculate the margin."}, ctx)
    assert invocation.error is None
    assert is_chat_direct_delivery_sentinel(invocation.result)[0] is eligible
    assert not invocation.result.get("payload_returned")


async def test_prose_peer_leaves_no_trace_in_payload_bag() -> None:
    """★review F-1 的机制钉：prose 收尾的 peer **不进 payload 袋**（生产 12.7%）。

    初版判据用「袋长度」当 spawn 计数，正是因为这条才失效（「先 prose 再 chat」
    会被误判成单 spawn → 短路 → 前一个 peer 的结论整段丢弃）。钉住机制防回归。
    """
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_cd_prose",
        datarc=None,
        subagent_runner=_ProseRunner(),
        parent_bus=_FakeBus(),
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查客流量"}, ctx)
    assert inv.error is None
    assert not (getattr(ctx, "_m137_subagent_payloads", None) or [])


async def test_handler_feeds_exec_summary_into_overclaim_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """★review#2 F-5：上一版那条测试直接调 guard 函数，**没钉住 wiring**——把
    `_claim_text` 改回只用 `fa` 全文件照绿。这条监视 handler 真正喂进去的文本。"""
    seen: list[str] = []
    import bowhead_core.subagent as sub_mod

    def _spy(ledger: Any, peer_answer: Any) -> tuple[dict, str]:
        seen.append(str(peer_answer or ""))
        return {}, ""

    # Handler imports within the function (`from bowhead_core.overclaim_guard import ...`);
    # patch the source module to affect the symbol actually called in production.
    import bowhead_core.overclaim_guard as og_mod

    monkeypatch.setattr(og_mod, "peer_facts_and_directive", _spy)
    _ = sub_mod  # Retain reference to show the patch targets the handler's upstream source module.

    class _EmptyFaRunner:
        """★fixture 必须还原**生产真形状**：M137 #145 下 peer 交 payload 时
        `final_answer` 故意留空。初版 fixture 两边都填 ⇒ 突变成 `fa` 也含
        exec_summary ⇒ 摘掉修复照样绿（自查逮到，正是本条要防的假绿）。"""

        async def run(self, **kwargs: Any) -> SubagentResult:
            return SubagentResult(
                profile=str(kwargs.get("profile_name") or "compute"),
                subagent_id="sub_spy",
                ok=True,
                final_answer="",  # Actual production shape
                payload={"delivery_surface": "chat", "exec_summary": SHORT_ANSWER},
            )

    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_cd_spy",
        datarc=None,
        subagent_runner=_EmptyFaRunner(),
        parent_bus=_FakeBus(),
    )
    await registry.execute("subagent_spawn", {"profile": "compute", "task": "查营业额"}, ctx)
    assert seen, "guard 未被调用 — harness 断在更上游"
    assert SHORT_ANSWER in seen[0], f"喂进 guard 的文本必须含 exec_summary，实际={seen[0]!r}"


def test_overclaim_detection_reads_exec_summary_not_empty_final_answer() -> None:
    """★review F-B：谎报检测必须看**用户实际读到的那段文本**。

    M137 #145 下 peer 交 payload 时 `final_answer` 故意留空（答案在
    exec_summary），只喂 final_answer ⇒ 恒空串 ⇒ 检测恒不触发 ⇒ 「已生成 PDF
    可下载」这类谎报在短答路上从没被查过。这里直接对拍 guard 的两种输入。

    ⚠ 不走 spawn handler 端到端：鸭子 ctx 的 ledger `files_verified=False`
    （账本不可信）时 guard 按「误判比漏判更伤」的双条件原则**故意不追加**
    directive ——那是设计，端到端会因此恒绿＝测不到我改的那行（初版这么写，
    当场被自己证伪）。
    """
    from bowhead_core.deliverable_ledger import DeliverableLedger
    from bowhead_core.overclaim_guard import peer_facts_and_directive

    claim = "报告已生成，PDF 文件可直接下载。"
    verified_empty = DeliverableLedger(
        files=0,
        file_exts=frozenset(),
        file_names=(),
        sections=0,
        memories=0,
        ok_tools=frozenset(),
        files_verified=True,  # Trusted ledger with zero entries ⇒ both conditions hold
        sections_verified=True,
        memories_verified=True,
    )
    _, directive_from_claim = peer_facts_and_directive(verified_empty, claim)
    _, directive_from_empty = peer_facts_and_directive(verified_empty, "")
    assert directive_from_claim, "账本为 0 而 peer 声称有文件 → 必须给纠偏 directive"
    assert not directive_from_empty, "空文本无从判断 → 不该乱加（正对照）"
    # ⇒ Nonempty exec_summary yields the directive; final_answer (always empty under DF-04) does not.
    assert _chat_direct_eligible(_Ctx(), SHORT_ANSWER, primary=True, no_file_directive=directive_from_claim) is False


async def test_spawn_omits_sentinel_on_multi_peer_turn() -> None:
    """handler 级正对照：本 turn 已派 ≥2 个 peer → **不打**哨兵键，其余字段照旧。

    转正前这条正对照是「flag 关 → 无哨兵」；flag 没了之后必须换一个**仍然会变红**
    的负例，否则这一族就只剩恒真断言（同 turn 派多少个 peer 都打哨兵也发现不了）。
    """
    inv = await _spawn_once(spawn_count=2)
    assert "_chat_direct_delivery" not in inv.result
    assert is_chat_direct_delivery_sentinel(inv.result)[0] is False
    # Legacy fields remain unchanged (negative cases disable only the sentinel, preserving delivery content).
    assert inv.result["ok"] is True
    assert inv.result["final_answer"] == SHORT_ANSWER


# ─────────────────────────────────────────────────────────────
# 3. Loop level: one fewer LLM iteration (core benefit; count router calls directly)
# ─────────────────────────────────────────────────────────────


def _counting_router(*rs: LLMResponse) -> tuple[Router, list[int]]:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = list(rs)
    calls = [0]
    orig = a.invoke

    async def _counted(req: Any) -> LLMResponse:
        calls[0] += 1
        return await orig(req)

    a.invoke = _counted  # type: ignore[method-assign]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "compute", "analyst", "echo"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r, calls


@dataclass
class _StubDatarc:
    async def aclose(self) -> None:
        pass


def _ctx_for_loop() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_cd_loop",
        datarc=_StubDatarc(),  # type: ignore[arg-type]
    )


@pytest.fixture()
def _lead_profile() -> Any:
    prev = PROFILES.get("lead")
    PROFILES["lead"] = SubagentProfile(
        name="lead",
        llm_agent_type="lead",
        # kb_search is listed for the case testing whether a non-spawn tool can hijack the final answer;
        # it must actually dispatch, or the test cannot reach the consumer-side criterion.
        tool_whitelist=("subagent_spawn", "subagent_return", "kb_search"),
        system_prompt="(stub)",
        max_iterations=4,
    )
    try:
        yield
    finally:
        if prev is not None:
            PROFILES["lead"] = prev
        else:
            PROFILES.pop("lead", None)


async def _run_lead_with_spawn_result(spawn_result: dict, router: Router) -> SubagentResult:
    runner = SubagentRunner(router=router, tool_registry=build_default_registry())

    async def _stub_exec(tc: Any, *args: Any, **kwargs: Any) -> Any:
        from bowhead_core.subagent import ToolInvocation

        return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result=spawn_result, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    return await runner.run(
        profile_name="lead",
        task="今年营业额是多少",
        parent_turn_id="t_cd_loop",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx_for_loop(),
    )


async def test_loop_short_circuits_and_saves_one_llm_round(_lead_profile: Any) -> None:
    """核心收益断言：带哨兵 → 只跑 1 轮 LLM，final_answer = peer 原文，标记置位。"""
    router, calls = _counting_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="subagent_spawn", arguments={"profile": "compute", "task": "查"})]),
        LLMResponse(content="Lead 复读轮（不该跑到）", tool_calls=[]),
    )
    result = await _run_lead_with_spawn_result(
        {"ok": True, "final_answer": SHORT_ANSWER, "_chat_direct_delivery": {"text": SHORT_ANSWER}},
        router,
    )
    assert calls[0] == 1, f"短路后不该再有 Lead 轮，实际 LLM 调用 {calls[0]} 次"
    assert result.final_answer == SHORT_ANSWER
    assert result.chat_direct_delivered is True


async def test_loop_without_sentinel_runs_the_extra_round(_lead_profile: Any) -> None:
    """正对照：无哨兵（peer 走报告 payload / prose 收尾的形状）→ 老路多跑一轮 Lead LLM 复读。
    这条与上一条成对——短路接线被摘掉时，上一条的计数会与本条趋同而变红。"""
    router, calls = _counting_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="subagent_spawn", arguments={"profile": "compute", "task": "查"})]),
        LLMResponse(content="Lead 复述：" + SHORT_ANSWER, tool_calls=[]),
    )
    result = await _run_lead_with_spawn_result({"ok": True, "final_answer": SHORT_ANSWER}, router)
    assert calls[0] == 2
    assert result.chat_direct_delivered is False
    assert result.final_answer.startswith("Lead 复述")


async def test_non_spawn_tool_cannot_hijack_final_answer(_lead_profile: Any) -> None:
    """★转正后的防线：哨兵**只认 `subagent_spawn` 的返回值**。

    哨兵谓词要的「结构」只是一个非空字符串，任何工具（外部/团队工具、未来某个
    返回 dict 的内置工具）都可能巧合或故意带上 `_chat_direct_delivery{text}`。
    转正前挡住这条的是 flag（OFF 时消费侧不认哨兵）——那其实是**碰巧管用**，
    flag 一删就没了。现在由工具名这个真判据顶上，本用例钉住它。
    """
    router, calls = _counting_router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="kb_search", arguments={"query": "x"})]),
        LLMResponse(content="Lead 正常收尾", tool_calls=[]),
    )
    result = await _run_lead_with_spawn_result(
        {"ok": True, "final_answer": SHORT_ANSWER, "_chat_direct_delivery": {"text": "劫持文本"}},
        router,
    )
    assert calls[0] == 2, "非 spawn 工具打的哨兵不该让 Lead 短路"
    assert result.chat_direct_delivered is False
    assert "劫持文本" not in (result.final_answer or "")


@pytest.mark.parametrize("parallel", ["1", "0"])
async def test_two_spawns_in_one_batch_must_not_short_circuit(_lead_profile: Any, monkeypatch: pytest.MonkeyPatch, parallel: str) -> None:
    """★review F-A 的对抗用例：**同一轮**派 2 个 peer。

    计数若在 spawn handler 入口（拿到并发信号量后才跑），先返回的 peer 会看到
    count==1 → 误判单 spawn → 短路 → 另一个 peer 的产出进不了 chat 终答。
    计数改到执行前（survived_tcs 结算）后，两条执行路径都必须不短路。
    串行档（BOWHEAD_CHI_PARALLEL_EXEC=0）是确定性复现档，一并参数化。
    """
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", parallel)
    router, calls = _counting_router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="subagent_spawn", arguments={"profile": "compute", "task": "查数"}),
                ToolCall(id="c2", name="subagent_spawn", arguments={"profile": "compute", "task": "做复盘"}),
            ],
        ),
        LLMResponse(content="Lead 整合两个 peer 的结论", tool_calls=[]),
    )
    # Both spawns return sentinels (worst case: accepting the first would swallow the other).
    result = await _run_lead_with_spawn_result(
        {"ok": True, "final_answer": SHORT_ANSWER, "_chat_direct_delivery": {"text": SHORT_ANSWER}},
        router,
    )
    assert calls[0] == 2, "多 peer 轮必须让 Lead 跑收尾轮做整合，不得短路"
    assert result.chat_direct_delivered is False
    assert result.final_answer == "Lead 整合两个 peer 的结论"


def test_turn_end_reset_lives_in_the_finally_sweep_not_the_canvas_branch() -> None:
    """★review#2 F-3/F-C：spawn 计数的 turn-end 清零必须在 `run_turn` 的 finally 扫里。

    上一版放在 composer 的 `_rk_for_composer == "canvas"` 分支内 —— 而 **chat 直达轮
    恰恰进不去那个分支**（短答 payload 被聚合器跳过 ⇒ aggregated_payload None ⇒
    `_composer_entry_ok` False）：canvas 轮会清、chat 轮永不清，要保护的那一半正好
    没保护到（sticky ctx 上 chat 直达永久静默失效）。

    ⚠ 这里用源码位置断言而非行为断言：清零发生在 `run_turn` 的 finally，驱动它需要
    完整 turn 装配（router/store/bus 全套），成本远高于收益；而「它在哪个块里」正是
    出错的那一维。锚 `finally` 扫内的邻居语句（`_ν_retry_counts` GC）以防挪位。
    """
    import re
    from pathlib import Path

    import bowhead_core.agents.lead_orchestrator as _lo

    src = Path(_lo.__file__).read_text(encoding="utf-8")
    # Reset must appear within 200 lines after `_ν_retry_counts` GC (the finally sweep's recognizable neighbor).
    m_gc = re.search(r"_ν_counts = getattr\(runtime\.parent_ctx, \"_ν_retry_counts\"", src)
    m_reset = re.search(r"runtime\.parent_ctx\._chat_direct_spawn_count = 0", src)
    assert m_gc and m_reset, "锚点缺失——清零或 finally 扫被挪走了"
    gc_line = src[: m_gc.start()].count("\n")
    reset_line = src[: m_reset.start()].count("\n")
    assert 0 < reset_line - gc_line < 200, f"清零应紧随 finally 扫（gc@{gc_line} reset@{reset_line}）"
    # It must **not** lie in `_compose_turn_output`: resetting there would again be blocked by
    # `_rk_for_composer == "canvas"`, so chat direct-delivery turns would never reset.
    # ⚠ Check the **function-body interval**, not `find('_rk_for_composer == "canvas"')`: the latter
    # returns the **first occurrence** offset, while reset is earlier in run_turn, mistaking before
    # for inside (the first version did this and immediately failed).
    m_compose_def = re.search(r"^async def _compose_turn_output\(", src, re.M)
    assert m_compose_def, "锚点缺失：_compose_turn_output 定义"
    compose_end = src.find("return composer_emitted_kinds, text_for_chat", m_compose_def.start())
    assert compose_end > m_compose_def.start()
    assert not (m_compose_def.start() < m_reset.start() < compose_end), "清零回到了 compose 体内——chat 轮将永不清零"


async def test_peer_profile_does_not_short_circuit() -> None:
    """I5 纪律（同 clarification break）：短路只对 lead；peer 内 spawn 照常回喂。"""
    prev = PROFILES.get("compute")
    PROFILES["compute"] = SubagentProfile(
        name="compute",
        llm_agent_type="analyst",
        tool_whitelist=("subagent_spawn", "subagent_return"),
        system_prompt="(stub)",
        max_iterations=4,
    )
    try:
        router, calls = _counting_router(
            LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="subagent_spawn", arguments={"profile": "compute", "task": "查"})]),
            LLMResponse(content="peer 继续干活", tool_calls=[]),
        )
        runner = SubagentRunner(router=router, tool_registry=build_default_registry())

        async def _stub_exec(tc: Any, *args: Any, **kwargs: Any) -> Any:
            from bowhead_core.subagent import ToolInvocation

            return ToolInvocation(
                tool_name=tc.name,
                arguments=tc.arguments,
                result={"ok": True, "_chat_direct_delivery": {"text": SHORT_ANSWER}},
                error=None,
            )

        runner._execute_tool = _stub_exec  # type: ignore[method-assign]
        result = await runner.run(
            profile_name="compute",
            task="子任务",
            parent_turn_id="t_cd_peer",
            parent_bus=TurnEventBus(),
            parent_ctx=_ctx_for_loop(),
        )
        assert calls[0] == 2, "peer 不该被 chat-direct 哨兵短路"
        assert result.chat_direct_delivered is False
    finally:
        if prev is not None:
            PROFILES["compute"] = prev
        else:
            PROFILES.pop("compute", None)


async def _run_lead_with_results(results: dict[str, dict], router: Router) -> SubagentResult:
    runner = SubagentRunner(router=router, tool_registry=build_default_registry())

    async def _stub_exec(tc: Any, *args: Any, **kwargs: Any) -> Any:
        from bowhead_core.subagent import ToolInvocation

        return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result=dict(results[tc.name]), error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    return await runner.run(
        profile_name="lead",
        task="8月营业额最高的门店；知识库里星舰 Pro 的定价",
        parent_turn_id="t_cd_loop",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx_for_loop(),
    )


def _capturing_router(*rs: LLMResponse) -> tuple[Router, list[Any]]:
    router, _calls = _counting_router(*rs)
    adapter = router._registry._adapters["p1"]  # type: ignore[attr-defined]
    seen: list[Any] = []
    inner = adapter.invoke

    async def _capture(req: Any) -> LLMResponse:
        seen.append(req)
        return await inner(req)

    adapter.invoke = _capture  # type: ignore[method-assign]
    return router, seen


PEER_OVERREACH = "8 月营业额最高的是东湖街。知识库部分：当前环境未接入知识库检索能力。"


@pytest.mark.parametrize("parallel", ["1", "0"])
async def test_lead_own_kb_result_blocks_direct_delivery(_lead_profile: Any, monkeypatch: pytest.MonkeyPatch, parallel: str) -> None:
    """116 1.3.51 实测：Lead 同一轮并行 kb_search + spawn，peer 短答直达 ⇒ Lead 自己检索到的片段整段丢掉，
    peer 越界说「未接入知识库」成了终答。Lead 有 peer 没看到的结果时必须跑收尾轮，且收尾轮读到的不再是
    「直接使用 final_answer」。"""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", parallel)
    router, seen = _capturing_router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="kb_search", arguments={"query": "星舰 Pro 定价"}),
                ToolCall(id="c2", name="subagent_spawn", arguments={"profile": "compute", "task": "查8月营业额最高门店"}),
            ],
        ),
        LLMResponse(content="Lead 整合：东湖街；星舰 Pro 6499 元起", tool_calls=[]),
    )
    result = await _run_lead_with_results(
        {
            "kb_search": {"chunks": [{"chunk_id": "chunk_a", "file_id": "kbf_1", "file_name": "评测.md", "content": "官方指导价 6499 元起"}]},
            "subagent_spawn": {
                "ok": True,
                "final_answer": PEER_OVERREACH,
                "directive": "这是简单查询/计算的最终 Chat 答案。直接使用 final_answer，不要生成报告、不要提及画布，也不要改写其中的数字。",
                "_chat_direct_delivery": {"text": PEER_OVERREACH},
            },
        },
        router,
    )
    assert len(seen) == 2, "Lead 自己有 kb_search 结果时不得直达"
    assert result.chat_direct_delivered is False
    assert result.final_answer.startswith("Lead 整合")
    spawn_msgs = [m.content for m in seen[1].messages if getattr(m, "role", None) == "tool" and getattr(m, "name", None) == "subagent_spawn"]
    assert len(spawn_msgs) == 1
    assert "直接使用 final_answer" not in spawn_msgs[0]
    assert "_chat_direct_delivery" not in spawn_msgs[0]
    assert "只覆盖你派给它的那部分" in spawn_msgs[0]


async def test_neutral_lead_tool_keeps_direct_delivery(_lead_profile: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：Lead 同轮只多调了不产出内容的工具（skill_list）⇒ 仍直达（116 直达轮的常见形状）。"""
    PROFILES["lead"] = replace(PROFILES["lead"], tool_whitelist=(*PROFILES["lead"].tool_whitelist, "skill_list"))
    router, seen = _capturing_router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="skill_list", arguments={}),
                ToolCall(id="c2", name="subagent_spawn", arguments={"profile": "compute", "task": "查"}),
            ],
        ),
        LLMResponse(content="Lead 复读轮（不该跑到）", tool_calls=[]),
    )
    result = await _run_lead_with_results(
        {"skill_list": {"skills": []}, "subagent_spawn": {"ok": True, "final_answer": SHORT_ANSWER, "_chat_direct_delivery": {"text": SHORT_ANSWER}}},
        router,
    )
    assert len(seen) == 1
    assert result.chat_direct_delivered is True
    assert result.final_answer == SHORT_ANSWER
