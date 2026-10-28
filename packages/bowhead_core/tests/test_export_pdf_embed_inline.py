"""embed-inline 全链回归: narrative 锚(含 #seg 片段块)过 render+evidence+validate.

1.0.29 真机实锤的缺口: validate_export_output 的 accounting 不变量假设
「扁平块数==节数」, #seg 派生块打破它 → renderer_failed 泛化报错。修=唯一
section 身份数口径。本测试是 segment 形状唯一的全链覆盖(24-kind fixture 的
insight 内联无片段, 逮不到)。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from bowhead_core.export_assets import SnapshotAssetResolution
from bowhead_core.export_ir import normalize_export_snapshot
from bowhead_core.export_materializer import materialize_export_visuals
from bowhead_core.export_pdf import extract_pdf_evidence, render_export_pdf
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.export_validation import validate_export_output
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import current_print_theme_snapshot
from bowhead_core.report_final_gate import STALE_NOTE
from pypdf import PdfReader
from test_export_pdf import _FakeBrowserRenderer  # noqa: F401  (harness in the same directory)

_PDFTOPPM = shutil.which("pdftoppm")


def _snapshot() -> ExportSnapshot:
    token = f"sha256:{'a' * 64}"
    sections = [
        {"id": "sec_h", "kind": "h1", "rev": 1, "source_refs": [], "payload": {"text": "标题"}},
        {
            "id": "sec_host",
            "kind": "narrative",
            "rev": 1,
            "source_refs": [],
            "payload": {"markdown": "开头段落文本。\n\n[figure:sec_c1]\n\n中段文本继续分析。\n\n[figure:sec_c2]\n\n尾段总结。"},
        },
        {
            "id": "sec_c1",
            "kind": "chart",
            "rev": 1,
            "source_refs": [],
            "payload": {"spec": {"kind": "bar", "title": "图一", "rows": [{"x": 1, "y": 2}]}},
        },
        {
            "id": "sec_c2",
            "kind": "chart",
            "rev": 1,
            "source_refs": [],
            "payload": {"spec": {"kind": "line", "title": "图二", "rows": [{"x": 1, "y": 3}]}},
        },
    ]
    entries = [
        {"section_id": "sec_h", "section_rev": 1, "order": 0, "placement": "report_body", "parent_section_id": None, "effective_kind": "h1"},
        {
            "section_id": "sec_host",
            "section_rev": 1,
            "order": 1,
            "placement": "report_body",
            "parent_section_id": None,
            "effective_kind": "narrative",
        },
        {
            "section_id": "sec_c1",
            "section_rev": 1,
            "order": 2,
            "placement": "report_embedded",
            "parent_section_id": "sec_host",
            "effective_kind": "chart",
        },
        {
            "section_id": "sec_c2",
            "section_rev": 1,
            "order": 3,
            "placement": "report_embedded",
            "parent_section_id": "sec_host",
            "effective_kind": "chart",
        },
    ]
    payload = {
        "request": {
            "format": "pdf",
            "profile": "full_report",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
            "options": {"include_appendix": True},
            "client_surface_receipt_sha256": f"sha256:{'d' * 64}",
        },
        "surface_projection": {"contract_version": 1, "token": token, "entries": entries, "exclusions": []},
        "sections": sections,
    }
    return ExportSnapshot(
        id="exs_repro",
        schema_version=1,
        canvas_id="cv_r",
        session_id="s_r",
        team_id="t_r",
        workspace_id="ws_r",
        canvas_owner="u",
        requested_by="u",
        fmt="pdf",
        profile="full_report",
        title="内联复现",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'b' * 64}",
        surface_contract_version=1,
        surface_token=token,
        parity_contract="full_content_v1",
        payload=payload,
        payload_sha256=f"sha256:{'c' * 64}",
        size_bytes=1024,
        section_count=4,
        created_at="2026-07-31T00:00:00+00:00",
        expires_at="2026-08-07T00:00:00+00:00",
    )


@pytest.mark.skipif(_PDFTOPPM is None, reason="needs poppler")
async def test_narrative_anchor_inline_full_chain(tmp_path: Path) -> None:
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    snapshot = _snapshot()
    export_ir = normalize_export_snapshot(snapshot)
    visuals = await materialize_export_visuals(
        export_ir=export_ir,
        asset_resolution=SnapshotAssetResolution(resolved=(), warnings=()),
        attempt_dir=attempt_dir,
        browser_renderer=_FakeBrowserRenderer(),
    )
    result = render_export_pdf(
        export_ir=export_ir,
        materialized_visuals=visuals,
        attempt_dir=attempt_dir,
        print_theme=current_print_theme_snapshot(),
    )
    evidence = extract_pdf_evidence(
        pdf_path=attempt_dir / result.relative_path,
        export_ir=export_ir,
        materialized_visuals=visuals,
        validation_dir=tmp_path / "validation",
        pdftoppm_path=Path(str(_PDFTOPPM)),
    )
    manifest = validate_export_output(
        job_id="exp_repro",
        snapshot=snapshot,
        export_ir=export_ir,
        target="pdf_full",
        include_appendix=True,
        renderer_name="canvas-reportlab-pdf-v2",
        renderer_version=EXPORT_VISUAL_RENDERER_VERSION,
        evidence=evidence,
    )
    assert evidence.hard_check_failures == (), evidence.hard_check_failures
    assert manifest.validation.result in {"clean", "warnings"}


@pytest.mark.skipif(_PDFTOPPM is None, reason="needs poppler")
@pytest.mark.parametrize("padding", [0, 30, 60])
@pytest.mark.parametrize("stale", [False, True])
async def test_figure_qualification_stays_with_visual_without_losing_content(tmp_path: Path, padding: int, stale: bool) -> None:
    if stale:
        from bowhead_core.export_pdf import _markup_font_coverage

        primary, symbol = _markup_font_coverage()
        if ord("⚠") not in primary | symbol:
            pytest.skip("Stale-note parity needs the pinned renderer's CJK and warning-symbol fonts")
    snapshot = _snapshot()
    sections = snapshot.payload["sections"]
    sections[1]["payload"]["markdown"] = (
        "Opening analysis.\n\n[figure:sec_c1]\n\n"
        + "Source observations are unchanged. " * padding
        + "\n\n[figure:sec_c2]\n\n图2：Observed revenue.\n\n"
        + (STALE_NOTE if stale else "")
        + "\n\nClosing analysis."
    )
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    export_ir = normalize_export_snapshot(snapshot)
    visuals = await materialize_export_visuals(
        export_ir=export_ir,
        asset_resolution=SnapshotAssetResolution(resolved=(), warnings=()),
        attempt_dir=attempt_dir,
        browser_renderer=_FakeBrowserRenderer(),
    )
    rendered = render_export_pdf(
        export_ir=export_ir,
        materialized_visuals=visuals,
        attempt_dir=attempt_dir,
        print_theme=current_print_theme_snapshot(),
    )
    pdf_path = attempt_dir / rendered.relative_path
    reader = PdfReader(pdf_path)
    expected = "数据未在本轮重算" if stale else "Observedrevenue."
    warning_pages = [page for page in reader.pages if expected in page.extract_text().replace("\n", "").replace(" ", "")]
    assert len(warning_pages) == 1
    assert len(warning_pages[0].images) >= 1, "The figure qualification must share a page with its visual"
    if padding == 0:
        assert len(warning_pages[0].images) == 2, "A complete fitting figure must not move to a new page"
    evidence = extract_pdf_evidence(
        pdf_path=pdf_path,
        export_ir=export_ir,
        materialized_visuals=visuals,
        validation_dir=tmp_path / "validation",
        pdftoppm_path=Path(str(_PDFTOPPM)),
    )
    manifest = validate_export_output(
        job_id="exp_caption",
        snapshot=snapshot,
        export_ir=export_ir,
        target="pdf_full",
        include_appendix=True,
        renderer_name=rendered.renderer_name,
        renderer_version=rendered.renderer_version,
        evidence=evidence,
    )
    assert evidence.hard_check_failures == (), evidence.hard_check_failures
    assert manifest.validation.result in {"clean", "warnings"}
