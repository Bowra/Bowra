"""source-accessibility 批 1 —— 归属判定模块的三道闸（tech-design §6）。

闸 A：CitationKind 每个成员必须有解析器（新增 kind 不配解析器 ⇒ 这里红，不进生产）。
闸 B：每个解析器成对测试 —— OWNED ⇒ 有 display；FOREIGN ⇒ 一个字段都不释放
     （互为 positive control，安全面松掉任何一半都会红）。
闸 E：「按字段名扫 id」的实现只允许存在于显式 allowlist（防第四份实现）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from bowhead_core.citations import CitationKind
from bowhead_core.source_attribution import (
    RESOLVER_FACTORIES,
    Attribution,
    AttributionStores,
    InFlightIndex,
    ReportScope,
    attribute_refs,
)

# ---------------------------------------------------------------- fakes


class _FakeArtifactStore:
    def __init__(self, artifacts: list[SimpleNamespace] | None = None, *, raises: bool = False) -> None:
        self._artifacts = {a.id: a for a in (artifacts or [])}
        self._raises = raises

    async def get_many(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        if self._raises:
            raise RuntimeError("boom")
        return [self._artifacts[a] for a in artifact_ids if a in self._artifacts]


class _FakeCanvasStore:
    def __init__(self, canvas_by_section: dict[str, str] | None = None, *, raises: bool = False) -> None:
        self._map = canvas_by_section or {}
        self._raises = raises

    async def get_section_canvas_id(self, section_id: str) -> str | None:
        if self._raises:
            raise RuntimeError("boom")
        return self._map.get(section_id)


class _FakeKbRowsStore:
    def __init__(self, rows: dict[str, SimpleNamespace] | None = None) -> None:
        self._rows = rows or {}

    async def get(self, row_id: str) -> SimpleNamespace | None:
        return self._rows.get(row_id)


def _art(artifact_id: str, *, created_by_turn: str | None = None, canvas_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=artifact_id, created_by_turn=created_by_turn, canvas_id=canvas_id)


def _scope(**overrides: object) -> ReportScope:
    values: dict = {
        "workspace_id": "ws_1",
        "session_id": "s_1",
        "canvas_id": "cv_1",
        "turn_id": "t_1",
        "session_turn_ids": frozenset({"t_1", "t_0"}),
    }
    values.update(overrides)
    return ReportScope(**values)


def _assert_released_nothing(verdict) -> None:
    """FOREIGN/MISSING/UNSUPPORTED 的安全面：display 与 exit_candidate 一个都不许有。"""
    assert verdict.attribution is not Attribution.OWNED
    assert verdict.display is None
    assert verdict.exit_candidate is None


# ---------------------------------------------------------------- Gate A


@pytest.mark.unit
def test_gate_a_every_citation_kind_has_a_resolver() -> None:
    kinds = set(get_args(CitationKind))
    assert set(RESOLVER_FACTORIES) == kinds, "CitationKind 与 RESOLVER_FACTORIES 必须一一对应（闸 A，设计 §6-A）"


# ---------------------------------------------------------------- Gate B: artifact family (data_ref / tool_output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact", "scope_overrides", "expected_reason"),
    [
        (_art("art_a", created_by_turn="t_0"), {}, "owned_by_session_turn"),
        (_art("art_a", canvas_id="cv_1"), {}, "owned_by_canvas"),
        (_art("art_a"), {"source_ref_artifact_ids": frozenset({"art_a"})}, "owned_by_source_ref"),
        (_art("art_a"), {"rendered_artifact_ids": frozenset({"art_a"})}, "owned_by_rendered"),
    ],
)
async def test_artifact_family_owned_arms(artifact, scope_overrides, expected_reason) -> None:
    stores = AttributionStores(artifact_store=_FakeArtifactStore([artifact]))
    out = await attribute_refs([("data_ref", "art_a")], scope=_scope(**scope_overrides), stores=stores)
    verdict = out[("data_ref", "art_a")]
    assert verdict.attribution is Attribution.OWNED
    assert verdict.reason_code == expected_reason
    assert verdict.exit_candidate == "art_a"  # Only artifact family offers byte-exit candidates.


@pytest.mark.asyncio
async def test_artifact_family_foreign_releases_nothing() -> None:
    # Exists, but all four ownership arms miss (other turn/canvas) → FOREIGN, strip all.
    stores = AttributionStores(artifact_store=_FakeArtifactStore([_art("art_x", created_by_turn="t_other", canvas_id="cv_other")]))
    out = await attribute_refs([("data_ref", "art_x")], scope=_scope(), stores=stores)
    verdict = out[("data_ref", "art_x")]
    assert verdict.attribution is Attribution.FOREIGN
    assert verdict.reason_code == "foreign_artifact"
    _assert_released_nothing(verdict)


@pytest.mark.asyncio
async def test_artifact_family_missing_and_failure_modes_fail_closed() -> None:
    scope = _scope(session_turn_ids=frozenset({"t_1"}))
    # Absent from store.
    out = await attribute_refs(
        [("tool_output", "art_gone")],
        scope=scope,
        stores=AttributionStores(artifact_store=_FakeArtifactStore()),
    )
    assert out[("tool_output", "art_gone")].reason_code == "artifact_missing"
    _assert_released_nothing(out[("tool_output", "art_gone")])
    # Store raises.
    out = await attribute_refs(
        [("data_ref", "art_a")],
        scope=scope,
        stores=AttributionStores(artifact_store=_FakeArtifactStore(raises=True)),
    )
    assert out[("data_ref", "art_a")].reason_code == "resolver_error"
    # Store missing.
    out = await attribute_refs([("data_ref", "art_a")], scope=scope, stores=AttributionStores())
    assert out[("data_ref", "art_a")].reason_code == "store_unavailable"
    _assert_released_nothing(out[("data_ref", "art_a")])


# ---------------------------------------------------------------- Gate B: chart_section


@pytest.mark.asyncio
async def test_chart_section_owned_by_canvas_gets_anchor_but_never_exit() -> None:
    stores = AttributionStores(canvas_store=_FakeCanvasStore({"sec_a": "cv_1"}))
    out = await attribute_refs([("chart_section", "sec_a")], scope=_scope(), stores=stores)
    verdict = out[("chart_section", "sec_a")]
    assert verdict.attribution is Attribution.OWNED
    assert verdict.reason_code == "owned_by_canvas"
    assert verdict.display is not None and verdict.display.anchor_id == "sec_a"
    assert verdict.exit_candidate is None  # Sections never have byte exits (design §3.3).


@pytest.mark.asyncio
async def test_chart_section_foreign_canvas_releases_nothing() -> None:
    stores = AttributionStores(canvas_store=_FakeCanvasStore({"sec_a": "cv_other"}))
    out = await attribute_refs([("chart_section", "sec_a")], scope=_scope(), stores=stores)
    verdict = out[("chart_section", "sec_a")]
    assert verdict.attribution is Attribution.FOREIGN
    assert verdict.reason_code == "foreign_section"
    _assert_released_nothing(verdict)


@pytest.mark.asyncio
async def test_chart_section_in_flight_owned_without_exit() -> None:
    # Unpersisted, but ID in server-minted in-flight index for this turn → OWNED (display during streaming).
    stores = AttributionStores(canvas_store=_FakeCanvasStore({}))
    out = await attribute_refs(
        [("chart_section", "sec_live")],
        scope=_scope(),
        stores=stores,
        in_flight=InFlightIndex(section_ids=frozenset({"sec_live"})),
    )
    verdict = out[("chart_section", "sec_live")]
    assert verdict.attribution is Attribution.OWNED
    assert verdict.reason_code == "owned_in_flight"
    assert verdict.display is not None and verdict.display.anchor_id == "sec_live"
    assert verdict.exit_candidate is None  # In-flight never offers exit candidate (§3.4-2).


@pytest.mark.asyncio
async def test_chart_section_scope_without_canvas_is_missing_not_foreign() -> None:
    # Review F9: "scope has no canvas" ≠ "section belongs to another canvas". FOREIGN reason also
    # signals security (batch 2 event projections build scope before canvas exists); false matches contaminate observations.
    stores = AttributionStores(canvas_store=_FakeCanvasStore({"sec_a": "cv_1"}))
    out = await attribute_refs(
        [("chart_section", "sec_a")],
        scope=_scope(canvas_id=None),
        stores=stores,
    )
    verdict = out[("chart_section", "sec_a")]
    assert verdict.attribution is Attribution.MISSING
    assert verdict.reason_code == "scope_canvas_unavailable"
    _assert_released_nothing(verdict)


@pytest.mark.asyncio
async def test_chart_section_missing_and_lookup_failure_fail_closed() -> None:
    out = await attribute_refs(
        [("chart_section", "sec_gone")],
        scope=_scope(),
        stores=AttributionStores(canvas_store=_FakeCanvasStore({})),
    )
    assert out[("chart_section", "sec_gone")].reason_code == "section_missing"
    # Lookup exceptions **never use in-flight fallback** (unprovable ownership means no access).
    out = await attribute_refs(
        [("chart_section", "sec_live")],
        scope=_scope(),
        stores=AttributionStores(canvas_store=_FakeCanvasStore(raises=True)),
        in_flight=InFlightIndex(section_ids=frozenset({"sec_live"})),
    )
    assert out[("chart_section", "sec_live")].reason_code == "resolver_error"
    _assert_released_nothing(out[("chart_section", "sec_live")])


# ---------------------------------------------------------------- Gate B: kb_row


@pytest.mark.asyncio
async def test_kb_row_owned_by_workspace_label_only() -> None:
    rows = {"row_a": SimpleNamespace(row_id="row_a", workspace_id="ws_1")}
    out = await attribute_refs(
        [("kb_row", "row_a")],
        scope=_scope(),
        stores=AttributionStores(kb_rows_store=_FakeKbRowsStore(rows)),
    )
    verdict = out[("kb_row", "row_a")]
    assert verdict.attribution is Attribution.OWNED
    assert verdict.reason_code == "owned_by_workspace"
    assert verdict.exit_candidate is None  # §8-1 decision: kb_row offers labels only, no exit.


@pytest.mark.asyncio
async def test_kb_row_foreign_workspace_and_missing_fail_closed() -> None:
    rows = {"row_a": SimpleNamespace(row_id="row_a", workspace_id="ws_other")}
    stores = AttributionStores(kb_rows_store=_FakeKbRowsStore(rows))
    out = await attribute_refs([("kb_row", "row_a"), ("kb_row", "row_gone")], scope=_scope(), stores=stores)
    assert out[("kb_row", "row_a")].attribution is Attribution.FOREIGN
    _assert_released_nothing(out[("kb_row", "row_a")])
    assert out[("kb_row", "row_gone")].reason_code == "row_missing"


# ---------------------------------------------------------------- Gate B: unsupported / unknown


@pytest.mark.asyncio
async def test_unsupported_kinds_are_explicit_not_silent() -> None:
    out = await attribute_refs(
        [("web_clip", "art_clip"), ("made_up_kind", "x_1")],
        scope=_scope(),
        stores=AttributionStores(),
    )
    assert out[("web_clip", "art_clip")].reason_code == "unsupported_kind"
    # Historical DB strings may be arbitrary: never guess unknown kinds; UNSUPPORTED + observable.
    assert out[("made_up_kind", "x_1")].reason_code == "unknown_kind"
    for verdict in out.values():
        _assert_released_nothing(verdict)


# ---------------------------------------------------------------- Entry point fail-closed behavior


@pytest.mark.asyncio
async def test_attribute_refs_covers_every_ref_even_if_resolver_underreports() -> None:
    class _Underreporting(_FakeCanvasStore):
        async def get_section_canvas_id(self, section_id: str) -> str | None:
            return "cv_1" if section_id == "sec_a" else None

    stores = AttributionStores(canvas_store=_Underreporting())
    refs = [("chart_section", "sec_a"), ("chart_section", "sec_b"), ("chart_section", "sec_a")]
    out = await attribute_refs(refs, scope=_scope(), stores=stores)
    # Every deduplicated ref still has a verdict; repeated IDs share the same one.
    assert set(out) == {("chart_section", "sec_a"), ("chart_section", "sec_b")}
    assert out[("chart_section", "sec_a")].attribution is Attribution.OWNED
    assert out[("chart_section", "sec_b")].attribution is Attribution.MISSING


# ---------------------------------------------------------------- Gate E


@pytest.mark.unit
def test_gate_e_field_name_scanning_only_in_allowlisted_files() -> None:
    """防第四份实现（设计 §6-E）：「按字段名递归扫 id」的判定只允许出现在 allowlist。

    批 3 删除 gateway 的 `_collect_artifact_ids` 后，本 allowlist 收缩到只剩
    typed extractor 一处；在那之前它钉住的不变量是「不许再多一处」。
    """
    packages_root = Path(__file__).resolve().parents[2]
    assert packages_root.name == "packages", packages_root
    markers = ("_ARTIFACT_SCALAR_KEYS", "def _collect_artifact_ids")
    allowlist = {
        "bowhead_gateway/src/bowhead_gateway/source_access_projection.py",  # Remove from this list after batch 3 deletion.
    }
    offenders: set[str] = set()
    for src_dir in packages_root.glob("*/src"):
        for path in src_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(marker in text for marker in markers):
                offenders.add(str(path.relative_to(packages_root)))
    assert offenders == allowlist, f"按字段名扫 id 的实现出现在 allowlist 之外: {offenders ^ allowlist}"


@pytest.mark.asyncio
async def test_artifact_whose_blob_is_missing_is_reported_missing_not_owned() -> None:
    """对账标为 missing（行在、原文件已丢失）：即使归属臂命中，也按来源已失效处理；对照：ready 时 OWNED。"""
    scope = _scope(session_turn_ids=frozenset({"t_1"}))
    gone = _art("art_a", created_by_turn="t_1")
    gone.status = "missing"
    out = await attribute_refs(
        [("tool_output", "art_a")],
        scope=scope,
        stores=AttributionStores(artifact_store=_FakeArtifactStore([gone])),
    )
    verdict = out[("tool_output", "art_a")]
    assert (verdict.attribution, verdict.reason_code) == (Attribution.MISSING, "artifact_blob_missing")
    _assert_released_nothing(verdict)

    gone.status = "ready"
    out = await attribute_refs(
        [("tool_output", "art_a")],
        scope=scope,
        stores=AttributionStores(artifact_store=_FakeArtifactStore([gone])),
    )
    assert out[("tool_output", "art_a")].attribution is Attribution.OWNED
