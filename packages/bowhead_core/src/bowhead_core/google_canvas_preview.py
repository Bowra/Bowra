"""Show a file the Lead just wrote to Google as a card in the report canvas.

Three layers, so every browser sees something (design §6.3, decision 2026-10-01):

1. always: the file title and an "open in Google" link;
2. default: a snapshot read back with the member's own token right after the write
   (Docs → markdown, Slides → slide text, Sheets → the rows just written), rendered
   by Bowhead itself, so it does not depend on third-party cookies;
3. optional: a live read-only Google preview, loaded only when the member clicks it
   (it needs the browser's Google session inside the iframe, which Chrome/Edge allow
   and Safari/Firefox/Brave block by default).

The card reuses the existing ``external_iframe`` canvas section (``provider:
"google_workspace"`` plus snapshot fields), so persistence, replay and live events
follow that section's contract. Repeated writes to the same file within a turn
update the card instead of adding another. Publishing is best effort: a failure here
never turns a successful write into an error.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlencode

from bowhead_core.google_docs_markdown import document_to_markdown, presentation_to_markdown
from bowhead_core.google_workspace import PROVIDER, http_client

logger = logging.getLogger(__name__)

_SNAPSHOT_CHARS: Final = 20_000
_SNAPSHOT_ROWS: Final = 100
_SNAPSHOT_COLS: Final = 30
_CARD_HEIGHT: Final = 560
_PREVIEW_PATH: Final = {
    "document": "https://docs.google.com/document/d/{id}/preview",
    "spreadsheet": "https://docs.google.com/spreadsheets/d/{id}/htmlview",
    "presentation": "https://docs.google.com/presentation/d/{id}/embed",
}


def preview_url(file_kind: str, file_id: str, account_email: str) -> str:
    """Read-only Google preview; ``authuser`` picks the connected account when several are signed in."""
    base = _PREVIEW_PATH[file_kind].format(id=file_id)
    return f"{base}?{urlencode({'authuser': account_email})}" if account_email else base


async def _get(token: str, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with http_client() as client:
        response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _text_snapshot(text: str) -> dict[str, Any]:
    return {"format": "markdown", "content": text[:_SNAPSHOT_CHARS], "truncated": len(text) > _SNAPSHOT_CHARS}


async def read_snapshot(token: str, file_kind: str, file_id: str, rows: list[list[Any]] | None = None, a1_range: str = "") -> dict[str, Any] | None:
    """What the file looks like right after the write, or None when it cannot be read back."""
    try:
        if file_kind == "document":
            document = await _get(token, f"https://docs.googleapis.com/v1/documents/{file_id}", {"includeTabsContent": "true"})
            return _text_snapshot(document_to_markdown(document))
        if file_kind == "presentation":
            presentation = await _get(token, f"https://slides.googleapis.com/v1/presentations/{file_id}")
            return _text_snapshot(presentation_to_markdown(presentation))
        if file_kind == "spreadsheet" and rows is not None:
            kept = [[("" if cell is None else str(cell)) for cell in row[:_SNAPSHOT_COLS]] for row in rows[:_SNAPSHOT_ROWS]]
            truncated = len(rows) > _SNAPSHOT_ROWS or any(len(row) > _SNAPSHOT_COLS for row in rows)
            return {"format": "table", "rows": kept, "range": a1_range, "truncated": truncated}
    except Exception as exc:  # the write already succeeded; the card falls back to link + live preview
        logger.info("google_canvas_preview.snapshot_failed kind=%s err=%s", file_kind, type(exc).__name__)
    return None


def card_payload(*, file_kind: str, file_id: str, title: str, open_url: str, account_email: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": preview_url(file_kind, file_id, account_email),
        "title": (title or "Google")[:80],
        "height": _CARD_HEIGHT,
        "provider": PROVIDER,
        "file_kind": file_kind,
        "file_id": file_id,
        "open_url": open_url,
        "snapshot_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Always set (None when unreadable): patches merge shallowly, so a missing key would keep the
    # previous write's snapshot under the new timestamp.
    payload["snapshot"] = snapshot
    return payload


async def publish(ctx: Any, payload: dict[str, Any]) -> bool:
    """Append (or, for the same file in this turn, update) the canvas card. Best effort."""
    store = getattr(ctx, "canvas_store", None)
    session_id = getattr(ctx, "session_id", None)
    turn_id = getattr(ctx, "turn_id", None)
    if store is None or not session_id:
        return False
    try:
        canvas = await store.ensure_canvas(
            session_id=session_id, workspace_id=getattr(ctx, "workspace_id", None), user_id=getattr(ctx, "user_id", None)
        )
        existing = next(
            (
                section
                for section in await store.list_sections(canvas.id, include_hidden=True)
                if section.kind == "external_iframe"
                and (section.payload or {}).get("provider") == PROVIDER
                and (section.payload or {}).get("file_id") == payload["file_id"]
                and getattr(section, "turn_id", None) == turn_id
            ),
            None,
        )
        bus = getattr(ctx, "parent_bus", None)
        if existing is not None:
            from bowhead_core.canvas_revise import emit_section_patched

            patched = await store.patch_section(section_id=existing.id, patch=payload, by_turn_id=turn_id, skip_owner_check=True, canvas_id=canvas.id)
            await emit_section_patched(bus, turn_id=turn_id, canvas_id=canvas.id, section=patched)
            return patched is not None
        stored = await store.append_section(canvas_id=canvas.id, kind="external_iframe", payload=payload, by_turn_id=turn_id)
        if bus is not None:
            from bowhead_core.events import OrchestratorEvent

            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=turn_id or "",
                    payload={"canvas_id": canvas.id, "section": stored.to_report_section(), "seq": stored.seq},
                )
            )
        return True
    except Exception as exc:
        logger.warning("google_canvas_preview.publish_failed err=%s", type(exc).__name__)
        return False
