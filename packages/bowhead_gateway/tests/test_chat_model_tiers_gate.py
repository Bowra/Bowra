"""团队模型配置是聊天的唯一 Provider 来源。"""

from __future__ import annotations

from bowhead_gateway.routes.chat import _reject_unconfigured_team_turn


def test_rejects_only_when_no_team_router() -> None:
    assert _reject_unconfigured_team_turn(team_router_applied=True) is False
    assert _reject_unconfigured_team_turn(team_router_applied=False) is True
