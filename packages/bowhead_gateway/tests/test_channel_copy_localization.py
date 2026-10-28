"""Slack, Teams, and routine-delivery copy comes from the IM catalog in the resolved locale."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from bowhead_core.language_context import LanguageContext
from bowhead_gateway.im.copy_locale import resolve_channel_copy_locale
from bowhead_gateway.im.error_messages import IM_INTERFACE_MESSAGES, im_interface_message
from bowhead_gateway.routes import slack, teams
from starlette.requests import Request

from bowhead_gateway import _scheduled_runner


@pytest.fixture(autouse=True)
def business_copy_identity(monkeypatch):
    """Pin historical display copy without changing the installed adapters."""
    from bowhead_gateway import product_identity

    monkeypatch.setattr(product_identity, "read_build_edition", lambda: "business")


_GATEWAY_SRC = Path(slack.__file__).resolve().parents[1]
_HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LOCALES = ("zh-CN", "en-US", "de-DE", "fr-FR")

# The exact strings these adapters hard-coded before localization. zh-CN output must not change.
_PREVIOUS_ZH_COPY = {
    "channel_space_switched_title": "空间已切换",
    "channel_space_picker_title": "选择 Bowhead 空间",
    "channel_clarify_in_web_title": "需要在 Bowhead Web 中确认",
    "channel_turn_incomplete": "本次对话未完成，请稍后重试。",
    "channel_datarc_missing": "该空间尚未配置可用的北极九章连接。",
    "slack_command_ack": "Bowhead 正在处理，请稍候。",
    "slack_space_switched": "已保存此 Slack 对话使用的空间；请重新发送问题。",
    "slack_action_expired": "此操作已失效，请重新发送问题。",
    "slack_channel_answers_disabled": "群聊公开回答尚未启用。请让团队管理员在 Slack 集成设置中确认后再使用。",
    "slack_account_not_found": "未找到可用的 Bowhead 账号。请使用与 Bowhead 相同的工作邮箱加入 Slack，并联系管理员确认团队成员资格。",
    "slack_no_spaces": "你的 Bowhead 账号没有可用空间。",
    "slack_space_picker_intro": "请选择这个 Slack 对话要使用的空间。",
    "slack_not_ready": "Bowhead 尚未就绪，请稍后重试。",
    "slack_model_unconfigured": "团队尚未配置可用模型。",
    "slack_data_permissions_unavailable": "无法确认你的数据权限，请联系管理员。",
    "slack_processing": "Bowhead 正在处理…",
    "slack_clarify_in_web": "请打开 Bowhead Web 继续处理此问题。",
    "teams_personal_only_title": "仅支持个人聊天",
    "teams_personal_only": "当前试点仅开放 Teams 个人聊天。",
    "teams_account_unavailable_title": "Bowhead 账号不可用",
    "teams_email_not_member": "此 Teams 邮箱未对应当前团队中的有效 Bowhead 用户，请联系团队管理员。",
    "teams_membership_lost_title": "账号不可用",
    "teams_membership_lost": "你的 Bowhead 团队成员资格已失效，请联系管理员。",
    "teams_unsupported_title": "暂不支持此内容",
    "teams_text_only": "首期 Teams 入口只支持文本消息。",
    "teams_no_spaces": "你的 Bowhead 账号尚未加入此团队的可用空间。",
    "teams_space_switched": "已保存此 Teams 对话使用的 Bowhead 空间；请重新发送问题。",
    "teams_space_picker_intro": "请选择这个 Teams 个人聊天要使用的空间。",
    "teams_signin": "请登录 Bowhead，然后重新发送问题。",
    "teams_not_ready_title": "Bowhead 未就绪",
    "teams_not_ready": "无法启动对话，请稍后重试。",
    "teams_credits_exhausted_title": "额度已用完",
    "teams_model_unconfigured_title": "团队模型未配置",
    "teams_model_unconfigured": "请让团队管理员先完成模型配置。",
    "teams_datarc_missing_title": "空间数据连接不可用",
    "teams_data_permissions_unavailable_title": "数据权限不可用",
    "teams_data_permissions_unavailable": "无法确认你在该空间的数据权限，请联系管理员。",
    "teams_clarify_in_web": "当前试点尚不在 Teams 内处理澄清，请打开 Bowhead Web 继续。",
    # Shared with Feishu: identical to the strings Slack and Teams used.
    "answer_title": "Bowhead 最终答案",
    "status_failed": "Bowhead 处理失败",
    "completed": "完成。",
    "processing_title": "Bowhead 正在处理",
    "no_spaces_title": "没有可用空间",
}


def _catalog(key: str, locale: str) -> str:
    return IM_INTERFACE_MESSAGES[locale][key]


# ── Catalog and guard ────────────────────────────────────────────────────────


@pytest.mark.parametrize("key, previous", sorted(_PREVIOUS_ZH_COPY.items()))
def test_zh_cn_copy_is_byte_identical_to_the_previous_hard_coded_strings(key: str, previous: str) -> None:
    assert im_interface_message(key, "zh-CN") == previous


def test_previous_routine_delivery_strings_render_unchanged_in_zh_cn() -> None:
    assert im_interface_message("scheduled_run_completed", "zh-CN", name="日报") == "日报 已完成"
    assert im_interface_message("scheduled_email_subject", "zh-CN", name="日报") == "[Bowhead 定时任务] 日报"
    assert im_interface_message("scheduled_email_job", "zh-CN", name="日报") == "任务: 日报"
    assert im_interface_message("scheduled_email_view_result", "zh-CN", url="https://b.example/?session=s_1") == (
        "查看完整结果: https://b.example/?session=s_1"
    )


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_new_channel_keys_are_translated(locale: str) -> None:
    new_keys = [key for key in IM_INTERFACE_MESSAGES["en-US"] if key.startswith(("channel_", "slack_", "teams_", "scheduled_"))]
    assert len(new_keys) == 42
    for key in new_keys:
        if key in {"scheduled_email_subject", "scheduled_email_job"}:
            if key == "scheduled_email_subject":
                assert _catalog(key, locale) == "[Bowhead routine] {name}"
            else:
                expected = "Routine: {name}" if locale == "de-DE" else "Routine : {name}"
                assert _catalog(key, locale) == expected
        else:
            assert _catalog(key, locale) != _catalog(key, "en-US"), key


def test_slack_and_teams_adapter_modules_contain_no_han_characters() -> None:
    modules = [
        _GATEWAY_SRC / "routes" / "slack.py",
        _GATEWAY_SRC / "routes" / "teams.py",
        *sorted((_GATEWAY_SRC / "slack").glob("*.py")),
        *sorted((_GATEWAY_SRC / "teams").glob("*.py")),
    ]
    assert len(modules) >= 6
    offenders = [
        f"{path.name}:{number}"
        for path in modules
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _HAN.search(line)
    ]
    assert offenders == []


# ── Locale resolution outside a turn ─────────────────────────────────────────


class _Settings:
    def __init__(self, values: dict[str, dict[str, str]], *, fail: bool = False) -> None:
        self.values = values
        self.fail = fail

    async def list_for_user(self, user_id: str) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("settings unavailable")
        return self.values.get(user_id, {})


async def test_copy_locale_uses_the_shared_channel_resolver_and_card_snapshot_first() -> None:
    app = SimpleNamespace(state=SimpleNamespace(user_settings_store=_Settings({"u_1": {"ui_language": "fr-FR"}})))
    assert await resolve_channel_copy_locale(app, user_id="u_1", message_text="看一下上个月的 GMV") == "fr-FR"
    assert await resolve_channel_copy_locale(app, user_id="u_2", message_text="看一下上个月的 GMV") == "zh-CN"
    assert await resolve_channel_copy_locale(app, user_id="u_2", message_text="Show me revenue for last month") == "en-US"
    assert await resolve_channel_copy_locale(app, user_id="u_2", message_text="123 👍") == "en-US"
    assert await resolve_channel_copy_locale(app, user_id="u_2") == "en-US"
    # A card button carries its turn's snapshot, which wins over later settings.
    assert await resolve_channel_copy_locale(app, user_id="u_1", rendered_locale="de-DE") == "de-DE"
    assert await resolve_channel_copy_locale(app, user_id="u_1", rendered_locale="xx") == "fr-FR"
    app.state.user_settings_store = _Settings({}, fail=True)
    assert await resolve_channel_copy_locale(app, user_id="u_1", message_text="看一下上个月的 GMV") == "zh-CN"


# ── Slack ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("locale", _LOCALES)
def test_slack_turn_cards_use_the_turn_locale(locale: str) -> None:
    answer = slack._turn_event_card("final_answer", {"text": ""}, locale, locale)
    assert answer is not None
    assert answer[0]["text"]["text"] == _catalog("answer_title", locale)
    assert answer[1]["text"]["text"] == _catalog("completed", locale)
    failed = slack._turn_event_card("turn_failed", {}, locale, locale)
    assert failed is not None
    assert [block["text"]["text"] for block in failed] == [_catalog("status_failed", locale), _catalog("channel_turn_incomplete", locale)]
    assert slack._turn_event_card("narrative.delta", {}, locale, locale) is None


def test_slack_cards_render_key_copy_per_locale() -> None:
    clarify = {locale: slack._turn_event_card("clarification_required", {}, locale, locale) for locale in _LOCALES}
    assert [block["text"]["text"] for block in clarify["zh-CN"] or []] == ["需要在 Bowhead Web 中确认", "请打开 Bowhead Web 继续处理此问题。"]
    assert [block["text"]["text"] for block in clarify["en-US"] or []] == [
        "Confirm in Bowhead Web",
        "Open Bowhead Web to continue with this question.",
    ]
    for locale in ("de-DE", "fr-FR"):
        assert [block["text"]["text"] for block in clarify[locale] or []] == [
            _catalog("channel_clarify_in_web_title", locale),
            _catalog("slack_clarify_in_web", locale),
        ]
    picker = slack._workspace_card([{"workspace_id": "ws_1", "name": "Sales"}], "en-US")
    assert picker[0]["text"]["text"] == "Choose a Bowhead space"
    assert picker[1]["text"]["text"] == "Choose the space for this Slack conversation."
    assert picker[2]["block_id"] == "bowhead.workspace:en-US"


def test_slack_zh_cn_cards_match_the_previous_structure() -> None:
    assert slack._turn_event_card("final_answer", {"text": "GMV 上涨"}, "zh-CN", "zh-CN") == slack._card("Bowhead 最终答案", "GMV 上涨")
    assert slack._turn_event_card("error", {}, "zh-CN", "zh-CN") == slack._card("Bowhead 处理失败", "本次对话未完成，请稍后重试。")
    picker = slack._workspace_card([{"workspace_id": "ws_1", "name": "Sales"}], "zh-CN")
    assert picker[:2] == slack._card("选择 Bowhead 空间", "请选择这个 Slack 对话要使用的空间。")


class _SlackStore:
    def __init__(self, identities: dict[str, str] | None = None) -> None:
        self.identities = identities or {}

    async def get_identity(self, connection_id: str, slack_user_id: str) -> str | None:
        return self.identities.get(slack_user_id)


def _slack_connection(*, channel_answers_enabled: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id="slc_1", team_id="team_1", slack_team_id="T1", channel_answers_enabled=channel_answers_enabled)


@pytest.mark.parametrize(
    ("text", "settings", "expected_key", "expected_locale"),
    [
        ("看一下 GMV", {}, "slack_channel_answers_disabled", "zh-CN"),
        ("show GMV", {}, "slack_channel_answers_disabled", "en-US"),
        ("看一下 GMV", {"u_1": {"ui_language": "en-US"}}, "slack_channel_answers_disabled", "en-US"),
    ],
)
async def test_slack_out_of_turn_copy_follows_preference_then_message(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    settings: dict[str, dict[str, str]],
    expected_key: str,
    expected_locale: str,
) -> None:
    posted: list[str] = []

    async def _post(_connection: Any, _channel: str, body: str, _thread_ts: str) -> None:
        posted.append(body)

    monkeypatch.setattr(slack, "_post", _post)
    app = SimpleNamespace(state=SimpleNamespace(slack_store=_SlackStore({"U1": "u_1"}), user_settings_store=_Settings(settings)))
    await slack._handle_inbound(app, _slack_connection(channel_answers_enabled=0), "U1", "C1", "1.0", text, True)
    assert posted == [im_interface_message(expected_key, expected_locale)]


@pytest.mark.parametrize(("text", "locale"), [("看一下 GMV", "zh-CN"), ("show GMV", "en-US")])
async def test_slack_unknown_account_copy_follows_the_message(monkeypatch: pytest.MonkeyPatch, text: str, locale: str) -> None:
    posted: list[str] = []

    async def _post(_connection: Any, _channel: str, body: str, _thread_ts: str) -> None:
        posted.append(body)

    async def _no_user(*_args: Any) -> None:
        return None

    monkeypatch.setattr(slack, "_post", _post)
    monkeypatch.setattr(slack, "_resolve_user", _no_user)
    app = SimpleNamespace(state=SimpleNamespace(slack_store=_SlackStore(), user_settings_store=_Settings({})))
    await slack._handle_inbound(app, _slack_connection(), "U1", "C1", "1.0", text, False)
    assert posted == [im_interface_message("slack_account_not_found", locale)]


def _form_request(app: Any, form: dict[str, str]) -> Request:
    body = urlencode(form).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "app": app,
        },
        receive,
    )


@pytest.mark.parametrize(("block_id", "locale"), [("bowhead.workspace:zh-CN", "zh-CN"), ("bowhead.workspace:fr-FR", "fr-FR"), ("legacy", "en-US")])
async def test_slack_space_switch_reply_follows_the_rendered_card_locale(monkeypatch: pytest.MonkeyPatch, block_id: str, locale: str) -> None:
    async def _connection(_request: Request, _connection_id: str) -> Any:
        return _slack_connection()

    async def _select(*_args: Any) -> None:
        return None

    monkeypatch.setattr(slack, "_authenticated_connection", _connection)
    monkeypatch.setattr(slack, "_select_workspace", _select)
    app = SimpleNamespace(state=SimpleNamespace(slack_store=_SlackStore(), user_settings_store=_Settings({})))
    payload = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "actions": [{"action_id": "bowhead.workspace", "value": "ws_1", "block_id": block_id}],
    }
    response = await slack.interactions("slc_1", _form_request(app, {"payload": json.dumps(payload)}))
    assert response["blocks"] == slack._card(_catalog("channel_space_switched_title", locale), _catalog("slack_space_switched", locale))


@pytest.mark.parametrize(("text", "locale"), [("看 GMV", "zh-CN"), ("GMV please", "en-US")])
async def test_slack_command_ack_follows_the_command_text(monkeypatch: pytest.MonkeyPatch, text: str, locale: str) -> None:
    async def _connection(_request: Request, _connection_id: str) -> Any:
        return _slack_connection()

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(slack, "_authenticated_connection", _connection)
    monkeypatch.setattr(slack, "_handle_inbound", _noop)
    app = SimpleNamespace(state=SimpleNamespace(slack_store=_SlackStore(), user_settings_store=_Settings({})))
    response = await slack.command("slc_1", _form_request(app, {"team_id": "T1", "user_id": "U1", "channel_id": "C1", "text": text}))
    assert response == {"response_type": "ephemeral", "text": _catalog("slack_command_ack", locale)}


class _Bus:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self.events = events

    async def subscribe(self) -> Any:
        for event_type, payload in self.events:
            yield SimpleNamespace(event_type=event_type, payload=payload)


class _Registry:
    def __init__(self, bus: _Bus) -> None:
        self.bus = bus

    async def get_or_create(self, _turn_id: str, producer: bool = True) -> _Bus:
        return self.bus


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_slack_mirror_renders_turn_cards_in_the_message_locale(monkeypatch: pytest.MonkeyPatch, locale: str) -> None:
    import bowhead_core

    posted: list[list[dict[str, Any]]] = []

    async def _post_blocks(_connection: Any, _channel: str, blocks: list[dict[str, Any]], _thread_ts: str) -> None:
        posted.append(blocks)

    bus = _Bus([("clarification_required", {}), ("turn_failed", {})])
    monkeypatch.setattr(bowhead_core, "get_event_registry", lambda: _Registry(bus))
    monkeypatch.setattr(slack, "_post_blocks", _post_blocks)
    await slack._mirror_turn(_slack_connection(), "C1", "1.0", "t_1", locale, locale)
    assert posted == [
        slack._card(_catalog("channel_clarify_in_web_title", locale), _catalog("slack_clarify_in_web", locale)),
        slack._card(_catalog("status_failed", locale), _catalog("channel_turn_incomplete", locale)),
    ]


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_teams_mirror_renders_turn_cards_in_the_message_locale(monkeypatch: pytest.MonkeyPatch, locale: str) -> None:
    import bowhead_core

    cards: list[dict[str, Any]] = []

    async def _send(_request: Any, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    async def _body(_neutral: Any, text: str, _app: Any, *, locale: str, answer_locale: str | None = None) -> str:
        return text or _catalog("completed", answer_locale or locale)

    bus = _Bus([("final_answer", {"text": ""}), ("error", {})])
    monkeypatch.setattr(bowhead_core, "get_event_registry", lambda: _Registry(bus))
    monkeypatch.setattr(teams, "_send_activity", _send)
    monkeypatch.setattr(teams, "teams_answer_body", _body)
    await teams._mirror_turn(SimpleNamespace(app=None), {}, SimpleNamespace(), "t_1", locale, locale)
    assert cards == [
        teams._card(_catalog("answer_title", locale), _catalog("completed", locale)),
        teams._card(_catalog("status_failed", locale), _catalog("channel_turn_incomplete", locale)),
    ]


class _CreditStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(id=user_id, email="a@test.com", credit_balance=0, credit_quota=1_000)

    async def get_team(self, team_id: str) -> Any:
        return SimpleNamespace(id=team_id, team_credit_balance=100_000, team_credit_quota=100_000)

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        return {"id": workspace_id, "datarc_workspace_id": "dp_1"}

    async def record_credit_exhaustion_event(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": "cex_test", **kwargs}


@pytest.mark.parametrize(("text", "locale"), [("看 GMV", "zh-CN"), ("show GMV", "en-US")])
async def test_slack_and_teams_refusals_before_a_turn_follow_the_message(monkeypatch: pytest.MonkeyPatch, text: str, locale: str) -> None:
    from bowhead_gateway.im.error_messages import im_error_message

    posted: list[str] = []
    cards: list[dict[str, Any]] = []

    async def _post(_connection: Any, _channel: str, body: str, _thread_ts: str) -> None:
        posted.append(body)

    async def _send(_request: Any, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(slack, "_post", _post)
    monkeypatch.setattr(teams, "_send_activity", _send)
    state = SimpleNamespace(orchestrator=object(), user_store=_CreditStore(), metrics_log=None, user_settings_store=_Settings({}))
    await slack._start_turn(
        SimpleNamespace(state=state), SimpleNamespace(id="slc_1", team_id="team_1"), SimpleNamespace(id="u_1"), "ws_1", "C1", "1.0", text
    )
    await teams._start_turn(
        request=SimpleNamespace(app=SimpleNamespace(state=state)),
        activity={},
        connection=SimpleNamespace(id="tc_1", entra_tenant_id="tenant_1"),
        user_id="u_1",
        team_id="team_1",
        workspace_id="ws_1",
        text=text,
    )
    assert posted == [im_error_message("user_credit_exhausted", locale)]
    assert cards == [teams._card(_catalog("teams_credits_exhausted_title", locale), im_error_message("user_credit_exhausted", locale))]


# ── Teams ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("locale", _LOCALES)
def test_teams_turn_and_picker_cards_use_the_locale(locale: str) -> None:
    assert teams._turn_event_title_and_body("clarification_required", locale, locale) == (
        _catalog("channel_clarify_in_web_title", locale),
        _catalog("teams_clarify_in_web", locale),
    )
    assert teams._turn_event_title_and_body("turn_aborted", locale, locale) == (
        _catalog("status_failed", locale),
        _catalog("channel_turn_incomplete", locale),
    )
    assert teams._turn_event_title_and_body("done", locale, locale) is None
    picker = teams._workspace_picker_card([{"workspace_id": "ws_1", "name": "Sales"}], locale)
    assert [block["text"] for block in picker["body"]] == [
        _catalog("channel_space_picker_title", locale),
        _catalog("teams_space_picker_intro", locale),
    ]
    assert picker["actions"][0]["data"] == {"workspace_id": "ws_1", "locale": locale}
    connection = SimpleNamespace(sso_connection_name="sso", sso_audience="api://bowhead")
    assert teams._signin_card(connection, locale)["_oauth"]["text"] == _catalog("teams_signin", locale)


def test_teams_cards_render_key_copy_and_unchanged_zh_cn() -> None:
    assert teams._turn_event_title_and_body("error", "zh-CN", "zh-CN") == ("Bowhead 处理失败", "本次对话未完成，请稍后重试。")
    assert teams._turn_event_title_and_body("error", "en-US", "en-US") == (
        "Bowhead could not complete the request",
        "This conversation did not finish. Try again later.",
    )
    picker = teams._workspace_picker_card([{"workspace_id": "ws_1", "name": "Sales"}], "zh-CN")
    assert picker["body"] == teams._card("选择 Bowhead 空间", "请选择这个 Teams 个人聊天要使用的空间。")["body"]
    connection = SimpleNamespace(sso_connection_name="sso", sso_audience="api://bowhead")
    assert teams._signin_card(connection, "zh-CN")["_oauth"]["text"] == "请登录 Bowhead，然后重新发送问题。"
    assert teams._signin_card(connection, "en-US")["_oauth"]["text"] == "Sign in to Bowhead, then send your question again."


class _TeamsStore:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def claim_activity(self, _activity_id: str) -> bool:
        return True

    async def get_identity(self, _tenant: str, oid: str) -> tuple[str, str] | None:
        return ("u_1", "team_1") if oid == "oid_1" else None

    async def set_workspace(self, _tenant: str, _conversation: str, _oid: str, workspace_id: str) -> None:
        self.saved.append(workspace_id)


class _UserStore:
    async def list_workspaces_for_user(self, _user_id: str, team_id: str | None = None) -> list[dict[str, str]]:
        return [{"workspace_id": "ws_1", "name": "Sales"}]


def _json_request(app: Any, body: dict[str, Any]) -> Request:
    raw = json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"application/json")], "app": app}, receive)


@pytest.mark.parametrize(
    ("text", "settings", "locale"),
    [("看一下 GMV", {}, "zh-CN"), ("show GMV", {}, "en-US"), ("看一下 GMV", {"u_1": {"ui_language": "fr-FR"}}, "fr-FR")],
)
async def test_teams_out_of_turn_copy_follows_preference_then_message(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    settings: dict[str, dict[str, str]],
    locale: str,
) -> None:
    cards: list[dict[str, Any]] = []

    async def _authenticate(_request: Request, _activity: dict[str, Any]) -> tuple[Any, str]:
        return SimpleNamespace(team_id="team_1"), "tenant_1"

    async def _send(_request: Request, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(teams, "_authenticate_activity", _authenticate)
    monkeypatch.setattr(teams, "_send_activity", _send)
    app = SimpleNamespace(state=SimpleNamespace(teams_store=_TeamsStore(), user_settings_store=_Settings(settings)))
    activity = {
        "id": "act_1",
        "type": "message",
        "text": text,
        "conversation": {"id": "conv_1", "conversationType": "groupChat"},
        "from": {"aadObjectId": "oid_1"},
    }
    assert await teams.messages(_json_request(app, activity)) == {}
    assert cards == [teams._card(_catalog("teams_personal_only_title", locale), _catalog("teams_personal_only", locale))]


@pytest.mark.parametrize(("rendered", "locale"), [("zh-CN", "zh-CN"), ("de-DE", "de-DE"), (None, "en-US")])
async def test_teams_space_switch_reply_follows_the_rendered_card_locale(monkeypatch: pytest.MonkeyPatch, rendered: str | None, locale: str) -> None:
    cards: list[dict[str, Any]] = []

    async def _send(_request: Request, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(teams, "_send_activity", _send)
    store = _TeamsStore()
    app = SimpleNamespace(state=SimpleNamespace(teams_store=store, user_store=_UserStore(), user_settings_store=_Settings({})))
    data: dict[str, Any] = {"verb": "bowhead.workspace.select", "workspace_id": "ws_1"}
    if rendered:
        data["locale"] = rendered
    activity = {"value": {"action": data}, "from": {"aadObjectId": "oid_1"}, "conversation": {"id": "conv_1"}}
    await teams._handle_workspace_selection(_json_request(app, {}), activity, SimpleNamespace(team_id="team_1"), "tenant_1")
    assert store.saved == ["ws_1"]
    assert cards == [teams._card(_catalog("channel_space_switched_title", locale), _catalog("teams_space_switched", locale))]


@pytest.mark.parametrize(
    ("text", "client_locale", "settings", "locale"),
    [
        ("123", "de-DE", {}, "de-DE"),  # no evidence: the Teams client language stands in for the browser
        ("123", "fr_FR", {}, "fr-FR"),
        ("123", "ja-JP", {}, "en-US"),  # unsupported client language is ignored
        ("看一下上个月的 GMV", "de-DE", {}, "zh-CN"),  # message evidence wins
        ("123", "de-DE", {"u_1": {"ui_language": "fr-FR"}}, "fr-FR"),  # saved preference wins
    ],
)
async def test_teams_client_locale_ranks_between_message_evidence_and_english(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    client_locale: str,
    settings: dict[str, dict[str, str]],
    locale: str,
) -> None:
    cards: list[dict[str, Any]] = []

    async def _authenticate(_request: Request, _activity: dict[str, Any]) -> tuple[Any, str]:
        return SimpleNamespace(team_id="team_1"), "tenant_1"

    async def _send(_request: Request, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(teams, "_authenticate_activity", _authenticate)
    monkeypatch.setattr(teams, "_send_activity", _send)
    app = SimpleNamespace(state=SimpleNamespace(teams_store=_TeamsStore(), user_settings_store=_Settings(settings)))
    activity = {
        "id": "act_1",
        "type": "message",
        "text": text,
        "locale": client_locale,
        "conversation": {"id": "conv_1", "conversationType": "groupChat"},
        "from": {"aadObjectId": "oid_1"},
    }
    assert await teams.messages(_json_request(app, activity)) == {}
    assert cards == [teams._card(_catalog("teams_personal_only_title", locale), _catalog("teams_personal_only", locale))]


class _SessionStore:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def list_turns(self, *, session_id: str, limit: int) -> list[Any]:
        return [SimpleNamespace(final_answer=self.answer)]


async def test_previous_answer_outranks_the_teams_client_locale() -> None:
    answer = "上个月营业额同比增长 8%，主要来自华东地区的新客户，其中线上渠道贡献了一半以上的增量。"
    app = SimpleNamespace(state=SimpleNamespace(orchestrator=SimpleNamespace(session_store=_SessionStore(answer))))
    assert await resolve_channel_copy_locale(app, user_id=None, message_text="1", session_id="s_1", client_language="de-DE") == "zh-CN"
    assert await resolve_channel_copy_locale(app, user_id=None, message_text="1", client_language="de-DE") == "de-DE"


async def test_teams_turn_language_uses_the_client_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_gateway.im.error_messages import im_error_message

    cards: list[dict[str, Any]] = []

    async def _send(_request: Any, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(teams, "_send_activity", _send)
    state = SimpleNamespace(orchestrator=object(), user_store=_CreditStore(), metrics_log=None, user_settings_store=_Settings({}))
    await teams._start_turn(
        request=SimpleNamespace(app=SimpleNamespace(state=state)),
        activity={"locale": "fr-FR"},
        connection=SimpleNamespace(id="tc_1", entra_tenant_id="tenant_1"),
        user_id="u_1",
        team_id="team_1",
        workspace_id="ws_1",
        text="123",
    )
    assert cards == [teams._card(_catalog("teams_credits_exhausted_title", "fr-FR"), im_error_message("user_credit_exhausted", "fr-FR"))]


# ── Routine delivery ─────────────────────────────────────────────────────────


async def test_delivery_locale_uses_the_run_snapshot_then_policy_fallback() -> None:
    job = SimpleNamespace(user_id="u_1", query="看 GMV")
    app = SimpleNamespace(state=SimpleNamespace(user_settings_store=_Settings({})))
    assert await _scheduled_runner._delivery_locale(app, job, LanguageContext(ui_language="de-DE")) == "de-DE"
    assert await _scheduled_runner._delivery_locale(app, job) == "zh-CN"
    assert await _scheduled_runner._delivery_locale(app, SimpleNamespace(user_id="u_1", query="weekly GMV")) == "en-US"
    app.state.user_settings_store = _Settings({"u_1": {"ui_language": "fr-FR"}})
    assert await _scheduled_runner._delivery_locale(app, job) == "fr-FR"


class _DeliveryUserStore:
    async def team_member_emails(self, _team_id: str) -> set[str]:
        return {"owner@a.com"}


class _Entries:
    async def get(self, _entry_id: str) -> None:
        return None

    async def default_for_team(self, team_id: str) -> Any:
        return SimpleNamespace(status="active", team_id=team_id, base_url="https://b.example")


@pytest.mark.parametrize(
    ("locale", "subject", "lines"),
    [
        ("zh-CN", "[Bowhead 定时任务] 日报", ["任务: 日报", "", "日报 已完成", "", "查看完整结果: https://b.example/?session=s_1", "", "— Bowhead"]),
        (
            "en-US",
            "[Bowhead routine] 日报",
            ["Routine: 日报", "", "日报 completed", "", "View the full result: https://b.example/?session=s_1", "", "— Bowhead"],
        ),
    ],
)
async def test_delivery_email_renders_in_the_run_locale(monkeypatch: pytest.MonkeyPatch, locale: str, subject: str, lines: list[str]) -> None:
    sent: list[tuple[str, str, str]] = []

    async def _send(addr: str, mail_subject: str, body: str, *, team_id: str | None = None, store: Any = None) -> tuple[bool, None]:
        sent.append((addr, mail_subject, body))
        return True, None

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", _send)
    app = SimpleNamespace(state=SimpleNamespace(user_store=_DeliveryUserStore(), public_entry_store=_Entries(), notification_service=None))
    job = SimpleNamespace(id="job_1", name="日报", user_id="u_1", notify_channels={"email": ["owner@a.com"]}, public_entry_id=None)
    await _scheduled_runner._deliver_run_result(
        app,
        job,
        summary=_scheduled_runner._summary_text("", job.name, locale),
        session_id="s_1",
        run_id="t_1",
        team_id="team_1",
        locale=locale,
    )
    assert sent == [("owner@a.com", subject, "\n".join(lines))]


# ── Answer-tied cards follow the answer language ─────────────────────────────


async def test_slack_mirror_answer_cards_follow_answer_language_and_failure_follows_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    import bowhead_core

    posted: list[list[dict[str, Any]]] = []

    async def _post_blocks(_connection: Any, _channel: str, blocks: list[dict[str, Any]], _thread_ts: str) -> None:
        posted.append(blocks)

    bus = _Bus([("final_answer", {"text": ""}), ("clarification_required", {}), ("turn_failed", {})])
    monkeypatch.setattr(bowhead_core, "get_event_registry", lambda: _Registry(bus))
    monkeypatch.setattr(slack, "_post_blocks", _post_blocks)
    await slack._mirror_turn(_slack_connection(), "C1", "1.0", "t_1", "zh-CN", "en-US")
    assert posted == [
        slack._card("Bowhead final answer", "Done."),
        slack._card("Confirm in Bowhead Web", "Open Bowhead Web to continue with this question."),
        slack._card("Bowhead 处理失败", "本次对话未完成，请稍后重试。"),
    ]


async def test_teams_mirror_answer_cards_follow_answer_language_and_failure_follows_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    import bowhead_gateway.im.turn_render as turn_render

    import bowhead_core

    cards: list[dict[str, Any]] = []

    async def _send(_request: Any, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    async def _no_labels(*_args: Any, **_kwargs: Any) -> None:
        return None

    bus = _Bus([("final_answer", {"text": ""}), ("clarification_required", {}), ("error", {})])
    monkeypatch.setattr(bowhead_core, "get_event_registry", lambda: _Registry(bus))
    monkeypatch.setattr(teams, "_send_activity", _send)
    monkeypatch.setattr(turn_render, "refresh_kb_citation_labels", _no_labels)
    await teams._mirror_turn(SimpleNamespace(app=None), {}, SimpleNamespace(), "t_1", "zh-CN", "en-US")
    assert cards == [
        teams._card("Bowhead final answer", "Done."),
        teams._card("Confirm in Bowhead Web", "This pilot does not handle clarifications in Teams yet. Open Bowhead Web to continue."),
        teams._card("Bowhead 处理失败", "本次对话未完成，请稍后重试。"),
    ]


class _Runtime:
    available = True
    router = object()


async def _start_turn_mirror_locales(monkeypatch: pytest.MonkeyPatch, channel: str) -> tuple[str, str]:
    """Run a Slack or Teams turn start with stubs and capture the locales handed to the mirror."""
    import asyncio

    from bowhead_gateway import _turn_runtime, turn_service

    captured: list[tuple[str, str]] = []

    async def _runtime(*_args: Any, **_kwargs: Any) -> _Runtime:
        return _Runtime()

    async def _datarc(*_args: Any) -> tuple[object, None, None]:
        return object(), None, None

    async def _admit(_orchestrator: Any, _turn: Any) -> object:
        return object()

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _mirror(*args: Any) -> None:
        captured.append((args[-2], args[-1]))

    class _Members:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> str:
            return "member_1"

    class _Sessions:
        async def ensure_session(self, **_kwargs: Any) -> None:
            return None

    class _Store(_CreditStore):
        async def get_user(self, user_id: str) -> Any:
            return SimpleNamespace(id=user_id, email="a@test.com", credit_balance=500, credit_quota=1_000, display_name="A")

    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _runtime)
    monkeypatch.setattr(turn_service, "admit_legacy_chat_execution", _admit)
    monkeypatch.setattr(turn_service, "spawn_turn", _noop)
    monkeypatch.setattr(_turn_runtime, "_resolve_turn_datarc_runtime", _datarc)
    state = SimpleNamespace(
        orchestrator=SimpleNamespace(session_store=_Sessions()),
        user_store=_Store(),
        metrics_log=None,
        user_settings_store=_Settings({"u_1": {"ui_language": "zh-CN", "output_language": "en-US"}}),
        member_resolver=_Members(),
        entitlements=None,
    )
    if channel == "slack":
        monkeypatch.setattr(slack, "_post", _noop)
        monkeypatch.setattr(slack, "_mirror_turn", _mirror)
        monkeypatch.setattr(slack, "_track_task", lambda *_args: None)
        user = SimpleNamespace(id="u_1", email="a@test.com")
        await slack._start_turn(SimpleNamespace(state=state), _slack_connection(), user, "ws_1", "C1", "1.0", "看 GMV")
    else:
        monkeypatch.setattr(teams, "_send_activity", _noop)
        monkeypatch.setattr(teams, "_mirror_turn", _mirror)
        await teams._start_turn(
            request=SimpleNamespace(app=SimpleNamespace(state=state)),
            activity={},
            connection=SimpleNamespace(id="tc_1", entra_tenant_id="tenant_1"),
            user_id="u_1",
            team_id="team_1",
            workspace_id="ws_1",
            text="看 GMV",
        )
    await asyncio.sleep(0)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("channel", ["slack", "teams"])
async def test_turn_start_hands_interface_and_answer_languages_to_the_mirror(monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    assert await _start_turn_mirror_locales(monkeypatch, channel) == ("zh-CN", "en-US")


# ── Receipts with a known Space, session, and Universal Action data ──────────


class _WorkspaceSettings:
    async def list_for_workspace(self, workspace_id: str) -> dict[str, str]:
        # zh-CN, because the new Slack/Teams keys still carry English placeholders in de-DE and fr-FR.
        return {"default_ui_language": "zh-CN"} if workspace_id == "ws_1" else {}


async def test_slack_space_switch_receipt_uses_the_selected_space_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _connection(_request: Request, _connection_id: str) -> Any:
        return _slack_connection()

    async def _select(*_args: Any) -> None:
        return None

    monkeypatch.setattr(slack, "_authenticated_connection", _connection)
    monkeypatch.setattr(slack, "_select_workspace", _select)
    state = SimpleNamespace(slack_store=_SlackStore({"U1": "u_1"}), user_settings_store=_Settings({}), workspace_settings_store=_WorkspaceSettings())
    payload = {"team": {"id": "T1"}, "user": {"id": "U1"}, "channel": {"id": "C1"}, "actions": [{"action_id": "bowhead.workspace", "value": "ws_1"}]}
    response = await slack.interactions("slc_1", _form_request(SimpleNamespace(state=state), {"payload": json.dumps(payload)}))
    assert response["blocks"] == slack._card(_catalog("channel_space_switched_title", "zh-CN"), _catalog("slack_space_switched", "zh-CN"))


async def test_slack_command_receipt_uses_the_thread_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _connection(_request: Request, _connection_id: str) -> Any:
        return _slack_connection()

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    sessions: list[str] = []

    class _Turns:
        async def list_turns(self, *, session_id: str, limit: int) -> list[Any]:
            sessions.append(session_id)
            return [SimpleNamespace(final_answer="上个月营业额同比增长 8%，主要来自华东地区的新客户，其中线上渠道贡献了一半以上的增量。")]

    monkeypatch.setattr(slack, "_authenticated_connection", _connection)
    monkeypatch.setattr(slack, "_handle_inbound", _noop)
    state = SimpleNamespace(
        slack_store=_SlackStore({"U1": "u_1"}), user_settings_store=_Settings({}), orchestrator=SimpleNamespace(session_store=_Turns())
    )
    form = {"team_id": "T1", "user_id": "U1", "channel_id": "C1", "thread_ts": "171.5", "text": "1"}
    response = await slack.command("slc_1", _form_request(SimpleNamespace(state=state), form))
    assert sessions == [slack._session_id(_slack_connection(), "C1", "171.5", "u_1")]
    assert response["text"] == _catalog("slack_command_ack", "zh-CN")


async def test_teams_space_selection_reads_universal_action_data(monkeypatch: pytest.MonkeyPatch) -> None:
    cards: list[dict[str, Any]] = []

    async def _send(_request: Request, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(teams, "_send_activity", _send)
    store = _TeamsStore()
    app = SimpleNamespace(state=SimpleNamespace(teams_store=store, user_store=_UserStore(), user_settings_store=_Settings({})))
    # The shape Bot Framework sends for an Action.Execute invoke (adaptiveCard/action).
    activity = {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "value": {"action": {"type": "Action.Execute", "verb": "bowhead.workspace.select", "data": {"workspace_id": "ws_1", "locale": "fr-FR"}}},
        "from": {"aadObjectId": "oid_1"},
        "conversation": {"id": "conv_1"},
    }
    await teams._handle_workspace_selection(_json_request(app, {}), activity, SimpleNamespace(team_id="team_1"), "tenant_1")
    assert store.saved == ["ws_1"]
    assert cards == [teams._card(_catalog("channel_space_switched_title", "fr-FR"), _catalog("teams_space_switched", "fr-FR"))]


async def test_teams_space_switch_receipt_uses_the_selected_space_default(monkeypatch: pytest.MonkeyPatch) -> None:
    cards: list[dict[str, Any]] = []

    async def _send(_request: Request, _activity: dict[str, Any], _connection: Any, card: dict[str, Any]) -> None:
        cards.append(card)

    monkeypatch.setattr(teams, "_send_activity", _send)
    state = SimpleNamespace(
        teams_store=_TeamsStore(), user_store=_UserStore(), user_settings_store=_Settings({}), workspace_settings_store=_WorkspaceSettings()
    )
    activity = {
        "value": {"action": {"type": "Action.Execute", "verb": "bowhead.workspace.select", "data": {"workspace_id": "ws_1"}}},
        "from": {"aadObjectId": "oid_1"},
        "conversation": {"id": "conv_1"},
    }
    await teams._handle_workspace_selection(_json_request(SimpleNamespace(state=state), {}), activity, SimpleNamespace(team_id="team_1"), "tenant_1")
    assert cards == [teams._card(_catalog("channel_space_switched_title", "zh-CN"), _catalog("teams_space_switched", "zh-CN"))]


# ── Routine delivery reconciliation ──────────────────────────────────────────


class _TerminalStore:
    def __init__(self, locale: str | None) -> None:
        self.locale = locale
        self.calls: list[tuple[str, str]] = []

    async def read_persisted_turn_presentation_locale(self, *, team_id: str, turn_id: str) -> str:
        self.calls.append((team_id, turn_id))
        if self.locale is None:
            raise LookupError("not a protocol turn")
        return self.locale


async def test_reconcile_delivery_prefers_the_persisted_turn_locale() -> None:
    job = SimpleNamespace(user_id="u_1", query="看 GMV", workspace_id="ws_x")
    store = _TerminalStore("fr-FR")
    app = SimpleNamespace(state=SimpleNamespace(user_settings_store=_Settings({}), terminal_protocol_store=store))
    assert await _scheduled_runner._delivery_locale(app, job, team_id="team_1", turn_id="t_1") == "fr-FR"
    assert store.calls == [("team_1", "t_1")]
    app.state.terminal_protocol_store = _TerminalStore(None)
    assert await _scheduled_runner._delivery_locale(app, job, team_id="team_1", turn_id="t_1") == "zh-CN"
    # The run's in-memory snapshot still wins over the persisted value.
    app.state.terminal_protocol_store = _TerminalStore("fr-FR")
    snapshot = LanguageContext(ui_language="de-DE")
    assert await _scheduled_runner._delivery_locale(app, job, snapshot, team_id="team_1", turn_id="t_1") == "de-DE"
