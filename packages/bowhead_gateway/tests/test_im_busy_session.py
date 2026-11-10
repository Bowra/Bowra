"""Slack and Teams answer a message sent while the conversation's Turn is still running."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_gateway.im.error_messages import im_interface_message
from bowhead_gateway.routes import slack as slack_routes
from bowhead_gateway.routes import teams as teams_routes

from bowhead_gateway import _turn_runtime as turn_runtime
from bowhead_gateway import channel_language, turn_service


class _UserStore:
    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(id=user_id, email="a@test.com", display_name="A", credit_balance=1_000, credit_quota=1_000)

    async def get_team(self, team_id: str) -> Any:
        return SimpleNamespace(id=team_id, team_credit_balance=100_000, team_credit_quota=100_000)

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        return {"id": workspace_id, "datarc_workspace_id": "dp_1"}


class _SessionStore:
    def __init__(self, active_turn_id: str | None) -> None:
        self.active_turn_id = active_turn_id

    async def get_active_turn_identity(self, session_id: str) -> tuple[str | None, int | None]:
        return self.active_turn_id, None

    async def ensure_session(self, **_kwargs: Any) -> None:
        return None


class _MemberResolver:
    async def resolve(self, *_args: Any, **_kwargs: Any) -> str:
        return "m_1"


def _state(active_turn_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        orchestrator=SimpleNamespace(session_store=_SessionStore(active_turn_id)),
        user_store=_UserStore(),
        member_resolver=_MemberResolver(),
        metrics_log=None,
        entitlements=None,
    )


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub everything before admission; record whether a Turn was admitted or spawned."""
    record: dict[str, Any] = {"admitted": 0, "spawned": 0, "spawn_error": None}

    async def _language(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(ui_language="en-US", output_language="en-US")

    async def _runtime(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(available=True, router=object())

    async def _datarc(*_args: Any, **_kwargs: Any) -> tuple[Any, bool, Any]:
        return object(), False, None

    async def _admit(*_args: Any, **_kwargs: Any) -> object:
        record["admitted"] += 1
        return object()

    async def _spawn(*_args: Any, **_kwargs: Any) -> None:
        record["spawned"] += 1
        if record["spawn_error"] is not None:
            raise record["spawn_error"]

    monkeypatch.setattr(channel_language, "resolve_channel_turn_language", _language)
    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _runtime)
    monkeypatch.setattr(turn_runtime, "_resolve_turn_datarc_runtime", _datarc)
    monkeypatch.setattr(turn_service, "admit_legacy_chat_execution", _admit)
    monkeypatch.setattr(turn_service, "spawn_turn", _spawn)
    return record


def _slack(monkeypatch: pytest.MonkeyPatch, active_turn_id: str | None) -> tuple[Any, list[str]]:
    posted: list[str] = []

    async def _post(_connection: Any, _channel: str, text: str, _thread_ts: str) -> None:
        posted.append(text)

    monkeypatch.setattr(slack_routes, "_post", _post)
    return SimpleNamespace(state=_state(active_turn_id)), posted


def _teams(monkeypatch: pytest.MonkeyPatch, active_turn_id: str | None) -> tuple[dict[str, Any], list[str]]:
    cards: list[str] = []

    async def _send_activity(_request: Any, _activity: Any, _connection: Any, card: Any) -> None:
        cards.append(str(card))

    monkeypatch.setattr(teams_routes, "_send_activity", _send_activity)
    kwargs = {
        "request": SimpleNamespace(app=SimpleNamespace(state=_state(active_turn_id))),
        "activity": {"conversation": {"id": "conv_1"}},
        "connection": SimpleNamespace(id="tc_1", entra_tenant_id="tenant_a"),
        "user_id": "u_1",
        "team_id": "team_a",
        "workspace_id": "ws_1",
        "text": "And by region?",
    }
    return kwargs, cards


async def test_slack_tells_the_user_to_resend_after_the_running_answer(monkeypatch, started) -> None:
    app, posted = _slack(monkeypatch, "t_running")

    await slack_routes._start_turn(
        app, SimpleNamespace(id="sc_1", team_id="team_a"), SimpleNamespace(id="u_1", email="a@test.com"), "ws_1", "C1", "1.0", "And by region?"
    )

    assert posted == [im_interface_message("channel_session_busy", "en-US")]
    assert started["admitted"] == 0 and started["spawned"] == 0


async def test_slack_reports_a_busy_session_detected_at_turn_start(monkeypatch, started) -> None:
    app, posted = _slack(monkeypatch, None)
    started["spawn_error"] = ValueError("submission_session_active")

    await slack_routes._start_turn(
        app, SimpleNamespace(id="sc_1", team_id="team_a"), SimpleNamespace(id="u_1", email="a@test.com"), "ws_1", "C1", "1.0", "And by region?"
    )

    assert posted == [im_interface_message("slack_processing", "en-US"), im_interface_message("channel_session_busy", "en-US")]


async def test_teams_tells_the_user_to_resend_after_the_running_answer(monkeypatch, started) -> None:
    kwargs, cards = _teams(monkeypatch, "t_running")

    await teams_routes._start_turn(**kwargs)

    assert len(cards) == 1
    assert im_interface_message("session_busy_title", "en-US") in cards[0]
    assert im_interface_message("channel_session_busy", "en-US") in cards[0]
    assert started["admitted"] == 0 and started["spawned"] == 0


async def test_teams_reports_a_busy_session_detected_at_turn_start(monkeypatch, started) -> None:
    kwargs, cards = _teams(monkeypatch, None)
    started["spawn_error"] = ValueError("submission_session_active")

    await teams_routes._start_turn(**kwargs)

    assert len(cards) == 2
    assert im_interface_message("channel_session_busy", "en-US") in cards[1]


async def test_other_turn_start_failures_still_propagate(monkeypatch, started) -> None:
    app, _posted = _slack(monkeypatch, None)
    started["spawn_error"] = ValueError("submission_turn_conflict")

    with pytest.raises(ValueError, match="submission_turn_conflict"):
        await slack_routes._start_turn(
            app, SimpleNamespace(id="sc_1", team_id="team_a"), SimpleNamespace(id="u_1", email="a@test.com"), "ws_1", "C1", "1.0", "And by region?"
        )
