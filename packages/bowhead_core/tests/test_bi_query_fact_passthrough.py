"""Preserve per-query evidence through single, batch, and model-view boundaries."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import anyio.lowlevel
import pytest
from bowhead_bi_bridge.types import CutResult, CutToken, DownloadDataResult
from bowhead_core.model_view import project_tool_result
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_TEXT_QUERY = "2026年11月 revenue"
_STRUCTURED_QUERY = "2026年1-12月 revenue 不含"
_TEXT = "Date range: November 2025."
_STRUCTURED = {"measures": ["revenue"], "date_filters": ["January-December 2026; through October 28"], "limit": ["top 40"]}


def _client() -> Any:
    client = MagicMock()
    client.active_downloads = 0
    client.peak_downloads = 0

    async def download_data(*, text: str, **kwargs: Any) -> DownloadDataResult:
        client.active_downloads += 1
        client.peak_downloads = max(client.peak_downloads, client.active_downloads)
        await anyio.lowlevel.checkpoint()
        client.active_downloads -= 1
        is_text = text == _TEXT_QUERY
        return DownloadDataResult(
            columns=["month", "revenue"],
            rows=[{"month": f"2025-{index % 12 + 1:02}", "revenue": index} for index in range(40)],
            total_rows=40,
            csv_filename="2025-11-01_至_30.csv",
            explain_json={} if is_text else _STRUCTURED,
            explain_message=_TEXT if is_text else "Structured report is available.",
            csv_count=2,
            download_errors=[1],
        )

    async def cut_question(*args: Any, **kwargs: Any) -> CutResult:
        return CutResult(tokens=[CutToken(word="revenue", prop="m1", unit_idx=0), CutToken(word="不含", prop="unknown", unit_idx=1)])

    client.download_data = AsyncMock(side_effect=download_data)
    client.cut_question = AsyncMock(side_effect=cut_question)
    client.list_measures = AsyncMock(return_value=[])
    client.list_dimensions = AsyncMock(return_value=[])
    return client


async def _run(client: Any, queries: list[str]) -> Any:
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        active_datarc_project="1",
        member_uid="member",
        user_id="user",
        turn_id=f"t_facts_{uuid4().hex}",
        datarc=client,
    )
    return project_tool_result("bi_query", await build_default_registry().get("bi_query").handler({"queries": queries}, ctx))


@pytest.mark.parametrize("parallel", ["0", "1"], ids=["sequential", "parallel"])
async def test_single_and_batch_preserve_query_facts(monkeypatch: pytest.MonkeyPatch, parallel: str) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", parallel)
    client = _client()
    singles = {query: await _run(client, [query]) for query in (_TEXT_QUERY, _STRUCTURED_QUERY)}
    client.peak_downloads = 0
    batch = await _run(client, [_TEXT_QUERY, _STRUCTURED_QUERY])
    assert client.peak_downloads == (2 if parallel == "1" else 1)
    by_query = {result["query_text"]: result for result in batch["results"]}
    for query, single in singles.items():
        result = by_query[query]
        for field in (
            "columns",
            "query_text",
            "query_explain",
            "query_explain_text",
            "_parse",
            "row_count",
            "total_rows",
            "_partial_data",
            "_rows_limited_by_query",
        ):
            assert result.get(field) == single.get(field), field
        assert result["columns"] == ["month", "revenue"]
        assert result["row_count"] == 40
        assert result["rows_shown"] == len(result["sample_rows"]) == 10
        assert result["_rows_truncated"] is True
        assert result["_partial_data"] is True
        assert result["_parse"]["unknown_words"] == ["不含"]
        assert result["sample_rows"] == single["rows"][:10]
    assert by_query[_TEXT_QUERY]["query_explain_text"] == _TEXT
    assert by_query[_STRUCTURED_QUERY]["query_explain"] == _STRUCTURED
    assert by_query[_STRUCTURED_QUERY]["_rows_limited_by_query"] is True
    assert client.download_data.await_count == 4


async def test_empty_result_retains_the_engine_report_and_query() -> None:
    client = _client()
    client.download_data.return_value = DownloadDataResult(columns=["month", "revenue"], rows=[], total_rows=0, explain_json=_STRUCTURED)
    client.download_data.side_effect = None
    result = await _run(client, [_STRUCTURED_QUERY])
    assert result["row_count"] == result["rows_shown"] == 0
    assert result["query_explain"] == _STRUCTURED
    assert result["query_text"] == _STRUCTURED_QUERY
    assert result["_no_data"] is True
    assert "Datarc returned 0 rows for this query" in result["_no_data_hint"]
    assert "does not establish whether the requested source data exists" in result["_no_data_hint"]
