"""Bounded native-chart reads and sparse in-place presentation edits."""

from __future__ import annotations

import copy
import math
from typing import Any

from bowhead_router.types import ToolSchema

from bowhead_core.chart.date_display import DATE_PRESETS, date_pattern, date_pattern_issue
from bowhead_core.chart.display_receipt import chart_display_receipt
from bowhead_core.chart.expression import ChartExpressionError, structured, validate_layout
from bowhead_core.chart.numeric_display import numeric_display, numeric_format_kinds
from bowhead_core.terminal_effect_contract import ClassifiedToolEffectResult, EffectProbeEvidence
from bowhead_core.terminal_protocol import EffectSettlementOutcome, canonical_digest_v1


def read_schema() -> ToolSchema:
    return ToolSchema(
        name="read_chart",
        description="Read an authorized chart's current revision, views, data sample, actual display receipt and supported presentation edits. Use a real section_id from read_report. A category axis has equal spacing, not proportional elapsed time; only a saved date column pattern confirms date formatting.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["section_id"],
            "properties": {"section_id": {"type": "string"}},
        },
    )


def edit_schema() -> ToolSchema:
    return ToolSchema(
        name="edit_chart",
        description="Edit an existing native chart in place using its current revision and a sparse presentation change. Read the chart first; never supply rows or source bindings. The display receipt reports the saved dimension types, date patterns and known axis scale.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["section_id", "expected_rev", "changes"],
            "properties": {
                "section_id": {"type": "string"},
                "expected_rev": {"type": "integer", "minimum": 1},
                "view_id": {"type": "string", "description": "Stable child view ID; omit for the root chart."},
                "changes": {
                    "anyOf": [{"type": "object"}, {"type": "string", "maxLength": 20000}],
                    "description": "Sparse title, subtitle, show_legend, show_data_labels, chart kind from supported_kinds, root layout preset (stack, grid2, split), or formats keyed by exact column. Date: {kind: date, preset: compact_date} or {kind: date, pattern: yyyyMMdd}; numeric precision: {kind: number, decimal_places: 2, decimal_mode: fixed}. Use percent or currency only when the column's editable_format_kinds admits it. Omit unchanged fields.",
                },
                "reset": {
                    "type": "object",
                    "description": "Optional formats: array of exact date column keys whose date pattern should be removed. Numeric source precision cannot be reset safely.",
                },
            },
        },
    )


async def _target(args: dict[str, Any], ctx: Any) -> tuple[Any, Any, dict[str, Any]] | dict[str, Any]:
    store, session_id = getattr(ctx, "canvas_store", None), getattr(ctx, "session_id", None)
    section_id = args.get("section_id")
    if not store or not session_id or not isinstance(section_id, str) or not section_id:
        return {"error": "chart_unavailable", "hint": "Use a chart section ID from the current session."}
    canvas = await store.get_canvas_by_session(session_id)
    if canvas is None:
        return {"error": "chart_unavailable", "hint": "The current session has no chart."}
    sections = await store.list_sections(canvas.id, include_hidden=True)
    section = next((item for item in sections if item.id == section_id and item.kind == "chart"), None)
    if section is None or section.superseded:
        return {"error": "chart_unavailable", "hint": "Choose a current chart in this session."}
    project = getattr(ctx, "chart_authoring_projection", None)
    if not callable(project):
        return {"error": "chart_source_projection_unavailable", "hint": "The current channel cannot verify chart source access."}
    try:
        projected = await project(section)
    except Exception:
        projected = None
    if not isinstance(projected, dict) or not isinstance(projected.get("spec"), dict):
        return {"error": "chart_source_projection_unavailable", "hint": "Check access to the chart's sources and try again."}
    plan = projected["spec"].get("presentation_plan")
    if plan is not None and (
        not isinstance(plan, dict)
        or plan.get("availability") == "unavailable"
        or not isinstance(plan.get("dataset"), dict)
        or plan["dataset"].get("availability") != "available"
    ):
        return {"error": "chart_source_projection_unavailable", "hint": "The chart dataset cannot currently be verified."}
    return canvas, section, projected["spec"]


def _views(spec: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    result = [(None, spec)]
    for view in spec.get("views") or []:
        if isinstance(view, dict) and isinstance(view.get("id"), str) and isinstance(view.get("spec"), dict):
            result.append((view["id"], view["spec"]))
    return result


def _simple_chart_kinds(spec: dict[str, Any]) -> list[str]:
    """Admit only complete, single-measure shapes shared by the existing native controls."""
    if "views" in spec or "presentation_plan" in spec:
        return []
    columns = [column for column in spec.get("columns") or [] if isinstance(column, dict)]
    numeric = [column for column in columns if column.get("type") == "numeric"]
    dimensions = [column for column in columns if column.get("type") in ("date", "categorical", "geo")]
    rows = spec.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return []
    row_count = spec.get("row_count")
    if type(row_count) is not int or row_count != len(rows) or spec.get("top_n"):
        return []
    for shown_key, total_key in (("series_shown", "series_total"), ("points_shown", "points_total")):
        if shown_key in spec or total_key in spec:
            shown, total = spec.get(shown_key), spec.get(total_key)
            if type(shown) is not int or type(total) is not int or shown < 0 or shown != total:
                return []
    if len(numeric) != 1 or not dimensions or not rows or not isinstance(numeric[0].get("key"), str):
        return ["table"]
    values = [row.get(numeric[0]["key"]) for row in rows]
    if any(
        value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 2**53 - 1 or not math.isfinite(value))
        for value in values
    ):
        return ["table"]
    if not any(value is not None for value in values):
        return ["table"]
    if not any(row.get(column.get("key")) not in (None, "") for row in rows for column in dimensions if isinstance(column.get("key"), str)):
        return ["table"]
    return ["line", "bar", "hbar", "table"]


async def _read_chart(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    target = await _target(args, ctx)
    if isinstance(target, dict):
        return target
    _, section, spec = target
    views = []
    for view_id, view in _views(spec):
        columns = [
            column
            for column in view.get("columns", [])
            if isinstance(column, dict) and isinstance(column.get("key"), str) and len(column["key"]) <= 128
        ][:64]
        keys = {column["key"] for column in columns}
        rows = [row for row in view.get("rows") or [] if isinstance(row, dict)]
        views.append(
            {
                "view_id": view_id,
                "kind": view.get("kind"),
                "title": view.get("title"),
                "subtitle": view.get("subtitle"),
                "show_legend": view.get("show_legend", True),
                "show_data_labels": view.get("show_data_labels"),
                "display_receipt": chart_display_receipt(view, has_root_plan=bool(spec.get("presentation_plan"))),
                "columns": [
                    {
                        key: column[key]
                        for key in ("key", "header", "type", "unit", "currency_code", "format", "display", "date_pattern", "percent_scale")
                        if key in column
                    }
                    | ({"editable_format_kinds": numeric_format_kinds(column)} if column.get("type") == "numeric" else {})
                    for column in columns
                ],
                "row_count": view.get("row_count", len(rows)),
                "sample": [
                    {
                        key: (row[key][:256] if isinstance(row[key], str) else row[key])
                        for key in keys
                        if key in row and (row[key] is None or isinstance(row[key], (str, int, float, bool)))
                    }
                    for row in rows[:5]
                ],
            }
        )
    return {
        "section_id": section.id,
        "rev": section.rev,
        "views": views,
        "allowed_changes": [
            "title",
            "subtitle",
            "show_legend",
            "show_data_labels",
            "formats",
            *(["layout"] if spec.get("views") else []),
            *(["kind"] if _simple_chart_kinds(spec) else []),
        ],
        "supported_kinds": _simple_chart_kinds(spec),
        "layout": spec.get("layout"),
        "defaults": {"show_legend": True, "show_data_labels": "automatic"},
        "date_presets": ["auto", *DATE_PRESETS],
    }


async def read_chart(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from bowhead_core.chart.edit_guard import blocked, denial, protect_existing_chart

    if "chart_guard_history_unavailable" in await blocked(ctx):
        return denial(history_unavailable=True)
    result = await _read_chart(args, ctx)
    if result.get("error") == "chart_source_projection_unavailable":
        if await protect_existing_chart(ctx, args.get("section_id")):
            result = {**result, "section_id": args["section_id"], "chart_guarded": True}
    return result


def _invalid(path: str, message: str) -> dict[str, Any]:
    return {"error": "invalid_chart_change", "path": path, "hint": message}


async def _edit_chart(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from bowhead_core.agents._write_guard import check_write_trust_class
    from bowhead_core.agents.report_tools import _resolve_writer_id
    from bowhead_core.canvas_revise import emit_section_patched, is_prior_turn_section, out_of_revise_scope, section_revise_enabled
    from bowhead_core.canvas_store import CanvasNotOwnerError, CanvasRevConflictError, CanvasSectionSupersededError

    if error := check_write_trust_class():
        return error
    target = await _target(args, ctx)
    if isinstance(target, dict):
        return target
    canvas, section, projected = target
    if getattr(ctx, "parent_bus", None) is None:
        return {"error": "chart_update_unavailable", "hint": "Open the chart in Canvas and try again when live updates are available."}
    rev = args.get("expected_rev")
    if type(rev) is not int or rev < 1:
        return _invalid("/expected_rev", "Read the chart and supply its current revision.")
    if rev != section.rev:
        return {"error": "rev_conflict", "current_rev": section.rev, "hint": "Read the current chart before editing."}
    prior = is_prior_turn_section(section, getattr(ctx, "turn_id", None))
    if prior and (not section_revise_enabled() or out_of_revise_scope(getattr(ctx, "parent_bus", None), section.id)):
        return {"error": "out_of_revise_scope", "hint": "This chart is outside the admitted revision scope."}
    try:
        changes, reset = structured(args.get("changes"), "changes"), structured(args.get("reset", {}), "reset")
    except (ValueError, TypeError, RecursionError):
        return _invalid("/changes", "Supply a valid bounded JSON object for changes and reset.")
    if (
        not isinstance(changes, dict)
        or not isinstance(reset, dict)
        or set(changes) - {"title", "subtitle", "show_legend", "show_data_labels", "formats", "layout", "kind"}
        or set(reset) - {"formats"}
    ):
        return _invalid("/changes", "Use only supported sparse presentation fields.")
    plan = projected.get("presentation_plan")
    if isinstance(plan, dict):
        if plan.get("availability") == "unavailable":
            return _invalid("/section_id", "The chart presentation plan is unavailable or unsupported.")
        notes = plan.get("evidence_notes") or []
        dataset = plan.get("dataset") or {}
        if ({"title", "subtitle"} & set(changes)) and (
            dataset.get("availability") != "available" or any(note.get("availability") != "available" for note in notes if isinstance(note, dict))
        ):
            return _invalid("/changes", "The original chart notes are unavailable; edit display settings only.")
    spec = copy.deepcopy(section.payload.get("spec"))
    if not isinstance(spec, dict):
        return _invalid("/section_id", "This section has no native chart specification.")
    view_id = args.get("view_id")
    canonical_view = next((view for key, view in _views(spec) if key == view_id), None)
    safe_view = next((view for key, view in _views(projected) if key == view_id), None)
    if canonical_view is None or safe_view is None:
        return _invalid("/view_id", "Use a stable view ID returned by read_chart, or omit it for the root.")
    if "kind" in changes:
        kind = changes["kind"]
        if view_id is not None or not isinstance(kind, str) or kind not in _simple_chart_kinds(projected) or kind not in _simple_chart_kinds(spec):
            return _invalid(
                "/changes/kind", "Choose a supported kind returned by read_chart; complex or incomplete figures keep their existing encoding."
            )
        spec["kind"] = kind
    if "layout" in changes:
        preset = changes["layout"]
        view_ids = [key for key, _ in _views(spec) if key is not None]
        if view_id is not None or not isinstance(preset, str) or preset not in {"stack", "grid2", "split"} or len(view_ids) < 2:
            return _invalid("/changes/layout", "Use stack, grid2 or split on a composed chart root.")
        try:
            ordered = validate_layout(spec.get("layout"), view_ids)
            layout = {"type": preset, "children": [{"view": key} for key in ordered]}
            validate_layout(layout, view_ids)
        except (ValueError, TypeError):
            return _invalid("/changes/layout", "The layout must preserve every view in order; split requires exactly two views.")
        spec["layout"] = layout
    for key in ("title", "subtitle"):
        if key in changes:
            value = changes[key]
            if not isinstance(value, str) or len(value) > 200 or (key == "title" and not value.strip()):
                return _invalid(f"/changes/{key}", "Use a text value of at most 200 characters; title cannot be empty.")
            canonical_view[key] = value
    for key in ("show_legend", "show_data_labels"):
        if key in changes:
            if type(changes[key]) is not bool:
                return _invalid(f"/changes/{key}", "Use true or false.")
            canonical_view[key] = changes[key]
    formats, resets = changes.get("formats", {}), reset.get("formats", [])
    if not isinstance(formats, dict) or not isinstance(resets, list) or any(not isinstance(key, str) for key in resets):
        return _invalid("/changes/formats", "Use a map of exact column keys and a reset list of exact keys.")
    safe_columns = {c.get("key"): c for c in safe_view.get("columns", []) if isinstance(c, dict) and isinstance(c.get("key"), str)}
    columns = {c.get("key"): c for c in canonical_view.get("columns", []) if isinstance(c, dict) and isinstance(c.get("key"), str)}
    if set(formats) & set(resets):
        return _invalid("/reset/formats", "A column cannot be changed and reset together.")
    for key in [*formats, *resets]:
        if key not in columns or key not in safe_columns or columns[key].get("type") != safe_columns[key].get("type"):
            return _invalid(f"/changes/formats/{key}", "Choose an accessible column returned by read_chart.")
    for key in resets:
        if columns[key].get("type") != "date":
            return _invalid(f"/reset/formats/{key}", "Only date overrides can be reset; numeric source precision cannot be reconstructed safely.")
        columns[key].pop("date_pattern", None)
    for key, value in formats.items():
        column = columns[key]
        if (
            isinstance(value, dict)
            and value.get("kind") == "date"
            and column.get("type") == "date"
            and set(value) in ({"kind", "pattern"}, {"kind", "preset"})
        ):
            if value.get("preset") == "auto":
                column.pop("date_pattern", None)
                continue
            preset = value.get("preset")
            raw_pattern = value.get("pattern") if "pattern" in value else DATE_PRESETS.get(preset) if isinstance(preset, str) else None
            pattern = date_pattern(raw_pattern)
            if pattern is None:
                return _invalid(f"/changes/formats/{key}", "Choose a supported date preset or pattern returned by read_chart.")
            rows = [row for row in canonical_view.get("rows") or [] if isinstance(row, dict)]
            row_count = canonical_view.get("row_count", len(rows))
            if canonical_view.get("kind") == "table" and isinstance(row_count, int) and row_count > len(rows):
                return _invalid(
                    f"/changes/formats/{key}",
                    "The Table has unloaded rows; create the chart from the complete source to validate the requested date format.",
                )
            if issue := date_pattern_issue(pattern, rows, key):
                return _invalid(f"/changes/formats/{key}", issue)
            column["date_pattern"] = pattern
        elif (
            isinstance(value, dict)
            and value.get("kind") in numeric_format_kinds(column)
            and value.get("kind") in numeric_format_kinds(safe_columns[key])
            and column.get("type") == "numeric"
            and set(value) == {"kind", "decimal_places", "decimal_mode"}
            and (display := numeric_display({k: v for k, v in value.items() if k != "kind"}))
        ):
            column["display"] = display
        else:
            return _invalid(f"/changes/formats/{key}", "Use a supported date pattern or numeric precision matching the column type.")
    if not changes and not resets:
        return _invalid("/changes", "Provide at least one change or reset.")
    try:
        updated = await ctx.canvas_store.patch_section(
            section_id=section.id,
            patch={"spec": spec},
            expected_rev=rev,
            by_turn_id=getattr(ctx, "turn_id", None),
            caller=_resolve_writer_id(ctx),
            skip_owner_check=prior,
            canvas_id=canvas.id,
            reject_superseded=True,
        )
    except CanvasRevConflictError as exc:
        return {"error": "rev_conflict", "current_rev": exc.current_rev, "hint": "Read the current chart before editing."}
    except (CanvasNotOwnerError, CanvasSectionSupersededError):
        return {"error": "chart_not_editable", "hint": "The chart is owned by another current writer or is no longer current."}
    except ChartExpressionError:
        return _invalid("/changes", "The change is incompatible with the saved chart presentation plan.")
    if updated is None:
        return {"error": "chart_unavailable", "hint": "The chart is no longer in this session."}
    try:
        await emit_section_patched(
            getattr(ctx, "parent_bus", None),
            turn_id=getattr(ctx, "turn_id", None),
            canvas_id=canvas.id,
            section=updated,
            open_turn=prior,
            require_delivery=True,
        )
    except Exception as exc:
        from bowhead_router.errors import InvocationOutcomeUnknownError

        raise InvocationOutcomeUnknownError("chart_update_event_unconfirmed") from exc
    return {
        "section_id": updated.id,
        "rev": updated.rev,
        "view_id": view_id,
        "display_receipt": chart_display_receipt(canonical_view, has_root_plan=bool(spec.get("presentation_plan"))),
    }


async def edit_chart(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from bowhead_core.chart.edit_guard import blocked, clear, denial, protect_existing_chart

    if "chart_guard_history_unavailable" in await blocked(ctx):
        return denial(history_unavailable=True)
    try:
        result = await _edit_chart(args, ctx)
    except Exception:
        await protect_existing_chart(ctx, args.get("section_id"))
        raise
    if "error" in result:
        if await protect_existing_chart(ctx, args.get("section_id")):
            result = {**result, "section_id": args["section_id"], "chart_guarded": True}
    elif isinstance(args.get("section_id"), str):
        clear(ctx, args["section_id"])
        result = {**result, "chart_guarded": False}
    return result


async def edit_chart_effect(args: dict[str, Any], ctx: Any) -> ClassifiedToolEffectResult:
    """Classify the observed write; an interrupted write is never retried automatically."""
    result = await edit_chart(args, ctx)
    outcome = EffectSettlementOutcome.CONFIRMED_NOT_APPLIED if "error" in result else EffectSettlementOutcome.CONFIRMED_APPLIED
    digest = canonical_digest_v1(result)
    return ClassifiedToolEffectResult(
        result=result,
        evidence=EffectProbeEvidence(
            outcome=outcome,
            settlement_code=f"tool_external_result/v1:{outcome.value}",
            evidence_digest=digest,
            object_version_digest=digest,
            audit_object_ref=f"chart:{result.get('section_id', 'denied')}",
            provider_external_ref_digest=digest,
            provider_external_ref_key_version="chart-edit-v1",
        ),
    )
