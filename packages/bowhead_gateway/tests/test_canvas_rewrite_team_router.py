"""A0-1 (report-edit-audit P0-3) — AI 改写走团队 router 的路由级契约.

背景: routing_mode=team-config 后 `app.state.router` 恒为进程级装配占位
(无 `narrative` 绑定), rewrite 此前直接用它 → prod 上 100% `KeyError` 且以
200+SSE error 形式泄漏异常原文。现在:
- 团队 router 经 team_router_registry 解析(与 chat /send 同路);
- binding 在返回 StreamingResponse **之前**解析 → 配置类错误是 HTTP 4xx/503;
- 绝不回退 app 级 mock router。
"""

from __future__ import annotations

import time

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import CanvasStore

SECRET = "test-secret-long-enough-for-hs256-32b"


def _jwt(user_id: str, team_id: str | None) -> str:
    import jwt as _jwt

    payload: dict = {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0}
    if team_id:
        payload["team_id"] = team_id
    return _jwt.encode(payload, SECRET, algorithm="HS256")


class _FakeStreamRouter:
    """narrative 绑定齐全的团队 router 替身."""

    def resolve(self, agent_type: str, profile=None, user_pref=None):
        if agent_type != "narrative":
            raise KeyError(agent_type)
        return object()

    async def stream(self, agent_type, req, profile=None, user_pref=None):
        from bowhead_router import LLMChunk

        yield LLMChunk(delta="改写结果")


class _NoNarrativeRouter:
    def resolve(self, agent_type: str, profile=None, user_pref=None):
        raise KeyError(f"No binding for agent_type={agent_type!r} profile=None")


class _Registry:
    def __init__(self, router) -> None:
        self._router = router

    async def get(self, team_id, llm_cfg, llm_version):
        return self._router


class _FakeUserStore:
    async def get_team_llm_config_with_version(self, team_id):
        provider = "team"
        model = "team-model"
        binding = {
            "primary": {"provider": provider, "model": model},
            "fallback": [],
        }
        return {
            "schema_version": 2,
            "providers": {
                provider: {
                    "type": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "test-team-secret",
                    "models": [model],
                },
            },
            "agents": {
                "lead": binding,
                "modeler": binding,
                "analyst": {"default": binding, "profiles": {}},
                "narrative": binding,
                "echo": binding,
                "judge": binding,
            },
        }, 1


class _PoisonRouter:
    """app 级 router 替身 — 任何访问即失败, 证明 rewrite 不再碰它."""

    def __getattr__(self, name):  # pragma: no cover - Triggered only by fallback bugs.
        raise AssertionError("rewrite must not touch app.state.router")


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    db = tmp_path / "rw.db"
    store = CanvasStore(db)
    await store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = store
    app.state.orchestrator = None
    app.state.datarc = None
    app.state.router = _PoisonRouter()
    app.state.user_store = _FakeUserStore()

    canvas = await store.ensure_canvas(session_id="s_rw", workspace_id="ws_t", user_id="usr_writer")
    return app, canvas


def _client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


def _payload() -> dict:
    return {"selection_text": "营收提升 [1]", "preset": "concise", "custom_prompt": None}


@pytest.mark.integration
async def test_rewrite_without_team_is_409_unconfigured(ctx) -> None:
    app, canvas = ctx
    async with _client(app) as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/_/rewrite",
            json=_payload(),
            headers={"Authorization": f"Bearer {_jwt('usr_writer', None)}"},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "team_model_unconfigured"


@pytest.mark.integration
async def test_rewrite_team_without_registry_is_409(ctx) -> None:
    app, canvas = ctx
    async with _client(app) as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/_/rewrite",
            json=_payload(),
            headers={"Authorization": f"Bearer {_jwt('usr_writer', 't_1')}"},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "team_model_unconfigured"


@pytest.mark.integration
async def test_rewrite_binding_missing_is_503_before_stream(ctx) -> None:
    app, canvas = ctx
    app.state.team_router_registry = _Registry(_NoNarrativeRouter())
    async with _client(app) as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/_/rewrite",
            json=_payload(),
            headers={"Authorization": f"Bearer {_jwt('usr_writer', 't_1')}"},
        )
    assert r.status_code == 503
    detail = r.json()["detail"]["error"]
    assert detail["code"] == "rewrite_binding_missing"
    # Never expose exception class names.
    assert "KeyError" not in r.text


@pytest.mark.integration
async def test_rewrite_streams_via_team_router_and_never_touches_app_router(ctx) -> None:
    app, canvas = ctx
    app.state.team_router_registry = _Registry(_FakeStreamRouter())
    async with _client(app) as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/_/rewrite",
            json=_payload(),
            headers={"Authorization": f"Bearer {_jwt('usr_writer', 't_1')}"},
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: chunk" in r.text
    assert "改写结果" in r.text
    assert "event: done" in r.text
