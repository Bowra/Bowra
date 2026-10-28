"""ξ B1 — LeadDispatchGuard: block Lead post-peer-return bi.* / spawn calls.

Prod prod trace: 4/10 turns 多 peer.return + Lead 后继续 bi.query — Lead 不知
peer 已交付继续 explore. Guard 应该 block Lead 在 peer.return 后调 bi.* /
subagent.spawn, force Lead emit final_answer text.
"""

from __future__ import annotations

from bowhead_core.subagent import LeadDispatchGuard


def test_helper_payload_allows_remaining_work_but_primary_still_converges() -> None:
    payloads = [{"delegation_role": "helper", "payload": {"delivery_surface": "chat", "exec_summary": "The source has no subtitle."}}]
    guard = LeadDispatchGuard(payloads_ref=payloads)
    assert not guard.should_block("subagent_spawn")
    assert not guard.should_block("bi_query")
    payloads.append({"delegation_role": "primary", "payload": {"delivery_surface": "chat", "exec_summary": "The comparison is complete."}})
    assert guard.should_block("subagent_spawn")
    assert guard.should_block("bi_query")


def test_b1_no_payloads_no_block() -> None:
    """Empty payloads_ref → no block (Lead can still call tools)."""
    g = LeadDispatchGuard(payloads_ref=[])
    assert g.should_block("bi_query") is False
    assert g.should_block("subagent_spawn") is False
    assert g.has_non_bail_payload() is False


def test_b1_non_bail_payload_blocks_bi_query() -> None:
    """Peer delivered non-bail AnalysisPayload → block subsequent bi.query."""
    payloads = [
        {
            "payload": {
                "title": "营业分析报告",
                "exec_summary": "整体合规率 11.39%",
                "kpis": [{"label": "合规率", "value": "11.39%"}],
                "insights": [{"title": "x", "facts": []}],
            },
        }
    ]
    g = LeadDispatchGuard(payloads_ref=payloads)
    assert g.has_non_bail_payload() is True
    assert g.should_block("bi_query") is True
    assert g.should_block("bi_schema") is True
    # Two legacy_alias registrations still under bi_schema (hidden from LLM, handlers remain)
    assert g.should_block("bi_list_measures") is True
    assert g.should_block("bi_list_dimensions") is True
    assert g.should_block("subagent_spawn") is True
    # 2026-09-05 (tool-inventory): previously also asserted bi_attribute / bi_fetch_rows were blocked.
    # Both retired with their registration sites, so they are no longer tools; blocking never matches (errant calls return tool not found).
    # Asserting a nonexistent tool is blocked is meaningless; remove these assertions with the deny list.


def test_b1_bail_payload_does_not_block() -> None:
    """Peer returned bail envelope (error.kind) → Lead 应该 continue explore, NOT block."""
    payloads = [
        {
            "payload": {
                "error": {"kind": "domain_mismatch", "message": "ws schema 不匹配"},
            },
        }
    ]
    g = LeadDispatchGuard(payloads_ref=payloads)
    assert g.has_non_bail_payload() is False
    assert g.should_block("bi_query") is False


def test_b1_mixed_payloads_one_non_bail_blocks() -> None:
    """If ANY peer returned non-bail payload → block (even if others were bail)."""
    payloads = [
        {"payload": {"error": {"kind": "transient", "message": "fail 1"}}},
        {"payload": {"title": "Real report", "kpis": [{"label": "x", "value": "y"}]}},
        {"payload": {"error": {"kind": "tool_failure", "message": "fail 2"}}},
    ]
    g = LeadDispatchGuard(payloads_ref=payloads)
    assert g.has_non_bail_payload() is True
    assert g.should_block("bi_query") is True


def test_b1_canvas_add_tools_not_in_deny() -> None:
    """canvas.add_chart NOT in deny list — Lead 可以继续 canvas 写 (post-peer is OK to chart)."""
    payloads = [{"payload": {"kpis": [{"label": "x", "value": "y"}]}}]
    g = LeadDispatchGuard(payloads_ref=payloads)
    assert g.should_block("add_chart") is False
    assert g.should_block("canvas_add_kpi_row") is False
    assert g.should_block("subagent_return") is False  # peer terminal tool ok


def test_b1_kb_tools_not_in_deny() -> None:
    """kb.search / kb.list_tables 也 OK 让 Lead 后置查 KB (e.g. cite SOP)."""
    payloads = [{"payload": {"kpis": [{"label": "x", "value": "y"}]}}]
    g = LeadDispatchGuard(payloads_ref=payloads)
    assert g.should_block("kb_search") is False
    assert g.should_block("kb_list_tables") is False


def test_b1_malformed_payloads_treated_safely() -> None:
    """Non-dict entries in payloads_ref ignored, no crash."""
    payloads = [None, "string", {"payload": "not a dict"}, {"no_payload_key": "x"}]
    g = LeadDispatchGuard(payloads_ref=payloads)
    assert g.has_non_bail_payload() is False
    assert g.should_block("bi_query") is False
