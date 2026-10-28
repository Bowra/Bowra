"""bi-hitl-cleanup P13/P13.5 — bi_query cut transparency + 砍 ask_question 路径.

P13: `_query_single` 同时 cut + fetch (并行), inject `_parse` 到 result.
P13.5: 砍 fast path 统一 _fetch_rows, _ask_question dead code.

Tests:
- P13-1: source has `_cut_safe` helper + asyncio.gather(_cut_safe, fetch_coro)
- P13-2: source injects `_parse` field with tokens/unknown_words/half_match_values
- P13-3: graceful fail — cut Exception 不 fail bi_query (omit _parse)
- P13.5-1: source no longer has `if fast: ... else:` branch in _query_single
- P13.5-2: tool description mentions `_parse`
- P13.5-3: compute.md mentions `_parse.unknown_words`
- P13 runtime: cut_question called → _parse injected (mocked datarc)
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_REGISTRY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "tool_registry.py"
COMPUTE_MD = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "prompts" / "system" / "compute.md"


# ── P13: cut wire in _query_single ─────────────────────────────


def test_p13_query_single_has_cut_safe_helper() -> None:
    """_query_single defines `_cut_safe` inner that calls ctx.datarc.cut_question."""
    src = TOOL_REGISTRY.read_text()
    # match in the _query_single function body
    assert "_cut_safe" in src
    assert "ctx.datarc.cut_question" in src
    # Function definition pattern
    assert re.search(r"async def _cut_safe\b", src), "P13: _cut_safe helper inner func missing"


def test_p13_uses_gather_to_parallelize_cut_and_fetch() -> None:
    """cut + fetch run via asyncio.gather (parallel, not sequential)."""
    src = TOOL_REGISTRY.read_text()
    assert "asyncio_p13.gather(_cut_safe(), fetch_coro, _field_names_safe())" in src, "P13: cut + fetch must be parallelized via asyncio.gather"


def test_p13_injects_parse_field_with_three_subfields() -> None:
    """Result dict gets `_parse` with tokens / unknown_words / half_match_values."""
    src = TOOL_REGISTRY.read_text()
    assert 'result["_parse"]' in src
    assert "unknown_words" in src
    assert "half_match_values" in src
    assert "tokens" in src
    # Verify it uses CutResult helpers (not raw token iteration)
    assert "cut_result.unknown_tokens()" in src
    assert "cut_result.value_without_dim()" in src


def test_p13_cut_failure_is_graceful() -> None:
    """`_cut_safe` catches Exception → returns None (P13.6: + metric + log).

    P13.6 (2026-05-21): bare `except Exception` 默默 swallow 全部 → 51/52 prod
    call 静默无 `_parse`。fix: 分类 TimeoutError / Exception, emit metric +
    log.warning 让信号可见. `return None` 现在在 _cut_safe 末尾 (failure path).
    """
    src = TOOL_REGISTRY.read_text()
    idx = src.find("async def _cut_safe")
    assert idx != -1, "P13: _cut_safe missing"
    # widen window: P13.6 added a metric + log, lengthening the body
    body = src[idx : idx + 1800]
    assert "try:" in body and "except" in body, "P13: _cut_safe must wrap cut call in try/except (graceful fail)"
    # P13.6 invariants: timeout classification + metric + log
    assert "TimeoutError" in body, "P13.6: must classify asyncio.TimeoutError"
    assert "bowhead_bi_query_cut_failed_total" in body, "P13.6: must emit failure metric (silent fail visibility)"
    assert "warning" in body and "bowhead.bi_query" in body, "P13.6: must log.warning so silent fails become visible"
    # Failure path returns None to keep bi_query non-failing
    assert "return None" in body


def test_p13_metric_emitted_on_inject() -> None:
    """Telemetry counter `bowhead_bi_query_cut_injected_total` increments per injection."""
    src = TOOL_REGISTRY.read_text()
    assert "bowhead_bi_query_cut_injected_total" in src


# ── P13.5: remove fast path ─────────────────────────────────────────


# lead-analyst-logic batch 2: `_query_single` split into a wrapper (cache + single-flight) and
# `_query_single_fetch` (actual data fetch). P13.5's two guards (no fast branch / direct _fetch_rows call)
# now span both functions; scan through the χ-3 batch helpers marker to cover both.
_QUERY_SINGLE_REGION = r"async def _query_single\(.*?\) -> Any:\s*(.*?)\n    \# ─── χ-3"


def test_p135_no_fast_else_branch_in_query_single() -> None:
    """`_query_single`(+fetch 拆分体) no longer has `if fast: ... else:` branch — always _fetch_rows."""
    src = TOOL_REGISTRY.read_text()
    m = re.search(_QUERY_SINGLE_REGION, src, re.DOTALL)
    assert m, "_query_single body not found"
    body = m.group(1)
    # No conditional fast-vs-not-fast routing left
    # (The `if fast:` ... `else: fetch_coro = _fetch_rows(...)` pattern is gone)
    assert "if fast:\n            fetch_coro = _ask_question(" not in body, "P13.5: fast branch should be cut; always _fetch_rows"
    # Direct call to _fetch_rows
    assert "fetch_coro = _fetch_rows(" in body


def test_p135_query_single_does_not_call_ask_question_directly() -> None:
    """Single-query path no longer dispatches to _ask_question (dead code, removed next ship)."""
    src = TOOL_REGISTRY.read_text()
    m = re.search(_QUERY_SINGLE_REGION, src, re.DOTALL)
    assert m
    body = m.group(1)
    # _ask_question dispatch removed from _query_single
    assert "_ask_question(" not in body, "P13.5: _ask_question dispatch should be removed from _query_single"


# ── Prompt + tool description ─────────────────────────────────


def test_p13_tool_description_documents_parse() -> None:
    """LLM-facing bi_query description mentions `_parse` + unknown_words guidance."""
    src = TOOL_REGISTRY.read_text()
    assert "_parse" in src
    # Description block (between description=( ... and ) close)
    # Just check both strings appear near each other
    assert "unknown_words" in src
    # cut transparency narrative
    assert "P13" in src or "cut transparency" in src


def test_p13_compute_md_documents_parse_bail_rule() -> None:
    """compute.md says: `_parse.unknown_words` 不空 → bail."""
    md = COMPUTE_MD.read_text()
    assert "_parse" in md
    assert "unknown_words" in md
    assert "half_match_values" in md
    # Rule: bail when unknown_words present
    assert ("不空" in md and "bail" in md.lower()) or "domain_mismatch" in md


# ── Runtime: cut injection via mocked datarc ──────────────────


@pytest.mark.asyncio
async def test_p13_runtime_cut_injected_via_mocked_datarc() -> None:
    """End-to-end: mock ctx.datarc with cut_question returning unknown token → result
    has _parse with that token in unknown_words. (Direct _query_single call via
    full tool_registry — tests the wire end-to-end.)"""
    from bowhead_bi_bridge.types import CutResult, CutToken, MeasureMeta
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        build_default_registry,
    )

    # Mock datarc client: cut returns "国美店" as unknown; download returns 1 row.
    fake_cut = CutResult(
        tokens=[
            CutToken(word="国美店", prop="unknown", unit_idx=0),
            CutToken(word="本月", prop="dateRange_20260501-20260531", unit_idx=1),
            CutToken(word="销售", prop="m1", unit_idx=2),
        ]
    )
    fake_download = MagicMock()
    fake_download.rows = [{"营业额 / 元": 868034553}]
    fake_download.columns = ["营业额 / 元"]
    fake_download.total_rows = 1
    fake_download.csv_filename = "国美.csv"
    fake_download.task_uid = "uid_xx"
    fake_download.download_ms = 200

    datarc = MagicMock()
    datarc.cut_question = AsyncMock(return_value=fake_cut)
    datarc.download_data = AsyncMock(return_value=fake_download)
    datarc.list_measures = AsyncMock(return_value=[MeasureMeta(id=1, names=["营业额", "销售"])])
    datarc.list_dimensions = AsyncMock(return_value=[])

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_p13",
        datarc=datarc,
    )
    spec = build_default_registry().get("bi_query")

    result = await spec.handler({"queries": ["国美店本月销售"]}, ctx)
    # N=1 → flat shape
    assert "results" not in result
    # _parse injected
    assert "_parse" in result, f"P13: _parse missing from result keys={list(result)}"
    parse = result["_parse"]
    assert parse["unknown_words"] == ["国美店"]
    assert parse["half_match_values"] == []
    # Names, not Datarc ids: the model never sees `m1`; raw props stay in the hidden audit field.
    assert parse["tokens"] == [
        {"word": "国美店", "kind": "unknown"},
        {"word": "本月", "kind": "time_range"},
        {"word": "销售", "kind": "measure", "field": "营业额"},
    ]
    assert [p["prop"] for p in result["_debug_cut_props"]] == ["unknown", "dateRange_20260501-20260531", "m1"]


@pytest.mark.asyncio
async def test_p13_runtime_cut_forwarded_through_batch_wrap() -> None:
    """N=2 batch — _wrap_sub_result must forward `_parse` from sub to wrapped.
    Found at dogfood 2026-05-21: original P13 wire injected _parse on _query_single
    result but batch path's _wrap_sub_result whitelist-stripped it. Fix adds
    `_parse` forward."""
    from bowhead_bi_bridge.types import CutResult, CutToken
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        build_default_registry,
    )

    fake_cut_a = CutResult(
        tokens=[
            CutToken(word="国美店", prop="unknown", unit_idx=0),
        ]
    )
    fake_cut_b = CutResult(
        tokens=[
            CutToken(word="本月", prop="dateRange_20260501-20260531", unit_idx=0),
            CutToken(word="销售", prop="m1", unit_idx=1),
        ]
    )
    fake_download = MagicMock()
    fake_download.rows = [{"x": 1}]
    fake_download.columns = ["x"]
    fake_download.total_rows = 1
    fake_download.csv_filename = "f.csv"
    fake_download.task_uid = "uid"
    fake_download.download_ms = 50

    datarc = MagicMock()
    # Each call returns different cut
    cuts = [fake_cut_a, fake_cut_b]
    cut_idx = {"i": 0}

    async def cut_side_effect(*args, **kwargs):
        c = cuts[cut_idx["i"] % len(cuts)]
        cut_idx["i"] += 1
        return c

    datarc.cut_question = AsyncMock(side_effect=cut_side_effect)
    datarc.download_data = AsyncMock(return_value=fake_download)

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_p13_batch",
        datarc=datarc,
    )
    spec = build_default_registry().get("bi_query")

    result = await spec.handler(
        {"queries": ["国美店本月销售", "今年GMV同比"]},
        ctx,
    )
    # N=2 → batch shape: {results: [...]}
    assert "results" in result
    assert len(result["results"]) == 2
    # Each sub-entry must have _parse (forwarded through _wrap_sub_result)
    for sub in result["results"]:
        assert "_parse" in sub, f"P13 batch fwd: sub missing _parse, keys={list(sub)}"
        assert "tokens" in sub["_parse"]
    # First query saw "国美店" unknown
    assert "国美店" in result["results"][0]["_parse"]["unknown_words"]
    # bi-answer-correctness A: batched subresults also include the backend's phrase naming the unknown word.
    assert "「国美店」" in (result["results"][0].get("_preflight_hint") or "")
    # Second query: no unknown
    assert result["results"][1]["_parse"]["unknown_words"] == []


@pytest.mark.asyncio
async def test_unknown_words_are_denoised_and_named_in_preflight_hint() -> None:
    """bi-answer-correctness A 步骤 1/2（116 q2）：空格不算没认出；真没认出的词由后端点名进 `_preflight_hint`。"""
    from bowhead_bi_bridge.types import CutResult, CutToken
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    def _download() -> MagicMock:
        d = MagicMock()
        d.rows = [{"营业额 / 元": 1}]
        d.columns = ["营业额 / 元"]
        d.total_rows = 1
        d.csv_filename = "f.csv"
        d.task_uid = "uid"
        d.download_ms = 10
        return d

    async def run(tokens: list[CutToken], turn_id: str) -> dict:
        datarc = MagicMock()
        datarc.cut_question = AsyncMock(return_value=CutResult(tokens=tokens))
        datarc.download_data = AsyncMock(return_value=_download())
        ctx = ToolExecutionContext(workspace_id="1", member_uid="m", user_id="u", turn_id=turn_id, datarc=datarc)
        return await build_default_registry().get("bi_query").handler({"queries": ["华东地区 2026年 营业额 每月"]}, ctx)

    noisy = await run(
        [
            CutToken(word="华东地", prop="unknown", unit_idx=0),
            CutToken(word=" ", prop="unknown", unit_idx=1),
            CutToken(word="区", prop="d48", unit_idx=2),
        ],
        "t_a_unknown_named",
    )
    assert noisy["_parse"]["unknown_words"] == ["华东地"]
    assert len(noisy["_parse"]["tokens"]) == 3, "tokens 原样保留"
    assert "「华东地」" in (noisy.get("_preflight_hint") or "")

    blank_only = await run(
        [CutToken(word=" ", prop="unknown", unit_idx=0), CutToken(word="营业额", prop="m1", unit_idx=1)],
        "t_a_unknown_blank",
    )
    assert blank_only["_parse"]["unknown_words"] == []
    assert "没认出" not in (blank_only.get("_preflight_hint") or "")


@pytest.mark.asyncio
async def test_p13_runtime_cut_failure_omits_parse_graceful() -> None:
    """If cut_question raises, _parse omitted but rows still returned (graceful)."""
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        build_default_registry,
    )

    fake_download = MagicMock()
    fake_download.rows = [{"x": 1}]
    fake_download.columns = ["x"]
    fake_download.total_rows = 1
    fake_download.csv_filename = "f.csv"
    fake_download.task_uid = "uid"
    fake_download.download_ms = 50

    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut fail"))
    datarc.download_data = AsyncMock(return_value=fake_download)

    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_p13_fail",
        datarc=datarc,
    )
    spec = build_default_registry().get("bi_query")

    result = await spec.handler({"queries": ["普通query 拉数据"]}, ctx)
    # rows still come back
    assert "rows" in result or result.get("ok") is False
    # _parse absent (graceful)
    assert "_parse" not in result or result.get("_parse") is None
