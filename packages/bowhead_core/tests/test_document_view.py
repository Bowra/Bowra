"""Word pages for the Results preview (docx inline edit design §5.1)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from bowhead_core.documents import render
from bowhead_core.documents.authoring_formats import WORD_PROFILE
from bowhead_core.documents.authoring_protocol import control, digest
from bowhead_core.documents.package_io import OfficePackageError
from docx import Document
from reportlab.pdfgen import canvas


def _docx() -> bytes:
    document = Document()
    document.add_heading("季度复盘", 1)
    document.add_paragraph("一季度销售额 2,905 万元。")
    raw = io.BytesIO()
    document.save(raw)
    return raw.getvalue()


def _pdf(pages: int) -> bytes:
    raw = io.BytesIO()
    drawing = canvas.Canvas(raw, pagesize=(595, 842))
    for page in range(pages):
        drawing.drawString(72, 770, f"page {page + 1}")
        drawing.showPage()
    drawing.save()
    return raw.getvalue()


def _fake_converter(monkeypatch: pytest.MonkeyPatch, pages: int) -> list[list[str]]:
    calls: list[list[str]] = []

    def run_binary(argv: list[str], **_: object) -> str:
        calls.append(argv)
        out = Path(argv[argv.index("--outdir") + 1])
        (out / (Path(argv[-1]).stem + ".pdf")).write_bytes(_pdf(pages))
        return ""

    monkeypatch.setattr(render, "run_binary", run_binary)
    return calls


def test_a_word_document_renders_its_pages_with_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_converter(monkeypatch, pages=3)
    pdf = render.render_document_view(_docx(), tmp_path)
    assert pdf.startswith(b"%PDF-")
    assert calls[0][calls[0].index("--convert-to") + 1] == "pdf:writer_pdf_Export"
    assert (tmp_path / "document.pdf").is_file(), "The executor reads the pages from output/document.pdf"


def test_page_limits_and_non_documents_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_converter(monkeypatch, pages=2)
    monkeypatch.setattr(render, "MAX_DOCUMENT_VIEW_PAGES", 1)
    with pytest.raises(OfficePackageError) as limited:
        render.render_document_view(_docx(), tmp_path)
    assert limited.value.code == "document_page_limit"
    with pytest.raises(OfficePackageError):
        render.render_document_view(b"not a word document", tmp_path)


def test_the_view_control_admits_the_word_profile() -> None:
    source = _docx()
    assert control("view", (source,), parent_sha256=digest(source), profile=WORD_PROFILE)
    with pytest.raises(OfficePackageError):
        control("view", (source, source), parent_sha256=digest(source), profile=WORD_PROFILE)


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed")
def test_libreoffice_renders_a_generated_document(tmp_path: Path) -> None:
    pdf = render.render_document_view(_docx(), tmp_path)
    assert pdf.startswith(b"%PDF-")
