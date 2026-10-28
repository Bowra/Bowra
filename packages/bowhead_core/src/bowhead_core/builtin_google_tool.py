"""Google Workspace read tools on the team built-in tool overlay (analyst only).

Design: ``docs/design/Google Workspace 连接-技术设计-2026-09-24.md`` §6.1 / §7.

* Built only for attended web runs (``config_lite_supported``, the same single
  criterion ``send_email`` uses): scheduled jobs, plans, workflow runs and IM
  channels never see these tools and never touch a member's refresh token.
* Every call runs as ``ctx.user_id`` — the member who started the conversation —
  with that member's own grant. A missing or stale grant is an expected outcome
  that carries the way out (connect / re-authorize), not a failure.
* Reads are bounded and truncation is always explicit (offset / range to continue).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import anyio
import httpx2
from bowhead_router.types import ToolSchema

from bowhead_core.google_docs_markdown import (
    document_tab_choices,
    document_tab_id,
    document_to_markdown,
    presentation_to_markdown,
    select_document_tab,
)
from bowhead_core.google_workspace import (
    BASE_BUNDLE,
    PROVIDER,
    GoogleWorkspaceError,
    bundle_granted,
    enabled_bundles,
    feature_enabled,
    http_client,
    token_broker,
)
from bowhead_core.tool_registry import TeamToolExecutionBinding, ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.types import ToolAnnotations

logger = logging.getLogger(__name__)

READ_OPERATION = "gworkspace_read"
CALENDAR_OPERATION = "gcalendar_events"
OPERATIONS = (READ_OPERATION, CALENDAR_OPERATION)
_TEXT_LIMIT = 30_000
_SHEET_ROW_LIMIT = 500
_SHEET_CHAR_LIMIT = 60_000
_CALENDAR_LIMIT = 250
_MAX_CALENDAR_DAYS = 92
# Expected outcomes: the result carries the way out; the step is not a failure.
EXPECTED_ERROR_KINDS = frozenset(
    {"google_not_connected", "google_reauth_required", "google_scope_missing", "google_file_not_granted", "google_file_not_found"}
)
_CONNECT_ERROR_KINDS = frozenset({"google_not_connected", "google_reauth_required", "google_scope_missing"})
_NATIVE_TYPES = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
}
_LINK = re.compile(
    r"^https://(?:docs\.google\.com/(?P<kind>document|spreadsheets|presentation)/(?:u/\d+/)?d/(?P<id1>[A-Za-z0-9_-]{10,})"
    r"|drive\.google\.com/(?:file/(?:u/\d+/)?d/(?P<id2>[A-Za-z0-9_-]{10,})|open\?(?:.*&)?id=(?P<id3>[A-Za-z0-9_-]{10,})))"
)
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_KIND_BY_PATH = {"document": "document", "spreadsheets": "spreadsheet", "presentation": "presentation"}


def google_tool_ref(tool_id: str, operation: str) -> str:
    return f"teammcp.{tool_id}.{operation}"


def google_source_label(name: str) -> str:
    return f"Google Workspace - {name}"


def runtime_operations(tool: Any) -> tuple[str, ...]:
    """Operations the connection's enabled bundles allow (for overlay, labels, catalog)."""
    if not feature_enabled() or tool.provider != PROVIDER or tool.status != "enabled":
        return ()
    bundles = enabled_bundles(tool.config)
    return tuple(op for op in OPERATIONS if op != CALENDAR_OPERATION or "calendar" in bundles)


def parse_file_reference(value: str) -> tuple[str | None, str] | None:
    """(kind or None when unknown, file id); None when the reference is not a Google file."""
    text = value.strip()
    match = _LINK.match(text)
    if match:
        kind = _KIND_BY_PATH.get(match.group("kind") or "")
        return kind, match.group("id1") or match.group("id2") or match.group("id3")
    if _BARE_ID.match(text):
        return None, text
    return None


class _GoogleCallError(Exception):
    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(status)
        self.status = status
        self.reason = reason


async def _get(token: str, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    delay = 0.5
    for attempt in range(3):
        try:
            async with http_client() as client:
                response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except httpx2.HTTPError:
            if attempt == 2:
                raise GoogleWorkspaceError("google_unreachable") from None
            await anyio.sleep(delay)
            delay *= 2
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                raise GoogleWorkspaceError("google_quota_exceeded" if response.status_code == 429 else "google_unreachable")
            await anyio.sleep(delay)
            delay *= 2
            continue
        if not response.is_success:
            reason = ""
            try:
                errors = (response.json().get("error") or {}).get("errors") or []
                reason = str(errors[0].get("reason") or "") if errors else ""
            except (ValueError, AttributeError, IndexError):
                reason = ""
            raise _GoogleCallError(response.status_code, reason)
        try:
            payload = response.json()
        except ValueError:
            raise GoogleWorkspaceError("google_operation_failed") from None
        return payload if isinstance(payload, dict) else {}
    raise GoogleWorkspaceError("google_unreachable")  # pragma: no cover - loop always returns or raises


def _file_error(exc: _GoogleCallError, *, by_link_available: bool) -> dict[str, Any]:
    if exc.status == 401:
        return _failure("google_reauth_required")
    if exc.status == 403 and exc.reason in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}:
        return _failure("google_quota_exceeded")
    if exc.status in {403, 404}:
        if by_link_available:
            return _failure("google_file_not_found")
        return _failure("google_file_not_granted")
    return _failure("google_operation_failed")


_GUIDANCE = {
    "google_not_connected": "The user has not connected their Google account. Tell them to connect it under My connections, then ask again. Do not guess the file's content.",
    "google_reauth_required": "The user's Google authorization has expired or was revoked. Tell them to reconnect Google under My connections, then ask again. Do not guess the file's content.",
    "google_scope_missing": "The user's Google authorization does not include this capability yet. Tell them to update their Google connection under My connections, then ask again.",
    "google_file_not_granted": (
        "Bowhead cannot open this file with the current permissions. The user can insert it with 'Select from Google Drive' in the "
        "message box, or ask a team administrator to enable 'Read by link' for Google Workspace. Do not guess the file's content."
    ),
    "google_file_not_found": "Google reports the file does not exist or the user cannot open it. Ask the user to check the link and their access.",
    "google_file_type_unsupported": "Only Google Docs, Sheets and Slides can be read directly. Ask the user to import other files into the knowledge base.",
    "google_invalid_reference": "The value is not a Google Docs, Sheets, Slides or Drive link. Ask the user for the file link.",
    "google_invalid_range": "The time range is invalid. Use ISO 8601 times, time_min before time_max, at most 92 days apart.",
}


def _failure(kind: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error_kind": kind}
    if kind in _GUIDANCE:
        result["guidance"] = _GUIDANCE[kind]
    result.update(extra)
    return result


def _page_text(text: str, offset: int) -> dict[str, Any]:
    total = len(text)
    start = min(max(offset, 0), total)
    chunk = text[start : start + _TEXT_LIMIT]
    end = start + len(chunk)
    page: dict[str, Any] = {"content": chunk, "offset": start, "returned_chars": len(chunk), "total_chars": total, "truncated": end < total}
    if end < total:
        page["next_offset"] = end
        page["note"] = f"Only characters {start}–{end} of {total} are shown. Call again with offset={end} to read more; do not assume the rest."
    return page


async def _read_file(token: str, grant_scopes: tuple[str, ...], by_link_enabled: bool, args: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_file_reference(str(args.get("file") or ""))
    if parsed is None:
        return _failure("google_invalid_reference")
    kind, file_id = parsed
    by_link = by_link_enabled and bundle_granted("read_by_link", grant_scopes)
    try:
        name = ""
        if not by_link_enabled and kind is not None:
            # "Read by link" is off: only files the member picked (or Bowhead created) may be read.
            # Drive metadata answers only for those, since the token never holds a Drive-wide scope.
            await _get(token, f"https://www.googleapis.com/drive/v3/files/{file_id}", {"fields": "id", "supportsAllDrives": "true"})
        if kind is None:
            try:
                meta = await _get(
                    token, f"https://www.googleapis.com/drive/v3/files/{file_id}", {"fields": "name,mimeType", "supportsAllDrives": "true"}
                )
            except _GoogleCallError:
                if not by_link:
                    raise
                # Read-by-link scopes do not cover Drive metadata: find the type through the editors' APIs.
                kind = await _probe_kind(token, file_id)
                if kind is None:
                    raise
            else:
                kind = _NATIVE_TYPES.get(str(meta.get("mimeType") or ""))
                name = str(meta.get("name") or "")
                if kind is None:
                    return _failure("google_file_type_unsupported", name=name)
        if kind == "document":
            document = await _get(token, f"https://docs.googleapis.com/v1/documents/{file_id}", {"includeTabsContent": "true"})
            tab_id = document_tab_id(str(args.get("file") or ""))
            content = document
            tabs = document_tab_choices(document, file_id)
            url = f"https://docs.google.com/document/d/{file_id}"
            if tab_id is not None:
                selected = select_document_tab(document, tab_id)
                if selected is None:
                    return _failure("google_invalid_reference", tabs=document_tab_choices(document, file_id))
                content = selected.get("documentTab", selected)
                url = next(tab["url"] for tab in tabs if tab["id"] == tab_id)
            page = _page_text(document_to_markdown(content), int(args.get("offset") or 0))
            return {
                "ok": True,
                "type": "document",
                "file_id": file_id,
                "title": document.get("title") or name,
                "url": url,
                "tabs": tabs,
                **page,
            }
        if kind == "presentation":
            presentation = await _get(token, f"https://slides.googleapis.com/v1/presentations/{file_id}")
            page = _page_text(presentation_to_markdown(presentation), int(args.get("offset") or 0))
            return {
                "ok": True,
                "type": "presentation",
                "file_id": file_id,
                "title": presentation.get("title") or name,
                "url": f"https://docs.google.com/presentation/d/{file_id}",
                "slide_count": len(presentation.get("slides") or []),
                **page,
            }
        return await _read_sheet(token, file_id, str(args.get("range") or "").strip(), name)
    except _GoogleCallError as exc:
        return _file_error(exc, by_link_available=by_link)


async def _probe_kind(token: str, file_id: str) -> str | None:
    probes = (
        ("document", f"https://docs.googleapis.com/v1/documents/{file_id}", {"fields": "documentId"}),
        ("spreadsheet", f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}", {"fields": "spreadsheetId"}),
        ("presentation", f"https://slides.googleapis.com/v1/presentations/{file_id}", {"fields": "presentationId"}),
    )
    for kind, url, params in probes:
        try:
            await _get(token, url, params)
        except _GoogleCallError:
            continue
        return kind
    return None


async def _read_sheet(token: str, file_id: str, a1_range: str, name: str) -> dict[str, Any]:
    url = f"https://docs.google.com/spreadsheets/d/{file_id}"
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}"
    if not a1_range:
        meta = await _get(token, base, {"fields": "properties.title,sheets.properties(title,gridProperties(rowCount,columnCount))"})
        sheets = [
            {
                "title": (sheet.get("properties") or {}).get("title"),
                "rows": ((sheet.get("properties") or {}).get("gridProperties") or {}).get("rowCount"),
                "columns": ((sheet.get("properties") or {}).get("gridProperties") or {}).get("columnCount"),
            }
            for sheet in meta.get("sheets") or []
        ]
        return {
            "ok": True,
            "type": "spreadsheet",
            "file_id": file_id,
            "title": (meta.get("properties") or {}).get("title") or name,
            "url": url,
            "sheets": sheets,
            "note": "No range was given, so only the sheet list is returned. Call again with range, e.g. 'Sheet1!A1:Z200', to read values.",
        }
    values = await _get(
        token, f"{base}/values/{quote(a1_range, safe='!:$')}", {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"}
    )
    rows = values.get("values") or []
    kept: list[list[Any]] = []
    chars = 0
    for row in rows[:_SHEET_ROW_LIMIT]:
        size = len(json.dumps(row, ensure_ascii=False))
        if chars + size > _SHEET_CHAR_LIMIT:
            break
        kept.append(row)
        chars += size
    result: dict[str, Any] = {
        "ok": True,
        "type": "spreadsheet",
        "file_id": file_id,
        "url": url,
        "range": values.get("range") or a1_range,
        "value_representation": "unformatted_numbers_with_source_temporal_labels",
        "rows": kept,
        "returned_rows": len(kept),
        "total_rows": len(rows),
        "truncated": len(kept) < len(rows),
    }
    if len(kept) < len(rows):
        result["note"] = (
            f"Only the first {len(kept)} of {len(rows)} rows are shown. Read the rest with a narrower range; do not assume the missing rows."
        )
    return result


def _iso(value: Any) -> str | None:
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


async def _calendar(token: str, args: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime

    start, end = _iso(args.get("time_min")), _iso(args.get("time_max"))
    if not start or not end:
        return _failure("google_invalid_range")
    span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    if span.total_seconds() <= 0 or span.days > _MAX_CALENDAR_DAYS:
        return _failure("google_invalid_range")
    params: dict[str, Any] = {"timeMin": start, "timeMax": end, "singleEvents": "true", "orderBy": "startTime", "maxResults": _CALENDAR_LIMIT}
    if str(args.get("query") or "").strip():
        params["q"] = str(args["query"]).strip()[:200]
    try:
        payload = await _get(token, "https://www.googleapis.com/calendar/v3/calendars/primary/events", params)
    except _GoogleCallError as exc:
        if exc.status == 401:
            return _failure("google_reauth_required")
        if exc.status == 403 and exc.reason in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}:
            return _failure("google_quota_exceeded")
        return _failure("google_operation_failed")
    events = [
        {
            "title": item.get("summary") or "",
            "start": (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date"),
            "end": (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date"),
            "location": item.get("location") or "",
            "attendees": [attendee.get("email") for attendee in item.get("attendees") or [] if attendee.get("email")],
        }
        for item in payload.get("items") or []
        if item.get("status") != "cancelled"
    ]
    result: dict[str, Any] = {
        "ok": True,
        "time_min": start,
        "time_max": end,
        "events": events,
        "returned": len(events),
        "truncated": bool(payload.get("nextPageToken")),
    }
    if payload.get("nextPageToken"):
        result["note"] = (
            f"More than {_CALENDAR_LIMIT} events exist in this range; only the first {len(events)} are shown. Narrow the range to see the rest."
        )
    return result


def _schema(operation: str, tool_ref: str, label: str) -> ToolSchema:
    if operation == READ_OPERATION:
        return ToolSchema(
            name=tool_ref,
            description=(
                f"Read a Google Docs, Sheets or Slides file the user linked, as the current user, via “{label}”. "
                "Docs return markdown (use offset to continue when truncated). Sheets without range return the sheet list; "
                "with an A1 range they return values. Slides return each slide's text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "minLength": 10, "maxLength": 500, "description": "Google file link or ID from the user's message"},
                    "range": {"type": "string", "maxLength": 200, "description": "Sheets only: A1 range such as 'Sheet1!A1:Z200'"},
                    "offset": {"type": "integer", "minimum": 0, "description": "Docs/Slides only: character offset to continue reading"},
                },
                "required": ["file"],
                "additionalProperties": False,
            },
        )
    return ToolSchema(
        name=tool_ref,
        description=f"List the current user's own Google Calendar events in a time range (at most 92 days) via “{label}”.",
        input_schema={
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO 8601 start with timezone, e.g. 2026-09-01T00:00:00+08:00"},
                "time_max": {"type": "string", "description": "ISO 8601 end with timezone"},
                "query": {"type": "string", "maxLength": 200, "description": "Optional free-text filter"},
            },
            "required": ["time_min", "time_max"],
            "additionalProperties": False,
        },
    )


def build_builtin_google_specs(*, team_id: str, tools: list[Any], store: Any, base_registry: ToolRegistry | None = None) -> list[ToolSpec]:
    del base_registry  # read tools do not delegate to built-in tools
    specs: list[ToolSpec] = []
    for tool in tools:
        label = google_source_label(tool.display_name)
        for operation in runtime_operations(tool):
            tool_ref = google_tool_ref(tool.id, operation)

            async def _execute(
                args: dict[str, Any], ctx: ToolExecutionContext, *, _tool: Any = tool, _operation: str = operation, _ref: str = tool_ref
            ) -> Any:
                started = time.perf_counter()
                result: dict[str, Any] = _failure("google_operation_failed")
                try:
                    result = await _run(_tool, _operation, args, ctx, team_id=team_id, store=store)
                    return result
                except GoogleWorkspaceError as exc:
                    result = _failure(exc.code)
                    return result
                except Exception as exc:  # upstream detail stays server-side
                    logger.warning("builtin_google.call_failed operation=%s err=%s", _operation, type(exc).__name__)
                    result = _failure("google_operation_failed")
                    return result
                finally:
                    # Structural evidence for "expected outcome" kinds and the connection notice:
                    # a third-party MCP tool returning the same error_kind is not a Google result.
                    result.setdefault("provider", PROVIDER)
                    digest = hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
                    try:
                        await store.log_tool_call(
                            team_id=team_id,
                            user_id=ctx.user_id,
                            source_id=_tool.id,
                            tool_name=_ref,
                            args_digest=digest,
                            status="ok" if result.get("ok") else "error",
                            error_kind=None if result.get("ok") else str(result.get("error_kind")),
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                    except Exception as audit_exc:
                        logger.warning("builtin_google.audit_failed team=%s tool=%s err=%s", team_id, _ref, type(audit_exc).__name__)

            specs.append(
                ToolSpec(
                    schema=_schema(operation, tool_ref, label),
                    handler=_execute,
                    allowed_for_roles=frozenset({"analyst"}),
                    risk_level="low",
                    source="external",
                    annotations=ToolAnnotations(open_world=True, destructive=False, read_only=True),
                    team_tool_binding=TeamToolExecutionBinding(team_id=team_id, source_id=tool.id, operation_id=operation, advertised_name=tool_ref),
                )
            )
    return specs


async def _run(tool: Any, operation: str, args: dict[str, Any], ctx: ToolExecutionContext, *, team_id: str, store: Any) -> dict[str, Any]:
    if ctx.team_id != team_id or not getattr(ctx, "config_lite_supported", False):
        # Defense in depth: the overlay is never built for unattended runs.
        return _failure("google_connection_unavailable")
    if not await store.user_has_active_team_membership(ctx.user_id, team_id):
        return _failure("google_connection_unavailable")
    live = await store.get_team_builtin_tool_instance(team_id, tool.id)
    if live is None or operation not in runtime_operations(live):
        return _failure("google_connection_unavailable")
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(team_id, tool.id)
    required = frozenset({BASE_BUNDLE, "calendar"} if operation == CALENDAR_OPERATION else {BASE_BUNDLE})
    token, grant = await token_broker(store.db_path).access_token(tool=live, auth=auth, user_id=ctx.user_id, required_bundles=required)
    if operation == CALENDAR_OPERATION:
        result = await _calendar(token, args)
    else:
        result = await _read_file(token, grant.granted_scopes, "read_by_link" in enabled_bundles(live.config), args)
    if result.get("error_kind") == "google_reauth_required":
        # A 401 only invalidates the cached access token; the next call refreshes. Only a refresh
        # answered with invalid_grant (in the broker) marks the grant as needing re-authorization.
        token_broker(store.db_path).forget(grant.id)
    return result


# --------------------------------------------------------------------------- turn-level notice


_NOTICE = {
    "zh-CN": {
        "google_not_connected": "**需要连接 Google**：本轮有 Google 文件因你尚未连接 Google 账号而未能读取。请到「我的连接」连接 Google 后重新提问。",
        "google_reauth_required": "**需要重新连接 Google**：你的 Google 授权已过期或被撤销，本轮有 Google 文件未能读取。请到「我的连接」重新连接后再试。",
        "google_scope_missing": "**需要更新 Google 授权**：本轮需要的 Google 权限尚未授予。请到「我的连接」更新 Google 授权后再试。",
    },
    "en-US": {
        "google_not_connected": "**Connect Google**: some Google files could not be read in this turn because your Google account is not connected. Connect Google under My connections, then ask again.",
        "google_reauth_required": "**Reconnect Google**: your Google authorization has expired or was revoked, so some Google files could not be read. Reconnect under My connections and try again.",
        "google_scope_missing": "**Update Google access**: this turn needed Google permissions you have not granted yet. Update your Google connection under My connections and try again.",
    },
    "de-DE": {
        "google_not_connected": "**Google verbinden**: Einige Google-Dateien konnten in dieser Gesprächsrunde nicht gelesen werden, weil Ihr Google-Konto nicht verbunden ist. Verbinden Sie Google unter „Meine Verbindungen“ und stellen Sie Ihre Frage erneut.",
        "google_reauth_required": "**Google erneut verbinden**: Ihre Google-Autorisierung ist abgelaufen oder wurde widerrufen. Einige Google-Dateien konnten daher nicht gelesen werden. Verbinden Sie Google unter „Meine Verbindungen“ erneut und versuchen Sie es noch einmal.",
        "google_scope_missing": "**Google-Berechtigungen aktualisieren**: Für diese Gesprächsrunde waren Google-Berechtigungen erforderlich, die Sie noch nicht erteilt haben. Aktualisieren Sie Ihre Google-Verbindung unter „Meine Verbindungen“ und versuchen Sie es erneut.",
    },
    "fr-FR": {
        "google_not_connected": "**Connecter Google** : certains fichiers Google n’ont pas pu être lus pendant cet échange, car votre compte Google n’est pas connecté. Connectez Google dans « Mes connexions », puis posez à nouveau votre question.",
        "google_reauth_required": "**Reconnecter Google** : votre autorisation Google a expiré ou a été révoquée. Certains fichiers Google n’ont donc pas pu être lus. Reconnectez Google dans « Mes connexions », puis réessayez.",
        "google_scope_missing": "**Mettre à jour les autorisations Google** : cet échange nécessitait des autorisations Google que vous n’avez pas encore accordées. Mettez à jour votre connexion Google dans « Mes connexions », puis réessayez.",
    },
}


def google_connection_notice(invocations: Any, lang: str = "en-US") -> str:
    """Deterministic "connect / reconnect Google" paragraph for the Lead's final answer.

    Owed when any Google tool call in this turn (including peers' nested calls) ended
    in a connection problem and no later Google call succeeded. The model's prose is
    not trusted to relay it.
    """
    pending = list(invocations or [])
    kinds: list[str] = []
    succeeded_after = False
    ordered: list[Any] = []
    while pending:
        inv = pending.pop(0)
        ordered.append(inv)
        pending[0:0] = list(getattr(inv, "nested", None) or [])
    for inv in ordered:
        name = str(getattr(inv, "tool_name", "") or "")
        if not name.startswith("teammcp.") or not name.endswith(OPERATIONS):
            continue
        result = getattr(inv, "result", None)
        if not isinstance(result, dict) or result.get("provider") != PROVIDER:
            continue
        kind = str(result.get("error_kind") or "")
        if kind in _CONNECT_ERROR_KINDS:
            kinds.append(kind)
            succeeded_after = False
        elif result.get("ok") is True and kinds:
            succeeded_after = True
    if not kinds or succeeded_after:
        return ""
    from bowhead_core.presentation_locale import resolve_presentation_locale

    table = _NOTICE[resolve_presentation_locale(lang)]
    return table[kinds[-1]]
