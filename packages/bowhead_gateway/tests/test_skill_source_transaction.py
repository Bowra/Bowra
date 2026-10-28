"""Real source metadata/permission checks on the caller's SQLite transaction.

Prepared bytes remain outside this metadata reader. A historical read snapshot
is not a current-use lease, and these tests do not claim mixed chat admission.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from test_skill_read_resolution import app_ctx as _app_ctx
from test_skill_read_resolution import case as _case
from test_skill_read_resolution import grant, prepare

app_ctx = _app_ctx
case = _case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def dictionary(cursor, row):
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


async def getter(case, kind, db=None, *, actor=None):
    if kind == "name":
        return await case.instances.get(user_id=actor or case.scope.user_id, name="dupont", connection=db)
    if kind == "id":
        return await case.instances.get_by_id(user_id=actor or case.scope.user_id, skill_id=case.personal.id, connection=db)
    return await case.resolver.users.get_team_skill(actor or case.scope.team_id, case.team.id, only_enabled=True, connection=db)


@pytest.mark.parametrize("kind", ["name", "id", "team"])
async def test_source_getters_see_uncommitted_rows_and_preserve_the_connection(case, kind):
    before = await getter(case, kind)
    assert before is not None
    async with aiosqlite.connect(case.db_path) as db:
        db.row_factory = dictionary
        await db.execute("BEGIN IMMEDIATE")
        if kind == "team":
            await db.execute("UPDATE team_skill SET status='disabled' WHERE id=?", (case.team.id,))
        else:
            await db.execute("UPDATE skill_instances SET enabled=0 WHERE id=?", (case.personal.id,))
        changes = db.total_changes
        current = await getter(case, kind, db)
        assert current is None if kind == "team" else current.enabled is False
        assert await getter(case, kind) == before
        assert db.row_factory is dictionary and db.in_transaction and db.total_changes == changes
        await db.rollback()
        assert await getter(case, kind) == before


@pytest.mark.parametrize("kind", ["name", "id", "team"])
async def test_source_getters_remain_owner_or_team_scoped(case, kind):
    async with aiosqlite.connect(case.db_path) as db:
        await db.execute("BEGIN")
        assert await getter(case, kind, db) is not None
        assert await getter(case, kind, db, actor="unrelated") is None
        await db.rollback()


@pytest.mark.parametrize("kind", ["name", "id", "team"])
@pytest.mark.parametrize("closed", [False, True])
async def test_source_getters_require_an_active_supplied_transaction(case, kind, closed):
    db = await aiosqlite.connect(case.db_path)
    try:
        if closed:
            await db.close()
        with pytest.raises(ValueError, match="^access_read_transaction_required$"):
            await getter(case, kind, db)
    finally:
        await db.close()


@pytest.mark.parametrize("kind", ["builtin", "user", "team"])
async def test_source_proof_revalidation_has_no_connection_or_filesystem_side_effects(case, kind, monkeypatch, tmp_path):
    selected = await prepare(case, kind)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("external dependency called")
        raise RuntimeError("private path must not escape")

    async with aiosqlite.connect(case.db_path) as db:
        db.row_factory = dictionary
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        with monkeypatch.context() as patch:
            patch.setattr(case.resolver, "access_factory", forbidden)
            patch.setattr(aiosqlite, "connect", forbidden)
            for name in ("resolve", "stat", "read_text", "read_bytes", "open"):
                patch.setattr(Path, name, forbidden)
            patch.setattr(case.resolver.users, "db_path", str(tmp_path / "absent" / "unused.db"))
            patch.setattr(case.resolver.instances, "_db_path", str(tmp_path / "absent" / "unused.db"))
            patch.setattr(case.resolver.access, "db_path", str(tmp_path / "absent" / "unused.db"))
            await case.resolver.revalidate_in_connection(db, selected.proof)
        assert not calls
        assert db.in_transaction and db.total_changes == 0 and db.row_factory is dictionary
        await db.rollback()
        assert (await (await db.execute("SELECT 1 AS value")).fetchone()) == {"value": 1}


CHANGES = [
    "builtin_preference",
    "user_pending",
    "user_disabled",
    "user_path",
    "user_name",
    "user_owner",
    "user_kind",
    "team_status",
    "team_approval",
    "team_hash",
    "team_visibility",
    "team_path",
    "team_preference",
    "team_legacy_preference",
    "team_share_group",
    "team_share_grant",
    "builtin_deny",
    "user_deny",
    "team_deny",
    "scope_role",
    "scope_groups",
    "scope_epoch",
]


@pytest.mark.parametrize("change", CHANGES)
async def test_source_revalidation_observes_uncommitted_source_and_policy_changes(case, change):
    kind = change.split("_", 1)[0]
    if kind == "scope":
        kind = "builtin"
    group = None
    resource_grant = None
    if change == "builtin_preference":
        await case.instances.upsert(user_id=case.scope.user_id, name="dupont", source_type="builtin", source_path=str(case.builtin))
    elif change in {"team_preference", "team_legacy_preference"}:
        name = f"team:{case.team.id}" if change == "team_preference" else "dupont"
        await case.instances.upsert(user_id=case.scope.user_id, name=name, source_type="team_shared", source_path=str(case.team_root))
    elif change.startswith("team_share") or change == "scope_groups":
        group = await case.access.create_group(team_id=case.scope.team_id, name="Shared analysts")
        await case.access.add_group_member(team_id=case.scope.team_id, group_id=group.id, user_id=case.scope.user_id)
        if change.startswith("team_share"):
            async with aiosqlite.connect(case.db_path) as db:
                await db.execute("UPDATE team_skill SET visibility='creator_only' WHERE id=?", (case.team.id,))
                await db.commit()
            resource_grant = await grant(case, "team_skill", case.team.id, actor=group.id, principal_type="group")
    elif change.endswith("_deny"):
        resource_grant = await grant(case, "team_skill" if kind == "team" else "skill", case.team.id if kind == "team" else "dupont")
    selected = await prepare(case, kind)
    async with aiosqlite.connect(case.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        before_changes = db.total_changes
        if change in {"builtin_preference", "team_preference", "team_legacy_preference"}:
            name = f"team:{case.team.id}" if change == "team_preference" else "dupont"
            await db.execute("UPDATE skill_instances SET enabled=0 WHERE user_id=? AND name=?", (case.scope.user_id, name))
        elif change in {"user_pending", "user_disabled", "user_path", "user_name", "user_owner", "user_kind"}:
            column, value = {
                "user_pending": ("import_status", "pending"),
                "user_disabled": ("enabled", 0),
                "user_path": ("source_path", "/different/package"),
                "user_name": ("name", "different-method"),
                "user_owner": ("user_id", case.app.owner.id),
                "user_kind": ("source_type", "builtin"),
            }[change]
            await db.execute(f"UPDATE skill_instances SET {column}=? WHERE id=?", (value, case.personal.id))
        elif change in {"team_status", "team_approval", "team_hash", "team_visibility", "team_path"}:
            column, value = {
                "team_status": ("status", "disabled"),
                "team_approval": ("approval", "pending"),
                "team_hash": ("approved_sha256", "0" * 64),
                "team_visibility": ("visibility", "creator_only"),
                "team_path": ("source_path", "/different/package"),
            }[change]
            await db.execute(f"UPDATE team_skill SET {column}=? WHERE id=?", (value, case.team.id))
        elif change in {"team_share_group", "scope_groups"}:
            await db.execute("UPDATE group_memberships SET status='removed' WHERE group_id=?", (group.id,))
        elif change == "team_share_grant":
            await db.execute("UPDATE resource_grants SET revoked_at='2026-09-14' WHERE id=?", (resource_grant.id,))
        elif change.endswith("_deny"):
            await db.execute("UPDATE resource_grants SET effect='deny' WHERE id=?", (resource_grant.id,))
        elif change == "scope_role":
            await db.execute("UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?", (case.scope.team_id, case.scope.user_id))
        else:
            await db.execute(
                "INSERT INTO access_policy_state(team_id,epoch,updated_at) VALUES(?,1,'2026-09-14') ON CONFLICT(team_id) DO UPDATE SET epoch=epoch+1",
                (case.scope.team_id,),
            )
        changes = db.total_changes
        assert changes > before_changes
        with pytest.raises(SkillReadResolutionError):
            await case.resolver.revalidate_in_connection(db, selected.proof)
        assert db.in_transaction and db.total_changes == changes and db.row_factory is None
        # Independent committed-state preparation still sees the original facts.
        assert (await prepare(case, kind)).proof == selected.proof
        await db.rollback()
        await db.execute("BEGIN")
        await case.resolver.revalidate_in_connection(db, selected.proof)
        await db.rollback()


@pytest.mark.parametrize("closed", [False, True])
async def test_source_revalidation_rejects_an_inactive_connection_with_a_finite_error(case, closed):
    selected = await prepare(case, "builtin")
    db = await aiosqlite.connect(case.db_path)
    try:
        if closed:
            await db.close()
        with pytest.raises(SkillReadResolutionError, match="^reference_skill_source_unavailable$"):
            await case.resolver.revalidate_in_connection(db, selected.proof)
    finally:
        await db.close()
