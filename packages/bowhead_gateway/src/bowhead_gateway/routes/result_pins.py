"""Sidebar result pins: pin, unpin and list a person's files and reports.

docs/sprints/result-pins-2026-11-24/tech-design.md §4.3. A pin is a personal shortcut: pinning needs only
read access (PRD D7), and the list shows only what the person can open now (``result_pin_resolver``).
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from bowhead_gateway.result_pin_resolver import ResolvedResultPin, resolve_result_pins

router = APIRouter()
log = structlog.get_logger(__name__)

#: Visible pinned results per person (PRD D4); pinned conversations keep their own limit.
RESULT_PIN_LIMIT = 100


class ResultPinRequest(BaseModel):
    kind: Literal["file", "report"]
    id: str = Field(min_length=1, max_length=200)


def _user_id(request: Request) -> str:
    from bowhead_gateway.routes import files as files_routes

    return files_routes._user_id(request)


def _error(status_code: int, code: str, **payload: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": {"code": code, **payload}})


def _not_found() -> HTTPException:
    # Missing and unreadable answer alike, as file reads do: a pin never reveals that a result exists.
    return _error(status.HTTP_404_NOT_FOUND, "result_pin_target_not_found")


async def _require_supported(request: Request) -> None:
    if not await request.app.state.session_store.result_pins_supported():
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "result_pins_unavailable")


async def _target_of(request: Request, kind: str, target_id: str, user_id: str) -> str:
    """The stored key of a pin request: a file's lineage root or a report's canvas ID; 404 when unreadable."""
    if kind == "report":
        from bowhead_gateway.routes import canvas as canvas_routes

        canvas = await request.app.state.canvas_store.get_canvas(target_id)
        if canvas is None or not await canvas_routes._can_read_canvas(request, canvas):
            raise _not_found()
        return str(canvas.id)
    from bowhead_gateway.routes import files as files_routes

    art = await files_routes._store(request).get(target_id)
    if art is None:
        raise _not_found()
    return str(art.lineage_root)


async def _resolve(request: Request, user_id: str, workspace_id: str | None) -> tuple[list[ResolvedResultPin], dict[str, int], float]:
    started = time.perf_counter()
    rows = await request.app.state.session_store.list_result_pins(user_id=user_id)
    listing = await resolve_result_pins(request, user_id=user_id, rows=rows, workspace_id=workspace_id)
    return listing.items, dict(listing.hidden), round((time.perf_counter() - started) * 1000, 1)


@router.get("")
async def list_result_pins(
    request: Request,
    workspace_id: str | None = Query(None, description="The current Space; own results of other Spaces are left out."),
) -> dict[str, Any]:
    user_id = _user_id(request)
    items, hidden, elapsed_ms = await _resolve(request, user_id, workspace_id)
    log.info("result_pins.list", count=len(items), hidden=hidden, elapsed_ms=elapsed_ms)
    return {"items": [item.dto() for item in items]}


@router.post("")
async def pin_result(body: ResultPinRequest, request: Request) -> dict[str, Any]:
    user_id = _user_id(request)
    await _require_supported(request)
    target_id = await _target_of(request, body.kind, body.id.strip(), user_id)
    # Check the result opens from the sidebar before writing anything (an empty report does not).
    probe = await resolve_result_pins(request, user_id=user_id, rows=[(body.kind, target_id, datetime.now(UTC).isoformat())], workspace_id=None)
    if not probe.items:
        raise _not_found()
    store = request.app.state.session_store
    current = {(kind, target) for kind, target, _ in await store.list_result_pins(user_id=user_id)}
    if (body.kind, target_id) not in current:
        # The limit counts the pins the person can see: hidden ones cannot be unpinned (PRD §4.4).
        visible, _, _ = await _resolve(request, user_id, None)
        if len(visible) >= RESULT_PIN_LIMIT:
            raise _error(status.HTTP_409_CONFLICT, "result_pin_limit", limit=RESULT_PIN_LIMIT)
        await store.pin_result(user_id=user_id, target_kind=body.kind, target_id=target_id)
    pinned_at = next(
        (at for kind, target, at in await store.list_result_pins(user_id=user_id) if (kind, target) == (body.kind, target_id)),
        probe.items[0].pinned_at,
    )
    return replace(probe.items[0], pinned_at=pinned_at).dto()


@router.delete("/{kind}/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unpin_result(kind: Literal["file", "report"], target_id: str, request: Request) -> Response:
    user_id = _user_id(request)
    store = request.app.state.session_store
    if not await store.result_pins_supported():
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    key = target_id.strip()
    if kind == "file" and key:
        from bowhead_gateway.routes import files as files_routes

        # Any version unpins the file, also a deleted one.
        art = await files_routes._store(request).get(key, include_deleted=True)
        if art is not None:
            key = str(art.lineage_root)
    if key:
        await store.unpin_result(user_id=user_id, target_kind=kind, target_id=key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
