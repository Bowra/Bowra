"""credential-readiness R9：kb 能力状态成员可读端点。

`/knowledge` 现场提示的数据源——必须对普通成员可读（onboarding status 是
admin-gated），且只回四态枚举、绝不泄露 provider/密钥。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import team_kb_capabilities as tkc
from bowhead_gateway.routes.team_kb_capabilities import kb_capabilities, kb_ingestion_readiness
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_SECRET = "secret-that-must-not-leak"


def _config(*, with_rerank: bool = False) -> dict:
    kb_models: dict = {
        "embedding": {
            "provider": "primary",
            "capability": "embed",
            "real_id": "embed-real-id",
            "protocol": "openai_compat",
            "dim": 1536,
        },
    }
    active: dict = {"embed": "embedding"}
    if with_rerank:
        kb_models["rerank_m"] = {
            "provider": "primary",
            "capability": "rerank",
            "real_id": "rerank-1",
            "protocol": "openai_compat",
        }
        active["rerank"] = "rerank_m"
    return {
        "schema_version": 2,
        "providers": {
            "primary": {
                "type": "openai_compatible",
                "base_url": "https://models.example.com/v1",
                "api_key": _SECRET,
                "models": [{"id": "agent-main", "capabilities": ["chat", "tool"]}],
            },
        },
        "tiers": {"primary": {"provider": "primary", "model": "agent-main"}},
        "kb": {"models": kb_models, "active": active, "index_version": "idx_1"},
    }


class _Store:
    def __init__(self, config: dict | None) -> None:
        self.config = config

    async def get_team_llm_config_with_version(self, _team_id: str):
        return self.config, 1


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(routing_config_loaded=None)))


def _user(team_id: str = "team_1") -> SimpleNamespace:
    return SimpleNamespace(id="u_member", team_id=team_id)


@pytest.mark.asyncio
async def test_no_team_is_403() -> None:
    with pytest.raises(HTTPException) as exc:
        await kb_capabilities(_request(), user=_user(team_id=""), store=_Store(None))  # type: ignore[arg-type]
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_no_config_reports_all_disabled() -> None:
    res = await kb_capabilities(_request(), user=_user(), store=_Store(None))  # type: ignore[arg-type]
    assert res == {
        "capabilities": {
            "kb_rerank": "disabled",
            "kb_vision": "disabled",
            "kb_row_extractor": "disabled",
        },
    }


def test_member_can_read_through_real_route(tmp_path) -> None:
    """审查 P2（[[feedback_verify_at_the_gate_not_around_it]]）：R9 的存在理由就是
    「onboarding status admin-gated 而成员看不到」——「member 经真实路由+前缀拿 200」
    必须穿过挂载与依赖链验证，函数直调证不了闸的松紧。"""

    def _run(coro):
        return asyncio.run(coro)

    users = UserStore(str(tmp_path / "u.db"))
    _run(users.initialize())
    team = _run(users.create_team(name="Acme", max_seats=5))
    member = _run(users.create_user(team_id=team.id, email="m@x.com", password="Passw0rd!"))
    assert _run(users.get_team_role(member.id, team.id)) not in {"owner", "team_admin"}

    app = FastAPI()
    app.include_router(tkc.router, prefix="/api/v1/team", tags=["team"])
    app.state.user_store = users
    app.state.routing_config_loaded = None
    app.dependency_overrides[tkc._current_user] = lambda: member

    resp = TestClient(app).get("/api/v1/team/kb-capabilities")
    assert resp.status_code == 200
    assert set(resp.json()["capabilities"]) == {"kb_rerank", "kb_vision", "kb_row_extractor"}
    readiness = TestClient(app).get("/api/v1/team/kb-ingestion-readiness")
    assert readiness.status_code == 200
    assert readiness.json() == {"state": "missing"}


@pytest.mark.asyncio
async def test_reports_states_without_leaking_config() -> None:
    res = await kb_capabilities(_request(), user=_user(), store=_Store(_config(with_rerank=True)))  # type: ignore[arg-type]
    caps = res["capabilities"]
    assert set(caps) == {"kb_rerank", "kb_vision", "kb_row_extractor"}
    assert caps["kb_rerank"] == "enabled"
    assert caps["kb_vision"] == "disabled"
    assert caps["kb_row_extractor"] == "disabled"
    # Return only four-state enum; no provider/base_url/keys.
    blob = json.dumps(res)
    assert _SECRET not in blob
    assert "models.example.com" not in blob


@pytest.mark.asyncio
async def test_ingestion_readiness_requires_an_active_runtime_without_disclosing_models() -> None:
    request = _request()
    config = _config()
    request.app.state.team_kb_runtime_registry = SimpleNamespace(
        get=lambda *_args: asyncio.sleep(0, result=SimpleNamespace(worker=object())),
    )
    ready = await kb_ingestion_readiness(request, user=_user(), store=_Store(config))  # type: ignore[arg-type]
    assert ready == {"state": "ready"}
    assert _SECRET not in json.dumps(ready)

    request.app.state.team_kb_runtime_registry = SimpleNamespace(
        get=lambda *_args: asyncio.sleep(0, result=None),
    )
    unavailable = await kb_ingestion_readiness(request, user=_user(), store=_Store(config))  # type: ignore[arg-type]
    assert unavailable == {"state": "unavailable"}

    config["kb"]["active"] = {"rerank": "embedding"}
    missing = await kb_ingestion_readiness(request, user=_user(), store=_Store(config))  # type: ignore[arg-type]
    assert missing == {"state": "missing"}
