"""Permission-preserving cross-object search for the Option A sidebar."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.kb.schemas import KbSearchRequest
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from bowhead_gateway.request_access_cache import request_with_cached_access
from bowhead_gateway.routes import canvas as canvas_routes
from bowhead_gateway.routes import files as files_routes
from bowhead_gateway.routes import kb as kb_routes
from bowhead_gateway.routes import sessions as sessions_routes
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/search", tags=["search"])

SearchKind = Literal["session", "report", "file", "knowledge"]
_SOURCE_TIMEOUT_SECONDS = 4.0
_KNOWLEDGE_SEMANTIC_TIMEOUT_SECONDS = 3.0
_KNOWLEDGE_METADATA_TIMEOUT_SECONDS = 3.0

#: Reports/files each initially fetch limit rows and filter **individually** by space readability;
#: if fewer than limit remain (newer hidden matches rank first), widen ×4 and refetch until enough, exhausted, or _VISIBLE_MAX_FETCH.
#: If still insufficient at the cap, return fewer (prefer fewer results over bypassing access).
#: **No** initial overfetch: most users have no hidden matches, and every extra row costs an access check (116 measurement: admins' ×4 initial file fetch
#: hit the 4s source timeout, dropping the whole group). Cost: users with hidden matches need at least another query per source (limit=5: 5 → 20 → 80 → 100).
#: Multiplier 4 and cap 100 are arbitrary choices; the cap bounds worst cases. Timeouts drop entire groups, worse than fewer results.
_VISIBLE_OVERFETCH = 4
_VISIBLE_MAX_FETCH = 100


async def _fetch_visible(
    fetch: Callable[[int], Awaitable[list[Any]]],
    keep: Callable[[list[Any]], Awaitable[list[Any]]],
    limit: int,
) -> list[Any]:
    """取「最新的 ``limit`` 条可读行」。

    ``fetch(n)`` 返回最新在前的至多 ``n`` 行；``keep(rows)`` 保序地滤掉不可读的。
    ``len(rows) < window`` 即已取尽（store 没有更多行了）。
    """
    window = min(limit, _VISIBLE_MAX_FETCH)
    while True:
        rows = await fetch(window)
        visible = await keep(rows)
        if len(visible) >= limit or len(rows) < window or window >= _VISIBLE_MAX_FETCH:
            return visible[:limit]
        window = min(window * _VISIBLE_OVERFETCH, _VISIBLE_MAX_FETCH)


class UnifiedSearchItemDTO(BaseModel):
    kind: SearchKind
    id: str
    title: str
    session_id: str | None = None
    snippet: str | None = None
    updated_at: str | None = None
    # Reports only: creation time. An untitled report has an empty ``title``; clients
    # build the same timestamped fallback title the report list shows from this value
    # (it must render in the viewer's time zone, so the server cannot format it).
    created_at: str | None = None
    # Sessions only: whether the hit came from the title or from one of the user's own
    # messages (``snippet`` then quotes that message).
    match: Literal["title", "content"] | None = None


class UnifiedSearchGroupDTO(BaseModel):
    kind: SearchKind
    unavailable: bool = False
    # The source hit its time budget (a subset of ``unavailable``): its empty ``items``
    # mean "not searched in time", never "no matches".
    timed_out: bool = False
    items: list[UnifiedSearchItemDTO]
    # Sessions only: every readable match (not just this page), and the cursor for the
    # next page of the same query. Other sources leave both null.
    total: int | None = None
    next_cursor: str | None = None


@dataclass(frozen=True)
class _SourcePage:
    items: list[UnifiedSearchItemDTO]
    total: int | None = None
    next_cursor: str | None = None


class UnifiedSearchResponseDTO(BaseModel):
    query: str
    partial: bool
    groups: list[UnifiedSearchGroupDTO]


class UnifiedSearchRequestDTO(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=5, ge=1, le=20)
    # Restrict the search to these sources (default: all). A ``cursor`` continues the
    # session group only, so it requires ``kinds == ["session"]``.
    kinds: list[SearchKind] | None = Field(default=None, min_length=1)
    cursor: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def cursor_pages_sessions_only(self) -> UnifiedSearchRequestDTO:
        if self.cursor is not None and self.kinds != ["session"]:
            raise ValueError("cursor requires kinds=['session']")
        return self

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("query must contain at least 2 characters")
        return normalized


@router.post("", response_model=UnifiedSearchResponseDTO)
async def unified_search(
    body: UnifiedSearchRequestDTO,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> UnifiedSearchResponseDTO:
    """Search only objects visible through their existing authority paths.

    Sources fail independently. Internal errors never cross the API boundary,
    and queries are deliberately omitted from logs because they may be
    sensitive.
    """
    response.headers["Cache-Control"] = "private, no-store"
    query = body.query
    limit = body.limit
    # A malformed or cross-query cursor is the caller's error (400), not a failed source.
    after = _decode_session_search_cursor(body.cursor, query) if body.cursor else None
    kinds = set(body.kinds or ("session", "report", "file", "knowledge"))
    # Three sources check space readability per row: share a request-level access-data cache (same logic, fewer database openings per space).
    filtered = request_with_cached_access(request, team_id=user.team_id)

    searches: list[tuple[SearchKind, Awaitable[list[UnifiedSearchItemDTO] | _SourcePage]]] = [
        (kind, start())
        for kind, start in (
            ("session", lambda: _search_sessions(filtered, user.id, query, limit, after=after)),
            ("report", lambda: _search_reports(filtered, user.id, query, limit)),
            ("file", lambda: _search_files(filtered, user.id, query, limit)),
            ("knowledge", lambda: _search_knowledge(request, user, query, limit)),
        )
        if kind in kinds
    ]
    results = await asyncio.gather(
        *(_bounded(search) for _, search in searches),
        return_exceptions=True,
    )

    groups: list[UnifiedSearchGroupDTO] = []
    partial = False
    for (source, _), result in zip(searches, results, strict=True):
        if isinstance(result, BaseException):
            partial = True
            log_source_unavailable = logger.info if isinstance(result, HTTPException) and result.status_code < 500 else logger.warning
            log_source_unavailable(
                "unified search source unavailable source=%s error_type=%s",
                source,
                type(result).__name__,
            )
            groups.append(
                UnifiedSearchGroupDTO(
                    kind=source,
                    unavailable=True,
                    timed_out=isinstance(result, TimeoutError),
                    items=[],
                ),
            )
            continue
        page = result if isinstance(result, _SourcePage) else _SourcePage(items=result)
        groups.append(
            UnifiedSearchGroupDTO(
                kind=source,
                unavailable=False,
                items=page.items,
                total=page.total,
                next_cursor=page.next_cursor,
            ),
        )
    if groups and all(group.unavailable for group in groups):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return UnifiedSearchResponseDTO(query=query, partial=partial, groups=groups)


async def _bounded(
    operation: Awaitable[list[UnifiedSearchItemDTO] | _SourcePage],
) -> list[UnifiedSearchItemDTO] | _SourcePage:
    async with asyncio.timeout(_SOURCE_TIMEOUT_SECONDS):
        return await operation


def _session_search_filter_key(query: str) -> str:
    # Bound to the query and to this endpoint: a sidebar-list cursor never continues a search.
    return sessions_routes._session_list_filter_key(view="unified_search", workspace_id=None, q=query)


def _encode_session_search_cursor(query: str, after: tuple[str, str]) -> str:
    return sessions_routes._encode_session_cursor(filter_key=_session_search_filter_key(query), after=after, skip=None)


def _decode_session_search_cursor(cursor: str, query: str) -> tuple[str, str]:
    after, skip = sessions_routes._decode_session_cursor(cursor, _session_search_filter_key(query))
    if skip is not None:
        raise sessions_routes._coded_error(status.HTTP_400_BAD_REQUEST, "invalid_session_cursor")
    return after


async def _search_sessions(
    request: Request,
    user_id: str,
    query: str,
    limit: int,
    *,
    after: tuple[str, str] | None = None,
) -> _SourcePage:
    """One page of matching sessions, newest first, plus the total and the next cursor.

    ``total`` counts every readable match over the same predicate, so the sidebar can say
    how many remain. It is computed on the first page only (cursor pages return null and
    clients keep the first value); ``after`` continues from the last row of the previous page.
    """
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise RuntimeError("session store unavailable")

    # Same Space check as GET /sessions (removed membership hides the Space), decided once per
    # Space and applied in SQL: ``limit`` then counts only readable rows, so hidden newer hits
    # can never empty or shorten the group the way filtering after LIMIT did.
    readable = await sessions_routes.readable_session_workspaces(
        request,
        user_id=user_id,
        workspace_ids=await store.list_session_workspace_ids(user_id=user_id),
    )
    workspace_ids = sorted(readable)
    rows = await store.list_sessions(user_id=user_id, q=query, limit=limit + 1, workspace_ids=workspace_ids, after=after)
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_session_search_cursor(query, (str(rows[-1].last_active_at or ""), str(rows[-1].id)))
    total = None
    if after is None:
        total = await store.count_sessions(user_id=user_id, q=query, workspace_ids=workspace_ids) if workspace_ids else 0
    # Titles are rewritten after the first turn, so a hit is often only in the user's own
    # words; show the matching message so the result explains itself.
    folded = query.casefold()
    content_hits = await store.first_matching_user_texts(
        session_ids=[row.id for row in rows if folded not in (row.title or "").casefold()],
        q=query,
    )
    items = [
        UnifiedSearchItemDTO(
            kind="session",
            id=row.id,
            title=_title(row.title, row.id),
            session_id=row.id,
            snippet=_match_snippet(content_hits[row.id], query) if row.id in content_hits else None,
            updated_at=row.last_active_at,
            match="content" if row.id in content_hits else "title" if folded in (row.title or "").casefold() else None,
        )
        for row in rows
    ]
    # A count taken a moment after the page can trail it (a session started in between);
    # never report fewer matches than are already on screen.
    return _SourcePage(items=items, total=None if total is None else max(total, len(items)), next_cursor=next_cursor)


async def _search_reports(
    request: Request,
    user_id: str,
    query: str,
    limit: int,
) -> list[UnifiedSearchItemDTO]:
    store = getattr(request.app.state, "canvas_store", None)
    if store is None:
        raise RuntimeError("canvas store unavailable")
    shared_session_ids = await canvas_routes._shared_report_session_ids(
        request,
        user_id,
    )

    async def fetch_mine(n: int) -> list[Any]:
        return await store.list_for_user(user_id=user_id, q=query, limit=n)

    async def fetch_shared(n: int) -> list[Any]:
        return await store.list_for_user(user_id=user_id, q=query, limit=n, session_ids=sorted(shared_session_ids))

    # Same as GET /canvas (list_reports): filter own reports by space membership; shared reports are visible without membership (batch 4).
    async def keep_mine(rows: list[Any]) -> list[Any]:
        return await canvas_routes._filter_report_history_items(request, user_id, rows, enforce_space_membership=True)

    async def keep_shared(rows: list[Any]) -> list[Any]:
        # **Must** remove owners' own reports from the shared category first, or owners bypass membership checks through it.
        rows = [row for row in rows if row.get("user_id") != user_id]
        return await canvas_routes._filter_report_history_items(request, user_id, rows, enforce_space_membership=False)

    # Each category fetches its latest limit readable rows; the combined first limit necessarily comes from these two lists.
    mine, shared = await asyncio.gather(
        _fetch_visible(fetch_mine, keep_mine, limit),
        _fetch_visible(fetch_shared, keep_shared, limit),
    )
    by_id = {str(row["id"]): row for row in [*mine, *shared] if row.get("id") and row.get("session_id")}
    ordered = sorted(
        by_id.values(),
        key=lambda row: str(row.get("updated_at") or ""),
        reverse=True,
    )[:limit]
    return [
        UnifiedSearchItemDTO(
            kind="report",
            id=str(row["id"]),
            # Never fall back to the canvas id: it is an internal identifier, not a title.
            title=_title(row.get("title"), _title(row.get("first_section_title"), "")),
            session_id=str(row["session_id"]),
            updated_at=_optional_string(row.get("updated_at")),
            created_at=_optional_string(row.get("created_at")),
        )
        for row in ordered
    ]


async def _search_files(
    request: Request,
    user_id: str,
    query: str,
    limit: int,
) -> list[UnifiedSearchItemDTO]:
    store = getattr(request.app.state, "artifact_store", None)
    if store is None:
        raise RuntimeError("artifact store unavailable")
    scopes: dict[str, Any] = {}  # Artifact id → attribution (_ArtifactScope); widened refetches reuse previously resolved rows.
    verdicts: dict[Any, bool] = {}  # Attribution → readability. Key is the whole hashable scope (NamedTuple), not workspace_id; see files_usage.

    async def fetch(n: int) -> list[Any]:
        rows, _, _ = await store.list_for_user(user_id=user_id, q=query, sources=None, limit=n)
        return rows

    async def keep(rows: list[Any]) -> list[Any]:
        # Same per-row checks as GET /files (artifact attribution + batch 4's four categories; loop matches files_usage); check each attribution once.
        # ★ Key and verdict must use the **same** scope: if each artifact independently recomputes attribution for the verdict,
        # transient inference failures become unresolvable/fail-open, caching that readable verdict for every row sharing the attribution.
        # Infer attribution in one batch (individually: ~3 DB openings/row; 116: 20 rows ≈66 openings ≈0.9s, risking whole-group loss within 4s source timeout).
        unseen = [row for row in rows if row.id not in scopes]
        if unseen:
            scopes.update(await files_routes._artifact_scopes(request, store, unseen))
        visible = []
        for row in rows:
            scope = scopes[row.id]
            if scope not in verdicts:
                try:
                    await files_routes._require_artifact_scope_access(
                        request,
                        scope,
                        user_id=user_id,
                        operation=SpaceOperation.READ_HISTORY,
                    )
                except HTTPException as exc:
                    if exc.status_code not in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}:
                        raise
                    verdicts[scope] = False
                else:
                    verdicts[scope] = True
            if verdicts[scope]:
                visible.append(row)
        return visible

    rows = await _fetch_visible(fetch, keep, limit)
    return [
        UnifiedSearchItemDTO(
            kind="file",
            id=row.id,
            title=_title(row.filename, row.id),
            updated_at=row.last_used_at or row.created_at,
        )
        for row in rows
    ]


async def _search_knowledge(
    request: Request,
    user: User,
    query: str,
    limit: int,
) -> list[UnifiedSearchItemDTO]:
    semantic_result, metadata_result = await asyncio.gather(
        _search_knowledge_semantic(request, user, query, limit),
        _search_knowledge_metadata(request, user, query, limit),
        return_exceptions=True,
    )

    semantic_available = not isinstance(semantic_result, BaseException)
    metadata_available = not isinstance(metadata_result, BaseException)
    semantic_items = [] if not semantic_available else semantic_result
    metadata_items = [] if not metadata_available else metadata_result
    if not semantic_available:
        logger.info(
            "unified search knowledge semantic enrichment unavailable error_type=%s",
            type(semantic_result).__name__,
        )
    if not metadata_available:
        logger.warning(
            "unified search knowledge metadata unavailable error_type=%s",
            type(metadata_result).__name__,
        )
    if not semantic_available and not metadata_available:
        raise metadata_result

    by_id = {item.id: item for item in metadata_items}
    for item in reversed(semantic_items):
        by_id[item.id] = item
    ordered_ids = [item.id for item in semantic_items]
    ordered_ids.extend(item.id for item in metadata_items if item.id not in ordered_ids)
    return [by_id[item_id] for item_id in ordered_ids[:limit]]


async def _search_knowledge_semantic(
    request: Request,
    user: User,
    query: str,
    limit: int,
) -> list[UnifiedSearchItemDTO]:
    async with asyncio.timeout(_KNOWLEDGE_SEMANTIC_TIMEOUT_SECONDS):
        result = await kb_routes.search(
            KbSearchRequest(query=query, top_k=limit),
            request,
            user,
        )
    items: list[UnifiedSearchItemDTO] = []
    seen: set[str] = set()
    for chunk in result.chunks:
        if chunk.file_id in seen:
            continue
        seen.add(chunk.file_id)
        items.append(
            UnifiedSearchItemDTO(
                kind="knowledge",
                id=chunk.file_id,
                title=_title(chunk.file_name, chunk.file_id),
                snippet=_plain_text_snippet(chunk.content, query),
            ),
        )
        if len(items) >= limit:
            break
    return items


async def _search_knowledge_metadata(
    request: Request,
    user: User,
    query: str,
    limit: int,
) -> list[UnifiedSearchItemDTO]:
    async with asyncio.timeout(_KNOWLEDGE_METADATA_TIMEOUT_SECONDS):
        scope_results = await asyncio.gather(
            *(
                kb_routes.list_files(
                    request=request,
                    scope=scope,
                    limit=limit,
                    offset=0,
                    since=None,
                    status=None,
                    mime=None,
                    doc_type_id=None,
                    uploaded_after=None,
                    uploaded_before=None,
                    q=query,
                    order_by="uploaded_at",
                    order_dir="desc",
                    user=user,
                )
                for scope in ("personal", "space", "team")
            ),
            return_exceptions=True,
        )
    rows: dict[str, object] = {}
    hard_errors: list[BaseException] = []
    completed_scopes = 0
    for result in scope_results:
        if isinstance(result, BaseException):
            if isinstance(result, HTTPException) and result.status_code in {
                status.HTTP_400_BAD_REQUEST,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
            }:
                continue
            hard_errors.append(result)
            continue
        completed_scopes += 1
        for row in result.files:
            rows[row.file_id] = row
    if not rows and hard_errors and completed_scopes == 0:
        raise hard_errors[0]
    ordered = sorted(
        rows.values(),
        key=lambda row: str(getattr(row, "uploaded_at", "") or ""),
        reverse=True,
    )[:limit]
    return [
        UnifiedSearchItemDTO(
            kind="knowledge",
            id=str(row.file_id),
            title=_title(
                getattr(row, "filename", None),
                str(row.file_id),
            ),
            updated_at=_optional_string(getattr(row, "uploaded_at", None)),
        )
        for row in ordered
    ]


def _title(value: object, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _snippet(value: str, limit: int = 240) -> str | None:
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


# Knowledge chunks are indexed Markdown: a report exported to Excel and uploaded comes back as table rows carrying
# its internal section ids and source-reference JSON (116 dogfood 1.5.25: the sidebar showed
# `| 标题 | sec_lead_t_… | Source references | ["1"] |`). A search hit shows readable text only. Every rule below is
# deliberately narrow: ordinary prose (`t_order_detail 表`, `3*4=12`, `见 [2024] 年报`, `a<b and c>d`) must survive.
_MD_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Block prefixes, at a real line start only: heading, quote, bullet, ordered item (up to three digits).
_MD_BLOCK_PREFIX_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d{1,3}[.)]\s+)")
# Emphasis and code only as paired marks around text.
_MD_PAIRED_RES = (
    re.compile(r"\*\*\*(\S(?:[^*\n]*?\S)?)\*\*\*"),
    re.compile(r"\*\*(\S(?:[^*\n]*?\S)?)\*\*"),
    re.compile(r"(?<![\w*])\*(\S(?:[^*\n]*?\S)?)\*(?![\w*])"),
    re.compile(r"~~(\S(?:[^~\n]*?\S)?)~~"),
    re.compile(r"`+([^`\n]+)`+"),
)
# A real tag: a known element name, then only `name=value` attributes.
_HTML_TAG_RE = re.compile(
    r"</?(?:br|p|div|span|b|i|u|em|strong|sub|sup|a|table|thead|tbody|tr|td|th|ul|ol|li|h[1-6]|img|hr|code|pre)"
    r"(?:\s+[\w:-]+=(?:\"[^\"]*\"|'[^']*'|[^\s>]+))*\s*/?>",
    re.IGNORECASE,
)
# Bowhead object ids a reader cannot use (docs/design/概念术语.md §4.1): canvas, section, revision, artifact, tool
# call. Inline only the generated hex form; a table cell that is nothing but such an id is dropped whole
# (`sec_lead_t_…`). Bare `s_` / `t_` prefixes are left alone: they are ordinary table and column names too.
_INTERNAL_ID_RE = re.compile(r"\b(?:cv|sec|rv|art|tl)_[0-9a-f]{8,}\b")
_INTERNAL_ID_CELL_RE = re.compile(r"(?:cv|sec|rv|art|tl)_[A-Za-z0-9_-]{4,}")
# Citation markers: `[cite:3]`, quoted lists `["1", "2"]` (exported source references), `[n]` of one to three digits.
_CITATION_RE = re.compile(r'\[cite:[^\]]*\]|\[\s*"\d+"(?:\s*,\s*"\d+")*\s*\]|\[\d{1,3}\]')
# Row labels of the report Excel export's structure sheets (bowhead_core.export_xlsx): metadata, not content.
_EXPORT_ROW_LABELS = frozenset(
    {
        "source references",
        "content",
        "block key",
        "section id",
        "revision",
        "kind",
        "path",
        "placement",
        "disposition",
        "target sheets",
    },
)


def _plain_text(value: str) -> str:
    """Readable text of an indexed Markdown chunk: table cells joined, ids, citations and markup removed."""
    parts: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or _MD_TABLE_RULE_RE.match(line):
            continue
        is_table_row = line.startswith("|") or line.count("|") >= 2
        cells = [cell.strip() for cell in line.strip("|").split("|")] if is_table_row else [_MD_BLOCK_PREFIX_RE.sub("", line)]
        kept: list[str] = []
        for cell in cells:
            if is_table_row and (_INTERNAL_ID_CELL_RE.fullmatch(cell) or cell.casefold() in _EXPORT_ROW_LABELS):
                continue
            text = _MD_IMAGE_RE.sub(r"\1", cell)
            text = _MD_LINK_RE.sub(r"\1", text)
            text = _HTML_TAG_RE.sub(" ", text)
            text = _CITATION_RE.sub(" ", text)
            text = _INTERNAL_ID_RE.sub(" ", text)
            for paired in _MD_PAIRED_RES:
                text = paired.sub(r"\1", text)
            text = " ".join(text.split())
            if not text or re.fullmatch(r"[\W_]+", text):
                continue
            kept.append(text)
        if kept:
            parts.append(" · ".join(kept))
    return " ".join(parts)


def _plain_text_snippet(value: str, query: str, limit: int = 240) -> str | None:
    """Readable snippet of an indexed Markdown chunk around the first match of ``query`` (or of one of its terms)."""
    text = _plain_text(value)
    folded = text.casefold()
    terms = [" ".join(query.split()), *query.split()]
    anchor = next((term for term in terms if term and term.casefold() in folded), query)
    return _match_snippet(text, anchor, limit)


def _match_snippet(value: str, query: str, limit: int = 240) -> str | None:
    """Like :func:`_snippet`, but keeps the first match of ``query`` inside the window."""
    normalized = " ".join(value.split())
    match = re.search(re.escape(" ".join(query.split())), normalized, flags=re.IGNORECASE)
    if match is None or match.end() <= limit - 1:
        return _snippet(normalized, limit)
    start = max(0, match.start() - limit // 3)
    window = normalized[start : start + limit - 2]
    tail = "…" if start + limit - 2 < len(normalized) else ""
    return f"…{window.strip()}{tail}"
