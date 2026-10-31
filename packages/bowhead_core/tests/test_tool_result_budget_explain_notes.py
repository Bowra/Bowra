"""超预算裁剪时，G 的两句事实不能丢（§2.5 没筛过 / §2.6 条数限制）。

裁剪层按字段重新生成必须保留的几句（不解析提示字符串）；这两句「漏了就是数字错」，
恰恰不能在数据大、提示被压缩时一起被删掉。判据与 handler 共用 `engine_applied_no_filter`，
不各写一份。
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_result_budget import DEFAULT_CAPS_TOKEN, cap_result


@pytest.fixture(autouse=True)
def _bi_query_budget_of_1500(monkeypatch: pytest.MonkeyPatch) -> None:
    """These cases exercise the trimming order, so they pin a small budget; the default is covered separately."""
    monkeypatch.setitem(DEFAULT_CAPS_TOKEN, "bi_query", 1500)


def _over_budget(**extra: object) -> dict:
    r: dict = {
        "columns": ["市", "营业额 / 元"],
        "rows": [{"市": f"区{i}", "营业额 / 元": i} for i in range(200)],
        "row_count": 200,
        "rows_shown": 200,
        "total_rows": 200,
        "artifact_id": "art_1",
        "_preflight_hint": "其他说明" * 3000,  # Force the "still over budget after clipping rows → retain only essential sentences" branch.
    }
    r.update(extra)
    return r


@pytest.mark.unit
def test_unfiltered_fact_survives_the_cap() -> None:
    r = _over_budget(_parse={"unknown_words": ["华东地"]}, query_explain={"models": ["门店表"], "measures": ["营业额"]})
    out, _ = cap_result("bi_query", r)
    hint = out["_preflight_hint"]
    assert "contains no `filters` field" in hint and "「华东地」" in hint
    assert "其他说明" not in hint


@pytest.mark.unit
def test_filtered_result_keeps_no_unfiltered_claim_after_the_cap() -> None:
    """正对照：真筛了的结果，裁剪层也不许凭空补一句「没筛过」。"""
    r = _over_budget(_parse={"unknown_words": ["华东地"]}, query_explain={"models": ["门店表"], "filters": ["上海"]})
    out, _ = cap_result("bi_query", r)
    assert "contains no `filters` field" not in out["_preflight_hint"]


@pytest.mark.unit
def test_query_limit_note_survives_the_cap() -> None:
    out, _ = cap_result("bi_query", _over_budget(_rows_limited_by_query=True))
    assert "条数限制" in out["_preflight_hint"]
