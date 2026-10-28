"""Sprint ω Phase 5 P1-3 fix — admin metrics rollup endpoint UT.

Covers:
- 403 for non-admin caller (require_team_admin enforcement)
- 200 + correct rollup shape for admin caller
- 503 when metrics_log unset (degraded mode)
- high-cardinality attrs (turn_id, session_id) excluded from rollup key
"""

from __future__ import annotations

import aiosqlite
import httpx2
import pytest
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_store import UserStore, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test_admin_metrics_secret_xxx")
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    db = tmp_path / "metrics.db"
    sess = SessionStore(str(db))
    await sess.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    metrics_log = MetricsLog(str(db))
    await metrics_log.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sess
    app.state.user_store = user_store
    app.state.metrics_log = metrics_log
    app.state.canvas_store = None
    app.state.orchestrator = None
    return app, user_store, metrics_log


async def _seed_team(store: UserStore, team_id: str = "team_a") -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Test Team", "test.com", 10, "ultra", 100_000, 100_000, "{}", "active", _now()),
        )
        await db.commit()


async def _seed_user(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    email: str | None = None,
    is_creator: bool = False,
) -> None:
    email = email or f"{user_id}@test.com"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, '', '[]', 'ultra', 30000, 30000, ?, 'zh-CN', 'UTC', '{}', 'active', ?)",
            (user_id, team_id, email, _now(), _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


async def _make_jwt(user_id: str, team_id: str = "team_a") -> str:
    import time

    import jwt

    payload = {"sub": user_id, "team_id": team_id, "iat": int(time.time()), "exp": int(time.time()) + 3600}
    return jwt.encode(payload, "test_admin_metrics_secret_xxx", algorithm="HS256")


async def _client(app, user_id: str = "u_admin", team_id: str = "team_a"):
    transport = httpx2.ASGITransport(app=app)
    c = httpx2.AsyncClient(transport=transport, base_url="http://gw")
    token = await _make_jwt(user_id, team_id)
    c.cookies.set("bowhead_jwt", token)
    return c


@pytest.mark.integration
async def test_metrics_rollup_admin_200(app_ctx) -> None:
    """Admin user can query rollup; aggregates by attrs excluding high-card keys."""
    app, store, metrics_log = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    # Seed 3 events: 2 with kind=chat, 1 with kind=canvas; turn_id should be
    # excluded from the by_attrs key (high-cardinality).
    await metrics_log.track(
        "response_kind_scanner_decision",
        attrs={"kind": "chat", "match_mode": "clean", "turn_id": "t_aaa"},
    )
    await metrics_log.track(
        "response_kind_scanner_decision",
        attrs={"kind": "chat", "match_mode": "clean", "turn_id": "t_bbb"},
    )
    await metrics_log.track(
        "response_kind_scanner_decision",
        attrs={"kind": "canvas", "match_mode": "missing_open", "turn_id": "t_ccc"},
    )

    async with await _client(app) as c:
        r = await c.get(
            "/api/v1/admin/metrics/rollup",
            params={"name": "response_kind_scanner_decision", "hours": 6},
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "response_kind_scanner_decision"
    assert data["total"] == 3
    # by_attrs should have 2 keys (turn_id stripped):
    #   "kind=chat,match_mode=clean": 2
    #   "kind=canvas,match_mode=missing_open": 1
    assert data["by_attrs"]["kind=chat,match_mode=clean"] == 2
    assert data["by_attrs"]["kind=canvas,match_mode=missing_open"] == 1
    assert len(data["by_attrs"]) == 2


@pytest.mark.integration
async def test_metrics_rollup_403_for_non_admin(app_ctx) -> None:
    """Non-admin user blocked by require_team_admin — Phase 5 P0-SRE-2 invariant."""
    app, store, _ = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_user")
    async with await _client(app, user_id="u_user") as c:
        r = await c.get(
            "/api/v1/admin/metrics/rollup",
            params={"name": "response_kind_scanner_decision"},
        )
    assert r.status_code == 403, r.text


@pytest.mark.integration
async def test_metrics_rollup_503_when_metrics_log_unset(app_ctx) -> None:
    """If app.state.metrics_log is None (graceful degrade) → 503."""
    app, store, _ = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    app.state.metrics_log = None
    async with await _client(app) as c:
        r = await c.get(
            "/api/v1/admin/metrics/rollup",
            params={"name": "response_kind_scanner_decision"},
        )
    assert r.status_code == 503, r.text
