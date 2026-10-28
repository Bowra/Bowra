"""Answer style follows account settings into the turn's user profile.

The account settings allowlist and chat profile projection must agree.
Request overrides take precedence, followed by saved account settings;
browser-local language state is not an answer-style preference store.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.routes.chat import ChatSendRequest, UserProfileHint

pytestmark = pytest.mark.unit

log = logging.getLogger(__name__)


class _UserSettings:
    def __init__(self, kv: dict[str, str]) -> None:
        self._kv = kv

    async def list_for_user(self, _user_id: str) -> dict[str, str]:
        return dict(self._kv)


class _WorkspaceSettings:
    def __init__(self, kv: dict[str, str] | None = None) -> None:
        self._kv = kv or {}

    async def list_for_workspace(self, _workspace_id: str) -> dict[str, str]:
        return dict(self._kv)


def _request(user_settings: dict[str, str], workspace_settings: dict[str, str] | None = None) -> Any:
    """最小 request 替身：只带这条路径真正读的两个 store + headers。

    `team_id=None` ⇒ 团队语言策略直接返回 {}；`session_id=None` ⇒ 不读上一轮语言。
    """
    state = SimpleNamespace(
        user_settings_store=_UserSettings(user_settings),
        workspace_settings_store=_WorkspaceSettings(workspace_settings),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state), headers={}, cookies={})


async def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_settings: dict[str, str],
    user_profile: UserProfileHint | None = None,
) -> dict[str, str] | None:
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _request: "user_1")
    body = ChatSendRequest(
        workspace_id="ws_1",
        message="今年各区域销量如何",
        user_profile=user_profile,
    )
    profile_dict, _workspace_settings, _language_context, _paradigm_v2 = await chat_routes._resolve_web_language_inputs(
        _request(user_settings),
        body,
        log=log,
    )
    return profile_dict


async def test_stored_answer_style_reaches_the_turn_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置页存的值必须到达 turn —— 请求本身不带 answer_style（真实前端就是不带）。"""
    profile = await _resolve(monkeypatch, user_settings={"answer_style": "concise"})

    assert profile is not None
    assert profile["answer_style"] == "concise"


async def test_request_scoped_answer_style_wins_over_the_stored_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """请求里带的优先——与语言轴同一套优先级，老客户端的透传不能被账号设置盖掉。"""
    profile = await _resolve(
        monkeypatch,
        user_settings={"answer_style": "concise"},
        user_profile=UserProfileHint(answer_style="technical"),
    )

    assert profile is not None
    assert profile["answer_style"] == "technical"


async def test_no_stored_answer_style_leaves_the_profile_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没设置过就不要凭空造一个默认值——下游按「缺失」处理。"""
    profile = await _resolve(monkeypatch, user_settings={"ui_language": "zh-CN"})

    assert profile is None or "answer_style" not in profile
