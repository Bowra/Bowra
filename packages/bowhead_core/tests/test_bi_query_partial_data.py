"""datarc 把结果拆成多个数据项时，别让模型以为拿到的就是全部。

taskType=22 一个问题可能生成多个 CSV（打在同一个 ZIP 里），`result.errors` 还会点名
哪几项下载失败（失败项不在 ZIP 里）。客户端**只读第一个 CSV**——这本身是既有行为，
但以前连「还有几份没读」都不说，模型和用户都无从知道数据不完整。

这里钉的是「说出来」：多文件或有失败项时，结果带 `_partial_data` 且 `_preflight_hint` 里写明。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry


def _download(*, csv_count: Any = 1, download_errors: Any = None) -> Any:
    d = MagicMock()
    d.rows = [{"品牌": "A", "营业额 / 元": 1}]
    d.columns = ["品牌", "营业额 / 元"]
    d.total_rows = 1
    d.csv_filename = "rows.csv"
    d.task_uid = "task"
    d.download_ms = 10
    d.explain_json = {}
    d.explain_message = ""
    d.csv_count = csv_count
    d.download_errors = download_errors if download_errors is not None else []
    return d


class _ArtifactStore:
    async def put_bytes(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id="art_1", filename=kwargs["filename"])


def _ctx(turn_id: str, dl: Any) -> ToolExecutionContext:
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut unavailable"))
    datarc.download_data = AsyncMock(return_value=dl)
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,  # ⚠ Unique across files: bi_query has a process-wide, turn-scoped cache.
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )


async def _run(ctx: ToolExecutionContext) -> Any:
    return await build_default_registry().get("bi_query").handler({"queries": ["2026年 各品牌 营业额"]}, ctx)


@pytest.mark.asyncio
async def test_multiple_data_files_are_called_out() -> None:
    out = await _run(_ctx("t-partial-multicsv", _download(csv_count=3)))
    assert out["_partial_data"] is True
    hint = out.get("_preflight_hint") or ""
    assert "3 个数据文件" in hint and "只读了第一个" in hint


@pytest.mark.asyncio
async def test_failed_data_items_are_called_out() -> None:
    out = await _run(_ctx("t-partial-errors", _download(csv_count=1, download_errors=[1, 2])))
    assert out["_partial_data"] is True
    hint = out.get("_preflight_hint") or ""
    assert "2 个数据项下载失败" in hint


@pytest.mark.asyncio
async def test_complete_result_says_nothing() -> None:
    """正对照：单文件、无失败项时不许出现这条提醒——天天响的提醒等于没有提醒。"""
    out = await _run(_ctx("t-partial-clean", _download(csv_count=1)))
    assert "_partial_data" not in out
    assert "数据不完整" not in (out.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_missing_fields_are_treated_as_complete() -> None:
    """旧客户端 / stub 不带这两个字段时，不能凭空报「数据不完整」。"""
    dl = _download()
    del dl.csv_count
    del dl.download_errors
    out = await _run(_ctx("t-partial-absent", dl))
    assert "_partial_data" not in out
