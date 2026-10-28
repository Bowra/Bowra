"""PyCell 渲染单测."""

from __future__ import annotations

from bowhead_core.pycell import render_code, render_output


def test_render_code_simple() -> None:
    s = render_code("bi_ask_question", {"text": "今年 GMV", "workspace_id": "1"})
    assert s == 'bi_ask_question(text="今年 GMV", workspace_id="1")'


def test_render_code_truncates_long_string() -> None:
    long = "a" * 400
    s = render_code("bi_ask_question", {"text": long})
    assert "…" in s
    assert len(s) < 200


def test_render_output_ok() -> None:
    s = render_output({"type": "metric", "value": 100}, None)
    assert '"metric"' in s
    assert "100" in s


def test_render_output_error() -> None:
    s = render_output(None, "boom")
    assert s.startswith("# ERROR: boom")


def test_render_output_truncates() -> None:
    big = {"data": ["x" * 100] * 100}
    s = render_output(big, None)
    assert "truncated" in s
    assert len(s) < 1700


def test_render_output_tabular_rows() -> None:
    """M 层: dict.rows = list[dict] → df shape + head(3) 视图."""
    result = {
        "rows": [
            {"region": "华北", "gmv": 1200, "share": 0.32},
            {"region": "华东", "gmv": 980, "share": 0.26},
            {"region": "华南", "gmv": 760, "share": 0.20},
            {"region": "西南", "gmv": 410, "share": 0.11},
        ]
    }
    s = render_output(result, None)
    assert "DataFrame shape=(4, 3)" in s
    assert "region" in s and "gmv" in s and "share" in s
    assert "华北" in s and "华东" in s and "华南" in s
    # 4 rows; display only the first 3 and indicate +1 remaining row at the end.
    assert "+1 行" in s


def test_render_output_tabular_flat_list() -> None:
    """扁平 list[dict] 也走 table 路径."""
    result = [
        {"name": "GMV", "value": 1200},
        {"name": "DAU", "value": 50000},
    ]
    s = render_output(result, None)
    assert "shape=(2, 2)" in s


def test_render_output_metric_dict_falls_back_to_json() -> None:
    """非 tabular 形态 (单 metric dict) 仍走原 JSON dump 路径."""
    s = render_output({"type": "metric", "value": 100}, None)
    assert '"metric"' in s and "100" in s
    assert "DataFrame" not in s


def test_render_output_empty_rows_falls_back() -> None:
    """rows 是空 list 时不进 table 路径."""
    s = render_output({"rows": []}, None)
    assert "DataFrame" not in s
