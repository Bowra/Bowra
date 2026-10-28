"""Sprint χ-3 bi_query batch tests — normalize / sanity / batch path / cap."""

from __future__ import annotations

import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_core.tool_result_budget import (
    _cap_bi_query,
    _cap_bi_query_batch,
)

from bowhead_bi_bridge import DatarcClient


@pytest.fixture
async def dc():
    import httpx2

    class _M(DatarcClient):
        def __init__(self) -> None:
            self._base_url = "http://bi-mock/"
            self._auth_key = "dev"
            self._timeout = 10
            self._http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=bi_mock_app),
                base_url="http://bi-mock/",
                headers={"Authorization": "Bearer dev"},
                timeout=10,
            )

    c = _M()
    try:
        yield c
    finally:
        await c.aclose()


@pytest.fixture
def bi_query_spec():
    reg = build_default_registry()
    return reg.get("bi_query")


# ─────────────────────────────────────────────────────────────
# Schema — queries: List[str] + text legacy
# ─────────────────────────────────────────────────────────────
def test_schema_exposes_queries_field(bi_query_spec) -> None:
    """χ-3: schema must declare `queries: array<string>` property.
    λ-F2 (2026-05-14): `text` 已从 schema 删除 (LLM not visible). Internal callers
    仍可通过 _normalize_bi_query_args 兜底 — see normalize tests below."""
    props = bi_query_spec.schema.input_schema["properties"]
    assert "queries" in props
    assert props["queries"]["type"] == "array"
    assert props["queries"]["items"]["type"] == "string"
    # λ-F2: text removed, no longer exposed in schema.
    assert "text" not in props
    # λ-F3: fast removed; backend auto-detection + group_by hard override.
    assert "fast" not in props


def test_schema_required_relaxed(bi_query_spec) -> None:
    """λ-F2 (2026-05-14): schema now requires `queries` (text 删后单 source).
    Internal text= callers 走 _normalize_bi_query_args 不经 schema 校验."""
    schema = bi_query_spec.schema.input_schema
    assert schema.get("required") == ["queries"]


# ─────────────────────────────────────────────────────────────
# normalize_args
# ─────────────────────────────────────────────────────────────
async def test_legacy_text_normalizes_to_queries(dc) -> None:
    """LLM emit `{"text": "..."}` (legacy) → normalize wraps to queries=[text].
    Returns flat shape (N=1 backward-compat path).

    Use sanity-fail text to short-circuit datarc; we只 verify normalize +
    flat shape return (not datarc behavior).
    """
    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    # Use a short query that triggers sanity fail (no datarc call).
    result = await spec.handler({"text": "短"}, ctx)
    # N=1 → flat shape (backward compat), not batch shape
    assert "results" not in result, f"N=1 should return flat shape, got {result.keys()}"
    # normalize happened: query_text is the original text
    assert result.get("query_text") == "短"
    assert result.get("error_kind") == "non_query_text"


# ─────────────────────────────────────────────────────────────
# Sanity check (RISK-3: meta keywords + min length)
# ─────────────────────────────────────────────────────────────
async def test_sanity_rejects_meta_query(dc) -> None:
    """Query 含 meta 关键词 → error_kind=non_query_text, 不调 datarc."""
    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    result = await spec.handler(
        {"queries": ["bi_query 怎么用", "2026年4月营业额 同比"]},
        ctx,
    )
    assert "results" in result and len(result["results"]) == 2
    # First item fails sanity.
    assert result["results"][0]["error_kind"] == "non_query_text"
    assert result["results"][0]["ok"] is False


async def test_sanity_rejects_short_query(dc) -> None:
    """Query 长度 <4 char → error_kind=non_query_text.
    ar2-v2 (2026-05-16): lowered from 8 → 4 to allow legitimate short Chinese
    queries like '上海销售' (5 chars). Sub-4 sanity-rejected as meta/junk."""
    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    result = await spec.handler({"queries": ["abc", "2026年4月营业额"]}, ctx)
    assert result["results"][0]["error_kind"] == "non_query_text"


# ─────────────────────────────────────────────────────────────
# Batch shape — results: List + _ref_artifact_id mirror
# ─────────────────────────────────────────────────────────────
async def test_batch_returns_results_array_with_ref_mirror(dc) -> None:
    """N=2 batch → {results: [...]} with each having _ref_artifact_id (mirror of artifact_id)."""
    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    result = await spec.handler(
        {"queries": ["2026年4月营业额 同比", "2026年4月利润 同比"]},
        ctx,
    )
    assert "results" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) == 2
    for sub in result["results"]:
        # ENG-4: _ref_artifact_id required (artifact_id mirror prevents LLM misuse of index paths).
        assert "_ref_artifact_id" in sub
        assert "query_text" in sub
        # query_text echoes input (alignment prevents wrong result→query mapping by the LLM).
        assert sub["query_text"] in ("2026年4月营业额 同比", "2026年4月利润 同比")


async def test_single_query_flat_shape_mirrors_ref_artifact_id() -> None:
    """review 批1-R4：N=1 flat 形态此前只有 artifact_id；prompt 教的「用 _ref_artifact_id 拿 ID」
    在最常见的单条调用上落空。现在两种形态同一取法（纯附加键，artifact_id 原样保留）。"""
    from unittest.mock import AsyncMock, MagicMock

    fake_download = MagicMock()
    fake_download.rows = [{"门店": "A", "营业额 / 元": 1}, {"门店": "B", "营业额 / 元": 2}]
    fake_download.columns = ["门店", "营业额 / 元"]
    fake_download.total_rows = 2
    fake_download.csv_filename = "stores.csv"
    fake_download.task_uid = "uid_flat"
    fake_download.download_ms = 100
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut unavailable"))
    datarc.download_data = AsyncMock(return_value=fake_download)

    class _Art:
        id = "art_flat01"
        filename = "stores.csv"

    class _Store:
        async def put_bytes(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            return _Art()

    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_flat",
        datarc=datarc,
    )
    ctx.artifact_store = _Store()  # type: ignore[attr-defined]
    result = await spec.handler({"queries": ["2026年4月 各门店 营业额"]}, ctx)
    assert "results" not in result, "N=1 仍是 flat 形态"
    assert result.get("artifact_id"), f"flat 路应落 artifact: {list(result)}"
    assert result["_ref_artifact_id"] == result["artifact_id"]


# ─────────────────────────────────────────────────────────────
# Batch cap (CTO-1 P0): hard total budget cap_tok × 1.0
# ─────────────────────────────────────────────────────────────
def test_cap_bi_query_dispatches_batch_vs_single() -> None:
    """_cap_bi_query 看 result shape, dispatch 到 _cap_bi_query_batch / _cap_bi_query_single."""
    batch = {"results": [{"artifact_id": "a1", "sample_rows": [{"a": 1}]}]}
    capped = _cap_bi_query(batch, cap_tok=1500)
    assert "results" in capped  # dispatched to batch path

    flat = {"artifact_id": "a1", "rows": [{"a": 1}]}
    capped_flat = _cap_bi_query(flat, cap_tok=1500)
    assert "rows" in capped_flat or "_rows_dropped" in capped_flat


def test_cap_batch_per_sub_cap_floor_400() -> None:
    """N=5 + cap_tok=1500 → per_cap = max(400, 1500//5=300) = 400."""
    big_rows = [{"k": "v" * 100} for _ in range(10)]
    batch = {"results": [{"artifact_id": f"a{i}", "sample_rows": big_rows} for i in range(5)]}
    capped = _cap_bi_query_batch(batch, cap_tok=1500)
    assert len(capped["results"]) == 5
    # sample_rows trimmed to 5 max (per cap logic)
    for sub in capped["results"]:
        if isinstance(sub.get("sample_rows"), list):
            assert len(sub["sample_rows"]) <= 5


def test_cap_batch_preserves_artifact_id_when_rows_dropped() -> None:
    """Per-sub over cap → sample_rows dropped, artifact_id remains."""
    huge_rows = [{"k": "x" * 5000}]
    batch = {
        "results": [
            {"artifact_id": "a1", "sample_rows": huge_rows},
        ]
    }
    capped = _cap_bi_query_batch(batch, cap_tok=1500)
    sub = capped["results"][0]
    # rows dropped but artifact_id still there
    assert sub["artifact_id"] == "a1"


async def test_batch_no_global_sem_recursion_deadlock(dc, monkeypatch) -> None:
    """χ-3.2 BLOCKER fix: inner gather acquired global_sem caused deadlock when
    outer subagent gather already held it. Verify N=4 batch completes (no hang).

    Test guards against reintroducing inner _global_sem acquisition.
    """
    import asyncio

    # Force global_sem cap=2 to amplify deadlock risk if reintroduced.
    monkeypatch.setenv("BOWHEAD_CHI_GLOBAL_MAX_CONCURRENT", "2")
    from bowhead_core import _chi_runtime

    _chi_runtime._reset_for_test()

    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
    )
    # Acquire global_sem outside (simulating outer subagent _bounded_exec).
    outer_sem = _chi_runtime.get_global_sem()
    async with outer_sem:
        # Now bi_query batch (N=4) inside. If inner re-acquires global_sem (cap 2),
        # 4 inner tasks would wait forever for outer to release → deadlock.
        # With χ-3.2 fix (inner skips global_sem), this completes via timeout-bound.
        result = await asyncio.wait_for(
            spec.handler(
                {"queries": ["短", "短", "短", "短"]},  # sanity-fail to skip datarc
                ctx,
            ),
            timeout=5.0,  # if deadlock, wait_for raises TimeoutError
        )
    assert "results" in result
    assert len(result["results"]) == 4


def test_cap_batch_merges_preflight_warnings_dedup() -> None:
    """All sub-results' _preflight_warnings → top-level _batch_preflight_summary,
    dedupe + max 6."""
    batch = {
        "results": [
            {"artifact_id": "a1", "_preflight_warnings": ["entity X not matched", "fuzzy match Y"]},
            {"artifact_id": "a2", "_preflight_warnings": ["entity X not matched"]},  # dup
            {"artifact_id": "a3", "_preflight_warnings": ["entity Z low conf"]},
        ]
    }
    capped = _cap_bi_query_batch(batch, cap_tok=2000)
    assert "_batch_preflight_summary" in capped
    summary = capped["_batch_preflight_summary"]
    # 3 unique (dup removed) ≤6
    assert "entity X not matched" in summary
    assert "fuzzy match Y" in summary
    assert "entity Z low conf" in summary
    assert len(summary) == 3
    # Sub-level _preflight_warnings stripped (already promoted)
    for sub in capped["results"]:
        assert "_preflight_warnings" not in sub


# ─────────────────────────────────────────────────────────────
# Empty fast-path (prod t_711b31fecee1): success-but-empty
# ─────────────────────────────────────────────────────────────
async def test_empty_batch_emits_no_data_signal_without_a_model_call() -> None:
    """N empty queries → each carries `_no_data` + `_no_data_hint` so the agent switches
    method instead of waiting. bi_query never calls the model itself: whether a word failed
    to match the data is the agent's call to check with bi_concept_resolve (2026-10-17).
    """
    from bowhead_bi_bridge.types import CutResult, DownloadDataResult

    class _EmptyDatarc:
        async def download_data(self, **kw):
            return DownloadDataResult(columns=[], rows=[], total_rows=0)

        async def cut_question(self, **kw):
            return CutResult(tokens=[])

    class _Router:
        async def invoke(self, *a, **kw):
            raise AssertionError("bi_query must not call the model")

    reg = build_default_registry()
    spec = reg.get("bi_query")
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=_EmptyDatarc(),
        router=_Router(),
    )
    result = await spec.handler(
        {
            "queries": [
                "2026年Q2 DSM 销售金额达成率",
                "2026年Q2 DSM 销售数量达成率",
                "2026年Q2 DSM 销售金额同比增长",
            ]
        },
        ctx,
    )
    assert "results" in result and len(result["results"]) == 3
    for sub in result["results"]:
        assert sub.get("_no_data") is True, sub
        assert "bi_concept_resolve" in sub.get("_no_data_hint", ""), sub
        assert "_term_resolutions" not in sub
