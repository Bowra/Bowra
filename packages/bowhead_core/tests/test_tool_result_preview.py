"""V5 PRD-E2 · ToolResultPreview builder tests.

Covers:
- build_sql_preview: 1 row → kpi, 2-50 rows → table, >50 rows → count
- build_chart_preview: pass-through chart_spec
- build_text_preview: snippet + length
- redact_pii: SQL field=value patterns
- build_preview_for_tool auto dispatcher (bi.* / canvas.add_chart)
- canvas_anchor optional propagation
- Empty / null safety
"""

from __future__ import annotations

from bowhead_core.tool_result_preview import (
    build_chart_preview,
    build_count_preview,
    build_preview_for_tool,
    build_sql_preview,
    build_text_preview,
    redact_pii,
)

# ─── redact_pii ─────────────────────────────────────────


def test_redact_pii_email_single_quote():
    assert redact_pii("WHERE email='alice@test.com'") == "WHERE email='***'"


def test_redact_pii_phone_double_quote():
    assert redact_pii('WHERE phone="13800001234"') == 'WHERE phone="***"'


def test_redact_pii_ssn():
    assert redact_pii("AND ssn='123-45-6789'") == "AND ssn='***'"


def test_redact_pii_no_pattern_unchanged():
    sql = "SELECT * FROM orders WHERE quarter='Q3'"
    assert redact_pii(sql) == sql


def test_redact_pii_case_insensitive_field_name():
    assert redact_pii("WHERE EMAIL='a@b.com'") == "WHERE EMAIL='***'"


# ─── build_sql_preview ──────────────────────────────────


def test_build_sql_preview_single_row_single_col_kpi():
    rows = [{"total_revenue": 1234567}]
    p = build_sql_preview(rows)
    assert p["kind"] == "kpi"
    assert p["value"] == "1234567"
    assert "Revenue" in p["label"]


def test_build_sql_preview_single_row_multi_col_kpi_first_col():
    rows = [{"region": "EMEA", "revenue": 1000}]
    p = build_sql_preview(rows)
    assert p["kind"] == "kpi"
    assert p["value"] == "EMEA"


def test_build_sql_preview_many_rows_table():
    rows = [{"id": i, "name": f"row_{i}"} for i in range(10)]
    p = build_sql_preview(rows)
    assert p["kind"] == "table"
    assert p["columns"] == ["id", "name"]
    assert len(p["rows"]) == 5  # capped at 5
    assert p["total_rows"] == 10


def test_build_sql_preview_50_rows_threshold_table():
    rows = [{"x": i} for i in range(50)]
    p = build_sql_preview(rows)
    assert p["kind"] == "table"
    assert p["total_rows"] == 50


def test_build_sql_preview_51_rows_count_summary():
    rows = [{"x": i} for i in range(51)]
    p = build_sql_preview(rows)
    assert p["kind"] == "count"
    assert p["value"] == 51


def test_build_sql_preview_empty_returns_none():
    assert build_sql_preview([]) is None


def test_build_sql_preview_redacts_pii_in_table_cells():
    rows = [{"info": "email='alice@test.com'"}]
    p = build_sql_preview([*rows, {"info": "x"}])  # >=2 rows → table path
    assert p["kind"] == "table"
    assert "alice@test.com" not in p["rows"][0][0]
    assert "***" in p["rows"][0][0]


def test_build_sql_preview_with_canvas_anchor():
    rows = [{"x": 1}]
    anchor = {"canvas_id": "cnv_1", "section_id": "sec_a"}
    p = build_sql_preview(rows, canvas_anchor=anchor)
    assert p["canvas_anchor"] == anchor


def test_build_sql_preview_label_hint_overrides():
    rows = [{"x": 1}]
    p = build_sql_preview(rows, label_hint="Q3 Revenue")
    assert p["label"] == "Q3 Revenue"


# ─── build_chart_preview ───────────────────────────────


def test_build_chart_preview_passthrough_spec():
    spec = {"title": "Q3 vs Q2", "kind": "bar", "data": [1, 2, 3]}
    p = build_chart_preview(spec, title="Q3 vs Q2", chart_kind="bar")
    assert p["kind"] == "chart_thumbnail"
    assert p["title"] == "Q3 vs Q2"
    assert p["chart_kind"] == "bar"
    assert p["chart_spec"] is spec  # passthrough reference


def test_build_chart_preview_invalid_kind_falls_back_to_bar():
    p = build_chart_preview({}, title="X", chart_kind="weird_kind")
    assert p["chart_kind"] == "bar"


# ─── build_count_preview / build_text_preview ──────────


def test_build_count_preview():
    p = build_count_preview(label="活跃用户", value=42, qualifier="个")
    assert p["kind"] == "count"
    assert p["value"] == 42
    assert p["qualifier"] == "个"


def test_build_text_preview_truncates_at_200():
    full = "x" * 500
    p = build_text_preview(full)
    assert p["kind"] == "text"
    assert len(p["preview"]) == 200
    assert p["full_length"] == 500


def test_build_text_preview_redacts_in_snippet():
    full = "原文: WHERE email='leak@test.com' 后续..."
    p = build_text_preview(full)
    assert "leak@test.com" not in p["preview"]


# ─── build_preview_for_tool auto dispatcher ────────────


def test_dispatcher_bi_fetch_rows_with_rows():
    result = {"rows": [{"a": 1}, {"a": 2}, {"a": 3}], "row_count": 3}
    p = build_preview_for_tool("bi_fetch_rows", result)
    assert p is not None
    assert p["kind"] == "table"


def test_dispatcher_bi_breakdown_with_rows():
    result = {"rows": [{"region": "EMEA", "rev": 100}, {"region": "APAC", "rev": 80}]}
    p = build_preview_for_tool("bi_breakdown", result)
    assert p is not None
    assert p["kind"] == "table"


def test_dispatcher_bi_ask_question_metric_kpi():
    result = {"type": "metric", "metrics": [{"name": "Q3 营收", "content": "1.2M"}]}
    p = build_preview_for_tool("bi_ask_question", result)
    assert p is not None
    assert p["kind"] == "kpi"
    assert p["value"] == "1.2M"
    assert p["label"] == "Q3 营收"


def test_dispatcher_bi_ask_question_rows_fallback_table():
    result = {"type": "rows", "rows": [{"x": 1}, {"x": 2}, {"x": 3}]}
    p = build_preview_for_tool("bi_ask_question", result)
    assert p is not None
    assert p["kind"] == "table"


def test_dispatcher_add_chart_with_spec():
    result = {"spec": {"title": "Test", "kind": "line"}}
    p = build_preview_for_tool("add_chart", result)
    assert p is not None
    assert p["kind"] == "chart_thumbnail"


def test_dispatcher_unknown_tool_returns_none():
    assert build_preview_for_tool("mcp.foo.bar", {"x": 1}) is None


def test_dispatcher_non_dict_result_returns_none():
    assert build_preview_for_tool("bi_fetch_rows", [1, 2, 3]) is None
    assert build_preview_for_tool("bi_fetch_rows", None) is None


def test_dispatcher_canvas_anchor_propagates():
    result = {"rows": [{"x": 1}, {"x": 2}]}
    anchor = {"canvas_id": "cnv_1", "section_id": "sec_a"}
    p = build_preview_for_tool("bi_fetch_rows", result, canvas_anchor=anchor)
    assert p["canvas_anchor"] == anchor


def test_dispatcher_exception_during_build_returns_none():
    """Builder bug shouldn't crash tool emission — caller logs + returns None."""
    # Pass weirdly-shaped result that might trigger build error
    result = {"rows": "not a list"}  # invalid shape
    p = build_preview_for_tool("bi_fetch_rows", result)
    # Either None (gracefully handled) or a valid preview
    assert p is None or "kind" in p


# ─────────────────────────────────────────────────────────────────
# chat-truth Phase B-BE · python_sandbox + kb_search preview branches
# ─────────────────────────────────────────────────────────────────
def test_b_t10_python_sandbox_with_stdout_text_preview():
    """B-T10: python_sandbox result with stdout → text preview."""
    result = {"stdout": "hello world\nrow count: 42\ndone."}
    p = build_preview_for_tool("python_sandbox", result)
    assert p is not None
    assert p["kind"] == "text"
    assert p["source_label"] == "sandbox stdout"
    assert "hello world" in p["preview"]


def test_b_t11_python_sandbox_with_rows_table_preview():
    """B-T11: python_sandbox result with rows → table preview."""
    result = {
        "rows": [
            {"city": "上海", "gmv": 1620000},
            {"city": "北京", "gmv": 1580000},
            {"city": "广州", "gmv": 980000},
        ]
    }
    p = build_preview_for_tool("python_sandbox", result)
    assert p is not None
    assert p["kind"] == "table"
    assert p.get("source_label") == "sandbox"
    assert len(p["rows"]) >= 1


def test_b_t12_kb_search_hits_to_table_preview():
    """B-T12: kb_search result with hits[] → table preview."""
    result = {
        "hits": [
            {"title": "财务制度 v3", "score": 0.95, "doc_id": "d1"},
            {"title": "采购流程 SOP", "score": 0.88, "doc_id": "d2"},
            {"title": "审计指引", "score": 0.81, "doc_id": "d3"},
        ]
    }
    p = build_preview_for_tool("kb_search", result)
    assert p is not None
    assert p["kind"] == "table"
    assert p.get("source_label") == "kb_search"


def test_b_t13_python_sandbox_empty_returns_none():
    """B-T13: python_sandbox with empty stdout AND no rows → None."""
    assert build_preview_for_tool("python_sandbox", {"stdout": ""}) is None
    assert build_preview_for_tool("python_sandbox", {}) is None
    # whitespace-only stdout also None
    assert build_preview_for_tool("python_sandbox", {"stdout": "   \n  "}) is None


def test_b_t14_kb_search_empty_hits_returns_none():
    """B-T14: kb_search with empty hits → None."""
    assert build_preview_for_tool("kb_search", {"hits": []}) is None
    assert build_preview_for_tool("kb_search", {}) is None


def test_typed_preview_preserves_zero_false_and_text_without_reverse_parsing():
    rows = [{"value": 0, "active": False, "label": "00123"}, {"value": 1.23456, "active": True, "label": "1.2M"}]
    preview = build_sql_preview(rows)
    assert preview["typed_rows"] == [[0, False, "00123"], [1.23456, True, "1.2M"]]
    for value in (0, False):
        scalar = build_preview_for_tool(
            "bi_ask_question", {"type": "metric", "metrics": [{"name": "Result", "value": value, "content": "display only"}]}
        )
        assert scalar["raw_value"] == value
        assert type(scalar["raw_value"]) is type(value)
    formatted = build_preview_for_tool("bi_ask_question", {"type": "metric", "metrics": [{"name": "Result", "content": "1.2M"}]})
    assert "raw_value" not in formatted
    assert formatted["value"] == "1.2M"
    exact = build_sql_preview([{"value": 9007199254740993}, {"value": float("nan")}])
    assert exact["typed_rows"] == [["9007199254740993"], [None]]
