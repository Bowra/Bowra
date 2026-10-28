"""Sprint chat-v3 Phase B tests — preview top-N heuristic + bi_query dispatcher.

Closes Phase A review:
    P0-A2 — bi_query in preview dispatcher (was falling through)
    P0-Eng-3 — preview row sort by delta col abs (not natural order)
"""

from __future__ import annotations

from bowhead_core.tool_result_preview import (
    _find_delta_col_v3,
    _is_delta_col,
    _pick_top_rows_by_delta,
    build_preview_for_tool,
)


# ─────────────────────────────────────────────────────────────────
# Top-N heuristic
# ─────────────────────────────────────────────────────────────────
def test_delta_col_detection_chinese():
    assert _is_delta_col("营业额同比变化率") is True
    assert _is_delta_col("环比变化率") is True
    assert _is_delta_col("涨跌幅") is True


def test_delta_col_detection_english():
    assert _is_delta_col("delta_pct") is True
    assert _is_delta_col("change_pct") is True
    assert _is_delta_col("ratio") is True
    assert _is_delta_col("revenue_yoy") is True


def test_delta_col_detection_negative():
    assert _is_delta_col("品牌") is False
    assert _is_delta_col("revenue") is False
    assert _is_delta_col("销量") is False


def test_find_delta_col_first_match():
    cols = ["品牌", "销量", "营业额同比变化率"]
    assert _find_delta_col_v3(cols) == "营业额同比变化率"


def test_find_delta_col_none():
    cols = ["品牌", "销量", "数量"]
    assert _find_delta_col_v3(cols) is None


def test_pick_top_rows_by_delta_sorts_desc_abs():
    rows = [
        {"brand": "A", "rate": -0.05},
        {"brand": "B", "rate": -1.28},
        {"brand": "C", "rate": 0.50},
        {"brand": "D", "rate": -0.20},
    ]
    cols = ["brand", "rate"]
    # No delta col → fallback natural order
    result = _pick_top_rows_by_delta(rows, cols, n=3)
    assert result == rows[:3]

    # With "ratio"-named col → sort by abs desc
    cols2 = ["brand", "ratio"]
    rows2 = [
        {"brand": "A", "ratio": -0.05},
        {"brand": "B", "ratio": -1.28},
        {"brand": "C", "ratio": 0.50},
        {"brand": "D", "ratio": -0.20},
    ]
    result2 = _pick_top_rows_by_delta(rows2, cols2, n=3)
    assert [r["brand"] for r in result2] == ["B", "C", "D"]  # 1.28, 0.50, 0.20


def test_pick_top_rows_string_percent_coerced():
    rows = [
        {"brand": "A", "营业额同比变化率": "-1.28%"},
        {"brand": "B", "营业额同比变化率": "-0.50%"},
        {"brand": "C", "营业额同比变化率": "2.00%"},
    ]
    cols = ["brand", "营业额同比变化率"]
    result = _pick_top_rows_by_delta(rows, cols, n=2)
    assert [r["brand"] for r in result] == ["C", "A"]  # 2.00, 1.28


def test_pick_top_rows_empty_rows():
    assert _pick_top_rows_by_delta([], ["x"], n=3) == []


def test_pick_top_rows_falls_back_on_unparseable():
    rows = [
        {"brand": "A", "ratio": "n/a"},
        {"brand": "B", "ratio": "—"},
    ]
    cols = ["brand", "ratio"]
    # Unparseable → 0.0 sort key → natural order (stable)
    result = _pick_top_rows_by_delta(rows, cols, n=2)
    assert len(result) == 2


# ─────────────────────────────────────────────────────────────────
# bi_query dispatcher (P0-A2 close)
# ─────────────────────────────────────────────────────────────────
def test_bi_query_dispatcher_flat_rows():
    """bi_query with `rows` shape → table preview built."""
    result = {
        "rows": [
            {"品牌": "阿迪", "销量": 1000, "同比变化率": -0.0128},
            {"品牌": "耐克", "销量": 900, "同比变化率": -0.0125},
            {"品牌": "欧莱雅", "销量": 800, "同比变化率": -0.0135},
        ],
    }
    preview = build_preview_for_tool("bi_query", result)
    assert preview is not None
    assert preview.get("kind") == "table"
    # top-N sort applied — most negative first
    assert preview["rows"][0][0] == "欧莱雅"  # abs(-1.35) is biggest


def test_bi_query_dispatcher_metric():
    """bi_query with type=metric → kpi preview."""
    result = {
        "type": "metric",
        "metrics": [{"name": "营业额", "content": "1.38B"}],
    }
    preview = build_preview_for_tool("bi_query", result)
    assert preview is not None
    assert preview.get("kind") == "kpi"


def test_bi_query_dispatcher_batch_results():
    """χ batch shape: {results: [...]} → preview from first non-empty sub."""
    result = {
        "results": [
            {"ok": True, "query_text": "q1", "rows": []},  # empty rows
            {
                "ok": True,
                "query_text": "q2",
                "rows": [
                    {"a": 1, "b": 2},
                    {"a": 3, "b": 4},
                ],
            },
        ],
    }
    preview = build_preview_for_tool("bi_query", result)
    assert preview is not None
    assert preview.get("kind") == "table"


def test_bi_query_dispatcher_batch_real_sample_rows_shape():
    """0.16.1 hotfix: real χ backend stores rows in `sample_rows` not `rows`.

    Prior version of dispatcher checked only `rows` → preview always null in
    prod even with valid data. Captured from real prod stream
    t_49ca317e341d payload as ground truth.
    """
    result = {
        "results": [
            {
                "ok": True,
                "query_text": "2026年4月 人工-外包 同比 2025年4月",
                "sample_rows": [
                    {"同比人工-外包": 57103482.91, "人工-外包": 61197310.44, "人工-外包同比变化率": 0.0716914},
                ],
                "_preflight_warnings": [],
            },
            {
                "ok": True,
                "query_text": "2026年4月 成品 同比 2025年4月",
                "sample_rows": [
                    {"同比成品": 228975316.52, "成品": 244902713.18, "成品同比变化率": 0.0695594},
                ],
            },
        ],
    }
    preview = build_preview_for_tool("bi_query", result)
    assert preview is not None, "real prod batch shape (sample_rows) must build preview"
    assert preview.get("kind") == "table"
    # λ-followup #715 fix: aggregate ALL sub-result first rows → 2 sub-results = 2 rows
    assert len(preview["rows"]) == 2


def test_bi_query_dispatcher_flat_sample_rows():
    """Flat (N=1) shape may also use sample_rows."""
    result = {
        "sample_rows": [
            {"营业额": 1000, "同比变化率": -0.05},
        ],
    }
    preview = build_preview_for_tool("bi_query", result)
    assert preview is not None
    assert preview.get("kind") in ("table", "kpi")


def test_bi_query_dispatcher_batch_empty():
    """Batch all-empty → no preview (return None, not crash)."""
    result = {
        "results": [
            {"ok": True, "query_text": "q1", "rows": []},
            {"ok": False, "error_kind": "x"},
        ],
    }
    preview = build_preview_for_tool("bi_query", result)
    assert preview is None


def test_bi_query_dispatcher_unknown_shape():
    """bi_query with neither rows nor metric → None."""
    result = {"unexpected_field": "x"}
    preview = build_preview_for_tool("bi_query", result)
    assert preview is None
