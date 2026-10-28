"""Current Canvas content projection and token contract."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from bowhead_core.canvas_export_projection import (
    SURFACE_KIND_POLICIES,
    build_canvas_content_projection,
    build_canvas_surface_projection,
    compute_surface_projection_token,
)
from bowhead_core.canvas_store import CanvasStore, Section

_SURFACE_FIXTURE = Path(__file__).resolve().parents[3] / "eval" / "fixtures" / "canvas_export" / "surface_projection_v1.json"


@pytest.fixture
async def store(tmp_path) -> CanvasStore:
    canvas_store = CanvasStore(tmp_path / "canvas.db")
    await canvas_store.initialize()
    return canvas_store


@pytest.mark.asyncio
async def test_list_export_projection_filters_current_content_without_changing_page_history(store: CanvasStore) -> None:
    canvas = await store.ensure_canvas(
        session_id="s_projection_boundary",
        workspace_id="ws_1",
        user_id="u_1",
        title="Current report",
    )
    report = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Current report body"},
        by_turn_id="t_current",
    )
    data = await store.append_section(
        canvas_id=canvas.id,
        kind="data_table",
        payload={"columns": ["region"], "rows": [["APAC"]]},
        by_turn_id="t_current",
    )
    hidden = await store.append_section(
        canvas_id=canvas.id,
        kind="sql",
        payload={"sql": "SELECT hidden"},
        by_turn_id="t_hidden",
    )
    history = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Superseded report"},
        by_turn_id="t_history",
    )
    before_lifecycle_change = build_canvas_content_projection(
        title=canvas.title,
        sections=await store.list_export_projection(canvas.id),
    )
    await store.mark_turn_hidden_for_chat_mode("t_hidden", "chat-only turn")
    await store.mark_sections_superseded(
        section_ids=[history.id],
        superseded_by_turn="t_current",
    )

    exported = await store.list_export_projection(canvas.id)
    page_default = await store.list_sections(canvas.id)
    page_with_hidden = await store.list_sections(canvas.id, include_hidden=True)

    assert [section.id for section in exported] == [report.id, data.id]
    assert [section.id for section in page_default] == [report.id, data.id, history.id]
    assert [section.id for section in page_with_hidden] == [report.id, data.id, hidden.id, history.id]
    after_lifecycle_change = build_canvas_content_projection(
        title=canvas.title,
        sections=exported,
    )
    assert after_lifecycle_change.projection_token != before_lifecycle_change.projection_token

    projection_state = await store.read_canvas_projection_state(canvas.id)
    assert projection_state is not None
    state_canvas, state_sections = projection_state
    state_content = build_canvas_content_projection(
        title=state_canvas.title,
        sections=state_sections,
    )
    state_surface = build_canvas_surface_projection(
        content_projection=state_content,
        sections=state_sections,
    )
    assert [(item.section_id, item.reason) for item in state_surface.exclusions] == [
        (hidden.id, "hidden_for_chat_mode"),
        (history.id, "superseded_history"),
    ]


@pytest.mark.asyncio
async def test_content_projection_token_is_stable_and_tracks_content_identity(store: CanvasStore) -> None:
    canvas = await store.ensure_canvas(
        session_id="s_projection_token",
        workspace_id="ws_1",
        user_id="u_1",
        title="Revenue report",
    )
    first = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Revenue grew"},
    )
    second = await store.append_section(
        canvas_id=canvas.id,
        kind="sql",
        payload={"sql": "SELECT revenue FROM facts"},
    )

    sections = await store.list_export_projection(canvas.id)
    projection = build_canvas_content_projection(title=canvas.title, sections=sections)
    reversed_projection = build_canvas_content_projection(title=canvas.title, sections=list(reversed(sections)))

    assert projection.contract_version == 1
    assert projection.projection_token.startswith("sha256:")
    assert len(projection.projection_token) == len("sha256:") + 64
    assert projection.projection_token == reversed_projection.projection_token
    assert [entry.section_id for entry in projection.sections] == [first.id, second.id]
    timestamp_only = [replace(section, updated_at="2099-01-01T00:00:00+00:00") for section in sections]
    assert (
        build_canvas_content_projection(
            title=canvas.title,
            sections=timestamp_only,
        ).projection_token
        == projection.projection_token
    )

    patched = await store.patch_section(
        section_id=first.id,
        patch={"markdown": "Revenue grew 12%"},
        expected_rev=first.rev,
    )
    assert patched is not None
    after_patch = build_canvas_content_projection(
        title=canvas.title,
        sections=await store.list_export_projection(canvas.id),
    )
    assert after_patch.projection_token != projection.projection_token

    await store.reorder_sections(canvas_id=canvas.id, ordered_ids=[second.id, first.id])
    after_reorder = build_canvas_content_projection(
        title=canvas.title,
        sections=await store.list_export_projection(canvas.id),
    )
    assert after_reorder.projection_token != after_patch.projection_token

    await store.set_title(canvas.id, "Revenue report — final")
    after_title = build_canvas_content_projection(
        title="Revenue report — final",
        sections=await store.list_export_projection(canvas.id),
    )
    assert after_title.projection_token != after_reorder.projection_token

    await store.set_quarantine(
        section_id=first.id,
        quarantined=True,
        reason="number mismatch",
    )
    after_quarantine = build_canvas_content_projection(
        title="Revenue report — final",
        sections=await store.list_export_projection(canvas.id),
    )
    assert after_quarantine.projection_token != after_title.projection_token


def test_surface_projection_matches_shared_kind_contract_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared fixture's expected order is the canonical rank order (seq not authoritative).
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    fixture = json.loads(_SURFACE_FIXTURE.read_text(encoding="utf-8"))
    sections = [
        Section(
            id=item["id"],
            canvas_id="cv_fixture",
            seq=item["seq"],
            kind=item["kind"],
            payload=item["payload"],
            rev=item["rev"],
            created_at="2026-07-16T00:00:00+00:00",
            updated_at="2026-07-16T00:00:00+00:00",
            quarantined=item.get("quarantined", False),
            hidden_for_chat_mode=item.get("hidden_for_chat_mode", False),
            superseded=item.get("superseded", False),
        )
        for item in fixture["sections"]
    ]
    content = build_canvas_content_projection(title=fixture["title"], sections=sections)
    surface = build_canvas_surface_projection(
        content_projection=content,
        sections=sections,
    )

    fixture_kinds = {section.kind for section in sections}
    assert fixture_kinds == set(SURFACE_KIND_POLICIES)
    assert len(surface.entries) == 22
    assert [asdict(entry) for entry in surface.entries] == fixture["expected_entries"]
    assert [asdict(exclusion) for exclusion in surface.exclusions] == fixture["expected_exclusions"]
    content_ids = {entry.section_id for entry in content.sections}
    accounted_ids = {entry.section_id for entry in surface.entries} | {
        exclusion.section_id for exclusion in surface.exclusions if exclusion.section_id in content_ids
    }
    assert accounted_ids == content_ids
    assert content.projection_token == fixture["projection_token"]
    assert surface.surface_token == fixture["surface_token"]

    next_contract_token = compute_surface_projection_token(
        surface_contract_version=surface.contract_version + 1,
        content_projection_token=content.projection_token,
        entries=surface.entries,
        exclusions=surface.exclusions,
    )
    assert next_contract_token != surface.surface_token

    changed_sections = [replace(section, rev=section.rev + 1) if section.id == "sec_chart" else section for section in sections]
    with pytest.raises(ValueError, match="content projection section changed"):
        build_canvas_surface_projection(
            content_projection=content,
            sections=changed_sections,
        )


@pytest.mark.asyncio
async def test_narrative_figure_anchors_project_as_embedded(store: CanvasStore) -> None:
    """embed-inline r3: narrative 的 [figure:id] 锚构成 report_embedded 关系,
    与页面 canvasToPMDoc 内联行为对齐 (此前只认 insight evidence 链)。"""
    canvas = await store.ensure_canvas(session_id="s_anchor", workspace_id="ws_1", user_id="u_1", title="t")
    c1 = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"kind": "bar", "rows": []}})
    c2 = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"kind": "line", "rows": []}})
    free = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"kind": "pie", "rows": []}})
    host = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": f"开头 [figure:{c1.id}] 中段 [figure:{c2.id}] [figure:{c1.id}] 尾"},
    )
    sections = await store.list_sections(canvas.id)
    content = build_canvas_content_projection(title="t", sections=sections)
    surface = build_canvas_surface_projection(content_projection=content, sections=sections)

    by_id = {entry.section_id: entry for entry in surface.entries}
    assert by_id[c1.id].placement == "report_embedded"
    assert by_id[c1.id].parent_section_id == host.id
    assert by_id[c2.id].placement == "report_embedded"
    assert by_id[c2.id].parent_section_id == host.id
    # Unreferenced visuals remain standalone report_body.
    assert by_id[free.id].placement == "report_body"
    assert by_id[free.id].parent_section_id is None
    # Embedded entries immediately follow their host, in anchor appearance order (duplicates removed).
    order = [entry.section_id for entry in sorted(surface.entries, key=lambda e: e.order)]
    host_pos = order.index(host.id)
    assert order[host_pos + 1 : host_pos + 3] == [c1.id, c2.id]


@pytest.mark.asyncio
async def test_insight_evidence_wins_over_narrative_anchor(store: CanvasStore) -> None:
    """同一图同时被 insight evidence 与 narrative 锚引用: insight 先到先得。"""
    canvas = await store.ensure_canvas(session_id="s_prec", workspace_id="ws_1", user_id="u_1", title="t")
    chart = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"kind": "bar", "rows": []}})
    insight = await store.append_section(
        canvas_id=canvas.id,
        kind="insight_card",
        payload={"title": "i", "evidence": [{"chart_section_id": chart.id}]},
    )
    await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": f"正文 [figure:{chart.id}]"},
    )
    sections = await store.list_sections(canvas.id)
    content = build_canvas_content_projection(title="t", sections=sections)
    surface = build_canvas_surface_projection(content_projection=content, sections=sections)
    entry = next(e for e in surface.entries if e.section_id == chart.id)
    assert entry.placement == "report_embedded"
    assert entry.parent_section_id == insight.id


@pytest.mark.asyncio
async def test_anchor_to_non_visual_or_missing_is_ignored(store: CanvasStore) -> None:
    canvas = await store.ensure_canvas(session_id="s_ghost", workspace_id="ws_1", user_id="u_1", title="t")
    code = await store.append_section(canvas_id=canvas.id, kind="code_block", payload={"code": "x"})
    host = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": f"[figure:sec_missing] [figure:{code.id}] 正文"},
    )
    sections = await store.list_sections(canvas.id)
    content = build_canvas_content_projection(title="t", sections=sections)
    surface = build_canvas_surface_projection(content_projection=content, sections=sections)
    by_id = {entry.section_id: entry for entry in surface.entries}
    # code_block is outside the allowlist → stays code_tab; phantom IDs have no entry.
    assert by_id[code.id].placement == "code_tab"
    assert by_id[host.id].placement == "report_body"
    assert "sec_missing" not in by_id


def _section(section_id: str, kind: str, seq: int, payload: dict | None = None, **flags: bool) -> Section:
    return Section(
        id=section_id,
        canvas_id="cv_unit",
        seq=seq,
        kind=kind,
        payload=payload or {},
        rev=1,
        created_at="2026-09-29T00:00:00+00:00",
        updated_at="2026-09-29T00:00:00+00:00",
        quarantined=flags.get("quarantined", False),
        hidden_for_chat_mode=flags.get("hidden_for_chat_mode", False),
        superseded=flags.get("superseded", False),
    )


def _surface(sections: list[Section]):
    content = build_canvas_content_projection(title="t", sections=sections)
    return build_canvas_surface_projection(content_projection=content, sections=sections)


def _chart(section_id: str, seq: int, **flags: bool) -> Section:
    return _section(section_id, "chart", seq, {"spec": {"kind": "bar", "rows": []}}, **flags)


def _insight(section_id: str, seq: int, *refs: str) -> Section:
    return _section(section_id, "insight_card", seq, {"title": "i", "evidence": [{"kind": "chart", "chart_section_id": ref} for ref in refs]})


@pytest.mark.unit
def test_unreferenced_chart_stays_top_level_when_report_has_insight_card() -> None:
    """决议 #4：页面把没被 evidence 引用的图照常顶层显示，导出不得把它记成排除项。"""
    surface = _surface(
        [
            _chart("sec_cited", 1),
            _chart("sec_orphan", 2),
            _insight("sec_insight", 3, "sec_cited"),
        ]
    )

    assert [(e.section_id, e.placement, e.parent_section_id) for e in surface.entries] == [
        ("sec_orphan", "report_body", None),
        ("sec_insight", "report_body", None),
        ("sec_cited", "report_embedded", "sec_insight"),
    ]
    assert surface.exclusions == ()


@pytest.mark.unit
def test_chart_cited_by_several_hosts_is_planned_once() -> None:
    surface = _surface(
        [
            _chart("sec_cited", 1),
            _insight("sec_first", 2, "sec_cited", "sec_cited"),
            _insight("sec_second", 3, "sec_cited"),
            _section("sec_prose", "narrative", 4, {"markdown": "[figure:sec_cited]"}),
        ]
    )

    planned = [entry.section_id for entry in surface.entries]
    assert planned.count("sec_cited") == 1
    cited = next(entry for entry in surface.entries if entry.section_id == "sec_cited")
    assert (cited.placement, cited.parent_section_id) == ("report_embedded", "sec_first")


@pytest.mark.unit
def test_charts_outside_the_current_report_stay_excluded() -> None:
    surface = _surface(
        [
            _insight("sec_insight", 1, "sec_live"),
            _chart("sec_live", 2),
            _chart("sec_replaced", 3, superseded=True),
            _chart("sec_chat_only", 4, hidden_for_chat_mode=True),
            _chart("sec_rejected", 5, quarantined=True),
        ]
    )

    assert [entry.section_id for entry in surface.entries] == ["sec_insight", "sec_live"]
    assert [(item.section_id, item.reason) for item in surface.exclusions] == [
        ("sec_replaced", "superseded_history"),
        ("sec_chat_only", "hidden_for_chat_mode"),
        ("sec_rejected", "quarantined_rejected"),
    ]


@pytest.mark.unit
def test_multi_report_top_level_order_matches_page_segments(monkeypatch) -> None:
    """多腿计划每章各带 h1：页面按报告分段各自排序，导出不得全局强排把两章互插。"""
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    surface = _surface(
        [
            _section("sec_h1_a", "h1", 1, {"text": "A"}),
            _section("sec_body_a", "narrative", 2, {"markdown": "a"}),
            _section("sec_kpi_a", "kpi_row", 3, {"cells": []}),
            _section("sec_h1_b", "h1", 4, {"text": "B"}),
            _chart("sec_chart_b", 5),
            _section("sec_kpi_b", "kpi_row", 6, {"cells": []}),
        ]
    )

    assert [entry.section_id for entry in surface.entries] == [
        "sec_h1_a",
        "sec_kpi_a",
        "sec_body_a",
        "sec_h1_b",
        "sec_kpi_b",
        "sec_chart_b",
    ]


@pytest.mark.unit
def test_eyebrow_before_anchored_chart_stays_with_previous_report(monkeypatch) -> None:
    """页面分段时锚引用的图还在序列里（canvasToPMDoc 才隐藏它），eyebrow 与第二个 h1
    不相邻 ⇒ 不随 h1 进新段。导出若先摘掉锚图再分段，eyebrow 会被归到另一章。"""
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    surface = _surface(
        [
            _section("sec_h1_a", "h1", 1, {"text": "A"}),
            _section("sec_body_a", "narrative", 2, {"markdown": "a"}),
            _section("sec_eyebrow_b", "eyebrow", 3, {"text": "B"}),
            _chart("sec_anchored", 4),
            _section("sec_h1_b", "h1", 5, {"text": "B"}),
            _section("sec_body_b", "narrative", 6, {"markdown": "[figure:sec_anchored]"}),
        ]
    )

    assert [(e.section_id, e.parent_section_id) for e in surface.entries] == [
        ("sec_eyebrow_b", None),
        ("sec_h1_a", None),
        ("sec_body_a", None),
        ("sec_h1_b", None),
        ("sec_body_b", None),
        ("sec_anchored", "sec_body_b"),
    ]


@pytest.mark.unit
def test_eyebrow_before_insight_cited_chart_joins_next_report(monkeypatch) -> None:
    """insight 引用的图在页面分段**之前**就被折掉，eyebrow 与第二个 h1 因此相邻、随 h1 进新段。"""
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    surface = _surface(
        [
            _section("sec_h1_a", "h1", 1, {"text": "A"}),
            _section("sec_eyebrow_b", "eyebrow", 2, {"text": "B"}),
            _chart("sec_cited", 3),
            _section("sec_h1_b", "h1", 4, {"text": "B"}),
            _insight("sec_insight_b", 5, "sec_cited"),
        ]
    )

    assert [(e.section_id, e.parent_section_id) for e in surface.entries] == [
        ("sec_h1_a", None),
        ("sec_eyebrow_b", None),
        ("sec_h1_b", None),
        ("sec_insight_b", None),
        ("sec_cited", "sec_insight_b"),
    ]


@pytest.mark.unit
def test_multi_report_keeps_stored_order_when_seq_is_authoritative(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "1")
    surface = _surface(
        [
            _section("sec_code", "code_block", 1, {"code": "x"}),
            _section("sec_body_a", "narrative", 2, {"markdown": "a"}),
            _section("sec_h1_a", "h1", 3, {"text": "A"}),
            _chart("sec_chart_b", 4),
            _section("sec_h1_b", "h1", 5, {"text": "B"}),
            _section("sec_kpi_b", "kpi_row", 6, {"cells": []}),
        ]
    )

    assert [entry.section_id for entry in surface.entries] == [
        "sec_body_a",
        "sec_h1_a",
        "sec_chart_b",
        "sec_h1_b",
        "sec_kpi_b",
        "sec_code",
    ]


@pytest.mark.unit
def test_export_projection_honors_seq_authority_flag(monkeypatch) -> None:
    """★seq 权威化翻开后 V2 导出不得再按 rank 强排（否则 V2 段序 ≠ 画布段序）。

    画布（report-view preprocessSections）与 legacy 导出（_canonical_export_order）
    都在 flag 开时改信 seq；这里此前**无条件**强排，flag 一翻就三路分叉——用户拖出
    来的顺序在 V2 导出里被打回（review 逮到，flag 当时 dark 故未爆）。
    """
    sections = [
        _section("sec_body", "narrative", 1, {"markdown": "x"}),
        _section("sec_title", "h1", 2, {"text": "t"}),
    ]

    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    assert [entry.section_id for entry in _surface(sections).entries] == ["sec_title", "sec_body"]  # Forced sorting: title first

    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "1")
    assert [entry.section_id for entry in _surface(sections).entries] == ["sec_body", "sec_title"]  # Trust seq: keep database order
