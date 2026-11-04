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
from bowhead_core.canvas_tools import _add_chart
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


@pytest.mark.asyncio
async def test_invalid_reference_is_a_correctable_input_failure() -> None:
    """LLM hallucinates artifact_id='4' (row number) → silent strip."""
    out = await _add_chart(
        {"data_artifact_id": "4", "title": "x"},
        _StubCtx(),
    )
    assert out["error_kind"] == "invalid_args"
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
        {"data_artifact_id": "12", "title": "x"},
        ctx,
    )

    assert out.get("skipped") != "invalid_artifact_id_format"
    assert "artifact 'art_123456abcdef' not found" in out.get("error", "")
    assert artifacts.seen == [("art_123456abcdef", "u")]


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
    assert out["kind"] == "table"


@pytest.mark.asyncio
async def test_simple_chart_ref_does_not_resolve_as_data_artifact() -> None:
    """ν1.5 <chart id="12"> belongs in evidence.chart_section_id, not here."""
    ctx = _StubCtx()
    ctx._ν15_allowlist = _FakeAllowlist({12: ("chart", "sec_chart_real")})

    out = await _add_chart(
        {"data_artifact_id": "12", "title": "x"},
        ctx,
    )

    assert out["skipped"] == "invalid_artifact_id_format"
    assert "chart" in out["hint"]
    assert "chart_section_id" in out["hint"]


@pytest.mark.asyncio
async def test_invalid_reference_retains_corrective_available_source_guidance() -> None:
    """Any non-matching string silent-strips (column name, title fragment, etc)."""
    for bad in ["GMV", "row_1", "art_123", "art_GHIJKL123456", "ART_a1b2c3d4e5f6", "art_a1b2c3d4e5f6_extra"]:
        out = await _add_chart(
            {"data_artifact_id": bad, "title": "x"},
            _StubCtx(),
        )
        assert out["error_kind"] == "invalid_args"
        assert out["skipped"] == "invalid_artifact_id_format", f"{bad!r} expected silent-strip"


@pytest.mark.asyncio
async def test_empty_id_keeps_legacy_required_error() -> None:
    """Empty stays a hard error (different signal — LLM forgot the arg entirely)."""
    out = await _add_chart(
        {"data_artifact_id": "", "title": "x"},
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
        {"data_artifact_id": "art_a1b2c3d4e5f6", "title": "x"},
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

    out = await _add_chart({"data_artifact_id": "77", "title": "x"}, ctx)

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

    out = await _add_chart({"data_artifact_id": "77", "title": "x"}, ctx)

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

    out = await _add_chart({"data_artifact_id": "77", "title": "x"}, ctx)

    assert out["skipped"] == "invalid_artifact_id_format"
    assert "不是合法格式" in out["hint"]


@pytest.mark.asyncio
async def test_bare_hex_id_is_coerced_instead_of_rejected() -> None:
    """Repair a valid synthetic hexadecimal ID missing only the Artifact prefix."""
    store = _FakeArtifactStore()
    out = await _add_chart(
        {"data_artifact_id": "123456abcdef", "title": "x"},
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
            {"data_artifact_id": bad, "title": "x"},
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

    await _add_chart({"data_artifact_id": full.id, "title": "门店营业额"}, ctx)
    out = await _add_chart({"data_artifact_id": big.id, "title": "门店营业额"}, ctx)

    assert "deduped" not in out
    assert len(canvas.sections) == 2
    assert not canvas.patched, "The existing source scope must not be overwritten"
