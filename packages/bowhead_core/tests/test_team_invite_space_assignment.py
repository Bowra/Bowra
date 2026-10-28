from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.space_governance import SpaceGovernanceError
from bowhead_core.user_store import UserStore, hash_password


@pytest.fixture
async def store(tmp_path) -> UserStore:
    value = UserStore(tmp_path / "invite-space-assignment.db")
    await value.initialize()
    return value


async def _seed_team_and_space(
    store: UserStore,
) -> tuple[str, str, str]:
    team = await store.create_team(name="Invite Team", max_seats=50)
    owner = await store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="Passw0rd!",
    )
    await store.set_team_creator(team.id, owner.id)
    workspace_id = await store.create_local_workspace(
        team_id=team.id,
        name="Initial Space",
        creator_user_id=owner.id,
    )
    return team.id, owner.id, workspace_id


async def _create_spaces_invite(
    store: UserStore,
    *,
    team_id: str,
    owner_id: str,
    workspace_id: str,
    invite_id: str,
    email: str,
) -> dict:
    return await store.create_or_merge_team_invite(
        team_id=team_id,
        invited_email=email,
        raw_token=f"token-{invite_id}",
        expires_at="2999-01-01T00:00:00+00:00",
        assignment_mode="spaces",
        initial_workspace_grants=[{"workspace_id": workspace_id, "role": "member"}],
        invited_by=owner_id,
        invite_id=invite_id,
    )


async def test_create_or_merge_persists_explicit_assignment(
    store: UserStore,
) -> None:
    team_id, owner_id, first_workspace_id = await _seed_team_and_space(store)
    second_workspace_id = await store.create_local_workspace(
        team_id=team_id,
        name="Second Space",
        creator_user_id=owner_id,
    )
    created = await _create_spaces_invite(
        store,
        team_id=team_id,
        owner_id=owner_id,
        workspace_id=first_workspace_id,
        invite_id="oinv_merge",
        email="merge@example.com",
    )
    merged = await store.create_or_merge_team_invite(
        team_id=team_id,
        invited_email="merge@example.com",
        raw_token="unused-token",
        expires_at="2999-01-01T00:00:00+00:00",
        assignment_mode="spaces",
        initial_workspace_grants=[{"workspace_id": second_workspace_id, "role": "viewer"}],
        invited_by=owner_id,
    )

    assert created["method"] == "created"
    assert merged["id"] == created["id"]
    assert merged["method"] == "grant_merged"
    assert merged["assignment_mode"] == "spaces"
    assert merged["assignment_revision"] == 2
    assert sorted(
        (
            grant["workspace_id"],
            grant["role"],
        )
        for grant in merged["initial_workspace_grants"]
    ) == sorted(
        [
            (first_workspace_id, "member"),
            (second_workspace_id, "viewer"),
        ]
    )

    with pytest.raises(SpaceGovernanceError) as implicit_delete:
        await store.create_or_merge_team_invite(
            team_id=team_id,
            invited_email="merge@example.com",
            raw_token="unused-token-2",
            expires_at="2999-01-01T00:00:00+00:00",
            assignment_mode="team_only",
            initial_workspace_grants=[],
            invited_by=owner_id,
        )
    assert implicit_delete.value.code == "invite_has_workspace_grants"


@pytest.mark.parametrize(
    "accept_path",
    [
        "create_invited_user",
        "activate_invited_identity",
        "accept_for_user",
        "accept_from_intent",
    ],
)
async def test_all_team_invite_accept_paths_apply_initial_space_grants(
    store: UserStore,
    accept_path: str,
) -> None:
    team_id, owner_id, workspace_id = await _seed_team_and_space(store)
    email = f"{accept_path}@example.com"
    invite_id = f"oinv_{accept_path}"
    await _create_spaces_invite(
        store,
        team_id=team_id,
        owner_id=owner_id,
        workspace_id=workspace_id,
        invite_id=invite_id,
        email=email,
    )

    if accept_path == "create_invited_user":
        user = await store.create_invited_user_atomic(
            invite_type="team",
            invite_id=invite_id,
            team_id=team_id,
            email=email,
            password="Passw0rd!",
        )
    elif accept_path == "activate_invited_identity":
        raw_secret = f"intent-{invite_id}"
        intent_hash = await store.create_invite_intent(
            raw_secret=raw_secret,
            invite_type="team",
            invite_id=invite_id,
            token_version=1,
            expires_at="2999-01-01T00:00:00+00:00",
        )
        user = await store.activate_invited_identity_atomic(
            intent_hash=intent_hash,
            invite_type="team",
            invite_id=invite_id,
            token_version=1,
            team_id=team_id,
            email=email,
            password_hash=hash_password("Passw0rd!"),
        )
    else:
        user = await store.create_user(
            team_id=team_id,
            email=email,
            password="Passw0rd!",
        )
        if accept_path == "accept_for_user":
            result = await store.accept_invite_for_user_atomic(
                invite_type="team",
                invite_id=invite_id,
                user_id=user.id,
                team_id=team_id,
                email=email,
            )
        else:
            raw_secret = f"intent-{invite_id}"
            intent_hash = await store.create_invite_intent(
                raw_secret=raw_secret,
                invite_type="team",
                invite_id=invite_id,
                token_version=1,
                expires_at="2999-01-01T00:00:00+00:00",
            )
            result = await store.accept_invite_from_intent_atomic(
                intent_hash=intent_hash,
                invite_type="team",
                invite_id=invite_id,
                token_version=1,
                user_id=user.id,
                target_team_id=team_id,
            )
        assert result["granted_workspace_count"] == 1
        assert result["skipped_workspace_grants"] == []

    assert await store.get_member_role(workspace_id, user.id) == "member"
    invite = await store.get_team_invite_by_id(invite_id)
    assert invite is not None
    assert invite["status"] == "consumed"


async def test_accept_skips_space_archived_after_invitation(
    store: UserStore,
) -> None:
    team_id, owner_id, workspace_id = await _seed_team_and_space(store)
    await _create_spaces_invite(
        store,
        team_id=team_id,
        owner_id=owner_id,
        workspace_id=workspace_id,
        invite_id="oinv_archived",
        email="archived@example.com",
    )
    await store.set_workspace_status(
        workspace_id=workspace_id,
        team_id=team_id,
        status="archived",
    )
    user = await store.create_user(
        team_id=team_id,
        email="archived@example.com",
        password="Passw0rd!",
    )

    result = await store.accept_invite_for_user_atomic(
        invite_type="team",
        invite_id="oinv_archived",
        user_id=user.id,
        team_id=team_id,
        email=user.email,
    )

    assert result["granted_workspace_count"] == 0
    assert result["skipped_workspace_grants"] == [
        {
            "workspace_id": workspace_id,
            "reason": "workspace_not_active",
        }
    ]
    assert await store.get_member_role(workspace_id, user.id) is None


async def test_assignment_replacement_uses_revision_and_rejects_corruption(
    store: UserStore,
) -> None:
    team_id, owner_id, workspace_id = await _seed_team_and_space(store)
    invite = await _create_spaces_invite(
        store,
        team_id=team_id,
        owner_id=owner_id,
        workspace_id=workspace_id,
        invite_id="oinv_replace",
        email="replace@example.com",
    )
    replaced = await store.replace_team_invite_assignment(
        invite_id=invite["id"],
        team_id=team_id,
        actor_user_id=owner_id,
        assignment_mode="team_only",
        initial_workspace_grants=[],
        expected_assignment_revision=1,
    )
    assert replaced["assignment_mode"] == "team_only"
    assert replaced["assignment_revision"] == 2
    assert replaced["initial_workspace_grants"] == []

    with pytest.raises(SpaceGovernanceError) as stale:
        await store.replace_team_invite_assignment(
            invite_id=invite["id"],
            team_id=team_id,
            actor_user_id=owner_id,
            assignment_mode="team_only",
            initial_workspace_grants=[],
            expected_assignment_revision=1,
        )
    assert stale.value.code == "stale_invite_assignment"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE team_invite_tokens SET assignment_mode='spaces' WHERE id=?",
            (invite["id"],),
        )
        await db.commit()
    user = await store.create_user(
        team_id=team_id,
        email="replace@example.com",
        password="Passw0rd!",
    )
    with pytest.raises(SpaceGovernanceError) as corrupt:
        await store.accept_invite_for_user_atomic(
            invite_type="team",
            invite_id=invite["id"],
            user_id=user.id,
            team_id=team_id,
            email=user.email,
        )
    assert corrupt.value.code == "invite_assignment_corrupt"
    persisted = await store.get_team_invite_by_id(invite["id"])
    assert persisted is not None
    assert persisted["status"] == "active"
