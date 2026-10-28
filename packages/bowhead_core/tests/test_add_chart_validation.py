"""0.39.7 #3 — add_chart artifact_id format validator.

Prod walkthrough: LLM hallucinated `data_artifact_id="4"` (a row number string)
→ artifact_store.get("4") returns None → tool returned `{"error": "artifact '4'
not found"}` → red "临时失败" chip in chat. Fix: validate format first; if not
`art_<12 hex>`, silent-strip with a success-shaped result and a corrective
hint, so the orchestrator doesn't count it as a failure and the LLM gets clean
feedback to use the real artifact_id next iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.canvas_tools import _add_chart, _desired_metric_keys, _explicit_series, _lead_dim_col
from bowhead_core.chart.infer import indistinguishable_rows_reason
from bowhead_core.chart.types import ChartColumn


@dataclass
class _StubCtx:
    turn_id: str = "t_test"
    user_id: str = "u"
    workspace_id: str = "w"
    session_id: str = "s1"
    artifact_store: Any = None
    canvas_store: Any = None
    parent_bus: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


class _FakeAllowlist:
    def __init__(self, refs: dict[int, tuple[str, str]]) -> None:
        self.refs = refs

    def kind_of(self, simple_id: int) -> str | None:
        ref = self.refs.get(simple_id)
        return ref[0] if ref else None

    def resolve(self, simple_or_real: str) -> str | None:
        try:
            ref = self.refs.get(int(simple_or_real))
        except ValueError:
            return simple_or_real
        return ref[1] if ref else simple_or_real


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []

    async def get(
        self,
        artifact_id: str,
        *,
        check_created_by: str | None = None,
    ) -> None:
        self.seen.append((artifact_id, check_created_by))


@dataclass
class _FakeArtifact:
    id: str
    filename: str
    path: str
    mime: str = "text/csv"


class _BytesBackend:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.read_paths: list[str] = []

    async def read(self, path: str) -> bytes:
        self.read_paths.append(path)
        return self.files[path]


class _RichArtifactStore:
    def __init__(self, artifacts: list[_FakeArtifact], files: dict[str, bytes]) -> None:
        self.artifacts = {a.id: a for a in artifacts}
        self.artifact_list = artifacts
        self.backend = _BytesBackend(files)
        self.seen: list[tuple[str, str | None]] = []

    async def get(
        self,
        artifact_id: str,
        *,
        check_created_by: str | None = None,
    ) -> _FakeArtifact | None:
        self.seen.append((artifact_id, check_created_by))
        return self.artifacts.get(artifact_id)

    async def list_for_session(
        self,
        *,
        session_id: str,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
    ) -> list[_FakeArtifact]:
        return self.artifact_list[offset : offset + limit]


class _CapturingCanvasStore:
    def __init__(self) -> None:
        self.appended_payload: dict[str, Any] | None = None

    async def ensure_canvas(self, **_: Any) -> Any:
        return SimpleNamespace(id="cv_test")

    async def list_sections_appended_by_turn(self, *_: Any, **__: Any) -> list[Any]:
        return []

    async def append_section(self, **kwargs: Any) -> Any:
        self.appended_payload = kwargs["payload"]
        return SimpleNamespace(id="sec_test", payload=kwargs["payload"], seq=1, rev=1)


def test_latin_metric_terms_require_token_boundaries() -> None:
    """ipt/pit must not match inside unrelated words like capital or hospital."""
    assert _desired_metric_keys({"title": "capital receipt hospital trend"}) == ()
    assert _desired_metric_keys({"title": "IPT 月度走势"}) == ("ipt",)


@pytest.mark.asyncio
async def test_invalid_format_pure_digit_silent_strips() -> None:
    """LLM hallucinates artifact_id='4' (row number) → silent strip."""
    out = await _add_chart(
        {"data_artifact_id": "4", "hint": "rank", "title": "x"},
        _StubCtx(),
    )
    assert "error" not in out, f"should not bubble error, got {out!r}"
    assert out["skipped"] == "invalid_artifact_id_format"
    assert out["data_artifact_id"] == "4"
    assert "art_xxxxxxxxxxxx" in out["hint"]


@pytest.mark.asyncio
async def test_simple_data_ref_resolves_before_format_gate() -> None:
    """ν1.5 <data id="12"> can be used as add_chart.data_artifact_id."""
    artifacts = _FakeArtifactStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=object())
    ctx._ν15_allowlist = _FakeAllowlist({12: ("data", "art_123456abcdef")})

    out = await _add_chart(
        {"data_artifact_id": "12", "hint": "rank", "title": "x"},
        ctx,
    )

    assert out.get("skipped") != "invalid_artifact_id_format"
    assert "artifact 'art_123456abcdef' not found" in out.get("error", "")
    assert artifacts.seen == [("art_123456abcdef", "u")]


@pytest.mark.asyncio
async def test_trend_simple_ref_reselects_monthly_metric_artifact() -> None:
    """Reselect a same-session monthly series when a trend reference resolves to a scalar."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026年1-6月pit营业额_fixture.csv",
        path="scalar.csv",
    )
    monthly_profit = _FakeArtifact(
        id="art_222222222222",
        filename="2026年1-6月利润各月_fixture.csv",
        path="monthly_profit.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, monthly_profit],
        {
            "scalar.csv": "营业额 / 元\n12000.125\n".encode(),
            "monthly_profit.csv": (
                "每月,利润 / 元\n2026-01,340000.25\n2026-02,300000.50\n2026-03,240000.75\n2026-04,230000.25\n2026-05,220000.50\n2026-06,210000.75\n"
            ).encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)
    ctx._ν15_allowlist = _FakeAllowlist({9: ("data", scalar.id)})

    out = await _add_chart(
        {
            "data_artifact_id": "9",
            "hint": "trend",
            "title": "2026年1-6月 月度利润走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["kind"] == "line"
    assert out["row_count"] == 6
    assert canvas.appended_payload is not None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == monthly_profit.id
    assert spec["source_meta"]["resolved_from_artifact_id"] == scalar.id
    assert artifacts.seen == [(scalar.id, "u")]


@pytest.mark.asyncio
async def test_real_artifact_id_scalar_for_trend_reselects_to_series() -> None:
    """An explicit scalar Artifact ID can still be corrected for a trend request."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026年1-6月pit营业额_fixture.csv",
        path="scalar.csv",
    )
    monthly_profit = _FakeArtifact(
        id="art_222222222222",
        filename="2026年1-6月利润各月_fixture.csv",
        path="monthly_profit.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, monthly_profit],
        {
            "scalar.csv": "营业额 / 元\n12000.125\n".encode(),
            "monthly_profit.csv": ("每月,利润 / 元\n2026-01,340000.25\n2026-02,300000.50\n").encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)

    out = await _add_chart(
        {
            "data_artifact_id": scalar.id,
            "hint": "trend",
            "title": "2026年1-6月 月度利润走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["row_count"] == 2
    assert canvas.appended_payload is not None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == monthly_profit.id
    assert spec["source_meta"]["resolved_from_artifact_id"] == scalar.id


@pytest.mark.asyncio
async def test_real_artifact_id_good_series_not_reselected() -> None:
    """The "don't mutate explicit evidence" rule still holds when the explicit
    artifact is already a valid multi-row series — no reselect even under a trend
    title (row_count>=2 → the mistake trigger never fires)."""
    good = _FakeArtifact(
        id="art_111111111111",
        filename="2026年1-6月利润各月_aaa000.csv",
        path="good.csv",
    )
    other = _FakeArtifact(
        id="art_222222222222",
        filename="2026年1-6月利润各月_bbb111.csv",
        path="other.csv",
    )
    artifacts = _RichArtifactStore(
        [good, other],
        {
            "good.csv": ("每月,利润 / 元\n2026-01,340000.25\n2026-02,300000.50\n2026-03,240000.75\n").encode(),
            "other.csv": ("每月,利润 / 元\n2026-01,1.0\n2026-02,2.0\n").encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)

    out = await _add_chart(
        {
            "data_artifact_id": good.id,
            "hint": "trend",
            "title": "2026年1-6月 月度利润走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["row_count"] == 3
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == good.id
    assert "resolved_from_artifact_id" not in spec["source_meta"]


@pytest.mark.asyncio
async def test_real_artifact_id_yoy_scalar_not_reselected_no_error() -> None:
    """同比/vs charts can legitimately be a single row (this period vs last), so a
    real-id scalar under a 同比 title is NOT treated as a trend mistake — no
    reselect, no error; it renders as a KPI. Guards the narrower progression gate
    against over-firing on 同比/环比/vs."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026营业额同比_x00000.csv",
        path="scalar.csv",
    )
    monthly = _FakeArtifact(
        id="art_222222222222",
        filename="2026营业额各月_y00000.csv",
        path="monthly.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, monthly],
        {
            "scalar.csv": "营业额 / 元,同比 / 元\n1188892.85,1100000.0\n".encode(),
            "monthly.csv": ("每月,营业额 / 元\n2026-01,1.0\n2026-02,2.0\n").encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)

    out = await _add_chart(
        {
            "data_artifact_id": scalar.id,
            "hint": "raw",
            "title": "2026年营业额同比",
            "column_types": {},
        },
        ctx,
    )

    assert out.get("error") is None
    assert out.get("error_kind") is None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == scalar.id
    assert "resolved_from_artifact_id" not in spec["source_meta"]
    # Followup #5 (Feishu bug list Wea1wLZ…): never reselect an explicit ID (enforced here), but truthfully report
    # the requested-series/single-row contradiction to the agent; it already has the correct monthly artifact. warning+hint
    # lets it redraw with the same title while preserving the resolved source identity.
    assert out["warning"] == "series_requested_but_single_row"
    assert "重新调用" in out["hint"]


@pytest.mark.asyncio
async def test_rows_without_a_distinguishing_label_warn_the_agent() -> None:
    """Warn when the Artifact has no categorical column that distinguishes rows.

    The synthetic constant-label fixture deliberately has no grouping column. Column
    selection fixes are tested separately; this case covers genuinely ambiguous data.
    """
    art = _FakeArtifact(id="art_333333333333", filename="大区同比_z00000.csv", path="regionless.csv")
    artifacts = _RichArtifactStore(
        [art],
        {
            "regionless.csv": (
                "中文品牌_2026,销售数量_2026,销售数量_2025,同比增长率\n"
                "示例产品甲,180.0,200.0,-0.10\n"
                "示例产品甲,330.0,300.0,0.10\n"
                "示例产品甲,360.0,400.0,-0.10\n"
            ).encode(),
        },
    )
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=_CapturingCanvasStore())

    out = await _add_chart(
        {"data_artifact_id": art.id, "hint": "raw", "title": "示例产品甲各大区上半年销量同比", "column_types": {}},
        ctx,
    )

    assert out.get("error") is None, "不阻断落库——图照旧存，只是把矛盾告诉 agent"
    assert out["warning"] == "rows_are_indistinguishable"
    assert "中文品牌_2026" in out["hint"], "hint 要点名是哪一列恒定，否则 agent 无从下手"
    assert "区分" in out["hint"], "hint 要说清楚缺的是「能区分行的那一列」"


@pytest.mark.asyncio
async def test_chart_with_a_real_grouping_column_has_no_such_warning() -> None:
    """★ 正对照：同样 3 行，但大区列在 —— 一个字的告警都不该有。

    没有这条，「见表就告警」的坏实现也会让上面那条绿。
    """
    art = _FakeArtifact(id="art_444444444444", filename="大区同比_w00000.csv", path="withregion.csv")
    artifacts = _RichArtifactStore(
        [art],
        {
            "withregion.csv": (
                "大区经理辖区,销售数量_2026,销售数量_2025,同比增长率\n"
                "LINE_A东一大区,180.0,200.0,-0.10\n"
                "LINE_A东二大区,330.0,300.0,0.10\n"
                "LINE_A北一大区,360.0,400.0,-0.10\n"
            ).encode(),
        },
    )
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=_CapturingCanvasStore())

    out = await _add_chart(
        {"data_artifact_id": art.id, "hint": "raw", "title": "示例产品甲各大区上半年销量同比", "column_types": {}},
        ctx,
    )
    assert out.get("error") is None
    assert "warning" not in out, f"误报：{out.get('warning')} / {out.get('hint')}"


@pytest.mark.asyncio
async def test_multirow_series_chart_has_no_warning() -> None:
    monthly = _FakeArtifact(
        id="art_222222222222",
        filename="2026年1-6月利润各月_fixture.csv",
        path="monthly_profit.csv",
    )
    artifacts = _RichArtifactStore(
        [monthly],
        {
            "monthly_profit.csv": ("每月,利润 / 元\n2026-01,340000.25\n2026-02,300000.50\n2026-03,240000.75\n").encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)
    out = await _add_chart(
        {
            "data_artifact_id": monthly.id,
            "hint": "trend",
            "title": "2026年1-3月 月度利润走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )
    assert out["row_count"] == 3
    assert "warning" not in out


@pytest.mark.asyncio
async def test_trend_scalar_no_series_available_returns_error() -> None:
    """#5 safety-net: a 走势 chart whose only data is a 1-row scalar and no better
    in-session series exists → refuse to render a misleading KPI; return an
    actionable error so the analyst re-fetches a time series."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026营业额总额_x00000.csv",
        path="scalar.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar],
        {"scalar.csv": "营业额 / 元\n1189637.86\n".encode()},
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)

    out = await _add_chart(
        {
            "data_artifact_id": scalar.id,
            "hint": "trend",
            "title": "2026年1-6月 月度营业额走势",
            "column_types": {},
        },
        ctx,
    )

    assert out.get("error_kind") == "chart_series_needs_time_dimension"
    assert canvas.appended_payload is None


@pytest.mark.asyncio
async def test_combined_revenue_profit_ref_prefers_combined_monthly_artifact() -> None:
    """营业额/利润走势 should not collapse to a profit-only artifact."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026年1-6月pit营业额_fixture.csv",
        path="scalar.csv",
    )
    monthly_profit = _FakeArtifact(
        id="art_222222222222",
        filename="2026年1-6月利润各月_fixture.csv",
        path="monthly_profit.csv",
    )
    monthly_both = _FakeArtifact(
        id="art_333333333333",
        filename="2026年1-6月营业额利润各月_874d2a.csv",
        path="monthly_both.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, monthly_profit, monthly_both],
        {
            "scalar.csv": "营业额 / 元\n12000.125\n".encode(),
            "monthly_profit.csv": ("每月,利润 / 元\n2026-01,340000.25\n2026-02,300000.50\n").encode(),
            "monthly_both.csv": (
                "每月,营业额 / 元,利润 / 元\n2026-01,1200000,347065.47\n2026-02,1100000,305098.99\n2026-03,980000,242055.20\n"
            ).encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)
    ctx._ν15_allowlist = _FakeAllowlist({9: ("data", scalar.id)})

    out = await _add_chart(
        {
            "data_artifact_id": "9",
            "hint": "trend",
            "title": "2026年1-6月 营业额/利润走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["row_count"] == 3
    assert canvas.appended_payload is not None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == monthly_both.id
    assert spec["source_meta"]["resolved_from_artifact_id"] == scalar.id


@pytest.mark.asyncio
async def test_simple_ref_reselect_prefers_matching_member_segment() -> None:
    """会员 trend should not recover to a 非会员 artifact just because it is monthly."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026年会员营业额.csv",
        path="scalar.csv",
    )
    non_member_monthly = _FakeArtifact(
        id="art_222222222222",
        filename="2026年非会员营业额各月.csv",
        path="non_member.csv",
    )
    member_monthly = _FakeArtifact(
        id="art_333333333333",
        filename="2026年会员营业额各月.csv",
        path="member.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, non_member_monthly, member_monthly],
        {
            "scalar.csv": "营业额 / 元\n1188892.85\n".encode(),
            "non_member.csv": ("每月,非会员营业额 / 元\n2026-01,300\n2026-02,320\n").encode(),
            "member.csv": ("每月,会员营业额 / 元\n2026-01,1000\n2026-02,1200\n").encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)
    ctx._ν15_allowlist = _FakeAllowlist({9: ("data", scalar.id)})

    out = await _add_chart(
        {
            "data_artifact_id": "9",
            "hint": "trend",
            "title": "2026年会员营业额月度走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["row_count"] == 2
    assert canvas.appended_payload is not None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == member_monthly.id


@pytest.mark.asyncio
async def test_simple_ref_reselect_prefers_matching_year() -> None:
    """A 2026 trend should not recover to the otherwise similar 2025 monthly artifact."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026年营业额.csv",
        path="scalar.csv",
    )
    monthly_2025 = _FakeArtifact(
        id="art_222222222222",
        filename="2025年营业额各月.csv",
        path="monthly_2025.csv",
    )
    monthly_2026 = _FakeArtifact(
        id="art_333333333333",
        filename="2026年营业额各月.csv",
        path="monthly_2026.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, monthly_2025, monthly_2026],
        {
            "scalar.csv": "营业额 / 元\n1188892.85\n".encode(),
            "monthly_2025.csv": ("每月,营业额 / 元\n2025-01,900\n2025-02,950\n").encode(),
            "monthly_2026.csv": ("每月,营业额 / 元\n2026-01,1000\n2026-02,1200\n").encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)
    ctx._ν15_allowlist = _FakeAllowlist({9: ("data", scalar.id)})

    out = await _add_chart(
        {
            "data_artifact_id": "9",
            "hint": "trend",
            "title": "2026年营业额月度走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["row_count"] == 2
    assert canvas.appended_payload is not None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == monthly_2026.id


@pytest.mark.asyncio
async def test_simple_ref_reselect_skips_obvious_filename_mismatches_before_read() -> None:
    """Do not CSV-parse candidates that are clearly wrong from filename metadata."""
    scalar = _FakeArtifact(
        id="art_111111111111",
        filename="2026年会员营业额.csv",
        path="scalar.csv",
    )
    wrong_year = _FakeArtifact(
        id="art_222222222222",
        filename="2025年会员营业额各月.csv",
        path="wrong_year.csv",
    )
    wrong_segment = _FakeArtifact(
        id="art_333333333333",
        filename="2026年非会员营业额各月.csv",
        path="wrong_segment.csv",
    )
    wrong_metric = _FakeArtifact(
        id="art_444444444444",
        filename="2026年会员利润各月.csv",
        path="wrong_metric.csv",
    )
    matching = _FakeArtifact(
        id="art_555555555555",
        filename="2026年会员营业额各月.csv",
        path="matching.csv",
    )
    artifacts = _RichArtifactStore(
        [scalar, wrong_year, wrong_segment, wrong_metric, matching],
        {
            "scalar.csv": "营业额 / 元\n1188892.85\n".encode(),
            "wrong_year.csv": "每月,会员营业额 / 元\n2025-01,900\n2025-02,950\n".encode(),
            "wrong_segment.csv": "每月,非会员营业额 / 元\n2026-01,300\n2026-02,320\n".encode(),
            "wrong_metric.csv": "每月,会员利润 / 元\n2026-01,100\n2026-02,120\n".encode(),
            "matching.csv": "每月,会员营业额 / 元\n2026-01,1000\n2026-02,1200\n".encode(),
        },
    )
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas)
    ctx._ν15_allowlist = _FakeAllowlist({9: ("data", scalar.id)})

    out = await _add_chart(
        {
            "data_artifact_id": "9",
            "hint": "trend",
            "title": "2026年会员营业额月度走势",
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    assert out["row_count"] == 2
    assert canvas.appended_payload is not None
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == matching.id
    read_paths = artifacts.backend.read_paths
    assert "matching.csv" in read_paths
    assert "wrong_year.csv" not in read_paths
    assert "wrong_segment.csv" not in read_paths
    assert "wrong_metric.csv" not in read_paths


@pytest.mark.asyncio
async def test_simple_chart_ref_does_not_resolve_as_data_artifact() -> None:
    """ν1.5 <chart id="12"> belongs in evidence.chart_section_id, not here."""
    ctx = _StubCtx()
    ctx._ν15_allowlist = _FakeAllowlist({12: ("chart", "sec_chart_real")})

    out = await _add_chart(
        {"data_artifact_id": "12", "hint": "rank", "title": "x"},
        ctx,
    )

    assert out["skipped"] == "invalid_artifact_id_format"
    assert "chart" in out["hint"]
    assert "chart_section_id" in out["hint"]


@pytest.mark.asyncio
async def test_invalid_format_garbage_silent_strips() -> None:
    """Any non-matching string silent-strips (column name, title fragment, etc)."""
    for bad in ["GMV", "row_1", "art_123", "art_GHIJKL123456", "ART_a1b2c3d4e5f6", "art_a1b2c3d4e5f6_extra"]:
        out = await _add_chart(
            {"data_artifact_id": bad, "hint": "rank", "title": "x"},
            _StubCtx(),
        )
        assert "error" not in out, f"{bad!r} should silent-strip, got {out!r}"
        assert out["skipped"] == "invalid_artifact_id_format", f"{bad!r} expected silent-strip"


@pytest.mark.asyncio
async def test_empty_id_keeps_legacy_required_error() -> None:
    """Empty stays a hard error (different signal — LLM forgot the arg entirely)."""
    out = await _add_chart(
        {"data_artifact_id": "", "hint": "rank", "title": "x"},
        _StubCtx(),
    )
    assert "error" in out
    assert "required" in out["error"]


@pytest.mark.asyncio
async def test_valid_format_passes_format_check() -> None:
    """art_<12 hex> matches regex → falls through to next gate (artifact_store
    check). We stub ctx.artifact_store=None to short-circuit there so this test
    stays focused on the format gate.
    """
    out = await _add_chart(
        {"data_artifact_id": "art_a1b2c3d4e5f6", "hint": "rank", "title": "x"},
        _StubCtx(artifact_store=None),
    )
    # Should NOT be the format-skip path
    assert out.get("skipped") != "invalid_artifact_id_format"
    # Should hit the next gate
    assert "artifact_store not configured" in out.get("error", "")


# ── Rejections must also provide the correct answer (116 dogfood, 2026-09-24) ───
#
# Observed: **346** add_chart + canvas_add_chart calls were skipped for invalid `data_artifact_id`
# format; **336** followed a data artifact already produced in the current turn,
# leaving the correct answer at hand. `t_17e216c2316b` guessed 52 times with no chart on canvas.
# Old hints only said the supplied value was invalid; with no other information, the model could only keep guessing.


def _real_allowlist(*entries: tuple[str, str, str]) -> Any:
    """真 `RefAllowlist`（不是本文件上面那个 `_FakeAllowlist`）。

    ★ 必须用真的：`_FakeAllowlist` 没有 `choices_of_kind`，而 `_available_data_hint`
    对取不到清单是**静默降级**的——拿替身测，这条路会整条跳过却依旧全绿
    （[[feedback_fixture_stub_skips_production_gate]]）。
    """
    from bowhead_core._ref_allowlist import RefAllowlist

    al = RefAllowlist()
    for kind, real_id, origin in entries:
        al.add(kind, real_id, origin=origin)
    return al


@pytest.mark.asyncio
async def test_rejection_tells_the_model_which_data_ids_it_can_actually_use() -> None:
    ctx = _StubCtx()
    ctx._ν15_allowlist = _real_allowlist(
        ("data", "art_aaaaaaaaaaaa", "2024 各省销量"),
        ("code", "art_cccccccccccc", "画图脚本"),
        ("data", "art_bbbbbbbbbbbb", "2024 各品牌销量"),
    )

    out = await _add_chart({"data_artifact_id": "77", "hint": "rank", "title": "x"}, ctx)

    assert out["skipped"] == "invalid_artifact_id_format"
    hint = out["hint"]
    # Provide ordinal and real ID: real ID is directly usable; ordinal is its representation in <available_refs>.
    assert "art_aaaaaaaaaaaa" in hint
    assert "art_bbbbbbbbbbbb" in hint
    assert "2024 各省销量" in hint
    # `code` is not a data artifact; listing it steers the model into the next trap.
    assert "art_cccccccccccc" not in hint


@pytest.mark.asyncio
async def test_rejection_with_an_empty_data_list_does_not_claim_there_is_none() -> None:
    """★ allowlist 每圈 iteration 开头才 refresh：同一圈里刚 `bi_query` 出来的那份
    还没进清单。此时说「本轮没有数据」是**假话**，会让模型放弃一份就在手边的结果。
    """
    ctx = _StubCtx()
    ctx._ν15_allowlist = _real_allowlist(("code", "art_cccccccccccc", "画图脚本"))

    out = await _add_chart({"data_artifact_id": "77", "hint": "rank", "title": "x"}, ctx)

    hint = out["hint"]
    assert "_ref_artifact_id" in hint
    assert "bi_query" in hint
    # The absolute claim is false: not surfaced yet is true; none exist is not.
    assert "没有任何" not in hint


@pytest.mark.asyncio
async def test_a_stub_allowlist_without_choices_still_gets_a_skip_not_a_crash() -> None:
    """提示取不到就不说——但绝不能把一次 skip 变成一次工具异常。"""
    ctx = _StubCtx()
    ctx._ν15_allowlist = _FakeAllowlist({3: ("data", "art_a1b2c3d4e5f6")})

    out = await _add_chart({"data_artifact_id": "77", "hint": "rank", "title": "x"}, ctx)

    assert out["skipped"] == "invalid_artifact_id_format"
    assert "不是合法格式" in out["hint"]


@pytest.mark.asyncio
async def test_bare_hex_id_is_coerced_instead_of_rejected() -> None:
    """Repair a valid synthetic hexadecimal ID missing only the Artifact prefix."""
    store = _FakeArtifactStore()
    out = await _add_chart(
        {"data_artifact_id": "123456abcdef", "hint": "rank", "title": "x"},
        _StubCtx(artifact_store=store, canvas_store=object()),
    )

    assert out.get("skipped") != "invalid_artifact_id_format"
    # ★ Prefix repair **must not** bypass ownership checks; still look up by requesting user after repair.
    assert store.seen == [("art_123456abcdef", "u")]


@pytest.mark.asyncio
async def test_bare_hex_coercion_does_not_swallow_near_misses() -> None:
    """长度/字符集不对的仍然是格式错——补前缀只救「只差前缀」那一种。"""
    for bad in ["123456abcde", "123456abcdef5", "123456ABCDEF", "123456abcdeg"]:
        out = await _add_chart(
            {"data_artifact_id": bad, "hint": "rank", "title": "x"},
            _StubCtx(artifact_store=_FakeArtifactStore()),
        )
        assert out.get("skipped") == "invalid_artifact_id_format", f"{bad!r} 不该被补前缀"


# A constant filter column must not displace a distinguishing grouping column
# when selecting the category axis.


def _cc(key: str, typ: str = "categorical") -> ChartColumn:
    return ChartColumn(key=key, header=key, type=typ)  # type: ignore[arg-type]


REGION_COLS = [
    _cc("产品线"),
    _cc("大区经理辖区"),
    _cc("2026年1-6月销售数量", "numeric"),
    _cc("2025年1-6月销售数量", "numeric"),
    _cc("同比变化率", "numeric"),
]
REGION_ROWS = [
    {"产品线": "LINE_A", "大区经理辖区": f"LINE_A{r}大区", "2026年1-6月销售数量": v, "2025年1-6月销售数量": v * 1.05, "同比变化率": -0.05}
    for r, v in zip(["东一", "东二", "北一", "北二", "南", "西"], [180, 330, 360, 420, 510, 640], strict=True)
]


def test_region_column_survives_pruning() -> None:
    """The synthetic product line is constant; the regional column distinguishes rows."""
    args = {"series": ["2026年1-6月销售数量", "2025年1-6月销售数量", "同比变化率"], "hint": "compare"}
    rows, cols = _explicit_series(args, REGION_ROWS, REGION_COLS)
    assert [c.key for c in cols][0] == "大区经理辖区", [c.key for c in cols]
    assert indistinguishable_rows_reason(rows, cols) is None


def test_picks_the_finest_dimension_not_the_first_distinguishing_one() -> None:
    """Choose the most distinguishing synthetic dimension, not the first nonconstant one."""
    cols = [_cc("产品名称"), _cc("省份"), _cc("市级"), _cc("份额", "numeric")]
    rows = [{"产品名称": "示例产品乙", "省份": f"P{i // 2}", "市级": f"C{i}", "份额": 0.1} for i in range(10)]
    dims = [c for c in cols if c.type == "categorical"]
    assert _lead_dim_col(rows, dims).key == "市级"


def test_table_keeps_every_distinguishing_dimension() -> None:
    """A table preserves every dimension needed to identify a compound key."""
    cols = [_cc("品牌"), _cc("种类大类"), _cc("营业额", "numeric")]
    rows = [{"品牌": f"B{i % 3}", "种类大类": f"C{i // 3}", "营业额": 100 + i} for i in range(9)]
    args = {"series": ["营业额"], "force_kind": "table"}
    got_rows, got_cols = _explicit_series(args, rows, cols)
    assert [c.key for c in got_cols] == ["品牌", "种类大类", "营业额"], [c.key for c in got_cols]
    assert len({(r["品牌"], r["种类大类"]) for r in got_rows}) == 9


def test_chart_keeps_the_grouping_dimension_when_it_pivots() -> None:
    """Preserve the grouping dimension required to pivot a long table into series.

    Dropping it would connect multiple values at the same timestamp as one line.
    """
    cols = [_cc("每月", "date"), _cc("定义市场"), _cc("份额", "numeric")]
    rows = [{"每月": f"2025年{m}月", "定义市场": market, "份额": 0.1 + m / 100} for m in range(1, 11) for market in ("零售市场", "批发市场")]
    got_rows, got_cols = _explicit_series({"series": ["份额"], "force_kind": "line", "hint": "trend"}, rows, cols)
    assert [c.key for c in got_cols] == ["每月", "定义市场", "份额"], [c.key for c in got_cols]
    assert all("定义市场" in r for r in got_rows)


def test_kept_grouping_dimension_reaches_two_rendered_series() -> None:
    """★★ 真正要证的不是「那一列还在」，是**用户最终看见两条线**。

    只断言 `_explicit_series` 的输出，等于把闸设在半路上：列留下了、下游却没摊开
    的话，用户看到的仍是一条锯齿线。所以这条一路走到共用的呈现归一
    （`normalize_chart_for_rendering_v1`，前端 `pivot-long.ts` 的同源实现）：
    20 行 × 10 个月份 ⇒ 应摊成 **10 行 × 2 条系列**。
    """
    from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1

    cols = [_cc("每月", "date"), _cc("定义市场"), _cc("份额", "numeric")]
    rows = [{"每月": f"2025年{m}月", "定义市场": market, "份额": 0.1 + m / 100} for m in range(1, 11) for market in ("零售市场", "批发市场")]
    kept_rows, kept_cols = _explicit_series({"series": ["份额"], "force_kind": "line", "hint": "trend"}, rows, cols)
    spec = {
        "kind": "line",
        "columns": [{"key": c.key, "header": c.key, "type": c.type} for c in kept_cols],
        "rows": kept_rows,
        "row_count": len(kept_rows),
    }
    normalized, _shape = normalize_chart_for_rendering_v1(spec)
    series_keys = [c["key"] for c in normalized["columns"] if str(c["key"]).startswith("__pivot__")]
    assert len(series_keys) == 2, normalized["columns"]
    assert len(normalized["rows"]) == 10, len(normalized["rows"])


def test_chart_keeps_one_dimension_when_several_metrics_are_named() -> None:
    """负对照：点了多个数值列时「一列一系列」的老约定本来就成立，第二个维度摊不开。

    判据与 `pivot-long.ts:detectLong` 的第 1 条（恰好 1 个数值列）同源。
    """
    cols = [_cc("品牌"), _cc("种类大类"), _cc("营业额", "numeric"), _cc("利润", "numeric")]
    rows = [{"品牌": f"B{i % 3}", "种类大类": f"C{i // 3}", "营业额": 100 + i, "利润": 10 + i} for i in range(9)]
    _, got_cols = _explicit_series({"series": ["营业额", "利润"], "hint": "compare"}, rows, cols)
    assert len([c for c in got_cols if c.type == "categorical"]) == 1


def test_chart_drops_the_second_dimension_when_the_two_do_not_cross() -> None:
    """负对照：两个维度**没有真交叉**（伪长表）时不留第二个。

    填充率判据读的是前后端共用的那份策略（`presentation_policy.json` 的
    `minimumLongTableFillRatio`），不在这里另写一个数。
    真实反例形如 `门店面积与营业额关系`：39 行摊出 39 条系列，只会得到一个对角线矩阵。
    """
    cols = [_cc("门店"), _cc("店长"), _cc("营业额", "numeric")]
    rows = [{"门店": f"S{i}", "店长": f"M{i}", "营业额": 100 + i} for i in range(10)]
    _, got_cols = _explicit_series({"series": ["营业额"], "hint": "rank"}, rows, cols)
    assert len([c for c in got_cols if c.type == "categorical"]) == 1


def test_all_constant_dims_falls_back_and_the_guard_still_fires() -> None:
    """全是常量列时退回第一个，并让 `indistinguishable_rows_reason` 照常报警——
    那道守卫剩下的正是这一半（artifact 里真的一列都区分不了行）。"""
    cols = [_cc("品牌"), _cc("销量", "numeric")]
    rows = [{"品牌": "示例产品甲", "销量": 100 + i} for i in range(3)]
    got_rows, got_cols = _explicit_series({"series": ["销量"]}, rows, cols)
    assert [c.key for c in got_cols] == ["品牌", "销量"]
    assert indistinguishable_rows_reason(got_rows, got_cols) is not None


@pytest.mark.asyncio
async def test_limit_takes_the_real_top_n_not_the_first_n() -> None:
    """★ `limit` 是给排名用的：必须按主数值列取**真**前 N。

    116 实测：1325 次 add_chart 里 449 次传了 `limit`（34%），而它此前只截头不排序——
    模型照工具说明传 `limit=10` 画「Top 10」，拿到的是文件前 10 行，与真 Top 10 只重合 4/10。
    """
    csv = "门店,营业额\n" + "".join(f"S{i},{(i * 37) % 100}\n" for i in range(40))
    art = _FakeArtifact(id="art_333333333333", filename="门店营业额_ccc222.csv", path="shops.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"shops.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart(
        {"data_artifact_id": art.id, "hint": "rank", "title": "门店营业额 Top 10", "limit": 10},
        ctx,
    )

    spec = canvas.appended_payload["spec"]
    drawn = sorted((r["营业额"] for r in spec["rows"]), reverse=True)
    expected = sorted(((i * 37) % 100 for i in range(40)), reverse=True)[:10]
    assert drawn == expected, f"画的不是真 Top 10：{drawn}"
    # Disclose truncation: restore total count, show only-first-N copy below the chart, and tell the model not to describe it as complete.
    assert spec["row_count"] == 40
    assert spec["top_n"] == 10
    assert out["row_count"] == 40 and out["drawn_rows"] == 10
    assert "别说成全部" in out["note"]


@pytest.mark.asyncio
async def test_series_cap_is_reported_back_to_the_model() -> None:
    """★★ 系列被截时也要告诉模型——否则它照着一张只有 15 个省的图写「各省走势如下」。

    `add_chart` 的返回值是模型写正文的**唯一**依据。这里此前只认 `top_n`，
    而系列截断走的是另一个口径（`series_shown`/`series_total`，`top_n` 是空的）：
    漏掉的后果不是少一句提示，是正文把 44 个省说没了。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    csv = "每月,省份,刷卡金额\n" + "".join(f"{m},省{p:02d},{1000 + p}\n" for m in months for p in range(59))
    art = _FakeArtifact(id="art_444444444444", filename="各省月度_ddd333.csv", path="prov.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"prov.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart(
        {"data_artifact_id": art.id, "hint": "trend", "title": "2025年各省月度刷卡金额走势"},
        ctx,
    )

    spec = canvas.appended_payload["spec"]
    assert spec["series_shown"] == 15 and spec["series_total"] == 59
    assert spec.get("top_n") is None, "系列截断不打 `top_n`"
    assert out["drawn_series"] == 15 and out["series_total"] == 59
    assert "别说成全部" in out["note"], f"模型拿不到提示：{out.get('note')!r}"


@pytest.mark.asyncio
async def test_the_model_is_told_who_the_top_series_actually_are() -> None:
    """★★ 只说「15/38」不够，得说**是哪 15 条、谁最大**。

    116 dogfood 2026-09-24 `t_f4edf5b877b3`（1.3.46 真机）：这道闸按合计正确挑出前 15 条、
    图也画对了（第一名女装 62.49 亿，线高出一个数量级），可回给模型的只有「15/38」这个数。
    模型手里唯一带品类名的数据是 `bi_query` 内联的**头 10 行**——1 月份按品类名拼音序的
    前 10 个，其中最大的是内暖衣(4988万)与厨房用具(773万)。于是终答写成：

        「『内暖衣』与『厨房用具』是绝对的销售支柱」

    而女装在正文里一次都没出现。**「不要照着这 10 行下结论」那句提醒当时已经在返回里**，
    模型照做了它明令禁止的事 —— 光说「别这么做」不够，得把正确答案摆出来
    （与 `_available_data_hint` 同一条教训）。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    # Names deliberately reverse **pinyin order** and **total order**: `aa_小类` sorts first but is smallest; `zz_龙头` last but largest.
    small = [f"aa小类{i:02d}" for i in range(20)]
    big = ["zz龙头", "yy次席", "xx第三"]
    csv = "每月,品类,营业额\n" + "".join(
        f"{m},{c},{(10**9 if c == 'zz龙头' else 4 * 10**8 if c == 'yy次席' else 2 * 10**8 if c == 'xx第三' else 10**5)}\n"
        for m in months
        for c in [*small, *big]
    )
    art = _FakeArtifact(id="art_888888888888", filename="品类月度_rank.csv", path="rank.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"rank.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart({"data_artifact_id": art.id, "hint": "trend", "title": "各品类月度营业额"}, ctx)

    note = out.get("note") or ""
    assert out["drawn_series"] == 15 and out["series_total"] == 23
    # True leaders must appear **by name** and come first.
    assert "zz龙头" in note, f"没把第一名告诉模型：{note!r}"
    assert note.index("zz龙头") < note.index("yy次席") < note.index("xx第三"), f"排名顺序不对：{note!r}"
    # Include magnitude too: names alone hide the difference between "120亿 vs 120万" from the model.
    assert "120.00亿" in note and "120.0万" in note, f"没给量级：{note!r}"
    # Small categories first in pinyin order must not precede the three true leaders (their names merely sort earlier).
    listed = note.split("按合计从大到小依次是：")[-1]
    assert listed.index("xx第三") < listed.index("aa小类"), f"小类目被排到头部前面：{note!r}"


@pytest.mark.asyncio
async def test_no_rank_line_when_nothing_was_capped() -> None:
    """★ 负对照：没截系列就不该多出一句排名——那会让每张普通图都平白多一段提示。"""
    csv = "每月,品类,营业额\n" + "".join(f"2025-{m:02d},甲,{m}00\n2025-{m:02d},乙,{m}0\n" for m in range(1, 13))
    art = _FakeArtifact(id="art_999999999999", filename="两条线.csv", path="two.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"two.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart({"data_artifact_id": art.id, "hint": "trend", "title": "两个品类"}, ctx)

    assert "drawn_series" not in out
    assert "按合计从大到小" not in (out.get("note") or "")


@pytest.mark.asyncio
async def test_wide_pivot_series_cut_is_reported_too() -> None:
    """★★ 宽表 pivot 那一刀此前**只打日志**——三处文案一起变成假话。

    116 dogfood 2026-09-24 `t_b06a7810265a` 实录：沙箱 `pivot_table` 出 38 个品类的宽表，
    `_MAX_SERIES_SAME_FAMILY=12` 按量级留 12 列、丢 26 个（日志 `total=38 kept=12`），
    而图上、副标题、正文、指标卡全写着 38：

        副标题「单位：元 · 共 38 个一级品类」
        正文  「2025 年 1-12 月**全部 38 个**一级品类的月度营业额折线图已出」
        指标卡「一级品类数: 38 个」
        分析  「头部品类几乎压平了**其余 35 条线**」「**34 条线**几乎重叠在底部」

    图例里实际只有 12 条。模型不是瞎写——`add_chart` 的返回是它写正文的唯一依据，
    而那一刀一个字节都没回给它。长表那条路（`_cap_time_series`）早就填了这两个字段，
    缺的从来不是机制，是这一支没接上。
    """
    # Fixture deliberately separates total-based and mean-based ordering; otherwise assertions cannot detect the ranking basis:
    # · 11 leaders: 1e6·(m+1) monthly ⇒ total 9e7, nonzero mean 7.5e6
    # · "尖峰" (spike): values in only 2 months, 2e7 each ⇒ total 4e7 (excluded), but mean 2e7 (first by mean)
    # · "负数" (negative): -1e7 monthly ⇒ |total| 1.2e8 (first), while signed sum -1.2e8 (last)
    heads = [f"品类{i:02d}" for i in range(11)]
    tail = [f"长尾{i:02d}" for i in range(25)]
    cats = [*heads, "尖峰", "负数", *tail]
    assert len(cats) == 38
    header = "月份," + ",".join(cats)

    def cell(cat: str, m: int) -> str:
        if cat in heads:
            return str(10**6 * (m + 1))
        if cat == "尖峰":
            return str(2 * 10**7) if m <= 2 else "0"
        if cat == "负数":
            return str(-(10**7))
        return str(10**2 * (m + 1))

    body = "".join(f"2025年{m}月," + ",".join(cell(c, m) for c in cats) + "\n" for m in range(1, 13))
    art = _FakeArtifact(id="art_555555555555", filename="品类月度宽表_abc123.csv", path="wide.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(
        artifact_store=_RichArtifactStore([art], {"wide.csv": (header + "\n" + body).encode()}),
        canvas_store=canvas,
    )

    out = await _add_chart(
        {"data_artifact_id": art.id, "hint": "trend", "title": "2025年各一级品类月度营业额"},
        ctx,
    )

    spec = canvas.appended_payload["spec"]
    assert spec["series_shown"] == 12 and spec["series_total"] == 38
    assert out["drawn_series"] == 12 and out["series_total"] == 38
    assert "别说成全部" in out["note"], f"模型拿不到提示：{out.get('note')!r}"
    # ★ Wide-table path must provide **rankings** too: if only one truncation path does, the bug remains
    # in half the scenarios (fixing only the long-table path does not fully address 116 `t_f4edf5b877b3`).
    listed = (out.get("note") or "").split("按合计从大到小依次是：")[-1]
    assert "负数" in listed, f"排名里没有合计最大的那条：{out.get('note')!r}"
    assert listed.index("负数") < listed.index("品类00"), f"排名顺序不对：{out.get('note')!r}"
    # Retain the 12 largest **absolute totals**: 11 leaders + "负数".
    # "尖峰" totals only 4e7, so excluded; mean ordering would rank it first, the exact wrong basis to prevent.
    kept = {c["key"] for c in spec["columns"] if c.get("type") == "numeric"}
    assert kept == {*heads, "负数"}, f"留错了列：{sorted(kept)}"


def test_the_two_series_cuts_cannot_both_fire() -> None:
    """两条系列截断路径互斥的**前提**，钉在这里。

    宽表那一刀把数值列裁到 `_MAX_SERIES_SAME_FAMILY`，而 `_cap_time_series` 要
    `seriesCount > _MAX_TIME_SERIES` 才动手。只要前者 < 后者，宽表裁完就再也够不到
    系列闸 —— `_add_chart` 里「不覆盖 infer 已写好的值」那个守卫因此今天不可达。

    这份互斥**只是两个常量当前的大小关系**，改任一个都会静默失效
    （[[feedback_correctness_by_coincident_constants]]）：真调大了这条会红，
    到那时上面那个守卫就从摆设变成必需，得连带补一条「谁说了算」的用例。
    """
    from bowhead_core.chart.infer import _MAX_SERIES_SAME_FAMILY, _MAX_TIME_SERIES

    assert _MAX_SERIES_SAME_FAMILY < _MAX_TIME_SERIES, (
        f"宽表上限 {_MAX_SERIES_SAME_FAMILY} 不再小于系列闸 {_MAX_TIME_SERIES}："
        "两刀现在可能同时触发，`_add_chart` 里的覆盖守卫需要一条用例说清谁说了算"
    )


@pytest.mark.asyncio
async def test_narrow_wide_table_pruning_reports_nothing() -> None:
    """★ 负对照：裁掉的是**别的指标**时不能报系列截断。

    18 列宽表里挑出 `营业额` 是「选指标」，不是「少画了 17 条系列」——
    报出来就是另一句假话。判据只长在宽表 pivot 那一支上。
    """
    header = "省份,营业额,利润,客流,退款率,件单价"
    body = "".join(f"省{i:02d},{100 + i},{10 + i},{5000 + i},{0.01 * i:.3f},{50 + i}\n" for i in range(20))
    art = _FakeArtifact(id="art_666666666666", filename="多指标_def456.csv", path="multi.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(
        artifact_store=_RichArtifactStore([art], {"multi.csv": (header + "\n" + body).encode()}),
        canvas_store=canvas,
    )

    out = await _add_chart(
        {"data_artifact_id": art.id, "hint": "rank", "title": "各省营业额排名", "sort_by": "营业额"},
        ctx,
    )

    spec = canvas.appended_payload["spec"]
    assert spec.get("series_shown") is None and spec.get("series_total") is None
    assert "drawn_series" not in out


@pytest.mark.asyncio
async def test_downsampling_is_reported_back_to_the_model() -> None:
    """★ 降采样也要回给模型：否则它会照着一张稀疏的图声称「图上每天都有」。"""
    # ⚠ Must use **real dates**: CSV path infers types via `_parse_csv_with_types`;
    # D00000-like values become category columns, yielding bar charts (the initial fixture failed here).
    import datetime as dt

    d0 = dt.date(2018, 1, 1)
    csv = "日期,金额\n" + "".join(f"{(d0 + dt.timedelta(days=i)).isoformat()},{1000 + i}\n" for i in range(3200))
    art = _FakeArtifact(id="art_666666666666", filename="日度_fff.csv", path="d.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"d.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart({"data_artifact_id": art.id, "hint": "trend", "title": "每日金额走势"}, ctx)

    spec = canvas.appended_payload["spec"]
    assert spec["kind"] == "line", f"超上限该降采样而不是退表（实际 {spec['kind']}）"
    assert spec["points_shown"] == 3000 and spec["points_total"] == 3200
    assert out["drawn_points"] == 3000 and out["points_total"] == 3200
    assert "降采样" in out["note"], f"模型拿不到提示：{out.get('note')!r}"


@pytest.mark.asyncio
async def test_limit_is_ignored_on_a_time_axis() -> None:
    """A ranking limit must not remove the end of a time series."""
    months = [f"2025年{m}月" for m in range(1, 13)]
    csv = "每月,品牌,销售金额\n" + "".join(f"{m},{b},{100 + i}\n" for i, (m, b) in enumerate((m, b) for m in months for b in "ABCDEF"))
    art = _FakeArtifact(id="art_444444444444", filename="品牌月度_ddd333.csv", path="brand.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"brand.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart(
        {
            "data_artifact_id": art.id,
            "hint": "trend",
            "title": "各品牌月度销售金额",
            "limit": 50,
            "column_types": {"每月": "date"},
        },
        ctx,
    )

    spec = canvas.appended_payload["spec"]
    assert len(spec["rows"]) == 72, "12 个月一个不少"
    assert {r["每月"] for r in spec["rows"]} == set(months)
    assert spec.get("top_n") is None
    assert "已忽略" in out["note"]


@pytest.mark.asyncio
async def test_limit_with_explicit_sort_by_keeps_that_order() -> None:
    """正对照：模型自己指定了排序列，就按它排完取前 N（不改判据）。"""
    csv = "门店,营业额,客流\n" + "".join(f"S{i},{(i * 37) % 100},{i}\n" for i in range(40))
    art = _FakeArtifact(id="art_555555555555", filename="门店_eee444.csv", path="s2.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"s2.csv": csv.encode()}), canvas_store=canvas)

    await _add_chart(
        {"data_artifact_id": art.id, "hint": "rank", "title": "客流 Top 5", "limit": 5, "sort_by": "客流"},
        ctx,
    )

    spec = canvas.appended_payload["spec"]
    assert [r["客流"] for r in spec["rows"]] == [39, 38, 37, 36, 35]


@pytest.mark.asyncio
async def test_limit_picks_top_n_of_the_drawn_measure_not_the_first_numeric_column() -> None:
    """★ 取前 N 要按**图上真正画的那个指标**——`limit` 的截断放在裁列之后。

    复审实测：放在裁列之前时 `_take_top_n` 取的是 CSV 第一个数值列（客流），
    而标题与 `series` 说的是营业额，于是「门店营业额 Top 10」画出的是营业额最低的 10 家。
    """
    csv = "门店,客流,营业额\n" + "".join(f"S{i},{1000 - i},{(i * 37) % 100}\n" for i in range(40))
    art = _FakeArtifact(id="art_666666666666", filename="门店_fff555.csv", path="s3.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"s3.csv": csv.encode()}), canvas_store=canvas)

    await _add_chart(
        {
            "data_artifact_id": art.id,
            "hint": "rank",
            "title": "门店营业额 Top 10",
            "limit": 10,
            "series": ["营业额"],
        },
        ctx,
    )

    drawn = sorted((r["营业额"] for r in canvas.appended_payload["spec"]["rows"]), reverse=True)
    assert drawn == sorted(((i * 37) % 100 for i in range(40)), reverse=True)[:10], f"画的不是营业额最大的 10 家：{drawn}"


@pytest.mark.asyncio
async def test_limit_with_unknown_sort_by_still_takes_the_real_top_n() -> None:
    """★ `sort_by` 指向不存在/非数值的列时，不能静默退回「文件头 N」。

    复审实测：判据与上面那句 `any(c.key == sort_by …)` 不同源，`sort_by="不存在"`
    画出的与真 Top 10 只命中 3/10——正是这次要消灭的那个 bug 的变体。
    """
    csv = "门店,营业额\n" + "".join(f"S{i},{(i * 37) % 100}\n" for i in range(40))
    art = _FakeArtifact(id="art_777777777777", filename="门店_ggg666.csv", path="s4.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"s4.csv": csv.encode()}), canvas_store=canvas)

    await _add_chart(
        {"data_artifact_id": art.id, "hint": "rank", "title": "门店营业额 Top 10", "limit": 10, "sort_by": "不存在的列"},
        ctx,
    )

    drawn = sorted((r["营业额"] for r in canvas.appended_payload["spec"]["rows"]), reverse=True)
    assert drawn == sorted(((i * 37) % 100 for i in range(40)), reverse=True)[:10]


@pytest.mark.asyncio
async def test_same_title_with_different_data_preserves_both_chart_scopes() -> None:
    """A shared title does not make different artifacts interchangeable evidence."""
    full_csv = "门店,营业额\n" + "".join(f"S{i},{(i * 37) % 100}\n" for i in range(40))
    big_csv = "门店,营业额\n" + "".join(f"T{i},{i}\n" for i in range(1000))
    full = _FakeArtifact(id="art_888888888888", filename="全量_hhh777.csv", path="full.csv")
    big = _FakeArtifact(id="art_999999999999", filename="大表_iii888.csv", path="big.csv")
    store = _RichArtifactStore([full, big], {"full.csv": full_csv.encode(), "big.csv": big_csv.encode()})

    class _RememberingCanvasStore(_CapturingCanvasStore):
        """Retain the current turn's charts to exercise deduplication."""

        def __init__(self) -> None:
            super().__init__()
            self.sections: list[Any] = []
            self.patched: list[dict[str, Any]] = []

        async def list_sections_appended_by_turn(self, *_: Any, **__: Any) -> list[Any]:
            return list(self.sections)

        async def append_section(self, **kwargs: Any) -> Any:
            section = await super().append_section(**kwargs)
            section = SimpleNamespace(id=f"sec_{len(self.sections)}", payload=kwargs["payload"], seq=1, rev=1)
            self.sections.append(section)
            return section

        async def patch_section(self, *, section_id: str, patch: dict[str, Any], by_turn_id: str) -> None:
            self.patched.append(patch)

    canvas = _RememberingCanvasStore()
    ctx = _StubCtx(artifact_store=store, canvas_store=canvas)

    await _add_chart({"data_artifact_id": full.id, "hint": "rank", "title": "门店营业额", "limit": 40}, ctx)
    out = await _add_chart({"data_artifact_id": big.id, "hint": "rank", "title": "门店营业额", "limit": 10}, ctx)

    assert "deduped" not in out
    assert len(canvas.sections) == 2
    assert not canvas.patched, "The existing source scope must not be overwritten"


@pytest.mark.asyncio
async def test_the_rank_never_reaches_the_stored_payload() -> None:
    """★ 排名是给**模型**的，不是给存储和前端的。

    它挂在 `spec._series_rank` 上（普通属性，不是 dataclass 字段），
    `spec_to_dict` 走 `dataclasses.asdict` ⇒ 不会被序列化。
    改成 dataclass 字段的话，每张截断过的图都会在落库 payload 与 SSE 里
    多带一串品类名，存量对拍全变（[[feedback_changed_means_rendered_field_changed]]）。
    """
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    cats = [f"类{i:02d}" for i in range(20)]
    csv = "每月,品类,营业额\n" + "".join(f"{m},{c},{(20 - int(c[1:])) * 1000}\n" for m in months for c in cats)
    art = _FakeArtifact(id="art_aaaaaaaaaaab", filename="rank2.csv", path="r2.csv")
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(artifact_store=_RichArtifactStore([art], {"r2.csv": csv.encode()}), canvas_store=canvas)

    out = await _add_chart({"data_artifact_id": art.id, "hint": "trend", "title": "排名不落库"}, ctx)

    assert "按合计从大到小" in (out.get("note") or ""), "前提：这一轮确实截了系列"
    spec = canvas.appended_payload["spec"]
    assert "_series_rank" not in spec, f"排名混进了落库 payload：{sorted(spec)}"
    assert "series_rank" not in spec
