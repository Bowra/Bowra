"""Sprint β G1 — invites/check (7-state) + resend tests."""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.user_store import (
    UserStore,
    WorkspaceTeamMismatchError,
    _now,
    hash_password,
)
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "g1.db")
    await s.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(s.db_path).initialize()
    return s


async def _seed_team(s, team_id="team_g1", domain=None):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'O', ?, 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, domain, _now()),
        )
        await db.commit()
    from bowhead_core.public_entries import PublicEntryStore

    registry = PublicEntryStore(s.db_path)
    await registry.create_bootstrap_active(
        team_id=team_id,
        base_url="http://test.local",
        actor_id="test",
    )


async def _seed_user(s, user_id, email, *, team_id="team_g1"):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        # Authorization after multi-team migration requires active team_members rows (same shape as production backfill).
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'member', 'active', ?)",
            (f"om_{user_id}", user_id, team_id, _now()),
        )
        await db.commit()


def _jwt(user_id, secret, pwd_v=0):
    import time

    import jwt as _jwt

    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": pwd_v},
        secret,
        algorithm="HS256",
    )


SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import (
        PublicAccessContext,
        public_access_context_for_entry,
        reset_public_access_context,
        set_public_access_context,
    )
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import members as members_routes

    auth_routes._RATE_BUCKET.clear()
    auth_routes._AUTO_MEMBER_DOMAINS_CACHE = None
    a = FastAPI()
    a.state.user_store = store
    registry = PublicEntryStore(store.db_path)
    a.state.public_entry_store = registry

    @a.middleware("http")
    async def _public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        entries = await registry.find_active_by_base_url("http://test.local")
        context = (
            public_access_context_for_entry(entries[0], source="direct")
            if len(entries) == 1
            else PublicAccessContext(
                base_url="http://test.local",
                origin="http://test.local",
                base_path="",
                app_root="http://test.local/",
                match_source="direct",
            )
        )
        token = set_public_access_context(context)
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(members_routes.router, prefix="/api/v1/workspaces")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def test_missing_member_store_returns_stable_code():
    from bowhead_gateway.routes.members import _store

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
    )

    with pytest.raises(HTTPException) as caught:
        _store(request)

    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "member_service_unavailable"}


async def _seed_admin_and_ws(store, *, ws_id="ws_1", team_id="team_g1"):
    await _seed_team(store, team_id=team_id, domain="company.com")
    await _seed_user(store, "u_admin", "admin@company.com", team_id=team_id)
    ws = await store.sync_workspace_from_datarc(
        team_id=team_id,
        datarc_workspace_id=ws_id,
        name="ws1",
    )
    await store.add_member(workspace_id=ws, user_id="u_admin", role="admin")
    return ws


# ─── 7-state coverage ──────────────────────────────────────


@pytest.mark.asyncio
async def test_check_state_empty(client, store):
    ws_id = await _seed_admin_and_ws(store)
    jwt = _jwt("u_admin", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "empty"


@pytest.mark.asyncio
async def test_check_state_invalid(client, store):
    ws_id = await _seed_admin_and_ws(store)
    jwt = _jwt("u_admin", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=not-an-email",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.json()["state"] == "invalid"


@pytest.mark.asyncio
async def test_check_state_member(client, store):
    ws_id = await _seed_admin_and_ws(store)
    await _seed_user(store, "u_existing", "user@company.com")
    await store.add_member(workspace_id=ws_id, user_id="u_existing", role="member")
    jwt = _jwt("u_admin", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=user@company.com",
        cookies={"bowhead_jwt": jwt},
    )
    body = r.json()
    assert body["state"] == "member"
    assert body["role"] == "member"


@pytest.mark.asyncio
async def test_check_state_invited(client, store):
    ws_id = await _seed_admin_and_ws(store)
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) + timedelta(days=6, hours=3)).isoformat()
    await store.record_invite(
        workspace_id=ws_id,
        team_id="team_g1",
        role="member",
        invited_email="alice@company.com",
        raw_token="dummy_token_xyz",
        expires_at=exp,
        invited_by="u_admin",
    )
    jwt = _jwt("u_admin", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=alice@company.com",
        cookies={"bowhead_jwt": jwt},
    )
    body = r.json()
    assert body["state"] == "invited"
    assert body["remaining_hours"] in (143, 144, 145, 146, 147)  # roughly 6d3h


@pytest.mark.asyncio
async def test_check_state_domain_whitelist(client, store):
    ws_id = await _seed_admin_and_ws(store)
    jwt = _jwt("u_admin", SECRET)
    # datarc.cn is in BOWHEAD_AUTO_MEMBER_DOMAINS
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=newuser@datarc.cn",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.json()["state"] == "domain"


@pytest.mark.asyncio
async def test_check_state_ok(client, store):
    """Same-domain email, not in ws, not pending, not whitelist."""
    ws_id = await _seed_admin_and_ws(store)
    jwt = _jwt("u_admin", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=newcolleague@company.com",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.json()["state"] == "ok"


@pytest.mark.asyncio
async def test_check_state_ok_foreign(client, store):
    """Different domain than ws's team domain."""
    ws_id = await _seed_admin_and_ws(store)
    jwt = _jwt("u_admin", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=external@partner.io",
        cookies={"bowhead_jwt": jwt},
    )
    body = r.json()
    assert body["state"] == "ok-foreign"
    assert body["ws_domain"] == "company.com"


@pytest.mark.asyncio
async def test_check_requires_admin(client, store):
    """Non-admin → 403."""
    ws_id = await _seed_admin_and_ws(store)
    await _seed_user(store, "u_m", "member@company.com")
    await store.add_member(workspace_id=ws_id, user_id="u_m", role="member")
    jwt = _jwt("u_m", SECRET)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=x@y.com",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_check_rate_limit_returns_stable_code(client, store, monkeypatch):
    ws_id = await _seed_admin_and_ws(store)

    async def deny_rate_limit(*args, **kwargs):
        return False

    monkeypatch.setattr(store, "rate_limit_check", deny_rate_limit)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/check-invite-eligibility?email=x@y.com",
        cookies={"bowhead_jwt": _jwt("u_admin", SECRET)},
    )

    assert r.status_code == 429
    assert r.json()["detail"] == {"code": "too_many_checks"}


# ─── Resend tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_resend_404_when_invite_not_found(client, store):
    ws_id = await _seed_admin_and_ws(store)
    jwt = _jwt("u_admin", SECRET)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/invite_does_not_exist/resend",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "invite_not_found"}


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("revoked", "invite_status_revoked"),
        ("consumed", "invite_status_consumed"),
        ("satisfied", "invite_status_satisfied"),
        ("expired", "invite_status_expired"),
        ("future_status", "invite_not_reissuable"),
    ],
)
@pytest.mark.asyncio
async def test_resend_inactive_status_uses_finite_code(
    client,
    store,
    status,
    expected_code,
):
    ws_id = await _seed_admin_and_ws(store)
    from datetime import datetime, timedelta

    invite_id = await store.record_invite(
        workspace_id=ws_id,
        team_id="team_g1",
        role="member",
        invited_email="inactive@example.com",
        raw_token=f"token-{status}",
        expires_at=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
        invited_by="u_admin",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE invite_tokens SET status = ? WHERE id = ?",
            (status, invite_id),
        )
        await db.commit()

    r = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": _jwt("u_admin", SECRET)},
    )

    assert r.status_code == 409
    assert r.json()["detail"] == {"code": expected_code}


@pytest.mark.asyncio
async def test_revoke_missing_invite_returns_stable_code(client, store):
    ws_id = await _seed_admin_and_ws(store)

    r = client.delete(
        f"/api/v1/workspaces/{ws_id}/invites/inv_missing",
        cookies={"bowhead_jwt": _jwt("u_admin", SECRET)},
    )

    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "invite_not_found"}


@pytest.mark.asyncio
async def test_direct_add_team_mismatch_returns_stable_code(
    client,
    store,
    monkeypatch,
):
    ws_id = await _seed_admin_and_ws(store)
    await _seed_user(store, "u_target", "target@company.com")

    async def reject_add_member(**kwargs):
        raise WorkspaceTeamMismatchError

    monkeypatch.setattr(store, "add_member", reject_add_member)
    # Since c7a14d057, direct addition uses POST /members (team-member user_id), no longer email.
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": "u_target", "role": "member"},
        cookies={"bowhead_jwt": _jwt("u_admin", SECRET)},
    )

    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "workspace_team_mismatch"}


@pytest.mark.asyncio
async def test_resend_enqueues_smtp(client, store, monkeypatch):
    # email-config: resend queues only with SMTP configured (otherwise email_enqueued=False, prompt to copy the link).
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.test")
    ws_id = await _seed_admin_and_ws(store)
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) + timedelta(days=6)).isoformat()
    invite_id = await store.record_invite(
        workspace_id=ws_id,
        team_id="team_g1",
        role="member",
        invited_email="x@y.com",
        raw_token="dummy_token_xyz",
        expires_at=exp,
        invited_by="u_admin",
    )
    jwt = _jwt("u_admin", SECRET)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    # Verify outbox row created
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT COUNT(*) FROM smtp_outbox WHERE to_email='x@y.com' AND purpose='invite_resend'") as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_resend_cooldown_1min(client, store):
    ws_id = await _seed_admin_and_ws(store)
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) + timedelta(days=6)).isoformat()
    invite_id = await store.record_invite(
        workspace_id=ws_id,
        team_id="team_g1",
        role="member",
        invited_email="x@y.com",
        raw_token="dummy_token_xyz",
        expires_at=exp,
        invited_by="u_admin",
    )
    jwt = _jwt("u_admin", SECRET)
    # First resend OK
    r1 = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": jwt},
    )
    assert r1.status_code == 200
    # Second resend within 1 min → 429
    r2 = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": jwt},
    )
    assert r2.status_code == 429
    assert r2.json()["detail"]["code"] == "resend_cooldown_1min"


@pytest.mark.asyncio
async def test_resend_remints_token_fixes_broken_link(client, store, monkeypatch):
    """email-config PRD P0-1: 重发必须重铸 token + URL 用 /invite?token=<jwt>(可消费),
    旧实现发 ?invite_email=(无 token, 两端无人消费→受邀人不进 workspace)。"""
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.test")  # Make email_enqueued=True.
    ws_id = await _seed_admin_and_ws(store)
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) + timedelta(days=6)).isoformat()
    invite_id = await store.record_invite(
        workspace_id=ws_id,
        team_id="team_g1",
        role="member",
        invited_email="x@y.com",
        raw_token="OLD_TOKEN_xyz",
        expires_at=exp,
        invited_by="u_admin",
    )
    jwt = _jwt("u_admin", SECRET)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Response includes a copyable invite_url with a real token (not ?invite_email=).
    assert "/invite?token=" in body["invite_url"]
    assert "invite_email=" not in body["invite_url"]
    assert body["email_enqueued"] is True
    # New token hash active; old stored hash replaced.
    new_token = body["invite_url"].split("?token=", 1)[1]
    assert await store.is_invite_active(new_token) is True
    assert await store.is_invite_active("OLD_TOKEN_xyz") is False
    assert body["email_enqueued"] is True


@pytest.mark.asyncio
async def test_resend_no_smtp_reports_not_enqueued(client, store, monkeypatch):
    """review-fix: 无 SMTP 时 resend 不入队(避免死信)+ email_enqueued=False
    → FE 引导复制链接, 不误显"已发送"。链接仍重铸有效。"""
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    ws_id = await _seed_admin_and_ws(store)
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) + timedelta(days=6)).isoformat()
    invite_id = await store.record_invite(
        workspace_id=ws_id,
        team_id="team_g1",
        role="member",
        invited_email="x@y.com",
        raw_token="OLD2",
        expires_at=exp,
        invited_by="u_admin",
    )
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": _jwt("u_admin", SECRET)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email_enqueued"] is False  # No SMTP → do not enqueue.
    assert "/invite?token=" in body["invite_url"]  # Link still reminted and usable.
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT COUNT(*) FROM smtp_outbox") as cur:
        assert (await cur.fetchone())[0] == 0  # No dead-letter row.
