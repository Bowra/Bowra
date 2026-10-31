"""Session / Turn HTTP API（M1 最小）.

GET /api/v1/sessions                   → 当前用户的 session 列表（供 sidebar 使用）
GET /api/v1/sessions/{id}              → 单 session + 全部 turns（供 history 回放）
GET /api/v1/sessions/{id}/artifacts    → 本 session 触碰过的 artifact 列表（#100 Data tab）
PATCH /api/v1/sessions/{id}            → owner renames the session (title becomes user-set)
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import unicodedata
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.artifact_references import is_user_intent_pin
from bowhead_core.session_lifecycle import SessionLifecycleError
from bowhead_core.session_store import SessionProtocolManagedError
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from bowhead_gateway.access_context import (
    build_access_context_for_user_from_request,
    has_workspace_access_source,
)
from bowhead_gateway.artifact_streaming import artifact_streaming_response
from bowhead_gateway.bounded_fanout import run_bounded
from bowhead_gateway.dtos import (
    ArtifactMetaDTO,
    ChatExecutionBootstrapDTO,
    PinAttachmentResultDTO,
    PinnedAttachmentListDTO,
    SessionDetailDTO,
    SessionDTO,
    SessionGroupDTO,
    SessionListDTO,
    SessionListItemDTO,
    TraceStepDTO,
    TurnAttachmentDTO,
    TurnDTO,
    TurnSummaryDTO,
)
from bowhead_gateway.media_preview import VIDEO_PREVIEW_MIMES
from bowhead_gateway.request_access_cache import request_with_cached_access
from bowhead_gateway.session_lifecycle import session_lifecycle_http_error
from bowhead_gateway.viewer_time_zone import local_day_start, viewer_time_zone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bowhead_core import SessionStore


router = APIRouter()
logger = logging.getLogger(__name__)


async def _session_chat_execution_bootstrap(
    request: Request,
    session: Any,
) -> ChatExecutionBootstrapDTO:
    """Project advisory write protocol from the authorized persisted Session."""

    from bowhead_gateway.terminal_protocol_rollout import (
        chat_execution_writer_readiness,
        durable_chat_execution_config,
        resolve_chat_execution_admission,
        terminal_v1_attachment_refs_wire_version,
        terminal_v1_web_client_capable,
    )

    config = durable_chat_execution_config()
    readiness = chat_execution_writer_readiness(
        getattr(request.app.state, "terminal_writer_service", None),
        config=config,
    )
    unavailable = ChatExecutionBootstrapDTO(
        write_protocol="unavailable",
        config_generation=config.config_generation,
        readiness_generation=readiness.readiness_generation,
    )
    protocol_store = getattr(
        request.app.state,
        "terminal_protocol_store",
        None,
    )
    user_store = getattr(request.app.state, "user_store", None)
    if protocol_store is None or user_store is None:
        return unavailable
    try:
        team_id = await user_store.get_workspace_team_id(
            session.workspace_id,
        )
        if not team_id:
            return unavailable
        facts = await protocol_store.read_session_protocol_facts(
            team_id=team_id,
            actor=session.user_id,
            workspace_id=session.workspace_id,
            session_id=session.id,
        )
    except Exception as exc:
        from bowhead_core.terminal_protocol import (
            TerminalProtocolFutureVersionError,
            TerminalProtocolMalformedError,
        )

        if isinstance(
            exc,
            (
                TerminalProtocolFutureVersionError,
                TerminalProtocolMalformedError,
            ),
        ):
            from bowhead_gateway.terminal_protocol_http import (
                terminal_protocol_http_error,
            )

            raise terminal_protocol_http_error(exc) from exc
        logger.warning(
            "session.chat_execution_bootstrap_unavailable session=%s error=%s",
            session.id,
            type(exc).__name__,
        )
        return unavailable
    if facts is None:
        return unavailable
    client_capable = terminal_v1_web_client_capable(request.headers)
    if facts.has_v1_history and client_capable:
        ensure_writer = getattr(
            getattr(request.app.state, "terminal_writer_service", None),
            "ensure_writer_ready",
            None,
        )
        if callable(ensure_writer):
            try:
                await ensure_writer()
            except Exception as exc:
                logger.warning(
                    "session.chat_execution_writer_unavailable session=%s error=%s",
                    session.id,
                    type(exc).__name__,
                )
        readiness = chat_execution_writer_readiness(
            getattr(request.app.state, "terminal_writer_service", None),
            config=config,
        )
    decision = resolve_chat_execution_admission(
        config=config,
        team_id=team_id,
        session_facts=facts,
        client_capable=client_capable,
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=readiness.existing_writer_ready,
        fresh_writer_ready=readiness.fresh_writer_ready,
    )
    return ChatExecutionBootstrapDTO(
        write_protocol=decision.write_protocol.value,
        config_generation=config.config_generation,
        readiness_generation=readiness.readiness_generation,
        attachment_refs_wire_version=(terminal_v1_attachment_refs_wire_version()),
        active_turn_id=(facts.v1_active_claim.turn_id if facts.v1_active_claim is not None else None),
        active_execution_generation=(facts.v1_active_claim.generation if facts.v1_active_claim is not None else None),
    )


def _store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session store not configured",
        )
    return store


def _user_id(request: Request) -> str:
    """P4.5.17: JWT > X-Bowhead-User-Id header > dev_default_user."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


def _intent_state(intents: list[str]) -> str:
    """把 intent 列表映射为前端 session dot 的 state."""
    if "modeling" in intents:
        return "pending"
    return "done"


def _coded_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code}},
    )


async def _require_workspace_access(
    request: Request,
    *,
    user_id: str,
    workspace_id: str,
    operation: SpaceOperation,
) -> None:
    """STRONG 闸的请求态包装: 成员校验 + 生命周期, 全部由 `build_access_context` 判。

    ★ 旧名 `_require_workspace_lifecycle` 与只查生命周期的弱闸
    `require_space_lifecycle_without_authorization` 只差一个词, 按名字挑闸必挑错 ——
    这正是空间隔离 sprint 要治的病, 故统一改叫 `_require_workspace_access`。
    """

    from bowhead_gateway.routes.auth import _dev_user_fallback_enabled

    if _dev_user_fallback_enabled(request):
        return
    await build_access_context_for_user_from_request(
        request,
        user_id=user_id,
        workspace_id=workspace_id,
        operation=operation,
    )


_SESSION_LIST_AUTH_CONCURRENCY = 8
#: Pinned sessions are injected into the first page regardless of age; bounded like the page size.
_SESSION_LIST_PIN_LIMIT = 100
_SESSION_CURSOR_MAX_LEN = 4096
_SESSION_CURSOR_MAX_STR = 512


async def readable_session_workspaces(
    request: Request,
    *,
    user_id: str,
    workspace_ids: Iterable[str],
) -> set[str]:
    """Return the Spaces whose session history ``user_id`` may read.

    Same predicate as the per-row filter below (``READ_HISTORY`` through
    ``_require_workspace_access``; 403/404 hide the Space, anything else
    propagates). Each Space is checked once, concurrently with bounded
    parallelism, so callers can filter in SQL instead of after ``LIMIT``.
    """

    unique = sorted({str(workspace_id) for workspace_id in workspace_ids if workspace_id})
    readable: set[str] = set()
    if not unique:
        return readable
    if len(unique) > 1:
        # One request-level snapshot of the caller's memberships and Team Spaces
        # serves every check (same decisions, fewer database openings per poll).
        request = await cached_access_request(request, user_id=user_id)

    async def _check(workspace_id: str) -> None:
        try:
            await _require_workspace_access(
                request,
                user_id=user_id,
                workspace_id=workspace_id,
                operation=SpaceOperation.READ_HISTORY,
            )
        except HTTPException as exc:
            if exc.status_code not in {
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
            }:
                raise
            return
        readable.add(workspace_id)

    await run_bounded(unique, _check, limit=_SESSION_LIST_AUTH_CONCURRENCY)
    return readable


async def cached_access_request(request: Request, *, user_id: str) -> Any:
    """``request`` whose access lookups are cached for this request (idempotent)."""

    if not has_workspace_access_source(request):
        return request
    user = await request.app.state.user_store.get_user(user_id)
    return request_with_cached_access(request, team_id=getattr(user, "team_id", None))


async def _filter_readable_sessions(
    sessions: list[Any],
    *,
    request: Request,
    user_id: str,
) -> list[Any]:
    """Hide sessions whose persisted Space is no longer readable."""

    readable = await readable_session_workspaces(
        request,
        user_id=user_id,
        workspace_ids=(str(session.workspace_id) for session in sessions),
    )
    return [session for session in sessions if str(session.workspace_id) in readable]


def _session_list_filter_key(*, view: str, workspace_id: str | None, q: str | None) -> str:
    """Short digest of the filters a cursor was issued under; a cursor never crosses filters."""
    raw = json.dumps([view, workspace_id or "", (q or "").strip()], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _session_key(session: Any, view: str) -> tuple[str, str]:
    sort_at = session.archived_at if view == "archived" else session.last_active_at
    return (str(sort_at or ""), str(session.id))


def _encode_session_cursor(
    *,
    filter_key: str,
    after: tuple[str, str],
    skip: tuple[tuple[str, str], tuple[str, str]] | None,
) -> str:
    payload: dict[str, Any] = {"f": filter_key, "k": list(after)}
    if skip is not None:
        payload["x"] = [list(skip[0]), list(skip[1])]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _valid_session_key(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(isinstance(part, str) and 0 < len(part) <= _SESSION_CURSOR_MAX_STR for part in value)


def _decode_session_cursor(
    cursor: str,
    filter_key: str,
) -> tuple[tuple[str, str], tuple[tuple[str, str], tuple[str, str]] | None]:
    """Return ``(after, skip)``; malformed or cross-filter cursors are a 400, never a restarted list."""
    try:
        if len(cursor) > _SESSION_CURSOR_MAX_LEN:
            raise ValueError("cursor too long")
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        after = payload["k"]
        skip = payload.get("x")
        valid = (
            isinstance(payload, dict)
            and payload["f"] == filter_key
            and _valid_session_key(after)
            and (skip is None or (isinstance(skip, list) and len(skip) == 2 and all(_valid_session_key(part) for part in skip)))
        )
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        valid = False
    if not valid:
        raise _coded_error(status.HTTP_400_BAD_REQUEST, "invalid_session_cursor")
    skip_range = None if skip is None else ((skip[0][0], skip[0][1]), (skip[1][0], skip[1][1]))
    return (after[0], after[1]), skip_range


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@router.get("", response_model=SessionListDTO)
async def list_sessions(
    request: Request,
    view: Literal["active", "archived"] = Query(default="active"),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=_SESSION_CURSOR_MAX_LEN),
    store: SessionStore = Depends(_store),
) -> SessionListDTO:
    """Sidebar session list, grouped server-side.

    ``q`` matches the title, the id, or any of the user's own messages in the
    session. Rows from Spaces the caller can no longer read are excluded in
    SQL, so a page is never shortened after ``LIMIT``.

    Pagination: the first page (no ``cursor``) keeps the long-standing shape —
    up to ``limit`` sessions from the last seven days plus up to ``limit``
    older ones, and every pinned active session regardless of age (active
    view without ``q``). ``next_cursor`` (``has_more`` mirrors it) continues
    the same newest-first stream; cursor pages return the same group ids and
    clients merge items by group id, deduplicating by session id, and keep the
    first page's ``archived_count`` (cursor pages return null). A cursor is
    bound to ``view``/``workspace_id``/``q``; any other or malformed cursor is a
    400 ``invalid_session_cursor``.
    """
    uid = _user_id(request)
    workspace = request.query_params.get("workspace_id")
    normalized_q = (q or "").strip() or None
    filter_key = _session_list_filter_key(view=view, workspace_id=workspace, q=normalized_q)
    after, skip = _decode_session_cursor(cursor, filter_key) if cursor else (None, None)

    if workspace:
        await _require_workspace_access(
            request,
            user_id=uid,
            workspace_id=workspace,
            operation=SpaceOperation.READ_HISTORY,
        )
        readable: list[str] = [workspace]
    else:
        candidates = await store.list_session_workspace_ids(user_id=uid)
        readable = sorted(await readable_session_workspaces(request, user_id=uid, workspace_ids=candidates))

    # Counted on the first page only (cursor pages return null; clients keep the
    # first-page value), over the same readable Spaces as the rows.
    archived_count = (
        None
        if after is not None
        else await store.count_archived_sessions(
            user_id=uid,
            workspace_id=workspace,
            workspace_ids=readable,
        )
    )
    base: dict[str, Any] = {
        "user_id": uid,
        "workspace_id": workspace,
        "q": normalized_q,
        "workspace_ids": readable,
    }

    # Group boundaries are the viewer's calendar days (``X-Bowhead-Time-Zone``, UTC
    # when absent or unknown). Cursor pages never use week_start (their skip range
    # lists the rows the first page sent), so a cursor stays valid across zones.
    zone = viewer_time_zone(request)
    now = datetime.now(tz=UTC)
    today_start = local_day_start(now, zone)
    yest_start = local_day_start(now, zone, days_back=1)
    week_start = local_day_start(now, zone, days_back=7)

    def _to_item(s: Any) -> SessionListItemDTO:
        # W12-3 actual signals:
        # - unread = last_seen_at IS NULL OR last_active_at > last_seen_at
        # - archived = explicit user_session_archives projection
        # - skill_applied = sessions.skill_applied=1 (marked during turn finalization).
        last = _parse_ts(s.last_active_at)
        unread = True
        if s.last_seen_at:
            with contextlib.suppress(ValueError, AttributeError):
                unread = last > _parse_ts(s.last_seen_at)
        return SessionListItemDTO(
            id=s.id,
            title=s.title or s.id,
            intents=s.intents,
            time=_relative_time(last, now, zone),
            updated_at=_iso_instant(last),
            state="active" if s.active_turn_id is not None else _intent_state(s.intents),
            unread=unread,
            archived=bool(s.archived_at),
            skill_applied="applied" if s.skill_applied else None,
            pinned=bool(s.pinned_at) if s.pinned_at else None,
        )

    def _bucket(s: Any) -> str:
        last = _parse_ts(s.last_active_at)
        if last >= today_start:
            return "today"
        if last >= yest_start:
            return "yest"
        if last >= week_start:
            return "last7"
        return "older"

    def _grouped(rows: list[Any]) -> list[SessionGroupDTO]:
        buckets: dict[str, list[SessionListItemDTO]] = {"today": [], "yest": [], "last7": [], "older": []}
        for row in rows:
            buckets[_bucket(row)].append(_to_item(row))
        return [
            SessionGroupDTO(id="today", label="TODAY", items=buckets["today"]),
            SessionGroupDTO(id="yest", label="YESTERDAY", items=buckets["yest"]),
            SessionGroupDTO(id="last7", label="LAST 7 DAYS", items=buckets["last7"]),
            SessionGroupDTO(id="older", label="OLDER", items=buckets["older"]),
        ]

    def _page(groups: list[SessionGroupDTO], next_cursor: str | None) -> SessionListDTO:
        return SessionListDTO(
            groups=groups,
            archived_count=archived_count,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    if view == "archived":
        rows = await store.list_sessions(**base, archive_view="archived", limit=limit + 1, after=after)
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _encode_session_cursor(filter_key=filter_key, after=_session_key(rows[-1], view), skip=None)
        return _page(
            [SessionGroupDTO(id="archived", label="ARCHIVED", items=[_to_item(row) for row in rows])],
            next_cursor,
        )

    if after is not None:
        # Cursor page: the plain newest-first stream after the cursor, minus the older
        # rows the first page already sent out of stream order.
        rows = await store.list_sessions(**base, limit=limit + 1, after=after, skip=skip)
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _encode_session_cursor(filter_key=filter_key, after=_session_key(rows[-1], view), skip=skip)
        return _page(_grouped(rows), next_cursor)

    # First page. sidebar-older-bucket (2026-07-20): the older group has its own window
    # so the last seven days cannot crowd it out. Both windows are prefixes of the same
    # stream split at week_start, so one cursor continues both.
    # Stored timestamps are UTC ISO strings compared as text, so the bound must be UTC too.
    week_bound = week_start.isoformat(timespec="seconds")
    recent = await store.list_sessions(**base, limit=limit + 1, since=week_bound)
    older = await store.list_sessions(**base, limit=limit + 1, before=week_bound)
    next_cursor = None
    if len(recent) > limit:
        recent = recent[:limit]
        older = older[:limit]
        older_range = (_session_key(older[0], view), _session_key(older[-1], view)) if older else None
        next_cursor = _encode_session_cursor(filter_key=filter_key, after=_session_key(recent[-1], view), skip=older_range)
    elif len(older) > limit:
        older = older[:limit]
        next_cursor = _encode_session_cursor(filter_key=filter_key, after=_session_key(older[-1], view), skip=None)
    rows = [*recent, *older]
    if normalized_q is None:
        # Pinned sessions stay reachable on the first page whatever their age; clients
        # show them in the pinned section and drop duplicates that later pages repeat.
        shown = {row.id for row in rows}
        pinned = await store.list_sessions(**base, limit=_SESSION_LIST_PIN_LIMIT, pinned_only=True)
        rows.extend(row for row in pinned if row.id not in shown)
        rows.sort(key=lambda row: _session_key(row, view), reverse=True)
    return _page(_grouped(rows), next_cursor)


@router.post("/{session_id}/seen", status_code=status.HTTP_204_NO_CONTENT)
async def mark_session_seen(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """W12-3 (#74): 前端 hydrate session 时调,把 (user_id, session_id) 标已 seen.
    list_sessions 用 last_seen_at vs last_active_at 算 unread.
    """
    uid = _user_id(request)
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
    )
    await store.set_session_seen(user_id=uid, session_id=session_id)


@router.post("/{session_id}/pin", status_code=status.HTTP_204_NO_CONTENT)
async def pin_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """W12-3b (#87): 用户 pin 该 session, idempotent."""
    uid = _user_id(request)
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
    )
    await store.pin_session(user_id=uid, session_id=session_id)


@router.delete("/{session_id}/pin", status_code=status.HTTP_204_NO_CONTENT)
async def unpin_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """W12-3b (#87): 取消 pin, idempotent."""
    uid = _user_id(request)
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
    )
    await store.unpin_session(user_id=uid, session_id=session_id)


@router.post("/{session_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """Archive one owned idle session. Existence is intentionally concealed."""
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
        conceal=True,
    )
    try:
        result = await store.archive_session(
            user_id=_user_id(request),
            session_id=session_id,
        )
    except Exception as exc:
        logger.exception(
            "session.archive_failed session=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        raise _coded_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "session_archive_failed",
        ) from exc
    if result == "not_found":
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
    if result == "active":
        raise _coded_error(
            status.HTTP_409_CONFLICT,
            "session_archive_active_turn",
        )


@router.delete("/{session_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def restore_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """Restore one owned session. Repeated restores are idempotent."""
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
        conceal=True,
    )
    try:
        result = await store.restore_session(
            user_id=_user_id(request),
            session_id=session_id,
        )
    except Exception as exc:
        logger.exception(
            "session.restore_failed session=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        raise _coded_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "session_restore_failed",
        ) from exc
    if result == "not_found":
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """Soft-delete one owned idle session, retaining history and supporting Undo."""
    session = await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
        conceal=True,
    )
    if session.deleted_at is not None:
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
    lifecycle = getattr(request.app.state, "session_lifecycle_coordinator", None)
    try:
        if lifecycle is not None:
            await lifecycle.soft_delete(
                session_id=session_id,
                actor_audit_ref=_user_id(request),
                request_id=await lifecycle.visibility_request_id(session_id=session_id, user_id=_user_id(request), action="soft_delete"),
                expected_user_id=_user_id(request),
                require_idle=True,
            )
            return
        if getattr(request.app.state, "legacy_session_lifecycle_allowed", None) is not True:
            raise _coded_error(status.HTTP_503_SERVICE_UNAVAILABLE, "terminal_protocol_writer_not_ready")
        result = await store.delete_session(
            user_id=_user_id(request),
            session_id=session_id,
        )
    except SessionLifecycleError as exc:
        raise session_lifecycle_http_error(exc) from exc
    except SessionProtocolManagedError as exc:
        raise _coded_error(status.HTTP_503_SERVICE_UNAVAILABLE, "terminal_protocol_writer_not_ready") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "session.delete_failed session=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        raise _coded_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "session_delete_failed",
        ) from exc
    if result == "not_found":
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
    if result == "active":
        raise _coded_error(
            status.HTTP_409_CONFLICT,
            "session_delete_active_turn",
        )


#: Upper bound for an owner-chosen session title, in characters after trimming.
SESSION_TITLE_MAX_CHARS = 100


#: Unicode categories never allowed in a session title.
_REJECTED_TITLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
#: Format characters real text needs: ZWNJ (Persian, Indic scripts) and ZWJ (emoji sequences such as
#: U+1F469 U+200D U+1F4BB). Bidi controls and other Cf characters stay rejected.
_ALLOWED_TITLE_FORMAT_CHARS = frozenset({"\u200c", "\u200d"})
#: Invisible on their own, so a title made only of them is empty: every Cf character plus the
#: emoji variation selectors (U+FE0E, U+FE0F; category Mn, allowed inside a title).
_INVISIBLE_TITLE_CHARS = frozenset({"\ufe0e", "\ufe0f"})


class SessionRenameBody(BaseModel):
    title: str


def _normalized_session_title(raw: str) -> str:
    """Trim and validate an owner-chosen title; raise a coded 422 when it is unusable."""

    title = raw.strip()
    # A title made only of whitespace and invisible characters (including the allowed joiners) reads as empty.
    visible = "".join(char for char in title if unicodedata.category(char) != "Cf" and char not in _INVISIBLE_TITLE_CHARS)
    if not visible.strip():
        raise _coded_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "session_title_empty")
    if len(title) > SESSION_TITLE_MAX_CHARS:
        raise _coded_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "session_title_too_long")
    # Reject C0/C1 controls (Cc: newline, tab, NUL), line/paragraph separators (Zl, Zp)
    # and invisible format characters (Cf: bidi marks/overrides/isolates U+200E/200F, U+061C,
    # U+202A-202E, U+2066-2069, and U+200B, U+2060, U+FEFF), except the ZWJ/ZWNJ joiners. They break the single-line title or let it
    # display differently from what is stored, in every list and top bar.
    if any(unicodedata.category(char) in _REJECTED_TITLE_CATEGORIES and char not in _ALLOWED_TITLE_FORMAT_CHARS for char in title):
        raise _coded_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "session_title_invalid")
    return title


@router.patch("/{session_id}", response_model=SessionDTO)
async def rename_session(
    session_id: str,
    body: SessionRenameBody,
    request: Request,
    store: SessionStore = Depends(_store),
) -> SessionDTO:
    """Rename one owned session. The title is marked user-set, so automatic title
    generation never overwrites it afterwards. Existence is concealed like archive."""
    title = _normalized_session_title(body.title)
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
        conceal=True,
    )
    try:
        session = await store.rename_session(
            user_id=_user_id(request),
            session_id=session_id,
            title=title,
        )
    except Exception as exc:
        logger.exception(
            "session.rename_failed session=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        raise _coded_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "session_rename_failed",
        ) from exc
    if session is None:
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
    return SessionDTO(
        id=session.id,
        workspace_id=session.workspace_id,
        title=session.title,
        started_at=session.started_at,
        last_active_at=session.last_active_at,
        status=session.status,
        cost_usd=session.cost_usd,
        tokens_total=session.tokens_total,
        intents=session.intents,
    )


@router.post("/{session_id}/undelete", status_code=status.HTTP_204_NO_CONTENT)
async def undelete_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """Undo an owned session's soft deletion without reopening historical executions."""
    session = await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
        conceal=True,
    )
    if session.deleted_at is None:
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
    lifecycle = getattr(request.app.state, "session_lifecycle_coordinator", None)
    try:
        if lifecycle is not None:
            result = await lifecycle.revive(
                session_id=session_id,
                actor_audit_ref=_user_id(request),
                request_id=await lifecycle.visibility_request_id(session_id=session_id, user_id=_user_id(request), action="revive"),
                expected_user_id=_user_id(request),
            )
            revived = result.changed
        else:
            if getattr(request.app.state, "legacy_session_lifecycle_allowed", None) is not True:
                raise _coded_error(status.HTTP_503_SERVICE_UNAVAILABLE, "terminal_protocol_writer_not_ready")
            revived = await store.undelete_session(user_id=_user_id(request), session_id=session_id)
    except SessionLifecycleError as exc:
        raise session_lifecycle_http_error(exc) from exc
    except SessionProtocolManagedError as exc:
        raise _coded_error(status.HTTP_503_SERVICE_UNAVAILABLE, "terminal_protocol_writer_not_ready") from exc
    if not revived:
        raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")


@router.get("/turn-completions")
async def list_turn_completions(
    request: Request,
    after: str | None = Query(default=None, max_length=64),
    store: SessionStore = Depends(_store),
) -> dict[str, Any]:
    """Turns of the caller's sessions that ended after the cursor (agent run limits, phase 3).

    The client polls this to show a completion popup when a turn ends while the user is
    elsewhere. Without ``after`` it returns no items and the current cursor, so only turns
    ending from now on are reported. Sessions in Spaces the caller can no longer read are
    left out.
    """
    from datetime import UTC, datetime

    uid = _user_id(request)
    now = datetime.now(tz=UTC).isoformat()
    if not after:
        return {"items": [], "cursor": now}
    items = await store.list_turn_completions(user_id=uid, after=after)
    if items:
        readable = set(await readable_session_workspaces(request, user_id=uid, workspace_ids=sorted({i["workspace_id"] for i in items})))
        cursor = items[-1]["ended_at"]
        items = [i for i in items if i["workspace_id"] in readable]
    else:
        cursor = after
    return {"items": items, "cursor": cursor}


@router.get("/{session_id}", response_model=SessionDetailDTO)
async def get_session(
    session_id: str,
    request: Request,
    response: Response,
    store: SessionStore = Depends(_store),
) -> SessionDetailDTO:
    response.headers["Cache-Control"] = "no-store, private"
    session = await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.READ_HISTORY,
    )

    turns = await store.list_turns(session_id=session_id, include_maintenance=True)
    art_store = getattr(request.app.state, "artifact_store", None)

    # chat-hydrate-overhaul: batch-fetch traces + summaries to avoid N+1.
    turn_ids = [t.id for t in turns]
    try:
        trace_by_turn = await store.list_trace_steps_for_turns(turn_ids)
    except Exception:
        trace_by_turn = {}
    try:
        summary_by_turn = await store.get_turn_summaries(turn_ids)
    except Exception:
        summary_by_turn = {}
    # Chat turn feedback D3 (design §8 Q1): an old failed turn persisted a system failure
    # sentence as its answer. Omit it so the failure card is the only failure statement after
    # refresh too; new failed turns persist an empty answer.
    try:
        legacy_failure_answers = await store.list_legacy_failure_answer_turn_ids([t.id for t in turns if t.status == "failed"])
    except Exception:
        legacy_failure_answers = set()

    turns_out: list[TurnDTO] = []
    for t in turns:
        attachments: list[TurnAttachmentDTO] = []
        try:
            canonical_source = await store.get_chat_turn_user_source(
                turn_id=t.id,
                user_id=session.user_id,
                workspace_id=session.workspace_id,
            )
        except Exception:
            canonical_source = None
        if canonical_source is not None and canonical_source.attachment_refs:
            attachments = [
                TurnAttachmentDTO(
                    file_id=str(ref["fileId"]),
                    filename=str(ref["filename"]),
                    mime=str(ref["mime"]),
                    size=int(ref["size"]),
                    reference_kind=("result" if ref.get("referenceKind") == "result" else None),
                    selected_context=ref.get("selectedContext"),
                    bundle_artifact_ids=(
                        [str(item) for item in ref["bundleArtifactIds"]] if isinstance(ref.get("bundleArtifactIds"), list) else None
                    ),
                )
                for ref in canonical_source.attachment_refs
            ]
        else:
            try:
                aids = await store.list_turn_attachments(turn_id=t.id)
            except Exception:
                aids = []
        if not attachments and aids and art_store is not None:
            for aid in aids:
                art = await art_store.get(aid)
                if art is None:
                    continue
                attachments.append(
                    TurnAttachmentDTO(
                        file_id=art.id,
                        filename=art.filename,
                        mime=art.mime,
                        size=art.size,
                    )
                )

        trace_rows = trace_by_turn.get(t.id, [])
        summary_row = summary_by_turn.get(t.id)

        turns_out.append(
            TurnDTO(
                operation_kind=t.operation_kind,
                id=t.id,
                seq=t.seq,
                user_text=(canonical_source.user_text if canonical_source is not None else t.user_text),
                intent=t.intent,
                status=t.status,
                execution_generation=t.execution_generation,
                terminal_protocol_version=t.terminal_protocol_version,
                error_kind=getattr(t, "error_kind", None),  # 0708 ⑩
                started_at=t.started_at,
                ended_at=t.ended_at,
                final_answer=("" if t.id in legacy_failure_answers else t.final_answer),
                agent_type=t.agent_type,
                attachments=attachments,
                trace_steps=[
                    TraceStepDTO(
                        id=r.step_id,
                        kind=r.kind,
                        title=r.title,
                        tool_name=r.tool_name,
                        duration_ms=r.duration_ms,
                        summary=r.summary,
                        subagent_id=r.subagent_id,
                    )
                    for r in trace_rows
                ],
                summary=TurnSummaryDTO(
                    total_ms=summary_row.total_ms,
                    cost_text=summary_row.cost_text,
                    tools_count=summary_row.tools_count,
                    llm_count=summary_row.llm_count,
                    tokens_input=summary_row.tokens_input,
                    tokens_output=summary_row.tokens_output,
                    usage_partial=summary_row.usage_partial,
                )
                if summary_row
                else None,
            )
        )

    return SessionDetailDTO(
        context_generation=await store.context_conversation_generation(session_id=session_id),
        session=SessionDTO(
            id=session.id,
            workspace_id=session.workspace_id,
            title=session.title,
            started_at=session.started_at,
            last_active_at=session.last_active_at,
            status=session.status,
            cost_usd=session.cost_usd,
            tokens_total=session.tokens_total,
            intents=session.intents,
        ),
        turns=turns_out,
        chat_execution=await _session_chat_execution_bootstrap(
            request,
            session,
        ),
    )


# ─────────────────────────────────────────────────────────────
# T1 · Session-level attachment pinning
# Uploaded attachments are pinned automatically; later turns render lightweight stubs into the Lead system prompt.
# Users can list/unpin attachments or manually pin existing ones.
# ─────────────────────────────────────────────────────────────


def _artifact_store(request: Request) -> Any:
    return getattr(request.app.state, "artifact_store", None)


async def _assert_session_access(
    session_id: str,
    request: Request,
    store: SessionStore,
    *,
    operation: SpaceOperation,
    conceal: bool = False,
) -> Any:
    sess = await store.get_session(session_id)
    if sess is None:
        if conceal:
            raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    if sess.user_id != _user_id(request):
        if conceal:
            raise _coded_error(status.HTTP_404_NOT_FOUND, "session_not_found")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your session")
    await _require_workspace_access(
        request,
        user_id=sess.user_id,
        workspace_id=sess.workspace_id,
        operation=operation,
    )
    return sess


def _is_user_composer_pin(art: Any) -> bool:
    """D16: only user-intent pins (inject='full' = user_upload class) belong in the composer
    pin strip. Tool artifacts (bi_query CSV → inject 'list', sandbox → 'stub') auto-pin to
    session_attachments for the Assets tab but must not flood the composer strip.

    判据本体在 `bowhead_core.artifact_references`——**同一条判据另一处在保活侧**
    （TTL 判「用户是不是自己 pin 了这份东西」）。两处必须同真同假：这边放行、那边不认，
    就会出现「composer 上挂着的附件被 GC 收走」。
    """
    return is_user_intent_pin(art)


@router.get("/{session_id}/attachments", response_model=PinnedAttachmentListDTO)
async def list_session_pinned_attachments(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> PinnedAttachmentListDTO:
    """列出 session 里当前 pinned 的附件（附带 metadata 供 UI 渲染）."""
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.READ_HISTORY,
    )
    aids = await store.list_pinned_attachments(session_id=session_id)
    art_store = _artifact_store(request)
    items: list[ArtifactMetaDTO] = []
    for aid in aids:
        art = None
        if art_store is not None:
            try:
                art = await art_store.get(aid)
            except Exception:
                art = None
        if art is None:
            # Stale pin (soft-deleted/missing artifact): skip instead of letting FE render broken entries.
            continue
        if not _is_user_composer_pin(art):
            # D16: tool artifact auto-pinned for the Assets tab (separate query) — keep it
            # out of the composer pin strip so 9 bi_query CSVs don't flood it.
            continue
        items.append(
            ArtifactMetaDTO(
                id=art.id,
                filename=art.filename,
                mime=art.mime,
                size=art.size,
            )
        )
    return PinnedAttachmentListDTO(items=items)


class PinRequest(BaseModel):
    artifact_id: str


@router.post(
    "/{session_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=PinAttachmentResultDTO,
)
async def pin_session_attachment(
    session_id: str,
    body: PinRequest,
    request: Request,
    store: SessionStore = Depends(_store),
) -> PinAttachmentResultDTO:
    """手动 pin 一个已存在的附件（补录 / 跨 session 引用预留接口）."""
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
    )
    art_store = _artifact_store(request)
    if art_store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "artifact store not configured")
    art = await art_store.get(body.artifact_id)
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    if getattr(art, "created_by", None) and art.created_by != _user_id(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your artifact")
    await store.pin_session_attachments(
        session_id=session_id,
        artifact_ids=[body.artifact_id],
    )
    return PinAttachmentResultDTO(ok=True, artifact_id=body.artifact_id)


@router.delete(
    "/{session_id}/attachments/{artifact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unpin_session_attachment(
    session_id: str,
    artifact_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
) -> None:
    """取消 pin —— 后续 turn 不再注入 stub；artifact 本身不动."""
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.WRITE,
    )
    ok = await store.unpin_session_attachment(
        session_id=session_id,
        artifact_id=artifact_id,
    )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment not pinned in this session")


# ─── #100 Data tab: session-scoped artifact list ─────────────
class SessionArtifactItemDTO(BaseModel):
    id: str
    filename: str
    mime: str
    size: int
    source: str
    created_at: str
    preview_available: bool
    # Sprint pptx-from-template M1: asset-model fields the Assets panel keys on.
    # `kind` drives the FE badge (DECK/WEB/UP/DATA/...); `origin` the localized
    # label; `trust` the "unverified" styling for web_clips.
    kind: str = "tool_data"
    origin: str = "upload"
    trust: str = "trusted"
    created_by_turn: str | None = None
    producer_kind: str | None = None
    derived_from: str | None = None
    manifest_artifact_id: str | None = None


class SessionArtifactListDTO(BaseModel):
    items: list[SessionArtifactItemDTO]


@router.get("/{session_id}/artifacts/{artifact_id}/content")
async def get_session_artifact_content(
    session_id: str,
    artifact_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
):  # type: ignore[no-untyped-def]
    """Stream owner-authorized artifact bytes without an object-store redirect.

    Browser renderers for HTML/PDF/DOCX must read response bytes. The regular
    file download endpoint may redirect to a cross-origin presigned URL, which
    is correct for downloads but unusable for authenticated ``fetch()`` under
    object-store CORS. This endpoint keeps the session and artifact checks on
    the application origin and never exposes a presigned URL.
    """
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.READ_HISTORY,
        conceal=True,
    )
    artifact_store = _artifact_store(request)
    if artifact_store is None:
        raise _coded_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "file_service_unavailable",
        )
    user_id = _user_id(request)
    visible = await artifact_store.list_for_session(
        session_id=session_id,
        user_id=user_id,
        artifact_id=artifact_id,
        limit=1,
        results_visible_only=True,
    )
    if not visible:
        raise _coded_error(status.HTTP_404_NOT_FOUND, "file_not_found")
    art = visible[0]

    from bowhead_gateway.routes.files import _require_artifact_operation

    try:
        await _require_artifact_operation(
            request,
            artifact_store,
            art,
            operation=SpaceOperation.READ_HISTORY,
            user_id=user_id,
        )
    except HTTPException as exc:
        if exc.status_code in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        }:
            raise _coded_error(status.HTTP_404_NOT_FOUND, "file_not_found") from exc
        raise
    return await artifact_streaming_response(
        request,
        artifact_store,
        art,
        inline=False,
    )


_PREVIEWABLE_MIME_PREFIXES = (
    "text/",
    "image/",
    "application/json",
)
_PREVIEWABLE_MIMES = {
    "application/x-ndjson",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
} | VIDEO_PREVIEW_MIMES


def _can_preview(mime: str) -> bool:
    if any(mime.startswith(p) for p in _PREVIEWABLE_MIME_PREFIXES):
        return True
    return mime in _PREVIEWABLE_MIMES


# file-management-v2 P0 consolidation: artifact_origin.py is the sole source/kind→origin implementation.
# Removed this file's manual copy, which mislabeled AI-generated backgrounds as uploaded in Data-tab origin badges.
from bowhead_gateway.artifact_origin import origin_for as _origin_for_kind_source


@router.get("/{session_id}/artifacts", response_model=SessionArtifactListDTO)
async def list_session_artifacts(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_store),
    q: str | None = Query(default=None, description="文件名 substring 过滤"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> SessionArtifactListDTO:
    """#100: 本 session 所有触碰过的 artifact（用户上传 + session pin + canvas 产物）."""
    await _assert_session_access(
        session_id,
        request,
        store,
        operation=SpaceOperation.READ_HISTORY,
    )
    art_store = _artifact_store(request)
    if art_store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "artifact store not configured")
    user_id = _user_id(request)
    try:
        items = await art_store.list_for_session(
            session_id=session_id,
            user_id=user_id,
            q=q,
            limit=limit,
            offset=offset,
            results_visible_only=True,
        )
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"list failed: {e}") from e
    from bowhead_gateway.routes.files import _export_trust_projection

    trust_by_id = await _export_trust_projection(request, art_store, list(items))
    return SessionArtifactListDTO(
        items=[
            SessionArtifactItemDTO(
                id=art.id,
                filename=art.filename,
                mime=art.mime,
                size=art.size,
                source=art.source,
                created_at=art.created_at,
                preview_available=_can_preview(art.mime),
                kind=getattr(art, "kind", None) or "tool_data",
                origin=_origin_for_kind_source(
                    getattr(art, "kind", None),
                    art.source,
                    producer_kind=getattr(art, "producer_kind", None),
                ),
                trust=trust_by_id.get(art.id) or getattr(art, "trust", None) or "trusted",
                created_by_turn=getattr(art, "created_by_turn", None),
                producer_kind=getattr(art, "producer_kind", None),
                derived_from=getattr(art, "derived_from", None),
                manifest_artifact_id=getattr(art, "manifest_artifact_id", None),
            )
            for art in items
        ]
    )


def _iso_instant(value: datetime) -> str:
    """UTC ISO 8601 with an explicit offset; legacy naive timestamps are stored as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _relative_time(past: datetime, now: datetime, zone: tzinfo = UTC) -> str:
    delta = now - past
    sec = int(delta.total_seconds())
    if sec < 60:
        return "just now"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    if sec < 7 * 86400:
        return f"{sec // 86400}d ago"
    return past.astimezone(zone).strftime("%Y-%m-%d")
