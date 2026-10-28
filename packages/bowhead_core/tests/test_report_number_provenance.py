"""report number provenance — gen3 形态（citation-audit M3-2 换形态，契约意图不变）。

原文件锁的是 gen2 的 `_apply_number_provenance`（琥珀 `[num:V:U]` + 勾
`[num:V:U:src:S]` 双 pass）。该函数随 M3 读端下线删除，**它承载的两层契约逐条
移到 gen3 形态**：

  1. **provenance 判据**（哪些数字该绑、哪些该标未验证、哪些结构行不碰）——
     现由 `report_writer_loop._apply_citation_pass` 承担：单源命中 → `值 [N]`；
     无工具背书 → 结构化 `payload.unverified_numbers`；表格/标题/blockquote 不碰；
     同值多源 → 歧义丢弃（保持纯文本，也不误标未验证）。
  2. **id seam 往返契约**（THE invariant，本文件的核心资产）——正文引用指向的
     来源 id 必须是**前端能查到的** id：`source_ref_aggregator` 的 SourceRef 列表
     键在事件 `call_id` 上，binder 走 `inv.call_id`；两者必须相等。曾经静默不等
     （invocation 带自动 `tl_*` id）导致引用点开全空。gen3 下这条更要紧：
     `[N]` → `sources[N-1]` → artifact_id，链路任一环 id 错位，用户点开就是空。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from bowhead_core.citation_marks import bind_to_numbered
from bowhead_core.number_verifier import build_authorized_numbers
from bowhead_core.report_writer_loop import _apply_citation_pass
from bowhead_core.source_ref_aggregator import aggregate_source_refs_from_events
from bowhead_core.sources_resolver import TurnSourcesRegistry
from bowhead_core.types import ToolInvocation

_NUMBERED_RE = re.compile(r"\[(\d{1,3})\]")


def _inv(call_id: str, tool: str, result: object, args: dict | None = None) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool,
        arguments=args or {},
        call_id=call_id,
        result=result,
    )


def _reg(invs: list[ToolInvocation]) -> TurnSourcesRegistry:
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations(invs)
    return reg


# ─── Layer 1: provenance criteria (gen2 dual pass → gen3 [N] + structured fields) ──


def test_verified_number_binds_to_numbered_ref() -> None:
    """单源命中的显著数字 → `值 [N]`（原 gen2：`[num:V:U:src:S]` 勾）。"""
    invs = [_inv("call_rev", "bi_query", {"rows": [{"region": "上海", "revenue": 962700000.0}]})]
    reg = _reg(invs)
    out, unverified = _apply_citation_pass("上海营收达到 9.627亿元，领跑全国。", invs, reg)
    assert "9.627亿元 [1]" in out
    assert not unverified  # Tool-backed → no unverified entry.


def test_unverified_number_goes_to_structured_field_not_inline_marker() -> None:
    """无工具背书的显著数字 → **结构化** `unverified_numbers`（原 gen2：琥珀 inline 标记）。

    正文里**不留任何 per-number 徽章**（总则 4：验证语义去标记化）。
    """
    invs = [_inv("call_rev", "bi_query", {"rows": [{"region": "上海", "revenue": 962700000.0}]})]
    out, unverified = _apply_citation_pass("此外利润高达 5.5亿元。", invs, _reg(invs))
    assert "5.5亿元" in unverified
    assert "[num:" not in out  # Never emit retired markers.
    assert not _NUMBERED_RE.search(out)  # No bindable source → no body [N].


def test_verified_and_unverified_coexist_on_one_line() -> None:
    invs = [_inv("call_rev", "bi_query", {"rows": [{"region": "上海", "revenue": 962700000.0}]})]
    out, unverified = _apply_citation_pass("上海营收 9.627亿元，预计明年达 150亿元。", invs, _reg(invs))
    assert "9.627亿元 [1]" in out  # Verified → traceable [N].
    assert "150亿元" in unverified  # Unverified → structured warning.
    assert "150亿元 [" not in out  # Unverified number never gets [N].


def test_headings_and_quotes_skipped_but_tables_are_bound() -> None:
    """结构行里**只剩标题/blockquote**永不标；表格行 2026-08-12 起要绑。

    skip 规则仍是 `citation_marks` 单一实现。表格改绑的理由：`_collect_unverified` 早已
    取消跳过 `|` 行，binder 还跳着就成了单边 —— 表格数字只能进 unverified 分母、
    进不了 verified 分子，trust 百分比对表格多的报告系统性偏低（详见
    `test_citation_bind_table_lines.py`）。
    """
    invs = [_inv("call_rev", "bi_query", {"rows": [{"v": 962700000.0}]})]
    md = "## 9.627亿元 的标题\n| 区域 | 9.627亿元 |\n> 引用 9.627亿元"
    out, unverified = _apply_citation_pass(md, invs, _reg(invs))
    lines = out.split("\n")
    assert lines[0] == "## 9.627亿元 的标题"
    assert lines[1] == "| 区域 | 9.627亿元 [1] |"
    assert lines[2] == "> 引用 9.627亿元"
    assert not unverified


def test_no_tool_data_no_marking() -> None:
    """零工具 → 授权数字集为空 → 不绑也不标（pre-existing posture；dogfood ② 记档：
    纯编造报告因此无警示，TrustStrip 隐藏兜住「不撒谎」底线）。"""
    out, unverified = _apply_citation_pass("营收 9.627亿元。", [], TurnSourcesRegistry())
    assert out == "营收 9.627亿元。"
    assert not unverified


def test_ambiguous_multi_source_number_stays_plain() -> None:
    """同值多源 → 无单一来源可指 → 不绑 [N]；但它**是**授权数字 → 也不标未验证。"""
    invs = [
        _inv("call_a", "bi_query", {"rows": [{"v": 962700000.0}]}),
        _inv("call_b", "bi_query", {"rows": [{"v": 962700000.0}]}),
    ]
    out, unverified = _apply_citation_pass("两处都报 9.627亿元。", invs, _reg(invs))
    assert out == "两处都报 9.627亿元。"  # ambiguous-drop: stays plain text.
    assert not unverified  # Backed, not fabricated.


# ─── Layer 2: round-trip resolution contract ────────────────────────


def _events_for(call_id: str, tool: str, args: dict, result: dict) -> list:
    """Build the tool_call + tool_result events the way the execution loop emits
    them (both keyed on the provider tc.id == call_id post-fix).

    Prod shape: the structured result lives in result_inline (full dict, small
    results) — the aggregator reads that; result_preview is a 500-char STRING the
    aggregator ignores."""
    import json as _json

    return [
        SimpleNamespace(
            event_type="tool_call",
            payload={
                "call_id": call_id,
                "tool_name": tool,
                "arguments_preview": args,
            },
        ),
        SimpleNamespace(
            event_type="tool_result",
            payload={
                "call_id": call_id,
                "tool_name": tool,
                "result_preview": _json.dumps(result, ensure_ascii=False)[:500],
                "result_inline": result,
            },
        ),
    ]


def test_numbered_ref_resolves_against_aggregator_ids() -> None:
    """THE invariant（gen3 形态）：正文 `[N]` → `sources[N-1].artifact_id` 必须是
    aggregator SourceRef 列表里的 id —— binder 用 `inv.call_id`、aggregator 用事件
    `call_id`，两者是同一个值。错位 = 用户点开引用是空的。"""
    tc_id = "call_rev"  # provider tc.id; post-fix inv.call_id == this
    result = {
        "sql": "SELECT region, SUM(revenue) FROM sales GROUP BY region",
        "rows": [{"region": "上海", "revenue": 962700000.0}],
    }
    # 1. Aggregator builds SourceRefs from turn events.
    srefs = aggregate_source_refs_from_events("t_1", _events_for(tc_id, "bi_query", {"query": "各区域营收"}, result))
    sref_ids = {s["id"] for s in srefs}
    assert tc_id in sref_ids
    # Parsed sref has SQL → popover can show drilldown.
    assert any(s.get("sql", "").startswith("SELECT") for s in srefs)

    # 2. Writer-loop binds body numbers to **same** ID (gen3 mints [N] + registers).
    invs = [_inv(tc_id, "bi_query", result, args={"query": "各区域营收"})]
    reg = _reg(invs)
    out, _ = _apply_citation_pass("上海营收 9.627亿元。", invs, reg)

    # 3. CONTRACT: every [N] resolves to aggregator-known ID.
    ns = [int(n) for n in _NUMBERED_RE.findall(out)]
    assert ns, f"expected a numbered ref, got: {out}"
    for n in ns:
        assert 1 <= n <= len(reg.sources), f"[{n}] 越界 sources（{len(reg.sources)} 条）"
        assert reg.sources[n - 1]["artifact_id"] in sref_ids, f"[{n}] → {reg.sources[n - 1]['artifact_id']!r} 不在 aggregator ids {sref_ids}"


def test_mismatched_ids_would_not_resolve_regression_guard() -> None:
    """记录这道 seam 修掉的 bug：invocation 的 call_id 若不是事件 tc.id（修复前的
    自动 `tl_*`），[N] 指向的来源 id 就查不到 → 引用点开全空。"""
    tc_id = "call_rev"
    result = {"sql": "SELECT 1", "rows": [{"region": "上海", "revenue": 962700000.0}]}
    srefs = aggregate_source_refs_from_events("t_1", _events_for(tc_id, "bi_query", {}, result))
    sref_ids = {s["id"] for s in srefs}
    # Before fix: invocation auto ID diverged from event tc.id.
    invs = [_inv("tl_auto_divergent", "bi_query", result)]
    reg = _reg(invs)
    out, _ = _apply_citation_pass("上海营收 9.627亿元。", invs, reg)
    for n in (int(x) for x in _NUMBERED_RE.findall(out)):
        assert reg.sources[n - 1]["artifact_id"] not in sref_ids


def test_authorized_numbers_superset_of_bound_values() -> None:
    """Sanity: 任何被绑到来源的数字必然「授权」，故绝不会同时被标成未验证。"""
    invs = [_inv("call_rev", "bi_query", {"rows": [{"revenue": 962700000.0}]})]
    authorized = build_authorized_numbers(invs)
    assert 962700000.0 in authorized


def test_binder_and_citation_pass_agree_on_shape() -> None:
    """binder 直调与 writer 路 `_apply_citation_pass` 产出同一形态（单一实现，无第二套）。"""
    invs = [_inv("call_rev", "bi_query", {"rows": [{"revenue": 962700000.0}]})]
    reg_a, reg_b = _reg(invs), _reg(invs)
    via_pass, _ = _apply_citation_pass("营收 9.627亿元。", invs, reg_a)
    via_binder = bind_to_numbered(
        "营收 9.627亿元。",
        value_to_source={962700000.0: "call_rev"},
        sources=reg_b.sources,
        id_to_index=reg_b.id_to_index,
        tool_invocations=invs,
    )
    assert via_pass == via_binder == "营收 9.627亿元 [1]。"
