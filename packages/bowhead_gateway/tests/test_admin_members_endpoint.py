"""Sprint β G5 — admin members + promote/demote endpoint tests."""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.public_entries import PublicEntry, PublicEntryStore
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.user_store import (
    MemberDeactivationResult,
    TeamOwnerTransferError,
    UserStore,
    WorkspaceTeamMismatchError,
    _now,
    hash_password,
)
from bowhead_gateway.public_entry_context import (
    public_access_context_for_entry,
    reset_public_access_context,
    set_public_access_context,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def member_schema(tmp_path_factory):
    from bowhead_cli.migrate import upgrade

    path = tmp_path_factory.mktemp("member-account-schema") / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return path


@pytest.fixture
async def store(tmp_path, member_schema):
    from bowhead_core.account_deletion_ledger import AccountDeletionLedger

    path = tmp_path / "g5.db"
    with sqlite3.connect(member_schema) as source, sqlite3.connect(path) as target:
        source.backup(target)
    s = UserStore(path)
    ledger = AccountDeletionLedger(tmp_path / "retirements.db", secret="synthetic-member-test-root")
    await ledger.initialize()
    s.account_deletion_recorder = ledger.record
    await s.initialize()
    await TeamSubagentStore(s.db_path).initialize()
    await AccessControlStore(s.db_path).initialize()
    return s


async def _seed_team(s, team_id="team_g5"):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'Team', 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()


async def _seed_user(s, user_id, email, *, team_id="team_g5", is_creator=False):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        # After multi-team migration, authorization requires active team_members rows (same as production backfill).
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'member', 'active', ?)",
            (f"om_{user_id}", user_id, team_id, _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


async def _seed_workspace(s, workspace_id="ws_people", *, team_id="team_g5"):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO workspaces(id, team_id, name, status, created_at, updated_at) VALUES (?, ?, 'People Space', 'active', ?, ?)",
            (workspace_id, team_id, _now(), _now()),
        )
        await db.commit()


async def _seed_public_entry(s, *, team_id: str = "team_g5") -> PublicEntry:
    registry = PublicEntryStore(s.db_path)
    await registry.initialize()
    return await registry.create_bootstrap_active(
        team_id=team_id,
        base_url=f"https://{team_id.replace('_', '-')}.example.test",
        actor_id="u_creator",
    )


async def _seed_team_invite(
    s,
    invite_id: str,
    *,
    email: str = "pending@t.com",
) -> str:
    entry = await _seed_public_entry(s)
    recorded_id = await s.record_team_invite(
        team_id="team_g5",
        team_role="member",
        invited_email=email,
        raw_token=f"token-{invite_id}",
        expires_at="2999-01-01T00:00:00+00:00",
        invited_by="u_creator",
        invite_id=invite_id,
    )
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "UPDATE team_invite_tokens SET public_entry_id=? WHERE id=?",
            (entry.id, recorded_id),
        )
        await db.commit()
    return recorded_id


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
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    from bowhead_gateway.routes import admin_members as admin_members_routes
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import invite_activation as invite_activation_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(invite_activation_routes.router, prefix="/api/v1/auth")
    a.include_router(admin_members_routes.router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── Direct creation tests ────────────────────────────────


@pytest.mark.asyncio
async def test_owner_invites_local_member_with_password_setup_link(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)

    response = client.post(
        "/api/v1/admin/members",
        json={"email": "new@t.com", "display_name": "New Member"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "new@t.com"
    assert body["display_name"] == "New Member"
    assert body["method"] == "created"
    assert body["recovery_path"].startswith("/reset-password?flow=admin&token=")
    assert body["recovery_expires_at"] is not None
    assert response.headers["cache-control"] == "no-store"

    user = await store.get_user_by_email("new@t.com")
    assert user is not None
    assert user.password_hash is None
    token = body["recovery_path"].split("token=", 1)[1]
    recovery = await store.get_credential_recovery_token(token)
    assert recovery is not None and recovery["purpose"] == "password_reset"

    listed = client.get(
        "/api/v1/admin/members",
        cookies={"bowhead_jwt": jwt},
    )
    assert listed.status_code == 200
    assert all("initial_password" not in item for item in listed.json()["items"])


@pytest.mark.asyncio
async def test_local_member_invite_does_not_recreate_existing_member(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "member@t.com")

    response = client.post(
        "/api/v1/admin/members",
        json={"email": "member@t.com", "display_name": "Ignored"},
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 201, response.text
    assert response.json()["method"] == "already_member"
    assert response.json()["recovery_path"] is None
    assert await store.count_active_seats("team_g5") == 2


@pytest.mark.asyncio
async def test_local_member_invite_replaces_deleted_identity(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_dirty", "dirty@t.com")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET status='deleted' WHERE id='u_dirty'")
        await db.execute("UPDATE team_members SET status='removed' WHERE user_id='u_dirty'")
        await db.commit()

    response = client.post(
        "/api/v1/admin/members",
        json={"email": "dirty@t.com", "display_name": "Replacement"},
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 201, response.text
    assert response.json()["method"] == "created"
    assert response.json()["id"] != "u_dirty"
    assert response.json()["display_name"] == "Replacement"
    original = await store.get_user("u_dirty")
    assert original is not None
    assert original.status == "deleted"
    assert original.email == "u_dirty@deleted.invalid"
    token = response.json()["recovery_path"].split("token=", 1)[1]
    recovery = await store.get_credential_recovery_token(token)
    assert recovery is not None and recovery["purpose"] == "password_reset"


@pytest.mark.asyncio
async def test_local_member_creation_requires_non_blank_display_name(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)

    response = client.post(
        "/api/v1/admin/members",
        json={"email": "new@t.com", "display_name": "   "},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, display name is required"
    assert await store.get_user_by_email("new@t.com") is None


@pytest.mark.asyncio
async def test_direct_creation_returns_stable_mode_errors(
    client,
    app,
    store,
    monkeypatch,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)

    app.state.deployment_mode = "saas"
    cloud_mode = client.post(
        "/api/v1/admin/members",
        json={"email": "new@t.com", "display_name": "New Member"},
        cookies={"bowhead_jwt": jwt},
    )
    assert cloud_mode.status_code == 409
    assert cloud_mode.json()["detail"] == {"code": "local_only"}


@pytest.mark.asyncio
async def test_direct_creation_denies_corrupt_identity_before_handler(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE team_members SET role='team_admin' WHERE user_id='u_admin'",
        )
        await db.execute("DELETE FROM teams WHERE id='team_g5'")
        await db.commit()

    response = client.post(
        "/api/v1/admin/members",
        json={"email": "new@t.com", "display_name": "New Member"},
        cookies={"bowhead_jwt": _jwt("u_admin", SECRET)},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "insufficient_role",
        "required": ["owner", "team_admin"],
        "your_role": "member",
    }
    assert await store.get_user_by_email("new@t.com") is None


@pytest.mark.asyncio
async def test_direct_creation_converts_unknown_integrity_conflict(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)

    async def fail_create_user(**_kwargs):
        raise aiosqlite.IntegrityError("internal constraint detail")

    monkeypatch.setattr(store, "create_user", fail_create_user)
    response = client.post(
        "/api/v1/admin/members",
        json={"email": "new@t.com", "display_name": "New Member"},
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "member_create_conflict"}
    assert "internal constraint detail" not in response.text


# ─── Owner transfer tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_owner_transfers_ownership_to_active_team_member(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_target", "target@t.com")
    jwt = _jwt("u_creator", SECRET)

    response = client.post(
        "/api/v1/admin/members/owner-transfer",
        json={"target_user_id": "u_target"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "owner_user_id": "u_target"}
    assert await store.get_team_role("u_target", "team_g5") == "owner"
    assert await store.get_team_role("u_creator", "team_g5") == "team_admin"


@pytest.mark.asyncio
async def test_owner_transfer_rejects_current_owner(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)

    response = client.post(
        "/api/v1/admin/members/owner-transfer",
        json={"target_user_id": "u_creator"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "owner_transfer_same_user"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target_state", ["missing", "inactive", "other_team"])
async def test_owner_transfer_rejects_non_active_team_target(
    client,
    store,
    target_state,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    if target_state == "inactive":
        await _seed_user(store, "u_target", "target@t.com")
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE users SET status='inactive' WHERE id='u_target'")
            await db.commit()
    elif target_state == "other_team":
        await _seed_team(store, team_id="team_other")
        await _seed_user(
            store,
            "u_target",
            "target@t.com",
            team_id="team_other",
        )
    jwt = _jwt("u_creator", SECRET)

    response = client.post(
        "/api/v1/admin/members/owner-transfer",
        json={"target_user_id": "u_target"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "owner_transfer_target_not_active",
    }
    assert "target is not an active team member" not in response.text


@pytest.mark.asyncio
async def test_owner_transfer_returns_stable_conflict_code(
    client,
    store,
    monkeypatch,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_target", "target@t.com")
    jwt = _jwt("u_creator", SECRET)

    async def conflict(*args, **kwargs):
        raise TeamOwnerTransferError("owner_transfer_conflict")

    monkeypatch.setattr(store, "transfer_team_owner", conflict)
    response = client.post(
        "/api/v1/admin/members/owner-transfer",
        json={"target_user_id": "u_target"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "owner_transfer_conflict"}
    assert "owner transfer conflict" not in response.text


# ─── Promote tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_promotes_member_to_team_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_member/role",
        json={"new_role": "team_admin", "reason": "需要他管理"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "changed"
    assert body["new_role"] == "team_admin"
    # Verify in DB
    assert await store.get_team_role("u_member", "team_g5") == "team_admin"


@pytest.mark.asyncio
async def test_member_cannot_promote_others(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_user(store, "u_target", "t@t.com")
    jwt = _jwt("u_member", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_target/role",
        json={"new_role": "team_admin"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "insufficient_role"


@pytest.mark.asyncio
async def test_audit_admin_cannot_promote(client, store):
    """audit_admin role read-only on member mgmt."""
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_auditor", "b@t.com")
    await store.promote_team_role(
        user_id="u_auditor",
        team_id="team_g5",
        new_role="audit_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_auditor", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_creator/role",
        json={"new_role": "member"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_team_admin_cannot_change_team_roles(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_other_admin", "a@t.com")
    await store.promote_team_role(
        user_id="u_other_admin",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    # A team_admin can manage members but cannot grant or revoke tenant-wide roles.
    jwt = _jwt("u_other_admin", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_creator/role",
        json={"new_role": "member"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_owner_cannot_change_owner_role(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_creator/role",
        json={"new_role": "member"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "owner_immutable"}


@pytest.mark.asyncio
async def test_cross_team_promote_blocked(client, store):
    """Cannot promote user from another team."""
    await _seed_team(store, team_id="team_A")
    await _seed_team(store, team_id="team_B")
    await _seed_user(store, "u_A_creator", "ca@t.com", team_id="team_A", is_creator=True)
    await _seed_user(store, "u_B_user", "ub@t.com", team_id="team_B")
    jwt = _jwt("u_A_creator", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_B_user/role",
        json={"new_role": "team_admin"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "cross_team_target_not_allowed"


@pytest.mark.asyncio
async def test_team_admin_cannot_self_demote(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_admin", "a@t.com")
    await store.promote_team_role(
        user_id="u_admin",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    # A team_admin cannot alter any team membership role, including their own.
    jwt = _jwt("u_admin", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_admin/role",
        json={"new_role": "member"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("new_role", ["audit_admin"])
async def test_team_admin_cannot_change_self_to_non_editor(client, store, new_role):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_admin", "a@t.com")
    await store.promote_team_role(
        user_id="u_admin",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_admin", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_admin/role",
        json={"new_role": new_role},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403
    assert await store.get_team_role("u_admin", "team_g5") == "team_admin"


@pytest.mark.asyncio
async def test_team_admin_cannot_self_demote_with_peer_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_admin1", "a1@t.com")
    await _seed_user(store, "u_admin2", "a2@t.com")
    await store.promote_team_role(
        user_id="u_admin1",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    await store.promote_team_role(
        user_id="u_admin2",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    # A peer admin does not grant role-changing authority.
    jwt = _jwt("u_admin1", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_admin1/role",
        json={"new_role": "member"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403
    assert await store.get_team_role("u_admin1", "team_g5") == "team_admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_role", ["audit_admin"])
async def test_team_admin_cannot_change_self_with_peer_admin(client, store, new_role):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_admin1", "a1@t.com")
    await _seed_user(store, "u_admin2", "a2@t.com")
    await store.promote_team_role(
        user_id="u_admin1",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    await store.promote_team_role(
        user_id="u_admin2",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_admin1", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_admin1/role",
        json={"new_role": new_role},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403
    assert await store.get_team_role("u_admin1", "team_g5") == "team_admin"


@pytest.mark.asyncio
async def test_promote_writes_audit_row(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_t", "target@t.com")
    jwt = _jwt("u_creator", SECRET)
    client.post(
        "/api/v1/admin/members/u_t/role",
        json={"new_role": "audit_admin", "reason": "财务"},
        cookies={"bowhead_jwt": jwt},
    )
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT action, payload_json FROM admin_audit_log WHERE action='role_change' ORDER BY ts DESC LIMIT 1") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    import json

    pl = json.loads(row[1])
    assert pl["new_role"] == "audit_admin"
    assert pl["reason"] == "财务"
    assert pl["target_email_mask"] == "t***@t.com"


@pytest.mark.asyncio
async def test_promote_noop_same_role(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_t", "t@t.com")
    await store.promote_team_role(
        user_id="u_t",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_t/role",
        json={"new_role": "team_admin"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    assert r.json()["method"] == "noop"


@pytest.mark.asyncio
async def test_target_user_not_found_404(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/members/u_does_not_exist/role",
        json={"new_role": "team_admin"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "target_user_not_found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"new_role": "owner"},
        {"new_role": "member", "reason": "x" * 501},
    ],
)
async def test_change_role_rejects_invalid_fields_without_mutation(
    client,
    store,
    payload,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_target", "target@t.com")
    jwt = _jwt("u_creator", SECRET)

    response = client.post(
        "/api/v1/admin/members/u_target/role",
        json=payload,
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert await store.get_team_role("u_target", "team_g5") == "member"


# ─── List members tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_list_members_owner_view(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_admin", "a@t.com")
    await _seed_user(store, "u_member", "m@t.com")
    await store.promote_team_role(
        user_id="u_admin",
        team_id="team_g5",
        new_role="team_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_creator", SECRET)
    r = client.get("/api/v1/admin/members", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner_id"] == "u_creator"
    assert body["your_role"] == "owner"
    assert len(body["items"]) == 3
    roles = {it["user_id"]: it["team_role"] for it in body["items"]}
    assert roles["u_creator"] == "owner"
    assert roles["u_admin"] == "team_admin"
    assert roles["u_member"] == "member"


@pytest.mark.asyncio
async def test_list_members_search_role_and_pagination(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "owner@t.com", is_creator=True)
    await _seed_user(store, "u_alice", "alice@t.com")
    await _seed_user(store, "u_bob", "bob@t.com")
    await store.promote_team_role(user_id="u_alice", team_id="team_g5", new_role="team_admin", promoted_by="u_creator")
    jwt = _jwt("u_creator", SECRET)
    first = client.get(
        "/api/v1/admin/members",
        params={"page": 1, "size": 1},
        cookies={"bowhead_jwt": jwt},
    )
    second = client.get(
        "/api/v1/admin/members",
        params={"page": 2, "size": 1},
        cookies={"bowhead_jwt": jwt},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 3
    assert first.json()["non_owner_team_admin_count"] == 1
    assert first.json()["items"][0]["user_id"] != second.json()["items"][0]["user_id"]
    searched = client.get(
        "/api/v1/admin/members",
        params={"page": 1, "size": 25, "q": "ALICE", "role": "team_admin"},
        cookies={"bowhead_jwt": jwt},
    )
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["email"] == "alice@t.com"
    literal_wildcard = client.get(
        "/api/v1/admin/members",
        params={"page": 1, "q": "%"},
        cookies={"bowhead_jwt": jwt},
    )
    assert literal_wildcard.json()["total"] == 0


@pytest.mark.asyncio
async def test_invites_search_and_pagination(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "owner@t.com", is_creator=True)
    await _seed_team_invite(store, "oinv_alice", email="alice@t.com")
    await _seed_team_invite(store, "oinv_bob", email="bob@t.com")
    jwt = _jwt("u_creator", SECRET)
    page = client.get(
        "/api/v1/admin/members/invites",
        params={"page": 1, "size": 1},
        cookies={"bowhead_jwt": jwt},
    )
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert len(page.json()["invites"]) == 1
    searched = client.get(
        "/api/v1/admin/members/invites",
        params={"page": 1, "q": "BOB"},
        cookies={"bowhead_jwt": jwt},
    )
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["invites"][0]["invited_email"] == "bob@t.com"


@pytest.mark.asyncio
async def test_list_members_member_403(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_m", "m@t.com")
    jwt = _jwt("u_m", SECRET)
    r = client.get("/api/v1/admin/members", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_members_audit_admin_sees_all_with_role(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_audit", "au@t.com")
    await store.promote_team_role(
        user_id="u_audit",
        team_id="team_g5",
        new_role="audit_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_audit", SECRET)
    r = client.get("/api/v1/admin/members", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200
    body = r.json()
    assert body["your_role"] == "audit_admin"


@pytest.mark.asyncio
async def test_team_invite_registers_member_without_workspace_grant(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_workspace(store, "ws_people")
    await store.add_member(workspace_id="ws_people", user_id="u_creator", role="admin")
    entry = await _seed_public_entry(store)

    jwt = _jwt("u_creator", SECRET)
    context_token = set_public_access_context(
        public_access_context_for_entry(entry),
    )
    try:
        invite = client.post(
            "/api/v1/admin/members/invite",
            json={"email": "newperson@example.com"},
            cookies={"bowhead_jwt": jwt},
        )
    finally:
        reset_public_access_context(context_token)
    assert invite.status_code == 200, invite.text
    invite_url = invite.json()["invite_url"]
    assert invite_url.startswith(entry.base_url)
    token = invite_url.split("token=", 1)[1]

    headers = {
        "Origin": entry.base_url,
        "Content-Type": "application/json",
    }
    context_token = set_public_access_context(
        public_access_context_for_entry(entry),
    )
    try:
        with TestClient(client.app, base_url=entry.base_url) as browser:
            exchanged = browser.post(
                "/api/v1/auth/invites/intent",
                json={"token": token},
                headers=headers,
            )
            assert browser.cookies.get("bowhead_invite_intent")
            activated = browser.post(
                "/api/v1/auth/invites/activate",
                json={
                    "password": "StrongPass123!",
                    "display_name": "New Person",
                },
                headers=headers,
            )
    finally:
        reset_public_access_context(context_token)
    assert exchanged.status_code == 200, exchanged.text
    assert activated.status_code == 200, activated.text
    user = await store.get_user_by_email("newperson@example.com")
    assert user is not None
    assert user.team_id == "team_g5"
    assert await store.get_member_role("ws_people", user.id) is None
    assert await store.list_team_invites("team_g5") == []


@pytest.mark.asyncio
async def test_team_invite_without_public_entry_leaves_no_pending_invite(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)

    response = client.post(
        "/api/v1/admin/members/invite",
        json={"email": "newperson@example.com"},
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "public_entry_required"
    assert await store.list_team_invites("team_g5") == []


@pytest.mark.asyncio
async def test_expired_team_invites_do_not_block_new_invite(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await store.record_team_invite(
        team_id="team_g5",
        team_role="member",
        invited_email="expired@example.com",
        raw_token="expired-token",
        expires_at="2000-01-01T00:00:00+00:00",
        invited_by="u_creator",
    )
    assert await store.list_team_invites("team_g5") == []
    entry = await _seed_public_entry(store)

    jwt = _jwt("u_creator", SECRET)
    context_token = set_public_access_context(
        public_access_context_for_entry(entry),
    )
    try:
        invite = client.post(
            "/api/v1/admin/members/invite",
            json={"email": "expired@example.com"},
            cookies={"bowhead_jwt": jwt},
        )
    finally:
        reset_public_access_context(context_token)
    assert invite.status_code == 200, invite.text
    assert invite.json()["method"] == "email_invite"


@pytest.mark.asyncio
async def test_team_invite_returns_stable_policy_and_capacity_errors(
    client,
    store,
    monkeypatch,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    feature_disabled = client.post(
        "/api/v1/admin/members/invite",
        json={"email": "new@t.com"},
        cookies={"bowhead_jwt": jwt},
    )
    assert feature_disabled.status_code == 409
    assert feature_disabled.json()["detail"] == {
        "code": "feature_disabled_use_direct_create",
    }

    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE teams SET max_seats=1 WHERE id='team_g5'")
        await db.commit()
    seat_full = client.post(
        "/api/v1/admin/members/invite",
        json={"email": "new@t.com"},
        cookies={"bowhead_jwt": jwt},
    )
    assert seat_full.status_code == 409
    assert seat_full.json()["detail"] == {"code": "seat_full"}


@pytest.mark.asyncio
async def test_team_invite_hides_cross_team_recipient(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "0")
    await _seed_team(store)
    await _seed_team(store, "team_other")
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(
        store,
        "u_other",
        "other@t.com",
        team_id="team_other",
    )

    response = client.post(
        "/api/v1/admin/members/invite",
        json={"email": "other@t.com"},
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "user_not_found"}
    assert "team_other" not in response.text


@pytest.mark.asyncio
async def test_team_invite_resend_and_revoke_return_not_found(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    cookies = {"bowhead_jwt": _jwt("u_creator", SECRET)}

    resend = client.post(
        "/api/v1/admin/members/invites/oinv_missing/resend",
        cookies=cookies,
    )
    revoke = client.delete(
        "/api/v1/admin/members/invites/oinv_missing",
        cookies=cookies,
    )

    assert resend.status_code == 404
    assert resend.json()["detail"] == {"code": "invite_not_found"}
    assert revoke.status_code == 404
    assert revoke.json()["detail"] == {"code": "invite_not_found"}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("revoked", "invite_status_revoked"),
        ("consumed", "invite_status_consumed"),
        ("satisfied", "invite_status_satisfied"),
        ("expired", "invite_status_expired"),
        ("unexpected", "invite_not_reissuable"),
    ],
)
@pytest.mark.asyncio
async def test_team_invite_resend_returns_stable_terminal_status_code(
    client,
    store,
    status,
    code,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    invite_id = await _seed_team_invite(store, f"oinv_{status}")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE team_invite_tokens SET status=? WHERE id=?",
            (status, invite_id),
        )
        await db.commit()

    response = client.post(
        f"/api/v1/admin/members/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": code}


@pytest.mark.asyncio
async def test_team_invite_resend_returns_stable_race_error(
    client,
    store,
    monkeypatch,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    invite_id = await _seed_team_invite(store, "oinv_race")

    async def fail_reissue(*_args, **_kwargs):
        return False

    monkeypatch.setattr(store, "reissue_team_invite_token", fail_reissue)
    response = client.post(
        f"/api/v1/admin/members/invites/{invite_id}/resend",
        cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "invite_not_reissuable"}


@pytest.mark.asyncio
async def test_team_invite_resend_returns_stable_cooldown_error(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    invite_id = await _seed_team_invite(store, "oinv_cooldown")
    cookies = {"bowhead_jwt": _jwt("u_creator", SECRET)}

    first = client.post(
        f"/api/v1/admin/members/invites/{invite_id}/resend",
        cookies=cookies,
    )
    second = client.post(
        f"/api/v1/admin/members/invites/{invite_id}/resend",
        cookies=cookies,
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == {"code": "resend_cooldown_1min"}


@pytest.mark.asyncio
async def test_team_invite_resend_backfills_verified_public_entry(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    invite_id = await _seed_team_invite(store, "oinv_legacy_entry")
    entry = await _seed_public_entry(store)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE team_invite_tokens SET public_entry_id=NULL WHERE id=?",
            (invite_id,),
        )
        await db.commit()

    context_token = set_public_access_context(
        public_access_context_for_entry(entry),
    )
    try:
        response = client.post(
            f"/api/v1/admin/members/invites/{invite_id}/resend",
            cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
        )
    finally:
        reset_public_access_context(context_token)

    assert response.status_code == 200, response.text
    persisted = await store.get_team_invite_by_id(invite_id)
    assert persisted is not None
    assert persisted["public_entry_id"] == entry.id
    assert response.json()["invite_url"].startswith(entry.base_url)


@pytest.mark.asyncio
async def test_workspace_grants_drawer_api_upserts_and_revokes(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    await store.add_member(workspace_id="ws_people", user_id="u_creator", role="admin")
    jwt = _jwt("u_creator", SECRET)

    before = client.get(
        "/api/v1/admin/members/u_member/workspace-grants",
        cookies={"bowhead_jwt": jwt},
    )
    assert before.status_code == 200
    assert before.json()["items"][0]["role"] is None

    upsert = client.put(
        "/api/v1/admin/members/u_member/workspace-grants",
        json={"workspace_id": "ws_people", "role": "viewer"},
        cookies={"bowhead_jwt": jwt},
    )
    assert upsert.status_code == 200, upsert.text
    assert await store.get_member_role("ws_people", "u_member") == "viewer"

    revoke = client.delete(
        "/api/v1/admin/members/u_member/workspace-grants/ws_people",
        cookies={"bowhead_jwt": jwt},
    )
    assert revoke.status_code == 200, revoke.text
    assert await store.get_member_role("ws_people", "u_member") is None


@pytest.mark.asyncio
async def test_archived_workspace_grant_is_readable_but_not_mutable(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    await store.add_member(
        workspace_id="ws_people",
        user_id="u_member",
        role="viewer",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id='ws_people'",
        )
        await db.commit()
    cookies = {"bowhead_jwt": _jwt("u_creator", SECRET)}

    listed = client.get(
        "/api/v1/admin/members/u_member/workspace-grants",
        cookies=cookies,
    )
    upserted = client.put(
        "/api/v1/admin/members/u_member/workspace-grants",
        json={"workspace_id": "ws_people", "role": "member"},
        cookies=cookies,
    )
    revoked = client.delete(
        "/api/v1/admin/members/u_member/workspace-grants/ws_people",
        cookies=cookies,
    )

    assert listed.status_code == 200, listed.text
    archived = next(item for item in listed.json()["items"] if item["workspace_id"] == "ws_people")
    assert archived["status"] == "archived"
    assert archived["role"] == "viewer"
    assert upserted.status_code == 409
    assert upserted.json()["detail"]["code"] == "workspace_archived"
    assert revoked.status_code == 409
    assert revoked.json()["detail"]["code"] == "workspace_archived"
    assert await store.get_member_role("ws_people", "u_member") == "viewer"


@pytest.mark.asyncio
async def test_workspace_grants_upsert_blocks_demoting_sole_workspace_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    await store.add_member(workspace_id="ws_people", user_id="u_member", role="admin")
    jwt = _jwt("u_creator", SECRET)

    demote = client.put(
        "/api/v1/admin/members/u_member/workspace-grants",
        json={"workspace_id": "ws_people", "role": "viewer"},
        cookies={"bowhead_jwt": jwt},
    )
    assert demote.status_code == 422, demote.text
    assert demote.json()["detail"]["code"] == "last_workspace_admin"
    assert await store.get_member_role("ws_people", "u_member") == "admin"


@pytest.mark.asyncio
async def test_workspace_grants_upsert_allows_demoting_workspace_admin_with_peer(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    await store.add_member(workspace_id="ws_people", user_id="u_creator", role="admin")
    await store.add_member(workspace_id="ws_people", user_id="u_member", role="admin")
    jwt = _jwt("u_creator", SECRET)

    demote = client.put(
        "/api/v1/admin/members/u_member/workspace-grants",
        json={"workspace_id": "ws_people", "role": "viewer"},
        cookies={"bowhead_jwt": jwt},
    )
    assert demote.status_code == 200, demote.text
    assert await store.get_member_role("ws_people", "u_member") == "viewer"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize("workspace_state", ["missing", "archived", "other_team"])
async def test_workspace_grant_mutations_reject_unavailable_space(
    client,
    store,
    method,
    workspace_state,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    workspace_id = "ws_unavailable"
    if workspace_state == "archived":
        await _seed_workspace(store, workspace_id)
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute(
                "UPDATE workspaces SET status='archived' WHERE id=?",
                (workspace_id,),
            )
            await db.commit()
    elif workspace_state == "other_team":
        await _seed_team(store, team_id="team_other")
        await _seed_workspace(store, workspace_id, team_id="team_other")
    jwt = _jwt("u_creator", SECRET)
    kwargs = {"json": {"workspace_id": workspace_id, "role": "viewer"}} if method == "PUT" else {}

    response = client.request(
        method,
        ("/api/v1/admin/members/u_member/workspace-grants" if method == "PUT" else f"/api/v1/admin/members/u_member/workspace-grants/{workspace_id}"),
        cookies={"bowhead_jwt": jwt},
        **kwargs,
    )

    expected_status = 409 if workspace_state == "archived" else 404
    expected_code = "workspace_archived" if workspace_state == "archived" else "workspace_not_found"
    assert response.status_code == expected_status
    assert response.json()["detail"] == {"code": expected_code}


@pytest.mark.asyncio
async def test_workspace_grant_upsert_returns_stable_team_mismatch(
    client,
    store,
    monkeypatch,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    jwt = _jwt("u_creator", SECRET)

    async def fail_add_member(**kwargs):
        raise WorkspaceTeamMismatchError

    monkeypatch.setattr(store, "add_member", fail_add_member)
    response = client.put(
        "/api/v1/admin/members/u_member/workspace-grants",
        json={"workspace_id": "ws_people", "role": "viewer"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "workspace_team_mismatch"}
    assert "workspace_members requires active team membership" not in response.text


@pytest.mark.asyncio
async def test_workspace_grant_revoke_returns_not_found_for_missing_or_removed_grant(
    client,
    store,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    jwt = _jwt("u_creator", SECRET)
    path = "/api/v1/admin/members/u_member/workspace-grants/ws_people"

    missing = client.delete(path, cookies={"bowhead_jwt": jwt})
    assert missing.status_code == 404
    assert missing.json()["detail"] == {"code": "grant_not_found"}

    await store.add_member(
        workspace_id="ws_people",
        user_id="u_member",
        role="viewer",
    )
    first = client.delete(path, cookies={"bowhead_jwt": jwt})
    repeated = client.delete(path, cookies={"bowhead_jwt": jwt})

    assert first.status_code == 200
    assert repeated.status_code == 404
    assert repeated.json()["detail"] == {"code": "grant_not_found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"workspace_id": "", "role": "viewer"},
        {"workspace_id": "ws_people", "role": "owner"},
    ],
)
async def test_workspace_grant_upsert_rejects_invalid_fields_without_mutation(
    client,
    store,
    payload,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    jwt = _jwt("u_creator", SECRET)

    response = client.put(
        "/api/v1/admin/members/u_member/workspace-grants",
        json=payload,
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert await store.get_member_role("ws_people", "u_member") is None


@pytest.mark.asyncio
async def test_deactivate_member_removes_from_people_list(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await store.create_credential_recovery_token(
        raw_token="recovery-token-for-deleted-member",
        user_id="u_member",
        team_id="team_g5",
        created_by_user_id="u_creator",
        expires_at="2999-01-01T00:00:00+00:00",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO user_federated_identities(provider,subject,user_id,email_at_link,created_at) "
            "VALUES ('provider','subject','u_member','m@t.com',?)",
            (_now(),),
        )
        await db.commit()
    jwt = _jwt("u_creator", SECRET)

    r = client.post(
        "/api/v1/admin/team/members/u_member/deactivate",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    assert r.json()["identity_deleted"] is True
    deleted = await store.get_user("u_member")
    assert deleted is not None
    assert deleted.status == "deleted"
    assert deleted.email == "u_member@deleted.invalid"
    recovery = await store.get_credential_recovery_token("recovery-token-for-deleted-member")
    assert recovery is not None and recovery["status"] == "revoked"
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT 1 FROM user_federated_identities WHERE user_id='u_member'",
        ) as cur,
    ):
        assert await cur.fetchone() is None
    body = client.get("/api/v1/admin/members", cookies={"bowhead_jwt": jwt}).json()
    assert {item["user_id"] for item in body["items"]} == {"u_creator"}

    replacement = client.post(
        "/api/v1/admin/members",
        json={"email": "m@t.com", "display_name": "Replacement"},
        cookies={"bowhead_jwt": jwt},
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["method"] == "created"
    assert replacement.json()["id"] != "u_member"
    assert replacement.json()["display_name"] == "Replacement"
    replacement_token = replacement.json()["recovery_path"].split("token=", 1)[1]
    replacement_recovery = await store.get_credential_recovery_token(replacement_token)
    assert replacement_recovery is not None
    assert replacement_recovery["user_id"] == replacement.json()["id"]
    assert replacement_recovery["purpose"] == "password_reset"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store_error", "expected_status", "expected_code"),
    [
        ("target_not_found", 404, "target_user_not_found"),
        ("already_inactive", 409, "already_inactive"),
        ("future_internal_state", 400, "deactivate_failed"),
    ],
)
async def test_deactivate_member_normalizes_concurrent_store_failures(
    client,
    store,
    monkeypatch,
    store_error,
    expected_status,
    expected_code,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    jwt = _jwt("u_creator", SECRET)

    async def fail_deactivate(**kwargs):
        return MemberDeactivationResult(False, store_error)

    monkeypatch.setattr(
        store,
        "admin_soft_delete_member_detailed",
        fail_deactivate,
    )
    response = client.post(
        "/api/v1/admin/team/members/u_member/deactivate",
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == expected_status
    assert response.json()["detail"] == {"code": expected_code}
    if store_error == "future_internal_state":
        assert store_error not in response.text


@pytest.mark.asyncio
async def test_deactivate_member_returns_safe_subagent_lifecycle_blocker(
    client,
    store,
    monkeypatch,
    caplog,
):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    jwt = _jwt("u_creator", SECRET)

    async def block_for_subagents(**kwargs):
        return MemberDeactivationResult(
            False,
            "subagent_lifecycle_action_required",
            {
                "enabled_shared": 2,
                "governance_path": "/team/settings/subagents/",
                "unsafe_agent_name": "must-not-leak",
            },
        )

    monkeypatch.setattr(
        store,
        "admin_soft_delete_member_detailed",
        block_for_subagents,
    )
    with caplog.at_level(
        "WARNING",
        logger="bowhead_gateway.routes.admin_members",
    ):
        response = client.post(
            "/api/v1/admin/team/members/u_member/deactivate",
            cookies={"bowhead_jwt": jwt},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "subagent_lifecycle_action_required",
        "enabled_shared": 2,
        "governance_path": "/team/settings/subagents/",
    }
    assert "must-not-leak" not in response.text
    assert "subagent_member_deactivation_blocked" in caplog.text


@pytest.mark.asyncio
async def test_deactivate_member_blocked_when_sole_workspace_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    await store.add_member(workspace_id="ws_people", user_id="u_member", role="admin")
    jwt = _jwt("u_creator", SECRET)

    r = client.post(
        "/api/v1/admin/team/members/u_member/deactivate",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "last_workspace_admin"
    assert await store.get_member_role("ws_people", "u_member") == "admin"


@pytest.mark.asyncio
async def test_deactivate_member_removes_workspace_grants_when_safe(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    await _seed_workspace(store, "ws_people")
    await store.add_member(workspace_id="ws_people", user_id="u_creator", role="admin")
    await store.add_member(workspace_id="ws_people", user_id="u_member", role="viewer")
    jwt = _jwt("u_creator", SECRET)

    r = client.post(
        "/api/v1/admin/team/members/u_member/deactivate",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    assert await store.get_member_role("ws_people", "u_member") is None


@pytest.mark.asyncio
async def test_team_invite_explicit_space_assignment_and_revision_edit(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_workspace(store, "ws_people")
    entry = await _seed_public_entry(store)
    cookies = {"bowhead_jwt": _jwt("u_creator", SECRET)}

    context_token = set_public_access_context(
        public_access_context_for_entry(entry),
    )
    try:
        created = client.post(
            "/api/v1/admin/members/invite",
            json={
                "email": "assigned@example.com",
                "assignment_mode": "spaces",
                "initial_workspace_grants": [
                    {"workspace_id": "ws_people", "role": "viewer"},
                ],
            },
            cookies=cookies,
        )
    finally:
        reset_public_access_context(context_token)

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["method"] == "created"
    assert body["assignment_mode"] == "spaces"
    assert body["assignment_revision"] == 1
    assert body["initial_workspace_grants"][0]["workspace_id"] == "ws_people"

    edited = client.patch(
        f"/api/v1/admin/members/invites/{body['invite_id']}/assignment",
        json={
            "assignment_mode": "team_only",
            "initial_workspace_grants": [],
            "expected_assignment_revision": 1,
        },
        cookies=cookies,
    )
    stale = client.patch(
        f"/api/v1/admin/members/invites/{body['invite_id']}/assignment",
        json={
            "assignment_mode": "team_only",
            "initial_workspace_grants": [],
            "expected_assignment_revision": 1,
        },
        cookies=cookies,
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["invite"]["assignment_mode"] == "team_only"
    assert edited.json()["invite"]["assignment_revision"] == 2
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "stale_invite_assignment",
        "details": {"current_assignment_revision": 2},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["archived", "other_team"])
async def test_team_invite_rejects_initial_grant_to_archived_or_foreign_space(
    client,
    store,
    monkeypatch,
    case,
):
    # After c7a14d057 removed email invitations from space pages, invitations with initial spaces use team invitations only;
    # archived/foreign-team spaces cannot be initial grants (invalid_initial_workspace_grant), and no invitation persists.
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    if case == "other_team":
        await _seed_team(store, team_id="team_other")
        await _seed_workspace(store, "ws_target", team_id="team_other")
    else:
        await _seed_workspace(store, "ws_target")
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE workspaces SET status='archived' WHERE id='ws_target'")
            await db.commit()
    entry = await _seed_public_entry(store)

    context_token = set_public_access_context(
        public_access_context_for_entry(entry),
    )
    try:
        response = client.post(
            "/api/v1/admin/members/invite",
            json={
                "email": "newcomer@example.com",
                "assignment_mode": "spaces",
                "initial_workspace_grants": [{"workspace_id": "ws_target", "role": "member"}],
            },
            cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
        )
    finally:
        reset_public_access_context(context_token)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_initial_workspace_grant"
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT COUNT(*) FROM team_invite_tokens") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize("stored_seats", [0, 1, 10**6])
async def test_community_invites_ignore_legacy_seat_column(client, app, store, monkeypatch, stored_seats):
    from bowhead_core.entitlements import CommunityResolver

    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE teams SET max_seats=?", (stored_seats,))
        await db.commit()
    app.state.edition = "community"
    app.state.entitlements = CommunityResolver(mode="local", user_store=store)
    store.set_seat_guard(app.state.entitlements)
    entry = await _seed_public_entry(store)
    context = set_public_access_context(public_access_context_for_entry(entry))
    try:
        response = client.post(
            "/api/v1/admin/members/invite",
            json={"email": "community-member@example.com"},
            cookies={"bowhead_jwt": _jwt("u_creator", SECRET)},
        )
    finally:
        reset_public_access_context(context)
    assert response.status_code == 200, response.text
    assert response.json()["invite_url"].startswith(entry.base_url)


async def test_local_deactivation_missing_privacy_ledger_returns_actionable_failure(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_member", "m@t.com")
    store.account_deletion_recorder = None
    response = client.post("/api/v1/admin/team/members/u_member/deactivate", cookies={"bowhead_jwt": _jwt("u_creator", SECRET)})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "account_deletion_not_ready"
    assert "privacy ledger" in response.json()["detail"]["message"]
    assert (await store.get_user_by_email("m@t.com")).status == "active"
    assert await store.get_team_role("u_member", "team_g5") == "member"
