"""行级取数落 CSV artifact 时，重复表头行必须被丢掉。

2026-09-05（tool-inventory）：入口从 `bi_fetch_rows` 换成 `bi_query`。行为没变，
变的是 `bi_fetch_rows` 已连注册点一起退役——`_fetch_rows` 现在只是 `bi_query` 的
内部实现。bi_query 单查询保持 flat shape backward-compat 且经
`_normalize_bi_query_args` 收 `text=`，所以断言面不变。
"""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    _drop_repeated_csv_header_rows,
    build_default_registry,
)


def test_drop_repeated_csv_header_rows_keeps_real_rows() -> None:
    rows = [
        {"city": "city", "gmv": "gmv"},
        {"city": "上海", "gmv": 100},
        {"city": "city", "gmv": 10},
    ]

    assert _drop_repeated_csv_header_rows(rows, ["city", "gmv"]) == [
        {"city": "上海", "gmv": 100},
        {"city": "city", "gmv": 10},
    ]


@pytest.mark.asyncio
async def test_bi_fetch_rows_csv_artifact_drops_repeated_header_row() -> None:
    captured: dict[str, Any] = {}

    class _ArtifactStore:
        async def put_bytes(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="art_rows", filename=kwargs["filename"])

    fake_download = MagicMock()
    fake_download.rows = [
        {"city": "city", "gmv": "gmv"},
        {"city": "上海", "gmv": 100},
    ]
    fake_download.columns = ["city", "gmv"]
    fake_download.total_rows = 2
    fake_download.csv_filename = "rows.csv"
    fake_download.task_uid = "task_1"
    fake_download.download_ms = 10

    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=fake_download)

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        # ⚠ turn_id must be unique across files: bi_query has a **process-wide** Turn-scoped cache
        # (same Turn + query reuses results); sharing "t" passes alone but fails in combined runs.
        turn_id="t-csv-artifact",
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )

    result = (
        await build_default_registry()
        .get("bi_query")
        .handler(
            {"text": "2026年营业额 按城市"},
            ctx,
        )
    )

    assert result["artifact_id"] == "art_rows"
    assert result["chart_catalog"]["artifact_rows"] == 1
    assert result["chart_catalog"]["candidates"][0]["observation_keys"] == ["city"]
    assert captured["session_id"] == "s1"
    assert captured["created_by_turn"] == "t-csv-artifact"
    content = captured["content"].decode("utf-8")
    assert content.count("city,gmv") == 1
    parsed = list(csv.DictReader(io.StringIO(content)))
    assert parsed == [{"city": "上海", "gmv": "100"}]
