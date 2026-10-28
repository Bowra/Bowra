"""canvas-summon: `[cite:N]` 不该因为「Lead 自己调的工具」而被剥成断句。

现场（116 prod t_fc73e8cf873c 实测，2026-08-03）：

    narrative.delta（实时气泡）: 「…完整数据见 [cite:1]）」
    final_answer（落库/刷新后）: 「…完整数据见 ）」        ← marker 被剥，句子断了

该轮**确实有来源**：`lead.final.payload.sources` 里就一条 bi_query 的
`art_35319cae0463`。模型写 `[cite:1]` 是对的，是后端没把它注册进校验用的那个表：
`TurnSourcesRegistry`（用户看见的「来源 N」）和 `TurnCitationRegistry`（校验
`[cite:N]`）是两个表，Lead 自己调工具的产物只进前者。

⚠ 本文件只按**真实形状**写用例：来源来自 `result.tool_invocations`。
早先版本有一半用例往 `parent_ctx.turn_sources_registry` 里塞数据——独立 review 查证
全仓没有任何生产代码往 ctx 上挂那张表（`ensure_turn_sources_registry` 的调用方一律
传 bus），那条兜底连同那些用例一起删了：**测试不该替调用方补齐它不会做的事**。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.lead_orchestrator import _resolve_citations
from bowhead_core.citations import TurnCitationRegistry


def _bi_invocation(artifact_id: str, query: str = "各门店营业额"):
    """真实形状：bi_query 的 invocation，结果里带 artifact_id。"""
    return SimpleNamespace(
        tool_name="bi_query",
        arguments={"question": query},
        result={"artifact_id": artifact_id, "ok": True},
    )


def test_lead_only_tool_turn_keeps_its_citation() -> None:
    """核心回归：单来源的 Lead-only 轮，`[cite:1]` 必须活下来。"""
    ctx = SimpleNamespace()  # Real path: no source table on ctx.
    out = _resolve_citations(
        "（共 39 家门店，前 30 如上；完整数据见 [cite:1]）",
        ctx,
        invocations=[_bi_invocation("art_35319cae0463")],
    )
    assert "[cite:1]" in out, out
    assert "完整数据见 ）" not in out, "marker 被剥掉 → 句子断成「见 ）」，正是用户看到的形态"


def test_numbering_aligns_with_visible_sources_list() -> None:
    """`[cite:2]` 必须指向「来源 2」，不能错位。"""
    ctx = SimpleNamespace()
    _resolve_citations(
        "见 [cite:1] 与 [cite:2]",
        ctx,
        invocations=[_bi_invocation("art_aaaaaaaaaaaa", "A"), _bi_invocation("art_bbbbbbbbbbbb", "B")],
    )
    reg: TurnCitationRegistry = ctx.turn_citation_registry
    assert reg.resolve(1).artifact_id == "art_aaaaaaaaaaaa"
    assert reg.resolve(2).artifact_id == "art_bbbbbbbbbbbb"


def test_seeding_stops_at_a_source_without_artifact_id(monkeypatch) -> None:
    """来源里有一条没 artifact_id 时必须停——继续播会让后面的 N 全部错位。

    ⚠ 今天 `_sources_from_invocations` 总会给出 artifact_id（web 结果用 url），
    所以这条防线在当前实现下走不到。保留它、并直接喂形状来测，是因为**编号对齐**
    就是这个修复的全部价值：一旦上游哪天允许无 id 的来源，错位是静默的。
    """
    from bowhead_core.agents import lead_orchestrator as lo

    monkeypatch.setattr(
        lo,
        "_sources_from_invocations",
        lambda invs: [
            {"label": "A", "artifact_id": "art_aaaaaaaaaaaa"},
            {"label": "只有标签"},  # ← Breakpoint.
            {"label": "C", "artifact_id": "art_cccccccccccc"},
        ],
    )
    ctx = SimpleNamespace()
    _resolve_citations("见 [cite:1]", ctx, invocations=[_bi_invocation("art_aaaaaaaaaaaa")])
    reg: TurnCitationRegistry = ctx.turn_citation_registry
    assert reg.size() == 1
    assert reg.resolve(1).artifact_id == "art_aaaaaaaaaaaa"
    assert reg.resolve(2) is None, "第三条被播进来会占到 seq 2 → `[cite:2]` 指向「来源 3」"


def test_existing_registry_is_not_renumbered() -> None:
    """peer/KB 路已经建了编号空间 → 不插队，维持现状（宁可维持也不能改数）。"""
    reg = TurnCitationRegistry()
    reg.register("art_peer00000000", label="peer 的", kind="tool_output")
    ctx = SimpleNamespace(turn_citation_registry=reg)
    _resolve_citations("见 [cite:1]", ctx, invocations=[_bi_invocation("art_aaaaaaaaaaaa")])
    assert reg.size() == 1
    assert reg.resolve(1).artifact_id == "art_peer00000000"


def test_orphan_beyond_sources_still_stripped() -> None:
    """真孤儿（模型编了个超出来源数的号）仍然剥——这条策略没变。"""
    ctx = SimpleNamespace()
    out = _resolve_citations("见 [cite:7]", ctx, invocations=[_bi_invocation("art_aaaaaaaaaaaa")])
    assert "[cite:7]" not in out


def test_no_invocations_is_a_noop() -> None:
    """没有工具调用（纯问答轮）→ 行为逐字不变：孤儿照剥。"""
    ctx = SimpleNamespace()
    out = _resolve_citations("见 [cite:1]", ctx, invocations=None)
    assert "[cite:1]" not in out
