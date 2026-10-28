"""skill-multiruntime S3 follow-up — bounded untrusted-file parsing (scope A+C).

Covers: zip-bomb pre-check, subprocess gating, flag-OFF Tier-1 equivalence,
flag-ON Tier-2 subprocess isolation (happy path + fail-soft + wall-timeout kill),
cluster-C template parse + JSON round-trip, the D4 dead-hint fix, and the XXE /
billion-laughs regression gate (threat model = DoS-primary, not exfil).
"""

from __future__ import annotations

import io
import zipfile

import pytest
from bowhead_core.attachments_injector import (
    PreparedAttachment,
    _ArtShim,
    _deep_read_hint,
    _extract_one,
    extract_attachment_bounded,
)

from bowhead_core import untrusted_parse as up

# ── sample-file builders (real, minimal) ─────────────────────────────


def _pptx_bytes(title: str = "Hello Bounded") -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = title
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _docx_bytes(text: str = "Bounded docx body") -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx_bytes(cell: str = "BoundedCell") -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active["A1"] = cell
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _ratio_bomb_xlsx() -> bytes:
    """OOXML whose declared uncompressed size hugely exceeds the compressed size."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", b"x" * 10)
        z.writestr("xl/worksheets/sheet1.xml", b"\0" * (4 * 1024 * 1024))  # ~4MB zeros → tiny compressed
    return buf.getvalue()


def _shim(mime: str, filename: str, size: int, aid: str = "art_x") -> _ArtShim:
    return _ArtShim(aid, mime, filename, size)


_OOXML_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_OOXML_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ── zip-bomb pre-check ───────────────────────────────────────────────


def test_zip_bomb_ratio_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up, "_ZIP_MAX_RATIO", 10)
    monkeypatch.setattr(up, "_ZIP_RATIO_FLOOR", 1024 * 1024)  # 1MB floor < 4MB bomb
    reason = up.zip_bomb_reason(_ratio_bomb_xlsx(), _OOXML_XLSX, "evil.xlsx")
    assert reason is not None and "ratio" in reason


def test_ratio_floor_spares_small_dense_ooxml(monkeypatch: pytest.MonkeyPatch) -> None:
    # A small-but-dense OOXML (high ratio, tiny absolute size) must NOT be rejected:
    # the ratio guard only applies above the absolute floor (default 100MB).
    monkeypatch.setattr(up, "_ZIP_MAX_RATIO", 10)  # floor stays at 100MB default
    assert up.zip_bomb_reason(_ratio_bomb_xlsx(), _OOXML_XLSX, "dense.xlsx") is None


def test_zip_bomb_uncompressed_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up, "_ZIP_MAX_UNCOMPRESSED", 1024 * 1024)  # 1MB cap
    reason = up.zip_bomb_reason(_ratio_bomb_xlsx(), _OOXML_XLSX, "evil.xlsx")
    assert reason is not None and "uncompressed too large" in reason


def test_zip_bomb_normal_ooxml_passes() -> None:
    assert up.zip_bomb_reason(_pptx_bytes(), _OOXML_PPTX, "deck.pptx") is None
    assert up.zip_bomb_reason(_xlsx_bytes(), _OOXML_XLSX, "book.xlsx") is None


def test_zip_bomb_non_ooxml_ignored() -> None:
    assert up.zip_bomb_reason(_pdf_bytes(), "application/pdf", "a.pdf") is None
    assert up.zip_bomb_reason(b"plain text", "text/plain", "a.txt") is None


def test_zip_bomb_bad_zip_defers() -> None:
    # A .xlsx-named non-zip → guard defers (parser produces its own error).
    assert up.zip_bomb_reason(b"not a zip", _OOXML_XLSX, "broken.xlsx") is None


# ── subprocess gating ────────────────────────────────────────────────


def test_should_subprocess_gating() -> None:
    big = up._SUBPROCESS_MIN_BYTES + 1
    small = up._SUBPROCESS_MIN_BYTES - 1
    assert up.should_subprocess("application/pdf", "a.pdf", big) is True
    assert up.should_subprocess(_OOXML_PPTX, "a.pptx", big) is True
    assert up.should_subprocess("application/pdf", "a.pdf", small) is False
    assert up.should_subprocess("text/csv", "a.csv", big) is False
    assert up.should_subprocess("image/png", "a.png", big) is False


# ── flag OFF (Tier-1): equivalence + zip-bomb still rejected ──────────


async def test_flag_off_equivalence_pptx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_BOUNDED_ATTACH_PARSE", raising=False)
    data = _pptx_bytes("Equiv")
    art = _shim(_OOXML_PPTX, "d.pptx", len(data))
    bounded = await extract_attachment_bounded(art, data)
    direct = _extract_one(art, data)
    assert bounded.extracted_text == direct.extracted_text
    assert bounded.note == direct.note
    assert bounded.error is None
    assert "Equiv" in bounded.extracted_text


async def test_flag_off_zip_bomb_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_BOUNDED_ATTACH_PARSE", raising=False)
    monkeypatch.setattr(up, "_ZIP_MAX_RATIO", 10)
    monkeypatch.setattr(up, "_ZIP_RATIO_FLOOR", 1024 * 1024)
    data = _ratio_bomb_xlsx()
    art = _shim(_OOXML_XLSX, "evil.xlsx", len(data))
    prep = await extract_attachment_bounded(art, data)
    assert prep.error is not None and "rejected" in prep.error
    assert prep.extracted_text == ""


# ── flag ON (Tier-2): subprocess happy path + fail-soft + timeout ────
# (subprocess routing gates on len(data) ≥ _SUBPROCESS_MIN_BYTES; tests use tiny
# real files, so drop the floor to 0 to force the child path.)


async def test_flag_on_subprocess_pptx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_BOUNDED_ATTACH_PARSE", "1")
    monkeypatch.setattr(up, "_SUBPROCESS_MIN_BYTES", 0)
    data = _pptx_bytes("Via Subprocess")
    art = _shim(_OOXML_PPTX, "d.pptx", len(data))
    prep = await extract_attachment_bounded(art, data)
    assert prep.error is None
    assert "Via Subprocess" in prep.extracted_text


async def test_flag_on_subprocess_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_BOUNDED_ATTACH_PARSE", "1")
    monkeypatch.setattr(up, "_SUBPROCESS_MIN_BYTES", 0)
    data = _docx_bytes("Subprocess docx text")
    art = _shim("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "d.docx", len(data))
    prep = await extract_attachment_bounded(art, data)
    assert prep.error is None
    assert "Subprocess docx text" in prep.extracted_text


async def test_flag_on_bad_file_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_BOUNDED_ATTACH_PARSE", "1")
    monkeypatch.setattr(up, "_SUBPROCESS_MIN_BYTES", 0)
    art = _shim("application/pdf", "broken.pdf", 40)
    prep = await extract_attachment_bounded(art, b"%PDF-1.4 garbage not a real pdf")
    # _extract_one is fail-soft → worker returns a record with an error, relayed.
    assert isinstance(prep, PreparedAttachment)
    assert prep.error is not None


async def test_worker_wall_timeout_returns_none() -> None:
    # wall=0 → wait_for times out before the child can finish → kill → (None, timeout).
    limits = up.ExtractLimits(cpu_seconds=1, max_memory_mb=512, wall_timeout_sec=0)
    res, why = await up.run_worker_json("template", {"path": "/nonexistent"}, limits=limits)
    assert res is None
    assert why == "timeout"


async def test_worker_failure_returns_none() -> None:
    # Nonexistent template path → worker parse raises → rc!=0 → (None, failed).
    res, why = await up.run_worker_json("template", {"path": "/does/not/exist.pptx"})
    assert res is None
    assert why in ("failed", "oom")


async def test_flag_on_worker_none_is_fail_soft_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # When the worker dies/times out (run_worker_json → (None, reason)), the seam
    # returns a fail-soft error record and must NOT fall back to an in-process
    # parse (that would reopen the DoS). extract_attachment_bounded imports
    # run_worker_json live, so patching it here takes effect.
    monkeypatch.setenv("BOWHEAD_BOUNDED_ATTACH_PARSE", "1")
    monkeypatch.setattr(up, "_SUBPROCESS_MIN_BYTES", 0)

    async def _none(*_a: object, **_k: object) -> tuple[None, str]:
        return None, "timeout"

    monkeypatch.setattr(up, "run_worker_json", _none)
    data = _pptx_bytes()
    art = _shim(_OOXML_PPTX, "d.pptx", up._SUBPROCESS_MIN_BYTES + 1)
    prep = await extract_attachment_bounded(art, data)
    assert prep.error is not None
    assert "timed out" in prep.error  # differentiated reason surfaced
    assert prep.extracted_text == ""  # did NOT fall back to a real in-process parse


# ── cluster C: template parse + JSON round-trip ──────────────────────


def test_parse_template_pptx(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bowhead_core.template_clone import parse_template

    p = tmp_path / "t.pptx"
    p.write_bytes(_pptx_bytes("Template Title"))
    parsed = parse_template(str(p))
    assert parsed.source_format == "pptx"
    assert parsed.page_count >= 1


# ── D4: dead pdf/docx/pptx deep-read hint now routes through sandbox ──


@pytest.mark.parametrize(
    ("mime", "filename"),
    [
        ("application/pdf", "a.pdf"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"),
        (_OOXML_PPTX, "a.pptx"),
    ],
)
def test_deep_read_hint_routes_through_sandbox(mime: str, filename: str) -> None:
    p = PreparedAttachment(
        artifact_id="art_dr",
        filename=filename,
        mime=mime,
        size=10,
        extracted_text="",
        truncated=True,
        summary_only=True,
        error=None,
    )
    hint = _deep_read_hint(p)
    # No longer the dead bare-skill call that errored "script required".
    assert 'name="pdf"' not in hint and 'name="docx"' not in hint and 'name="pptx"' not in hint
    assert 'name="python_sandbox"' in hint
    assert "art_dr" in hint


# ── threat-model regression gate: XXE exfil neutralized ──────────────


def _xxe_xlsx(secret_path: str) -> bytes:
    sheet = (
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE x [ <!ENTITY xxe SYSTEM "file://{secret_path}"> ]>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&xxe;</t></is></c></row></sheetData>'
        "</worksheet>"
    )
    ct = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    wb = '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    wbrels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_xxe_external_entity_no_leak(tmp_path) -> None:  # type: ignore[no-untyped-def]
    secret = tmp_path / "secret.txt"
    secret.write_text("SUPERSECRET_XXE_TOKEN_9f3a")
    art = _shim(_OOXML_XLSX, "evil.xlsx", 4096)
    prep = _extract_one(art, _xxe_xlsx(str(secret)))
    # Either a parse error record or empty text — NEVER the secret.
    assert "SUPERSECRET_XXE_TOKEN_9f3a" not in (prep.extracted_text or "")


def _billion_laughs_xlsx() -> bytes:
    # Classic entity-amplification bomb in the worksheet part.
    ents = "".join(f'<!ENTITY e{i} "&e{i - 1};&e{i - 1};&e{i - 1};">' for i in range(1, 6))
    sheet = (
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE x [ <!ENTITY e0 "AAAAAAAAAA"> {ents} ]>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&e5;</t></is></c></row></sheetData>'
        "</worksheet>"
    )
    ct = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    wb = '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    wbrels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_billion_laughs_xlsx_bounded() -> None:
    # Amplification bomb must not expand unboundedly — stdlib expat's built-in
    # cap / undefined-entity handling stops it → fail-soft error record, fast.
    art = _shim(_OOXML_XLSX, "bomb.xlsx", 4096)
    prep = _extract_one(art, _billion_laughs_xlsx())
    # No megabytes of "A" expanded into the text.
    assert len(prep.extracted_text or "") < 10_000


def test_ooxml_parsers_disable_entities() -> None:
    """D3 regression gate: the pptx/docx lxml parsers must keep entities OFF. A
    dependency upgrade that flips resolve_entities back on re-opens XXE — this
    fails loudly if that happens. Functional check (lxml doesn't expose the flag
    as a readable attribute): a defined internal entity must NOT expand into text."""
    import docx.oxml.parser as dparser
    import pptx.oxml as poxml
    from lxml import etree

    payload = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "PWNED_ENTITY">]><d>&e;</d>'
    for parser in (poxml.oxml_parser, dparser.oxml_parser):
        try:
            root = etree.fromstring(payload, parser)
            text = "".join(root.itertext())
        except etree.XMLSyntaxError:
            text = ""
        assert "PWNED_ENTITY" not in text
