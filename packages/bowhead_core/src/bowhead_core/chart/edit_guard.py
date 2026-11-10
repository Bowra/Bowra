"""Turn-local protection for a chart whose requested edit could not be completed."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any


def _owner(ctx: Any) -> Any:
    return getattr(ctx, "parent_bus", None) or ctx


def _key(ctx: Any) -> tuple[str, str]:
    return str(getattr(ctx, "session_id", "") or ""), str(getattr(ctx, "turn_id", "") or "")


def coordinate_chart_handler(handler: Callable[..., Awaitable[Any]], *, inspection: bool = False) -> Callable[..., Awaitable[Any]]:
    """Order only chart-related writes that overlap a chart read or edit."""

    @wraps(handler)
    async def coordinated(args: dict[str, Any], ctx: Any) -> Any:
        owner = _owner(ctx)
        lock = getattr(owner, "_chart_edit_operation_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            owner._chart_edit_operation_lock = lock
        if inspection or lock.locked():
            async with lock:
                return await handler(args, ctx)
        return await handler(args, ctx)

    return coordinated


async def blocked(ctx: Any) -> set[str]:
    owner = _owner(ctx)
    lock = getattr(owner, "_chart_edit_failure_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        owner._chart_edit_failure_lock = lock
    async with lock:
        guards = getattr(owner, "_chart_edit_failures", None)
        if not isinstance(guards, dict):
            guards = {}
            owner._chart_edit_failures = guards
        key = _key(ctx)
        if key not in guards:
            restored: set[str] = set()
            path = getattr(getattr(ctx, "session_store", None), "db_path", None)
            if path and key[1]:
                from bowhead_core.turn_log_store import TurnLogStore

                try:
                    events = await TurnLogStore(db_path=str(path)).list_events(key[1], event_types=("tool_result",))
                except Exception:
                    return {"chart_guard_history_unavailable"}
                for event in events:
                    payload = event.payload
                    result = payload.get("result_inline") if isinstance(payload, dict) else None
                    meta = payload.get("result_meta") if isinstance(payload, dict) else None
                    name = payload.get("tool_name") if isinstance(payload, dict) else None
                    if name not in {"read_chart", "edit_chart"}:
                        continue
                    marker = meta.get("chart_guard") if isinstance(meta, dict) else None
                    if isinstance(marker, dict) and type(marker.get("blocked")) is bool:
                        section_id, is_blocked = marker.get("section_id"), marker["blocked"]
                    elif isinstance(result, dict):
                        section_id = result.get("section_id")
                        is_blocked = result.get("chart_guarded")
                    else:
                        continue
                    if not isinstance(section_id, str):
                        continue
                    if is_blocked is True:
                        restored.add(section_id)
                    elif name == "edit_chart" and is_blocked is False:
                        restored.discard(section_id)
            guards[key] = restored
        return set(guards[key])


async def protect_existing_chart(ctx: Any, section_id: Any) -> bool:
    if not isinstance(section_id, str) or not section_id:
        return False
    store = getattr(ctx, "canvas_store", None)
    session = getattr(ctx, "session_id", None)
    if store is None or not session:
        return False
    canvas = await store.get_canvas_by_session(session)
    if canvas is None:
        return False
    sections = await store.list_sections(canvas.id, include_hidden=True)
    if not any(section.id == section_id and section.kind == "chart" and not section.superseded for section in sections):
        return False
    owner = _owner(ctx)
    guards = getattr(owner, "_chart_edit_failures", None)
    if not isinstance(guards, dict):
        guards = {}
        owner._chart_edit_failures = guards
    guards.setdefault(_key(ctx), set()).add(section_id)
    return True


def clear(ctx: Any, section_id: str) -> None:
    guards = getattr(_owner(ctx), "_chart_edit_failures", None)
    if isinstance(guards, dict):
        guards.get(_key(ctx), set()).discard(section_id)


def _title_key(value: Any) -> str | None:
    text = " ".join(value.split()).casefold() if isinstance(value, str) else ""
    return text or None


def _chart_identity(spec: Any) -> tuple[set[str], set[str]]:
    """Data artifacts and titles that identify one saved chart and its views."""
    data: set[str] = set()
    titles: set[str] = set()
    specs = [spec] if isinstance(spec, dict) else []
    specs += [view["spec"] for view in (spec.get("views") or []) if isinstance(view, dict) and isinstance(view.get("spec"), dict)] if specs else []
    for item in specs:
        source = item.get("source_meta") if isinstance(item.get("source_meta"), dict) else {}
        plan = item.get("presentation_plan") if isinstance(item.get("presentation_plan"), dict) else {}
        dataset = plan.get("dataset") if isinstance(plan.get("dataset"), dict) else {}
        for value in (item.get("data_artifact_id"), source.get("artifact_id"), source.get("id"), dataset.get("artifact_id")):
            if isinstance(value, str) and value.startswith("art_"):
                data.add(value)
        if title := _title_key(item.get("title")):
            titles.add(title)
    return data, titles


async def replaced_protected_chart(ctx: Any, protected: set[str], args: dict[str, Any], *, resolve_data_ref: Callable[[str], str]) -> str | None:
    """Return the protected chart an add_chart request would replace, if any.

    A failed in-place edit protects only that chart: a new chart that reuses
    its data or its title is treated as a replacement and refused, while an
    unrelated chart remains allowed. An unverifiable comparison fails closed.
    """
    if not protected:
        return None
    requests = [args, *[view for view in args.get("views") or [] if isinstance(view, dict)]]
    data = {resolve_data_ref(str(item.get("data_artifact_id") or "").strip()) for item in requests if item.get("data_artifact_id")}
    titles = {title for item in requests if (title := _title_key(item.get("title")))}
    ordered = sorted(protected)
    try:
        store, session = getattr(ctx, "canvas_store", None), getattr(ctx, "session_id", None)
        canvas = await store.get_canvas_by_session(session) if store is not None and session else None
        if canvas is None:
            return ordered[0]
        sections = await store.list_sections(canvas.id, include_hidden=True)
    except Exception:
        return ordered[0]
    for section in sections:
        if section.id not in protected or section.kind != "chart":
            continue
        protected_data, protected_titles = _chart_identity((section.payload or {}).get("spec"))
        if data & protected_data or titles & protected_titles:
            return section.id
    return None


def denial(*, history_unavailable: bool = False, section_id: str | None = None) -> dict[str, str]:
    if history_unavailable:
        return {
            "error": "chart_guard_history_unavailable",
            "hint": "The current turn's chart edit history could not be checked. Restore the turn history and retry.",
        }
    if section_id:
        return {
            "error": "chart_edit_incomplete",
            "section_id": section_id,
            "hint": f"This new chart reuses the data or title of chart {section_id}, whose in-place edit did not complete in this turn. Retry read_chart and edit_chart on that chart, or report why it cannot be edited; do not replace it with a new chart or image. Charts with other data and titles can still be added.",
        }
    return {
        "error": "chart_edit_incomplete",
        "hint": "The chart edit did not complete. Retry the in-place edit or report the error; do not replace charts or rewrite the report as a fallback. A separately requested replacement can be made in a new turn.",
    }
