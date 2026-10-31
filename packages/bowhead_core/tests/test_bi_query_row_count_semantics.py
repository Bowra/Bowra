"""bi-answer-correctness C（2026-09-15）：bi_query 的行数语义。

`row_count` = 全量行数（与 artifact / 图表 spec / 沙箱 INPUTS 同义），`rows_shown` = 实际给模型看的行数。
116 实测（q6 `t_c806c2d14a59`）：旧实现 `row_count=len(inline rows)=30`，模型写出「共 30 个种类大类」，实际 38 个。
方案：`docs/sprints/bi-answer-correctness-2026-09-15/c-truncated-row-count.md`。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

_COLS = ["种类大类", "营业额 / 元"]


def _download(rows: list[dict[str, Any]]) -> Any:
    d = MagicMock()
    d.rows = rows
    d.columns = list(_COLS)
    d.total_rows = len(rows)
    d.csv_filename = "rows.csv"
    d.task_uid = "task"
    d.download_ms = 10
    return d


def _categories(n: int) -> list[dict[str, Any]]:
    return [{"种类大类": f"品类{i}", "营业额 / 元": 1000 - i} for i in range(n)]


class _ArtifactStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def put_bytes(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"art_{len(self.calls)}", filename=kwargs["filename"])


def _ctx(turn_id: str, download_data: AsyncMock) -> tuple[ToolExecutionContext, _ArtifactStore]:
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut unavailable"))
    datarc.download_data = download_data
    store = _ArtifactStore()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        # ⚠ turn_id must be unique across files: bi_query has a process-wide turn-scoped cache.
        turn_id=turn_id,
        datarc=datarc,
        artifact_store=store,
        session_id="s1",
    )
    return ctx, store


async def _run(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    return await build_default_registry().get("bi_query").handler(args, ctx)


@pytest.mark.asyncio
async def test_truncated_result_reports_full_row_count_and_rows_shown() -> None:
    ctx, store = _ctx("t-rowcount-trunc", AsyncMock(return_value=_download(_categories(38))))
    out = await _run({"queries": ["2026年 各种类大类 营业额"]}, ctx)

    assert len(out["rows"]) == 30
    assert out["rows_shown"] == 30
    assert out["row_count"] == 38, "row_count 必须是全量，不是给模型看的行数"
    assert out["total_rows"] == 38
    assert out["_rows_truncated"] is True
    assert out["_rows_full_count"] == 38
    assert "`rows_shown` is the displayed count" in out["_preflight_hint"]
    assert "`row_count` counts all returned rows" in out["_preflight_hint"]
    assert store.calls[0]["row_count"] == 38, "artifact 与结果的全量行数必须同源"


@pytest.mark.asyncio
async def test_limit_is_also_a_partial_view() -> None:
    ctx, _ = _ctx("t-rowcount-limit", AsyncMock(return_value=_download(_categories(38))))
    out = await _run({"queries": ["2026年 各种类大类 营业额"], "limit": 10}, ctx)

    assert out["rows_shown"] == 10
    assert out["row_count"] == 38
    assert out["_rows_truncated"] is True
    assert out["_rows_full_count"] == 38


@pytest.mark.asyncio
async def test_small_result_is_complete() -> None:
    ctx, _ = _ctx("t-rowcount-small", AsyncMock(return_value=_download(_categories(5))))
    out = await _run({"queries": ["2026年 各种类大类 营业额"]}, ctx)

    assert out["row_count"] == 5
    assert out["rows_shown"] == 5
    assert "_rows_truncated" not in out


@pytest.mark.asyncio
async def test_repeated_header_rows_are_not_data_rows() -> None:
    rows = [{"种类大类": "种类大类", "营业额 / 元": "营业额 / 元"}, *_categories(3)]
    ctx, store = _ctx("t-rowcount-header", AsyncMock(return_value=_download(rows)))
    out = await _run({"queries": ["2026年 各种类大类 营业额"]}, ctx)

    assert out["row_count"] == 3
    assert out["rows_shown"] == 3
    assert all(r["种类大类"] != "种类大类" for r in out["rows"])
    assert store.calls[0]["row_count"] == 3
    assert "_rows_truncated" not in out


@pytest.mark.asyncio
async def test_batch_sub_results_carry_row_counts() -> None:
    big, small = _download(_categories(38)), _download(_categories(4))

    async def _by_text(*args: Any, **kwargs: Any) -> Any:
        text = str(kwargs.get("text") or (args[2] if len(args) > 2 else ""))
        return big if "种类大类" in text else small

    ctx, _ = _ctx("t-rowcount-batch", AsyncMock(side_effect=_by_text))
    out = await _run({"queries": ["2026年 各种类大类 营业额", "2026年 各省 营业额"]}, ctx)

    subs = {s["query_text"]: s for s in out["results"]}
    big_sub, small_sub = subs["2026年 各种类大类 营业额"], subs["2026年 各省 营业额"]
    assert big_sub["row_count"] == 38
    assert big_sub["rows_shown"] == len(big_sub["sample_rows"]) == 10
    assert big_sub["_rows_truncated"] is True
    assert small_sub["row_count"] == 4
    assert small_sub["rows_shown"] == 4
    assert "_rows_truncated" not in small_sub
