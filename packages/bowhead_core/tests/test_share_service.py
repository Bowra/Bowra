"""S1 core tests — content sharing primitives + share_service domain logic.

Covers (tech-design §2, PRD §9.4/§9.7):
- granted_resource_ids: principal chain, deny-wins, revoked/expired exclusion
- set_audience_tx: full §4.3 matrix incl. roster retention round-trip,
  visibility write-through, single-transaction rollback, validation
- can_manage_shares / is_visible / add_grant guards / derive_audience / flag
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.resource_access import SHAREABLE_KINDS
from bowhead_core.share_service import (
    add_grant,
    can_manage_shares,
    content_sharing_enabled,
    derive_audience,
    grants_for_viewer,
    is_visible,
    remove_grant,
    reset_content_sharing_cache,
    set_audience,
)
from bowhead_core.subagent_write_boundary import (
    TeamSubagentWriteBoundaryError,
    _issue_team_subagent_transaction_token,
)
from bowhead_core.user_store import TeamSubagent, UserStore

TEAM = "team_test"
OTHER_ORG = "team_other"
_SUBAGENT_TRANSACTION_TOKEN = _issue_team_subagent_transaction_token()


@pytest.fixture
async def stores(tmp_path):
    db = tmp_path / "share.db"
    users = UserStore(db)
    await users.initialize()
    access = AccessControlStore(db)
    await access.initialize()
    return users, access


async def _grant(
    access: AccessControlStore,
    *,
    kind: str = "team_subagent",
    rid: str = "osa_1",
    ptype: str = "user",
    pid: str = "u_bob",
    effect: str = "allow",
    permission: str = "use",
    team_id: str = TEAM,
    expires_at: str | None = None,
):
    return await access.create_or_update_resource_grant(
        team_id=team_id,
        resource_kind=kind,
        resource_id=rid,
        principal_type=ptype,
        principal_id=pid,
        permission=permission,
        effect=effect,
        expires_at=expires_at,
        subagent_transaction_token=(_SUBAGENT_TRANSACTION_TOKEN if kind == "team_subagent" else None),
    )


# ── granted_resource_ids ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_granted_ids_principal_chain(stores):
    _, access = stores
    await _grant(access, rid="osa_user", ptype="user", pid="u_bob")
    await _grant(access, rid="osa_group", ptype="group", pid="grp_1")
    await _grant(access, rid="osa_team", ptype="team", pid=TEAM)
    await _grant(access, rid="osa_other_user", ptype="user", pid="u_carol")
    await _grant(access, rid="osa_other_team", ptype="team", pid=OTHER_ORG, team_id=OTHER_ORG)

    ids = await access.granted_resource_ids(
        TEAM,
        user_id="u_bob",
        group_ids=("grp_1",),
        kind="team_subagent",
    )
    assert ids == frozenset({"osa_user", "osa_group", "osa_team"})

    # group not joined → group grant invisible
    ids = await access.granted_resource_ids(TEAM, user_id="u_bob", kind="team_subagent")
    assert ids == frozenset({"osa_user", "osa_team"})


@pytest.mark.asyncio
async def test_granted_ids_deny_wins(stores):
    _, access = stores
    await _grant(access, rid="osa_1", ptype="team", pid=TEAM, effect="allow")
    await _grant(access, rid="osa_1", ptype="user", pid="u_bob", effect="deny")
    ids = await access.granted_resource_ids(TEAM, user_id="u_bob", kind="team_subagent")
    assert "osa_1" not in ids
    # another user without the deny still sees it
    ids2 = await access.granted_resource_ids(TEAM, user_id="u_carol", kind="team_subagent")
    assert "osa_1" in ids2


@pytest.mark.asyncio
async def test_granted_ids_excludes_revoked_and_expired(stores):
    _, access = stores
    g = await _grant(access, rid="osa_revoked", ptype="user", pid="u_bob")
    await access.revoke_resource_grant(
        TEAM,
        g.id,
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await _grant(access, rid="osa_expired", ptype="user", pid="u_bob", expires_at=past)
    ids = await access.granted_resource_ids(TEAM, user_id="u_bob", kind="team_subagent")
    assert ids == frozenset()


# ── set_audience_tx matrix ──────────────────────────────────────────────


async def _active_grants(access: AccessControlStore, rid: str):
    return await access.list_resource_grants(
        TEAM,
        resource_kind="team_subagent",
        resource_id=rid,
    )


@pytest.mark.asyncio
async def test_team_wide_plus_user_grant_does_not_flip_third_party(stores):
    """PRD §9.3: team_wide 资产追加一条 user grant 后,第三成员可见性不翻转。
    写穿后 team grant 恒在 → 引擎不进 allowlist-miss-deny;追加 user grant 只是
    additive,carol(既非 owner 也无个人 grant)仍经 team grant 命中。"""
    _, access = stores
    rid = "osa_pub"
    # team_wide via write-through (create a team grant).
    await access.set_audience_tx(
        team_id=TEAM,
        resource_kind="team_subagent",
        resource_id=rid,
        target="team_wide",
        permission="use",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    carol_before = await access.granted_resource_ids(TEAM, user_id="u_carol", kind="team_subagent")
    assert rid in carol_before  # Team grant matches.
    # Add a user grant for bob.
    await access.create_or_update_resource_grant(
        team_id=TEAM,
        resource_kind="team_subagent",
        resource_id=rid,
        principal_type="user",
        principal_id="u_bob",
        permission="use",
        created_by="u_alice",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    carol_after = await access.granted_resource_ids(TEAM, user_id="u_carol", kind="team_subagent")
    assert rid in carol_after, "追加 user grant 不应让第三成员失去 team_wide 可见性"
    bob = await access.granted_resource_ids(TEAM, user_id="u_bob", kind="team_subagent")
    assert rid in bob  # bob matches through either user or team grant.


@pytest.mark.asyncio
async def test_audience_roundtrip_keeps_roster(stores):
    """custom → team_wide → custom: user/group grants survive as the roster."""
    _, access = stores
    rid = "osa_rt"
    await _grant(access, rid=rid, ptype="user", pid="u_bob")

    res = await access.set_audience_tx(
        team_id=TEAM,
        resource_kind="team_subagent",
        resource_id=rid,
        target="team_wide",
        permission="use",
        created_by="u_alice",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    assert res["team_grant_id"]
    kinds = {(g.principal_type, g.principal_id) for g in await _active_grants(access, rid)}
    assert ("team", TEAM) in kinds and ("user", "u_bob") in kinds

    res = await access.set_audience_tx(
        team_id=TEAM,
        resource_kind="team_subagent",
        resource_id=rid,
        target="custom",
        permission="use",
        created_by="u_alice",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    assert len(res["revoked_grant_ids"]) == 1  # only the team grant
    kinds = {(g.principal_type, g.principal_id) for g in await _active_grants(access, rid)}
    assert kinds == {("user", "u_bob")}


@pytest.mark.asyncio
async def test_audience_private_revokes_allows_keeps_deny(stores):
    _, access = stores
    rid = "osa_priv"
    await _grant(access, rid=rid, ptype="user", pid="u_bob")
    await _grant(access, rid=rid, ptype="team", pid=TEAM)
    await _grant(access, rid=rid, ptype="user", pid="u_evil", effect="deny")

    res = await access.set_audience_tx(
        team_id=TEAM,
        resource_kind="team_subagent",
        resource_id=rid,
        target="private",
        permission="use",
        created_by="u_alice",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    assert len(res["revoked_grant_ids"]) == 2
    remaining = await _active_grants(access, rid)
    assert [(g.principal_id, g.effect) for g in remaining] == [("u_evil", "deny")]


@pytest.mark.asyncio
async def test_audience_team_wide_visibility_write_through(stores):
    users, access = stores
    team = await users.create_team(name="Acme", max_seats=10)
    sa = TeamSubagent(
        id="osa_wt",
        team_id=team.id,
        name="wt-agent",
        description="d",
        system_prompt="p",
        visibility="creator_only",
        created_by="u_alice",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    await users.create_team_subagent(sa)

    await access.set_audience_tx(
        team_id=team.id,
        resource_kind="team_subagent",
        resource_id="osa_wt",
        target="team_wide",
        permission="use",
        visibility_table="team_subagent",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    rec = await users.get_team_subagent(team.id, "osa_wt")
    assert rec is not None and rec.visibility == "team_wide"

    await access.set_audience_tx(
        team_id=team.id,
        resource_kind="team_subagent",
        resource_id="osa_wt",
        target="custom",
        permission="use",
        visibility_table="team_subagent",
        subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
    )
    rec = await users.get_team_subagent(team.id, "osa_wt")
    assert rec is not None and rec.visibility == "creator_only"


@pytest.mark.asyncio
async def test_audience_tx_atomic_rollback(stores, monkeypatch):
    """Mid-transaction failure leaves NO partial state (the §2.2 P0 fix)."""
    _, access = stores
    rid = "osa_atomic"
    await _grant(access, rid=rid, ptype="team", pid=TEAM)

    async def _boom(db, team_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(access, "_bump_policy_epoch_conn", _boom)
    with pytest.raises(RuntimeError):
        await access.set_audience_tx(
            team_id=TEAM,
            resource_kind="team_subagent",
            resource_id=rid,
            target="private",
            permission="use",
            subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
        )
    monkeypatch.undo()
    grants = await _active_grants(access, rid)
    assert len(grants) == 1  # revoke rolled back with the failed tx


@pytest.mark.asyncio
async def test_audience_validation(stores):
    _, access = stores
    with pytest.raises(ValueError):
        await access.set_audience_tx(
            team_id=TEAM,
            resource_kind="team_subagent",
            resource_id="x",
            target="everyone",
            permission="use",
            subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
        )
    with pytest.raises(ValueError):
        await access.set_audience_tx(
            team_id=TEAM,
            resource_kind="team_subagent",
            resource_id="x",
            target="private",
            permission="use",
            visibility_table="users",
            subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
        )
    with pytest.raises(ValueError, match="must match resource_kind"):
        await access.set_audience_tx(
            team_id=TEAM,
            resource_kind="report",
            resource_id="osa_1",
            target="team_wide",
            permission="read",
            visibility_table="team_subagent",
        )
    with pytest.raises(ValueError):
        await set_audience(
            access,
            kind="kb_file",
            resource_id="x",
            team_id=TEAM,
            target="private",
            actor_id=None,
        )


@pytest.mark.asyncio
async def test_generic_share_service_rejects_subagent_mutations(stores):
    _, access = stores
    with pytest.raises(TeamSubagentWriteBoundaryError) as audience_error:
        await set_audience(
            access,
            kind="team_subagent",
            resource_id="osa_1",
            team_id=TEAM,
            target="private",
            actor_id="u_a",
        )
    assert audience_error.value.code == "subagent_write_api_obsolete"

    with pytest.raises(TeamSubagentWriteBoundaryError):
        await add_grant(
            access,
            kind="team_subagent",
            resource_id="osa_1",
            team_id=TEAM,
            principal_type="user",
            principal_id="u_b",
            actor_id="u_a",
        )
    with pytest.raises(TeamSubagentWriteBoundaryError):
        await remove_grant(
            access,
            kind="team_subagent",
            team_id=TEAM,
            grant_id="grant_1",
        )


# ── share_service guards & helpers ──────────────────────────────────────


@pytest.mark.asyncio
async def test_can_manage_shares_roles(stores):
    users, _ = stores
    team = await users.create_team(name="Acme", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="o@x.com", password="Passw0rd!")
    creator = await users.create_user(team_id=team.id, email="c@x.com", password="Passw0rd!")
    admin = await users.create_user(team_id=team.id, email="a@x.com", password="Passw0rd!")
    member = await users.create_user(team_id=team.id, email="m@x.com", password="Passw0rd!")
    await users.set_team_creator(team.id, creator.id)
    await users.promote_team_role(
        user_id=admin.id,
        team_id=team.id,
        new_role="team_admin",
        promoted_by=creator.id,
    )

    assert await can_manage_shares(users, owner, owner.id) is True
    assert await can_manage_shares(users, creator, owner.id) is True
    assert await can_manage_shares(users, admin, owner.id) is True
    assert await can_manage_shares(users, member, owner.id) is False


def test_is_visible_formula():
    granted = frozenset({"osa_g"})
    common = {"viewer_id": "u_bob", "granted_ids": granted}
    assert is_visible(owner_id="u_bob", visibility="creator_only", resource_id="x", **common)
    assert is_visible(owner_id="u_a", visibility="team_wide", resource_id="x", **common)
    assert is_visible(owner_id="u_a", visibility="creator_only", resource_id="osa_g", **common)
    assert not is_visible(owner_id="u_a", visibility="creator_only", resource_id="x", **common)


@pytest.mark.asyncio
async def test_add_grant_rejects_team_principal_and_fixed_permission(stores):
    _, access = stores
    with pytest.raises(ValueError):
        await add_grant(
            access,
            kind="team_subagent",
            resource_id="osa_1",
            team_id=TEAM,
            principal_type="team",
            principal_id=TEAM,
            actor_id="u_a",
        )
    g = await add_grant(
        access,
        kind="report",
        resource_id="ses_1",
        team_id=TEAM,
        principal_type="user",
        principal_id="u_bob",
        actor_id="u_a",
    )
    assert g.permission == SHAREABLE_KINDS["report"] == "read"
    assert (
        await remove_grant(
            access,
            kind="report",
            team_id=TEAM,
            grant_id=g.id,
        )
        is True
    )


@pytest.mark.asyncio
async def test_grants_for_viewer_flag_off(stores, monkeypatch):
    _, access = stores
    await _grant(access, rid="osa_1", ptype="user", pid="u_bob")
    monkeypatch.setenv("BOWHEAD_CONTENT_SHARING", "0")
    reset_content_sharing_cache()
    assert content_sharing_enabled() is False
    ids = await grants_for_viewer(
        access,
        team_id=TEAM,
        user_id="u_bob",
        kind="team_subagent",
    )
    assert ids == frozenset()
    monkeypatch.delenv("BOWHEAD_CONTENT_SHARING")
    reset_content_sharing_cache()
    ids = await grants_for_viewer(
        access,
        team_id=TEAM,
        user_id="u_bob",
        kind="team_subagent",
    )
    assert ids == frozenset({"osa_1"})


@pytest.mark.asyncio
async def test_derive_audience(stores):
    _, access = stores
    rid = "ses_aud"
    grants = await access.list_resource_grants(TEAM, resource_kind="report", resource_id=rid)
    assert derive_audience(grants, team_id=TEAM) == "private"
    await _grant(access, kind="report", rid=rid, ptype="user", pid="u_bob", permission="read")
    grants = await access.list_resource_grants(TEAM, resource_kind="report", resource_id=rid)
    assert derive_audience(grants, team_id=TEAM) == "custom"
    await _grant(access, kind="report", rid=rid, ptype="team", pid=TEAM, permission="read")
    grants = await access.list_resource_grants(TEAM, resource_kind="report", resource_id=rid)
    assert derive_audience(grants, team_id=TEAM) == "team_wide"


# ── wave 2: resolve_shareable extraction (report-only used by access-requests) ──
async def test_resolve_shareable_report_cross_team_and_misses():
    from types import SimpleNamespace

    from bowhead_core.share_service import resolve_shareable

    canvas = SimpleNamespace(user_id="u_alice", workspace_id="ws1", title="Q4 Report")

    class _Canvas:
        async def get_canvas_by_session(self, sid):
            return canvas if sid == "s1" else None

    class _Users:
        async def workspace_in_team(self, team_id, ws):
            return team_id == "o1" and ws == "ws1"

        async def get_team_subagent(self, team_id, rid):
            return None

        async def get_team_skill(self, team_id, rid):
            return None

    us, cs = _Users(), _Canvas()
    common = {"user_store": us, "canvas_store": cs, "subagent_enabled": True, "workflow_enabled": True}

    # in-team report → resolved (owner + label)
    res = await resolve_shareable(kind="report", resource_id="s1", team_id="o1", **common)
    assert res is not None and res.owner_id == "u_alice" and res.label == "Q4 Report" and res.visibility is None
    # cross-team report → None (anti-enum; workspace_in_team False for o2)
    assert await resolve_shareable(kind="report", resource_id="s1", team_id="o2", **common) is None
    # not found → None
    assert await resolve_shareable(kind="report", resource_id="sX", team_id="o1", **common) is None
    # unsupported / empty kind → None
    assert await resolve_shareable(kind="bogus", resource_id="s1", team_id="o1", **common) is None
    # dark capability flag → None
    assert (
        await resolve_shareable(
            kind="team_subagent",
            resource_id="osa_1",
            team_id="o1",
            user_store=us,
            canvas_store=cs,
            subagent_enabled=False,
            workflow_enabled=True,
        )
        is None
    )
