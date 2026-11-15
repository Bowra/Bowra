"""Facts about a presentation an Agent just wrote, returned with the tool result.

The model knows the code it ran, not what the file now is. This view states what
can be read from the saved file: charts without values, number formats, series of
very different size, text still equal to the template and added shapes that cover
others. It echoes no slide content, gives no instruction and no verdict.
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
_MAX_UNCHANGED_SLIDES = 6
_MAX_UNCHANGED_NAMES = 5
_SCALE_FACTOR = 100
_BACKGROUND_SHARE = 0.5
_OVERLAP_SHARE = 0.05
NOT_CHECKED = (
    "Text layout is not rendered, so text running outside its shape is not reported.",
    "Shapes inside groups, lines, rotated shapes, table heights and plots that share one axis are not compared.",
    "Data correctness is not checked.",
)
PPTX_RESULT_VIEW_GUIDANCE = (
    "A .pptx written by a sandbox call comes back with result_view: one summary line and facts read from the saved file "
    "(charts without values, General or percent number formats that do not fit the values, series of very different size, "
    "text and chart titles equal to the attached template, "
    "added shapes that cover others). It lists what was not checked and gives no verdict. The file's content is read by "
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
    plots = list(chart.plots)
    series_values = [[value for value in series.values if value is not None] for plot in plots for series in plot.series]
    if not any(series_values):
        return [_cut(f"{where}: no values in any series.")]
    many_decimals = any(isinstance(value, float) and abs(value - round(value, 4)) > 1e-9 for values in series_values for value in values)
    cache_format = None
    node = chart._chartSpace.find(".//" + qn("c:val") + "//" + qn("c:formatCode"))
    if node is None:
        node = chart._chartSpace.find(".//" + qn("c:yVal") + "//" + qn("c:formatCode"))
    if node is not None and node.text:
        cache_format = node.text
    labels = next((_effective_format(plot.data_labels, cache_format) for plot in plots if plot.has_data_labels), None)
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


def inspect_pptx(data: bytes, template: bytes | None = None) -> dict[str, Any]:
    """Read facts from a saved deck. Runs in the bounded parse child."""
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    slide_area = int(presentation.slide_width) * int(presentation.slide_height)
    reference = _template_index(template) if template is not None else None
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
    facts = [{"kind": "chart", "detail": detail} for detail in chart_facts[:_MAX_CHART_FACTS]]
    facts += [{"kind": "overlap", "detail": detail} for detail in overlap_facts[:_MAX_OVERLAP_FACTS]]
    for number, names in unchanged[:_MAX_UNCHANGED_SLIDES]:
        rest = len(names) - _MAX_UNCHANGED_NAMES
        listed = "; ".join(names[:_MAX_UNCHANGED_NAMES]) + (f"; and {rest} more" if rest > 0 else "")
        facts.append({"kind": "same_text_as_template", "detail": _cut(f"slide {number}: same text as the template in {listed}", _MAX_GROUPED_DETAIL)})
    omitted = {
        "chart": max(0, len(chart_facts) - _MAX_CHART_FACTS),
        "overlap": max(0, len(overlap_facts) - _MAX_OVERLAP_FACTS),
        "same_text_as_template_slides": max(0, len(unchanged) - _MAX_UNCHANGED_SLIDES),
    }
    return {
        "status": "checked",
        "summary": f"{len(slides)} slides; {counts['charts']} charts, {counts['tables']} tables, {counts['pictures']} pictures.",
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


async def pptx_result_view(ctx: Any, artifact: Artifact) -> dict[str, Any]:
    """Advisory: a view that cannot be built never undoes or questions the saved file."""
    from bowhead_core.untrusted_parse import run_worker_json

    try:
        if not 0 < artifact.size <= MAX_VIEW_BYTES:
            return _not_available("file_size_limit")
        data = await ctx.artifact_store.backend.read_bounded(artifact.path, limit=MAX_VIEW_BYTES)
        if len(data) != artifact.size:
            return _not_available("content_changed")
        template = await _template_bytes(ctx, artifact)
        result, outcome = await run_worker_json(
            "pptx_result_view", {}, data=data, extra_files={"template": template} if template is not None else None
        )
        if outcome != "ok" or not isinstance(result, dict):
            return _not_available({"timeout": "time_limit", "oom": "memory_limit"}.get(outcome, "file_not_readable"))
        return result
    except Exception as exc:
        _LOG.warning("pptx_result_view.unavailable artifact_id=%s error_type=%s", artifact.id, type(exc).__name__)
        return _not_available("inspection_unavailable")


async def attach_result_views(result: Any, ctx: Any) -> Any:
    """Add a view to every PPTX or HTML file this call wrote, work files included."""
    from bowhead_core.html_static_check import inspect_retained_html

    outputs = result.get("output_artifacts") if isinstance(result, dict) else None
    store = getattr(ctx, "artifact_store", None)
    if not isinstance(outputs, list) or store is None or not getattr(ctx, "user_id", None):
        return result
    for output in outputs:
        if not isinstance(output, dict) or not isinstance(output.get("artifact_id"), str) or output.get("data_role") == "stdout":
            continue
        pptx = is_pptx(output.get("mime"), output.get("name"))
        html = str(output.get("mime") or "").split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}
        if not pptx and not html:
            continue
        try:
            artifact = await store.get(output["artifact_id"], check_created_by=ctx.user_id, reject_null_owner=True)
            if artifact is None or artifact.created_by_turn != getattr(ctx, "turn_id", None) or artifact.filename != output.get("name"):
                continue
            if pptx:
                output["result_view"] = await pptx_result_view(ctx, artifact)
            elif "html_static_check" not in output and (inspection := await inspect_retained_html(store, artifact)) is not None:
                output["html_static_check"] = inspection
        except Exception as exc:
            _LOG.warning("result_view.attach_failed artifact_id=%s error_type=%s", output.get("artifact_id"), type(exc).__name__)
    return result
