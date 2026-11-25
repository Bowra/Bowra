"""Inline editing of PowerPoint content (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md).

Runs only inside the bounded parse child (``untrusted_parse_worker``): the deck is untrusted
user or model output. Two entry points:

- ``build_inventory``: every element a person can see on each slide, its absolute box and
  whether it can be edited in place (text frames, single-paragraph table cells, chart values).
- ``apply_edits``: applies a list of edits to a copy and returns the new bytes. Only changed
  characters change (unchanged characters keep their own run), only the touched package parts
  are written back (every other zip entry is copied byte for byte), and every edit must match
  the value the person saw (``expect``) or the whole save is refused.
"""

from __future__ import annotations

import copy
import difflib
import io
import math
import os
import re
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

FEATURE_ENV = "FEATURE_PPTX_INLINE_EDIT"
FIELD_TOKEN = "￼"
BREAK = "\n"
MAX_SLIDES = 64
MAX_EDITS = 200
MAX_NEW_TEXT_CHARS = 20_000
MAX_TEXT_CHARS = 5_000

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DGM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"
_OLE_URI = "http://schemas.openxmlformats.org/presentationml/2006/ole"
#: Run properties that do not change how text looks (spell-check and language bookkeeping).
_INVISIBLE_RPR_ATTRS = ("lang", "altLang", "dirty", "err", "noProof", "smtClean", "smtId", "bmk")
_ALLOWED_CONTROL = {"\n", "\t"}


def is_presentation(mime: str | None, filename: str | None) -> bool:
    """A .pptx deck, judged the way the presentation preview judges it."""
    from bowhead_core.documents.package_io import OFFICE_MIMES, office_upload_mime

    return office_upload_mime(filename or "", mime) == OFFICE_MIMES[".pptx"]


def feature_enabled() -> bool:
    return os.environ.get(FEATURE_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


class EditRefused(Exception):
    """One or more edits cannot be applied; nothing is written."""

    def __init__(self, refusals: Sequence[Mapping[str, Any]]) -> None:
        self.refusals = [dict(item) for item in refusals]
        super().__init__(",".join(str(item.get("code")) for item in self.refusals))


class PresentationUnreadable(Exception):
    """The deck cannot be opened or exceeds the limits this feature supports."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _open(data: bytes) -> Any:
    from pptx import Presentation

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - any parser failure means the deck is unreadable
        raise PresentationUnreadable("unreadable") from exc
    if not 1 <= len(prs.slides) <= MAX_SLIDES:
        raise PresentationUnreadable("slide_limit")
    return prs


# ── Geometry ──────────────────────────────────────────────────────────────────────────


def _group_transform(group: Any) -> tuple[float, ...] | None:
    xfrm = group._element.grpSpPr.find(_q(_A, "xfrm"))
    if xfrm is None:
        return None
    parts = [xfrm.find(_q(_A, name)) for name in ("off", "ext", "chOff", "chExt")]
    if any(part is None for part in parts):
        return None
    off, ext, ch_off, ch_ext = parts
    return (
        float(off.get("x")),
        float(off.get("y")),
        float(ext.get("cx")),
        float(ext.get("cy")),
        float(ch_off.get("x")),
        float(ch_off.get("y")),
        float(ch_ext.get("cx")),
        float(ch_ext.get("cy")),
    )


def _absolute_box(shape: Any, transforms: Sequence[tuple[float, ...] | None]) -> tuple[float, float, float, float] | None:
    try:
        left, top, width, height = shape.left, shape.top, shape.width, shape.height
    except (AttributeError, ValueError):
        return None
    if None in (left, top, width, height):
        return None
    box = (float(left), float(top), float(width), float(height))
    for transform in reversed(transforms):
        if transform is None:
            return None
        ox, oy, ex, ey, cx, cy, cex, cey = transform
        sx = ex / cex if cex else 1.0
        sy = ey / cey if cey else 1.0
        x, y, w, h = box
        box = (ox + (x - cx) * sx, oy + (y - cy) * sy, w * sx, h * sy)
    return box


def _walk(shapes: Any, transforms: list[tuple[float, ...] | None] | None = None):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    transforms = transforms or []
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(shape.shapes, [*transforms, _group_transform(shape)])
        else:
            yield shape, _absolute_box(shape, transforms)


# ── Paragraph text and the style-keeping writer ──────────────────────────────────────


def _segments(p_el: Any) -> list[tuple[str, Any, str]]:
    out: list[tuple[str, Any, str]] = []
    for child in p_el:
        if child.tag == _q(_A, "r"):
            text_el = child.find(_q(_A, "t"))
            out.append(("r", child, (text_el.text or "") if text_el is not None else ""))
        elif child.tag == _q(_A, "br"):
            out.append(("br", child, BREAK))
        elif child.tag == _q(_A, "fld"):
            out.append(("fld", child, FIELD_TOKEN))
    return out


def paragraph_text(p_el: Any) -> str:
    """The paragraph as the editor shows it: line breaks as ``\\n``, each field as one token."""
    return "".join(text for _, _, text in _segments(p_el))


def _style_key(run_el: Any) -> bytes:
    rpr = run_el.find(_q(_A, "rPr"))
    if rpr is None:
        return b""
    norm = copy.deepcopy(rpr)
    for attr in _INVISIBLE_RPR_ATTRS:
        norm.attrib.pop(attr, None)
    return etree.tostring(norm, method="c14n", exclusive=True)


def _mixed_style(p_el: Any) -> bool:
    return len({_style_key(el) for kind, el, text in _segments(p_el) if kind == "r" and text}) > 1


def _check_text(text: Any) -> str | None:
    if not isinstance(text, str):
        return "invalid_text"
    if len(text) > MAX_TEXT_CHARS:
        return "text_too_long"
    if any(ord(ch) < 0x20 and ch not in _ALLOWED_CONTROL for ch in text):
        return "control_character"
    return None


def write_keep_styles(p_el: Any, new: str) -> None:
    """Rewrite a paragraph's content to ``new`` while every unchanged character keeps its run.

    Replaced characters take the style of the first character they replace; inserted ones take
    the style of their left neighbour (the right one at the start). A newline stored inside a
    run stays run text; a new newline becomes ``<a:br>``. A field is one token that must stay.
    """
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls

    segs = _segments(p_el)
    owners: list[int] = []
    old_chars: list[str] = []
    for index, (_, _, text) in enumerate(segs):
        for ch in text:
            owners.append(index)
            old_chars.append(ch)
    old = "".join(old_chars)
    if new.count(FIELD_TOKEN) != old.count(FIELD_TOKEN):
        raise EditRefused([{"code": "field_changed"}])
    run_segments = [index for index, seg in enumerate(segs) if seg[0] == "r"]
    if not run_segments:
        raise EditRefused([{"code": "no_run"}])

    def style_owner(position: int) -> int:
        owner = owners[position]
        if segs[owner][0] == "r":
            return owner
        for j in range(position - 1, -1, -1):
            if segs[owners[j]][0] == "r":
                return owners[j]
        for j in range(position + 1, len(owners)):
            if segs[owners[j]][0] == "r":
                return owners[j]
        return run_segments[0]

    out: list[tuple[str, int, bool]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            out.extend((old[i], owners[i], True) for i in range(i1, i2))
            continue
        if FIELD_TOKEN in old[i1:i2] or FIELD_TOKEN in new[j1:j2]:
            raise EditRefused([{"code": "field_changed"}])
        if tag == "delete":
            continue
        if tag == "replace":
            owner = style_owner(i1)
        elif i1 > 0:
            owner = style_owner(i1 - 1)
        elif old:
            owner = style_owner(0)
        else:
            owner = run_segments[0]
        out.extend((ch, owner, False) for ch in new[j1:j2])

    end = p_el.find(_q(_A, "endParaRPr"))
    for _, element, _ in segs:
        p_el.remove(element)

    def place(element: Any) -> None:
        if end is not None:
            end.addprevious(element)
        else:
            p_el.append(element)

    def rpr_of(owner: int) -> Any:
        rpr = segs[owner][1].find(_q(_A, "rPr"))
        return copy.deepcopy(rpr) if rpr is not None else None

    buffer: list[str] = []
    buffer_owner: int | None = None

    def flush() -> None:
        nonlocal buffer, buffer_owner
        if not buffer or buffer_owner is None:
            buffer, buffer_owner = [], None
            return
        run = parse_xml(f"<a:r {nsdecls('a')}/>")
        rpr = rpr_of(buffer_owner)
        if rpr is not None:
            run.append(rpr)
        text_el = parse_xml(f"<a:t {nsdecls('a')}/>")
        text_el.text = "".join(buffer)
        run.append(text_el)
        place(run)
        buffer, buffer_owner = [], None

    for ch, owner, unchanged in out:
        if ch == BREAK and unchanged and segs[owner][0] == "r":
            if buffer_owner is not None and owner != buffer_owner:
                flush()
            buffer_owner = owner
            buffer.append(ch)
        elif ch == FIELD_TOKEN:
            flush()
            place(copy.deepcopy(segs[owner][1]))
        elif ch == BREAK:
            flush()
            if segs[owner][0] == "br":
                place(copy.deepcopy(segs[owner][1]))
            else:
                br = parse_xml(f"<a:br {nsdecls('a')}/>")
                rpr = rpr_of(owner) if segs[owner][0] == "r" else None
                if rpr is not None:
                    br.append(rpr)
                place(br)
        else:
            if buffer_owner is not None and owner != buffer_owner:
                flush()
            buffer_owner = owner
            buffer.append(ch)
    flush()


# ── Charts ────────────────────────────────────────────────────────────────────────────

_XY_CHART_TAGS = ("scatterChart", "bubbleChart")


def _chart_formats(chart: Any) -> tuple[str | None, dict[str, str | None]]:
    space = chart._chartSpace
    ns = {"c": _C}
    categories = space.findall(".//c:ser/c:cat//c:formatCode", ns)
    series: dict[str, str | None] = {}
    for ser in space.findall(".//c:ser", ns):
        name_el = ser.findall(".//c:tx//c:v", ns)
        name = name_el[0].text if name_el and name_el[0].text else None
        value_format = ser.findall(".//c:val//c:formatCode", ns)
        if name is not None:
            series[name] = value_format[0].text if value_format and value_format[0].text else None
    return (categories[0].text if categories and categories[0].text else None), series


def _chart_data(chart: Any) -> dict[str, Any] | None:
    space = chart._chartSpace
    if any(space.findall(f".//{{{_C}}}{tag}") for tag in _XY_CHART_TAGS):
        return None
    try:
        _, formats = _chart_formats(chart)
        categories = [str(category) for category in chart.plots[0].categories] if chart.plots else []
        series = [
            {"name": item.name, "values": [None if value is None else float(value) for value in item.values], "format": formats.get(item.name)}
            for item in chart.series
        ]
    except Exception:  # noqa: BLE001 - a chart python-pptx cannot read is not editable
        return None
    if not categories or not series or any(len(item["values"]) != len(categories) for item in series):
        return None
    return {"type": str(chart.chart_type), "categories": categories, "series": series}


def _same_chart_values(current: Mapping[str, Any], expect: Any) -> bool:
    if not isinstance(expect, Mapping):
        return False
    if list(expect.get("categories") or []) != current["categories"]:
        return False
    expected = expect.get("series")
    if not isinstance(expected, list) or len(expected) != len(current["series"]):
        return False
    for got, want in zip(current["series"], expected, strict=True):
        if not isinstance(want, Mapping) or want.get("name") != got["name"]:
            return False
        values = want.get("values")
        if not isinstance(values, list) or len(values) != len(got["values"]):
            return False
        for a, b in zip(got["values"], values, strict=True):
            if a is None or b is None:
                if a is not b:
                    return False
            elif not isinstance(b, int | float) or isinstance(b, bool) or not math.isclose(a, float(b), rel_tol=1e-9, abs_tol=1e-12):
                return False
    return True


def _replace_chart_values(chart: Any, current: Mapping[str, Any], series: Any) -> list[dict[str, Any]]:
    from pptx.chart.data import CategoryChartData

    if not isinstance(series, list) or [item.get("name") if isinstance(item, Mapping) else None for item in series] != [
        item["name"] for item in current["series"]
    ]:
        raise EditRefused([{"code": "series_changed"}])
    category_format, formats = _chart_formats(chart)
    data = CategoryChartData(number_format=category_format or "General")
    data.categories = current["categories"]
    changes: list[dict[str, Any]] = []
    for got, new in zip(current["series"], series, strict=True):
        values = new.get("values")
        if not isinstance(values, list) or len(values) != len(current["categories"]):
            raise EditRefused([{"code": "values_shape"}])
        clean: list[float | None] = []
        for category, before, value in zip(current["categories"], got["values"], values, strict=True):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value))):
                raise EditRefused([{"code": "not_a_number"}])
            after = None if value is None else float(value)
            clean.append(after)
            if before != after:
                changes.append({"category": category, "series": got["name"], "before": before, "after": after})
        data.add_series(got["name"], tuple(clean), number_format=formats.get(got["name"]) or "General")
    chart.replace_data(data)
    return changes


# ── Inventory ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Element:
    slide: Any
    shape: Any
    key: str
    kind: str
    box: tuple[float, float, float, float] | None
    editable: bool = True
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _graphic_uri(shape: Any) -> str | None:
    data = shape._element.find(".//" + _q(_A, "graphicData"))
    return data.get("uri") if data is not None else None


def _classify(shape: Any) -> str:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if getattr(shape, "has_table", False) and shape.has_table:
        return "table"
    if getattr(shape, "has_chart", False) and shape.has_chart:
        return "chart"
    uri = _graphic_uri(shape)
    if uri == _DGM_URI:
        return "smartart"
    if shape.shape_type == MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT or uri == _OLE_URI:
        return "ole"
    if (
        shape.shape_type == MSO_SHAPE_TYPE.PICTURE
        or shape._element.find(".//" + _q(_A, "blip")) is not None
        and not getattr(shape, "has_text_frame", False)
    ):
        return "picture"
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
        return "text"
    return "other"


def _slide_elements(slide: Any) -> list[_Element]:
    pairs = list(_walk(slide.shapes))
    counts = Counter(shape.shape_id for shape, _ in pairs)
    elements: list[_Element] = []
    for shape, box in pairs:
        kind = _classify(shape)
        if kind == "other":
            continue
        element = _Element(slide=slide, shape=shape, key=f"{slide.slide_id}:{shape.shape_id}", kind=kind, box=box)
        if kind in {"picture", "smartart", "ole"}:
            element.editable, element.reason = False, kind
        elif counts[shape.shape_id] > 1:
            element.editable, element.reason = False, "duplicate_shape_id"
        elif kind == "chart":
            data = _chart_data(shape.chart)
            if data is None:
                element.editable, element.reason = False, "chart_unsupported"
            else:
                element.extra["chart"] = data
        elements.append(element)
    return elements


def _inherited_text(slide: Any) -> bool:
    for source in (slide.slide_layout, slide.slide_layout.slide_master):
        for shape in source.shapes:
            if not shape.is_placeholder and getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
                return True
    return False


def _cell_info(cell: Any) -> dict[str, Any]:
    paragraphs = cell.text_frame.paragraphs
    text = paragraph_text(paragraphs[0]._p) if paragraphs else ""
    if getattr(cell, "is_spanned", False):
        return {"text": "", "editable": False, "reason": "covered_by_merge"}
    if len(paragraphs) > 1:
        return {"text": "\n".join(paragraph_text(p._p) for p in paragraphs), "editable": False, "reason": "multi_paragraph"}
    return {"text": text, "editable": True, "reason": None, "mixed_style": _mixed_style(paragraphs[0]._p) if paragraphs else False}


def build_inventory(data: bytes) -> dict[str, Any]:
    prs = _open(data)
    width, height = float(prs.slide_width or 1), float(prs.slide_height or 1)
    slides: list[dict[str, Any]] = []
    for index, slide in enumerate(prs.slides, start=1):
        items: list[dict[str, Any]] = []
        for element in _slide_elements(slide):
            box = element.box
            item: dict[str, Any] = {
                "key": element.key,
                "shape_id": element.shape.shape_id,
                "name": element.shape.name,
                "kind": element.kind,
                "box": None
                if box is None
                else [round(box[0] / width, 5), round(box[1] / height, 5), round(box[2] / width, 5), round(box[3] / height, 5)],
                "editable": element.editable,
                "reason": element.reason,
            }
            if element.kind == "text" and element.editable:
                item["paragraphs"] = [
                    {"text": paragraph_text(p._p), "mixed_style": _mixed_style(p._p), "fields": paragraph_text(p._p).count(FIELD_TOKEN)}
                    for p in element.shape.text_frame.paragraphs
                ]
            elif element.kind == "table" and element.editable:
                item["rows"] = [[_cell_info(cell) for cell in row.cells] for row in element.shape.table.rows]
            elif element.kind == "chart" and element.editable:
                item["chart"] = element.extra["chart"]
            items.append(item)
        slides.append({"slide_id": slide.slide_id, "index": index, "inherited_text": _inherited_text(slide), "elements": items})
    return {"slide_width": int(width), "slide_height": int(height), "field_token": FIELD_TOKEN, "slides": slides}


# ── Apply ─────────────────────────────────────────────────────────────────────────────


def _validate_request(edits: Any) -> list[Mapping[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise EditRefused([{"index": None, "code": "no_edits"}])
    if len(edits) > MAX_EDITS:
        raise EditRefused([{"index": None, "code": "too_many_edits"}])
    total = 0
    refusals: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index, edit in enumerate(edits):
        if not isinstance(edit, Mapping) or edit.get("kind") not in {"text", "cell", "chart"} or not isinstance(edit.get("key"), str):
            refusals.append({"index": index, "code": "invalid_edit"})
            continue
        if edit["kind"] in {"text", "cell"}:
            problem = _check_text(edit.get("text")) or _check_text(edit.get("expect"))
            if problem:
                refusals.append({"index": index, "code": problem})
                continue
            total += len(edit["text"])
            target = (edit["key"], edit.get("paragraph")) if edit["kind"] == "text" else (edit["key"], edit.get("row"), edit.get("col"))
        else:
            target = (edit["key"], "chart")
        if target in seen:
            refusals.append({"index": index, "code": "duplicate_edit"})
        seen.add(target)
    if total > MAX_NEW_TEXT_CHARS:
        refusals.append({"index": None, "code": "too_much_text"})
    if refusals:
        raise EditRefused(refusals)
    return list(edits)


def _part_name(part: Any) -> str:
    return str(part.partname).lstrip("/")


def _rels_name(name: str) -> str:
    head, _, tail = name.rpartition("/")
    return f"{head}/_rels/{tail}.rels" if head else f"_rels/{tail}.rels"


def _preserving_save(base: bytes, rewritten: bytes, touched: set[str]) -> tuple[bytes, list[str]]:
    with zipfile.ZipFile(io.BytesIO(base)) as old, zipfile.ZipFile(io.BytesIO(rewritten)) as new:
        old_names = set(old.namelist())
        new_names = set(new.namelist())
        added = [name for name in new.namelist() if name not in old_names and not name.endswith("/")]
        missing = sorted(name for name in touched if name not in new_names)
        if missing:
            raise PresentationUnreadable("verification_failed")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as target:
            for info in old.infolist():
                name = info.filename
                if name in touched or (name == "[Content_Types].xml" and added):
                    target.writestr(info, new.read(name), compress_type=zipfile.ZIP_DEFLATED)
                else:
                    target.writestr(info, old.read(name), compress_type=info.compress_type)
            for name in added:
                target.writestr(new.getinfo(name), new.read(name), compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue(), added


def _verify(base: bytes, output: bytes, allowed: set[str]) -> None:
    _open(output)
    with zipfile.ZipFile(io.BytesIO(base)) as old, zipfile.ZipFile(io.BytesIO(output)) as new:
        old_names = set(old.namelist())
        for name in new.namelist():
            if name in old_names and new.read(name) == old.read(name):
                continue
            if name not in allowed:
                raise PresentationUnreadable("verification_failed")
            if name.endswith((".xml", ".rels")):
                try:
                    etree.fromstring(new.read(name))
                except etree.XMLSyntaxError as exc:
                    raise PresentationUnreadable("verification_failed") from exc


def apply_edits(data: bytes, edits: Any) -> tuple[bytes, dict[str, Any]]:
    """Apply ``edits`` to a copy of ``data``. Returns the new bytes and what changed.

    Raises ``EditRefused`` (nothing written) or ``PresentationUnreadable``.
    """
    requested = _validate_request(edits)
    prs = _open(data)
    elements: dict[str, _Element] = {}
    for slide in prs.slides:
        for element in _slide_elements(slide):
            elements[element.key] = element

    refusals: list[dict[str, Any]] = []
    plans: list[tuple[int, Mapping[str, Any], _Element, Any]] = []
    for index, edit in enumerate(requested):
        element = elements.get(edit["key"])
        if element is None:
            refusals.append({"index": index, "code": "target_missing"})
            continue
        if not element.editable:
            refusals.append({"index": index, "code": element.reason or "not_editable"})
            continue
        if edit["kind"] == "text":
            paragraphs = element.shape.text_frame.paragraphs if element.kind == "text" else []
            number = edit.get("paragraph")
            if element.kind != "text" or not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < len(paragraphs):
                refusals.append({"index": index, "code": "target_missing"})
                continue
            p_el = paragraphs[number]._p
            if paragraph_text(p_el) != edit["expect"]:
                refusals.append({"index": index, "code": "target_changed"})
                continue
            plans.append((index, edit, element, p_el))
        elif edit["kind"] == "cell":
            row, col = edit.get("row"), edit.get("col")
            if element.kind != "table" or not all(isinstance(v, int) and not isinstance(v, bool) for v in (row, col)):
                refusals.append({"index": index, "code": "target_missing"})
                continue
            table = element.shape.table
            if not (0 <= row < len(table.rows) and 0 <= col < len(table.columns)):
                refusals.append({"index": index, "code": "target_missing"})
                continue
            info = _cell_info(table.cell(row, col))
            if not info["editable"]:
                refusals.append({"index": index, "code": info["reason"]})
                continue
            if info["text"] != edit["expect"]:
                refusals.append({"index": index, "code": "target_changed"})
                continue
            plans.append((index, edit, element, table.cell(row, col).text_frame.paragraphs[0]._p))
        else:
            if element.kind != "chart":
                refusals.append({"index": index, "code": "target_missing"})
                continue
            if not _same_chart_values(element.extra["chart"], edit.get("expect")):
                refusals.append({"index": index, "code": "target_changed"})
                continue
            plans.append((index, edit, element, None))
    if refusals:
        raise EditRefused(refusals)

    touched: set[str] = set()
    changes: list[dict[str, Any]] = []
    for index, edit, element, p_el in plans:
        slide_part = _part_name(element.slide.part)
        if edit["kind"] in {"text", "cell"}:
            before = paragraph_text(p_el)
            try:
                write_keep_styles(p_el, edit["text"])
            except EditRefused as exc:
                raise EditRefused([{"index": index, "code": item.get("code")} for item in exc.refusals]) from exc
            touched.add(slide_part)
            change: dict[str, Any] = {"index": index, "key": edit["key"], "kind": edit["kind"], "before": before, "after": edit["text"]}
            if edit["kind"] == "text":
                change["paragraph"] = edit["paragraph"]
            else:
                change["row"], change["col"] = edit["row"], edit["col"]
            changes.append(change)
        else:
            chart = element.shape.chart
            try:
                values = _replace_chart_values(chart, element.extra["chart"], edit.get("series"))
            except EditRefused as exc:
                raise EditRefused([{"index": index, "code": item.get("code")} for item in exc.refusals]) from exc
            chart_part = chart.part
            chart_name = _part_name(chart_part)
            touched |= {chart_name, _rels_name(chart_name)}
            workbook = chart_part.chart_workbook.xlsx_part
            if workbook is not None:
                touched.add(_part_name(workbook))
            changes.append({"index": index, "key": edit["key"], "kind": "chart", "values": values})

    rewritten = io.BytesIO()
    prs.save(rewritten)
    output, added = _preserving_save(data, rewritten.getvalue(), touched)
    allowed = touched | set(added) | ({"[Content_Types].xml"} if added else set())
    _verify(data, output, allowed)
    return output, {"touched_parts": sorted(touched), "added_parts": added, "changes": changes}


_SUFFIX = re.compile(r"\.pptx$", re.IGNORECASE)


# The marker an edited upload's new file carries, in the person's interface language.
_EDITED_MARKERS = {
    "zh-CN": ("（已修改）", "演示文稿"),
    "en-US": (" (edited)", "Presentation"),
    "de-DE": (" (bearbeitet)", "Präsentation"),
    "fr-FR": (" (modifié)", "Présentation"),
}


def edited_filename(filename: str, language: str = "zh-CN") -> str:
    """The name of a new file saved from an edited upload, such as ``<stem>（已修改）.pptx``."""
    marker, fallback = _EDITED_MARKERS.get(language, _EDITED_MARKERS["en-US"])
    stem = _SUFFIX.sub("", filename or "").strip() or fallback
    return f"{stem}{marker}.pptx"
