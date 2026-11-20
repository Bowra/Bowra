"""Visual self-check: rasterize renders, tile pages, detect blank renders, compare content.

Design: docs/sprints/visual-self-check-2026-11-16/tech-design.md §5 and §7.

Every function here is pure and takes bytes. Functions that parse model-written files
(PPTX, PDF, images) are called from ``untrusted_parse_worker`` children, never in the
application process directly; ``visual_worker_entry`` is the single worker entry point.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

SINGLE_PAGE_WIDTH = 1280
SHEET_TILE_WIDTH = 400
SHEET_COLUMNS = 4
MAX_PAGES_PER_SHEET = 12
MAX_SHEETS_PER_FILE = 2
MAX_FULL_PAGES = 4
BLANK_SHARE = 0.995
BLANK_TOLERANCE = 4
MAX_CHANGES_PER_KIND = 30
# A page shown in a sheet is this many pixels wide; text below about this height cannot be judged.
LEGIBLE_TEXT_PX = 10
_PNG_SIZE_LIMIT = 1_500_000
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_SPACE = re.compile(r"\s+")


# ---------------------------------------------------------------- rasterizing


def _pdf_page_images(pdf: bytes, pages: Sequence[int], width: int) -> list[Any]:
    """Render the given zero-based pages at ``width`` pixels."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        images = []
        for index in pages:
            page = document[index]
            try:
                scale = width / max(page.get_width(), 1.0)
                images.append(page.render(scale=scale).to_pil().convert("RGB"))
            finally:
                page.close()
        return images
    finally:
        document.close()


def pdf_page_count(pdf: bytes) -> int:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def is_blank(image: Any) -> bool:
    """At least ``BLANK_SHARE`` of the pixels within ``BLANK_TOLERANCE`` of one colour."""
    sample = image.convert("RGB").copy()
    sample.thumbnail((160, 160))
    flattened = getattr(sample, "get_flattened_data", None)
    pixels = list(flattened() if callable(flattened) else sample.getdata())
    if not pixels:
        return True
    dominant = Counter(pixels).most_common(1)[0][0]
    close = sum(1 for pixel in pixels if max(abs(pixel[c] - dominant[c]) for c in range(3)) <= BLANK_TOLERANCE)
    return close / len(pixels) >= BLANK_SHARE


def is_uniform(image: Any) -> bool:
    """Every pixel at full resolution within ``BLANK_TOLERANCE`` of the corner colour.

    For page captures (slice B): a sparse page, one heading on white, is not blank, which the
    share-based ``is_blank`` on a thumbnail would call blank.
    """
    from PIL import Image, ImageChops

    rgb = image.convert("RGB")
    solid = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    difference = ImageChops.difference(rgb, solid).convert("L").point(lambda value: 255 if value > BLANK_TOLERANCE else 0)
    return difference.getbbox() is None


def tile(images: Sequence[Any], labels: Sequence[str]) -> Any:
    """One sheet: ``SHEET_COLUMNS`` columns of ``SHEET_TILE_WIDTH`` px tiles, each labelled."""
    from PIL import Image, ImageDraw

    pad, label_height = 10, 20
    first = images[0]
    tile_height = round(first.height * SHEET_TILE_WIDTH / first.width)
    rows = (len(images) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    columns = min(SHEET_COLUMNS, len(images))
    sheet = Image.new("RGB", (columns * (SHEET_TILE_WIDTH + pad) + pad, rows * (tile_height + label_height + pad) + pad), (200, 204, 210))
    draw = ImageDraw.Draw(sheet)
    for index, (image, label) in enumerate(zip(images, labels, strict=True)):
        row, column = divmod(index, SHEET_COLUMNS)
        x, y = pad + column * (SHEET_TILE_WIDTH + pad), pad + row * (tile_height + label_height + pad)
        draw.text((x, y + 3), label, fill=(20, 20, 20))
        sheet.paste(image.resize((SHEET_TILE_WIDTH, tile_height)), (x, y + label_height))
    return sheet


def encode(image: Any) -> dict[str, str]:
    """PNG keeps text sharp; a sheet above 1.5 MB falls back to JPEG quality 85."""
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    if buffer.tell() > _PNG_SIZE_LIMIT:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=85)
        return {"mime": "image/jpeg", "base64": base64.b64encode(buffer.getvalue()).decode("ascii")}
    return {"mime": "image/png", "base64": base64.b64encode(buffer.getvalue()).decode("ascii")}


def _ranges(numbers: Sequence[int]) -> str:
    """1-based page numbers as ``1-3, 5``."""
    parts: list[str] = []
    start = previous = None
    for number in numbers:
        if start is None:
            start = previous = number
        elif number == previous + 1:
            previous = number
        else:
            parts.append(f"{start}-{previous}" if start != previous else f"{start}")
            start = previous = number
    if start is not None:
        parts.append(f"{start}-{previous}" if start != previous else f"{start}")
    return ", ".join(parts)


def render_pdf_view(
    pdf: bytes,
    *,
    noun: str,
    pages: Sequence[int] | None = None,
    detail: str = "sheet",
    text_pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Render a PDF (an executor's PPTX render or a stored PDF) for the model.

    ``pages`` are zero-based; ``None`` means all. ``noun`` is ``slide`` or ``page``.
    ``text_pages`` (zero-based) are pages whose source has text; when every rendered
    page that has text comes out blank, the renderer failed (``blank_render``).
    """
    total = pdf_page_count(pdf)
    wanted = sorted({index for index in (pages if pages is not None else range(total)) if 0 <= index < total})
    if not wanted:
        return {"status": "not_completed", "reason": "render_failed", "pages": total}
    if detail == "full" and len(wanted) > 1:
        shown, rest = wanted[:MAX_FULL_PAGES], wanted[MAX_FULL_PAGES:]
        images = _pdf_page_images(pdf, shown, SINGLE_PAGE_WIDTH)
        blank = _all_text_pages_blank(images, shown, text_pages)
        if blank:
            return {"status": "not_completed", "reason": "blank_render", "pages": total}
        return {
            "status": "shown",
            "pages": total,
            "shown": f"{noun}s {_ranges([i + 1 for i in shown])}, each at {images[0].width}x{images[0].height} px",
            "not_shown": f"{noun}{'s' if len(rest) > 1 else ''} {_ranges([i + 1 for i in rest])}" if rest else None,
            "page_px": [images[0].width, images[0].height],
            "images": [{**encode(image), "label": f"{noun.capitalize()} {index + 1}"} for image, index in zip(images, shown, strict=True)],
        }
    if len(wanted) == 1:
        images = _pdf_page_images(pdf, wanted, SINGLE_PAGE_WIDTH)
        if _all_text_pages_blank(images, wanted, text_pages):
            return {"status": "not_completed", "reason": "blank_render", "pages": total}
        return {
            "status": "shown",
            "pages": total,
            "shown": f"{noun} {wanted[0] + 1}, at {images[0].width}x{images[0].height} px",
            "not_shown": None,
            "page_px": [images[0].width, images[0].height],
            "images": [{**encode(images[0]), "label": f"{noun.capitalize()} {wanted[0] + 1}"}],
        }
    capacity = MAX_PAGES_PER_SHEET * MAX_SHEETS_PER_FILE
    shown, rest = wanted[:capacity], wanted[capacity:]
    images = _pdf_page_images(pdf, shown, SHEET_TILE_WIDTH)
    if _all_text_pages_blank(images, shown, text_pages):
        return {"status": "not_completed", "reason": "blank_render", "pages": total}
    sheets = []
    for start in range(0, len(shown), MAX_PAGES_PER_SHEET):
        chunk = shown[start : start + MAX_PAGES_PER_SHEET]
        sheet = tile(images[start : start + len(chunk)], [f"{noun.capitalize()} {index + 1}" for index in chunk])
        sheets.append({**encode(sheet), "label": f"{noun}s {_ranges([i + 1 for i in chunk])}"})
    tile_px = [images[0].width, images[0].height]
    return {
        "status": "shown",
        "pages": total,
        "shown": f"{noun}s {_ranges([i + 1 for i in shown])} in {len(sheets)} sheet{'s' if len(sheets) > 1 else ''}, each {noun} at {tile_px[0]}x{tile_px[1]} px",
        "not_shown": f"{noun}s {_ranges([i + 1 for i in rest])}" if rest else None,
        "page_px": tile_px,
        "images": sheets,
    }


def _all_text_pages_blank(images: Sequence[Any], indexes: Sequence[int], text_pages: Sequence[int] | None) -> bool:
    populated = [image for image, index in zip(images, indexes, strict=True) if text_pages is None or index in set(text_pages)]
    return bool(populated) and all(is_blank(image) for image in populated)


def render_image_view(data: bytes) -> dict[str, Any]:
    """A stored PNG/JPEG/WebP, downscaled to at most ``SINGLE_PAGE_WIDTH`` on the long side."""
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    original = (image.width, image.height)
    image = image.convert("RGB")
    image.thumbnail((SINGLE_PAGE_WIDTH, SINGLE_PAGE_WIDTH))
    if is_blank(image):
        return {"status": "not_completed", "reason": "blank_render", "pages": 1}
    return {
        "status": "shown",
        "pages": 1,
        "shown": f"the image at {image.width}x{image.height} px"
        + (f" (stored {original[0]}x{original[1]})" if original != (image.width, image.height) else ""),
        "not_shown": None,
        "page_px": [image.width, image.height],
        "images": [{**encode(image), "label": "Image"}],
    }


SCREEN_HEIGHT = 800


def _screens(image: Any) -> list[Any]:
    """A page capture cut into 1280x800 screens; the last one padded with white."""
    from PIL import Image

    screens = []
    for top in range(0, image.height, SCREEN_HEIGHT):
        screen = image.crop((0, top, image.width, min(top + SCREEN_HEIGHT, image.height)))
        if screen.height < SCREEN_HEIGHT:
            padded = Image.new("RGB", (image.width, SCREEN_HEIGHT), (255, 255, 255))
            padded.paste(screen, (0, 0))
            screen = padded
        screens.append(screen)
    return screens


def render_screens_view(
    png: bytes, *, kind: str, pages: Sequence[int] | None = None, detail: str = "sheet", text_present: bool = True
) -> dict[str, Any]:
    """An HTML page capture (slice B) as screens; an SVG capture as one image.

    Default: the first screen at full width and, for a longer page, every screen in a
    numbered sheet. ``detail`` ``full`` shows up to ``MAX_FULL_PAGES`` chosen screens at
    full width. A capture whose screens are all one colour while the page has text is
    ``blank_render``.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    image.load()
    image = image.convert("RGB")
    if kind == "svg":
        if is_uniform(image):
            return {"status": "not_completed", "reason": "blank_render", "pages": 1}
        original = (image.width, image.height)
        image.thumbnail((SINGLE_PAGE_WIDTH, SINGLE_PAGE_WIDTH))
        resized = "" if original == (image.width, image.height) else f" (captured {original[0]}x{original[1]})"
        return {
            "status": "shown",
            "pages": 1,
            "shown": f"the SVG at {image.width}x{image.height} px{resized}",
            "not_shown": None,
            "page_px": [image.width, image.height],
            "images": [{**encode(image), "label": "SVG"}],
        }
    screens = _screens(image)
    total = len(screens)
    if text_present and all(is_uniform(screen) for screen in screens):
        return {"status": "not_completed", "reason": "blank_render", "pages": total}
    wanted = sorted({index for index in (pages if pages is not None else range(total)) if 0 <= index < total})
    if not wanted:
        return {"status": "not_completed", "reason": "render_failed", "pages": total}
    width, height = screens[0].width, SCREEN_HEIGHT
    if detail == "full" or len(wanted) == 1:
        shown, rest = wanted[:MAX_FULL_PAGES], wanted[MAX_FULL_PAGES:]
        noun = "screen" if len(shown) == 1 else "screens"
        return {
            "status": "shown",
            "pages": total,
            "shown": f"{noun} {_ranges([i + 1 for i in shown])}, each at {width}x{height} px",
            "not_shown": f"screens {_ranges([i + 1 for i in rest])}" if rest else None,
            "page_px": [width, height],
            "images": [{**encode(screens[index]), "label": f"Screen {index + 1}"} for index in shown],
        }
    in_sheet, rest = wanted[:MAX_PAGES_PER_SHEET], wanted[MAX_PAGES_PER_SHEET:]
    sheet = tile([screens[index] for index in in_sheet], [f"Screen {index + 1}" for index in in_sheet])
    tile_height = round(height * SHEET_TILE_WIDTH / width)
    return {
        "status": "shown",
        "pages": total,
        "shown": (
            f"screen {wanted[0] + 1} at {width}x{height} px, then screens {_ranges([i + 1 for i in in_sheet])} in 1 sheet, "
            f"each screen at {SHEET_TILE_WIDTH}x{tile_height} px"
        ),
        "not_shown": f"screens {_ranges([i + 1 for i in rest])}" if rest else None,
        "page_px": [SHEET_TILE_WIDTH, tile_height],
        "images": [
            {**encode(screens[wanted[0]]), "label": f"Screen {wanted[0] + 1}"},
            {**encode(sheet), "label": f"screens {_ranges([i + 1 for i in in_sheet])}"},
        ],
    }


def html_content_snapshot(text: str) -> dict[str, Any]:
    """Visible text of a captured HTML page, for comparing a declared presentation fix."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {"kind": "html", "texts": [lines], "tables": [[]], "charts": {}}


def svg_content_snapshot(svg: bytes) -> dict[str, Any] | None:
    """The text of an SVG's ``<text>`` and ``<foreignObject>`` elements; ``None`` when it is not well-formed XML.

    The capture shows the SVG as an image, which has no readable text, so the file is read instead.
    """
    from lxml import etree

    try:
        root = etree.fromstring(svg, etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))
    except etree.XMLSyntaxError:
        return None
    lines: list[str] = []
    for element in root.iter("{*}text", "{*}foreignObject"):
        if any(etree.QName(parent).localname in {"text", "foreignObject"} for parent in element.iterancestors()):
            continue
        text = " ".join("".join(element.itertext()).split())
        if text:
            lines.append(text)
    return {"kind": "svg", "texts": [lines], "tables": [[]], "charts": {}}


# ------------------------------------------------------------- PPTX structure


def _walk(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _walk(shape.shapes)


def pptx_slide_hashes(pptx: bytes) -> dict[str, Any]:
    """Per-slide hashes over the slide part and the parts it uses; one hash for layouts, masters and theme."""
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(pptx))
    slides = []
    for slide in presentation.slides:
        digest = hashlib.sha256(slide.part.blob)
        for rel in sorted(slide.part.rels.values(), key=lambda r: r.rId):
            if rel.is_external or "slideLayout" in rel.reltype:
                continue
            digest.update(rel.target_part.blob)
        slides.append(digest.hexdigest())
    shared = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(pptx)) as archive:
        for name in sorted(archive.namelist()):
            if name.startswith(("ppt/slideLayouts/", "ppt/slideMasters/", "ppt/theme/")) and name.endswith(".xml"):
                shared.update(name.encode())
                shared.update(archive.read(name))
    return {"slides": slides, "shared": shared.hexdigest()}


def changed_slides(before: dict[str, Any] | None, after: dict[str, Any]) -> list[int] | None:
    """Zero-based slides to render again, or ``None`` for all (first save, layout change, new count)."""
    if not before or before.get("shared") != after.get("shared") or len(before.get("slides") or []) != len(after["slides"]):
        return None
    return [index for index, (old, new) in enumerate(zip(before["slides"], after["slides"], strict=True)) if old != new]


def pptx_text_slides(pptx: bytes) -> list[int]:
    """Zero-based slides with any text, for blank-render detection."""
    from pptx import Presentation

    out = []
    for index, slide in enumerate(Presentation(io.BytesIO(pptx)).slides):
        if any((shape.has_text_frame and shape.text_frame.text.strip()) or getattr(shape, "has_table", False) for shape in _walk(slide.shapes)):
            out.append(index)
    return out


def _font_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_RUN = re.compile(r"<a:r>(.*?)</a:r>", re.S)
_TYPEFACE = re.compile(r'<a:(?:latin|ea|cs) typeface="([^"]+)"')
# LibreOffice substitutes these with fonts of identical metrics; wrapping does not change.
METRIC_COMPATIBLE = {
    "calibri": "carlito",
    "cambria": "caladea",
    "arial": "liberationsans",
    "helvetica": "liberationsans",
    "timesnewroman": "liberationserif",
    "couriernew": "liberationmono",
}


def pptx_requested_fonts(pptx: bytes) -> list[str]:
    """Font families the deck's text uses: explicit run fonts, and theme fonts where text has none.

    Theme fonts count when a slide run carries no explicit typeface or a chart sets no
    typeface of its own, since that text then renders in the theme font.
    """
    names: set[str] = set()
    uses_theme = False
    with zipfile.ZipFile(io.BytesIO(pptx)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            if name.startswith("ppt/slides/slide"):
                xml = archive.read(name).decode("utf-8", "replace")
                for run in _RUN.findall(xml):
                    faces = _TYPEFACE.findall(run)
                    names.update(face for face in faces if not face.startswith("+"))
                    uses_theme = uses_theme or not faces or any(face.startswith("+") for face in faces)
            elif name.startswith("ppt/charts/chart"):
                xml = archive.read(name).decode("utf-8", "replace")
                faces = _TYPEFACE.findall(xml)
                names.update(face for face in faces if not face.startswith("+"))
                uses_theme = uses_theme or not faces
        if uses_theme:
            for name in archive.namelist():
                if name.startswith("ppt/theme/theme") and name.endswith(".xml"):
                    xml = archive.read(name).decode("utf-8", "replace")
                    names.update(re.findall(r'<a:(?:latin|ea) typeface="([^"]+)"', xml))
    return sorted(name for name in names if name and not name.startswith("+"))


def pdf_font_names(pdf: bytes) -> list[str]:
    """Base font names embedded in a PDF, subset prefixes removed."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    names: set[str] = set()
    for page in reader.pages:
        resources = page.get("/Resources") or {}
        fonts = resources.get("/Font") if hasattr(resources, "get") else None
        if not fonts:
            continue
        for reference in fonts.values():
            font = reference.get_object()
            base = str(font.get("/BaseFont") or "").lstrip("/")
            names.add(base.split("+", 1)[-1])
    return sorted(name for name in names if name)


def substituted_fonts(requested: Sequence[str], rendered: Sequence[str]) -> dict[str, Any] | None:
    """Requested Latin-script families with no embedded font of that name.

    Non-ASCII family names (for example 思源黑体) cannot be matched against PostScript
    names in the PDF and are not reported.
    """
    rendered_keys = [_font_key(name) for name in rendered]

    def present(name: str) -> bool:
        key = _font_key(name)
        compatible = METRIC_COMPATIBLE.get(key)
        return any(found.startswith(key) or (compatible is not None and found.startswith(compatible)) for found in rendered_keys)

    missing = [name for name in requested if name.isascii() and _font_key(name) and not present(name)]
    if not missing:
        return None
    families = sorted({re.split(r"[-,]", name)[0] for name in rendered})
    return {"requested": missing, "rendered_with": families[:8]}


# ---------------------------------------------------------------- content


def _norm(text: str) -> str:
    return _SPACE.sub("", text)


def _numbers(texts: Iterable[str]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(number.replace(",", "") for number in _NUMBER.findall(text))
    return counter


def pptx_content_snapshot(pptx: bytes) -> dict[str, Any]:
    """Text paragraphs, table rows and chart series per slide."""
    from pptx import Presentation

    texts: list[list[str]] = []
    tables: list[list[list[str]]] = []
    charts: dict[str, dict[str, Any]] = {}
    for slide_index, slide in enumerate(Presentation(io.BytesIO(pptx)).slides, start=1):
        slide_texts: list[str] = []
        slide_rows: list[list[str]] = []
        for shape in _walk(slide.shapes):
            if shape.has_text_frame:
                slide_texts.extend(paragraph.text.strip() for paragraph in shape.text_frame.paragraphs if paragraph.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    slide_rows.append([cell.text.strip() for cell in row.cells])
            if getattr(shape, "has_chart", False):
                chart = shape.chart
                try:
                    categories = [str(category) for category in chart.plots[0].categories] if chart.plots else []
                except Exception:  # noqa: BLE001 - a chart without category data still has series
                    categories = []
                for series in chart.series:
                    values = [None if value is None else float(value) for value in series.values]
                    charts[f"slide {slide_index} '{series.name}'"] = {"categories": categories, "values": values}
        texts.append(slide_texts)
        tables.append(slide_rows)
    return {"kind": "pptx", "texts": texts, "tables": tables, "charts": charts}


def pdf_content_snapshot(pdf: bytes) -> dict[str, Any]:
    """Text lines per page of a PDF."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    texts = [[line.strip() for line in (page.extract_text() or "").splitlines() if line.strip()] for page in reader.pages]
    return {"kind": "pdf", "texts": texts, "tables": [[] for _ in texts], "charts": {}}


def pdf_text_pages(pdf: bytes) -> list[int]:
    return [index for index, lines in enumerate(pdf_content_snapshot(pdf)["texts"]) if lines]


def _unit(snapshot: dict[str, Any]) -> str:
    return "slide" if snapshot.get("kind") == "pptx" else "page"


def compare_content(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]] | None:
    """Content differences after a shown render; ``None`` when nothing changed.

    Text is compared without whitespace, and a paragraph counts as unchanged when it is
    contained in the other version's text of the same slide, so re-flowed or split
    sentences are equal. Numbers are a multiset over the file. Removed table rows and
    changed chart series are reported.
    """
    unit = _unit(after)
    added_text: list[str] = []
    removed_text: list[str] = []
    before_texts, after_texts = before.get("texts") or [], after.get("texts") or []
    for index in range(max(len(before_texts), len(after_texts))):
        old = before_texts[index] if index < len(before_texts) else []
        new = after_texts[index] if index < len(after_texts) else []
        old_joined, new_joined = "".join(_norm(text) for text in old), "".join(_norm(text) for text in new)
        added_text += [f"{unit} {index + 1}: {text}" for text in new if _norm(text) and _norm(text) not in old_joined]
        removed_text += [f"{unit} {index + 1}: {text}" for text in old if _norm(text) and _norm(text) not in new_joined]
    old_numbers = _numbers(text for page in before_texts for text in page) + _numbers(
        cell for page in before.get("tables") or [] for row in page for cell in row
    )
    new_numbers = _numbers(text for page in after_texts for text in page) + _numbers(
        cell for page in after.get("tables") or [] for row in page for cell in row
    )
    added_numbers = sorted((new_numbers - old_numbers).elements())
    removed_numbers = sorted((old_numbers - new_numbers).elements())
    removed_rows: list[str] = []
    added_rows: list[str] = []
    before_tables, after_tables = before.get("tables") or [], after.get("tables") or []
    for index in range(max(len(before_tables), len(after_tables))):
        old_rows = [tuple(row) for row in (before_tables[index] if index < len(before_tables) else [])]
        new_rows = [tuple(row) for row in (after_tables[index] if index < len(after_tables) else [])]
        gone = list((Counter(old_rows) - Counter(new_rows)).elements())
        new = list((Counter(new_rows) - Counter(old_rows)).elements())
        removed_rows += [f"{unit} {index + 1} table row: {' | '.join(row)}" for row in gone]
        added_rows += [f"{unit} {index + 1} table row: {' | '.join(row)}" for row in new]
    changed_series: list[str] = []
    before_charts, after_charts = before.get("charts") or {}, after.get("charts") or {}
    for key, series in after_charts.items():
        old = before_charts.get(key)
        if old is None:
            changed_series.append(f"{key}: new series {series['categories']} {series['values']}")
        elif old != series:
            changed_series.append(f"{key}: {old['categories']} {old['values']} -> {series['categories']} {series['values']}")
    changed_series += [f"{key}: series removed" for key in before_charts if key not in after_charts]
    changes = {
        "added_text": added_text,
        "removed_text": removed_text,
        "added_numbers": added_numbers,
        "removed_numbers": removed_numbers,
        "added_rows": added_rows,
        "removed_rows": removed_rows,
        "changed_series": changed_series,
    }
    changes = {key: value[:MAX_CHANGES_PER_KIND] for key, value in changes.items() if value}
    return changes or None


# -------------------------------------------------------------- worker entry


def visual_worker_entry(control: dict[str, Any], data: bytes, extra: dict[str, bytes]) -> dict[str, Any]:
    """Single ``untrusted_parse_worker`` entry (kind ``visual_view``).

    ``op``: ``screens_view`` (data = an HTML or SVG capture; an extra ``svg`` file adds its
    content snapshot), ``pptx_render`` (one process for a saved PPTX: slide hashes, content snapshot,
    the slides changed since ``previous_hashes`` and, with ``render`` true and an extra
    ``pdf``, their view), ``pptx_view`` (data = PPTX, extra pdf = the executor render),
    ``pdf_view`` (with ``snapshot`` true it adds the PDF's content snapshot),
    ``image_view``, ``pptx_inspect`` or ``pdf_inspect``.
    """
    op = control.get("op")
    if op == "screens_view":
        view = render_screens_view(
            data,
            kind=control.get("kind") or "html",
            pages=control.get("pages"),
            detail=control.get("detail") or "sheet",
            text_present=bool(control.get("text_present")),
        )
        if "svg" in extra:
            view["snapshot"] = svg_content_snapshot(extra["svg"])
        return view
    if op == "pptx_render":
        hashes = pptx_slide_hashes(data)
        result: dict[str, Any] = {"hashes": hashes, "snapshot": pptx_content_snapshot(data)}
        pages = control["pages"] if "pages" in control else changed_slides(control.get("previous_hashes"), hashes)
        result["changed_pages"] = pages
        if control.get("render") and pages != [] and "pdf" in extra:
            result["view"] = visual_worker_entry({"op": "pptx_view", "pages": pages, "detail": control.get("detail")}, data, extra)
        return result
    if op == "image_view":
        return render_image_view(data)
    if op == "pptx_inspect":
        return {"hashes": pptx_slide_hashes(data), "snapshot": pptx_content_snapshot(data)}
    if op == "pdf_inspect":
        return {"snapshot": pdf_content_snapshot(data)}
    if op == "pptx_view":
        pdf = extra["pdf"]
        view = render_pdf_view(
            pdf, noun="slide", pages=control.get("pages"), detail=control.get("detail") or "sheet", text_pages=pptx_text_slides(data)
        )
        fonts = substituted_fonts(pptx_requested_fonts(data), pdf_font_names(pdf))
        if fonts and view.get("status") == "shown":
            view["substituted_fonts"] = fonts
        return view
    if op == "pdf_view":
        snapshot = pdf_content_snapshot(data)
        text_pages = [index for index, lines in enumerate(snapshot["texts"]) if lines]
        view = render_pdf_view(data, noun="page", pages=control.get("pages"), detail=control.get("detail") or "sheet", text_pages=text_pages)
        if control.get("snapshot"):
            view["snapshot"] = snapshot
        return view
    raise ValueError(f"unknown visual_view op {op!r}")
