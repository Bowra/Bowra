"""Facts about a presentation an Agent just wrote, returned with the tool result.

The model knows the code it ran, not what the file now is. This view states what
can be read from the saved file: charts without values, number formats, series of
very different size, text still equal to the template, added shapes that cover
others and shapes an opaque rectangle drawn after them hides. A render in the sandbox adds where the text was drawn: outside the page,
beyond its frame, over other text, or smaller than in the template. It echoes no
slide content, gives no instruction and no verdict.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact

_LOG = logging.getLogger(__name__)

MAX_VIEW_BYTES = 32 * 1024 * 1024  # documents.package_io.MAX_OFFICE_BYTES
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_MAX_DETAIL = 120
_MAX_GROUPED_DETAIL = 320
_MAX_CHART_FACTS = 8
_MAX_OVERLAP_FACTS = 6
_MAX_COVERED_FACTS = 6
_MAX_COVERED_NAMES = 4
_MAX_UNCHANGED_SLIDES = 6
_MAX_UNCHANGED_NAMES = 5
_SCALE_FACTOR = 100
_BACKGROUND_SHARE = 0.5
_OVERLAP_SHARE = 0.05
_LAYOUT_NOT_RENDERED = "Text layout is not rendered, so text running outside its shape is not reported."
NOT_CHECKED = (
    _LAYOUT_NOT_RENDERED,
    "Shapes inside groups, lines, rotated shapes, table heights and plots that share one axis are not compared.",
    "Data correctness is not checked.",
)
PPTX_RESULT_VIEW_GUIDANCE = (
    "A .pptx written by a sandbox call comes back with result_view: one summary line and facts read from the saved file "
    "(charts without values, General or percent number formats that do not fit the values, series of very different size, "
    "text and chart titles equal to the attached template, "
    "slides identical to the template, including slides that an earlier file of this Turn had changed, "
    "added shapes that cover others, shapes under an opaque rectangle drawn after them, "
    "layout or master pictures under an opaque shape over the whole slide) and, when the sandbox could render the file, "
    "layout facts measured in that render "
    "(text drawn outside the page, beyond its frame, over other text, or smaller than in the template), "
    "or the fact that the renderer produced no pages from the file. "
    "It lists what was not checked and gives no verdict. The file's content is read by "
    "passing its artifact_id in a later call's inputs."
)


def is_pptx(mime: str | None, filename: str | None) -> bool:
    return (mime or "").split(";", 1)[0].strip().lower() == PPTX_MIME or (filename or "").lower().endswith(".pptx")


def _cut(text: str, limit: int = _MAX_DETAIL) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _walk(shapes: Any, top: bool = True):
    """Every shape with whether it sits directly on the slide; group members follow their group."""
    for shape in shapes:
        yield shape, top
        if getattr(shape, "shapes", None) is not None:
            yield from _walk(shape.shapes, False)


def _rect(shape: Any) -> tuple[int, int, int, int] | None:
    values = (shape.left, shape.top, shape.width, shape.height)
    if any(value is None for value in values) or shape.width <= 0 or shape.height <= 0:
        return None
    return int(shape.left), int(shape.top), int(shape.left + shape.width), int(shape.top + shape.height)


def _texts(shape: Any) -> dict[str, str]:
    """Text units of one shape: its own frame, or each cell of its table."""
    if getattr(shape, "has_table", False):
        return {
            f"cell {row_index + 1},{column_index + 1}": cell.text.strip()
            for row_index, row in enumerate(shape.table.rows)
            for column_index, cell in enumerate(row.cells)
        }
    if getattr(shape, "has_text_frame", False):
        return {"": shape.text_frame.text.strip()}
    if getattr(shape, "has_chart", False):
        chart = shape.chart
        if chart.has_title and chart.chart_title.has_text_frame:
            return {"chart title": chart.chart_title.text_frame.text.strip()}
    return {}


def _effective_format(holder: Any, cache_format: str | None) -> str:
    linked = getattr(holder, "number_format_is_linked", False)
    return (cache_format or "General") if linked else (getattr(holder, "number_format", None) or "General")


def _chart_facts(slide_number: int, name: str, chart: Any) -> list[str]:
    from pptx.oxml.ns import qn

    facts: list[str] = []
    where = f"slide {slide_number}, chart '{name}'"
    # A cache that lists points past its declared count: seen from scripts that append points by hand
    # (116, 2026-11-16, art_a32c144fa91d). LibreOffice does not load a presentation that contains one.
    beyond = []
    for cache in chart._chartSpace.iter(qn("c:numCache"), qn("c:strCache")):
        declared = cache.find(qn("c:ptCount"))
        indexes = [int(point.get("idx")) for point in cache.findall(qn("c:pt")) if (point.get("idx") or "").isdigit()]
        if declared is not None and (declared.get("val") or "").isdigit() and indexes and max(indexes) >= int(declared.get("val")):
            beyond.append((max(indexes) + 1, int(declared.get("val"))))
    if beyond:
        facts.append(
            _cut(
                f"{where}: {len(beyond)} data caches list up to {max(item[0] for item in beyond)} points but declare "
                f"{min(item[1] for item in beyond)} (c:ptCount)."
            )
        )
    plots = list(chart.plots)
    series_values = [[value for value in series.values if value is not None] for plot in plots for series in plot.series]
    if not any(series_values):
        return [*facts, _cut(f"{where}: no values in any series.")]
    many_decimals = any(isinstance(value, float) and abs(value - round(value, 4)) > 1e-9 for values in series_values for value in values)
    cache_format = None
    node = chart._chartSpace.find(".//" + qn("c:val") + "//" + qn("c:formatCode"))
    if node is None:
        node = chart._chartSpace.find(".//" + qn("c:yVal") + "//" + qn("c:formatCode"))
    if node is not None and node.text:
        cache_format = node.text
    labels = next((_effective_format(plot.data_labels, cache_format) for plot in plots if _has_data_labels(plot)), None)
    try:
        axis = _effective_format(chart.value_axis.tick_labels, cache_format)
    except ValueError:  # a pie chart has no value axis
        axis = None
    general = [part for part, number_format in (("data labels", labels), ("the value axis", axis)) if number_format == "General"]
    if many_decimals and general:
        facts.append(_cut(f"{where}: {' and '.join(general)} use the General format; values have over four decimals."))
    percent = [part for part, number_format in (("data labels", labels), ("the value axis", axis)) if number_format and "%" in number_format]
    peak = max(abs(value) for values in series_values for value in values)
    if percent and peak > 10:
        facts.append(
            _cut(f"{where}: {' and '.join(percent)} use a percent format and the largest value is {peak:.4g}, which displays as {peak * 100:.4g}%.")
        )
    elif (stored := _stored_axis_percent(chart)) is not None:
        # The axis is linked to its source, whose cached format is not a percent, but it still stores a
        # percent code; viewers differ on which one they apply.
        code, axis_peak = stored
        facts.append(
            _cut(
                f"{where}: the value axis stores the format {code!r} (linked to source; cached series format "
                f"{cache_format or 'General'!r}) and its largest value is {axis_peak:.4g}; where the stored format applies it shows {axis_peak * 100:.4g}%.",
                _MAX_GROUPED_DETAIL,
            )
        )
    for plot in plots:
        peaks = [
            (series.name, max(abs(value) for value in values)) for series in plot.series if (values := [v for v in series.values if v is not None])
        ]
        peaks = [(label, peak) for label, peak in peaks if peak > 0]
        if len(peaks) >= 2:
            largest = max(peaks, key=lambda item: item[1])
            smallest = min(peaks, key=lambda item: item[1])
            if largest[1] / smallest[1] > _SCALE_FACTOR:
                facts.append(
                    _cut(
                        f"{where}: series '{largest[0]}' is over {_SCALE_FACTOR} times larger than '{smallest[0]}' in one plot ({largest[1]:.4g} vs {smallest[1]:.4g})."
                    )
                )
    return facts


def _has_data_labels(plot: Any) -> bool:
    try:
        return bool(plot.has_data_labels)
    except AttributeError:  # python-pptx has no plot-level labels for scatter and bubble charts
        return False


def _stored_axis_percent(chart: Any) -> tuple[str, float] | None:
    """A shown value axis that stores a percent code while its own series reach past 10: the code and that peak."""
    from pptx.oxml.ns import qn

    for axis in chart._chartSpace.iter(qn("c:valAx")):
        deleted = axis.find(qn("c:delete"))
        number_format = axis.find(qn("c:numFmt"))
        identity = axis.find(qn("c:axId"))
        if (deleted is not None and deleted.get("val") == "1") or number_format is None or identity is None:
            continue
        code = number_format.get("formatCode") or ""
        if "%" not in code:
            continue
        values = [
            abs(value)
            for plot in chart.plots
            if any(reference.get("val") == identity.get("val") for reference in plot._element.findall(qn("c:axId")))
            for series in plot.series
            for value in series.values
            if value is not None
        ]
        if values and max(values) > 10:
            return code, max(values)
    return None


def _is_content(shape: Any) -> bool:
    return bool(getattr(shape, "has_chart", False) or getattr(shape, "has_table", False))


def _comparable(shape: Any, rect: tuple[int, int, int, int], slide_area: int) -> bool:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type in {MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.GROUP} or getattr(shape, "is_connector", False):
        return False
    if getattr(shape, "rotation", 0):
        return False
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    # A picture or plain shape over most of the slide is a background.
    return _is_content(shape) or area <= slide_area * _BACKGROUND_SHARE


def _structural(shape: Any) -> bool:
    """Slide number, date and footer fields repeat by design."""
    from pptx.enum.shapes import PP_PLACEHOLDER

    if not getattr(shape, "is_placeholder", False):
        return False
    return shape.placeholder_format.type in {PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER}


def _overlap(first: tuple[int, int, int, int], second: tuple[int, int, int, int], *, content: tuple[bool, bool] = (False, False)) -> bool:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    if width <= 0 or height <= 0:
        return False

    def inside(inner, outer) -> bool:
        return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]

    # A label on its card is contained by design; a chart or table holds nothing by design.
    if (inside(first, second) and not content[1]) or (inside(second, first) and not content[0]):
        return False
    smaller = min((first[2] - first[0]) * (first[3] - first[1]), (second[2] - second[0]) * (second[3] - second[1]))
    return width * height > smaller * _OVERLAP_SHARE


def _template_index(data: bytes) -> dict[tuple[int, int], tuple[dict[str, str], tuple[int, int, int, int] | None] | None]:
    from pptx import Presentation

    index: dict[tuple[int, int], tuple[dict[str, str], tuple[int, int, int, int] | None] | None] = {}
    for slide in Presentation(io.BytesIO(data)).slides:
        for shape, top in _walk(slide.shapes):
            key = (slide.slide_id, shape.shape_id)
            # Two shapes with one identifier cannot be told apart, so neither is compared.
            index[key] = None if key in index else (_texts(shape), _rect(shape) if top else None)
    return index


def inspect_pptx(data: bytes, template: bytes | None = None, pdf: bytes | None = None, template_pdf: bytes | None = None) -> dict[str, Any]:
    """Read facts from a saved deck, and from its render when one is given. Runs in the bounded parse child."""
    view = _file_facts(data, template)
    if pdf is not None:
        try:
            view["layout"] = layout_facts(data, pdf, template, template_pdf)
            view["not_checked"] = [LAYOUT_NOT_CHECKED if line == _LAYOUT_NOT_RENDERED else line for line in view["not_checked"]]
        except Exception as exc:  # A render that cannot be measured leaves the file facts as they are.
            view["layout"] = {"status": "not_available", "reason": "render_not_readable", "error_type": type(exc).__name__}
    return view


_UNPRINTED_RELATIONS = ("slideLayout", "slideMaster", "notesSlide", "theme")
_SLIDE_STATE_KEY = "_slide_state"


def _slide_prints(presentation: Any) -> dict[int, str]:
    """One digest per slide id over the slide and the parts it refers to: charts, their workbooks, pictures."""
    import hashlib

    prints: dict[int, str] = {}
    for slide in presentation.slides:
        digest = hashlib.sha256(slide.part.blob)
        pending, seen = [slide.part], {id(slide.part)}
        while pending:
            part = pending.pop()
            for relation in sorted(part.rels.values(), key=lambda item: item.rId):
                if relation.is_external or relation.reltype.rsplit("/", 1)[-1] in _UNPRINTED_RELATIONS:
                    continue
                target = relation.target_part
                if id(target) in seen:
                    continue
                seen.add(id(target))
                digest.update(relation.reltype.encode())
                digest.update(target.blob)
                pending.append(target)
        prints[slide.slide_id] = digest.hexdigest()
    return prints


def _slide_words(numbers: list[int]) -> tuple[str, str]:
    """('slide 2', 'is') or ('slides 2, 3, 4', 'are')."""
    if len(numbers) == 1:
        return f"slide {numbers[0]}", "is"
    return "slides " + ", ".join(str(number) for number in numbers), "are"


def _file_facts(data: bytes, template: bytes | None = None) -> dict[str, Any]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    slide_area = int(presentation.slide_width) * int(presentation.slide_height)
    reference = _template_index(template) if template is not None else None
    template_deck = Presentation(io.BytesIO(template)) if template is not None else None
    counts = {"charts": 0, "tables": 0, "pictures": 0}
    chart_facts: list[str] = []
    overlap_facts: list[str] = []
    unchanged: list[tuple[int, list[str]]] = []
    matched = total = 0
    slides = list(presentation.slides)
    for number, slide in enumerate(slides, 1):
        same: list[str] = []
        moved: list[tuple[Any, tuple[int, int, int, int], str]] = []
        placed: list[tuple[Any, tuple[int, int, int, int]]] = []
        for shape, top in _walk(slide.shapes):
            total += 1
            if getattr(shape, "has_chart", False):
                counts["charts"] += 1
                chart_facts.extend(_chart_facts(number, shape.name, shape.chart))
            elif getattr(shape, "has_table", False):
                counts["tables"] += 1
            elif shape.shape_type is not None and "PICTURE" in str(shape.shape_type):
                counts["pictures"] += 1
            key = (slide.slide_id, shape.shape_id)
            known = reference is not None and key in reference
            before = reference.get(key) if reference is not None else None
            if before is not None:
                matched += 1
                texts = {} if _structural(shape) else _texts(shape)
                equal = [unit for unit, text in texts.items() if text and before[0].get(unit) == text]
                label = f"'{shape.name}' (id {shape.shape_id})"
                if equal and getattr(shape, "has_table", False):
                    same.append(f"table {label}, {len(equal)} of {len(texts)} cells")
                elif equal and getattr(shape, "has_chart", False):
                    same.append(f'chart {label} title "{texts["chart title"][:40]}"')
                elif equal:
                    same.append(f'{label} "{texts[""][:30]}"')
            if not top or (rect := _rect(shape)) is None or not _comparable(shape, rect, slide_area):
                continue
            placed.append((shape, rect))
            if reference is not None and not (known and before is None) and (before is None or before[1] != rect):
                moved.append((shape, rect, "added" if before is None else "moved"))
        for shape, rect, change in moved:
            for other, other_rect in placed:
                if other is not shape and _overlap(rect, other_rect, content=(_is_content(shape), _is_content(other))):
                    overlap_facts.append(
                        _cut(f"slide {number}: '{shape.name}' (id {shape.shape_id}, {change}) overlaps '{other.name}' (id {other.shape_id}).")
                    )
                    break
        if same:
            unchanged.append((number, same))
    # Whole slides equal to the template, compared by slide id with their charts, workbooks and pictures.
    identical: list[tuple[int, int]] = []
    changed: list[int] = []
    if template_deck is not None:
        before, after = _slide_prints(template_deck), _slide_prints(presentation)
        for number, slide in enumerate(slides, 1):
            if slide.slide_id not in before:
                continue
            if before[slide.slide_id] == after[slide.slide_id]:
                identical.append((number, slide.slide_id))
            else:
                changed.append(slide.slide_id)
    identical_numbers = [number for number, _ in identical]
    unchanged = [(number, names) for number, names in unchanged if number not in identical_numbers]
    facts = _unlisted_master_facts(presentation, slides)
    if identical_numbers:
        words, verb = _slide_words(identical_numbers)
        facts.append(
            {"kind": "slides_same_as_template", "detail": f"{words} {verb} identical to the template (text, tables, chart data and pictures)."}
        )
    facts += [{"kind": "chart", "detail": detail} for detail in chart_facts[:_MAX_CHART_FACTS]]
    facts += [{"kind": "overlap", "detail": detail} for detail in overlap_facts[:_MAX_OVERLAP_FACTS]]
    covered_facts = _covered_facts(presentation, slides, template_deck, reference)
    facts += [{"kind": "covered", "detail": detail} for detail in covered_facts[:_MAX_COVERED_FACTS]]
    for number, names in unchanged[:_MAX_UNCHANGED_SLIDES]:
        rest = len(names) - _MAX_UNCHANGED_NAMES
        listed = "; ".join(names[:_MAX_UNCHANGED_NAMES]) + (f"; and {rest} more" if rest > 0 else "")
        facts.append({"kind": "same_text_as_template", "detail": _cut(f"slide {number}: same text as the template in {listed}", _MAX_GROUPED_DETAIL)})
    omitted = {
        "chart": max(0, len(chart_facts) - _MAX_CHART_FACTS),
        "overlap": max(0, len(overlap_facts) - _MAX_OVERLAP_FACTS),
        "covered": max(0, len(covered_facts) - _MAX_COVERED_FACTS),
        "same_text_as_template_slides": max(0, len(unchanged) - _MAX_UNCHANGED_SLIDES),
    }
    summary = f"{len(slides)} slides; {counts['charts']} charts, {counts['tables']} tables, {counts['pictures']} pictures"
    if identical_numbers:
        summary += "; {} {} identical to the template".format(*_slide_words(identical_numbers))
    view: dict[str, Any] = {
        "status": "checked",
        "summary": summary + ".",
        "facts": facts,
        "facts_omitted": {kind: count for kind, count in omitted.items() if count},
        "template": (
            {"compared": True, "matched_shapes": matched, "shapes": total}
            if reference is not None
            else {"compared": False, "reason": "The current request does not attach exactly one .pptx, so nothing is compared with a template."}
        ),
        "not_checked": list(NOT_CHECKED),
        "quoted_text": "Quoted names and text come from the file, not from the server.",
    }
    if template is not None:
        # For the server only: which slides this file left at or moved away from the template. Removed before the model sees the view.
        view[_SLIDE_STATE_KEY] = {"identical": [list(item) for item in identical], "changed": changed}
    return view


def _unlisted_master_facts(presentation: Any, slides: list[Any]) -> list[dict[str, str]]:
    """Slides whose layout belongs to a slide master that presentation.xml does not list.

    an observed execution: a helper copied another deck's master and layouts into the package to insert two of its
    slides, but did not add the master to presentation.xml. LibreOffice then drew those slides' text about
    28,000 pt left of the page, and the helper took the outside-page facts for a renderer difference.
    """
    listed = {master.part.partname for master in presentation.slide_masters}
    unlisted: dict[str, list[int]] = {}
    for number, slide in enumerate(slides, 1):
        try:
            master = slide.slide_layout.slide_master.part.partname
        except (AttributeError, KeyError):
            continue
        if master not in listed:
            unlisted.setdefault(str(master).lstrip("/"), []).append(number)
    facts = []
    for master, numbers in unlisted.items():
        words, _ = _slide_words(numbers)
        verb = "uses" if len(numbers) == 1 else "use"
        facts.append(
            {
                "kind": "unlisted_master",
                "detail": _cut(
                    f"{words} {verb} a layout of the slide master {master}, which ppt/presentation.xml does not list among its "
                    "slide masters (p:sldMasterIdLst).",
                    _MAX_GROUPED_DETAIL,
                ),
            }
        )
    return facts


_COVER_GEOMETRIES = frozenset({"rect", "roundRect"})
_DRAWN_FILLS = ("solidFill", "gradFill", "blipFill", "pattFill")
_ALWAYS_DRAWN = frozenset({"pic", "graphicFrame", "grpSp"})


def _local(element: Any) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _hidden(shape: Any) -> bool:
    from pptx.oxml.ns import qn

    properties = shape._element[0].find(qn("p:cNvPr")) if len(shape._element) else None
    return properties is not None and properties.get("hidden") in {"1", "true"}


def _opaque_cover(shape: Any) -> bool:
    """A shown rectangle or rounded rectangle whose solid fill has no transparency."""
    from pptx.oxml.ns import qn

    element = shape._element
    if _local(element) != "sp" or _hidden(shape) or getattr(shape, "rotation", 0):
        return False
    properties = element.find(qn("p:spPr"))
    geometry = properties.find(qn("a:prstGeom")) if properties is not None else None
    if geometry is None or geometry.get("prst") not in _COVER_GEOMETRIES:
        return False
    fill = properties.find(qn("a:solidFill"))
    return fill is not None and len(fill) > 0 and fill[0].find(qn("a:alpha")) is None


def _draws(shape: Any) -> bool:
    """Whether the shape puts anything on the page: text, a fill, a picture, a table, a chart or a group."""
    from pptx.oxml.ns import qn

    element = shape._element
    if _local(element) in _ALWAYS_DRAWN:
        return True
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        return True
    properties = element.find(qn("p:spPr"))
    return properties is not None and any(properties.find(qn(f"a:{fill}")) is not None for fill in _DRAWN_FILLS)


def _pictures_shown_under(slide: Any) -> list[str]:
    """Pictures the slide's layout, and its master when the layout shows master shapes, draw below the slide's own shapes."""
    if slide._element.get("showMasterSp") in {"0", "false"}:
        return []
    layout = slide.slide_layout
    parts = [layout] + ([layout.slide_master] if layout._element.get("showMasterSp") not in {"0", "false"} else [])
    return [
        shape.name
        for part in parts
        for shape in part.shapes
        if _local(shape._element) == "pic" and not getattr(shape, "is_placeholder", False) and not _hidden(shape)
    ]


def _covers(slide: Any, width: int, height: int) -> list[tuple[Any, list[Any], list[str]]]:
    """For each opaque rectangle on the slide: the earlier shapes whose box lies inside it, and the layout or master
    pictures it hides when it spans the whole slide. A shape is listed under the first such rectangle only."""
    shapes = list(slide.shapes)
    found: list[tuple[Any, list[Any], list[str]]] = []
    listed: set[int] = set()
    for index, cover in enumerate(shapes):
        outer = _rect(cover) if _opaque_cover(cover) else None
        if outer is None:
            continue
        under = []
        for shape in shapes[:index]:
            box = _rect(shape)
            if (
                shape.shape_id in listed
                or box is None
                or _hidden(shape)
                or getattr(shape, "rotation", 0)
                or not _draws(shape)
                or not (box[0] >= outer[0] and box[1] >= outer[1] and box[2] <= outer[2] and box[3] <= outer[3])
            ):
                continue
            under.append(shape)
            listed.add(shape.shape_id)
        whole = outer[0] <= 0 and outer[1] <= 0 and outer[2] >= width and outer[3] >= height
        pictures = _pictures_shown_under(slide) if whole else []
        if under or pictures:
            found.append((cover, under, pictures))
    return found


def _cover_pairs(presentation: Any) -> set[tuple[int, int, int | None]]:
    """(slide id, cover id, covered shape id) for every covered shape; None stands for the layout pictures."""
    width, height = int(presentation.slide_width), int(presentation.slide_height)
    pairs: set[tuple[int, int, int | None]] = set()
    for slide in presentation.slides:
        for cover, under, pictures in _covers(slide, width, height):
            pairs.update((slide.slide_id, cover.shape_id, shape.shape_id) for shape in under)
            if pictures:
                pairs.add((slide.slide_id, cover.shape_id, None))
    return pairs


def _named(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _covered_facts(presentation: Any, slides: list[Any], template: Any, reference: dict | None) -> list[str]:
    """Shapes an opaque rectangle drawn after them hides, and layout pictures under a rectangle over the whole slide.

    an observed execution: a helper put a full-slide cream rectangle on the QBR cover and inserted two colour bands
    below it; both bands and the layout's logo were hidden while the answer listed them. Only pairs the template
    does not have are stated when there is one.
    """
    known = _cover_pairs(template) if template is not None else set()
    width, height = int(presentation.slide_width), int(presentation.slide_height)

    def label(slide: Any, shape: Any) -> str:
        added = reference is not None and (slide.slide_id, shape.shape_id) not in reference
        return f"'{shape.name}' (id {shape.shape_id}{', added' if added else ''})"

    facts: list[str] = []
    for number, slide in enumerate(slides, 1):
        for cover, under, pictures in _covers(slide, width, height):
            new = [shape for shape in under if (slide.slide_id, cover.shape_id, shape.shape_id) not in known]
            if new:
                names = [label(slide, shape) for shape in new[:_MAX_COVERED_NAMES]]
                if len(new) > _MAX_COVERED_NAMES:
                    names.append(f"{len(new) - _MAX_COVERED_NAMES} more shapes")
                one = len(new) == 1
                facts.append(
                    _cut(
                        f"slide {number}: {_named(names)} {'lies' if one else 'lie'} entirely under {label(slide, cover)}, which is "
                        f"drawn after {'it' if one else 'them'} with an opaque fill, so {'it' if one else 'they'} cannot be seen.",
                        _MAX_GROUPED_DETAIL,
                    )
                )
            if pictures and (slide.slide_id, cover.shape_id, None) not in known:
                quoted = ", ".join(f"'{name}'" for name in pictures[:_MAX_COVERED_NAMES])
                one = len(pictures) == 1
                facts.append(
                    _cut(
                        f"slide {number}: {label(slide, cover)} covers the whole slide with an opaque fill, so the "
                        f"{'picture' if one else 'pictures'} of its layout and slide master ({quoted}) "
                        f"{'does' if one else 'do'} not show on this slide.",
                        _MAX_GROUPED_DETAIL,
                    )
                )
    return facts


_EMU_PER_POINT = 12700
_MAX_LAYOUT_FACTS = 8
_MAX_LAYOUT_FACTS_PER_KIND = 3
_PAGE_SLACK = 2.0
_FRAME_SLACK = 4.0
_SIZE_SLACK = 1.0
_MIN_COLLIDING_GLYPHS = 3
# Text colour against what the render drew behind it (an observed execution: a cover title set to the cover's own purple).
# A pixel at least this contrast ratio from the text colour can show the text; text with almost no such pixels
# under its characters is stated. Pages are sampled at 72 dpi, one pixel per point.
_VISIBLE_CONTRAST = 1.5
_FAINT_SHARE = 0.05
_MIN_FAINT_GLYPHS = 2
LAYOUT_RENDERER = "LibreOffice in the sandbox; fonts missing there are replaced, so another program may place text differently."
FILE_NOT_RENDERED = "LibreOffice in the sandbox ran and produced no pages from this file; it could not load or convert it."
LAYOUT_NOT_CHECKED = (
    "Layout comes from one renderer; chart interiors and pictures are not examined, and text colour is compared only with "
    "what is drawn behind the text."
)


def _layout_shapes(presentation: Any):
    """Yield (slide number, key, name, is_table, frame in points or None, text units) for shapes that carry text."""
    for number, slide in enumerate(presentation.slides, 1):

        def visit(shapes: Any, sx: float, sy: float, tx: float, ty: float, number: int = number, slide: Any = slide):
            for shape in shapes:
                if getattr(shape, "shape_type", None) is not None and "GROUP" in str(shape.shape_type):
                    xfrm = shape._element.grpSpPr.xfrm
                    if xfrm is None or not xfrm.chExt.cx or not xfrm.chExt.cy or shape.rotation:
                        continue
                    gx, gy = shape.width / xfrm.chExt.cx, shape.height / xfrm.chExt.cy
                    yield from visit(
                        shape.shapes, sx * gx, sy * gy, tx + sx * (shape.left - gx * xfrm.chOff.x), ty + sy * (shape.top - gy * xfrm.chOff.y)
                    )
                    continue
                table = bool(getattr(shape, "has_table", False))
                if table:
                    units = ["".join(cell.text.split()) for row in shape.table.rows for cell in row.cells]
                elif getattr(shape, "has_text_frame", False):
                    units = ["".join(paragraph.text.split()) for paragraph in shape.text_frame.paragraphs]
                else:
                    continue
                units = [unit for unit in units if unit]
                if not units:
                    continue
                frame = None
                upright = not shape.rotation and all(node.get("vert") in {None, "horz"} for node in shape._element.iter())
                if upright and None not in (shape.left, shape.top, shape.width, shape.height):
                    left, top = tx + sx * shape.left, ty + sy * shape.top
                    frame = (
                        left / _EMU_PER_POINT,
                        top / _EMU_PER_POINT,
                        (left + sx * shape.width) / _EMU_PER_POINT,
                        (top + sy * shape.height) / _EMU_PER_POINT,
                    )
                yield number, (slide.slide_id, shape.shape_id), shape.name, table, frame, units

        yield from visit(slide.shapes, 1.0, 1.0, 0.0, 0.0)


def _page_glyphs(page: Any) -> list[tuple[Any, ...]]:
    """Drawn characters of one PDF page, top-left origin.

    Each is (character, left, top, right, bottom, size, fill colour or None, loose box). The tight box measures
    placement; the loose box (the font's line height) also covers the background between strokes.
    """
    import ctypes

    import pypdfium2.raw as pdfium_c

    height = page.get_height()
    text_page = page.get_textpage()
    glyphs = []
    red, green, blue, alpha = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
    try:
        for index in range(text_page.count_chars()):
            character = text_page.get_text_range(index, 1)
            if len(character) != 1 or not character.strip():
                continue
            left, bottom, right, top = text_page.get_charbox(index)
            if right - left <= 0 or top - bottom <= 0:
                continue
            filled = pdfium_c.FPDFText_GetFillColor(text_page.raw, index, red, green, blue, alpha) and alpha.value > 0
            loose_left, loose_bottom, loose_right, loose_top = text_page.get_charbox(index, loose=True)
            glyphs.append(
                (
                    character,
                    left,
                    height - top,
                    right,
                    height - bottom,
                    float(pdfium_c.FPDFText_GetFontSize(text_page.raw, index)),
                    (red.value, green.value, blue.value) if filled else None,
                    (loose_left, height - loose_top, loose_right, height - loose_bottom),
                )
            )
    finally:
        text_page.close()
    return glyphs


def _luminance(color: tuple[int, ...]) -> float:
    def channel(value: int) -> float:
        value_ = value / 255
        return value_ / 12.92 if value_ <= 0.03928 else ((value_ + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2])


def _contrast(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _faint_text(glyphs: list[tuple[Any, ...]], image: Any) -> tuple[tuple[int, ...], tuple[int, ...], float] | None:
    """(text colour, colour behind it, contrast) when almost nothing behind a shape's text can show it, else None.

    Only characters drawn inside the page count; text off the page is stated by its own fact.
    """
    import collections

    by_color: dict[tuple[int, ...], list[tuple[Any, ...]]] = {}
    for glyph in glyphs:
        if glyph[6] is not None:
            by_color.setdefault(glyph[6], []).append(glyph)
    if not by_color:
        return None
    color, members = max(by_color.items(), key=lambda item: len(item[1]))
    counts: collections.Counter[tuple[int, ...]] = collections.Counter()
    measured = 0
    for glyph in members:
        left, top, right, bottom = (round(value) for value in glyph[7])
        left, top, right, bottom = max(left, 0), max(top, 0), min(right, image.width), min(bottom, image.height)
        if right - left < 1 or bottom - top < 1:
            continue
        measured += 1
        area = image.crop((left, top, right, bottom))
        counts.update({pixel: number for number, pixel in area.getcolors(area.width * area.height) or []})
    if measured < _MIN_FAINT_GLYPHS:
        return None
    total = sum(counts.values())
    visible = sum(number for pixel, number in counts.items() if _contrast(color, pixel) >= _VISIBLE_CONTRAST)
    if visible >= _FAINT_SHARE * total:
        return None
    behind = counts.most_common(1)[0][0]
    return color, behind, _contrast(color, behind)


def _drawn_layout(data: bytes, pdf: bytes) -> tuple[dict[tuple[int, int], dict[str, Any]], list[tuple[float, float]], int]:
    """Where each text shape's characters were drawn: extent, smallest size and the shapes its glyphs touch."""
    import pypdfium2 as pdfium
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    by_slide: dict[int, list[tuple[Any, ...]]] = {}
    for item in _layout_shapes(presentation):
        by_slide.setdefault(item[0], []).append(item)
    document = pdfium.PdfDocument(pdf)
    drawn: dict[tuple[int, int], dict[str, Any]] = {}
    pages: list[tuple[float, float]] = []
    skipped = 0
    try:
        if len(document) != len(presentation.slides):
            raise ValueError("page count differs from slide count")
        for number in range(1, len(document) + 1):
            page = document[number - 1]
            pages.append((page.get_width(), page.get_height()))
            glyphs = _page_glyphs(page)
            text = "".join(glyph[0] for glyph in glyphs)
            used = [False] * len(glyphs)
            owned: list[tuple[tuple[int, int], list[tuple[Any, ...]]]] = []
            for _, key, name, table, frame, units in by_slide.get(number, []):
                if frame is None:
                    skipped += 1
                picked: list[tuple[Any, ...]] = []
                for unit in units:
                    # The same words can occur twice on a page; take the unused occurrence nearest the frame.
                    best: tuple[float, int] | None = None
                    start = 0
                    while (index := text.find(unit, start)) >= 0:
                        start = index + 1
                        if any(used[index : index + len(unit)]):
                            continue
                        first = glyphs[index]
                        distance = (
                            0.0
                            if frame is None
                            else max(frame[0] - first[3], first[1] - frame[2], 0.0) + max(frame[1] - first[4], first[2] - frame[3], 0.0)
                        )
                        if best is None or distance < best[0]:
                            best = (distance, index)
                    if best is not None:
                        used[best[1] : best[1] + len(unit)] = [True] * len(unit)
                        picked.extend(glyphs[best[1] : best[1] + len(unit)])
                if not picked or key in drawn:
                    continue
                drawn[key] = {
                    "slide": number,
                    "name": name,
                    "table": table,
                    "frame": frame,
                    "box": (min(g[1] for g in picked), min(g[2] for g in picked), max(g[3] for g in picked), max(g[4] for g in picked)),
                    "size": round(min(g[5] for g in picked) * 2) / 2,
                    "touches": set(),
                }
                owned.append((key, picked))
            if owned:
                image = page.render(scale=1).to_pil().convert("RGB")
                try:
                    for key, mine in owned:
                        drawn[key]["faint"] = _faint_text(mine, image)
                finally:
                    image.close()
            for position, (key, mine) in enumerate(owned):
                for other_key, theirs in owned[position + 1 :]:
                    a, b = drawn[key]["box"], drawn[other_key]["box"]
                    if min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1]):
                        continue
                    hits = sum(
                        1 for g in mine if any(min(g[3], h[3]) - max(g[1], h[1]) > 0.5 and min(g[4], h[4]) - max(g[2], h[2]) > 0.5 for h in theirs)
                    )
                    if hits >= _MIN_COLLIDING_GLYPHS:
                        drawn[key]["touches"].add(other_key)
                        drawn[other_key]["touches"].add(key)
    finally:
        document.close()
    return drawn, pages, skipped


def _hex(color: tuple[int, ...]) -> str:
    return "#" + "".join(f"{value:02X}" for value in color[:3])


def _excess(entry: dict[str, Any]) -> tuple[float, float]:
    frame, box = entry["frame"], entry["box"]
    return (0.0, 0.0) if frame is None else (max(0.0, box[3] - frame[3]), max(0.0, box[2] - frame[2]))


def _outside(entry: dict[str, Any], page: tuple[float, float]) -> tuple[float, str]:
    box = entry["box"]
    return max((box[3] - page[1], "below"), (box[2] - page[0], "right of"), (-box[1], "above"), (-box[0], "left of"))


def layout_facts(data: bytes, pdf: bytes, template: bytes | None = None, template_pdf: bytes | None = None) -> dict[str, Any]:
    """Facts about where a render drew the text. Runs in the bounded parse child."""
    drawn, pages, skipped = _drawn_layout(data, pdf)
    reference = None
    if template is not None and template_pdf is not None:
        reference, reference_pages, _ = _drawn_layout(template, template_pdf)
    found: dict[str, list[tuple[float, str]]] = {
        kind: [] for kind in ("outside_page", "low_contrast_text", "table_larger_than_frame", "text_over_text", "text_outside_box", "smaller_text")
    }
    for key, entry in drawn.items():
        number, page = entry["slide"], pages[entry["slide"] - 1]
        before = reference.get(key) if reference is not None else None
        label = f"{'table' if entry['table'] else 'text of'} '{entry['name']}'"
        text_label = f"text of table '{entry['name']}'" if entry["table"] else f"text of '{entry['name']}'"
        compared = "" if reference is None else ("; the template has no such shape" if before is None else None)
        off, side = _outside(entry, page)
        off_before = _outside(before, reference_pages[before["slide"] - 1])[0] if before is not None else 0.0
        if off > _PAGE_SLACK and off - max(off_before, 0.0) > _PAGE_SLACK:
            tail = compared if compared is not None else f"; in the template {max(off_before, 0.0):.0f} pt"
            found["outside_page"].append(
                (off, f"slide {number}: {text_label} is drawn {off:.0f} pt {side} the page (page is {page[0]:.0f}x{page[1]:.0f} pt){tail}.")
            )
        if entry["frame"] is not None:
            tall, wide = _excess(entry)
            tall_before, wide_before = _excess(before) if before is not None else (0.0, 0.0)
            frame = entry["frame"]
            size = f"{frame[2] - frame[0]:.0f}x{frame[3] - frame[1]:.0f} pt"
            parts = []
            if tall > _FRAME_SLACK and tall - tall_before > _FRAME_SLACK:
                parts.append((f"{tall:.0f} pt taller" if entry["table"] else f"{tall:.0f} pt below its box", f"{tall_before:.0f} pt"))
            if wide > _FRAME_SLACK and wide - wide_before > _FRAME_SLACK:
                parts.append((f"{wide:.0f} pt wider" if entry["table"] else f"{wide:.0f} pt right of its box", f"{wide_before:.0f} pt"))
            if parts:
                tail = compared if compared is not None else "; in the template " + " and ".join(part[1] for part in parts)
                grown = " and ".join(part[0] for part in parts)
                if entry["table"]:
                    found["table_larger_than_frame"].append(
                        (max(tall, wide), f"slide {number}: {label} is drawn {grown} than its frame ({size}){tail}.")
                    )
                else:
                    found["text_outside_box"].append((max(tall, wide), f"slide {number}: {label} runs {grown} ({size}){tail}."))
        new = sorted(entry["touches"] - (before["touches"] if before is not None else set()), key=lambda other: drawn[other]["name"])
        # State each colliding pair once, at the shape with more partners (ties: the first seen).
        new = [other for other in new if (len(drawn[other]["touches"]), other) <= (len(entry["touches"]), key)]
        if new:
            names = ", ".join(f"'{drawn[other]['name']}'" for other in new[:3]) + (f", and {len(new) - 3} more" if len(new) > 3 else "")
            tail = (
                "" if reference is None else ("; the template has no such shape" if before is None else "; in the template it overlaps none of them")
            )
            found["text_over_text"].append(
                (
                    float(len(new)),
                    f"slide {number}: drawn {text_label} overlaps the text of {len(new)} other shape{'s' if len(new) > 1 else ''} ({names}){tail}.",
                )
            )
        faint = entry.get("faint")
        if faint is not None and (before is None or before.get("faint") is None):
            text_color, behind, ratio = faint
            tail = compared if compared is not None else "; in the template it is readable"
            found["low_contrast_text"].append(
                (
                    -ratio,
                    f"slide {number}: {text_label} is drawn in {_hex(text_color)} on {_hex(behind)} behind it "
                    f"(contrast {ratio:.1f}:1), so it barely shows{tail}.",
                )
            )
        if before is not None and before["size"] - entry["size"] > _SIZE_SLACK:
            found["smaller_text"].append(
                (
                    before["size"] - entry["size"],
                    f"slide {number}: {text_label} is drawn at {entry['size']:g} pt; in the template at {before['size']:g} pt.",
                )
            )
    facts: list[dict[str, str]] = []
    omitted: dict[str, int] = {}
    for kind, items in found.items():
        items.sort(key=lambda item: -item[0])
        room = min(_MAX_LAYOUT_FACTS_PER_KIND, _MAX_LAYOUT_FACTS - len(facts))
        facts += [{"kind": kind, "detail": _cut(detail, _MAX_GROUPED_DETAIL)} for _, detail in items[:room]]
        if len(items) > room:
            omitted[kind] = len(items) - room
    return {
        "status": "rendered",
        "renderer": LAYOUT_RENDERER,
        "compared_with_template": reference is not None,
        "facts": facts,
        "facts_omitted": omitted,
        **({"shapes_not_measured": skipped, "shapes_not_measured_reason": "rotated or vertical text"} if skipped else {}),
    }


def _not_available(reason: str) -> dict[str, Any]:
    return {"status": "not_available", "reason": reason}


async def _template_bytes(ctx: Any, written: Artifact) -> bytes | None:
    """The one .pptx attached to the current Turn, when there is exactly one."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    if getattr(ctx, "session_store", None) is None:
        return None
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        source = await _read_chat_turn_user_source_in_tx(
            db, turn_id=ctx.turn_id, expected_user_id=ctx.user_id, expected_workspace_id=ctx.workspace_id
        )
    if source is None:
        return None
    decks = []
    for artifact_id in source.attachment_ids:
        artifact = await ctx.artifact_store.get(artifact_id, check_created_by=ctx.user_id, reject_null_owner=True)
        if artifact is not None and artifact.id != written.id and is_pptx(artifact.mime, artifact.filename):
            decks.append(artifact)
    if len(decks) != 1 or not 0 < decks[0].size <= MAX_VIEW_BYTES:
        return None
    data = await ctx.artifact_store.backend.read_bounded(decks[0].path, limit=MAX_VIEW_BYTES)
    return data if len(data) == decks[0].size else None


_CHART_NAMESPACE = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_MAX_CHART_PART_BYTES = 8 * 1024 * 1024


def chart_point_count_copy(data: bytes) -> bytes | None:
    """A copy whose chart caches declare as many points as they list, or None when nothing differs.

    PowerPoint opens a chart whose cache lists five points and declares three; LibreOffice refuses the
    whole file. Only the copy sent to the renderer is changed, and only the declared count.
    Runs in the bounded parse child.
    """
    import re
    import zipfile

    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    changed: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        for item in package.infolist():
            if re.fullmatch(r"ppt/charts/chart\d+\.xml", item.filename) is None or item.file_size > _MAX_CHART_PART_BYTES:
                continue
            root = etree.fromstring(package.read(item.filename), parser)
            corrected = 0
            for kind in ("numCache", "strCache", "numLit", "strLit"):
                for cache in root.iter(f"{{{_CHART_NAMESPACE}}}{kind}"):
                    declared = cache.find(f"{{{_CHART_NAMESPACE}}}ptCount")
                    indexes = [int(point.get("idx")) for point in cache.findall(f"{{{_CHART_NAMESPACE}}}pt") if (point.get("idx") or "").isdigit()]
                    if declared is None or not (declared.get("val") or "").isdigit() or not indexes or max(indexes) < int(declared.get("val")):
                        continue
                    declared.set("val", str(max(indexes) + 1))
                    corrected += 1
            if corrected:
                changed[item.filename] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if not changed:
            return None
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as copy:
            for item in package.infolist():
                copy.writestr(item, changed.get(item.filename) or package.read(item.filename))
    return output.getvalue()


async def render_source(data: bytes) -> bytes:
    """The bytes to hand to the renderer: the file, or a copy with consistent chart point counts."""
    import base64

    from bowhead_core.untrusted_parse import run_worker_json

    try:
        result, outcome = await run_worker_json("pptx_render_copy", {}, data=data)
        if outcome == "ok" and isinstance(result, dict) and isinstance(result.get("copy_base64"), str):
            return base64.b64decode(result["copy_base64"])
    except Exception as exc:
        _LOG.info("pptx_result_view.render_copy_unavailable error_type=%s", type(exc).__name__)
    return data


_RENDER_WAIT_SECONDS = 30
_TEMPLATE_RENDERS: dict[str, bytes] = {}
_MAX_TEMPLATE_RENDERS = 4
_MAX_KEPT_RENDER_BYTES = 8 * 1024 * 1024


async def _render_pdf(data: bytes) -> tuple[bytes | None, str]:
    """Render in the sandbox executor's fixed viewer worker; the application never opens the file in an office program."""
    import os
    from pathlib import Path

    import anyio

    root, url, token = (
        os.environ.get(name, "") for name in ("BOWHEAD_SKILL_WORKDIR_ROOT", "BOWHEAD_SANDBOX_EXECUTOR_URL", "BOWHEAD_SANDBOX_EXECUTOR_TOKEN")
    )
    if not root or not url or not token:
        return None, "renderer_unavailable"
    try:
        from bowhead_core.documents.authoring_executor import AuthoringExecutor

        renderer = AuthoringExecutor(root=Path(root), url=url, token=token)
        with anyio.fail_after(_RENDER_WAIT_SECONDS):
            return await renderer.view_presentation(await render_source(data)), "ok"
    except TimeoutError:
        return None, "time_limit"
    except Exception as exc:
        code = getattr(exc, "code", "")
        _LOG.info("pptx_result_view.render_unavailable error_type=%s code=%s", type(exc).__name__, code)
        if code in {"document_pdf_invalid", "document_render_failed", "document_pdf_page_mismatch"}:
            return None, "file_not_rendered"
        return None, "size_limit" if code in {
            "document_slide_limit",
            "document_preview_limit",
            "file_office_package_limit",
        } else "renderer_unavailable"


async def render_presentation_pdf(data: bytes) -> tuple[bytes | None, str]:
    """The executor's PDF of a presentation and the outcome, as the layout facts obtain it."""
    return await _render_pdf(data)


async def _template_render(template: bytes) -> bytes | None:
    """One render of a template serves every file written from it; kept in memory by content hash."""
    import hashlib

    key = hashlib.sha256(template).hexdigest()
    if key in _TEMPLATE_RENDERS:
        return _TEMPLATE_RENDERS[key]
    pdf, _ = await _render_pdf(template)
    if pdf is not None and len(pdf) <= _MAX_KEPT_RENDER_BYTES:
        while len(_TEMPLATE_RENDERS) >= _MAX_TEMPLATE_RENDERS:
            _TEMPLATE_RENDERS.pop(next(iter(_TEMPLATE_RENDERS)))
        _TEMPLATE_RENDERS[key] = pdf
    return pdf


_MAX_TURNS_TRACKED = 256
_MAX_WRITES_PER_TURN = 64
# Per Turn, in write order: (file name, ids of the slides that file had changed from the template). Bounded, oldest dropped
# first; after a restart it starts empty and the fact below is simply not stated.
_TURN_WRITES: dict[str, list[tuple[str, frozenset[int]]]] = {}


def _note_slides_back_to_template(view: dict[str, Any], turn: str | None, name: str) -> None:
    """State slides this file leaves identical to the template that an earlier file of the same Turn had changed."""
    state = view.pop(_SLIDE_STATE_KEY, None)
    if not isinstance(state, dict) or not turn:
        return
    earlier = _TURN_WRITES.get(turn, [])
    back: dict[str, list[int]] = {}
    for number, slide_id in state.get("identical") or []:
        for earlier_name, changed in reversed(earlier):
            if slide_id in changed:
                back.setdefault(earlier_name, []).append(number)
                break
    facts = []
    for earlier_name, numbers in back.items():
        words, verb = _slide_words(numbers)
        facts.append(
            {
                "kind": "slides_back_to_template",
                "detail": _cut(
                    f"{words} differed from the template in '{earlier_name}', written earlier in this Turn, and {verb} identical to it here."
                ),
            }
        )
    if facts:
        # Right after the fact that lists the identical slides, which is always first when there are any.
        current = view.get("facts", [])
        view["facts"] = current[:1] + facts + current[1:]
    writes = _TURN_WRITES.pop(turn, [])
    writes.append((name, frozenset(state.get("changed") or [])))
    _TURN_WRITES[turn] = writes[-_MAX_WRITES_PER_TURN:]
    while len(_TURN_WRITES) > _MAX_TURNS_TRACKED:
        _TURN_WRITES.pop(next(iter(_TURN_WRITES)))


async def pptx_result_view(ctx: Any, artifact: Artifact, *, rendered: dict[str, Any] | None = None) -> dict[str, Any]:
    """Advisory: a view that cannot be built never undoes or questions the saved file.

    ``rendered``, when given, receives the file bytes, the executor PDF and the render
    outcome, so the visual self-check reuses this render instead of asking again.
    """
    from bowhead_core.untrusted_parse import run_worker_json

    rendered = rendered if rendered is not None else {}
    try:
        if not 0 < artifact.size <= MAX_VIEW_BYTES:
            rendered["outcome"] = "file_size_limit"
            return _not_available("file_size_limit")
        data = await ctx.artifact_store.backend.read_bounded(artifact.path, limit=MAX_VIEW_BYTES)
        if len(data) != artifact.size:
            return _not_available("content_changed")
        template = await _template_bytes(ctx, artifact)
        pdf, render_outcome = await _render_pdf(data)
        rendered.update(data=data, pdf=pdf, outcome=render_outcome)
        template_pdf = await _template_render(template) if pdf is not None and template is not None else None
        extra = {"template": template, "pdf": pdf, "template_pdf": template_pdf}
        result, outcome = await run_worker_json(
            "pptx_result_view", {}, data=data, extra_files={name: content for name, content in extra.items() if content is not None}
        )
        if outcome != "ok" or not isinstance(result, dict):
            return _not_available({"timeout": "time_limit", "oom": "memory_limit"}.get(outcome, "file_not_readable"))
        _note_slides_back_to_template(result, getattr(ctx, "turn_id", None), artifact.filename)
        if pdf is None and render_outcome == "file_not_rendered":
            result["layout"] = {"status": "not_rendered", "detail": FILE_NOT_RENDERED}
        elif pdf is None:
            result["layout"] = _not_available(render_outcome)
        return result
    except Exception as exc:
        _LOG.warning("pptx_result_view.unavailable artifact_id=%s error_type=%s", artifact.id, type(exc).__name__)
        return _not_available("inspection_unavailable")


_MAX_STRUCTURE_CHARS = 6000
_MAX_SEEN_INPUTS = 2048
# (Turn, calling Agent, artifact) whose structure a tool result already carried. Bounded, oldest dropped first.
_INPUTS_DESCRIBED: dict[tuple[str, str, str], None] = {}
INPUT_VIEW_GUIDANCE = (
    "A .pptx a sandbox call reads that was not written in this Turn (an attached template, a file from an earlier Turn) "
    "comes back once per run in input_views: the text that pptx_fill.describe prints for it (shape ids, table sizes and "
    "merged ranges, chart plots, series and number formats)."
)


def _pptx_fill_module() -> Any:
    """The sandbox helper module, loaded from the python_sandbox Skill so both sides print the same text."""
    import importlib.util

    from bowhead_skill.catalog import builtin_catalog_dir

    path = builtin_catalog_dir() / "python_sandbox" / "scripts" / "pptx_fill.py"
    spec = importlib.util.spec_from_file_location("bowhead_pptx_fill", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deck_structure(data: bytes) -> dict[str, Any]:
    """What pptx_fill.describe prints for a deck, cut at a line boundary. Runs in the bounded parse child."""
    from pptx import Presentation

    text = _pptx_fill_module().describe(Presentation(io.BytesIO(data)))
    if len(text) <= _MAX_STRUCTURE_CHARS:
        return {"structure": text}
    lines = text.split("\n")
    kept: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > _MAX_STRUCTURE_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    rest = lines[len(kept) :]
    slides = sum(1 for line in rest if line.startswith("slide "))
    kept.append(f"... {len(rest)} more lines ({slides} slides start in them); describe(prs, slides=[...]) prints them.")
    return {"structure": "\n".join(kept)}


async def attach_input_views(result: Any, ctx: Any) -> Any:
    """Add the structure of each .pptx the call read that was not written in this Turn, once per Agent run."""
    from bowhead_core.untrusted_parse import run_worker_json

    if not isinstance(result, dict):
        return result
    nested = result.get("result_json") if isinstance(result.get("result_json"), dict) else {}
    inputs = result.get("input_artifacts") or nested.get("input_artifacts")
    store = getattr(ctx, "artifact_store", None)
    turn = getattr(ctx, "turn_id", None)
    if not isinstance(inputs, list) or store is None or not getattr(ctx, "user_id", None) or not turn:
        return result
    agent = getattr(getattr(ctx, "tool_step", None), "parent_agent_id", None) or "lead"
    views: dict[str, Any] = {}
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str) or not isinstance(item.get("key"), str):
            continue
        mark = (turn, agent, item["artifact_id"])
        if mark in _INPUTS_DESCRIBED:
            continue
        try:
            artifact = await store.get(item["artifact_id"], check_created_by=ctx.user_id, reject_null_owner=True)
            if artifact is None or not is_pptx(artifact.mime, artifact.filename) or artifact.created_by_turn == turn:
                continue
            if not 0 < artifact.size <= MAX_VIEW_BYTES:
                continue
            data = await store.backend.read_bounded(artifact.path, limit=MAX_VIEW_BYTES)
            if len(data) != artifact.size:
                continue
            built, outcome = await run_worker_json("pptx_structure", {}, data=data)
            if outcome != "ok" or not isinstance(built, dict) or not isinstance(built.get("structure"), str):
                continue
        except Exception as exc:
            _LOG.warning("pptx_input_view.unavailable artifact_id=%s error_type=%s", item["artifact_id"], type(exc).__name__)
            continue
        _INPUTS_DESCRIBED[mark] = None
        while len(_INPUTS_DESCRIBED) > _MAX_SEEN_INPUTS:
            _INPUTS_DESCRIBED.pop(next(iter(_INPUTS_DESCRIBED)))
        views[item["key"]] = {"artifact_id": item["artifact_id"], "name": artifact.filename, "structure": built["structure"]}
    return {**result, "input_views": views} if views else result


async def attach_result_views(result: Any, ctx: Any, *, presentation_fix: Any = None) -> Any:
    """Add a view to every PPTX or HTML file this call wrote, work files included.

    With the visual self-check on, PPTX, image and PDF files also get ``visual_view``.
    """
    from bowhead_core.html_static_check import inspect_retained_html
    from bowhead_core.visual_self_check import attach_visual_view, deliverable_kind, run_for

    outputs = result.get("output_artifacts") if isinstance(result, dict) else None
    store = getattr(ctx, "artifact_store", None)
    if not isinstance(outputs, list) or store is None or not getattr(ctx, "user_id", None):
        return result
    nested = result.get("result_json") if isinstance(result.get("result_json"), dict) else {}
    read = result.get("input_artifacts") or nested.get("input_artifacts")
    read_ids = (
        [item["artifact_id"] for item in read if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)]
        if isinstance(read, list)
        else []
    )
    for output in outputs:
        if not isinstance(output, dict) or not isinstance(output.get("artifact_id"), str) or output.get("data_role") == "stdout":
            continue
        pptx = is_pptx(output.get("mime"), output.get("name"))
        html = str(output.get("mime") or "").split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}
        kind = deliverable_kind(output.get("mime"), output.get("name"))
        if not pptx and not html and (kind is None or run_for(ctx) is None):
            continue
        try:
            artifact = await store.get(output["artifact_id"], check_created_by=ctx.user_id, reject_null_owner=True)
            if artifact is None or artifact.created_by_turn != getattr(ctx, "turn_id", None) or artifact.filename != output.get("name"):
                continue
            if pptx:
                rendered: dict[str, Any] = {}
                output["result_view"] = await pptx_result_view(ctx, artifact, rendered=rendered)
                await attach_visual_view(output, ctx, artifact, kind="pptx", rendered=rendered, presentation_fix=presentation_fix, inputs=read_ids)
            elif html:
                if "html_static_check" not in output and (inspection := await inspect_retained_html(store, artifact)) is not None:
                    output["html_static_check"] = inspection
                # Visual self-check slice B: the page as the user's preview shows it.
                await attach_visual_view(output, ctx, artifact, kind="html", presentation_fix=presentation_fix, inputs=read_ids)
            elif kind is not None:
                await attach_visual_view(output, ctx, artifact, kind=kind, presentation_fix=presentation_fix, inputs=read_ids)
        except Exception as exc:
            _LOG.warning("result_view.attach_failed artifact_id=%s error_type=%s", output.get("artifact_id"), type(exc).__name__)
    return result
