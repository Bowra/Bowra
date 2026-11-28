"""In-place editing of Word text (docx-inline-edit design §6, §9, §13).

The document is generated here (python-docx plus raw WordprocessingML) so every construct the
engine must handle is present: a paragraph mixing run styles with a hyperlink, simple and complex
fields (DATE, NUMPAGES, PAGE, a TOC spanning paragraphs), a smart tag and a symbol inside a run,
inline and block content controls, tracked changes, bookmarks and a comment range, a literal tab
stored inside ``w:t``, a text box, a footnote reference, merged and nested table cells, a header
with a PAGE field, a footer, a footnote and a comment.
"""

from __future__ import annotations

import difflib
import io
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.document_edit import (
    FIELD_TOKEN,
    MAX_EDITS,
    MAX_TEXT_CHARS,
    DocumentUnreadable,
    EditRefused,
    apply_edits,
    build_inventory,
    edited_filename,
    is_word_document,
)
from bowhead_core.presentation_edit import FIELD_TOKEN as PRESENTATION_FIELD_TOKEN
from bowhead_core.untrusted_parse import run_worker_json
from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.constants import CONTENT_TYPE, RELATIONSHIP_TYPE
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.shared import Twips
from lxml import etree

from bowhead_core import document_edit, document_store_inventory

pytestmark = pytest.mark.unit

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = (
    f'xmlns:w="{W}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
)
BOLD = "<w:rPr><w:b/></w:rPr>"
RED = '<w:rPr><w:i/><w:color w:val="C00000"/></w:rPr>'
LINK = '<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr>'
FOOTNOTES = (
    f"<w:footnotes {NS}>"
    '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
    '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
    '<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Source: annual report.</w:t></w:r></w:p></w:footnote>'
    "</w:footnotes>"
)


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _r(text: str, rpr: str = "") -> str:
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _field(instr: str, result: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {instr} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f"<w:r><w:t>{result}</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


TEXT_BOX = (
    '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="1828800" cy="457200"/>'
    '<wp:docPr id="5" name="Text Box 5"/><a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
    '<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1828800" cy="457200"/></a:xfrm>'
    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr><wps:txbx><w:txbxContent><w:p>'
    + _r("Boxed ")
    + _r("note", BOLD)
    + "</w:p></w:txbxContent></wps:txbx><wps:bodyPr/></wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
)
NESTED = '<w:tbl><w:tblGrid><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/></w:tblGrid><w:tr><w:tc><w:p>{a}</w:p></w:tc><w:tc><w:p>{b}</w:p></w:tc></w:tr></w:tbl>'
TABLE = (
    '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
    '<w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/><w:gridCol w:w="3000"/></w:tblGrid>'
    '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p>' + _r("Merged header", BOLD) + "</w:p></w:tc>"
    "<w:tc><w:p>" + _r("Col C") + "</w:p></w:tc></w:tr>"
    '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p>' + _r("Region") + "</w:p></w:tc>"
    "<w:tc><w:p>" + _r("Outer cell") + "</w:p></w:tc>"
    "<w:tc>" + NESTED.format(a=_r("Nested A"), b=_r("Nested B")) + "<w:p/></w:tc></w:tr>"
    "<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p>" + _r("Hidden") + "</w:p></w:tc>"
    "<w:tc><w:p>" + _r("Value 42") + "</w:p></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl>"
)

# Body blocks in order: (name, raw XML or None for a python-docx paragraph).
BLOCKS: list[tuple[str, str]] = [
    (
        "mixed",
        _r("Contract value is ")
        + _r("120", BOLD)
        + _r(" thousand, see ")
        + '<w:hyperlink r:id="{link}">'
        + _r("the price list", LINK)
        + "</w:hyperlink>"
        + _r(" for details."),
    ),
    (
        "date",
        _r("Signed on ")
        + '<w:fldSimple w:instr=" DATE \\@ &quot;yyyy-MM-dd&quot; "><w:r><w:t>2026-11-25</w:t></w:r></w:fldSimple>'
        + _r(" by the ")
        + _r("supplier", RED)
        + _r("."),
    ),
    ("numpages", _r("Total pages: ") + _field("NUMPAGES", "3") + _r(" pages in ") + _r("this annex", BOLD) + _r(".")),
    (
        "symbol",
        _r("Ship to ")
        + '<w:smartTag w:uri="urn:schemas-microsoft-com:office:smarttags" w:element="City">'
        + _r("Shanghai", BOLD)
        + "</w:smartTag>"
        + '<w:r><w:t xml:space="preserve"> before Friday </w:t><w:sym w:font="Wingdings" w:char="F0FC"/><w:t xml:space="preserve"> done.</w:t></w:r>',
    ),
    (
        "inline_control",
        _r("Buyer: ")
        + '<w:sdt><w:sdtPr><w:alias w:val="Buyer"/></w:sdtPr><w:sdtContent>'
        + _r("Example Ltd", BOLD)
        + "</w:sdtContent></w:sdt>"
        + _r(", address on file."),
    ),
    (
        "tracked",
        _r("Delivery in ")
        + '<w:del w:id="1" w:author="A" w:date="2026-11-01T00:00:00Z"><w:r><w:delText>30</w:delText></w:r></w:del>'
        + '<w:ins w:id="2" w:author="A" w:date="2026-11-01T00:00:00Z">'
        + _r("45", BOLD)
        + "</w:ins>"
        + _r(" days."),
    ),
    (
        "ranges",
        '<w:bookmarkStart w:id="10" w:name="clause"/>'
        + _r("Penalty of ")
        + _r("5%", BOLD)
        + '<w:bookmarkEnd w:id="10"/>'
        + _r(" per week of delay")
        + '<w:commentRangeStart w:id="7"/>'
        + _r(", capped at ")
        + _r("20%", RED)
        + '<w:commentRangeEnd w:id="7"/>'
        + _r("."),
    ),
    ("breaks", _r("Tab") + "<w:r><w:tab/></w:r>" + _r("separated ") + _r("value", BOLD) + "<w:r><w:br/></w:r>" + _r("second line.")),
    ("literal_tab", '<w:r><w:t xml:space="preserve">Literal\ttab kept</w:t></w:r>' + _r(" and more", BOLD)),
    ("box_anchor", _r("Box anchor: ") + TEXT_BOX + _r(" after box.")),
    (
        "footnote",
        _r("Revenue grew") + '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteReference w:id="1"/></w:r>' + _r(" last year."),
    ),
    (
        "page",
        '<w:r><w:t xml:space="preserve">Page </w:t><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/><w:t xml:space="preserve"> of the report.</w:t></w:r>',
    ),
    ("block_control", None),
    (
        "toc_begin",
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + _r("1 Overview") + "<w:r><w:tab/></w:r>" + _r("1"),
    ),
    ("toc_entry", _r("2 Terms") + "<w:r><w:tab/></w:r>" + _r("2")),
    ("toc_end", '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
    ("table", None),
    ("empty_mark", "<w:pPr><w:rPr><w:b/></w:rPr></w:pPr>"),
]
BLOCK_CONTROL = (
    f'<w:sdt {NS}><w:sdtPr><w:alias w:val="Clause"/></w:sdtPr><w:sdtContent><w:p>{_r("Controlled clause text")}</w:p></w:sdtContent></w:sdt>'
)


def _build() -> tuple[bytes, dict[str, str]]:
    doc = Document()
    first = doc.sections[0]
    first.page_width, first.page_height = Twips(11906), Twips(16838)
    header = first.header.paragraphs[0]._p
    for xml in (_r("Page "), *_field("PAGE", "1").replace("</w:r><w:r>", "</w:r>\n<w:r>").split("\n")):
        header.append(parse_xml(xml.replace("<w:r>", f"<w:r {NS}>", 1)))
    first.footer.paragraphs[0].text = "Confidential"
    notes = Part(PackURI("/word/footnotes.xml"), CONTENT_TYPE.WML_FOOTNOTES, FOOTNOTES.encode(), doc.part.package)
    doc.part.relate_to(notes, RELATIONSHIP_TYPE.FOOTNOTES)
    link = doc.part.relate_to("https://example.com/", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    chapter = doc.styles.add_style("Chapter", WD_STYLE_TYPE.PARAGRAPH)
    chapter.base_style = doc.styles["Heading 2"]

    body = doc.element.body
    names: list[str] = []

    def add(name: str, element) -> None:
        body.sectPr.addprevious(element)
        names.append(name)

    doc.add_heading("Overview", level=1)
    names.append("heading")
    doc.add_paragraph("Terms and scope", style="Chapter")
    names.append("chapter")
    for name, inner in BLOCKS:
        if name == "block_control":
            add(name, parse_xml(BLOCK_CONTROL))
        elif name == "table":
            add(name, parse_xml(TABLE.replace("<w:tbl>", f"<w:tbl {NS}>", 1)))
        else:
            add(name, parse_xml(f"<w:p {NS}>{inner.format(link=link)}</w:p>"))
    doc.add_paragraph()
    names.append("empty")
    anchor = doc.add_paragraph()
    run = anchor.add_run("Reviewed by legal")
    doc.add_comment(run, text="Check the cap", author="R")
    names.append("commented")
    doc.add_section(WD_SECTION.NEW_PAGE)
    last = doc.sections[-1]
    last.orientation, last.page_width, last.page_height = WD_ORIENT.LANDSCAPE, Twips(16838), Twips(11906)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue(), {name: f"document:b{index}" for index, name in enumerate(names)}


@pytest.fixture(scope="module")
def built() -> tuple[bytes, dict[str, str]]:
    return _build()


@pytest.fixture(scope="module")
def document(built) -> bytes:
    return built[0]


@pytest.fixture(scope="module")
def keys(built) -> dict[str, str]:
    return built[1]


@pytest.fixture(scope="module")
def inventory(document: bytes) -> dict:
    return build_inventory(document)


def _paragraphs(inventory: dict) -> dict[str, dict]:
    return {item["key"]: item for item in inventory["paragraphs"]}


def _edit(inventory: dict, key: str, change) -> dict:
    text = _paragraphs(inventory)[key]["text"]
    return {"key": key, "expect": text, "text": change(text)}


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _block(data: bytes, key: str):
    """A top-level body block, found by position independently of the engine."""
    root = etree.fromstring(_entries(data)["word/document.xml"])
    blocks = [child for child in root.find(_q("body")) if child.tag in {_q("p"), _q("tbl"), _q("sdt")}]
    return blocks[int(key.removeprefix("document:b"))]


def _plain(p_el) -> list[tuple[str, bytes]]:
    """(character, look) for the paragraph's own text: runs outside fields, simple fields, content
    controls and text boxes. The look is the run's properties plus its hyperlink or smart tag."""
    out: list[tuple[str, bytes]] = []
    depth = 0
    hidden = {_q("fldSimple"), _q("sdt"), _q("txbxContent")}
    for el in p_el.iter():
        if el.tag == _q("fldChar"):
            kind = el.get(_q("fldCharType"))
            depth += 1 if kind == "begin" else -1 if kind == "end" else 0
            continue
        if el.tag not in {_q("t"), _q("tab"), _q("br")} or depth or any(a.tag in hidden for a in el.iterancestors()):
            continue
        run = el.getparent()
        if run.tag != _q("r"):
            continue
        rpr = run.find(_q("rPr"))
        look = etree.tostring(rpr, method="c14n") if rpr is not None else b""
        look += b"".join(b"|" + a.tag.encode() for a in el.iterancestors() if a.tag in {_q("hyperlink"), _q("smartTag")})
        chars = (el.text or "") if el.tag == _q("t") else ("\t" if el.tag == _q("tab") else "\n")
        out.extend((ch, look) for ch in chars)
    return out


STRUCTURES = (
    "bookmarkStart",
    "bookmarkEnd",
    "commentRangeStart",
    "commentRangeEnd",
    "fldChar",
    "fldSimple",
    "hyperlink",
    "drawing",
    "sdt",
    "sym",
    "smartTag",
    "footnoteReference",
)


def _structures(p_el) -> Counter:
    return Counter(el.tag.split("}")[1] for el in p_el.iter() if isinstance(el.tag, str) and el.tag.split("}")[1] in STRUCTURES)


# ── Inventory ────────────────────────────────────────────────────────────────────────


def test_the_inventory_lists_body_and_cell_paragraphs_with_their_text_and_reasons(inventory: dict, keys: dict[str, str]) -> None:
    items = _paragraphs(inventory)
    assert inventory["page"] == {"width": 11906, "height": 16838}  # the first section, not the landscape last one
    assert inventory["field_token"] == FIELD_TOKEN == PRESENTATION_FIELD_TOKEN
    assert inventory["document"] == {"refusal": None}
    assert inventory["read_only_parts"] == {"header_footer": 2, "note": 1, "comment": 1}
    token = FIELD_TOKEN
    expected = {
        "mixed": ("Contract value is 120 thousand, see the price list for details.", True, None),
        "date": (f"Signed on {token} by the supplier.", True, None),
        "numpages": (f"Total pages: {token} pages in this annex.", True, None),
        "symbol": (f"Ship to Shanghai before Friday {token} done.", True, None),
        "inline_control": (f"Buyer: {token}, address on file.", True, None),
        "tracked": ("Delivery in 45 days.", False, "tracked_change"),
        "ranges": ("Penalty of 5% per week of delay, capped at 20%.", True, None),
        "breaks": ("Tab\tseparated value\nsecond line.", True, None),
        "literal_tab": ("Literal\ttab kept and more", True, None),
        "box_anchor": (f"Box anchor: {token} after box.", True, None),
        "footnote": (f"Revenue grew{token} last year.", True, None),
        "page": (f"Page {token} of the report.", True, None),
        "toc_begin": ("1 Overview\t1", False, "field_span"),
        "toc_entry": ("2 Terms\t2", False, "field_span"),
        "commented": ("Reviewed by legal", True, None),
    }
    for name, (text, editable, reason) in expected.items():
        item = items[keys[name]]
        assert (item["text"], item["editable"], item["reason"], item["kind"], item["table"]) == (text, editable, reason, "body", None), name
    control = items[f"{keys['block_control']}.b0"]
    assert (control["text"], control["editable"], control["reason"]) == ("Controlled clause text", False, "content_control")
    box = items[f"{keys['box_anchor']}.x0.b0"]
    assert (box["text"], box["kind"], box["editable"], box["reason"]) == ("Boxed note", "body", False, "text_box")
    # Blank paragraphs are not listed: the editor finds paragraphs on the pages by their text.
    assert keys["empty"] not in items and keys["toc_end"] not in items and keys["empty_mark"] not in items
    assert items[keys["heading"]]["heading_level"] == 1
    assert items[keys["chapter"]]["heading_level"] == 2  # a custom style based on Heading 2
    assert items[keys["mixed"]]["heading_level"] is None
    keys_in_order = [item["key"] for item in inventory["paragraphs"]]
    assert keys_in_order.index(keys["box_anchor"]) + 1 == keys_in_order.index(f"{keys['box_anchor']}.x0.b0")


def test_headers_footers_and_notes_are_listed_read_only_with_their_text(document: bytes, inventory: dict) -> None:
    """Canvas W7 (2026-11-27): the editor finds them on the pages and says why they cannot change there."""
    assert inventory["read_only_paragraphs"] == [
        {"key": "header1:b0", "part": "header", "text": f"Page {FIELD_TOKEN}", "reason": "header_footer"},
        {"key": "footer1:b0", "part": "footer", "text": "Confidential", "reason": "header_footer"},
        {"key": "footnotes:n2.b0", "part": "footnote", "text": " Source: annual report.", "reason": "note"},
    ]
    with pytest.raises(EditRefused) as refused:
        apply_edits(document, [{"key": "footer1:b0", "expect": "Confidential", "text": "Internal"}])
    assert refused.value.refusals == [{"index": 0, "key": "footer1:b0", "code": "not_editable", "reason": "header_footer"}]


def test_table_cells_carry_their_grid_position_spans_and_merge_rule(inventory: dict, keys: dict[str, str]) -> None:
    table = keys["table"]
    cells = {item["text"]: item for item in inventory["paragraphs"] if item["kind"] == "cell"}
    assert set(cells) == {"Merged header", "Col C", "Region", "Outer cell", "Nested A", "Nested B", "Hidden", "Value 42"}

    def position(text: str) -> tuple:
        item = cells[text]
        info = item["table"]
        return item["key"], info["key"], info["row"], info["col"], info["row_span"], info["col_span"], item["editable"], item["reason"]

    assert position("Merged header") == (f"{table}.r0.c0.b0", table, 0, 0, 1, 2, True, None)
    assert position("Col C") == (f"{table}.r0.c1.b0", table, 0, 2, 1, 1, True, None)  # grid column after the span
    assert position("Region") == (f"{table}.r1.c0.b0", table, 1, 0, 2, 1, True, None)
    assert position("Hidden") == (f"{table}.r2.c0.b0", table, 2, 0, 1, 1, False, "merged_continuation")
    nested = f"{table}.r1.c2.b0"
    assert position("Nested B") == (f"{nested}.r0.c1.b0", nested, 0, 1, 1, 1, True, None)


# ── Writer ───────────────────────────────────────────────────────────────────────────

CASES = [
    ("mixed", lambda t: t.replace("120", "125")),
    ("mixed", lambda t: t.replace("price list", "tariff")),  # inside the hyperlink
    ("mixed", lambda t: t.replace("see the price", "see price")),  # across the hyperlink's start
    ("mixed", lambda t: t + "（已更新）"),
    ("date", lambda t: t.replace("Signed on ", "Signed at noon on ")),
    ("date", lambda t: t.replace(" by the", " (local) by the")),  # right after a simple field
    ("numpages", lambda t: t.replace(" pages in", " printed pages in")),  # after a complex field
    ("symbol", lambda t: t.replace("Shanghai", "Ningbo")),  # inside a smart tag
    ("symbol", lambda t: t.replace(f"{FIELD_TOKEN} done", f"{FIELD_TOKEN}, all done")),  # right after a symbol in mid-run
    ("inline_control", lambda t: t.replace(", address", " and partners, address")),
    ("ranges", lambda t: t.replace("of delay, capped at 20%", "of delay")),  # across the comment range's end
    ("ranges", lambda t: t.replace("5%", "6%")),  # inside the bookmark
    ("breaks", lambda t: t.replace("value\nsecond", "value\n\tsecond")),
    ("literal_tab", lambda t: t.replace("kept", "stays")),
    ("box_anchor", lambda t: t.replace(" after box.", " after the box.")),
    ("footnote", lambda t: t.replace("Revenue grew", "Revenue rose")),
    ("page", lambda t: t.replace(f"{FIELD_TOKEN} of", f"{FIELD_TOKEN}, printed, of")),  # the end run also holds text
    ("commented", lambda t: t.replace("legal", "the legal team")),
]


@pytest.mark.parametrize(("name", "change"), CASES)
def test_the_writer_keeps_every_untouched_characters_look_and_every_structure(
    document: bytes, inventory: dict, keys: dict[str, str], name, change
) -> None:
    edit = _edit(inventory, keys[name], change)
    output, meta = apply_edits(document, [edit])
    before, after = _block(document, edit["key"]), _block(output, edit["key"])
    old, new = _plain(before), _plain(after)
    new_plain = edit["text"].replace(FIELD_TOKEN, "")
    assert "".join(ch for ch, _ in new) == new_plain
    old_plain = "".join(ch for ch, _ in old)
    restyled = 0
    for tag, i1, i2, j1, _ in difflib.SequenceMatcher(None, old_plain, new_plain, autojunk=False).get_opcodes():
        if tag == "equal":
            restyled += sum(old[i1 + k][1] != new[j1 + k][1] for k in range(i2 - i1))
    assert restyled == 0
    assert _structures(after) == _structures(before)
    assert meta == {
        "touched_parts": ["word/document.xml"],
        "changes": [{"index": 0, "key": edit["key"], "before": edit["expect"], "after": edit["text"]}],
    }
    assert _paragraphs(build_inventory(output))[edit["key"]]["text"] == edit["text"]


def test_new_text_lands_in_the_right_run_next_to_fields_symbols_and_literal_tabs(document: bytes, inventory: dict, keys: dict[str, str]) -> None:
    edits = [
        _edit(inventory, keys["mixed"], lambda t: t.replace("120", "125")),
        _edit(inventory, keys["page"], lambda t: t.replace(f"{FIELD_TOKEN} of", f"{FIELD_TOKEN}, printed, of")),
        _edit(inventory, keys["symbol"], lambda t: t.replace(f"{FIELD_TOKEN} done", f"{FIELD_TOKEN}, all done")),
        _edit(inventory, keys["literal_tab"], lambda t: t.replace("kept", "stays")),
        {"key": keys["empty_mark"], "expect": "", "text": "Filled in"},
    ]
    output, _ = apply_edits(document, edits)

    def texts(name: str) -> list[str]:
        return [t.text for t in _block(output, keys[name]).iter(_q("t"))]

    bold = [t.text for t in _block(output, keys["mixed"]).iter(_q("t")) if t.getparent().find(f"{_q('rPr')}/{_q('b')}") is not None]
    assert bold == ["125"]  # the replaced digit joined its bold neighbour
    assert texts("page") == ["Page ", "1", ", printed,", " of the report."]
    symbol = _block(output, keys["symbol"])
    assert texts("symbol") == ["Ship to ", "Shanghai", " before Friday ", ", all", " done."]
    assert len(list(symbol.iter(_q("sym")))) == 1
    literal = _block(output, keys["literal_tab"])
    assert texts("literal_tab") == ["Literal\ttab stays", " and more"] and not list(literal.iter(_q("tab")))
    filled = _block(output, keys["empty_mark"]).find(_q("r"))
    assert filled.find(f"{_q('rPr')}/{_q('b')}") is not None and filled.find(_q("t")).text == "Filled in"


def test_only_the_document_part_is_rewritten_and_every_other_entry_is_copied_byte_for_byte(
    document: bytes, inventory: dict, keys: dict[str, str]
) -> None:
    edits = [
        _edit(inventory, keys["mixed"], lambda t: t.replace("120", "130")),
        _edit(inventory, f"{keys['table']}.r1.c2.b0.r0.c1.b0", lambda t: t.replace("B", "Beta")),
        _edit(inventory, keys["breaks"], lambda t: t.replace("second line", "next line\nthird line")),
    ]
    output, meta = apply_edits(document, edits)
    before, after = _entries(document), _entries(output)
    assert list(after) == list(before)
    assert [name for name in before if before[name] != after[name]] == ["word/document.xml"] == meta["touched_parts"]
    root = etree.fromstring(after["word/document.xml"])
    untouched = [etree.tostring(p, method="c14n") for p in etree.fromstring(before["word/document.xml"]).iter(_q("p"))]
    changed = sum(old != etree.tostring(p, method="c14n") for old, p in zip(untouched, root.iter(_q("p")), strict=True))
    assert changed == 3


def test_the_saved_file_reopens_in_python_docx_with_every_part(document: bytes, inventory: dict, keys: dict[str, str]) -> None:
    output, _ = apply_edits(document, [_edit(inventory, keys["ranges"], lambda t: t.replace("5%", "7%"))])
    reopened = Document(io.BytesIO(output))
    assert any(p.text == "Penalty of 7% per week of delay, capped at 20%." for p in reopened.paragraphs)
    names = {str(part.partname) for part in Document(io.BytesIO(document)).part.package.iter_parts()}
    assert names == {str(part.partname) for part in reopened.part.package.iter_parts()}
    assert {"/word/footnotes.xml", "/word/comments.xml", "/word/header1.xml", "/word/footer1.xml"} <= names


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice is required for a real conversion")
def test_libreoffice_converts_the_saved_file(document: bytes, inventory: dict, keys: dict[str, str], tmp_path: Path) -> None:
    import pdfplumber

    output, _ = apply_edits(document, [_edit(inventory, keys["mixed"], lambda t: t.replace("120", "987"))])
    source = tmp_path / "edited.docx"
    source.write_bytes(output)
    subprocess.run(  # noqa: S603 - fixed arguments, a file this test wrote
        [
            shutil.which("soffice"),
            f"-env:UserInstallation={(tmp_path / 'profile').as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmp_path),
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    with pdfplumber.open(tmp_path / "edited.pdf") as pdf:
        text = "".join(page.extract_text() or "" for page in pdf.pages)
    assert "Contract value is 987 thousand" in text


def test_an_edit_that_keeps_the_text_writes_nothing(document: bytes, inventory: dict, keys: dict[str, str]) -> None:
    same = _edit(inventory, keys["mixed"], lambda t: t)
    assert apply_edits(document, [same]) == (document, {"touched_parts": [], "changes": []})


# ── Refusals ─────────────────────────────────────────────────────────────────────────


def _refusals(document: bytes, edits) -> list[dict]:
    with pytest.raises(EditRefused) as caught:
        apply_edits(document, edits)
    return caught.value.refusals


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("tracked", "tracked_change"),
        ("toc_entry", "field_span"),
        ("toc_end", "field_span"),
        ("block_control", "content_control"),
        ("box_anchor", "text_box"),
        ("table", "merged_continuation"),
        ("header1:b0", "header_footer"),
        ("footer1:b0", "header_footer"),
        ("footnotes:n2.b0", "note"),
    ],
)
def test_each_read_only_paragraph_is_refused_with_its_reason(document: bytes, keys: dict[str, str], target: str, reason: str) -> None:
    key = {"block_control": f"{keys['block_control']}.b0", "box_anchor": f"{keys['box_anchor']}.x0.b0", "table": f"{keys['table']}.r2.c0.b0"}.get(
        target, keys.get(target, target)
    )
    with pytest.raises(EditRefused) as caught:
        apply_edits(document, [{"key": key, "expect": "anything", "text": "changed"}])
    assert caught.value.code == "not_editable"
    assert caught.value.detail == {"index": 0, "key": key, "reason": reason}


def test_requests_and_targets_that_cannot_be_honoured_are_refused_and_nothing_is_written(
    document: bytes, inventory: dict, keys: dict[str, str]
) -> None:
    mixed = _paragraphs(inventory)[keys["mixed"]]["text"]
    date = _paragraphs(inventory)[keys["date"]]["text"]

    def codes(edits) -> list[tuple]:
        return [(item["code"], item.get("reason")) for item in _refusals(document, edits)]

    assert codes("not a list") == [("invalid_request", "no_edits")]
    assert codes([]) == [("invalid_request", "no_edits")]
    assert codes([{"key": 1, "expect": "", "text": ""}]) == [("invalid_request", "malformed_edit")]
    assert codes([{"key": keys["mixed"], "expect": mixed}]) == [("invalid_request", "malformed_edit")]
    duplicate = {"key": keys["mixed"], "expect": mixed, "text": mixed + "!"}
    assert codes([duplicate, duplicate]) == [("invalid_request", "duplicate_key")]
    for character in ("\r", "\x0b", " ", "\x85"):  # a new paragraph or another control character
        assert codes([{"key": keys["mixed"], "expect": mixed, "text": mixed + character + "more"}]) == [("invalid_request", "control_character")]
    assert codes([{"key": f"document:b{i}", "expect": "", "text": ""} for i in range(MAX_EDITS + 1)]) == [("too_many_edits", None)]
    assert codes([{"key": keys["mixed"], "expect": mixed, "text": "x" * (MAX_TEXT_CHARS + 1)}]) == [("text_too_long", "paragraph")]
    many = [{"key": f"document:b{i}", "expect": "", "text": "x" * 4_500} for i in range(5)]
    assert codes(many) == [("text_too_long", "total")]
    assert codes([{"key": "document:b999", "expect": "", "text": "x"}]) == [("not_found", None)]
    assert codes([{"key": "comments:n0.b0", "expect": "Check the cap", "text": "x"}]) == [("not_found", None)]
    assert codes([{"key": keys["mixed"], "expect": mixed.replace("120", "100"), "text": mixed}]) == [("text_changed", None)]
    assert codes([{"key": keys["date"], "expect": date, "text": date.replace(FIELD_TOKEN, "")}]) == [("token_changed", None)]
    assert codes([{"key": keys["date"], "expect": date, "text": date.replace(FIELD_TOKEN, "today")}]) == [("token_changed", None)]
    assert codes([{"key": keys["mixed"], "expect": mixed, "text": mixed + FIELD_TOKEN}]) == [("token_changed", None)]
    # Every refused edit is named, with its position in the request.
    refusals = _refusals(
        document,
        [
            {"key": keys["mixed"], "expect": mixed, "text": mixed + " ok"},
            {"key": "document:b999", "expect": "", "text": "x"},
            {"key": keys["date"], "expect": "stale", "text": "x"},
        ],
    )
    assert refusals == [{"index": 1, "key": "document:b999", "code": "not_found"}, {"index": 2, "key": keys["date"], "code": "text_changed"}]


def _rezip(data: bytes, changes: dict[str, bytes]) -> bytes:
    entries = _entries(data)
    entries.update(changes)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return out.getvalue()


def _with_settings(data: bytes, element: str) -> bytes:
    settings = _entries(data)["word/settings.xml"]
    end = settings.index(b">", settings.index(b"<w:settings")) + 1
    return _rezip(data, {"word/settings.xml": settings[:end] + element.encode() + settings[end:]})


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda d: _with_settings(d, '<w:documentProtection w:edit="readOnly" w:enforcement="1"/>'), "protection_enforced"),
        (lambda d: _with_settings(d, '<w:writeProtection w:recommended="1"/>'), "protection_enforced"),
        (lambda d: _rezip(d, {"_xmlsignatures/origin.sigs": b""}), "digitally_signed"),
        (lambda d: _with_settings(d, "<w:trackRevisions/>"), "track_changes_on"),
        (lambda d: _with_settings(d, '<w:documentProtection w:edit="readOnly" w:enforcement="0"/>'), None),  # the WPS default
        (lambda d: _with_settings(d, '<w:trackRevisions w:val="false"/>'), None),
    ],
)
def test_document_level_refusals_are_reported_and_refuse_every_edit(document: bytes, inventory: dict, keys: dict[str, str], change, refusal) -> None:
    changed = change(document)
    assert build_inventory(changed)["document"] == {"refusal": refusal}
    edit = _edit(inventory, keys["mixed"], lambda t: t + " ok")
    if refusal is None:
        apply_edits(changed, [edit])
    else:
        assert _refusals(changed, [edit]) == [{"index": None, "key": None, "code": refusal}]


def test_files_that_are_not_word_documents_are_unreadable(document: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    strict = b'<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"><w:body/></w:document>'
    for data in (b"not a zip", _rezip(document, {"word/document.xml": b"<w:document"}), _rezip(document, {"word/document.xml": strict})):
        with pytest.raises(DocumentUnreadable) as caught:
            build_inventory(data)
        assert caught.value.code == "unreadable"
    monkeypatch.setattr(document_edit, "MAX_PARAGRAPHS", 3)
    with pytest.raises(DocumentUnreadable) as caught:
        build_inventory(document)
    assert caught.value.code == "paragraph_limit"


def test_word_files_are_recognised_and_an_edited_upload_gets_a_distinct_name() -> None:
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert is_word_document(docx, "报告.docx") and is_word_document(None, "report.DOCX") and is_word_document("application/octet-stream", "a.docx")
    assert not is_word_document("application/pdf", "report.docx") and not is_word_document(None, "deck.pptx") and not is_word_document(None, None)
    assert edited_filename("合同.docx") == "合同（已修改）.docx"
    assert edited_filename("Contract.DOCX", "en-US") == "Contract (edited).docx"
    assert edited_filename("Vertrag.docx", "de-DE") == "Vertrag (bearbeitet).docx"
    assert edited_filename("Contrat.docx", "fr-FR") == "Contrat (modifié).docx"
    assert edited_filename("", "zh-CN") == "文档（已修改）.docx"


# ── Bounded child and the stored-document inventory ──────────────────────────────────


class _Store:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.reads = 0

    async def iter_read_range(self, art, *, start: int, end: int):
        self.reads += 1
        yield self.data[start:end]


async def test_the_stored_document_inventory_comes_from_the_bounded_child_once(document: bytes, inventory: dict) -> None:
    document_store_inventory._inventory_cache.clear()
    store = _Store(document)
    art = SimpleNamespace(id="art_docx_inventory", size=len(document))
    first = await document_store_inventory.document_inventory(store, art)
    second = await document_store_inventory.document_inventory(store, art)
    assert first == inventory and second is first and store.reads == 1


async def test_the_bounded_child_applies_edits_and_returns_refusals_as_results(document: bytes, inventory: dict, keys: dict[str, str]) -> None:
    edit = _edit(inventory, keys["mixed"], lambda t: t.replace("120", "140"))
    result, outcome = await run_worker_json("docx_edit_apply", {"edits": [edit]}, data=document, binary_result=True)
    assert outcome == "ok"
    output = result.pop("_result_bytes")
    assert result == {
        "touched_parts": ["word/document.xml"],
        "changes": [{"index": 0, "key": edit["key"], "before": edit["expect"], "after": edit["text"]}],
    }
    assert _paragraphs(build_inventory(output))[edit["key"]]["text"] == edit["text"]
    refused, outcome = await run_worker_json("docx_edit_apply", {"edits": [{**edit, "expect": "stale"}]}, data=document, binary_result=True)
    assert (outcome, refused) == ("ok", {"refused": [{"index": 0, "key": edit["key"], "code": "text_changed"}]})
