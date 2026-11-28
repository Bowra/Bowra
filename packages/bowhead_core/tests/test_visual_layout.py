"""HTML page layout measured at capture time: validation, screen cuts, names, findings and element views.

Design: docs/sprints/visual-geometry-2026-11-27/tech-design.md §3-§6 and §5b. The layout dicts here have the
shape the sandbox worker writes (``documents/html_view.py`` ``_LAYOUT``); the worker script itself runs in the
sandbox image's Chromium.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image, ImageDraw

from bowhead_core import visual_layout as vl
from bowhead_core import visual_view as vv


def _layout(**changes: Any) -> dict[str, Any]:
    """A 1,700 px page: an overview block, a detail block taller than a screen, three named elements, one finding."""
    layout = {
        "version": 1,
        "width": 1280,
        "height": 1700,
        "blocks": [{"y": 0, "h": 800, "label": "Overview"}, {"y": 800, "h": 900, "label": "Detail by month"}],
        "guards": [[40, 160], [340, 460], [640, 760], [940, 1060], [1240, 1360], [1540, 1660]],
        "elements": [
            {"kind": "heading", "x": 80, "y": 40, "w": 820, "h": 40, "label": "Overview"},
            {"kind": "chart", "x": 80, "y": 340, "w": 820, "h": 120, "label": ""},
            {"kind": "chart", "x": 80, "y": 940, "w": 820, "h": 120, "label": ""},
        ],
        "findings": [{"kind": "text_overlap", "selector": "", "text": "Q3", "box": {"x": 100, "y": 950, "w": 30, "h": 14}, "other_text": "Q4"}],
    }
    return {**layout, **changes}


def _png(height: int = 1700) -> bytes:
    image = Image.new("RGB", (1280, height), "white")
    draw = ImageDraw.Draw(image)
    for top in range(40, height - 40, 300):
        draw.rectangle((80, top, 900, top + 120), fill=(40, 60, 120))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_the_worker_layout_round_trips_and_is_bounded() -> None:
    layout = vl.parse_layout(json.dumps(_layout()).encode())
    assert [b.ref for b in layout.blocks] == ["b1", "b2"] and [e.ref for e in layout.elements] == ["e1", "e2", "e3"]
    assert vl.parse_layout(layout.to_dict()) == layout
    for bad in (
        _layout(version=2),
        _layout(guards=[[340, 460], [40, 160]]),
        _layout(guards=[[40, 40]]),
        _layout(width=1280.5),
        _layout(elements=[{"kind": "button", "x": 0, "y": 0, "w": 1, "h": 1, "label": ""}]),
        _layout(findings=[{"kind": "ugly", "selector": "", "text": "", "box": {"x": 0, "y": 0, "w": 1, "h": 1}}]),
        _layout(blocks=[{"y": 0, "h": 800, "label": "x" * 41}]),
    ):
        with pytest.raises(vl.LayoutInvalid):
            vl.parse_layout(bad)
    with pytest.raises(vl.LayoutInvalid):
        vl.parse_layout(b"{" + b" " * vl.MAX_LAYOUT_BYTES + b"}")


def test_screens_end_at_block_boundaries_and_split_a_tall_block_near_its_middle() -> None:
    layout = vl.parse_layout(_layout())
    # The detail block (900 px) is split at the free row nearest its middle, so the next screen reaches its end.
    assert [(c.top, c.bottom, c.crossing) for c in vl.screen_cuts(layout, 1700)] == [(0, 800, None), (800, 1240, None), (1240, 1700, None)]


def test_a_screen_ends_at_a_block_boundary_even_when_content_crosses_it_and_names_the_crossing() -> None:
    # An svg overflowing a slide across the gap made screens run through the next slide (spike §4).
    layout = vl.parse_layout(
        _layout(
            height=1500,
            blocks=[{"y": 0, "h": 700, "label": "Slide 1"}, {"y": 700, "h": 800, "label": "Slide 2"}],
            guards=[[650, 760]],
            elements=[{"kind": "chart", "x": 80, "y": 650, "w": 600, "h": 110, "label": ""}],
            findings=[],
        )
    )
    cuts = vl.screen_cuts(layout, 1500)
    assert [(c.top, c.bottom, c.crossing) for c in cuts] == [(0, 700, 700), (700, 1500, None)]
    line = vl.screen_line(layout, cuts, 0, 1500)
    assert line == (
        "Screen 1: page y 0-700 of 1,500 px; block b1 of 2 ('Slide 1', page y 0-700); its bottom edge crosses e1 (chart, "
        "page y 650-760), which continues on screen 2; named elements: e1 chart (page y 650-760)."
    )


def test_a_screen_without_a_free_row_is_cut_at_800_px() -> None:
    layout = vl.parse_layout(_layout(height=2000, blocks=[{"y": 0, "h": 2000, "label": "Poster"}], guards=[[0, 2000]], elements=[], findings=[]))
    cuts = vl.screen_cuts(layout, 2000)
    assert [(c.top, c.bottom, c.crossing) for c in cuts] == [(0, 800, 800), (800, 1600, 1600), (1600, 2000, None)]
    assert "; its bottom edge crosses content at page y 0-2,000, which continues on screen 2." in vl.screen_line(layout, cuts, 0, 2000)


def test_screen_lines_name_the_part_of_the_page_its_blocks_and_elements() -> None:
    layout = vl.parse_layout(_layout())
    cuts = vl.screen_cuts(layout, 1700)
    assert vl.screen_line(layout, cuts, 1, 1700) == (
        "Screen 2: page y 800-1,240 of 1,700 px; block b2 of 2 ('Detail by month', page y 800-1,700), continues on screen 3; "
        "named elements: e3 chart (page y 940-1,060)."
    )
    assert vl.screen_line(layout, cuts, 2, 1700).startswith(
        "Screen 3: page y 1,240-1,700 of 1,700 px; block b2 of 2 ('Detail by month', page y 800-1,700), continued from screen 2"
    )
    assert vl.screen_map(layout, cuts, range(3), 1700) == "Screens: 1 = page y 0-800 (b1), 2 = page y 800-1,240 (b2), 3 = page y 1,240-1,700 (b2)."


def test_a_page_taller_than_the_capture_says_where_the_capture_stops() -> None:
    layout = vl.parse_layout(_layout(height=12000))
    cuts = vl.screen_cuts(layout, 1700)
    assert vl.screen_line(layout, cuts, 2, 1700).endswith("; the capture stops here at page y 1,700, so the rest of the page is not shown.")
    assert vl.screen_map(layout, cuts, [0], 1700).endswith("; the capture stops at page y 1,700 of 12,000 px, so the rest of the page is not shown.")


def test_findings_are_measurements_in_page_positions() -> None:
    box = {"x": 0, "y": 900, "w": 400, "h": 300}
    layout = vl.parse_layout(
        _layout(
            findings=[
                {"kind": "clipped", "selector": "div.card", "text": "Revenue", "box": box, "other_text": "Total 163.42"},
                {"kind": "beyond_page_width", "selector": "table.wide", "text": "Region", "box": box, "right": 1460},
                {
                    "kind": "chart_text_outside_chart",
                    "selector": "svg",
                    "text": "Nov",
                    "box": box,
                    "other_box": {"x": 0, "y": 940, "w": 800, "h": 120},
                },
                {
                    "kind": "outside_block",
                    "selector": "div.sub",
                    "text": "By province",
                    "box": {"x": 0, "y": 780, "w": 400, "h": 30},
                    "other_text": "Section 3",
                    "other_box": {"x": 0, "y": 800, "w": 1280, "h": 900},
                    "edge": "top",
                },
                {"kind": "text_overlap", "selector": "", "text": "Q3", "box": {"x": 0, "y": 950, "w": 30, "h": 14}, "other_text": "Q4"},
            ]
        )
    )
    assert vl.findings_sentence(layout) == (
        "div.card at page y 900-1,200 hides overflow, and its 'Total 163.42' extends past its edge. "
        "'Region' (table.wide) at page y 900 extends to x 1,460 px; the page is 1,280 px wide. "
        "Chart svg at page y 940-1,060: text 'Nov' lies partly outside the chart, which clips it. "
        "Block 'Section 3' spans page y 800-1,700, but its 'By province' (div.sub) reaches page y 780, past the block's top at 800. "
        "At page y 950 the text 'Q3' overlaps the text 'Q4'."
    )
    assert vl.findings_sentence(vl.parse_layout(_layout(findings=[]))) == "no clipped, overlapping or out-of-bounds text measured."


def test_element_and_region_targets_and_their_refusals() -> None:
    layout = vl.parse_layout(_layout())
    assert vl.element_target(layout, "e3", 1700) == (vl.Box(64, 924, 852, 152), "element e3 (chart, page y 940-1,060 of 1,700 px)")
    assert vl.element_target(layout, " b2 ", 1700) == (vl.Box(0, 784, 1280, 916), "element b2 (block, page y 800-1,700 of 1,700 px)")
    with pytest.raises(vl.ViewTargetInvalid, match="'e9' is not a ref of this version of the file; its refs are b1-b2, e1-e3"):
        vl.element_target(layout, "e9", 1700)
    with pytest.raises(vl.ViewTargetInvalid, match="below the capture, which stops at page y 900"):
        vl.element_target(layout, "e3", 900)
    assert vl.element_target(layout, "e3", 1000)[1].endswith("; the capture stops at page y 1,000, so the rest of it is not shown")
    assert vl.region_target([0, 900, 640, 1200], 1280, 1000, 1700)[1] == (
        "page region x 0-640, y 900-1,000 of 1,700 px; the capture stops at page y 1,000, so the rest of the region is not shown"
    )
    with pytest.raises(vl.ViewTargetInvalid, match="no element refs; view_file with region"):
        vl.element_target(None, "e1", 1700)
    assert vl.region_target([-10, 700, 2000, 900], 1280, 1700, 1700) == (vl.Box(0, 700, 1280, 200), "page region x 0-1,280, y 700-900 of 1,700 px")
    for bad in ([0, 0, 10], [0, 0, 10, 1.5], [100, 100, 100, 200], [0, 1800, 100, 1900]):
        with pytest.raises(vl.ViewTargetInvalid):
            vl.region_target(bad, 1280, 1700, 1700)
    # A tall element comes in slices of at most one screen, cut between elements, and at most 4 of them.
    assert vl.crop_slices(layout, vl.Box(0, 784, 1280, 916)) == [(784, 1540), (1540, 1700)]
    assert len(vl.crop_slices(None, vl.Box(0, 0, 1280, 9600))) == vl.MAX_VIEW_IMAGES


def test_the_screens_view_follows_the_layout_and_states_it() -> None:
    view = vv.render_screens_view(_png(), kind="html", layout=_layout())
    assert view["pages"] == 3 and view["images"][0]["px"] == [1280, 800]
    # The screen lines name what a cut crosses, so the pixel-cut wording is not added.
    assert view["shown"] == "screen 1 at 1280x800 px, then screens 1-3 in 1 sheet, each screen 400 px wide"
    assert view["screen_notes"] == [
        "Screen 1: page y 0-800 of 1,700 px; block b1 of 2 ('Overview', page y 0-800); named elements: e1 heading 'Overview' "
        "(page y 40-80), e2 chart (page y 340-460)."
    ]
    assert view["screen_map"] == "Screens: 1 = page y 0-800 (b1), 2 = page y 800-1,240 (b2), 3 = page y 1,240-1,700 (b2)."
    assert view["layout_findings"] == "At page y 950 the text 'Q3' overlaps the text 'Q4'."
    full = vv.render_screens_view(_png(), kind="html", layout=_layout(), pages=[1], detail="full")
    assert full["shown"] == "screen 2, 1280x440 px" and len(full["screen_notes"]) == 1 and "screen_map" not in full


def test_an_invalid_layout_falls_back_to_pixel_cuts() -> None:
    view = vv.render_screens_view(_png(), kind="html", layout=_layout(version=9))
    assert view["images"][0]["px"] == [1280, 780] and "screen_notes" not in view and "layout_findings" not in view


def test_element_and_region_views_are_crops_at_capture_resolution() -> None:
    element = vv.render_screens_view(_png(), kind="html", layout=_layout(), element="e3")
    assert element["status"] == "shown" and element["crop"] is True
    assert element["shown"] == "element e3 (chart, page y 940-1,060 of 1,700 px), 852x152 px" and element["not_shown"] is None
    assert [image["label"] for image in element["images"]] == ["Element e3"]
    assert Image.open(io.BytesIO(base64.b64decode(element["images"][0]["base64"]))).size == (852, 152)
    block = vv.render_screens_view(_png(), kind="html", layout=_layout(), element="b2")
    assert block["shown"] == "element b2 (block, page y 800-1,700 of 1,700 px), 1280 px wide and 756, 160 px tall in 2 images"
    region = vv.render_screens_view(_png(), kind="html", region=[0, 700, 1280, 900])
    assert region["shown"] == "page region x 0-1,280, y 700-900 of 1,700 px, 1280x200 px" and region["images"][0]["label"] == "Region"
    refused = vv.render_screens_view(_png(), kind="html", layout=_layout(), element="e7")
    assert refused == {
        "status": "not_completed",
        "reason": "view_target_invalid",
        "message": "'e7' is not a ref of this version of the file; its refs are b1-b2, e1-e3, and the latest render lists them per screen",
    }


def test_rows_of_a_solid_colour_block_are_not_blank() -> None:
    # A cut inside a purple divider slide read as an overflowing strip and the model reflowed the deck (an observed deployment).
    image = Image.new("RGB", (1280, 1800), "white")
    ImageDraw.Draw(image).rectangle((0, 500, 1279, 1500), fill=(88, 60, 170))
    # Its rows have no colour changes; the screen ends in the white band above it, not at row 600 inside it.
    assert vv._screen_cuts(image)[0] == (0, 449, False)
