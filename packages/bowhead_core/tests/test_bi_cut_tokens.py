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


def test_model_view_shows_what_ran_instead_of_the_cut() -> None:
    """116 on 1.7.43: the cut bound 女装 to 种类大类 while the executed query on 门店表 applied no filter.

    The cut is Datarc's separate word-level parse, made before a model is chosen, so the model reads only what ran.
    """
    report = {"models": ["门店表"], "measures": ["月坪效"], "dimensions": ["每月"]}
    result = {
        "rows": [],
        "_parse": {
            "tokens": [{"word": "女装", "kind": "dimension_value", "field": "种类大类", "value": "女装"}],
            "unknown_words": [],
            "half_match_values": [],
            "fields_not_in_report": {"dimensions": ["种类大类"]},
        },
        "query_explain": report,
        "_preflight_hint": "note",
        "_debug_cut_props": [{"word": "女装", "prop": "d55=5aWz6KOF"}],
        "_debug_task_uid": "task-1",
    }
    batch = {"results": [dict(result)]}
    for shown in (model_view.project_tool_result("bi_query", result), model_view.project_tool_result("bi_query", batch)["results"][0]):
        assert "_debug_cut_props" not in shown and "_debug_task_uid" not in shown
        assert shown["_parse"] == {"unknown_words": [], "half_match_values": []}
        assert list(shown)[:2] == ["_preflight_hint", "query_explain"]
        assert shown["query_explain"] == {**report, "filters": []}
    # The stored result keeps the cut and the verbatim report: the hint, the trimmer and the audit read them.
    assert result["_parse"]["tokens"][0]["field"] == "种类大类"
    assert result["query_explain"] == report
    assert result["_debug_cut_props"] == [{"word": "女装", "prop": "d55=5aWz6KOF"}]


@pytest.mark.parametrize(
    ("report_key", "report"),
    [
        # A filter that took effect is shown as reported.
        ("query_explain", {"models": ["门店品牌品类表"], "filters": ["种类大类为女装"], "measures": ["营业额"]}),
        # Instances that return only the prose sentence keep it as reported, ahead of the rows.
        ("query_explain_text", "生效模型为「门店表」；查询指标为「月坪效」。"),
    ],
)
def test_model_view_leads_with_the_report_as_reported(report_key: str, report: object) -> None:
    result = {"rows": [{"每月": "2026年1月"}], "query_text": "q", report_key: report}
    shown = model_view.project_tool_result("bi_query", result)
    assert next(iter(shown)) == report_key
    assert shown[report_key] == report
