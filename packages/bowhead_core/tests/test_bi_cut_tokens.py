"""Model-facing `_parse.tokens` carry field names, never Datarc ids."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from bowhead_core.bi_cut_tokens import describe_token, field_names

from bowhead_core import model_view

_MEASURES = {1: "营业额"}
_DIMENSIONS = {50: "省", 54: "品牌"}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    ("word", "prop", "expected"),
    [
        # Props measured on demo.datarc.cn /data/cut (2026-10-20).
        ("销售额", "m1", {"word": "销售额", "kind": "measure", "field": "营业额"}),
        ("品牌", "d54", {"word": "品牌", "kind": "dimension", "field": "品牌"}),
        ("上海", f"d50={_b64('上海')}", {"word": "上海", "kind": "dimension_value", "field": "省", "value": "上海"}),
        ("日期", "t1", {"word": "日期", "kind": "time_field"}),
        ("2024年", "dateRange_2024010120241231", {"word": "2024年", "kind": "time_range"}),
        ("上个月", "dateBefore_1M", {"word": "上个月", "kind": "time_range"}),
        ("数量", "op_count", {"word": "数量", "kind": "operator"}),
        ("包含", "$like", {"word": "包含", "kind": "operator"}),
        ("前10", "limit_10", {"word": "前10", "kind": "limit"}),
        ("阿迪的", f"value_{_b64('阿迪的')}", {"word": "阿迪的", "kind": "value", "value": "阿迪的"}),
        ("各", "unknown", {"word": "各", "kind": "unknown"}),
        # An id missing from the schema keeps its kind and omits the field rather than showing the id.
        ("库存", "m999", {"word": "库存", "kind": "measure"}),
        ("坏值", "d50=%%%", {"word": "坏值", "kind": "dimension_value", "field": "省"}),
    ],
)
def test_describe_token(word, prop, expected) -> None:
    assert describe_token(word, prop, measures=_MEASURES, dimensions=_DIMENSIONS) == expected


def test_field_names_uses_primary_name() -> None:
    items = [SimpleNamespace(id=1, names=["营业额", "销售额"]), SimpleNamespace(id=2, names=[])]
    assert field_names(items) == {1: "营业额"}


def test_model_view_hides_raw_cut_props_and_task_uid() -> None:
    result = {
        "rows": [],
        "_parse": {"tokens": [{"word": "销售额", "kind": "measure", "field": "营业额"}]},
        "_debug_cut_props": [{"word": "销售额", "prop": "m1"}],
        "_debug_task_uid": "task-1",
    }
    batch = {"results": [dict(result)]}
    for shown in (model_view.project_tool_result("bi_query", result), model_view.project_tool_result("bi_query", batch)["results"][0]):
        assert "_debug_cut_props" not in shown and "_debug_task_uid" not in shown
        assert shown["_parse"] == result["_parse"]
    # The stored result keeps the raw props for audit.
    assert result["_debug_cut_props"] == [{"word": "销售额", "prop": "m1"}]
