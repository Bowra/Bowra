"""Sprint omicron Phase A — invariant check unit tests."""

from __future__ import annotations

from dataclasses import dataclass

from bowhead_core.pipeline_invariants import check_invariants


@dataclass
class _MockInv:
    tool_name: str
    arguments: dict | None = None


def test_inv1_triggered_when_lead_calls_bi_without_peer():
    invs = [_MockInv("bi_fetch_rows"), _MockInv("bi_schema")]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="some prose",
        has_final_answer_event=True,
    )
    assert "INV-1" in r.violated
    assert r.bi_tool_count == 1  # bi.schema excluded
    assert r.peer_dispatched == 0


def test_inv1_not_triggered_when_peer_dispatched():
    invs = [_MockInv("bi_fetch_rows"), _MockInv("subagent_spawn", {"profile": "compute"})]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="some prose",
        has_final_answer_event=True,
    )
    assert "INV-1" not in r.violated
    assert r.peer_dispatched == 1


def test_inv1_not_triggered_when_no_bi_at_all():
    invs = [_MockInv("skill_read")]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="just chat",
        has_final_answer_event=True,
    )
    assert "INV-1" not in r.violated


def test_inv5_triggered_when_lead_self_answer_no_cite():
    invs = [_MockInv("bi_fetch_rows")]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="销售额 100 万",  # no [ref:N]
        has_final_answer_event=True,
    )
    assert "INV-5" in r.violated


def test_inv5_not_triggered_when_cite_present():
    invs = [_MockInv("bi_fetch_rows")]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="销售额 100 万 [ref:1]",
        has_final_answer_event=True,
    )
    assert "INV-5" not in r.violated
    assert r.cite_count == 1


def test_inv6_silent_done():
    r = check_invariants(
        tool_invocations=[],
        final_answer_text=None,
        has_final_answer_event=False,
    )
    assert "INV-6" in r.violated


def test_inv6_not_triggered_when_final_answer_emitted():
    r = check_invariants(
        tool_invocations=[],
        final_answer_text="OK",
        has_final_answer_event=True,
    )
    assert "INV-6" not in r.violated


def test_inv8_triggered_on_hardcoded_numbers_no_bi():
    r = check_invariants(
        tool_invocations=[],  # no bi.*
        final_answer_text="GMV 约 13.87 亿元, 同比下降 5.3%",
        has_final_answer_event=True,
    )
    assert "INV-8" in r.violated


def test_inv8_not_triggered_when_bi_called():
    invs = [_MockInv("bi_fetch_rows")]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="GMV 13.87 亿",
        has_final_answer_event=True,
    )
    assert "INV-8" not in r.violated


def test_inv8_not_triggered_on_dates():
    """Tightened regex per Eng-r3 — `元` / `个` / `CNY` dropped, only 亿/万/% match.
    Dates like '2026年' don't match."""
    r = check_invariants(
        tool_invocations=[],
        final_answer_text="今天是 2026 年 5 月 11 日, 第 2 季度.",
        has_final_answer_event=True,
    )
    assert "INV-8" not in r.violated


def test_inv8_triggers_on_percent_without_bi():
    r = check_invariants(
        tool_invocations=[],
        final_answer_text="增长了 23.5%",
        has_final_answer_event=True,
    )
    assert "INV-8" in r.violated


def test_multiple_violations():
    """Lead self-answer + bi.* + no cite = INV-1 + INV-5 (disjoint with INV-8)."""
    invs = [_MockInv("bi_fetch_rows")]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="销售 100 万",
        has_final_answer_event=True,
    )
    assert "INV-1" in r.violated
    assert "INV-5" in r.violated
    # INV-8 disjoint: bi_tool_count > 0
    assert "INV-8" not in r.violated


def test_clean_turn_zero_violations():
    """Peer dispatched + cite + final_answer present + bi called: clean."""
    invs = [_MockInv("bi_fetch_rows"), _MockInv("subagent_spawn", {"profile": "compute"})]
    r = check_invariants(
        tool_invocations=invs,
        final_answer_text="报告已生成 [ref:1] [ref:2]",
        has_final_answer_event=True,
    )
    assert r.violated == []
    assert r.bi_tool_count == 1
    assert r.peer_dispatched == 1
    assert r.cite_count == 2


def test_canvas_section_kinds_chart_count():
    r = check_invariants(
        tool_invocations=[],
        final_answer_text="x",
        canvas_section_kinds={"narrative": 2, "chart": 3, "kpi_row": 1},
        has_final_answer_event=True,
    )
    assert r.chart_count == 3
