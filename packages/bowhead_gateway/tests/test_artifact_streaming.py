from __future__ import annotations

import pytest
from bowhead_gateway.artifact_streaming import (
    ByteRange,
    InvalidByteRange,
    parse_single_byte_range,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 10, None),
        ("bytes=0-4", 10, ByteRange(0, 5)),
        ("bytes=4-", 10, ByteRange(4, 10)),
        ("bytes=-3", 10, ByteRange(7, 10)),
        ("bytes=-30", 10, ByteRange(0, 10)),
        ("bytes=7-99", 10, ByteRange(7, 10)),
    ],
)
def test_parse_single_byte_range(
    header: str | None,
    size: int,
    expected: ByteRange | None,
) -> None:
    assert parse_single_byte_range(header, size) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    [
        "items=0-2",
        "bytes=",
        "bytes=0-1,4-5",
        "bytes=-0",
        "bytes=10-",
        "bytes=7-3",
        "bytes=abc-def",
        "bytes=+1-2",
        "bytes=1-+2",
    ],
)
def test_parse_single_byte_range_rejects_invalid_or_multi_range(
    header: str,
) -> None:
    with pytest.raises(InvalidByteRange):
        parse_single_byte_range(header, 10)


@pytest.mark.asyncio
async def test_html_response_uses_originating_team_instead_of_viewer_headers():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bowhead_core.execution_limits import ExecutionLimits
    from bowhead_gateway.artifact_streaming import artifact_streaming_response
    from fastapi import FastAPI, Request

    app = FastAPI()
    app.state.user_store = SimpleNamespace(get_workspace_team_id=AsyncMock(return_value="team_origin"))
    app.state.team_settings_store = SimpleNamespace(get=AsyncMock(return_value=ExecutionLimits(120, 600, 8388608).to_json()))
    request = Request({"type": "http", "app": app, "headers": [(b"x-team-id", b"team_viewer"), (b"range", b"bytes=0-3")]})
    art = SimpleNamespace(id="art_report", filename="report.html", mime="text/html", size=100, workspace_id="ws_origin")
    response = await artifact_streaming_response(request, SimpleNamespace(), art, inline=False)
    assert response.headers["Content-Security-Policy"] == "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.status_code == 206
    assert response.headers["X-Bowhead-HTML-Max-Bytes"] == "8388608"
    assert response.headers["Cache-Control"] == "private, no-store"
    app.state.user_store.get_workspace_team_id.assert_awaited_once_with("ws_origin")
    app.state.team_settings_store.get.assert_awaited_once_with("team_origin", "execution_limits")


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["text/html", "text/html; charset=utf-8", "application/xhtml+xml", "TEXT/HTML"])
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("range_header", [None, "bytes=0-15"])
async def test_active_original_bytes_keep_response_isolation_even_if_inline_is_requested(mime, inline, range_header):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bowhead_gateway.artifact_streaming import artifact_streaming_response
    from fastapi import FastAPI, Request

    content = b"<!doctype html><script>document.cookie='synthetic';</script>"
    app = FastAPI()
    headers = [(b"range", range_header.encode())] if range_header else []
    request = Request({"type": "http", "app": app, "headers": headers})
    art = SimpleNamespace(id="art_synthetic", filename="original.html", mime=mime, size=len(content), workspace_id=None)
    store = SimpleNamespace(open_read=AsyncMock(return_value=content))
    response = await artifact_streaming_response(request, store, art, inline=inline)
    assert response.headers["Content-Security-Policy"] == "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Content-Disposition"].startswith("inline" if inline else "attachment")
    assert b"".join([chunk async for chunk in response.body_iterator]) == (content[:16] if range_header else content)
