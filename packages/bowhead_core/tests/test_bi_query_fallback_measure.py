"""指标位一个都没认出、引擎自己补了默认指标 ⇒ 那一列是兜底，不交出去（判据见 bi_unknown_words 模块尾）。

116 1.3.47：「2025 年的 IPT 是多少」3 轮里 2 轮把兜底的 2,297,450（营业额按字面「pit」筛出的一小块）
当成客流量 / 营业额代进公式，给出 7,197 元、0.098 元/人次。走真 bi_query handler，只替身 datarc。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_bi_bridge.types import CutResult, CutToken
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

# Actual shape on 116: execution basis for the IPT query.
_IPT_EXPLAIN = {"models": ["门店品牌品类表"], "filters": ["pit"], "measures": ["营业额"], "date_filters": ["2025年1-12月"]}


def _download(columns: list[str], rows: list[dict[str, Any]], explain_json: Any) -> Any:
    d = MagicMock()
    d.rows = rows
    d.columns = columns
    d.total_rows = len(rows)
    d.csv_filename = "rows.csv"
    d.task_uid = "task"
    d.download_ms = 10
    d.explain_json = explain_json
    d.explain_message = None
    d.csv_count = 1
    d.download_errors = []
    return d


class _ArtifactStore:
    async def put_bytes(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id="art_fb", filename=kwargs["filename"])


def _cut(*tokens: tuple[str, str]) -> CutResult:
    return CutResult(tokens=[CutToken(word=w, prop=p, unit_idx=i) for i, (w, p) in enumerate(tokens)])


async def _run(turn_id: str, dl: Any, cut: CutResult, query: str) -> dict:
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(return_value=cut)
    datarc.download_data = AsyncMock(return_value=dl)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,  # ⚠ Unique across files: bi_query has a process-wide Turn-scoped cache.
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )
    return await build_default_registry().get("bi_query").handler({"queries": [query]}, ctx)


def _numbers_anywhere(out: dict) -> str:
    import json

    return json.dumps({k: v for k, v in out.items() if k not in ("_preflight_hint", "_parse", "query_explain")}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_ipt_fallback_value_never_reaches_the_model() -> None:
    """★ 命门：兜底数既不在内联行里，也不能经数据文件进沙箱。

    突变：删掉 bi_query handler 里 `withhold_fallback_measures(...)` 的接线 → 本条必红。
    """
    dl = _download(["营业额 / 元"], [{"营业额 / 元": 2297450.2482638885}], _IPT_EXPLAIN)
    cut = _cut(("2025年1-12月", "dateRange_2025010120251231"), ("IPT", "unknown"))
    out = await _run("t-fb-ipt", dl, cut, "2025年1-12月 IPT")
    assert "2297450" not in _numbers_anywhere(out), "兜底数还在结果里"
    for key in ("artifact_id", "_ref_artifact_id", "_how_to_use_in_sandbox"):
        assert key not in out, f"数据文件引用 {key} 还在——沙箱照样能读到兜底数"
    assert out.get("_withheld_fallback_measures") == ["营业额 / 元"]
    hint = out.get("_preflight_hint") or ""
    assert "「IPT」" in hint and "不要" in hint


@pytest.mark.asyncio
async def test_dimension_columns_are_kept() -> None:
    """「各门店 面积」这类要的可能只是清单——维度列照常返回，只拿掉兜底的那一列。"""
    rows = [{"门店": "东湖街", "营业额 / 元": 1.0}, {"门店": "望湖", "营业额 / 元": 2.0}]
    dl = _download(["门店", "营业额 / 元"], rows, {"measures": ["营业额"], "dimensions": ["门店"]})
    cut = _cut(("门店", "d57"), ("面积", "unknown"))
    out = await _run("t-fb-dims", dl, cut, "各门店 面积")
    assert out.get("columns") == ["门店"]
    assert [r.get("门店") for r in out.get("rows") or []] == ["东湖街", "望湖"]
    assert all("营业额 / 元" not in r for r in out.get("rows") or [])


@pytest.mark.asyncio
async def test_recognized_measure_is_left_alone() -> None:
    """负对照：查询里有被认出的指标（m1）——那一列是用户要的，只是还有别的词没认出。"""
    dl = _download(["营业额 / 元"], [{"营业额 / 元": 165.35}], {"measures": ["营业额"]})
    cut = _cut(("华东地", "unknown"), ("营业额", "m1"))
    out = await _run("t-fb-recognized", dl, cut, "华东地 营业额")
    assert "_withheld_fallback_measures" not in out
    assert out.get("artifact_id") or out.get("_ref_artifact_id")


@pytest.mark.asyncio
async def test_text_only_explain_does_not_assert() -> None:
    """负对照：拿不到结构化口径（没有 measures）就不断言那一列是兜底。"""
    dl = _download(["营业额 / 元"], [{"营业额 / 元": 2297450.0}], None)
    cut = _cut(("2025年", "dateRange_2025010120251231"), ("IPT", "unknown"))
    out = await _run("t-fb-textonly", dl, cut, "2025年 IPT")
    assert "_withheld_fallback_measures" not in out
