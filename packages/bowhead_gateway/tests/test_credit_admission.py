"""Credit 准入：一份判据，所有会起模型工作的入口共用。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_gateway.credit_admission import check_credit_admission, credit_denial_code
from bowhead_gateway.routes import slack as slack_routes
from bowhead_gateway.routes import teams as teams_routes

from bowhead_gateway import turn_service


class _Store:
    def __init__(self, *, user_balance: int = 1_000, team_balance: int = 100_000) -> None:
        self.user_balance = user_balance
        self.team_balance = team_balance
        self.events: list[dict[str, Any]] = []

    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(id=user_id, email="a@test.com", credit_balance=self.user_balance, credit_quota=1_000)

    async def get_team(self, team_id: str) -> Any:
        return SimpleNamespace(id=team_id, team_credit_balance=self.team_balance, team_credit_quota=100_000)

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        return {"id": workspace_id, "datarc_workspace_id": "dp_1"}

    async def record_credit_exhaustion_event(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": "cex_test", **kwargs}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("user_balance", "team_balance", "expected"),
    [
        (1, 1, None),
        (0, 1_000, "user_credit_exhausted"),
        (-50, 1_000, "user_credit_exhausted"),
        (1_000, 0, "team_pool_exhausted"),
        (-5, -5, "user_credit_exhausted"),
    ],
)
def test_credit_denial_code_blocks_at_zero_or_below(user_balance: int, team_balance: int, expected: str | None) -> None:
    user = SimpleNamespace(credit_balance=user_balance)
    team = SimpleNamespace(team_credit_balance=team_balance)
    assert credit_denial_code(user, team) == expected


@pytest.mark.unit
def test_credit_denial_code_does_not_block_without_user_or_team() -> None:
    assert credit_denial_code(None, SimpleNamespace(team_credit_balance=0)) is None
    assert credit_denial_code(SimpleNamespace(credit_balance=0), None) is None


async def test_check_credit_admission_records_only_real_blocks() -> None:
    store = _Store(user_balance=0)
    assert (
        await check_credit_admission(
            store,
            team_id="team_a",
            user_id="u_1",
            workspace_id="ws_1",
            metrics_log=None,
            turn_id="t_1",
        )
        == "user_credit_exhausted"
    )
    assert [(e["code"], e["turn_id"], e["credit_balance"]) for e in store.events] == [("user_credit_exhausted", "t_1", 0)]

    store.user_balance = 10
    assert await check_credit_admission(store, team_id="team_a", user_id="u_1", workspace_id="ws_1", metrics_log=None) is None
    assert len(store.events) == 1
    assert await check_credit_admission(store, team_id=None, user_id="u_1", workspace_id="ws_1", metrics_log=None) is None
    assert await check_credit_admission(None, team_id="team_a", user_id="u_1", workspace_id="ws_1", metrics_log=None) is None


class _ReachedRuntime(Exception):
    """准入之后的下一步：到了这里说明没被 credit 拦。"""


async def _reached_runtime(*_args: Any, **_kwargs: Any) -> Any:
    raise _ReachedRuntime


@pytest.mark.parametrize(("user_balance", "blocked"), [(0, True), (500, False)])
async def test_slack_turn_blocked_when_credit_exhausted(monkeypatch, user_balance: int, blocked: bool) -> None:
    store = _Store(user_balance=user_balance)
    app = SimpleNamespace(state=SimpleNamespace(orchestrator=object(), user_store=store, metrics_log=None))
    posted: list[str] = []

    async def _post(_connection, _channel, text, _thread_ts) -> None:
        posted.append(text)

    monkeypatch.setattr(slack_routes, "_post", _post)
    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _reached_runtime)
    connection = SimpleNamespace(id="sc_1", team_id="team_a")
    user = SimpleNamespace(id="u_1", email="a@test.com")

    if blocked:
        await slack_routes._start_turn(app, connection, user, "ws_1", "C1", "1.0", "看 GMV")
        assert len(posted) == 1 and "额度已用完" in posted[0]
        assert [e["code"] for e in store.events] == ["user_credit_exhausted"]
    else:
        with pytest.raises(_ReachedRuntime):
            await slack_routes._start_turn(app, connection, user, "ws_1", "C1", "1.0", "看 GMV")
        assert posted == []


@pytest.mark.parametrize(("team_balance", "blocked"), [(0, True), (500, False)])
async def test_teams_turn_blocked_when_credit_exhausted(monkeypatch, team_balance: int, blocked: bool) -> None:
    store = _Store(team_balance=team_balance)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=object(), user_store=store, metrics_log=None)))
    cards: list[dict[str, Any]] = []

    async def _send_activity(_request, _activity, _connection, card) -> None:
        cards.append(card)

    monkeypatch.setattr(teams_routes, "_send_activity", _send_activity)
    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _reached_runtime)
    kwargs = {
        "request": request,
        "activity": {},
        "connection": SimpleNamespace(id="tc_1", entra_tenant_id="tenant_a"),
        "user_id": "u_1",
        "team_id": "team_a",
        "workspace_id": "ws_1",
        "text": "看 GMV",
    }

    if blocked:
        await teams_routes._start_turn(**kwargs)
        assert len(cards) == 1 and "团队的可用额度已用完" in str(cards[0])
        assert [e["code"] for e in store.events] == ["team_pool_exhausted"]
    else:
        with pytest.raises(_ReachedRuntime):
            await teams_routes._start_turn(**kwargs)
        assert cards == []
