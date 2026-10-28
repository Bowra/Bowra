"""Canonical Gateway access projection parity and uncommitted reference scope."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection
from bowhead_core.session_store import SessionStore
from bowhead_core.space_governance import SpaceOperation
from bowhead_gateway.access_context import build_access_context
from test_access_control import _seed_team_ws
from test_access_control import stores as _stores

stores = _stores
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def case(stores):
    users, access = stores
    await SessionStore(users.db_path).initialize()
    team, creator, member, workspace = await _seed_team_ws(users)
    group = await access.create_group(team_id=team.id, workspace_id=workspace, name="Analysts", created_by=creator.id)
    await access.add_group_member(team_id=team.id, group_id=group.id, user_id=member.id, created_by=creator.id)
    scope = ReferenceRequestScope(team_id=team.id, user_id=member.id, workspace_id=workspace)

    async def canonical(selected=scope):
        return await build_access_context(
            user_store=users,
            access_store=access,
            user_id=selected.user_id,
            workspace_id=selected.workspace_id,
            expected_team_id=selected.team_id,
            operation=SpaceOperation.EXECUTE,
        )

    return SimpleNamespace(users=users, access=access, scope=scope, creator=creator, group=group, canonical=canonical)


@pytest.mark.parametrize("role", ["member", "team_admin", "audit_admin", "owner", "viewer"])
async def test_reference_transaction_projection_matches_canonical_gateway(case, role):
    scope = case.scope
    if role == "owner":
        scope = scope.model_copy(update={"user_id": case.creator.id})
    elif role != "member":
        async with aiosqlite.connect(case.users.db_path) as db:
            if role == "viewer":
                await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (scope.workspace_id, scope.user_id))
            else:
                await db.execute("UPDATE team_members SET role=? WHERE team_id=? AND user_id=?", (role, scope.team_id, scope.user_id))
            await db.commit()
    expected = await case.canonical(scope)
    async with aiosqlite.connect(case.users.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        actual = await read_reference_access_context_in_connection(db, scope=scope, access_store=case.access)
        assert actual == expected
        assert db.in_transaction and db.row_factory is None and db.total_changes == 0
        await db.rollback()


async def test_pending_session_roles_groups_and_epoch_are_seen_without_committing(case):
    before = await case.canonical()
    scope = case.scope.model_copy(update={"session_id": "s_pending"})
    async with aiosqlite.connect(case.users.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?", (scope.team_id, scope.user_id))
        await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (scope.workspace_id, scope.user_id))
        await db.execute("UPDATE group_memberships SET status='removed' WHERE group_id=?", (case.group.id,))
        await db.execute("UPDATE access_policy_state SET epoch=epoch+1 WHERE team_id=?", (scope.team_id,))
        await db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES(?,?,?,'2026-09-14','2026-09-14')",
            (scope.session_id, scope.workspace_id, scope.user_id),
        )
        changes = db.total_changes
        actual = await read_reference_access_context_in_connection(db, scope=scope, access_store=case.access)
        assert actual == replace(
            before, team_role="team_admin", workspace_role="viewer", group_ids=(), policy_epoch=before.policy_epoch + 1, is_team_admin=True
        )
        assert await case.canonical() == before
        assert db.in_transaction and db.total_changes == changes and db.row_factory is aiosqlite.Row
        await db.rollback()
        assert await case.canonical() == before


@pytest.mark.parametrize("change", ["user", "team_member", "workspace_member", "team", "workspace", "unknown_role", "session_owner"])
async def test_reference_scope_denials_see_the_callers_transaction(case, change):
    scope = case.scope
    async with aiosqlite.connect(case.users.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if change == "user":
            await db.execute("UPDATE users SET status='disabled' WHERE id=?", (scope.user_id,))
        elif change == "team_member":
            await db.execute("UPDATE team_members SET status='removed' WHERE team_id=? AND user_id=?", (scope.team_id, scope.user_id))
        elif change == "workspace_member":
            await db.execute("UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?", (scope.workspace_id, scope.user_id))
        elif change == "team":
            await db.execute("UPDATE teams SET status='disabled' WHERE id=?", (scope.team_id,))
        elif change == "workspace":
            await db.execute("UPDATE workspaces SET status='archived' WHERE id=?", (scope.workspace_id,))
        elif change == "unknown_role":
            # A malformed historical role is not accepted as a valid projection.
            await db.execute(
                "UPDATE workspace_members SET role='unsupported' WHERE workspace_id=? AND user_id=?", (scope.workspace_id, scope.user_id)
            )
        else:
            scope = scope.model_copy(update={"session_id": "s_other"})
            await db.execute(
                "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES(?,?,?,'2026-09-14','2026-09-14')",
                (scope.session_id, scope.workspace_id, case.creator.id),
            )
        with pytest.raises(ValueError, match="^reference_scope_unavailable$"):
            await read_reference_access_context_in_connection(db, scope=scope, access_store=case.access)
        assert db.in_transaction and db.row_factory is None
        await db.rollback()


async def test_reference_projection_never_opens_its_own_transaction(case):
    async with aiosqlite.connect(case.users.db_path) as db:
        with pytest.raises(ValueError, match="^access_read_transaction_required$"):
            await read_reference_access_context_in_connection(db, scope=case.scope, access_store=case.access)
        assert db.total_changes == 0 and not db.in_transaction
