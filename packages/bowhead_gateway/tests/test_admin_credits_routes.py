"""V3 admin credits routes — integration tests (8 endpoints + 权限).

覆盖:
- 8 endpoint 各 happy path
- require_team_admin 拦普通用户 (403)
- 跨 team 访问 (404)
- grant idempotency
- period reset preview / confirm / cooldown 429
- CSV export 4 scope
"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.credit_admin_store import _reset_for_tests as _reset_cache
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_store import UserStore, _id, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from fastapi import FastAPI

from bowhead_core import SessionStore


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test_secret_credits_admin_e2e_xxx")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    db = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db}", target="core") == ("core",)
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
    await _reset_cache()
    return app, user_store


async def _seed_team(store: UserStore, team_id: str = "team_a", quota: int = 100_000) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Test Team", "test.com", 10, "ultra", quota, quota, '{"pro":5000,"ultra":30000}', "active", _now()),
        )
        await db.commit()


async def _seed_user(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    email: str | None = None,
    tier: str = "ultra",
    balance: int = 30_000,
    quota: int = 30_000,
    status: str = "active",
    is_creator: bool = False,
) -> None:
    email = email or f"{user_id}@test.com"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, '', '[]', ?, ?, ?, ?, 'zh-CN', 'UTC', '{}', ?, ?)",
            (user_id, team_id, email, tier, balance, quota, _now(), status, _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


async def _seed_team_role(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    role: str = "team_admin",
) -> None:
    changed, _ = await store.promote_team_role(
        user_id=user_id,
        team_id=team_id,
        new_role=role,
        promoted_by="u_admin",
    )
    assert changed is True


async def _seed_ledger(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    workspace_id: str = "ws_1",
    delta: int = -100,
    turn_id: str | None = None,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
            "delta, reason, tokens_input, tokens_output, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'turn', 350, 150, ?)",
            (_id("led"), user_id, team_id, workspace_id, turn_id or _id("t"), delta, _now()),
        )
        await db.commit()


async def _make_jwt(user_id: str, team_id: str = "team_a") -> str:
    """生 JWT cookie 用. 跟 auth.py 同 secret 同算法."""
    payload = {"sub": user_id, "team_id": team_id, "iat": int(time.time()), "exp": int(time.time()) + 3600}
    return jwt.encode(payload, "test_secret_credits_admin_e2e_xxx", algorithm="HS256")


async def _admin_client(app, user_id: str = "u_admin", team_id: str = "team_a"):
    """返回带 JWT cookie 的 client (admin)."""
    transport = httpx2.ASGITransport(app=app)
    c = httpx2.AsyncClient(transport=transport, base_url="http://gw")
    token = await _make_jwt(user_id, team_id)
    c.cookies.set("bowhead_jwt", token)
    return c


# ─── happy path ─────────────────────────────────


@pytest.mark.integration
async def test_summary_admin_200(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/summary")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "team" in data and "last_30d" in data and "view_attention" in data


@pytest.mark.integration
async def test_summary_403_for_non_admin(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    # u_admin is creator; u_user is not.
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_user")
    async with await _admin_client(app, user_id="u_user") as c:
        r = await c.get("/api/v1/admin/credits/summary")
        assert r.status_code == 403, r.text


@pytest.mark.integration
async def test_summary_team_admin_200_with_management_actions(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_credit_admin")
    await _seed_team_role(store, user_id="u_credit_admin")
    await _seed_user(store, user_id="u_alice", balance=1_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_low_{i}")
    await store.materialize_credit_attention("team_a")

    async with await _admin_client(app, user_id="u_credit_admin") as c:
        r = await c.get("/api/v1/admin/credits/summary")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["credit_actor"]["team_role"] == "team_admin"
        item = next(i for i in data["activation_attention"] if i["user_id"] == "u_alice")
        assert item["recommended_action"]["key"] == "grant_credit"
        assert item["recommended_action"]["allowed"] is True
        tier_action = next(a for a in item["available_actions"] if a["key"] == "edit_user_tier")
        assert tier_action["allowed"] is True
        async with (
            aiosqlite.connect(store.db_path) as db,
            db.execute("SELECT shown_count FROM credit_attention_state WHERE user_id = 'u_alice'") as cur,
        ):
            shown_once = await cur.fetchone()
        assert shown_once is not None
        assert shown_once[0] == 1

        cached = await c.get("/api/v1/admin/credits/summary")
        assert cached.status_code == 200, cached.text
        async with (
            aiosqlite.connect(store.db_path) as db,
            db.execute("SELECT shown_count FROM credit_attention_state WHERE user_id = 'u_alice'") as cur,
        ):
            shown_twice = await cur.fetchone()
        assert shown_twice is not None
        assert shown_twice[0] == 2


@pytest.mark.integration
async def test_team_admin_can_dismiss_attention_item(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_credit_admin")
    await _seed_team_role(store, user_id="u_credit_admin")
    await _seed_user(store, user_id="u_alice", balance=1_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_attn_{i}")
    await store.materialize_credit_attention("team_a")

    async with await _admin_client(app, user_id="u_credit_admin") as c:
        summary = await c.get("/api/v1/admin/credits/summary")
        assert summary.status_code == 200, summary.text
        item = next(i for i in summary.json()["activation_attention"] if i["user_id"] == "u_alice")
        r = await c.post(
            f"/api/v1/admin/credits/attention/{item['attention_item_id']}/state",
            json={"state": "dismissed", "reason": "handled_offline"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["item"]["state"] == "dismissed"

        repeated = await c.post(
            f"/api/v1/admin/credits/attention/{item['attention_item_id']}/state",
            json={"state": "dismissed"},
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["item"]["state"] == "dismissed"

        summary_after = await c.get("/api/v1/admin/credits/summary")
        assert summary_after.status_code == 200, summary_after.text
        assert all(i["user_id"] != "u_alice" for i in summary_after.json()["activation_attention"])

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT COUNT(*) FROM metrics_event_log WHERE name = 'credit.attention.dismissed'") as cur,
    ):
        metric_count = await cur.fetchone()

    assert metric_count is not None
    assert metric_count[0] == 1


@pytest.mark.integration
async def test_attention_state_rejects_manual_acted_and_protected(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=1_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_manual_state_{i}")
    item = (await store.materialize_credit_attention("team_a"))[0]

    async with await _admin_client(app) as c:
        for state in ("acted", "protected"):
            r = await c.post(
                f"/api/v1/admin/credits/attention/{item['attention_item_id']}/state",
                json={"state": state, "reason": "manual outcome should fail"},
            )
            assert r.status_code == 422, r.text

    current = await store.get_credit_attention_item("team_a", item["attention_item_id"])
    assert current is not None
    assert current["state"] == "open"


@pytest.mark.integration
async def test_attention_action_clicked_tracks_metric(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=1_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_action_click_{i}")
    item = (await store.materialize_credit_attention("team_a"))[0]

    async with await _admin_client(app) as c:
        r = await c.post(
            f"/api/v1/admin/credits/attention/{item['attention_item_id']}/action-clicked",
            json={
                "action_key": "grant_credit",
                "source_surface": "summary",
                "allowed": True,
                "disabled_reason": None,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, attrs_json FROM metrics_event_log WHERE name = 'credit.attention.action_clicked'") as cur:
            metric = await cur.fetchone()

    assert metric is not None
    assert metric["user_id"] == "u_admin"
    assert '"action_key": "grant_credit"' in metric["attrs_json"]
    assert '"source_surface": "summary"' in metric["attrs_json"]


@pytest.mark.integration
async def test_attention_endpoints_return_stable_errors(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=1_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_attention_error_{i}")
    item = (await store.materialize_credit_attention("team_a"))[0]

    async with await _admin_client(app) as c:
        missing = await c.post(
            "/api/v1/admin/credits/attention/attn_missing/action-clicked",
            json={
                "action_key": "grant_credit",
                "source_surface": "summary",
            },
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["detail"] == {"code": "credit_attention_not_found"}

        no_reason = await c.post(
            f"/api/v1/admin/credits/attention/{item['attention_item_id']}/state",
            json={"state": "dismissed"},
        )
        assert no_reason.status_code == 422, no_reason.text
        assert no_reason.json()["detail"] == {
            "code": "credit_attention_reason_required",
        }


@pytest.mark.integration
async def test_users_list_pagination(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    for i in range(15):
        await _seed_user(store, user_id=f"u_{i:02d}")
    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/users?page=1&size=10&sort=email")
        assert r.status_code == 200
        d = r.json()
        assert len(d["items"]) == 10
        assert d["total"] == 16  # 15 + admin


@pytest.mark.integration
async def test_users_list_rejects_invalid_pagination_with_stable_error(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)

    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/users?page=0&size=10")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"code": "invalid_credit_pagination"}


@pytest.mark.integration
async def test_users_list_marks_attention_shown_and_tracks_metric(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=1_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_users_shown_{i}")
    await store.materialize_credit_attention("team_a")

    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/users?page=1&size=20&sort=attention")
        assert r.status_code == 200, r.text
        item = next(i for i in r.json()["items"] if i["user_id"] == "u_alice")
        assert item["activation_attention"]["attention_item_id"]
        assert item["activation_attention"]["expires_at"]

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT shown_count FROM credit_attention_state WHERE user_id = 'u_alice'") as cur:
            shown = await cur.fetchone()
        async with db.execute("SELECT attrs_json FROM metrics_event_log WHERE name = 'credit.attention.item_shown'") as cur:
            metric = await cur.fetchone()

    assert shown is not None
    assert shown["shown_count"] == 1
    assert metric is not None
    assert '"surface": "users"' in metric["attrs_json"]


@pytest.mark.integration
async def test_user_info_uses_exact_user_id_not_fuzzy_email_match(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", email="alice@example.com")
    await _seed_user(store, user_id="u_shadow", email="shadow@example.com")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE users SET display_name = ? WHERE id = 'u_shadow'",
            ("alice@example.com",),
        )
        await db.commit()
    for i in range(3):
        await _seed_ledger(store, user_id="u_shadow", turn_id=f"t_shadow_{i}")
    await store.materialize_credit_attention("team_a")

    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/users/u_alice/info")
        assert r.status_code == 200, r.text
        assert r.json()["user_id"] == "u_alice"


@pytest.mark.integration
async def test_patch_user_changes_tier(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", tier="pro", quota=5000)
    async with await _admin_client(app) as c:
        r = await c.patch(
            "/api/v1/admin/credits/users/u_alice",
            json={"tier": "ultra", "reset_quota_with_tier": True},
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["user"]["tier"] == "ultra"
        assert d["user"]["credit_quota"] == 30_000


@pytest.mark.integration
async def test_patch_user_rejects_empty_noop_body(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice")

    async with await _admin_client(app) as c:
        r = await c.patch(
            "/api/v1/admin/credits/users/u_alice",
            json={"reset_quota_with_tier": True, "pull_balance_to_quota": False},
        )

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == {"code": "credit_changes_required"}


@pytest.mark.integration
async def test_patch_user_same_value_does_not_mark_attention_acted(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=3_000, quota=30_000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_same_value_{i}")
    await store.materialize_credit_attention("team_a")

    async with await _admin_client(app) as c:
        r = await c.patch(
            "/api/v1/admin/credits/users/u_alice",
            json={"credit_quota": 30_000},
        )
        assert r.status_code == 200, r.text

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT state, acted_by FROM credit_attention_state WHERE user_id = 'u_alice'",
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["state"] == "open"
    assert row["acted_by"] is None


@pytest.mark.integration
async def test_patch_user_404_cross_team(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store, "team_a")
    await _seed_team(store, "team_b")
    await _seed_user(store, user_id="u_admin", team_id="team_a", is_creator=True)
    await _seed_user(store, user_id="u_b", team_id="team_b")
    async with await _admin_client(app, user_id="u_admin", team_id="team_a") as c:
        r = await c.patch(
            "/api/v1/admin/credits/users/u_b",
            json={"tier": "pro"},
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == {"code": "member_not_found"}


@pytest.mark.integration
async def test_grant_increments_balance(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=5000)
    for i in range(3):
        await _seed_ledger(store, user_id="u_alice", turn_id=f"t_grant_attn_{i}")
    await store.materialize_credit_attention("team_a")
    async with await _admin_client(app) as c:
        r = await c.post(
            "/api/v1/admin/credits/users/u_alice/grant",
            json={
                "amount": 2000,
                "reason": "test",
                "idempotency_key": "test_key_001_aaaa",
            },
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["new_balance"] == 7000
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT state, acted_by FROM credit_attention_state WHERE user_id = 'u_alice'") as cur:
                item = await cur.fetchone()
        assert item is not None
        assert item["state"] == "acted"
        assert item["acted_by"] == "u_admin"


@pytest.mark.integration
async def test_grant_idempotency_replays_same_result(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=5000)
    async with await _admin_client(app) as c:
        body = {"amount": 2000, "idempotency_key": "dup_key_aaaaaaaa"}
        r1 = await c.post("/api/v1/admin/credits/users/u_alice/grant", json=body)
        r2 = await c.post("/api/v1/admin/credits/users/u_alice/grant", json=body)
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        # Actually debit only once.
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT credit_balance FROM users WHERE id='u_alice'") as cur:
                row = await cur.fetchone()
            assert row["credit_balance"] == 7000  # One-time grant.


@pytest.mark.integration
async def test_grant_rejects_removed_user(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store, quota=10_000)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_removed", balance=0, status="removed")

    async with await _admin_client(app) as c:
        r = await c.post(
            "/api/v1/admin/credits/users/u_removed/grant",
            json={"amount": 1000, "idempotency_key": "removed_grant_001"},
        )

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": "credit_user_inactive"}
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_balance FROM users WHERE id = 'u_removed'") as cur:
            user_row = await cur.fetchone()
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id = 'team_a'") as cur:
            team_row = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) AS n FROM credit_ledger WHERE user_id = 'u_removed'") as cur:
            ledger_row = await cur.fetchone()

    assert user_row["credit_balance"] == 0
    assert team_row["team_credit_balance"] == 10_000
    assert ledger_row["n"] == 0


@pytest.mark.integration
async def test_grant_rejects_insufficient_team_pool_with_stable_error(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store, quota=500)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=0)

    async with await _admin_client(app) as c:
        r = await c.post(
            "/api/v1/admin/credits/users/u_alice/grant",
            json={"amount": 1_000, "idempotency_key": "pool_shortage_001"},
        )

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": "credit_team_pool_insufficient"}


@pytest.mark.integration
async def test_period_reset_preview(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=5000, quota=30000)
    async with await _admin_client(app) as c:
        r = await c.post(
            "/api/v1/admin/credits/period-reset",
            json={"mode": "balance_only", "confirm": False},
        )
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "preview" in d
        # Actually unchanged.
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT credit_balance FROM users WHERE id='u_alice'") as cur:
                assert (await cur.fetchone())["credit_balance"] == 5000


@pytest.mark.integration
async def test_period_reset_confirm_then_cooldown_429(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=5000, quota=30000)
    async with await _admin_client(app) as c:
        r1 = await c.post(
            "/api/v1/admin/credits/period-reset",
            json={"mode": "balance_only", "confirm": True},
        )
        assert r1.status_code == 200
        assert r1.json()["ok"] is True
        # Immediate second attempt → 429.
        r2 = await c.post(
            "/api/v1/admin/credits/period-reset",
            json={"mode": "balance_only", "confirm": True},
        )
        assert r2.status_code == 429
        assert r2.json()["detail"] == {"code": "credit_period_reset_cooldown"}
        assert "retry-after" in {k.lower() for k in r2.headers}


@pytest.mark.integration
async def test_user_ledger_drilldown(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=10_000)
    async with aiosqlite.connect(store.db_path) as db:
        ledger_ids = []
        for i in range(3):
            ledger_id = _id(f"led{i}")
            ledger_ids.append(ledger_id)
            await db.execute(
                "INSERT INTO credit_ledger(id, user_id, team_id, workspace_id, turn_id, "
                "delta, reason, tokens_input, tokens_output, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ledger_id, "u_alice", "team_a", "ws_1", f"t_{i}", -100, "turn", 100, 50, _now()),
            )
        await db.execute(
            "INSERT INTO credit_model_usage(ledger_id,line_no,provider,model,tokens_input,tokens_cached,tokens_output,"
            "rate_input,rate_cached,rate_output,weighted_tokens) VALUES(?,1,'openai','model-a',80,20,50,'2','0.5','3','320')",
            (ledger_ids[0],),
        )
        await db.commit()
    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/users/u_alice/ledger")
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 3
        assert all(item["type"] == "turn" for item in d["items"])
        by_id = {item["id"]: item for item in d["items"]}
        assert by_id[ledger_ids[0]]["model_usage"][0]["model"] == "model-a"
        assert by_id[ledger_ids[0]]["model_usage"][0]["tokens_cached"] == 20
        assert all(by_id[ledger_id]["model_usage"] == [] for ledger_id in ledger_ids[1:])
        exported = await c.get("/api/v1/admin/credits/export.csv?scope=user_ledger&user_id=u_alice")
        assert exported.status_code == 200
        assert "model_usage_json" in exported.text.splitlines()[0]
        assert "model-a" in exported.text


@pytest.mark.integration
async def test_audit_log_returns_credit_admin_events(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice", balance=0)
    async with await _admin_client(app) as c:
        # Trigger grant + set.
        await c.post(
            "/api/v1/admin/credits/users/u_alice/grant",
            json={"amount": 1000, "idempotency_key": "k1aaaaaaaaaa"},
        )
        await c.patch(
            "/api/v1/admin/credits/users/u_alice",
            json={"credit_balance": 2000},
        )
        r = await c.get("/api/v1/admin/credits/audit-log")
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 2
        types = sorted([i["event_type"] for i in d["items"]])
        assert types == ["credit_admin_grant", "credit_admin_set_user"]


@pytest.mark.integration
async def test_export_csv_summary(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/export.csv?scope=summary")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers.get("content-disposition", "")
        body = r.text
        assert "metric,value" in body
        assert "team_balance" in body


@pytest.mark.integration
async def test_export_csv_users(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_alice")
    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/export.csv?scope=users")
        assert r.status_code == 200
        body = r.text
        assert "user_id,email" in body
        assert "u_alice@test.com" in body


@pytest.mark.integration
async def test_export_csv_user_ledger_requires_user_id(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    async with await _admin_client(app) as c:
        r = await c.get("/api/v1/admin/credits/export.csv?scope=user_ledger")
        assert r.status_code == 422
        assert r.json()["detail"] == {"code": "credit_user_id_required"}


@pytest.mark.integration
async def test_community_credit_export_preserves_billing_and_rejects_business_scope(
    app_ctx: tuple[FastAPI, UserStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bowhead_gateway import edition_extensions

    manifest = tmp_path / "community.json"
    manifest.write_text('{"schema_version": 1, "edition": "community"}')
    monkeypatch.setattr(edition_extensions, "BUILD_MANIFEST_PATH", manifest)
    app, store = app_ctx
    app.state.gateway_extensions = edition_extensions.load_gateway_extensions()
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    async with await _admin_client(app) as client:
        summary = await client.get("/api/v1/admin/credits/export.csv?scope=summary")
        assert summary.status_code == 200, summary.text
        assert "team_balance" in summary.text
        audit = await client.get("/api/v1/admin/credits/export.csv?scope=audit")
        assert audit.status_code == 404
        assert audit.json()["detail"] == {"code": "feature_unavailable"}


# ─── permission edge cases ────────────────────────


@pytest.mark.integration
async def test_no_jwt_returns_401(app_ctx) -> None:
    app, _ = app_ctx
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/admin/credits/summary")
        assert r.status_code == 401


@pytest.mark.integration
async def test_invalid_jwt_returns_401(app_ctx) -> None:
    app, _ = app_ctx
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        c.cookies.set("bowhead_jwt", "not_a_real_token")
        r = await c.get("/api/v1/admin/credits/summary")
        assert r.status_code == 401


@pytest.mark.integration
async def test_non_admin_blocked_on_all_endpoints(app_ctx) -> None:
    """普通 user 触每个 endpoint 都 403."""
    app, store = app_ctx
    await _seed_team(store)
    await _seed_user(store, user_id="u_admin", is_creator=True)
    await _seed_user(store, user_id="u_normal")
    async with await _admin_client(app, user_id="u_normal") as c:
        cases = [
            ("GET", "/api/v1/admin/credits/summary"),
            ("GET", "/api/v1/admin/credits/users"),
            ("PATCH", "/api/v1/admin/credits/users/u_admin"),
            ("POST", "/api/v1/admin/credits/users/u_admin/grant"),
            ("POST", "/api/v1/admin/credits/period-reset"),
            ("POST", "/api/v1/admin/credits/attention/attn_missing/state"),
            ("GET", "/api/v1/admin/credits/users/u_admin/ledger"),
            ("GET", "/api/v1/admin/credits/audit-log"),
            ("GET", "/api/v1/admin/credits/export.csv?scope=summary"),
        ]
        for method, path in cases:
            r = await c.request(
                method,
                path,
                json={"amount": 1, "idempotency_key": "x" * 10}
                if "grant" in path
                else (
                    {"mode": "balance_only"}
                    if "period-reset" in path
                    else ({"state": "dismissed"} if "attention" in path else ({"tier": "pro"} if method == "PATCH" else None))
                ),
            )
            assert r.status_code == 403, f"{method} {path} → {r.status_code}: {r.text}"
