"""A parsed measure or dimension that Datarc's structured report does not list is named in `_preflight_hint`.

Regression check on 1.7.42 (2026-11-15): 「2026年 女装 月坪效 按种类大类 每月」 returned 女装 revenue under a report that
listed the measure 营业额, and compute took it as the filtered 月坪效; 「2026年 女装 门店面积 每月」 returned monthly revenue
grouped by 每月 only. These cases run the real bi_query handler with a Datarc stand-in.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_bi_bridge.types import DimensionMeta, MeasureMeta
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from test_bi_query_fallback_measure import _ArtifactStore, _cut, _download, _run

_WOMEN = "d10=" + base64.b64encode("女装".encode()).decode()
_DATES = ["「日期」的「2026年」（2026-01-01 至 2026-11-14）"]
_REVENUE_ROWS = [{"每月": "2026年1月", "种类大类": "女装", "营业额 / 元": 796407339.56}]


@pytest.fixture(autouse=True)
def _schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema names for the parse fields; the handler reads them through the shared schema cache."""
    schema = SimpleNamespace(
        measures=[MeasureMeta(id=1, names=["营业额", "销售额"]), MeasureMeta(id=3, names=["月坪效"])],
        dimensions=[DimensionMeta(id=10, names=["种类大类"]), DimensionMeta(id=11, names=["门店面积"])],
    )

    async def _get_schema(**_: object) -> SimpleNamespace:
        return schema

    monkeypatch.setattr("bowhead_core.datarc_schema_cache.get_schema_with_cache", _get_schema)


def _measure_swap_cut():
    return _cut(
        ("2026年 ", "dateRange_2026010120261231"),
        ("女装", _WOMEN),
        ("月坪效", "m3"),
        ("按", "unknown"),
        ("种类大类", "d10"),
        ("每月", "groupByF_monthly"),
    )


_SWAP_EXPLAIN = {
    "models": ["门店品牌品类表"],
    "filters": ["女装"],
    "measures": ["营业额"],
    "dimensions": ["种类大类", "每月"],
    "date_filters": _DATES,
}


@pytest.mark.asyncio
async def test_a_measure_the_report_replaced_is_named() -> None:
    dl = _download(["每月", "种类大类", "营业额 / 元"], _REVENUE_ROWS, _SWAP_EXPLAIN)
    out = await _run("t-unreported-measure", dl, _measure_swap_cut(), "2026年 女装 月坪效 按种类大类 每月")
    # 种类大类 also qualifies the parsed value 女装, so it is a filter here and not reported as a missing grouping.
    assert out["_parse"]["fields_not_in_report"] == {"measures": ["月坪效"]}
    assert "measure 「月坪效」 (executed measures: 「营业额」)" in out["_preflight_hint"]
    # The rows stay available: the tool names the gap and leaves the decision to the caller.
    assert out["rows"] == _REVENUE_ROWS and "_withheld_fallback_measures" not in out


@pytest.mark.asyncio
async def test_each_batched_query_carries_its_own_note() -> None:
    """The 116 case was one query of a batch; each sub-result forwards its parse and hint."""
    datarc_dl = _download(["每月", "种类大类", "营业额 / 元"], _REVENUE_ROWS, _SWAP_EXPLAIN)
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(return_value=_measure_swap_cut())
    datarc.download_data = AsyncMock(return_value=datarc_dl)
    ctx = ToolExecutionContext(
        workspace_id="1", member_uid="m", user_id="u", turn_id="t-unreported-batch", datarc=datarc, artifact_store=_ArtifactStore(), session_id="s1"
    )
    queries = ["2026年 女装 月坪效 按种类大类 每月", "2026年 女装 月坪效 种类大类 每月"]
    out = await build_default_registry().get("bi_query").handler({"queries": queries}, ctx)
    for item in out["results"]:
        assert item["_parse"]["fields_not_in_report"] == {"measures": ["月坪效"]}
        assert "measure 「月坪效」" in item["_preflight_hint"]


@pytest.mark.parametrize(
    ("question", "fields"),
    [
        ("2026年 女装 门店面积 每月", [("门店面积", "d11"), ("每月", "groupByF_monthly")]),
        # 总计 parses as an operator, but no value follows it: 门店面积 is what was to be summed, not a condition.
        ("2026年 女装 门店面积 总计 每月", [("门店面积", "d11"), ("总计", "op_sum"), ("每月", "groupByF_monthly")]),
    ],
)
@pytest.mark.asyncio
async def test_a_dimension_the_report_did_not_group_by_is_named(question: str, fields: list[tuple[str, str]]) -> None:
    explain = {"models": ["门店品牌品类表"], "filters": ["女装"], "measures": ["营业额"], "dimensions": ["每月"], "date_filters": _DATES}
    rows = [{"每月": "2026年1月", "营业额 / 元": 796407339.56}]
    cut = _cut(("2026年 ", "dateRange_2026010120261231"), ("女装", _WOMEN), *fields)
    out = await _run("t-unreported-dimension", _download(["每月", "营业额 / 元"], rows, explain), cut, question)
    assert out["_parse"]["fields_not_in_report"] == {"dimensions": ["门店面积"]}
    hint = out["_preflight_hint"]
    assert "dimension 「门店面积」 (executed grouping: 「每月」)" in hint
    # The parse bound no measure, so nothing is said about where 营业额 came from.
    assert "measure 「" not in hint


@pytest.mark.parametrize(
    ("question", "condition"),
    [
        # The value token carries the dimension as its field.
        ("种类大类=女装 营业额 每月", [("种类大类", "d10"), ("=", "$eq"), ("女装", _WOMEN)]),
        # 116 on 1.7.43: the dimension, an operator, then a value token without a field; the report listed the filter.
        (
            "2026年 种类大类为女装 营业额 每月",
            [("2026年 ", "dateRange_2026010120261231"), ("种类大类", "d10"), ("为", "op_eq"), ("女装", "value_" + _WOMEN[4:])],
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_dimension_word_that_qualifies_a_value_is_a_filter(question: str, condition: list[tuple[str, str]]) -> None:
    """「种类大类为女装」 names the dimension only to scope the value; a report that filtered on it grouped by nothing new."""
    explain = {"models": ["门店品牌品类表"], "filters": ["种类大类为女装"], "measures": ["营业额"], "dimensions": ["每月"], "date_filters": _DATES}
    rows = [{"每月": "2026年1月", "营业额 / 元": 796407339.56}]
    cut = _cut(*condition, (" ", "unknown"), ("营业额", "m1"), (" ", "unknown"), ("每月", "groupByF_monthly"))
    out = await _run("t-unreported-qualifier", _download(["每月", "营业额 / 元"], rows, explain), cut, question)
    assert "fields_not_in_report" not in out["_parse"]
    assert "does not use them" not in (out.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_a_report_spelled_in_another_language_adds_no_claim() -> None:
    """Report values follow the member's account language; without schema spelling there is nothing to compare."""
    explain = {"models": ["Store category"], "measures": ["Revenue"], "dimensions": ["Monthly"], "date_filters": _DATES}
    dl = _download(["每月", "种类大类", "营业额 / 元"], _REVENUE_ROWS, explain)
    out = await _run("t-unreported-english", dl, _measure_swap_cut(), "2026年 女装 月坪效 每月")
    assert "fields_not_in_report" not in out["_parse"]
    assert "does not use them" not in (out.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_a_report_listing_the_parsed_fields_adds_no_claim() -> None:
    explain = {"models": ["门店表"], "measures": ["月坪效"], "dimensions": ["每月"], "date_filters": _DATES}
    cut = _cut(("2026年 ", "dateRange_2026010120261231"), ("月坪效", "m3"), ("每月", "groupByF_monthly"))
    out = await _run(
        "t-unreported-none", _download(["每月", "月坪效"], [{"每月": "2026年1月", "月坪效": 1987.2}], explain), cut, "2026年 月坪效 每月"
    )
    assert "fields_not_in_report" not in out["_parse"]
    assert "does not use them" not in (out.get("_preflight_hint") or "")
