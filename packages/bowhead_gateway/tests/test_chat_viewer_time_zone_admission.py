"""Chat admission snapshots the viewer's browser zone with the turn's language context.

The zone (``X-Bowhead-Time-Zone``) decides the model's "today" for the turn and every
resume/continuation of it; a missing or unknown zone admits none, so execution uses
the deployment clock.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.routes.chat import ChatSendRequest
from bowhead_gateway.viewer_time_zone import TIME_ZONE_HEADER, viewer_time_zone_name

pytestmark = pytest.mark.unit

log = logging.getLogger(__name__)


class _Settings:
    async def list_for_user(self, _user_id: str) -> dict[str, str]:
        return {}

    async def list_for_workspace(self, _workspace_id: str) -> dict[str, str]:
        return {}


def _request(headers: dict[str, str], query: dict[str, str] | None = None) -> Any:
    state = SimpleNamespace(user_settings_store=_Settings(), workspace_settings_store=_Settings())
    return SimpleNamespace(app=SimpleNamespace(state=state), headers=headers, query_params=query or {}, cookies={})


async def _admitted_time_zone(monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]) -> str:
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _request: "user_1")
    body = ChatSendRequest(workspace_id="ws_1", message="今年各区域销量如何")
    _profile, _ws, language_context, _v2 = await chat_routes._resolve_web_language_inputs(_request(headers), body, log=log)
    assert language_context is not None
    return language_context.time_zone


async def test_send_admits_the_viewer_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _admitted_time_zone(monkeypatch, {TIME_ZONE_HEADER: "America/Los_Angeles"}) == "America/Los_Angeles"


@pytest.mark.parametrize("headers", [{}, {TIME_ZONE_HEADER: ""}, {TIME_ZONE_HEADER: "Mars/Base"}, {TIME_ZONE_HEADER: "America"}])
async def test_missing_or_unknown_zone_admits_none(monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]) -> None:
    assert await _admitted_time_zone(monkeypatch, headers) == ""


async def _send_and_capture(monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]) -> Any:
    """POST /chat/send through the real route (legacy writer) and capture the admitted TurnRequest."""
    import asyncio

    from test_chat_plan_approve import _app, _capture

    from bowhead_core import get_event_registry

    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    app = _app(None)
    captured = _capture(monkeypatch)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/api/v1/chat/send", json={"workspace_id": "1", "message": "今年各区域销量如何"}, headers=headers)
    assert response.status_code == 200, response.text
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    assert captured
    await get_event_registry().close(captured[0].turn_id)
    return captured[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kill", "headers", "expected"),
    [
        (False, {TIME_ZONE_HEADER: "America/Los_Angeles"}, "America/Los_Angeles"),
        (False, {}, ""),
        (True, {TIME_ZONE_HEADER: "America/Los_Angeles"}, "America/Los_Angeles"),
        (True, {TIME_ZONE_HEADER: "Mars/Base"}, ""),
    ],
    ids=["i18n-on", "i18n-on-no-header", "i18n-killed", "i18n-killed-unknown-zone"],
)
async def test_send_route_admits_the_zone_with_or_without_i18n(
    monkeypatch: pytest.MonkeyPatch, kill: bool, headers: dict[str, str], expected: str
) -> None:
    """The zone is not a language setting: switching i18n off must not drop the viewer's calendar."""
    from bowhead_core.model_clock import request_time_zone_name

    if kill:
        monkeypatch.setenv("BOWHEAD_I18N_V1_KILL", "1")
    else:
        monkeypatch.delenv("BOWHEAD_I18N_V1_KILL", raising=False)

    request = await _send_and_capture(monkeypatch, headers)

    assert request_time_zone_name(request) == expected
    if kill:
        assert request.language_context is None
        assert request.viewer_time_zone == expected
    else:
        assert request.language_context.time_zone == expected
        assert request.viewer_time_zone == ""


def test_zone_name_prefers_the_header_then_the_query() -> None:
    assert viewer_time_zone_name(_request({TIME_ZONE_HEADER: " Europe/Berlin "}, {"tz": "Asia/Tokyo"})) == "Europe/Berlin"
    assert viewer_time_zone_name(_request({}, {"tz": "Asia/Tokyo"})) == "Asia/Tokyo"
    assert viewer_time_zone_name(_request({TIME_ZONE_HEADER: "../etc/passwd"})) == ""
