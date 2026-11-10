"""A cleanly parsed filter value that the engine report does not list is named in `_preflight_hint`.

Customer session, 2026-11-10: "中文通用名=菲诺利单抗 每月 销售金额" had no unknown words, the structured report carried
no `filters`, and the unfiltered monthly totals were answered as that product's sales. These cases run the real
bi_query handler with a Datarc stand-in.
"""

from __future__ import annotations

import base64

import pytest
from test_bi_query_fallback_measure import _cut, _download, _run

_VALUE = "菲诺利单抗"
_VALUE_PROP = "value_" + base64.b64encode(_VALUE.encode()).decode()
_QUERY = "中文通用名=菲诺利单抗 每月 销售金额"
_ROWS = [{"每月": "2025年11月", "销售金额": 771233796.72}]
_DATES = ["「年月」的「前12月」（2025-11-01 至 2026-10-31）"]


def _clean_cut(measure_prop: str = "m56"):
    return _cut(
        ("中文通用名", "d186"), ("=", "$eq"), (_VALUE, _VALUE_PROP), (" ", "unknown"), ("每月", "groupByF_monthly"), ("销售金额", measure_prop)
    )


@pytest.mark.asyncio
async def test_parsed_filter_value_missing_from_the_report_is_named() -> None:
    explain = {"models": ["TMS_Latest"], "measures": ["销售金额"], "dimensions": ["每月"], "date_filters": _DATES}
    out = await _run("t-dropped-filter", _download(["每月", "销售金额"], _ROWS, explain), _clean_cut(), _QUERY)
    assert out["_parse"]["unknown_words"] == []
    hint = out["_preflight_hint"]
    assert f"「{_VALUE}」" in hint and "no `filters` or `having` condition" in hint
    # The rows stay available: the tool names the gap and leaves the decision to the caller.
    assert out["rows"] == _ROWS and "_withheld_fallback_measures" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("condition_key", ["filters", "having"])
async def test_reported_condition_adds_no_claim(condition_key: str) -> None:
    explain = {"models": ["CHPA_National"], "measures": ["销售金额"], "dimensions": ["每月"], condition_key: ["中文通用名=菲诺利单抗"]}
    out = await _run(f"t-dropped-filter-{condition_key}", _download(["每月", "销售金额"], _ROWS, explain), _clean_cut(), _QUERY)
    assert "recognized these filter values" not in (out.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_text_only_report_adds_no_claim() -> None:
    """Without a structured report there is no key to check, so nothing is asserted."""
    out = await _run("t-dropped-filter-textonly", _download(["每月", "销售金额"], _ROWS, None), _clean_cut(), _QUERY)
    assert "recognized these filter values" not in (out.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_dimension_bound_value_is_named_once_with_unknown_words() -> None:
    """A `d{id}=value` token counts too, and replaces the generic missing-`filters` sentence."""
    cut = _cut(("上海", "d50=" + base64.b64encode("上海".encode()).decode()), ("华东地", "unknown"), ("营业额", "m1"))
    out = await _run("t-dropped-filter-dimvalue", _download(["营业额"], [{"营业额": 1.0}], {"measures": ["营业额"]}), cut, "上海 华东地 营业额")
    hint = out["_preflight_hint"]
    assert "「华东地」" in hint and "recognized these filter values: 「上海」" in hint
    assert "contains no `filters` field" not in hint
