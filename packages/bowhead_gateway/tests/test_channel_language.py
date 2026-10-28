"""IM bridges and scheduled jobs share one turn-language resolver.

Without saved account preferences or a team default/lock, a channel turn follows the
incoming message language, then the session's previous answer language, then English.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_gateway.channel_language import resolve_channel_turn_language

pytestmark = pytest.mark.unit

CHINESE_ANSWER = "上个月华东区销售额环比增长 5%，主要来自上海和杭州。请选择要展开的区域：1. 上海 2. 杭州"


class _UserSettings:
    def __init__(self, values: dict[str, dict[str, str]] | None = None) -> None:
        self.values = values or {}

    async def list_for_user(self, user_id: str) -> dict[str, str]:
        return dict(self.values.get(user_id, {}))


class _TeamSettings:
    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        self.policy = policy

    async def get(self, team_id: str, key: str) -> str | None:
        assert key == "language_policy"
        return json.dumps(self.policy) if self.policy else None


class _SessionStore:
    def __init__(self, *answers: str) -> None:
        self.turns = [SimpleNamespace(final_answer=answer) for answer in answers]
        self.reads = 0

    async def list_turns(self, *, session_id: str, limit: int) -> list[SimpleNamespace]:
        self.reads += 1
        return self.turns[-limit:]


def _app(*, settings: dict[str, str] | None = None, policy: dict[str, Any] | None = None, answers: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            user_settings_store=_UserSettings({"u1": settings or {}}),
            team_settings_store=_TeamSettings(policy),
            orchestrator=SimpleNamespace(session_store=_SessionStore(*answers)),
        )
    )


async def _resolve(app: Any, message: str) -> Any:
    return await resolve_channel_turn_language(app, user_id="u1", workspace_id="ws1", team_id="team1", session_id="s1", message_text=message)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("上个月各省的销售额是多少？", "zh-CN"),
        ("What was revenue by province last month?", "en-US"),
        ("Quel est le chiffre d'affaires du mois dernier par région ?", "fr-FR"),
    ],
    ids=["zh", "en", "fr"],
)
async def test_unset_preferences_follow_the_message(message: str, expected: str) -> None:
    ctx = await _resolve(_app(), message)
    assert (ctx.ui_language, ctx.output_language) == (expected, expected)


@pytest.mark.asyncio
async def test_german_message_needs_the_german_output_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "Wie hat sich der Umsatz im letzten Monat entwickelt?"
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert (await _resolve(_app(), message)).output_language == "de-DE"
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "0")
    assert (await _resolve(_app(), message)).output_language == "en-US"


@pytest.mark.asyncio
async def test_bare_choice_after_a_chinese_answer_stays_chinese() -> None:
    app = _app(answers=(CHINESE_ANSWER,))
    ctx = await _resolve(app, "1")
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "zh-CN")
    assert app.state.orchestrator.session_store.reads == 1


@pytest.mark.asyncio
async def test_message_with_language_evidence_skips_the_history_read() -> None:
    app = _app(answers=(CHINESE_ANSWER,))
    ctx = await _resolve(app, "Show revenue by region")
    assert ctx.output_language == "en-US"
    assert app.state.orchestrator.session_store.reads == 0


@pytest.mark.asyncio
async def test_bare_choice_without_history_uses_english() -> None:
    ctx = await _resolve(_app(), "1")
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "en-US")


@pytest.mark.asyncio
async def test_saved_preference_overrides_the_message() -> None:
    ctx = await _resolve(_app(settings={"output_language": "en-US"}), "上个月各省的销售额是多少？")
    assert ctx.output_language == "en-US"


@pytest.mark.asyncio
async def test_team_lock_overrides_the_message_and_saved_preference() -> None:
    policy = {"version": 1, "defaults": {"output_language": "en-US"}, "locks": {"output_language": True}}
    ctx = await _resolve(_app(settings={"output_language": "zh-CN"}, policy=policy), "上个月各省的销售额是多少？")
    assert ctx.output_language == "en-US"


@pytest.mark.asyncio
async def test_message_language_beats_the_team_default() -> None:
    # language-policy.md: a concrete team default ranks below the message language.
    policy = {"version": 1, "defaults": {"output_language": "zh-CN"}}
    ctx = await _resolve(_app(policy=policy), "What was revenue last month?")
    assert ctx.output_language == "en-US"
    # A message without language evidence and no previous answer uses the team default.
    ctx = await _resolve(_app(policy=policy), "1")
    assert ctx.output_language == "zh-CN"


@pytest.mark.asyncio
async def test_settings_lookup_failure_still_follows_the_message() -> None:
    class _Broken:
        async def list_for_user(self, user_id: str) -> dict[str, str]:
            raise RuntimeError("settings unavailable")

    app = _app()
    app.state.user_settings_store = _Broken()
    ctx = await _resolve(app, "上个月各省的销售额是多少？")
    assert ctx.output_language == "zh-CN"


# ─── Slack and Teams admit the turn with the shared resolver's context ───


class _Orchestrator:
    def __init__(self) -> None:
        self.session_store = _EnsureSessionStore()


class _EnsureSessionStore:
    async def ensure_session(self, **_kwargs: Any) -> None:
        return None

    async def list_turns(self, *, session_id: str, limit: int) -> list[Any]:
        return []


class _UserStore:
    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        return {"workspace_id": workspace_id, "datarc_workspace_id": "datarc_ws_1"}

    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(id=user_id, email="alice@example.com", display_name="Alice")


class _MemberResolver:
    async def resolve(self, *_args: Any, **_kwargs: Any) -> str:
        return "member_1"


def _patch_turn_admission(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import bowhead_gateway._turn_runtime as turn_runtime
    import bowhead_gateway.credit_admission as credit_admission
    import bowhead_gateway.turn_service as turn_service

    admitted: list[Any] = []

    async def _no_denial(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _runtime(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(available=True, router=object())

    async def _datarc(*_args: Any, **_kwargs: Any) -> tuple[Any, bool, Any]:
        return object(), False, None

    async def _admit(_orchestrator: Any, turn: Any) -> object:
        admitted.append(turn)
        return object()

    async def _spawn(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(credit_admission, "check_credit_admission", _no_denial)
    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _runtime)
    monkeypatch.setattr(turn_service, "admit_legacy_chat_execution", _admit)
    monkeypatch.setattr(turn_service, "spawn_turn", _spawn)
    monkeypatch.setattr(turn_runtime, "_resolve_turn_datarc_runtime", _datarc)
    return admitted


def _channel_app(settings: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            orchestrator=_Orchestrator(),
            user_store=_UserStore(),
            member_resolver=_MemberResolver(),
            user_settings_store=_UserSettings({"u1": settings or {}}),
            metrics_log=None,
            entitlements=None,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("settings", "expected"), [(None, "zh-CN"), ({"output_language": "en-US"}, "en-US")], ids=["unset", "saved"])
async def test_slack_turn_uses_the_shared_channel_language(monkeypatch: pytest.MonkeyPatch, settings: dict[str, str] | None, expected: str) -> None:
    from bowhead_gateway.routes import slack

    admitted = _patch_turn_admission(monkeypatch)

    async def _post(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(slack, "_post", _post)
    monkeypatch.setattr(slack, "_track_task", lambda _app, task: task.cancel())
    connection = SimpleNamespace(id="conn1", team_id="team1")
    user = SimpleNamespace(id="u1", email="alice@example.com", display_name="Alice")
    await slack._start_turn(_channel_app(settings), connection, user, "ws1", "C1", "1.0", "上个月各省的销售额是多少？")
    assert len(admitted) == 1
    assert admitted[0].language_context.output_language == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("settings", "expected"), [(None, "zh-CN"), ({"output_language": "en-US"}, "en-US")], ids=["unset", "saved"])
async def test_teams_turn_uses_the_shared_channel_language(monkeypatch: pytest.MonkeyPatch, settings: dict[str, str] | None, expected: str) -> None:
    import asyncio

    from bowhead_gateway.routes import teams

    admitted = _patch_turn_admission(monkeypatch)

    async def _send(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _mirror(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(teams, "_send_activity", _send)
    monkeypatch.setattr(teams, "_mirror_turn", _mirror)
    request = SimpleNamespace(app=_channel_app(settings))
    connection = SimpleNamespace(entra_tenant_id="tenant1")
    await teams._start_turn(
        request=request,
        activity={"conversation": {"id": "conv1"}},
        connection=connection,
        user_id="u1",
        team_id="team1",
        workspace_id="ws1",
        text="上个月各省的销售额是多少？",
    )
    await asyncio.sleep(0)
    assert len(admitted) == 1
    assert admitted[0].language_context.output_language == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["slack", "teams"])
async def test_credit_denial_uses_the_turn_interface_language(monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    import bowhead_gateway.credit_admission as credit_admission
    from bowhead_gateway.im.error_messages import im_error_message
    from bowhead_gateway.routes import slack, teams

    admitted = _patch_turn_admission(monkeypatch)
    sent: list[str] = []

    async def _denied(*_args: Any, **_kwargs: Any) -> str:
        return "user_credit_exhausted"

    async def _post(_connection: Any, _channel: str, text: str, _thread: str) -> None:
        sent.append(text)

    async def _send(_request: Any, _activity: Any, _connection: Any, card: dict[str, Any]) -> None:
        sent.append(json.dumps(card, ensure_ascii=False))

    monkeypatch.setattr(credit_admission, "check_credit_admission", _denied)
    monkeypatch.setattr(slack, "_post", _post)
    monkeypatch.setattr(teams, "_send_activity", _send)
    question = "What was revenue by province last month?"
    if channel == "slack":
        user = SimpleNamespace(id="u1", email="alice@example.com", display_name="Alice")
        await slack._start_turn(_channel_app(), SimpleNamespace(id="conn1", team_id="team1"), user, "ws1", "C1", "1.0", question)
    else:
        await teams._start_turn(
            request=SimpleNamespace(app=_channel_app()),
            activity={"conversation": {"id": "conv1"}},
            connection=SimpleNamespace(entra_tenant_id="tenant1"),
            user_id="u1",
            team_id="team1",
            workspace_id="ws1",
            text=question,
        )
    assert admitted == []
    expected = im_error_message("user_credit_exhausted", "en-US")
    assert expected != im_error_message("user_credit_exhausted", "zh-CN")
    assert len(sent) == 1 and expected in sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["ok", "yes", "A", "SKU TOP10", "<@U123> :thumbsup:"])
async def test_short_or_markup_only_reply_keeps_the_chinese_session(reply: str) -> None:
    app = _app(answers=(CHINESE_ANSWER,))
    ctx = await _resolve(app, reply)
    assert ctx.output_language == "zh-CN"
    assert app.state.orchestrator.session_store.reads == 1


@pytest.mark.asyncio
async def test_chinese_question_with_english_product_names_is_chinese() -> None:
    ctx = await _resolve(_app(), "看下 Salesforce Opportunity pipeline")
    assert ctx.output_language == "zh-CN"


@pytest.mark.asyncio
async def test_automatic_answer_preference_takes_the_team_default() -> None:
    # Personal auto counts as unset: the team default fills in when the message gives no language.
    policy = {"version": 1, "defaults": {"output_language": "zh-CN"}}
    ctx = await _resolve(_app(settings={"output_language": "auto"}, policy=policy), "👍")
    assert ctx.output_language == "zh-CN"
