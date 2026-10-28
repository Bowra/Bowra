"""Fix A — invite-resend footgun. `resend`/`remind` mints a NEW token and rotates the
stored hash (the raw token isn't stored, only its hash, so the old link can't be
re-sent). Register used to validate by exact token_hash → resend silently invalidated a
link already in the invitee's inbox (an invited user — an administrator clicked
resend 47s later, invitee clicked the FIRST email → "invite token invalid/revoked/expired").

Fix A: the invite JWT is signature+exp verified upstream; register now matches the ACTIVE
invite by (email, team/workspace), not by token hash. These cover the store methods behind
that: the invite SURVIVES a resend, and revoke/expiry/consume still gate. Team + workspace.
"""

from __future__ import annotations

from bowhead_core.user_store import UserStore

FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"


async def _store(tmp_path, name: str = "u.db") -> UserStore:
    s = UserStore(str(tmp_path / name))
    await s.initialize()
    return s


async def test_team_invite_survives_resend_by_email(tmp_path) -> None:
    store = await _store(tmp_path)
    team, email = "team_x", "invitee@datarc.cn"
    inv = await store.record_team_invite(
        team_id=team,
        team_role="member",
        invited_email=email,
        raw_token="TOKEN_A",
        expires_at=FUTURE,
        invited_by="admin",
    )
    # the first link resolves
    assert (await store.get_active_team_invite_by_email(team, email))["id"] == inv
    # RESEND rotates the stored hash to token B (old token A hash replaced)
    assert await store.reissue_team_invite_token(inv, team, "TOKEN_B", FUTURE)
    # THE FIX: email lookup still finds the active invite (hash-agnostic) → the link
    # already in the invitee's inbox keeps working after a resend
    assert (await store.get_active_team_invite_by_email(team, email))["id"] == inv
    # the OLD hash-keyed path is exactly what broke (token A's hash is gone) — this is
    # why the register gate had to stop matching by the exact submitted token
    assert await store.get_active_team_invite_by_token("TOKEN_A") is None
    # consume by id → no longer active; second consume is a no-op
    assert await store.consume_team_invite_by_id(inv)
    assert await store.get_active_team_invite_by_email(team, email) is None
    assert not await store.consume_team_invite_by_id(inv)


async def test_team_invite_email_lookup_gates_revoke_expiry_scope(tmp_path) -> None:
    store = await _store(tmp_path)
    team = "team_y"
    # revoked → not returned (revoke now kills ALL links for the invite, stronger than hash)
    r = await store.record_team_invite(
        team_id=team,
        team_role="member",
        invited_email="a@x.cn",
        raw_token="T1",
        expires_at=FUTURE,
        invited_by="adm",
    )
    assert await store.revoke_team_invite(r, team)
    assert await store.get_active_team_invite_by_email(team, "a@x.cn") is None
    # expired → not returned
    await store.record_team_invite(
        team_id=team,
        team_role="member",
        invited_email="b@x.cn",
        raw_token="T2",
        expires_at=PAST,
        invited_by="adm",
    )
    assert await store.get_active_team_invite_by_email(team, "b@x.cn") is None
    # wrong team / wrong email → not returned (no cross-team or cross-email bleed)
    await store.record_team_invite(
        team_id=team,
        team_role="member",
        invited_email="c@x.cn",
        raw_token="T3",
        expires_at=FUTURE,
        invited_by="adm",
    )
    assert await store.get_active_team_invite_by_email("team_other", "c@x.cn") is None
    assert await store.get_active_team_invite_by_email(team, "nobody@x.cn") is None


async def test_workspace_invite_survives_resend_by_email(tmp_path) -> None:
    store = await _store(tmp_path)
    ws, team, email = "ws_1", "team_z", "wsinvitee@x.cn"
    inv = await store.record_invite(
        workspace_id=ws,
        team_id=team,
        role="member",
        invited_email=email,
        raw_token="WTOKEN_A",
        expires_at=FUTURE,
        invited_by="adm",
    )
    assert (await store.get_active_workspace_invite_by_email(ws, email))["id"] == inv
    assert await store.reissue_invite_token(inv, team, "WTOKEN_B", FUTURE)
    # survives the resend (email lookup, not hash)
    assert (await store.get_active_workspace_invite_by_email(ws, email))["id"] == inv
    assert await store.is_invite_active("WTOKEN_A") is False  # old hash dead → gate had to change
    # consume by id
    assert await store.consume_invite_by_id(inv)
    assert await store.get_active_workspace_invite_by_email(ws, email) is None
