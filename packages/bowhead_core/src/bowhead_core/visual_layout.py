"""The layout of a captured HTML page, measured in the sandbox at capture time, and what it gives the model.

Design: docs/sprints/visual-geometry-2026-11-27/tech-design.md §3, §5, §5b and §6. The sandbox worker
(``documents/html_view.py``) writes ``layout.json``; this module validates it, cuts screens at the page's own
block boundaries, names screens, elements and measured findings, and finds the crop for an element or region
view. Pure Python: it runs in the application and in the parse worker.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MAX_LAYOUT_BYTES = 65_536
MAX_BLOCKS = 64
MAX_GUARDS = 1_000
MAX_ELEMENTS = 120
MAX_FINDINGS = 40
MAX_LABEL = 40
MAX_COORDINATE = 1_000_000
SCREEN_HEIGHT = 800
# A screen ends at a block boundary at least this far below its top.
MIN_BLOCK_SCREEN = 100
# Inside a block taller than a screen, a screen ends at a free row at least this far below its top.
MIN_SCREEN = 200
ELEMENT_MARGIN = 16
MAX_VIEW_IMAGES = 4
FINDINGS_IN_MESSAGE = 12
ELEMENTS_PER_SCREEN = 8
SCREEN_MAP_ENTRIES = 12
ELEMENT_KINDS = ("chart", "picture", "table", "heading")
FINDING_KINDS = ("clipped", "beyond_page_width", "chart_text_outside_chart", "outside_block", "text_overlap")
_REF = re.compile(r"^(?P<kind>[be])(?P<number>[1-9][0-9]{0,3})$")


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class Block:
    ref: str
    y: int
    h: int
    label: str

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class Element:
    ref: str
    kind: str
    box: Box
    label: str


@dataclass(frozen=True)
class Finding:
    kind: str
    selector: str
    text: str
    box: Box
    other_text: str = ""
    other_box: Box | None = None
    edge: str = ""
    right: int = 0


@dataclass(frozen=True)
class PageLayout:
    width: int
    height: int
    blocks: tuple[Block, ...]
    guards: tuple[tuple[int, int], ...]
    elements: tuple[Element, ...]
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        """The ``layout.json`` shape, for passing to the parse worker."""
        return {
            "version": 1,
            "width": self.width,
            "height": self.height,
            "blocks": [{"y": b.y, "h": b.h, "label": b.label} for b in self.blocks],
            "guards": [list(g) for g in self.guards],
            "elements": [{"kind": e.kind, **_box_dict(e.box), "label": e.label} for e in self.elements],
            "findings": [_finding_dict(f) for f in self.findings],
        }


def _box_dict(box: Box) -> dict[str, int]:
    return {"x": box.x, "y": box.y, "w": box.w, "h": box.h}


def _finding_dict(f: Finding) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": f.kind, "selector": f.selector, "text": f.text, "box": _box_dict(f.box)}
    if f.other_text:
        out["other_text"] = f.other_text
    if f.other_box is not None:
        out["other_box"] = _box_dict(f.other_box)
    if f.edge:
        out["edge"] = f.edge
    if f.right:
        out["right"] = f.right
    return out


class LayoutInvalid(ValueError):
    """``layout.json`` does not have the agreed shape or bounds."""


def _int(value: Any, *, low: int = -MAX_COORDINATE, high: int = MAX_COORDINATE) -> int:
    if type(value) is not int or not low <= value <= high:
        raise LayoutInvalid("number")
    return value


def _text(value: Any, limit: int = MAX_LABEL) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise LayoutInvalid("text")
    return value


def _box(value: Any) -> Box:
    if not isinstance(value, dict):
        raise LayoutInvalid("box")
    return Box(_int(value.get("x")), _int(value.get("y")), _int(value.get("w"), low=0), _int(value.get("h"), low=0))


def _list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise LayoutInvalid("list")
    return value


def parse_layout(raw: bytes | dict[str, Any]) -> PageLayout:
    """Validate ``layout.json`` (bytes from the worker, or its dict); raise ``LayoutInvalid``."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_LAYOUT_BYTES:
            raise LayoutInvalid("size")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LayoutInvalid("json") from exc
    else:
        data = raw
    if not isinstance(data, dict) or data.get("version") != 1:
        raise LayoutInvalid("version")
    width, height = _int(data.get("width"), low=1), _int(data.get("height"), low=1)
    blocks = tuple(
        Block(f"b{n}", _int(b.get("y"), low=0), _int(b.get("h"), low=1), _text(b.get("label")))
        for n, b in enumerate(_list(data.get("blocks"), MAX_BLOCKS), start=1)
        if isinstance(b, dict) or _raise("block")
    )
    guards: list[tuple[int, int]] = []
    for g in _list(data.get("guards"), MAX_GUARDS):
        if not isinstance(g, list) or len(g) != 2:
            raise LayoutInvalid("guard")
        a, b = _int(g[0]), _int(g[1])
        if b <= a or (guards and a < guards[-1][0]):
            raise LayoutInvalid("guard order")
        guards.append((a, b))
    elements = []
    for n, e in enumerate(_list(data.get("elements"), MAX_ELEMENTS), start=1):
        if not isinstance(e, dict) or e.get("kind") not in ELEMENT_KINDS:
            raise LayoutInvalid("element")
        elements.append(Element(f"e{n}", e["kind"], _box(e), _text(e.get("label"))))
    findings = []
    for f in _list(data.get("findings"), MAX_FINDINGS):
        if not isinstance(f, dict) or f.get("kind") not in FINDING_KINDS:
            raise LayoutInvalid("finding")
        findings.append(
            Finding(
                kind=f["kind"],
                selector=_text(f.get("selector")),
                text=_text(f.get("text")),
                box=_box(f.get("box")),
                other_text=_text(f.get("other_text", "")),
                other_box=_box(f["other_box"]) if "other_box" in f else None,
                edge=f.get("edge", "") if f.get("edge", "") in {"", "top", "bottom"} else _raise("edge"),
                right=_int(f.get("right", 0), low=0),
            )
        )
    return PageLayout(width, height, blocks, tuple(guards), tuple(elements), tuple(findings))


def _raise(what: str) -> Any:
    raise LayoutInvalid(what)


# ---------------------------------------------------------------- screens


@dataclass(frozen=True)
class Cut:
    top: int
    bottom: int
    crossing: int | None = None  # a row the bottom edge runs through content at, when it does


def _inside(guards: Sequence[tuple[int, int]], y: int) -> bool:
    return any(a < y < b for a, b in guards)


def screen_cuts(layout: PageLayout, capture_height: int) -> list[Cut]:
    """Screens of at most ``SCREEN_HEIGHT`` px over ``capture_height`` px of the page (§5).

    A screen ends at the lowest block boundary ``MIN_BLOCK_SCREEN``-800 px below its top, even when content
    overflowing its block crosses it. Inside a block taller than a screen it ends at the free row nearest the
    middle of what remains of the block, so the next screen reaches the block's end. Otherwise at 800 px.
    """
    bounds = sorted({b.y for b in layout.blocks} | {b.bottom for b in layout.blocks})
    cuts: list[Cut] = []
    top = 0
    while top < capture_height:
        if capture_height - top <= SCREEN_HEIGHT:
            cuts.append(Cut(top, capture_height))
            break
        high = top + SCREEN_HEIGHT
        at_blocks = [y for y in bounds if top + MIN_BLOCK_SCREEN <= y <= high]
        if at_blocks:
            y = max(at_blocks)
            cuts.append(Cut(top, y, y if _inside(layout.guards, y) else None))
            top = y
            continue
        end = min((b.bottom for b in layout.blocks if b.y < high < b.bottom), default=high)
        target = (top + end) // 2 if end - top <= 2 * SCREEN_HEIGHT else high
        free = [y for y in range(top + MIN_SCREEN, high + 1) if not _inside(layout.guards, y)]
        if free:
            y = min(free, key=lambda v: (abs(v - target), -v))
            cuts.append(Cut(top, y))
            top = y
            continue
        cuts.append(Cut(top, high, high))
        top = high
    return cuts


def _label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 36 else text[:36] + "…"


def _range(a: int, b: int) -> str:
    return f"page y {a:,}-{b:,}"


def _crossing_name(layout: PageLayout, y: int) -> str:
    named = [e for e in layout.elements if e.box.y < y < e.box.bottom]
    if named:
        e = min(named, key=lambda e: e.box.h)
        return f"{e.ref} ({e.kind}, {_range(e.box.y, e.box.bottom)})"
    guard = next(((a, b) for a, b in layout.guards if a < y < b), None)
    return f"content at {_range(*guard)}" if guard else "content"


def screen_line(layout: PageLayout, cuts: Sequence[Cut], index: int, capture_height: int) -> str:
    """``Screen 7: page y 3,821-4,509 of 11,154 px; block b6 of 14 (…), continued from screen 6; named elements: …``."""
    cut = cuts[index]
    parts = []
    for b in layout.blocks:
        if b.y < cut.bottom and b.bottom > cut.top:
            name = f"block {b.ref} of {len(layout.blocks)} ('{_label(b.label)}', {_range(b.y, b.bottom)})"
            if b.y < cut.top:
                name += f", continued from screen {index}"
            if b.bottom > cut.bottom and cut.bottom < capture_height:
                name += f", continues on screen {index + 2}"
            parts.append(name)
    line = f"Screen {index + 1}: {_range(cut.top, cut.bottom)} of {layout.height:,} px; " + ("; ".join(parts) if parts else "between blocks")
    if cut.crossing is not None and index + 1 < len(cuts):
        line += f"; its bottom edge crosses {_crossing_name(layout, cut.crossing)}, which continues on screen {index + 2}"
    named = [e for e in layout.elements if e.box.y < cut.bottom and e.box.bottom > cut.top]
    if named:
        items = []
        for e in named[:ELEMENTS_PER_SCREEN]:
            text = f" '{_label(e.label)}'" if e.kind in {"heading", "table"} and e.label else ""
            items.append(f"{e.ref} {e.kind}{text} ({_range(e.box.y, e.box.bottom)})")
        more = f", and {len(named) - ELEMENTS_PER_SCREEN} more" if len(named) > ELEMENTS_PER_SCREEN else ""
        line += "; named elements: " + ", ".join(items) + more
    if cut.bottom == capture_height and capture_height < layout.height:
        line += f"; the capture stops here at page y {capture_height:,}, so the rest of the page is not shown"
    return line + "."


def screen_map(layout: PageLayout, cuts: Sequence[Cut], indices: Sequence[int], capture_height: int) -> str:
    """``Screens: 1 = page y 0-720 (b1), 2 = …`` for a sheet."""
    items = []
    for i in list(indices)[:SCREEN_MAP_ENTRIES]:
        cut = cuts[i]
        refs = [b.ref for b in layout.blocks if b.y < cut.bottom and b.bottom > cut.top]
        items.append(f"{i + 1} = {_range(cut.top, cut.bottom)}" + (f" ({', '.join(refs)})" if refs else ""))
    line = "Screens: " + ", ".join(items)
    if capture_height < layout.height:
        line += f"; the capture stops at page y {capture_height:,} of {layout.height:,} px, so the rest of the page is not shown"
    return line + "."


def findings_sentence(layout: PageLayout) -> str:
    """Measured layout facts, never judgements (§3, §6)."""
    if not layout.findings:
        return "no clipped, overlapping or out-of-bounds text measured."
    out = []
    for f in layout.findings[:FINDINGS_IN_MESSAGE]:
        if f.kind == "clipped":
            out.append(f"{f.selector} at {_range(f.box.y, f.box.bottom)} hides overflow, and its '{_label(f.other_text)}' extends past its edge.")
        elif f.kind == "beyond_page_width":
            out.append(f"'{_label(f.text)}' ({f.selector}) at page y {f.box.y:,} extends to x {f.right:,} px; the page is {layout.width:,} px wide.")
        elif f.kind == "chart_text_outside_chart" and f.other_box is not None:
            out.append(
                f"Chart {f.selector} at {_range(f.other_box.y, f.other_box.bottom)}: text '{_label(f.text)}' lies partly outside the chart, which clips it."
            )
        elif f.kind == "outside_block" and f.other_box is not None:
            reach = f.box.y if f.edge == "top" else f.box.bottom
            limit = f.other_box.y if f.edge == "top" else f.other_box.bottom
            out.append(
                f"Block '{_label(f.other_text)}' spans {_range(f.other_box.y, f.other_box.bottom)}, but its '{_label(f.text)}' ({f.selector}) "
                f"reaches page y {reach:,}, past the block's {f.edge or 'bottom'} at {limit:,}."
            )
        elif f.kind == "text_overlap":
            out.append(f"At page y {f.box.y:,} the text '{_label(f.text)}' overlaps the text '{_label(f.other_text)}'.")
    more = f" And {len(layout.findings) - FINDINGS_IN_MESSAGE} more." if len(layout.findings) > FINDINGS_IN_MESSAGE else ""
    return " ".join(out) + more


# ---------------------------------------------------------------- element and region views


class ViewTargetInvalid(ValueError):
    """An element or region view that cannot be shown; ``str()`` is the model-facing reason."""


def element_target(layout: PageLayout | None, ref: str, capture_height: int) -> tuple[Box, str]:
    """(crop box, description) for ``ref``; raise ``ViewTargetInvalid`` naming the refs that exist."""
    if layout is None:
        raise ViewTargetInvalid("this capture has no element refs; view_file with region ([x1, y1, x2, y2] in page pixels) or pages works")
    match = _REF.match(ref.strip()) if isinstance(ref, str) else None
    found: Block | Element | None = None
    if match:
        pool: Sequence[Block | Element] = layout.blocks if match["kind"] == "b" else layout.elements
        number = int(match["number"])
        found = pool[number - 1] if number <= len(pool) else None
    if found is None:
        refs = ", ".join(x for x in (_refs_span("b", len(layout.blocks)), _refs_span("e", len(layout.elements))) if x)
        raise ViewTargetInvalid(
            f"{ref!r} is not a ref of this version of the file; its refs are {refs or 'none'}, and the latest render lists them per screen"
        )
    box = found.box if isinstance(found, Element) else Box(0, found.y, layout.width, found.h)
    top = max(0, box.y - ELEMENT_MARGIN)
    if top >= capture_height:
        raise ViewTargetInvalid(f"{ref} starts at page y {box.y:,}, below the capture, which stops at page y {capture_height:,}")
    left = max(0, box.x - ELEMENT_MARGIN)
    right = max(left + 1, min(layout.width, box.x + box.w + ELEMENT_MARGIN))
    crop = Box(left, top, right - left, min(capture_height, box.bottom + ELEMENT_MARGIN) - top)
    kind = found.kind if isinstance(found, Element) else "block"
    description = f"element {found.ref} ({kind}, {_range(box.y, box.bottom)} of {layout.height:,} px)"
    if box.bottom > capture_height:
        description += f"; the capture stops at page y {capture_height:,}, so the rest of it is not shown"
    return crop, description


def _refs_span(prefix: str, count: int) -> str:
    return "" if count == 0 else f"{prefix}1" if count == 1 else f"{prefix}1-{prefix}{count}"


def region_target(region: Any, width: int, capture_height: int, page_height: int) -> tuple[Box, str]:
    """(crop box, description) for ``[x1, y1, x2, y2]`` in page pixels, clamped to the capture."""
    if not isinstance(region, list | tuple) or len(region) != 4 or any(type(v) is not int for v in region):
        raise ViewTargetInvalid("region is [x1, y1, x2, y2]: four whole page pixels")
    x1, y1, x2, y2 = (max(0, v) for v in region)
    asked_bottom = y2
    x1, x2 = min(x1, width), min(x2, width)
    y1, y2 = min(y1, capture_height), min(y2, capture_height)
    if x2 <= x1 or y2 <= y1:
        raise ViewTargetInvalid(f"region {list(region)} is empty within the capture, which is {width:,} px wide and {capture_height:,} px tall")
    description = f"page region x {x1:,}-{x2:,}, y {y1:,}-{y2:,} of {page_height:,} px"
    if asked_bottom > capture_height and capture_height < page_height:
        description += f"; the capture stops at page y {capture_height:,}, so the rest of the region is not shown"
    return Box(x1, y1, x2 - x1, y2 - y1), description


def crop_slices(layout: PageLayout | None, crop: Box) -> list[tuple[int, int]]:
    """Rows of ``crop`` cut into at most ``MAX_VIEW_IMAGES`` slices of at most ``SCREEN_HEIGHT`` px."""
    slices: list[tuple[int, int]] = []
    top, end = crop.y, crop.bottom
    guards = layout.guards if layout is not None else ()
    while top < end and len(slices) < MAX_VIEW_IMAGES:
        if end - top <= SCREEN_HEIGHT:
            slices.append((top, end))
            break
        high = top + SCREEN_HEIGHT
        free = [y for y in range(high, top + MIN_SCREEN - 1, -1) if not _inside(guards, y)]
        y = free[0] if free else high
        slices.append((top, y))
        top = y
    return slices
