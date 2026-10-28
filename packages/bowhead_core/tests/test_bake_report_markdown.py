"""delivery-surface M-B —— 卫兵下沉进笔：bake_report_markdown 写路管线。

原两份实现（writer tool-loop + _direct_compose）收敛为一份；语义逐条保留：
strip_legacy → (¬update) strip_untrusted → [cite:N] 解析 → 去头部 h1 →
[N] 铸造+数字防伪 → 裸 artifact id 换写 → 非法 figure 锚剥除。
handler 端接线由 test_report_tools 系列 + writer-loop 全量测试盯（characterization）。
review #8/#9 后 bake 为 async（ContextVar 优先取 invocations + registry rehydrate）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.report_writer_loop import bake_report_markdown
from bowhead_core.sources_resolver import ensure_turn_sources_registry

pytestmark = pytest.mark.asyncio


def _ctx(bus: object | None = None, invs: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(parent_bus=bus, _report_bake_invocations=invs or [])


async def test_resolve_cites_valid_and_orphan() -> None:
    bus = SimpleNamespace()
    reg = ensure_turn_sources_registry(bus)
    reg.ingest_payload_sources([{"label": "bi_query: GMV", "tool": "bi_query", "artifact_id": "art_1"}], None)
    ctx = _ctx(bus)
    ctx._cites_are_source_numbers = True  # Writer loop: prompt lists source numbers.
    md, _ = await bake_report_markdown("GMV 5.2 亿 [cite:1]，另见 [cite:99]。", ctx=ctx)
    assert "[1]" in md
    assert "[cite:" not in md
    assert "[99]" not in md  # Strip hallucinated numbers.


async def test_untrusted_bare_numbered_stripped_on_append_kept_on_update() -> None:
    """模型自写裸 [N] 只在净新 append 时剥；update 回合旧 [N] 是合法引用。"""
    text = "营收上行 [1]。"
    md_append, _ = await bake_report_markdown(text, ctx=_ctx(), is_update=False)
    assert "[1]" not in md_append
    md_update, _ = await bake_report_markdown(text, ctx=_ctx(), is_update=True)
    assert "[1]" in md_update


async def test_leading_h1_stripped_chapter_headers_kept() -> None:
    md, _ = await bake_report_markdown("# 报告标题\n\n开头段。\n\n## 区域结构\n正文。", ctx=_ctx())
    assert not md.startswith("#")
    assert "## 区域结构" in md


async def test_invalid_figure_anchor_stripped_valid_kept() -> None:
    text = "论点 A。\n[figure:sec_real]\n**图1**:真图。\n\n论点 B。\n[figure:sec_fake]\n**图2**:编造的。"
    md, _ = await bake_report_markdown(text, ctx=_ctx(), figure_ids={"sec_real"})
    assert "[figure:sec_real]" in md
    assert "[figure:sec_fake]" not in md
    assert "**图2**" not in md  # Strip dangling figure captions too.


async def test_no_bus_no_registry_still_bakes_static_guards() -> None:
    """peer/直调没有 bus 时：[N] 铸造跳过，但静态卫兵（strip/h1/figure）照跑。"""
    md, unv = await bake_report_markdown("# 标题\n正文 [cite:1]。", ctx=SimpleNamespace())
    assert not md.startswith("#")
    assert "[cite:" not in md
    assert unv == []


# ── Bare [N] written by the model (116 s_18c8b86f3003: revision body full of [127]/[130], only 62 sources) ──


def _allowlist_with_data_at(n: int, real_id: str):
    from bowhead_core._ref_allowlist import RefAllowlist

    al = RefAllowlist()
    for i in range(1, n):
        al.add("tl", f"call_{i}")
    assert al.add("data", real_id) == n
    return al


def _bi_inv(aid: str, query: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name="bi_query",
        call_id="call_bi",
        arguments={"query": query},
        result={"results": [{"artifact_id": aid, "query_text": query}]},
    )


def _seeded_bus(n: int) -> SimpleNamespace:
    bus = SimpleNamespace()
    reg = ensure_turn_sources_registry(bus)
    reg.seed([{"label": f"来源{i}", "tool": "bi_query", "artifact_id": f"art_old{i}", "query": f"q{i}"} for i in range(1, n + 1)])
    return bus


async def test_update_keeps_prior_numbers_and_strips_new_ones() -> None:
    """更新路只放过旧正文里本来就有的号；模型新写的号不再整段豁免。"""
    md, _ = await bake_report_markdown(
        "营收上行 [1]，客流下滑 [2]。",
        ctx=_ctx(_seeded_bus(3)),
        is_update=True,
        prior_markdown="营收上行 [1]。",
    )
    assert "[1]" in md
    assert "[2]" not in md
    assert "" not in md


async def test_update_translates_list_number_to_real_source() -> None:
    """★★ 写成 [127] 的是本 run 清单里的数据编号：登记成来源、换成真编号，点得开也对得上。"""
    from bowhead_core.agents._lead_context import reset_current_ref_allowlist, set_current_ref_allowlist

    bus = _seeded_bus(62)
    tok = set_current_ref_allowlist(_allowlist_with_data_at(127, "art_store"))
    try:
        md, _ = await bake_report_markdown(
            "东湖街营业额 12.46亿元 [127]。",
            ctx=_ctx(bus, [_bi_inv("art_store", "2026年1-6月 各门店 营业额")]),
            is_update=True,
            prior_markdown="东湖街营业额 12.00亿元 [26]。",
        )
    finally:
        reset_current_ref_allowlist(tok)
    reg = ensure_turn_sources_registry(bus)
    assert "[127]" not in md
    assert "[63]" in md
    assert reg.sources[62]["artifact_id"] == "art_store"


async def test_small_new_number_is_stripped_not_translated() -> None:
    """不超过登记表条数的号可能是抄来的别段来源号——翻了会错绑，照剥。"""
    from bowhead_core.agents._lead_context import reset_current_ref_allowlist, set_current_ref_allowlist

    bus = _seeded_bus(10)
    tok = set_current_ref_allowlist(_allowlist_with_data_at(5, "art_store"))
    try:
        md, _ = await bake_report_markdown(
            "东湖街营业额 12.46亿元 [5]。",
            ctx=_ctx(bus, [_bi_inv("art_store", "门店营业额")]),
            is_update=True,
            prior_markdown="旧正文。",
        )
    finally:
        reset_current_ref_allowlist(tok)
    assert "[5]" not in md
    assert ensure_turn_sources_registry(bus).size() == 10


async def test_unknown_list_number_is_stripped() -> None:
    """清单里没有、或不是本轮认得的数据 ⇒ 剥，不留点不开的号。"""
    from bowhead_core.agents._lead_context import reset_current_ref_allowlist, set_current_ref_allowlist

    tok = set_current_ref_allowlist(_allowlist_with_data_at(127, "art_elsewhere"))
    try:
        md, _ = await bake_report_markdown(
            "营业额 [127]，另见 [300]。",
            ctx=_ctx(_seeded_bus(62), []),
            is_update=True,
            prior_markdown="旧正文。",
        )
    finally:
        reset_current_ref_allowlist(tok)
    assert "[127]" not in md and "[300]" not in md
    assert "" not in md


async def test_peer_cite_number_is_a_list_number_not_a_source_number() -> None:
    """★★ 116 `t_418272316b73`：非写手写的 `[cite:24]` 是清单编号——按清单翻成真来源号，
    不按来源号收下（原样收下挂到了来源 24「各月客流量」上）。不受「大于条数」限制。"""
    from bowhead_core.agents._lead_context import reset_current_ref_allowlist, set_current_ref_allowlist

    bus = _seeded_bus(62)
    reg = ensure_turn_sources_registry(bus)
    reg.seed([])
    reg._sources[57]["artifact_id"] = "art_cat"
    reg._id_to_index["art_cat"] = 58
    tok = set_current_ref_allowlist(_allowlist_with_data_at(24, "art_cat"))
    try:
        md, _ = await bake_report_markdown(
            "女装占比 37.28% [cite:24]，另见 [cite:5]。",
            ctx=_ctx(bus, []),
            is_update=True,
            prior_markdown="旧正文。",
        )
    finally:
        reset_current_ref_allowlist(tok)
    assert "[58]" in md
    assert "[24]" not in md and "[5]" not in md and "[cite:" not in md
