"""V4 Sprint 1 PRD-D-4-4 · Memory pack replay route integration tests.

Covers:
  USER PATH (GET /api/v1/memory/turns/{tid}/memory-pack-snapshot):
    - 200 own turn
    - 404 cross-user (opaque — never reveal turn exists for someone else)
    - 404 unknown turn_id
    - 503 store not configured

  ADMIN PATH (GET /api/v1/admin/turns/{tid}/memory-pack-snapshot):
    - 200 same-team turn
    - 204 accessible turn with no snapshot
    - 403 non-admin
    - 404 cross-team turn (admin from team_b can't see team_a's turn)
    - 404 NULL team_id orphan turn (scheduled / dev / no-team)
    - 404 admin without team_id
    - audit log row written on success
"""

from __future__ import annotations

import time

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_core.memory_pack_snapshots import MemoryPackSnapshotsStore
from bowhead_core.user_store import UserStore, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore

_JWT_SECRET = "test_secret_replay_routes_xxx"


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _JWT_SECRET)
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)

    db = tmp_path / "replay.db"
    sess = SessionStore(str(db))
    await sess.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    snaps = MemoryPackSnapshotsStore(str(db))
    await snaps.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sess
    app.state.user_store = user_store
    app.state.canvas_store = None
    app.state.orchestrator = None
    app.state.memory_pack_snapshots_store = snaps
    return app, user_store, snaps


async def _seed_team(store: UserStore, team_id: str = "team_a") -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Team", "x.com", 10, "ultra", 100000, 100000, '{"ultra":30000}', "active", _now()),
        )
        await db.commit()


async def _seed_user(
    store: UserStore,
    *,
    user_id: str,
    team_id: str = "team_a",
    is_creator: bool = False,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, '', '[]', 'ultra', 30000, 30000, ?, "
            "'zh-CN', 'UTC', '{}', 'active', ?)",
            (user_id, team_id, f"{user_id}@x.com", _now(), _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


def _jwt(user_id: str, team_id: str | None = "team_a") -> str:
    payload = {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600}
    if team_id:
        payload["team_id"] = team_id
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


async def _client(app, user_id: str, team_id: str | None = "team_a"):
    transport = httpx2.ASGITransport(app=app)
    c = httpx2.AsyncClient(transport=transport, base_url="http://gw")
    c.cookies.set("bowhead_jwt", _jwt(user_id, team_id))
    return c


async def _save_snapshot(
    snaps: MemoryPackSnapshotsStore,
    *,
    turn_id: str,
    user_id: str,
    team_id: str | None,
    workspace_id: str | None = "ws1",
    content: str = "memory pack content",
) -> None:
    await snaps.save(
        turn_id=turn_id,
        user_id=user_id,
        team_id=team_id,
        workspace_id=workspace_id,
        content=content,
        sections_breakdown={"notes": 12, "entries": 0},
        pack_version="v2",
    )


# ─── USER PATH ──────────────────────────────────────────


@pytest.mark.integration
async def test_user_replay_200_own_turn(app_ctx):
    app, ustore, snaps = app_ctx
    await _seed_team(ustore)
    await _seed_user(ustore, user_id="u1")
    await _save_snapshot(
        snaps,
        turn_id="t_own",
        user_id="u1",
        team_id="team_a",
    )
    async with await _client(app, "u1") as c:
        r = await c.get("/api/v1/memory/turns/t_own/memory-pack-snapshot")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["turn_id"] == "t_own"
    assert d["user_id"] == "u1"
    assert d["content"] == "memory pack content"
    assert d["pack_version"] == "v2"
    assert d["sections_breakdown"] == {"notes": 12, "entries": 0}


@pytest.mark.integration
async def test_user_replay_404_cross_user(app_ctx):
    """User A queries user B's turn → opaque 404 (never leak existence)."""
    app, ustore, snaps = app_ctx
    await _seed_team(ustore)
    await _seed_user(ustore, user_id="u_a")
    await _seed_user(ustore, user_id="u_b")
    await _save_snapshot(
        snaps,
        turn_id="t_b_owned",
        user_id="u_b",
        team_id="team_a",
    )
    async with await _client(app, "u_a") as c:
        r = await c.get("/api/v1/memory/turns/t_b_owned/memory-pack-snapshot")
    assert r.status_code == 404


@pytest.mark.integration
async def test_user_replay_404_unknown_turn(app_ctx):
    app, ustore, _snaps = app_ctx
    await _seed_team(ustore)
    await _seed_user(ustore, user_id="u1")
    async with await _client(app, "u1") as c:
        r = await c.get("/api/v1/memory/turns/t_nonexistent/memory-pack-snapshot")
    assert r.status_code == 404


@pytest.mark.integration
async def test_user_replay_503_when_store_not_configured(app_ctx):
    app, ustore, _snaps = app_ctx
    await _seed_team(ustore)
    await _seed_user(ustore, user_id="u1")
    app.state.memory_pack_snapshots_store = None
    async with await _client(app, "u1") as c:
        r = await c.get("/api/v1/memory/turns/t_x/memory-pack-snapshot")
    assert r.status_code == 503


# ─── ADMIN PATH ─────────────────────────────────────────


@pytest.mark.integration
async def test_admin_replay_200_same_team(app_ctx):
    app, ustore, snaps = app_ctx
    await _seed_team(ustore, "team_a")
    await _seed_user(ustore, user_id="u_admin", team_id="team_a", is_creator=True)
    await _seed_user(ustore, user_id="u_member", team_id="team_a")
    await _save_snapshot(
        snaps,
        turn_id="t_m1",
        user_id="u_member",
        team_id="team_a",
    )
    async with await _client(app, "u_admin", team_id="team_a") as c:
        r = await c.get("/api/v1/admin/turns/t_m1/memory-pack-snapshot")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["user_id"] == "u_member"


@pytest.mark.integration
async def test_admin_replay_204_when_accessible_turn_has_no_snapshot(app_ctx):
    app, ustore, _snaps = app_ctx
    await _seed_team(ustore, "team_a")
    await _seed_team(ustore, "team_b")
    await _seed_user(ustore, user_id="u_admin", team_id="team_a", is_creator=True)
    await _seed_user(ustore, user_id="u_member", team_id="team_a")
    await _seed_user(ustore, user_id="u_other", team_id="team_b")
    async with aiosqlite.connect(ustore.db_path) as db:
        await db.executemany(
            "INSERT INTO workspaces(id, team_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("ws_replay", "team_a", "Replay", _now(), _now()),
                ("ws_other", "team_b", "Other", _now(), _now()),
            ],
        )
        await db.commit()
    await app.state.session_store.ensure_session(session_id="s_without_snapshot", workspace_id="ws_replay", user_id="u_member")
    await app.state.session_store.ensure_session(session_id="s_other", workspace_id="ws_other", user_id="u_other")
    async with aiosqlite.connect(ustore.db_path) as db:
        await db.executemany(
            "INSERT INTO turns(id, session_id, seq, user_text, started_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("t_without_snapshot", "s_without_snapshot", 1, "question", _now()),
                ("t_other", "s_other", 1, "question", _now()),
            ],
        )
        await db.commit()
    async with await _client(app, "u_admin", team_id="team_a") as c:
        r = await c.get("/api/v1/admin/turns/t_without_snapshot/memory-pack-snapshot")
        unknown = await c.get("/api/v1/admin/turns/t_unknown/memory-pack-snapshot")
        cross_team = await c.get("/api/v1/admin/turns/t_other/memory-pack-snapshot")
    assert r.status_code == 204
    assert r.content == b""
    assert unknown.status_code == 404
    assert cross_team.status_code == 404


@pytest.mark.integration
async def test_admin_replay_403_non_admin(app_ctx):
    """Plain member (not creator) can't access admin endpoint."""
    app, ustore, snaps = app_ctx
    await _seed_team(ustore, "team_a")
    await _seed_user(ustore, user_id="u_admin", team_id="team_a", is_creator=True)
    await _seed_user(ustore, user_id="u_member", team_id="team_a")
    await _save_snapshot(
        snaps,
        turn_id="t_x",
        user_id="u_member",
        team_id="team_a",
    )
    async with await _client(app, "u_member", team_id="team_a") as c:
        r = await c.get("/api/v1/admin/turns/t_x/memory-pack-snapshot")
    assert r.status_code == 403


@pytest.mark.integration
async def test_admin_replay_404_cross_team(app_ctx):
    """Admin from team_a can't see team_b's turns."""
    app, ustore, snaps = app_ctx
    await _seed_team(ustore, "team_a")
    await _seed_team(ustore, "team_b")
    await _seed_user(ustore, user_id="u_admin_a", team_id="team_a", is_creator=True)
    await _seed_user(ustore, user_id="u_b_member", team_id="team_b")
    await _save_snapshot(
        snaps,
        turn_id="t_b1",
        user_id="u_b_member",
        team_id="team_b",
    )
    async with await _client(app, "u_admin_a", team_id="team_a") as c:
        r = await c.get("/api/v1/admin/turns/t_b1/memory-pack-snapshot")
    assert r.status_code == 404


@pytest.mark.integration
async def test_admin_replay_404_orphan_null_team(app_ctx):
    """NULL team_id rows (scheduled / dev / no-team) NOT in any admin scope."""
    app, ustore, snaps = app_ctx
    await _seed_team(ustore, "team_a")
    await _seed_user(ustore, user_id="u_admin", team_id="team_a", is_creator=True)
    # Orphan turn (NULL team_id)
    await _save_snapshot(
        snaps,
        turn_id="t_orphan",
        user_id="u_anon",
        team_id=None,
    )
    async with await _client(app, "u_admin", team_id="team_a") as c:
        r = await c.get("/api/v1/admin/turns/t_orphan/memory-pack-snapshot")
    assert r.status_code == 404


@pytest.mark.integration
async def test_admin_replay_writes_audit_log(app_ctx):
    """Side effect: 'admin.replay.viewed' audit row written on success."""
    app, ustore, snaps = app_ctx
    await _seed_team(ustore, "team_a")
    await _seed_user(ustore, user_id="u_admin", team_id="team_a", is_creator=True)
    await _seed_user(ustore, user_id="u_member", team_id="team_a")
    await _save_snapshot(
        snaps,
        turn_id="t_audited",
        user_id="u_member",
        team_id="team_a",
    )
    async with await _client(app, "u_admin", team_id="team_a") as c:
        r = await c.get("/api/v1/admin/turns/t_audited/memory-pack-snapshot")
    assert r.status_code == 200
    # Verify audit row exists
    async with aiosqlite.connect(ustore.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT event_type, user_id, payload_json FROM user_audit_log WHERE event_type='admin.replay.viewed' AND user_id=?",
            ("u_admin",),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["event_type"] == "admin.replay.viewed"
    import json as _json

    payload = _json.loads(row["payload_json"] or "{}")
    assert payload.get("turn_id") == "t_audited"
    assert payload.get("target_user_id") == "u_member"
