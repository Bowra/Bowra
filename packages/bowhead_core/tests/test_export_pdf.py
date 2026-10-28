"""PDF V2 renders frozen IR and proves parity from the closed file."""

from __future__ import annotations

import copy
import functools
import hashlib
import io
import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from bowhead_core.export_assets import (
    ResolvedSnapshotAsset,
    SnapshotAssetResolution,
)
from bowhead_core.export_ir import (
    ExportIR,
    ExportIRBlock,
    normalize_export_snapshot,
)
from bowhead_core.export_materializer import (
    BrowserRenderedVisual,
    MaterializedVisual,
    materialize_export_visuals,
)
from bowhead_core.export_native_palette import native_export_palette
from bowhead_core.export_pdf import (
    PdfExportError,
    extract_pdf_evidence,
    render_export_pdf,
)
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.export_validation import validate_export_output
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import current_print_theme_snapshot
from PIL import Image, ImageDraw
from reportlab.pdfbase import pdfmetrics

from bowhead_core import export_pdf as export_pdf_module

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/canvas_export/surface_projection_v1.json"
_PDFTOPPM = shutil.which("pdftoppm")


def test_pdf_blank_gate_ignores_footer_chrome_but_detects_body_content() -> None:
    page = Image.new("RGB", (800, 1100), "white")
    draw = ImageDraw.Draw(page)
    draw.text((760, 1060), "1", fill="#74817b")
    assert export_pdf_module._pdf_body_is_blank(page)  # noqa: SLF001

    draw.rectangle((100, 300, 300, 500), fill="#173d32")
    assert not export_pdf_module._pdf_body_is_blank(page)  # noqa: SLF001


def test_page_chrome_draws_no_extractable_text_so_page_number_cannot_leak() -> None:
    """SimpleDocTemplate fires the onPage chrome callback before the page's
    body flowables are written to the content stream, so a drawn page number
    lands at the start of that page's extracted text. When a narrative block
    spans a page break, _segment_text() reconstructs the block from the whole
    page text and the leaked page-number digit ends up inside the segment,
    breaking the exact semantic-token match (real 116 export:
    section_semantic_mismatch from a page-4 "4"). The footer chrome must draw
    no extractable text at all — only the decorative rule, which is a
    graphics operator, not text."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    buffer = io.BytesIO()
    style = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14)
    # Long enough to force a second page, so any per-page footer number would
    # appear and a naive extractor would surface "1"/"2".
    story = [Paragraph("BODYSENTINEL word " * 400, style)]
    chrome = functools.partial(
        export_pdf_module._draw_page_chrome,  # noqa: SLF001
        palette=native_export_palette(current_print_theme_snapshot()),
    )
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
    )
    document.build(
        story,
        onFirstPage=chrome,
        onLaterPages=chrome,
    )

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(buffer.getvalue()))
    assert len(reader.pages) >= 2
    import re

    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        assert "BODYSENTINEL" in text
        bare_digits = re.findall(r"(?<![A-Za-z.])\b\d+\b(?![A-Za-z.])", text)
        assert bare_digits == [], f"page {index} leaked chrome digits: {bare_digits!r}"


def test_register_ttf_embeds_a_real_truetype_font() -> None:
    """Debian's fonts-noto-cjk ships PostScript/CFF-outline OpenType fonts,
    which reportlab's TTFont rejects with "postscript outlines are not
    supported" even though the file exists — every real Linux PDF export
    then silently fell back to the non-embedded STSong-Light font and was
    later rejected by the closed-file pdf_font_not_embedded check. This
    proves the embedding path itself works for a genuine TrueType (glyf)
    font, which is what production now registers first on Linux."""
    import reportlab

    vera = Path(reportlab.__file__).resolve().parent / "fonts" / "Vera.ttf"
    assert vera.is_file(), "reportlab bundled Vera.ttf is expected to ship with the dependency"
    assert export_pdf_module._register_ttf("VeraSmokeTest", vera)  # noqa: SLF001
    assert "VeraSmokeTest" in pdfmetrics.getRegisteredFontNames()


def test_register_ttf_skips_a_file_it_cannot_parse_without_raising(
    tmp_path: Path,
) -> None:
    """The candidate-selection loop in _register_pdf_fonts() relies on
    _register_ttf() failing closed (returning False) rather than raising,
    so an existing-but-unembeddable candidate (e.g. a CFF-outline .ttc) does
    not abort the whole selection and can fall through to the next
    candidate."""
    garbage = tmp_path / "not-a-font.ttc"
    garbage.write_bytes(b"this is not a valid font file")
    assert export_pdf_module._register_ttf("GarbageSmokeTest", garbage) is False  # noqa: SLF001


def test_paragraph_markup_wraps_symbols_outside_the_cjk_font() -> None:
    # Regression: real 116 reports used ⚠ (U+26A0) in the narrative; the
    # embeddable wqy-zenhei CJK font has no glyph for it, so reportlab rendered
    # .notdef, pypdf extracted a NUL/replacement char, and the closed-file check
    # rejected the export as pdf_missing_glyph + semantic mismatch. Characters
    # outside the primary font but covered by the symbol fallback are wrapped in
    # a <font> tag so they render (and extract) faithfully.
    render = export_pdf_module._render_markup  # noqa: SLF001
    primary = frozenset({ord("经"), ord("营"), ord("a"), ord("<")})
    symbol = frozenset({0x26A0})  # ⚠
    out = render("经营⚠a", primary=primary, symbol=symbol)
    assert '<font name="BowheadSymbol">⚠</font>' in out
    assert out == '经营<font name="BowheadSymbol">⚠</font>a'
    # A char in neither font is left as-is (not spuriously wrapped).
    assert "BowheadSymbol" not in render("经x", primary=primary, symbol=symbol)
    # Escaping and newline behaviour is unchanged from the plain-markup path.
    assert render("a<b\nc", primary=primary, symbol=symbol) == "a&lt;b<br/>c"
    # With no known primary coverage (degraded font path) nothing is wrapped.
    assert "BowheadSymbol" not in render("经营⚠", primary=frozenset(), symbol=frozenset())


def test_normalize_visible_strips_wrap_artifact_whitespace() -> None:
    """A real 116 export of long Chinese narratives was rejected with
    section_semantic_mismatch even though the rendered PDF visibly contained
    the full text. Root cause: pypdf's extract_text() inserts a spurious space
    at every soft line-wrap it infers from a glyph Y-coordinate jump. In a
    CJK-dominant report a wrap can split ANYTHING -- two ideographs, a number
    ("29.6%" -> "2 9.6%"), a citation ("[10]" -> "[1 0]"), an ASCII acronym
    ("SOP" -> "S OP"), a percent/comma/quote/em-dash boundary. None of those
    spaces are semantic, so a single artifact anywhere in a multi-thousand-
    character block broke the whole contiguous match. _normalize_visible drops
    ALL whitespace (after NFKC). Applied symmetrically to the expected token
    and the observed PDF text, it can only let a true match succeed, never
    fabricate a false one; two texts differing solely in spacing become equal,
    which is acceptable for a text-presence check (visual + typed-data checks
    enforce structural fidelity)."""
    n = export_pdf_module._normalize_visible  # noqa: SLF001
    # Each (wrapped, source) pair is an exact wrap boundary from rejected 116
    # exports; strip-all normalization must make both sides equal. pypdf
    # substitutes a space for the soft line-wrap.
    pairs = [
        ("侵蚀利润高达88.5 %，退款率", "侵蚀利润高达88.5%，退款率"),  # digit-percent
        ("同比下滑4.22% ，降幅", "同比下滑4.22%，降幅"),  # percent-comma
        ("高度集中特征— —这种", "高度集中特征——这种"),  # split em-dash
        ('这种" 尖峰', '这种"尖峰'),  # quote-CJK
        ("东湖街单店退款2, 886.8万", "东湖街单店退款2,886.8万"),  # comma-digit
        ("退款率2 9.6%", "退款率29.6%"),  # digit-digit (mid-number)
        ("区域差异高达1.75倍[1 0]", "区域差异高达1.75倍[10]"),  # digit-digit (citation)
        ("重点审查S OP进行", "重点审查SOP进行"),  # letter-letter (acronym)
        ("客单价(IPT )599元", "客单价(IPT)599元"),  # letter-paren
        ("拆解退款NL构成", "拆解退款构成"),  # hard newline mid-CJK
    ]
    for wrapped, source in pairs:
        wrapped = wrapped.replace("NL", chr(10))
        assert n(wrapped) == n(source), f"wrap artifact not collapsed: {wrapped!r}"
    # Whitespace is dropped symmetrically: a source with spaces still matches
    # its space-free / differently-wrapped rendering (text-presence, not spacing).
    assert n("hello world") == n("helloworld") == "helloworld"
    assert n("revenue declined by 6.51 %") == n("revenuedeclinedby6.51%")
    # The in-order containment used by semantic parity now matches a source
    # token against a wrap-artifact-laden extracted segment.
    assert export_pdf_module._contains_tokens_in_order(  # noqa: SLF001
        "近30天营业额10.64亿，同比下滑4.28 %和4.22% ，退款率升至1 5.31%[1 0]。S OP流程",
        ("近30天营业额10.64亿，同比下滑4.28%和4.22%，退款率升至15.31%[10]。SOP流程",),
    )


@dataclass
class _FakeBrowserRenderer:
    name: str = "canvas-browser-visual-v1"
    version: str = EXPORT_VISUAL_RENDERER_VERSION

    async def render(
        self,
        block: ExportIRBlock,
        *,
        asset_paths: dict[str, Path],
    ) -> BrowserRenderedVisual:
        del asset_paths
        color = (
            int(hashlib.sha256(block.block_key.encode()).hexdigest()[:2], 16),
            90,
            150,
            255,
        )
        return BrowserRenderedVisual(
            content=_png_bytes((1600, 900), color),
            mime="image/png",
            visual_input_sha256=str(block.visual_input_sha256),
        )


def _snapshot() -> ExportSnapshot:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    by_id = {section["id"]: section for section in fixture["sections"]}
    entries = copy.deepcopy(fixture["expected_entries"])
    sections = [copy.deepcopy(by_id[entry["section_id"]]) for entry in entries]
    sections[1]["source_refs"] = ["tl_pdf_source"]
    payload = {
        "request": {
            "format": "pdf",
            "profile": "full_report",
            "parity_contract": "full_content_v1",
            "client_surface_receipt_sha256": f"sha256:{'d' * 64}",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
            "options": {"include_appendix": True},
        },
        "surface_projection": {
            "contract_version": 1,
            "token": fixture["surface_token"],
            "entries": entries,
            "exclusions": fixture["expected_exclusions"],
        },
        "sections": sections,
    }
    return ExportSnapshot(
        id="exs_pdf",
        schema_version=1,
        canvas_id="cv_pdf",
        session_id="s_pdf",
        team_id="team_pdf",
        workspace_id="ws_pdf",
        canvas_owner="u_owner",
        requested_by="u_owner",
        fmt="pdf",
        profile="full_report",
        title="PDF 完整性测试",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'a' * 64}",
        surface_contract_version=1,
        surface_token=fixture["surface_token"],
        parity_contract="full_content_v1",
        payload=payload,
        payload_sha256=f"sha256:{'c' * 64}",
        size_bytes=4096,
        section_count=len(sections),
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-24T00:00:00+00:00",
    )


async def _render_fixture(
    tmp_path: Path,
) -> tuple[
    ExportSnapshot,
    ExportIR,
    tuple[MaterializedVisual, ...],
    Path,
]:
    attempt_dir = tmp_path / "attempt"
    image = _png_bytes((640, 480), (200, 40, 20, 200))
    background = _png_bytes((1080, 1080), (240, 240, 240, 255))
    assets_dir = attempt_dir / "assets"
    assets_dir.mkdir(parents=True)
    assets = (
        _stage_asset(
            assets_dir=assets_dir,
            artifact_id="art_image",
            content=image,
        ),
        _stage_asset(
            assets_dir=assets_dir,
            artifact_id="art_bg",
            content=background,
        ),
    )
    resolution = SnapshotAssetResolution(
        resolved=assets,
        warnings=(),
    )
    snapshot = _snapshot()
    export_ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={asset.artifact_id: asset.content_sha256 for asset in assets},
    )
    visuals = await materialize_export_visuals(
        export_ir=export_ir,
        asset_resolution=resolution,
        attempt_dir=attempt_dir,
        browser_renderer=_FakeBrowserRenderer(),
    )
    result = render_export_pdf(
        print_theme=current_print_theme_snapshot(),
        export_ir=export_ir,
        materialized_visuals=visuals,
        attempt_dir=attempt_dir,
    )
    return (
        snapshot,
        export_ir,
        visuals,
        attempt_dir / result.relative_path,
    )


@pytest.mark.skipif(_PDFTOPPM is None, reason="Poppler is a PDF V2 runtime gate")
async def test_pdf_closed_file_evidence_matches_all_kinds(
    tmp_path: Path,
) -> None:
    snapshot, export_ir, visuals, pdf_path = await _render_fixture(tmp_path)

    evidence = extract_pdf_evidence(
        pdf_path=pdf_path,
        export_ir=export_ir,
        materialized_visuals=visuals,
        validation_dir=tmp_path / "validation",
        pdftoppm_path=Path(str(_PDFTOPPM)),
    )
    manifest = validate_export_output(
        job_id="exp_pdf",
        snapshot=snapshot,
        export_ir=export_ir,
        target="pdf_full",
        include_appendix=True,
        renderer_name="canvas-reportlab-pdf-v2",
        renderer_version=EXPORT_VISUAL_RENDERER_VERSION,
        evidence=evidence,
    )

    assert evidence.parser_opened
    assert evidence.hard_check_failures == ()
    assert evidence.demo_residue_count == 0
    assert len(evidence.sections) == 22
    assert manifest.validation.result == "warnings"
    assert manifest.summary.rendered == 19
    assert manifest.summary.fallback == 3
    assert manifest.summary.semantic_parity_rate == 1.0
    assert manifest.summary.visual_identity_rate == 1.0
    assert manifest.summary.typed_data_parity_rate == 1.0
    appendix = {section.section_kind for section in manifest.sections if any(target.kind == "appendix" for target in section.targets)}
    assert appendix == {"data_table", "code_block"}
    forged = (
        replace(visuals[0], pixel_sha256=f"sha256:{'f' * 64}"),
        *visuals[1:],
    )

    evidence = extract_pdf_evidence(
        pdf_path=pdf_path,
        export_ir=export_ir,
        materialized_visuals=forged,
        validation_dir=tmp_path / "validation-forged",
        pdftoppm_path=Path(str(_PDFTOPPM)),
    )
    manifest = validate_export_output(
        job_id="exp_pdf_forged",
        snapshot=snapshot,
        export_ir=export_ir,
        target="pdf_full",
        include_appendix=True,
        renderer_name="canvas-reportlab-pdf-v2",
        renderer_version=EXPORT_VISUAL_RENDERER_VERSION,
        evidence=evidence,
    )

    assert "pdf_visual_pixel_mismatch" in evidence.hard_check_failures
    assert manifest.validation.result == "rejected"
    assert manifest.summary.visual_identity_rate < 1.0


async def test_pdf_output_name_cannot_escape_attempt_directory(
    tmp_path: Path,
) -> None:
    export_ir = normalize_export_snapshot(
        _snapshot(),
        asset_content_hashes={
            "art_image": f"sha256:{'1' * 64}",
            "art_bg": f"sha256:{'2' * 64}",
        },
    )

    with pytest.raises(PdfExportError, match="pdf_staging_invalid"):
        render_export_pdf(
            print_theme=current_print_theme_snapshot(),
            export_ir=export_ir,
            materialized_visuals=(),
            attempt_dir=tmp_path / "other-attempt",
            output_name="../escaped.pdf",
        )


def _stage_asset(
    *,
    assets_dir: Path,
    artifact_id: str,
    content: bytes,
) -> ResolvedSnapshotAsset:
    path = assets_dir / f"{artifact_id}.png"
    path.write_bytes(content)
    return ResolvedSnapshotAsset(
        artifact_id=artifact_id,
        content_sha256=_sha256(content),
        size_bytes=len(content),
        mime="image/png",
        relative_path=f"assets/{path.name}",
    )


def _png_bytes(
    size: tuple[int, int],
    color: tuple[int, int, int, int],
) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
