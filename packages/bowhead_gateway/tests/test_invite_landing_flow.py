from __future__ import annotations

import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_core.email_sender import make_verify_token
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "test-secret-long-enough-for-hs256-32-bytes"
ROTATED_SECRET = "rotated-test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "invite_landing.db")
    await s.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    registry = PublicEntryStore(s.db_path)
    await registry.initialize()
    await registry.create_bootstrap_active(
        team_id="team_invite",
        base_url="http://test.local",
        actor_id="test",
    )
    return s


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    monkeypatch.delenv("BOWHEAD_REGISTRATION_DISABLED", raising=False)

    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import (
        PublicAccessContext,
        public_access_context_for_entry,
        reset_public_access_context,
        set_public_access_context,
    )
    from bowhead_gateway.routes import admin_members as admin_members_routes
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import members as members_routes
    from bowhead_gateway.routes import oauth as oauth_routes

    auth_routes._RATE_BUCKET.clear()
    auth_routes._AUTO_MEMBER_DOMAINS_CACHE = None

    a = FastAPI()
    a.state.user_store = store
    a.state.deployment_mode = "saas"
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

    class _Settings:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _Settings()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(oauth_routes.router, prefix="/api/v1/auth/oauth")
    a.include_router(members_routes.router, prefix="/api/v1/workspaces")
    a.include_router(admin_members_routes.router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


async def _seed_team(store: UserStore, *, team_id: str = "team_invite", name: str = "Datarc Strategy"):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, name, _now()),
        )
        await db.commit()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(store.db_path).create_bootstrap_active(
        team_id=team_id,
        base_url="http://test.local",
        actor_id="test",
    )


async def _seed_user(
    store: UserStore,
    user_id: str,
    email: str,
    *,
    team_id: str = "team_invite",
    creator: bool = False,
    email_verified: bool = True,
):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, int(email_verified), hash_password("Passw0rd!"), _now()),
        )
        # Authorization after multi-team migration requires active team_members rows (same shape as production backfill).
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'member', 'active', ?)",
            (f"om_{user_id}", user_id, team_id, _now()),
        )
        if creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


async def _seed_admin_workspace(store: UserStore) -> str:
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@datarc.test", creator=True)
    workspace_id = await store.sync_workspace_from_datarc(
        team_id="team_invite",
        datarc_workspace_id="datarc_ws_market",
        name="Market Expansion Lab",
        description="Invite landing test workspace",
    )
    await store.add_member(workspace_id=workspace_id, user_id="u_admin", role="admin")
    return workspace_id


def _jwt(user_id: str, email: str | None = None) -> str:
    payload = {
        "sub": user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "pwd_v": 0,
    }
    if email:
        payload["email"] = email
        payload["team_id"] = "team_invite"
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _token_from_url(invite_url: str) -> str:
    parsed = urlparse(invite_url)
    assert parsed.path == "/invite"
    token = parse_qs(parsed.query).get("token", [""])[0]
    assert token
    return token


def _signed_workspace_invite(
    *,
    invite_id: str,
    workspace_id: str,
    email: str,
    role: str = "member",
) -> tuple[str, str]:
    now_ts = int(time.time())
    exp_ts = now_ts + 7 * 86400
    token = jwt.encode(
        {
            "scope": "workspace",
            "invite_id": invite_id,
            "workspace_id": workspace_id,
            "role": role,
            "invited_email": email.lower().strip(),
            "iat": now_ts,
            "exp": exp_ts,
        },
        SECRET,
        algorithm="HS256",
    )
    return token, datetime.fromtimestamp(exp_ts, UTC).isoformat()


def _signed_team_invite(
    *,
    invite_id: str,
    team_id: str,
    email: str,
    team_role: str = "member",
) -> tuple[str, str]:
    now_ts = int(time.time())
    exp_ts = now_ts + 7 * 86400
    token = jwt.encode(
        {
            "scope": "team",
            "invite_id": invite_id,
            "team_id": team_id,
            "team_role": team_role,
            "invited_email": email.lower().strip(),
            "iat": now_ts,
            "exp": exp_ts,
        },
        SECRET,
        algorithm="HS256",
    )
    return token, datetime.fromtimestamp(exp_ts, UTC).isoformat()


@pytest.mark.asyncio
async def test_workspace_invite_preview_and_register_routes_to_profile_then_ready(client, store):
    workspace_id = await _seed_admin_workspace(store)
    admin_jwt = _jwt("u_admin")

    # c7a14d057 removed email-based invitations from space pages (spaces grant access only to team members); invitations with initial spaces
    # now explicitly declare assignment_mode="spaces" and initial_workspace_grants through team invitations.
    invite = client.post(
        "/api/v1/admin/members/invite",
        json={
            "email": "analyst@datarc.test",
            "assignment_mode": "spaces",
            "initial_workspace_grants": [{"workspace_id": workspace_id, "role": "member"}],
        },
        cookies={"bowhead_jwt": admin_jwt},
    )
    assert invite.status_code == 200, invite.text
    invite_body = invite.json()
    assert invite_body["invite_url"].startswith("http://test.local/invite?token=")
    token = _token_from_url(invite_body["invite_url"])

    payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert payload["scope"] == "team"
    assert payload["invite_id"] == invite_body["invite_id"]
    assert invite_body["assignment_mode"] == "spaces"
    assert len(invite_body["initial_workspace_grants"]) == 1
    assert invite_body["initial_workspace_grants"][0]["workspace_id"] == workspace_id
    assert invite_body["initial_workspace_grants"][0]["role"] == "member"

    preview = client.post("/api/v1/auth/invites/preview", json={"token": token})
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body == {
        "status": "valid",
        "invite_type": "team",
        "invited_email_mask": "a***@datarc.test",
        "expires_at": preview_body["expires_at"],
        "team": {"name": "Datarc Strategy"},
        "viewer_context": {"state": "anonymous"},
    }
    assert "analyst@datarc.test" not in preview.text
    assert "workspace_id" not in preview_body

    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "analyst@datarc.test",
            "password": "Passw0rd!",
            "display_name": "Analyst",
            "invite_token": token,
        },
    )
    assert registered.status_code == 200, registered.text
    registered_body = registered.json()
    user_id = registered_body["user"]["id"]
    assert registered_body["invite_result"]["invite_type"] == "team"
    assert registered_body["invite_result"]["status"] == "accepted"
    assert registered_body["invite_result"]["next_step"] == "profile_setup"
    assert registered_body["invite_result"]["next_route"].startswith(
        "/onboarding/profile?source=invite&next=",
    )
    assert registered_body["invite_result"]["ready_route"] == "/invite/ready?state=workspace_ready"
    assert registered_body["invite_result"]["team"] == {
        "id": "team_invite",
        "name": "Datarc Strategy",
    }
    assert "workspace" not in registered_body["invite_result"]
    assert await store.get_member_role(workspace_id, user_id) == "member"

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT status, consumed_at FROM team_invite_tokens WHERE id = ?",
            (invite_body["invite_id"],),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == "consumed"
    assert row[1]


@pytest.mark.asyncio
async def test_workspace_invite_mints_with_active_rotation_key(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", f"active={ROTATED_SECRET},old={SECRET}")
    workspace_id = await _seed_admin_workspace(store)
    admin_jwt = _jwt("u_admin")

    invite = client.post(
        "/api/v1/admin/members/invite",
        json={
            "email": "rotation@datarc.test",
            "assignment_mode": "spaces",
            "initial_workspace_grants": [{"workspace_id": workspace_id, "role": "member"}],
        },
        cookies={"bowhead_jwt": admin_jwt},
    )
    assert invite.status_code == 200, invite.text
    token = _token_from_url(invite.json()["invite_url"])

    assert jwt.get_unverified_header(token)["kid"] == "active"
    payload = jwt.decode(token, ROTATED_SECRET, algorithms=["HS256"])
    assert payload["invited_email"] == "rotation@datarc.test"

    preview = client.post("/api/v1/auth/invites/preview", json={"token": token})
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("  李华  ", "李华"),
        ("", None),
        ("hua.li@datarc.test", None),
        # A full-width "＠" is treated as an email too
        ("hua.li＠datarc.test", None),
        # Only zero-width characters or whitespace: treated as no display name
        ("\u200b", None),
        (" \u200b\u3000\u200d ", None),
        # Truncated at 80 grapheme clusters
        ("A" * 200, "A" * 80),
        # Combining marks are not split: g + combining tilde (no precomposed form) counts as one grapheme
        ("g\u0303" * 100, "g\u0303" * 80),
        # Emoji ZWJ sequences are not split
        ("A" * 79 + "👨\u200d👩\u200d👧" + "BBB", "A" * 79 + "👨\u200d👩\u200d👧"),
    ],
)
async def test_preview_shows_inviter_display_name_but_never_an_email(client, store, display_name, expected):
    await _seed_team(store)
    await _seed_user(store, "u_inviter", "hua.li@datarc.test", creator=True)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, "u_inviter"))
        await db.commit()
    token, expires_at = _signed_team_invite(
        invite_id="oinv_inviter",
        team_id="team_invite",
        email="guest@datarc.test",
    )
    await store.record_team_invite(
        team_id="team_invite",
        team_role="member",
        invited_email="guest@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_inviter",
        invite_id="oinv_inviter",
    )

    preview = client.post("/api/v1/auth/invites/preview", json={"token": token})

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["status"] == "valid"
    assert body.get("inviter_display_name") == expected
    # Only the display name is returned: the inviter's email (including an email used as display name) never appears in the public preview
    assert "hua.li@datarc.test" not in preview.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["user_deleted", "user_disabled", "removed_from_team", "other_team_only"],
)
async def test_preview_omits_inviter_who_is_no_longer_an_active_team_member(client, store, state):
    await _seed_team(store)
    await _seed_user(store, "u_inviter", "hua.li@datarc.test", creator=True)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET display_name = ? WHERE id = ?", ("李华", "u_inviter"))
        if state == "user_deleted":
            await db.execute("UPDATE users SET status = 'deleted' WHERE id = ?", ("u_inviter",))
        elif state == "user_disabled":
            await db.execute("UPDATE users SET status = 'disabled' WHERE id = ?", ("u_inviter",))
        elif state == "removed_from_team":
            await db.execute("UPDATE team_members SET status = 'removed' WHERE user_id = ?", ("u_inviter",))
        await db.commit()
    team_id = "team_invite"
    if state == "other_team_only":
        await _seed_team(store, team_id="team_other", name="Other")
        team_id = "team_other"
    token, expires_at = _signed_team_invite(invite_id="oinv_gone", team_id=team_id, email="guest@datarc.test")
    await store.record_team_invite(
        team_id=team_id,
        team_role="member",
        invited_email="guest@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_inviter",
        invite_id="oinv_gone",
    )

    preview = client.post("/api/v1/auth/invites/preview", json={"token": token})

    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "valid"
    assert "inviter_display_name" not in preview.json()
    assert "李华" not in preview.text


@pytest.mark.asyncio
async def test_preview_omits_inviter_when_the_inviting_account_is_gone(client, store):
    await _seed_team(store)
    token, expires_at = _signed_team_invite(
        invite_id="oinv_orphan",
        team_id="team_invite",
        email="guest@datarc.test",
    )
    await store.record_team_invite(
        team_id="team_invite",
        team_role="member",
        invited_email="guest@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_deleted",
        invite_id="oinv_orphan",
    )

    preview = client.post("/api/v1/auth/invites/preview", json={"token": token})

    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "valid"
    assert "inviter_display_name" not in preview.json()


@pytest.mark.asyncio
async def test_team_invite_register_routes_to_profile_then_wait_for_workspace_without_autosync(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@datarc.test", creator=True)
    creator_jwt = _jwt("u_creator")

    invite = client.post(
        "/api/v1/admin/members/invite",
        json={"email": "newperson@datarc.test"},
        cookies={"bowhead_jwt": creator_jwt},
    )
    assert invite.status_code == 200, invite.text
    invite_body = invite.json()
    token = _token_from_url(invite_body["invite_url"])

    payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert payload["scope"] == "team"
    assert payload["invite_id"] == invite_body["invite_id"]

    preview = client.post("/api/v1/auth/invites/preview", json={"token": token})
    assert preview.status_code == 200, preview.text
    assert preview.json()["invite_type"] == "team"
    assert "workspace" not in preview.json()
    assert "newperson@datarc.test" not in preview.text

    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "newperson@datarc.test",
            "password": "Passw0rd!",
            "display_name": "New Person",
            "invite_token": token,
        },
    )
    assert registered.status_code == 200, registered.text
    registered_body = registered.json()
    assert registered_body["invite_result"]["invite_type"] == "team"
    assert registered_body["invite_result"]["status"] == "accepted"
    assert registered_body["invite_result"]["next_step"] == "profile_setup"
    assert registered_body["invite_result"]["next_route"].startswith(
        "/onboarding/profile?source=invite&next=",
    )
    assert registered_body["invite_result"]["ready_route"] == "/invite/ready?state=wait_for_workspace"
    assert registered_body["invite_result"]["team"] == {
        "id": "team_invite",
        "name": "Datarc Strategy",
    }
    assert "workspace" not in registered_body["invite_result"]

    workspaces = client.get("/api/v1/auth/me/workspaces?autosync=0")
    assert workspaces.status_code == 200, workspaces.text
    assert workspaces.json()["items"] == []


@pytest.mark.asyncio
async def test_existing_matching_user_accepts_workspace_invite(client, store):
    workspace_id = await _seed_admin_workspace(store)
    await _seed_user(store, "u_existing", "existing@datarc.test")
    token, expires_at = _signed_workspace_invite(
        invite_id="inv_existing_workspace",
        workspace_id=workspace_id,
        email="existing@datarc.test",
        role="viewer",
    )
    invite_id = await store.record_invite(
        workspace_id=workspace_id,
        team_id="team_invite",
        role="viewer",
        invited_email="existing@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_admin",
        invite_id="inv_existing_workspace",
    )

    preview = client.post(
        "/api/v1/auth/invites/preview",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_existing", "existing@datarc.test")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["viewer_context"]["state"] == "logged_in_matching_email"

    accepted = client.post(
        "/api/v1/auth/invites/accept",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_existing")},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["ok"] is True
    assert body["invite_result"]["status"] == "accepted"
    assert body["invite_result"]["next_step"] == "profile_setup"
    assert body["invite_result"]["ready_route"] == "/invite/ready?state=workspace_ready"
    assert body["invite_result"]["workspace"]["id"] == workspace_id
    assert body["invite_result"]["workspace"]["role"] == "viewer"
    assert body["invite_result"]["team"]["name"] == "Datarc Strategy"
    assert await store.get_member_role(workspace_id, "u_existing") == "viewer"

    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT status, consumed_at FROM invite_tokens WHERE id = ?",
            (invite_id,),
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT preferences_json FROM users WHERE id = ?",
            ("u_existing",),
        ) as cur:
            pref_row = await cur.fetchone()
    assert row[0] == "consumed"
    assert row[1]
    assert workspace_id in pref_row[0]


@pytest.mark.asyncio
async def test_preview_marks_same_email_different_team_as_wrong_team(client, store):
    workspace_id = await _seed_admin_workspace(store)
    await _seed_team(store, team_id="team_other", name="Other Team")
    await _seed_user(store, "u_other_team", "existing@datarc.test", team_id="team_other")
    token, expires_at = _signed_workspace_invite(
        invite_id="inv_wrong_team_preview",
        workspace_id=workspace_id,
        email="existing@datarc.test",
    )
    await store.record_invite(
        workspace_id=workspace_id,
        team_id="team_invite",
        role="member",
        invited_email="existing@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_admin",
        invite_id="inv_wrong_team_preview",
    )

    preview = client.post(
        "/api/v1/auth/invites/preview",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_other_team", "existing@datarc.test")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["viewer_context"]["state"] == "logged_in_wrong_team"


@pytest.mark.asyncio
async def test_preview_does_not_auto_match_unverified_user_when_verification_required(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    workspace_id = await _seed_admin_workspace(store)
    await _seed_user(
        store,
        "u_unverified",
        "existing@datarc.test",
        email_verified=False,
    )
    token, expires_at = _signed_workspace_invite(
        invite_id="inv_unverified_preview",
        workspace_id=workspace_id,
        email="existing@datarc.test",
    )
    await store.record_invite(
        workspace_id=workspace_id,
        team_id="team_invite",
        role="member",
        invited_email="existing@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_admin",
        invite_id="inv_unverified_preview",
    )

    preview = client.post(
        "/api/v1/auth/invites/preview",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_unverified", "existing@datarc.test")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["viewer_context"]["state"] == "anonymous"

    accepted = client.post(
        "/api/v1/auth/invites/accept",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_unverified")},
    )
    assert accepted.status_code == 403, accepted.text
    assert accepted.json()["detail"]["code"] == "email_not_verified"


@pytest.mark.asyncio
async def test_existing_user_cannot_accept_invite_for_different_email(client, store):
    workspace_id = await _seed_admin_workspace(store)
    await _seed_user(store, "u_existing", "existing@datarc.test")
    await _seed_user(store, "u_other", "other@datarc.test")
    token, expires_at = _signed_workspace_invite(
        invite_id="inv_email_mismatch",
        workspace_id=workspace_id,
        email="existing@datarc.test",
    )
    await store.record_invite(
        workspace_id=workspace_id,
        team_id="team_invite",
        role="member",
        invited_email="existing@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_admin",
        invite_id="inv_email_mismatch",
    )

    accepted = client.post(
        "/api/v1/auth/invites/accept",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_other")},
    )
    assert accepted.status_code == 403, accepted.text
    assert accepted.json()["detail"]["code"] == "invite_email_mismatch"
    assert await store.get_member_role(workspace_id, "u_other") is None


@pytest.mark.asyncio
async def test_existing_same_team_user_accepts_team_invite(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "creator@datarc.test", creator=True)
    await _seed_user(store, "u_existing", "existing@datarc.test")
    token, expires_at = _signed_team_invite(
        invite_id="oinv_existing_team",
        team_id="team_invite",
        email="existing@datarc.test",
    )
    invite_id = await store.record_team_invite(
        team_id="team_invite",
        team_role="member",
        invited_email="existing@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_creator",
        invite_id="oinv_existing_team",
    )

    accepted = client.post(
        "/api/v1/auth/invites/accept",
        json={"token": token},
        cookies={"bowhead_jwt": _jwt("u_existing")},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["invite_result"]["invite_type"] == "team"
    assert body["invite_result"]["status"] == "already_member"
    assert body["invite_result"]["next_step"] == "profile_setup"
    assert body["invite_result"]["ready_route"] == "/invite/ready?state=wait_for_workspace"
    assert body["invite_result"]["team"] == {
        "id": "team_invite",
        "name": "Datarc Strategy",
    }
    assert "workspace" not in body["invite_result"]

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT status, consumed_at FROM team_invite_tokens WHERE id = ?",
            (invite_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == "consumed"
    assert row[1]


@pytest.mark.asyncio
async def test_invite_register_requires_verification_keeps_profile_next_on_verify_reclick(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "1")

    async def fail_legacy_preference_write(*_args, **_kwargs):
        raise AssertionError("post-commit invite preference write should not be used")

    monkeypatch.setattr(store, "set_user_preference_key", fail_legacy_preference_write)

    workspace_id = await _seed_admin_workspace(store)
    token, expires_at = _signed_workspace_invite(
        invite_id="inv_verify_recovery",
        workspace_id=workspace_id,
        email="verify-me@datarc.test",
    )
    await store.record_invite(
        workspace_id=workspace_id,
        team_id="team_invite",
        role="member",
        invited_email="verify-me@datarc.test",
        raw_token=token,
        expires_at=expires_at,
        invited_by="u_admin",
        invite_id="inv_verify_recovery",
    )

    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "verify-me@datarc.test",
            "password": "Passw0rd!",
            "display_name": "Verify Me",
            "invite_token": token,
        },
    )
    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert body["requires_verification"] is True
    assert body["invite_result"]["next_step"] == "verify_email"
    assert body["invite_result"]["next_route"] == "/invite/verify-pending?next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready"

    user = await store.get_user_by_email("verify-me@datarc.test")
    assert user is not None
    assert (
        user.preferences.get("invite_after_verify_next_route") == "/onboarding/profile?source=invite&next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready"
    )
    jti = await store.get_verify_token_jti(user.id)
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys

    verify_token = make_verify_token(user.id, user.email, derive_jwt_secret_keys("email-verify")[0][1], jti=jti)

    verified = client.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert verified.status_code == 200, verified.text
    verified_body = verified.json()
    assert verified_body["status"] == "verified"
    assert _login_next(verified_body["next_route"]).startswith(
        "/onboarding/profile?source=invite&next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready",
    )

    repeated = client.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert repeated.status_code == 200, repeated.text
    repeated_body = repeated.json()
    assert repeated_body["status"] == "already_verified"
    assert repeated_body["next_route"] == verified_body["next_route"]


@pytest.mark.asyncio
async def test_oauth_login_preserves_invite_next_and_callback_redirects(client, store, monkeypatch):
    from bowhead_gateway.routes import oauth as oauth_routes

    assert oauth_routes._safe_next_path("/invite?accept=1") == "/invite?accept=1"
    assert oauth_routes._safe_next_path("//evil.example") is None
    assert oauth_routes._safe_next_path("/bad\npath") is None
    assert oauth_routes._safe_next_path("/bad\\path") is None

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")

    login = client.get("/api/v1/auth/oauth/feishu/login?next=/invite?accept=1")
    assert login.status_code == 200, login.text
    provider_url = login.json()["redirect_url"]
    provider_qs = parse_qs(urlparse(provider_url).query)
    state = provider_qs["state"][0]
    assert provider_qs["redirect_uri"] == ["http://test.local/api/v1/auth/oauth/feishu/callback"]

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT provider, next_path FROM oauth_state WHERE state = ?",
            (state,),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row == ("feishu", "/invite?accept=1")

    await _seed_team(store)
    await _seed_user(store, "u_oauth_invite", "oauth-invite@datarc.test")
    monkeypatch.setattr(oauth_routes.httpx2, "AsyncClient", _FakeOAuthClient)
    callback = client.get(
        f"/api/v1/auth/oauth/feishu/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/invite?accept=1"
    assert "bowhead_jwt=" in callback.headers["set-cookie"]


@pytest.mark.asyncio
async def test_oauth_invite_next_for_new_user_redirects_to_register_without_creating_team(
    client,
    store,
    monkeypatch,
):
    from bowhead_gateway.routes import oauth as oauth_routes

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")

    login = client.get(
        "/api/v1/auth/oauth/feishu/login?next=%2Finvite%3Ftoken%3Dinvite-token%26accept%3D1",
    )
    assert login.status_code == 200, login.text
    state = parse_qs(urlparse(login.json()["redirect_url"]).query)["state"][0]

    monkeypatch.setattr(oauth_routes.httpx2, "AsyncClient", _FakeOAuthClient)
    callback = client.get(
        f"/api/v1/auth/oauth/feishu/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/register?source=invite&token=invite-token"
    assert "set-cookie" not in callback.headers
    assert await store.get_user_by_email("oauth-invite@datarc.test") is None


@pytest.mark.asyncio
async def test_oauth_start_returns_structured_configuration_errors(
    client,
    monkeypatch,
):
    unknown = client.get("/api/v1/auth/oauth/unknown/login")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == {"code": "oauth_provider_unsupported"}

    monkeypatch.delenv("BOWHEAD_OAUTH_FEISHU_APP_ID", raising=False)
    missing = client.get("/api/v1/auth/oauth/feishu/login")
    assert missing.status_code == 503
    assert missing.json()["detail"] == {"code": "oauth_not_configured"}

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")
    invalid_flow = client.get("/api/v1/auth/oauth/feishu/login?flow=other")
    assert invalid_flow.status_code == 422
    assert invalid_flow.json()["detail"] == {"code": "invalid_oauth_flow"}


def test_login_capabilities_only_publish_complete_oauth_configurations(
    client,
    monkeypatch,
) -> None:
    for name in (
        "BOWHEAD_OAUTH_FEISHU_APP_ID",
        "BOWHEAD_OAUTH_FEISHU_APP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    assert client.get("/api/v1/auth/login-capabilities").json() == {
        "oauth_providers": [],
        "enterprise_oidc": False,
    }

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    assert client.get("/api/v1/auth/login-capabilities").json()["oauth_providers"] == []

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")
    assert client.get("/api/v1/auth/login-capabilities").json()["oauth_providers"] == ["feishu"]


@pytest.mark.asyncio
async def test_oauth_callback_cancellation_returns_to_login_and_consumes_state(
    client,
    store,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")
    login = client.get("/api/v1/auth/oauth/feishu/login?next=/reports/")
    state = parse_qs(urlparse(login.json()["redirect_url"]).query)["state"][0]

    callback = client.get(
        f"/api/v1/auth/oauth/feishu/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    parsed = urlparse(callback.headers["location"])
    assert parsed.path == "/login/"
    assert parse_qs(parsed.query) == {
        "next": ["/reports/"],
        "oauth_error": ["oauth_authorization_incomplete"],
        "oauth_provider": ["feishu"],
    }
    assert await _oauth_state_exists(store, state) is False


@pytest.mark.asyncio
async def test_oauth_callback_hides_provider_response_body(
    client,
    monkeypatch,
):
    from bowhead_gateway.routes import oauth as oauth_routes

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")
    login = client.get("/api/v1/auth/oauth/feishu/login")
    state = parse_qs(urlparse(login.json()["redirect_url"]).query)["state"][0]
    monkeypatch.setattr(oauth_routes.httpx2, "AsyncClient", _FailingOAuthClient)

    callback = client.get(
        f"/api/v1/auth/oauth/feishu/callback?code=ok&state={state}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert _oauth_redirect_error(callback) == "oauth_token_exchange_failed"
    assert "upstream-secret-detail" not in callback.headers["location"]
    assert "upstream-secret-detail" not in callback.text


@pytest.mark.asyncio
async def test_oauth_callback_hides_unexpected_exception_details(
    client,
    monkeypatch,
    caplog,
):
    from bowhead_gateway.routes import oauth as oauth_routes

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "feishu-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "feishu-secret")
    login = client.get("/api/v1/auth/oauth/feishu/login")
    state = parse_qs(urlparse(login.json()["redirect_url"]).query)["state"][0]
    monkeypatch.setattr(oauth_routes.httpx2, "AsyncClient", _MalformedOAuthClient)

    callback = client.get(
        f"/api/v1/auth/oauth/feishu/callback?code=ok&state={state}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert _oauth_redirect_error(callback) == "oauth_callback_failed"
    assert "upstream-secret-detail" not in callback.headers["location"]
    assert "upstream-secret-detail" not in callback.text
    assert "upstream-secret-detail" not in caplog.text
    assert "JSONDecodeError" in caplog.text


async def _oauth_state_exists(store: UserStore, state: str) -> bool:
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT 1 FROM oauth_state WHERE state = ?",
            (state,),
        ) as cur,
    ):
        return await cur.fetchone() is not None


def _oauth_redirect_error(response) -> str:
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/login/"
    return parse_qs(parsed.query)["oauth_error"][0]


def _login_next(route: str) -> str:
    parsed = urlparse(route)
    assert parsed.path == "/login"
    return parse_qs(parsed.query)["next"][0]


class _FakeOAuthClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, data: dict):
        return httpx2.Response(200, json={"access_token": "provider-token"})

    async def get(self, url: str, headers: dict):
        return httpx2.Response(
            200,
            json={
                "email": "oauth-invite@datarc.test",
                "name": "OAuth Invite",
                "picture": "https://avatar.test/oauth.png",
            },
        )


class _FailingOAuthClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, data: dict):
        return httpx2.Response(401, text="upstream-secret-detail")


class _MalformedOAuthClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, data: dict):
        return httpx2.Response(200, text="upstream-secret-detail")
