"""Security boundaries for the local Slack callback adapter."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from bowhead_gateway.routes import slack
from bowhead_gateway.routes.slack import _strip_mention, _verify_signature
from bowhead_gateway.slack.store import SlackConnection
from fastapi import HTTPException
from starlette.requests import Request


def _request(timestamp: str, signature: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-slack-request-timestamp", timestamp.encode()), (b"x-slack-signature", signature.encode())],
        }
    )


def test_slack_signature_accepts_only_current_raw_body() -> None:
    body = b'{"type":"event_callback"}'
    timestamp = str(int(time.time()))
    secret = "signing-secret"
    signature = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    _verify_signature(_request(timestamp, signature), body, secret)
    with pytest.raises(HTTPException) as exc:
        _verify_signature(_request(timestamp, signature), body + b" ", secret)
    assert exc.value.status_code == 401


def test_slack_signature_rejects_stale_replay_and_mentions_are_removed() -> None:
    with pytest.raises(HTTPException) as exc:
        _verify_signature(_request(str(int(time.time()) - 301), "v0=ignored"), b"{}", "secret")
    assert exc.value.status_code == 401
    assert _strip_mention("<@U_BOT>  帮我分析销售额") == "帮我分析销售额"


@pytest.mark.anyio
async def test_slack_config_includes_oauth_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public_url(_request: Request, *, team_id: str, path: str) -> str:
        return f"https://bowhead.example{path}"

    monkeypatch.delenv("FEATURE_PERSONAL_SLACK_MCP", raising=False)
    monkeypatch.setattr(slack, "team_public_app_url", public_url)
    connection = SlackConnection(
        id="slc_1",
        team_id="team_1",
        slack_team_id="T1",
        slack_app_id="A1",
        client_id="client_1",
        client_secret_ref=None,
        signing_secret_ref=None,
        bot_token_ref=None,
        bot_user_id="",
        channel_answers_enabled=0,
        status="draft",
        updated_by_user_id="user_1",
        updated_at="2026-01-01T00:00:00Z",
    )
    payload = await slack._config_payload(_request("0", ""), connection)

    assert payload["oauth_callback_url"] == "https://bowhead.example/api/v1/integrations/slack/oauth/callback"
    assert payload["personal_mcp_oauth_callback_url"] is None
    assert payload["required_scopes"] == ["app_mentions:read", "chat:write", "commands", "im:history", "users:read", "users:read.email"]
    assert payload["slack_settings"] == {
        "events": "https://api.slack.com/apps/A1/event-subscriptions",
        "slash_commands": "https://api.slack.com/apps/A1/slash-commands",
        "interactivity": "https://api.slack.com/apps/A1/interactive-messages",
    }


@pytest.mark.anyio
async def test_slack_config_includes_personal_mcp_oauth_redirect_url_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public_url(_request: Request, *, team_id: str, path: str) -> str:
        return f"https://bowhead.example{path}"

    monkeypatch.setenv("FEATURE_PERSONAL_SLACK_MCP", "1")
    monkeypatch.setattr(slack, "team_public_app_url", public_url)
    connection = SlackConnection(
        id="slc_1",
        team_id="team_1",
        slack_team_id="T1",
        slack_app_id="A1",
        client_id="client_1",
        client_secret_ref=None,
        signing_secret_ref=None,
        bot_token_ref=None,
        bot_user_id="",
        channel_answers_enabled=0,
        status="draft",
        updated_by_user_id="user_1",
        updated_at="2026-01-01T00:00:00Z",
    )

    payload = await slack._config_payload(_request("0", ""), connection)

    assert payload["personal_mcp_oauth_callback_url"] == "https://bowhead.example/api/v1/personal-mcp/slack/oauth/callback"


@pytest.mark.anyio
async def test_slack_install_result_redirect_uses_team_public_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public_entry(_request: Request, *, team_id: str):
        assert team_id == "team_1"
        from types import SimpleNamespace

        return SimpleNamespace(base_url="https://deployment.example/bowhead")

    monkeypatch.setattr("bowhead_gateway.public_url.team_public_entry", public_entry)

    response = await slack._install_result_redirect(_request("0", ""), team_id="team_1", result="success")

    assert response.status_code == 303
    assert response.headers["location"] == "https://deployment.example/bowhead/team/settings/integrations/slack/?install=success"


@pytest.mark.anyio
async def test_slack_oauth_error_redirect_uses_the_installing_teams_public_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = SlackConnection(
        id="slc_1",
        team_id="team_1",
        slack_team_id="",
        slack_app_id="",
        client_id="client_1",
        client_secret_ref="encrypted-secret",
        signing_secret_ref="encrypted-signing-secret",
        bot_token_ref=None,
        bot_user_id="",
        channel_answers_enabled=0,
        status="draft",
        updated_by_user_id="user_1",
        updated_at="2026-01-01T00:00:00Z",
    )

    class Store:
        async def consume_oauth_state(self, state: str) -> object:
            from bowhead_gateway.slack.store import SlackOAuthState

            assert state == "oauth-state"
            return SlackOAuthState(connection.id)

        async def by_id(self, connection_id: str) -> SlackConnection | None:
            assert connection_id == connection.id
            return connection

    async def public_entry(_request: Request, *, team_id: str):
        assert team_id == connection.team_id
        from types import SimpleNamespace

        return SimpleNamespace(base_url="https://deployment.example/bowhead")

    monkeypatch.setattr(slack, "_store", lambda _request: Store())
    monkeypatch.setattr("bowhead_gateway.public_url.team_public_entry", public_entry)

    response = await slack.oauth_callback(_request("0", ""), state="oauth-state", error="access_denied")

    assert response.status_code == 303
    assert response.headers["location"] == "https://deployment.example/bowhead/team/settings/integrations/slack/?install=failed"
