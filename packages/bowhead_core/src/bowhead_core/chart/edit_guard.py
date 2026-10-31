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


def denial(*, history_unavailable: bool = False) -> dict[str, str]:
    if history_unavailable:
        return {
            "error": "chart_guard_history_unavailable",
            "hint": "The current turn's chart edit history could not be checked. Restore the turn history and retry.",
        }
    return {
        "error": "chart_edit_incomplete",
        "hint": "The chart edit did not complete. Retry the in-place edit or report the error; do not replace charts or rewrite the report as a fallback. A separately requested replacement can be made in a new turn.",
    }
