"""Visible, safe failure reasons for the personal Slack OAuth callback."""

from types import SimpleNamespace

import pytest
from bowhead_gateway.routes import personal_mcp


class _SlackStore:
    def __init__(self, pending: object | None = None) -> None:
        self.pending = pending

    async def consume_personal_oauth_state(self, _state: str) -> object | None:
        return self.pending


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(slack_store=SimpleNamespace(db_path="unused"))))


@pytest.mark.unit
@pytest.mark.anyio
async def test_callback_reports_cancelled_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _SlackStore()
    monkeypatch.setenv("FEATURE_PERSONAL_SLACK_MCP", "1")
    monkeypatch.setattr(personal_mcp, "SlackStore", lambda _db_path: store)

    response = await personal_mcp.slack_oauth_callback(_request(), state="state", error="access_denied")

    assert response.status_code == 303
    assert response.headers["location"] == "../../../../../connections/?slack=failed&slack_reason=authorization_denied"


@pytest.mark.unit
@pytest.mark.anyio
async def test_callback_reports_expired_or_used_state(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _SlackStore()
    monkeypatch.setenv("FEATURE_PERSONAL_SLACK_MCP", "1")
    monkeypatch.setattr(personal_mcp, "SlackStore", lambda _db_path: store)

    response = await personal_mcp.slack_oauth_callback(_request(), code="code", state="expired")

    assert response.status_code == 303
    assert response.headers["location"] == "../../../../../connections/?slack=failed&slack_reason=session_expired"
