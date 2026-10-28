"""Phase 1.6 G4 — `_collect_inline_artifacts` helper contract.

The helper is private to `lead_orchestrator`. We test the public-shape
contract that FE `ChatBubbleNew.artifacts={...}` relies on, using a real
CanvasStore (no network, file-backed sqlite) so the SQL join is exercised.

Invariants:
1. chart sections → {kind: "chart", id, spec} (spec comes from payload.spec)
2. kpi_row sections → {kind: "kpi", id, data: {id, kind, cells, ...}}
3. data_table sections → {kind: "table", id, data: {id, kind, caption, ...}}
4. Sections from other turns excluded.
5. Unrelated kinds (narrative, eyebrow, …) excluded.
6. Missing canvas_store on ctx → empty list (best-effort, no crash).
7. chart section with no `spec` payload skipped.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents.lead_orchestrator import (
    _DECK_SLIDE_COUNT_MAX_BYTES,
    _collect_inline_artifacts,
    _deliverable_inline_artifact,
    _file_type_label,
    _inject_inline_artifacts,
    _pptx_slide_count,
)
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "ia.db")
    await s.initialize()
    return s


@pytest.fixture
async def art_store(tmp_path: Path) -> ArtifactStore:
    backend = LocalBackend(root_dir=tmp_path / "files")
    s = ArtifactStore(backend=backend, db_path=tmp_path / "art.db")
    await s.initialize()
    return s


_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _fake_pptx(n_slides: int) -> bytes:
    """A minimal .pptx (zip) with ``n_slides`` slide parts — enough for the
    stdlib slide counter; not a renderable deck."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        for i in range(1, n_slides + 1):
            z.writestr(f"ppt/slides/slide{i}.xml", "<s/>")
            # rels live under ppt/slides/_rels/slideN.xml.rels — must NOT count
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", "<r/>")
    return buf.getvalue()


async def test_collect_chart_kpi_table_shapes(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s_ia", workspace_id="w", user_id="u")
    chart = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {"type": "bar", "x": ["Q1", "Q2"]}},
        by_turn_id="t_ia",
    )
    kpi = await store.append_section(
        canvas_id=c.id,
        kind="kpi_row",
        payload={"cells": [{"label": "GMV", "value": "5.96B"}]},
        by_turn_id="t_ia",
    )
    table = await store.append_section(
        canvas_id=c.id,
        kind="data_table",
        payload={
            "caption": "Top 5",
            "columns": [{"key": "k"}],
            "rows": [{"k": "v"}],
        },
        by_turn_id="t_ia",
    )

    ctx = SimpleNamespace(canvas_store=store)
    arts = await _collect_inline_artifacts(ctx, "t_ia")
    assert len(arts) == 3
    by_id = {a["id"]: a for a in arts}

    a_chart = by_id[chart.id]
    assert a_chart["kind"] == "chart"
    assert a_chart["spec"] == {"type": "bar", "x": ["Q1", "Q2"]}

    a_kpi = by_id[kpi.id]
    assert a_kpi["kind"] == "kpi"
    assert a_kpi["data"]["kind"] == "kpi_row"
    assert a_kpi["data"]["id"] == kpi.id
    assert a_kpi["data"]["cells"] == [{"label": "GMV", "value": "5.96B"}]

    a_table = by_id[table.id]
    assert a_table["kind"] == "table"
    assert a_table["data"]["kind"] == "data_table"
    assert a_table["data"]["caption"] == "Top 5"


async def test_other_turn_sections_excluded(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s_ia2", workspace_id="w", user_id="u")
    mine = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t_me",
    )
    await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t_someone_else",
    )
    ctx = SimpleNamespace(canvas_store=store)
    arts = await _collect_inline_artifacts(ctx, "t_me")
    assert [a["id"] for a in arts] == [mine.id]


async def test_unrelated_kinds_excluded(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s_ia3", workspace_id="w", user_id="u")
    chart = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t",
    )
    await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "x"},
        by_turn_id="t",
    )
    await store.append_section(
        canvas_id=c.id,
        kind="eyebrow",
        payload={"text": "y"},
        by_turn_id="t",
    )
    ctx = SimpleNamespace(canvas_store=store)
    arts = await _collect_inline_artifacts(ctx, "t")
    assert [a["id"] for a in arts] == [chart.id]


async def test_report_worthy_turn_gets_bounded_inline(store: CanvasStore) -> None:
    """delivery-surface C7：报告轮不再整体互斥——有界选取（≤1 kpi + ≤1 chart）。

    原断言（→ []）保护的 dup fix（prod t_2e569479）防的是**全量重复**；C7 后
    报告轮 chat 气泡内联一小组关键证据，画布仍是完整文档。上限由
    _INLINE_MAX_KPI_ROWS/_INLINE_MAX_CHARTS 钉住（test_inline_bounded_select）。
    """
    c = await store.ensure_canvas(session_id="s_ia_rw", workspace_id="w", user_id="u")
    ch1 = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t_rw",
    )
    # Second chart: still inline only 1 (cap); with zero references, select the first by seq.
    await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {"kind": "bar"}},
        by_turn_id="t_rw",
    )
    await store.append_section(
        canvas_id=c.id,
        kind="insight_card",
        payload={"title": "x"},
        by_turn_id="t_rw",
    )
    ctx = SimpleNamespace(canvas_store=store)
    arts = await _collect_inline_artifacts(ctx, "t_rw")
    assert [a["kind"] for a in arts] == ["chart"]
    assert arts[0]["id"] == ch1.id


async def test_missing_canvas_store_returns_empty() -> None:
    ctx = SimpleNamespace()  # no canvas_store attribute
    arts = await _collect_inline_artifacts(ctx, "t")
    assert arts == []


async def test_chart_without_spec_skipped(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s_ia4", workspace_id="w", user_id="u")
    # Chart with empty payload — no `spec` key. Should NOT appear.
    await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={},
        by_turn_id="t",
    )
    kpi = await store.append_section(
        canvas_id=c.id,
        kind="kpi_row",
        payload={"cells": []},
        by_turn_id="t",
    )
    ctx = SimpleNamespace(canvas_store=store)
    arts = await _collect_inline_artifacts(ctx, "t")
    assert [a["id"] for a in arts] == [kpi.id]


async def test_no_appended_sections_returns_empty(store: CanvasStore) -> None:
    """Turn with no canvas appends → empty list (chat-only answer)."""
    await store.ensure_canvas(session_id="s_ia5", workspace_id="w", user_id="u")
    ctx = SimpleNamespace(canvas_store=store)
    arts = await _collect_inline_artifacts(ctx, "t_no_arts")
    assert arts == []


# ─── _inject_inline_artifacts — sole inject chokepoint (both emit sites) ──


async def test_inject_writes_field_when_artifacts_present(store: CanvasStore) -> None:
    """When the turn has chart/kpi/table sections, the helper MUST write the
    `inline_artifacts` key on the payload. Covers both lead.final.payload emit
    sites (CHANGE-W4.1 + lookup short-path) since both call this helper.
    """
    c = await store.ensure_canvas(session_id="s_inj1", workspace_id="w", user_id="u")
    await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {"type": "bar"}},
        by_turn_id="t_inj1",
    )
    await store.append_section(
        canvas_id=c.id,
        kind="kpi_row",
        payload={"cells": [{"label": "GMV"}]},
        by_turn_id="t_inj1",
    )
    ctx = SimpleNamespace(canvas_store=store)
    payload: dict[str, object] = {
        "title": "",
        "exec_summary": "summary text",
        "kpis": [],
        "insights": [],
        "recommendations": [],
        "actions": [],
        "caveats": [],
        "sources": [],
    }
    count = await _inject_inline_artifacts(payload, ctx, "t_inj1")
    assert count == 2
    assert "inline_artifacts" in payload
    assert isinstance(payload["inline_artifacts"], list)
    kinds_in_payload = [
        a["kind"]
        for a in payload["inline_artifacts"]  # type: ignore[union-attr]
    ]
    assert kinds_in_payload == ["chart", "kpi"]


async def test_inject_no_op_when_no_artifacts(store: CanvasStore) -> None:
    """Chat-only turn (no canvas appends) → payload key must NOT be written.
    FE distinguishes "no artifacts" via absence, not empty list, so the
    extra key would be wire-noise.
    """
    await store.ensure_canvas(session_id="s_inj2", workspace_id="w", user_id="u")
    ctx = SimpleNamespace(canvas_store=store)
    payload: dict[str, object] = {"title": "", "exec_summary": "x"}
    count = await _inject_inline_artifacts(payload, ctx, "t_inj2_no_arts")
    assert count == 0
    assert "inline_artifacts" not in payload


async def test_inject_missing_canvas_store_no_op() -> None:
    """ctx without canvas_store → return 0, payload unchanged. Lookup
    short-path called with stub ctx must not crash.
    """
    ctx = SimpleNamespace()  # no canvas_store
    payload: dict[str, object] = {"title": ""}
    count = await _inject_inline_artifacts(payload, ctx, "t_inj3")
    assert count == 0
    assert "inline_artifacts" not in payload


async def test_inject_does_not_overwrite_unrelated_keys(store: CanvasStore) -> None:
    """The helper must only set `inline_artifacts`; existing AnalysisPayload
    keys (exec_summary, kpis, insights, …) must pass through unchanged.
    """
    c = await store.ensure_canvas(session_id="s_inj4", workspace_id="w", user_id="u")
    await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t",
    )
    ctx = SimpleNamespace(canvas_store=store)
    payload: dict[str, object] = {
        "title": "T",
        "exec_summary": "S",
        "kpis": [{"label": "X"}],
        "insights": ["i1"],
        "recommendations": ["r1"],
        "actions": ["r1"],
        "caveats": ["c1"],
        "sources": [],
    }
    snapshot_before = dict(payload.items())
    await _inject_inline_artifacts(payload, ctx, "t")
    for k, original in snapshot_before.items():
        assert payload[k] == original, f"key '{k}' was mutated"
    assert "inline_artifacts" in payload


# ─── subagent-unification P5a — deliverable download chip (any file type) ────


def test_pptx_slide_count_counts_slides_excludes_rels() -> None:
    assert _pptx_slide_count(_fake_pptx(3)) == 3
    assert _pptx_slide_count(_fake_pptx(1)) == 1


def test_pptx_slide_count_none_on_garbage() -> None:
    assert _pptx_slide_count(b"definitely not a zip") is None


@pytest.mark.parametrize(
    ("filename", "mime", "expect"),
    [
        ("consultant-deck.pptx", _PPTX_MIME, "PPTX"),
        ("model.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "XLSX"),
        ("memo.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "DOCX"),
        ("report.pdf", "application/pdf", "PDF"),
        ("dashboard.html", "text/html", "HTML"),
        ("export.csv", "text/csv", "CSV"),
        # no extension → mime fallback
        ("noext", "application/pdf", "PDF"),
        # unknown → FILE, but still downloadable
        ("mystery.bin", "application/octet-stream", "BIN"),
        ("nameless", "application/x-weird", "FILE"),
    ],
)
def test_file_type_label(filename: str, mime: str, expect: str) -> None:
    assert _file_type_label(filename, mime) == expect


async def _put_deliverable(
    art_store: ArtifactStore,
    *,
    turn: str,
    content: bytes | None = None,
    mime: str = _PPTX_MIME,
    filename: str = "consultant-deck.pptx",
    kind: str = "deliverable",
):
    return await art_store.put_bytes(
        content=content if content is not None else _fake_pptx(3),
        filename=filename,
        mime=mime,
        source="consultant_deck",
        kind=kind,
        producer_kind="consultant_deck",
        created_by="u",
        created_by_turn=turn,
        session_id="s_deck",
    )


async def test_pptx_deliverable_emits_chip_when_report_worthy(
    store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """A deliverable turn is ALWAYS report-worthy (has_deliverable) → the keep
    gate would normally return []. The download chip is an affordance (not a
    canvas duplicate), so it survives and is the sole inline artifact even with
    no canvas sections. A .pptx also carries slide_count."""
    await store.ensure_canvas(session_id="s_deck", workspace_id="w", user_id="u")
    deck = await _put_deliverable(art_store, turn="t_deck")
    ctx = SimpleNamespace(canvas_store=store, artifact_store=art_store)
    arts = await _collect_inline_artifacts(ctx, "t_deck")
    assert len(arts) == 1
    chip = arts[0]
    assert chip["kind"] == "file"
    assert chip["id"] == deck.id  # inline-artifact identity == artifact id
    assert chip["filename"] == "consultant-deck.pptx"
    assert chip["file_type"] == "PPTX"
    assert chip["mime"] == _PPTX_MIME
    assert chip["download_url"] == f"/api/v1/files/{deck.id}"
    assert chip["size_bytes"] == deck.size
    assert chip["slide_count"] == 3


async def test_non_pptx_deliverable_emits_chip_without_slide_count(
    store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """A non-pptx deliverable (e.g. an .xlsx) is ALSO downloadable — it emits a
    file chip with the right type label and NO slide_count (slides are pptx-only).
    A chart in the same turn is still suppressed (deliverable keep-signal)."""
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    c = await store.ensure_canvas(session_id="s_deck", workspace_id="w", user_id="u")
    await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t_xlsx",
    )
    art = await _put_deliverable(
        art_store,
        turn="t_xlsx",
        mime=xlsx_mime,
        filename="财务模型.xlsx",
        content=b"PK\x03\x04 fake xlsx bytes",
    )
    ctx = SimpleNamespace(canvas_store=store, artifact_store=art_store)
    arts = await _collect_inline_artifacts(ctx, "t_xlsx")
    # review R5-12: deck/deliverable turns retain CR-6 chip-only; the download chip opens the answer
    # itself. Do not inline chat charts (C7 bounded selection applies only to nondeliverable report turns).
    assert [a["kind"] for a in arts] == ["file"]
    chip = arts[0]
    assert chip["id"] == art.id
    assert chip["file_type"] == "XLSX"
    assert chip["filename"] == "财务模型.xlsx"
    assert "slide_count" not in chip  # not a deck


async def test_video_deliverable_emits_exact_id_file_chip(
    store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """显式交付的 sandbox 视频进入最终答案卡；身份与 Results artifact 相同。"""
    await store.ensure_canvas(session_id="s_deck", workspace_id="w", user_id="u")
    video = await _put_deliverable(
        art_store,
        turn="t_video",
        mime="video/mp4",
        filename="final.mp4",
        content=b"fake video bytes",
    )
    ctx = SimpleNamespace(canvas_store=store, artifact_store=art_store)

    arts = await _collect_inline_artifacts(ctx, "t_video")

    assert len(arts) == 1
    assert arts[0] == {
        "kind": "file",
        "id": video.id,
        "filename": "final.mp4",
        "file_type": "MP4",
        "mime": "video/mp4",
        "download_url": f"/api/v1/files/{video.id}",
        "size_bytes": video.size,
    }


async def test_deck_chip_omits_slide_count_when_unparseable(
    store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """A .pptx deliverable whose bytes don't parse as a zip still produces a chip
    (degraded gracefully) — file_type=PPTX but no slide_count."""
    await store.ensure_canvas(session_id="s_deck", workspace_id="w", user_id="u")
    await _put_deliverable(art_store, turn="t_bad", content=b"not a zip")
    ctx = SimpleNamespace(canvas_store=store, artifact_store=art_store)
    arts = await _collect_inline_artifacts(ctx, "t_bad")
    assert len(arts) == 1
    assert arts[0]["kind"] == "file"
    assert arts[0]["file_type"] == "PPTX"
    assert "slide_count" not in arts[0]


async def test_deck_preview_kind_is_not_a_download_chip(
    store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """A deck_preview artifact contributes to the keep signal but is NOT a
    downloadable output → no chip (only kind='deliverable' downloads)."""
    await store.ensure_canvas(session_id="s_deck", workspace_id="w", user_id="u")
    await _put_deliverable(
        art_store,
        turn="t_prev",
        kind="deck_preview",
        mime="image/png",
        filename="preview.png",
        content=b"\x89PNG fake",
    )
    ctx = SimpleNamespace(canvas_store=store, artifact_store=art_store)
    arts = await _collect_inline_artifacts(ctx, "t_prev")
    assert arts == []


async def test_file_chip_injected_onto_payload(
    store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """End-to-end through the inject chokepoint: a deliverable turn writes the
    inline_artifacts key carrying the file chip."""
    await store.ensure_canvas(session_id="s_deck", workspace_id="w", user_id="u")
    await _put_deliverable(art_store, turn="t_inj_deck")
    ctx = SimpleNamespace(canvas_store=store, artifact_store=art_store)
    payload: dict[str, object] = {"title": "T", "exec_summary": "S"}
    count = await _inject_inline_artifacts(payload, ctx, "t_inj_deck")
    assert count == 1
    assert payload["inline_artifacts"][0]["kind"] == "file"  # type: ignore[index]


async def test_deck_chip_skips_slide_count_above_size_cap() -> None:
    """CR P2: a huge .pptx must NOT trigger the full-blob open_read on the hot
    path — slide_count is omitted above the cap and open_read is never called."""

    class _BoomStore:
        async def open_read(self, art):
            raise AssertionError("open_read must not be called above the size cap")

    art = SimpleNamespace(
        id="art_big",
        kind="deliverable",
        filename="big.pptx",
        mime=_PPTX_MIME,
        size=_DECK_SLIDE_COUNT_MAX_BYTES + 1,
    )
    chip = await _deliverable_inline_artifact(_BoomStore(), art)
    assert chip is not None
    assert chip["file_type"] == "PPTX"
    assert chip["size_bytes"] == _DECK_SLIDE_COUNT_MAX_BYTES + 1
    assert "slide_count" not in chip  # capped → omitted, chip still emitted


async def test_deck_chip_reads_slide_count_below_size_cap() -> None:
    """Below the cap the read DOES happen and slide_count is populated."""
    data = _fake_pptx(5)

    class _Store:
        async def open_read(self, art):
            return data

    art = SimpleNamespace(
        id="art_small",
        kind="deliverable",
        filename="small.pptx",
        mime=_PPTX_MIME,
        size=len(data),
    )
    chip = await _deliverable_inline_artifact(_Store(), art)
    assert chip["slide_count"] == 5


async def test_collect_no_artifact_store_still_works(store: CanvasStore) -> None:
    """ctx with canvas_store but NO artifact_store (older call sites / tests):
    deliverable collection is skipped, chart/kpi path unaffected."""
    c = await store.ensure_canvas(session_id="s_noart", workspace_id="w", user_id="u")
    chart = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t_noart",
    )
    ctx = SimpleNamespace(canvas_store=store)  # no artifact_store
    arts = await _collect_inline_artifacts(ctx, "t_noart")
    assert [a["id"] for a in arts] == [chart.id]
