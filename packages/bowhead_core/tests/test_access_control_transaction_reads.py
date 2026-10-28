"""Resource-grant reads on a real caller-owned SQLite transaction.

These test ACL primitives and principal projections, not authenticated scope or
Skill/Agent admission. Scope and current group context remain caller obligations.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.share_service import grants_for_viewer, reset_content_sharing_cache

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
READERS = ("epoch", "groups", "matches", "allowlist", "decision", "shares")


@pytest.fixture
async def case(tmp_path):
    path = tmp_path / "access.db"
    store = AccessControlStore(path)
    await store.initialize()
    group = await store.create_group(team_id="team", name="Analysts")
    grant = await store.create_or_update_resource_grant(
        team_id="team", resource_kind="skill", resource_id="method", principal_type="group", principal_id=group.id, permission="use", effect="allow"
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT INTO group_memberships(group_id,team_id,principal_type,principal_id,role,status,created_at) "
            "VALUES(?,'team','user','member','member','active','2026-09-14T00:00:00+00:00')",
            (group.id,),
        )
        await db.commit()
    ctx = AccessContext(user_id="member", team_id="team", workspace_id="space", workspace_role="member", group_ids=(group.id,))
    return SimpleNamespace(path=path, store=store, group=group, grant=grant, ctx=ctx)


async def read(store, case, reader, connection=None):
    if reader == "epoch":
        return await store.get_policy_epoch("team", connection=connection)
    if reader == "groups":
        return await store.principal_group_ids("team", "member", "space", connection=connection)
    if reader == "matches":
        return await store.matching_grants(case.ctx, "skill", "method", "use", connection=connection)
    if reader == "allowlist":
        return await store.has_active_allowlist("team", "skill", "method", "use", connection=connection)
    if reader == "decision":
        return await store.explain_decision(case.ctx, "skill", "method", "use", connection=connection)
    return await store.granted_resource_ids("team", user_id="member", group_ids=(case.group.id,), kind="skill", connection=connection)


@pytest.mark.parametrize("reader", READERS)
async def test_reads_uncommitted_changes_without_owning_commit_or_rollback(case, reader):
    before = await read(case.store, case, reader)
    async with aiosqlite.connect(case.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if reader == "epoch":
            await db.execute("UPDATE access_policy_state SET epoch=epoch+1 WHERE team_id='team'")
        elif reader == "groups":
            await db.execute("UPDATE group_memberships SET status='removed'")
        else:
            await db.execute("UPDATE resource_grants SET effect='deny' WHERE id=?", (case.grant.id,))
        changes = db.total_changes
        result = await read(case.store, case, reader, db)
        if reader == "epoch":
            assert result == before + 1
        elif reader == "groups":
            assert result == ()
        elif reader == "matches":
            assert len(result) == 1 and result[0].effect == "deny"
        elif reader == "allowlist":
            assert result is False
        elif reader == "decision":
            assert result["allowed"] is False and result["reason"] == "denied_by_grant"
        else:
            assert result == frozenset()
        assert db.in_transaction and db.total_changes == changes and db.row_factory is None
        assert await read(case.store, case, reader) == before
        await db.rollback()
        assert await read(case.store, case, reader) == before
        assert (await (await db.execute("SELECT 1")).fetchone()) == (1,)


@pytest.mark.parametrize("reader", READERS)
async def test_supplied_connection_is_authoritative_and_preserves_arbitrary_row_factory(case, reader, tmp_path):
    expected = await read(case.store, case, reader)
    # Opening the constructor path would fail; no fallback DB may be created.
    store = AccessControlStore(tmp_path / "absent" / "unusable.db")

    def dictionary(cursor, row):
        return dict(zip((column[0] for column in cursor.description), row, strict=True))

    async with aiosqlite.connect(case.path) as db:
        db.row_factory = dictionary
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        assert await read(store, case, reader, db) == expected
        assert db.row_factory is dictionary and db.in_transaction and db.total_changes == 0
        assert (await (await db.execute("SELECT 1 AS value")).fetchone()) == {"value": 1}
        await db.rollback()


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("state", ["open", "closed"])
async def test_supplied_connection_requires_an_active_transaction(case, reader, state):
    db = await aiosqlite.connect(case.path)
    try:
        if state == "closed":
            await db.close()
        with pytest.raises(ValueError, match="^access_read_transaction_required$"):
            await read(case.store, case, reader, db)
    finally:
        await db.close()


async def test_empty_principals_do_not_bypass_transaction_validation(case):
    async with aiosqlite.connect(case.path) as db:
        with pytest.raises(ValueError, match="^access_read_transaction_required$"):
            await case.store.matching_grants({}, "skill", "method", "use", connection=db)
        with pytest.raises(ValueError, match="^access_read_transaction_required$"):
            await case.store.granted_resource_ids("", user_id="", kind="skill", connection=db)


async def test_explanation_fallback_reads_the_same_wal_snapshot(case, monkeypatch):
    original = case.store.matching_grants
    entered = []

    async def interleave(ctx, kind, resource, permission, **kwargs):
        result = await original(ctx, kind, resource, permission, **kwargs)
        entered.append(result)
        await AccessControlStore(case.path).create_or_update_resource_grant(
            team_id="team", resource_kind="skill", resource_id="new-method", principal_type="user", principal_id="other", permission="use"
        )
        return result

    monkeypatch.setattr(case.store, "matching_grants", interleave)
    async with aiosqlite.connect(case.path) as db:
        await db.execute("BEGIN")
        result = await case.store.explain_decision(case.ctx, "skill", "new-method", "use", connection=db)
        assert entered == [[]]
        assert result["allowed"] is True and result["reason"] == "allowed_default_no_allowlist"
        await db.rollback()
    # A read transaction is a coherent historical view, not a current-use lease.
    current = await AccessControlStore(case.path).explain_decision(case.ctx, "skill", "new-method", "use")
    assert current["allowed"] is False and current["reason"] == "not_in_allowlist"


@pytest.mark.parametrize("enforce", [False, True])
async def test_permission_coverage_allowlist_and_shadow_semantics_match_existing_reads(case, monkeypatch, enforce):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", str(int(enforce)))
    await case.store.create_or_update_resource_grant(
        team_id="team", resource_kind="skill", resource_id="method", principal_type="user", principal_id="member", permission="admin", effect="deny"
    )
    async with aiosqlite.connect(case.path) as db:
        await db.execute("BEGIN")
        result = await case.store.explain_decision(case.ctx, "skill", "method", "use", connection=db)
        assert result == await case.store.explain_decision(case.ctx, "skill", "method", "use")
        assert result["reason"] == "denied_by_grant" and result["shadow"] is not enforce
        # An unmatched viewer sees the original active-allowlist behavior.
        other = replace(case.ctx, user_id="other", group_ids=())
        denied = await case.store.explain_decision(other, "skill", "method", "read", connection=db)
        assert denied["reason"] == "not_in_allowlist"
        await db.rollback()


@pytest.mark.parametrize("change", ["membership", "grant"])
async def test_sharing_uses_transaction_local_facts(case, monkeypatch, change):
    monkeypatch.setenv("BOWHEAD_CONTENT_SHARING", "1")
    reset_content_sharing_cache()
    try:
        await case.store.create_or_update_resource_grant(
            team_id="team",
            resource_kind="team_skill",
            resource_id="shared-method",
            principal_type="group",
            principal_id=case.group.id,
            permission="use",
        )
        async with aiosqlite.connect(case.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            assert await grants_for_viewer(case.store, team_id="team", user_id="member", kind="team_skill", connection=db) == {"shared-method"}
            if change == "membership":
                await db.execute("UPDATE group_memberships SET status='removed'")
            else:
                await db.execute("UPDATE resource_grants SET effect='deny' WHERE resource_kind='team_skill'")
            assert await grants_for_viewer(case.store, team_id="team", user_id="member", kind="team_skill", connection=db) == frozenset()
            assert await grants_for_viewer(case.store, team_id="team", user_id="member", kind="team_skill") == {"shared-method"}
            assert db.in_transaction
            await db.rollback()
    finally:
        reset_content_sharing_cache()
