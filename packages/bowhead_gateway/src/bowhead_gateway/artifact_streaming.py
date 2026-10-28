"""Permission-agnostic artifact byte-range response helpers.

Callers must complete owner/report-closure authorization before entering here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from bowhead_gateway._http import content_disposition


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int  # exclusive


class InvalidByteRange(ValueError):
    pass


def parse_single_byte_range(value: str | None, size: int) -> ByteRange | None:
    if value is None:
        return None
    if size <= 0 or not value.lower().startswith("bytes="):
        raise InvalidByteRange
    raw = value.split("=", 1)[1].strip()
    if not raw or "," in raw or "-" not in raw:
        raise InvalidByteRange
    start_text, end_text = (part.strip() for part in raw.split("-", 1))
    try:
        if not start_text:
            if not _is_ascii_digits(end_text):
                raise InvalidByteRange
            suffix = int(end_text)
            if suffix <= 0:
                raise InvalidByteRange
            return ByteRange(start=max(0, size - suffix), end=size)

        if not _is_ascii_digits(start_text):
            raise InvalidByteRange
        start = int(start_text)
        if start < 0 or start >= size:
            raise InvalidByteRange
        if not end_text:
            return ByteRange(start=start, end=size)
        if not _is_ascii_digits(end_text):
            raise InvalidByteRange
        inclusive_end = int(end_text)
        if inclusive_end < start:
            raise InvalidByteRange
        return ByteRange(start=start, end=min(size, inclusive_end + 1))
    except ValueError as exc:
        raise InvalidByteRange from exc


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and all("0" <= char <= "9" for char in value)


async def _iter_artifact_bytes(
    store: Any,
    art: Any,
    *,
    start: int,
    end: int,
) -> AsyncIterator[bytes]:
    iter_read_range = getattr(store, "iter_read_range", None)
    if callable(iter_read_range):
        async for chunk in iter_read_range(art, start=start, end=end):
            yield chunk
        return

    # Compatibility for narrow test doubles. Production ArtifactStore always
    # exposes iter_read_range and never buffers the whole reportOnly response.
    data = await store.open_read(art)
    if chunk := data[start:end]:
        yield chunk


async def artifact_streaming_response(
    request: Request,
    store: Any,
    art: Any,
    *,
    inline: bool,
) -> Response:
    size = max(0, int(art.size))
    try:
        requested = parse_single_byte_range(request.headers.get("range"), size)
    except InvalidByteRange:
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes */{size}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    selected = requested or ByteRange(start=0, end=size)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(selected.end - selected.start),
        "Content-Disposition": ("inline" if inline else content_disposition("attachment", art.filename or art.id)),
        "X-Content-Type-Options": "nosniff",
    }
    if (art.mime or "").split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}:
        from bowhead_core.execution_limits import load_team_limits
        from bowhead_core.html_preview_policy import HTML_PREVIEW_LIMIT_HEADER

        from bowhead_gateway.routes.files import _artifact_workspace_id

        # The artifact's persisted Space owns this policy, regardless of the viewer's
        # selected Team or request headers. Authorization has already succeeded.
        team_id = None
        user_store = getattr(request.app.state, "user_store", None)
        if user_store is not None:
            workspace_id = getattr(art, "workspace_id", None) or await _artifact_workspace_id(request, store, art)
            if workspace_id and workspace_id != "__team__":
                team_id = await user_store.get_workspace_team_id(workspace_id)
        limits = await load_team_limits(getattr(request.app.state, "team_settings_store", None), team_id)
        headers[HTML_PREVIEW_LIMIT_HEADER] = str(limits.html_max_bytes)
        headers["Cache-Control"] = "private, no-store"
    status_code = 200
    if requested is not None:
        status_code = 206
        headers["Content-Range"] = f"bytes {selected.start}-{selected.end - 1}/{size}"

    return StreamingResponse(
        _iter_artifact_bytes(
            store,
            art,
            start=selected.start,
            end=selected.end,
        ),
        status_code=status_code,
        media_type=art.mime or "application/octet-stream",
        headers=headers,
    )
