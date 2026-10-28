"""Release A schema compatibility contracts for TeamSubagentStore."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.subagent_compatibility import (
    TeamSubagentApplicationIncompatibleError,
    TeamSubagentCompatibilityDatabaseError,
    TeamSubagentCompatibilityMetadataError,
    TeamSubagentCompatibilityStore,
)
from bowhead_core.subagent_write_boundary import (
    TeamSubagentWriteBoundaryError,
)
from bowhead_core.team_subagent_store import (
    RELEASE_A_COMPATIBILITY_VERSION,
    RELEASE_A_SCHEMA_VERSION,
    TeamSubagentAudienceInvalidError,
    TeamSubagentOwnerInactiveError,
    TeamSubagentStore,
    TeamSubagentVersionConflictError,
)
from bowhead_core.user_store import TeamSubagent, UserStore

_CREATED_AT = "2026-07-28T01:00:00Z"
_UPDATED_AT = "2026-07-28T01:00:01Z"


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


def _run(coro):
    return asyncio.run(coro)


async def _legacy_store(db_path) -> UserStore:
    store = UserStore(db_path)
    await store.initialize()
    return store


async def _insert_legacy_row(
    db_path,
    *,
    subagent_id: str = "osa_legacy",
    name: str = "legacy-agent",
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO team_subagent (
              id,
              team_id,
              name,
              display_name,
              description,
              created_by,
              status,
              created_at,
              updated_at
            ) VALUES (?, 'team_legacy', ?, 'Legacy', 'legacy description',
                      'user_creator', 'enabled', ?, ?)
            """,
            (subagent_id, name, _CREATED_AT, _UPDATED_AT),
        )
        await db.commit()


async def _table_columns(db_path, table_name: str) -> dict[str, tuple[object, ...]]:
    async with aiosqlite.connect(db_path) as db, db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return {str(row[1]): tuple(row) for row in rows}


async def _schema_names(db_path, object_type: str) -> set[str]:
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT name FROM sqlite_schema WHERE type = ?",
            (object_type,),
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    return {str(row[0]) for row in rows}


async def _seed_team_member(
    db_path,
    *,
    team_id: str,
    user_id: str,
    status: str = "active",
    membership_status: str = "active",
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO teams(
                id,
                name,
                max_seats,
                status,
                created_at
            )
            VALUES (?, ?, 20, 'active', ?)
            """,
            (team_id, team_id, _CREATED_AT),
        )
        await db.execute(
            """
            INSERT INTO users(id, team_id, email, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                team_id,
                f"{user_id}@example.com",
                status,
                _CREATED_AT,
            ),
        )
        await db.execute(
            """
            INSERT INTO team_members(
                id,
                user_id,
                team_id,
                role,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'member', ?, ?)
            """,
            (
                f"tm_{team_id}_{user_id}",
                user_id,
                team_id,
                membership_status,
                _CREATED_AT,
            ),
        )
        await db.commit()


def _subagent(
    *,
    subagent_id: str = "osa_boundary",
    name: str = "boundary-agent",
    visibility: str = "team_wide",
) -> TeamSubagent:
    return TeamSubagent(
        id=subagent_id,
        team_id="team_boundary",
        name=name,
        tool_refs=["bi_query"],
        visibility=visibility,
        created_by="user_owner",
        created_at=_CREATED_AT,
        updated_at=_UPDATED_AT,
    )


def test_generic_access_store_rejects_subagent_writes_without_capability(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()

        with pytest.raises(TeamSubagentWriteBoundaryError) as create_error:
            await access.create_or_update_resource_grant(
                team_id="team_boundary",
                resource_kind="team_subagent",
                resource_id="osa_boundary",
                principal_type="user",
                principal_id="user_viewer",
                permission="use",
            )
        assert create_error.value.code == "subagent_write_api_obsolete"

        with pytest.raises(TeamSubagentWriteBoundaryError):
            await access.set_audience_tx(
                team_id="team_boundary",
                resource_kind="team_subagent",
                resource_id="osa_boundary",
                target="team_wide",
                permission="use",
            )
        with pytest.raises(TeamSubagentWriteBoundaryError):
            await access.revoke_grants_for_resource(
                "team_boundary",
                "team_subagent",
                "osa_boundary",
            )
        store = TeamSubagentStore(db_path)
        await store.initialize()
        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
        )
        subagent_grants = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        with pytest.raises(TeamSubagentWriteBoundaryError):
            await access.revoke_resource_grant(
                subagent.team_id,
                subagent_grants[0].id,
            )

        # The generic repository remains unchanged for non-Subagent kinds.
        grant = await access.create_or_update_resource_grant(
            team_id="team_boundary",
            resource_kind="report",
            resource_id="session_boundary",
            principal_type="user",
            principal_id="user_viewer",
            permission="read",
        )
        assert await access.revoke_resource_grant(
            "team_boundary",
            grant.id,
        )

    _run(go())


def test_canonical_create_commits_definition_grant_and_audit_together(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()

        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
            actor_user_id="user_owner",
            audit_action="team_subagent_created",
            audit_payload={"tool_count": 1},
        )

        assert (
            await legacy.get_team_subagent(
                subagent.team_id,
                subagent.id,
            )
            is not None
        )
        grants = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert [(grant.principal_type, grant.principal_id, grant.permission) for grant in grants] == [("team", subagent.team_id, "use")]
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
            SELECT action, target_id, payload_json
            FROM admin_audit_log
            WHERE target_kind='team_subagent' AND target_id=?
            """,
                (subagent.id,),
            ) as cursor,
        ):
            audit = await cursor.fetchone()
        assert audit is not None
        assert audit[0:2] == ("team_subagent_created", subagent.id)
        assert '"tool_count": 1' in str(audit[2])

    _run(go())


def test_canonical_create_rejects_inactive_owner_without_partial_writes(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()
        await _seed_team_member(
            db_path,
            team_id="team_boundary",
            user_id="user_owner",
            membership_status="removed",
        )
        epoch_before = await access.get_policy_epoch("team_boundary")
        subagent = _subagent(subagent_id="osa_rejected", name="rejected-agent")

        with pytest.raises(TeamSubagentOwnerInactiveError):
            await store.create_team_subagent(
                subagent,
                initial_audience_access_store=access,
                actor_user_id="user_owner",
                audit_action="team_subagent_created",
                audit_payload={"tool_count": 1},
                require_active_owner=True,
            )

        assert (
            await legacy.get_team_subagent(
                subagent.team_id,
                subagent.id,
            )
            is None
        )
        assert (
            await access.list_resource_grants(
                subagent.team_id,
                resource_kind="team_subagent",
                resource_id=subagent.id,
            )
            == []
        )
        assert await access.get_policy_epoch("team_boundary") == epoch_before
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT COUNT(*)
                FROM admin_audit_log
                WHERE target_kind='team_subagent' AND target_id=?
                """,
                (subagent.id,),
            ) as cursor,
        ):
            assert await cursor.fetchone() == (0,)

    _run(go())


def test_create_rejects_initial_audience_store_for_non_team_wide_row(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()

        with pytest.raises(
            ValueError,
            match="initial audience store requires team_wide visibility",
        ):
            await store.create_team_subagent(
                _subagent(visibility="creator_only"),
                initial_audience_access_store=access,
            )

    _run(go())


def test_legacy_user_store_delegate_discovers_and_cleans_shared_acl(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        subagent = _subagent()

        await legacy.create_team_subagent(subagent)
        grants = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert len(grants) == 1

        assert await legacy.delete_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert (
            await access.list_resource_grants(
                subagent.team_id,
                resource_kind="team_subagent",
                resource_id=subagent.id,
            )
            == []
        )

    _run(go())


def test_create_failure_rolls_back_definition_grant_epoch_and_audit(
    tmp_path,
    monkeypatch,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()

        async def fail_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(store, "_write_audit_conn", fail_audit)
        subagent = _subagent()
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await store.create_team_subagent(
                subagent,
                initial_audience_access_store=access,
                actor_user_id="user_owner",
                audit_action="team_subagent_created",
            )

        assert (
            await legacy.get_team_subagent(
                subagent.team_id,
                subagent.id,
            )
            is None
        )
        assert (
            await access.list_resource_grants(
                subagent.team_id,
                resource_kind="team_subagent",
                resource_id=subagent.id,
            )
            == []
        )
        assert await access.get_policy_epoch(subagent.team_id) == 0

    _run(go())


def test_delete_failure_restores_definition_and_active_grants(
    tmp_path,
    monkeypatch,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()
        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
        )

        async def fail_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(store, "_write_audit_conn", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await store.delete_team_subagent(
                subagent.team_id,
                subagent.id,
                access_store=access,
                actor_user_id="user_owner",
                audit_action="team_subagent_deleted",
            )

        assert (
            await legacy.get_team_subagent(
                subagent.team_id,
                subagent.id,
            )
            is not None
        )
        grants = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert len(grants) == 1
        assert grants[0].revoked_at is None

    _run(go())


def test_update_failure_restores_definition_and_omits_audit(
    tmp_path,
    monkeypatch,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        store = TeamSubagentStore(db_path)
        await store.initialize()
        subagent = _subagent(visibility="creator_only")
        await store.create_team_subagent(subagent)

        async def fail_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(store, "_write_audit_conn", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await store.update_team_subagent_meta(
                subagent.team_id,
                subagent.id,
                description="must roll back",
                actor_user_id="user_owner",
                audit_action="team_subagent_updated",
            )

        restored = await legacy.get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert restored is not None
        assert restored.description == ""
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
            SELECT COUNT(*)
            FROM admin_audit_log
            WHERE target_kind='team_subagent' AND target_id=?
            """,
                (subagent.id,),
            ) as cursor,
        ):
            audit_count = await cursor.fetchone()
        assert audit_count == (0,)

    _run(go())


def test_compare_and_update_is_versioned_and_semantic_noop_is_quiet(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        store = TeamSubagentStore(
            db_path,
            clock=_FixedClock(datetime(2026, 7, 28, 2, tzinfo=UTC)),
        )
        await store.initialize()
        subagent = _subagent(visibility="creator_only")
        await store.create_team_subagent(subagent)

        changed = await store.compare_and_update_team_subagent(
            subagent.team_id,
            subagent.id,
            expected_version=1,
            description="changed",
            tool_refs=["bi_query"],
            actor_user_id="user_owner",
            audit_action="team_subagent_updated",
        )
        assert changed is not None
        assert changed.changed is True
        assert changed.version == 2

        with pytest.raises(TeamSubagentVersionConflictError) as conflict:
            await store.compare_and_update_team_subagent(
                subagent.team_id,
                subagent.id,
                expected_version=1,
                description="stale overwrite",
                actor_user_id="user_owner",
                audit_action="team_subagent_updated",
            )
        assert conflict.value.expected_version == 1
        assert conflict.value.current_version == 2

        noop = await store.compare_and_update_team_subagent(
            subagent.team_id,
            subagent.id,
            expected_version=2,
            description="changed",
            tool_refs=["bi_query"],
            actor_user_id="user_owner",
            audit_action="team_subagent_updated",
        )
        assert noop is not None
        assert noop.changed is False
        assert noop.version == 2

        persisted = await legacy.get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert persisted is not None
        assert persisted.description == "changed"
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT row_version, updated_at
                FROM team_subagent
                WHERE id=? AND team_id=?
                """,
                (subagent.id, subagent.team_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        assert row == (2, "2026-07-28T02:00:00+00:00")

        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT COUNT(*)
                FROM admin_audit_log
                WHERE target_kind='team_subagent'
                  AND target_id=?
                  AND action='team_subagent_updated'
                """,
                (subagent.id,),
            ) as cursor,
        ):
            audit_count = await cursor.fetchone()
        assert audit_count == (1,)

    _run(go())


def test_compare_and_update_audit_failure_restores_definition_and_version(
    tmp_path,
    monkeypatch,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        store = TeamSubagentStore(db_path)
        await store.initialize()
        subagent = _subagent(visibility="creator_only")
        await store.create_team_subagent(subagent)

        async def fail_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(store, "_write_audit_conn", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await store.compare_and_update_team_subagent(
                subagent.team_id,
                subagent.id,
                expected_version=1,
                description="must roll back",
                actor_user_id="user_owner",
                audit_action="team_subagent_updated",
            )

        restored = await legacy.get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert restored is not None
        assert restored.description == ""
        assert restored.row_version == 1

    _run(go())


def test_exact_audience_replace_is_atomic_versioned_and_preserves_denies(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(
            db_path,
            clock=_FixedClock(datetime(2026, 7, 28, 3, tzinfo=UTC)),
        )
        await store.initialize()
        await _seed_team_member(
            db_path,
            team_id="team_boundary",
            user_id="user_owner",
        )
        await _seed_team_member(
            db_path,
            team_id="team_boundary",
            user_id="user_viewer",
        )
        group = await access.create_group(
            team_id="team_boundary",
            name="Reviewers",
            created_by="user_owner",
        )
        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT INTO resource_grants(
                    id,
                    team_id,
                    resource_kind,
                    resource_id,
                    principal_type,
                    principal_id,
                    permission,
                    effect,
                    created_by,
                    created_at,
                    updated_at,
                    expires_at,
                    revoked_at
                )
                VALUES (
                    'grant_security_deny',
                    'team_boundary',
                    'team_subagent',
                    ?,
                    'user',
                    'user_blocked',
                    'use',
                    'deny',
                    'security_admin',
                    ?,
                    ?,
                    NULL,
                    NULL
                )
                """,
                (subagent.id, _CREATED_AT, _CREATED_AT),
            )
            await db.commit()

        before_epoch = await access.get_policy_epoch(subagent.team_id)
        replaced = await store.replace_team_subagent_audience(
            subagent.team_id,
            subagent.id,
            expected_version=1,
            mode="custom",
            user_ids=["user_viewer", "user_viewer"],
            group_ids=[group.id],
            access_store=access,
            actor_user_id="user_owner",
            audit_action="subagent_audience_changed",
        )
        assert replaced is not None
        assert replaced.changed is True
        assert replaced.version == 2
        assert replaced.user_ids == ("user_viewer",)
        assert replaced.group_ids == (group.id,)
        assert await access.get_policy_epoch(subagent.team_id) == before_epoch + 1

        active = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert {
            (
                grant.effect,
                grant.principal_type,
                grant.principal_id,
                grant.permission,
                grant.expires_at,
            )
            for grant in active
        } == {
            ("allow", "user", "user_viewer", "use", None),
            ("allow", "group", group.id, "use", None),
            ("deny", "user", "user_blocked", "use", None),
        }
        persisted = await legacy.get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert persisted is not None
        assert persisted.visibility == "creator_only"
        assert persisted.row_version == 2
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT audience_mode, payload_json
                FROM team_subagent
                JOIN admin_audit_log
                  ON admin_audit_log.target_id=team_subagent.id
                WHERE team_subagent.id=?
                  AND admin_audit_log.action='subagent_audience_changed'
                """,
                (subagent.id,),
            ) as cursor,
        ):
            audience_mode, payload_json = await cursor.fetchone()
        assert audience_mode == "custom"
        assert '"user_count": 1' in payload_json
        assert '"group_count": 1' in payload_json
        assert "user_viewer" not in payload_json
        assert group.id not in payload_json

        noop_epoch = await access.get_policy_epoch(subagent.team_id)
        noop = await store.replace_team_subagent_audience(
            subagent.team_id,
            subagent.id,
            expected_version=2,
            mode="custom",
            user_ids=["user_viewer"],
            group_ids=[group.id],
            access_store=access,
            actor_user_id="user_owner",
            audit_action="subagent_audience_changed",
        )
        assert noop is not None
        assert noop.changed is False
        assert noop.version == 2
        assert await access.get_policy_epoch(subagent.team_id) == noop_epoch

        teamwide = await store.replace_team_subagent_audience(
            subagent.team_id,
            subagent.id,
            expected_version=2,
            mode="teamwide",
            access_store=access,
            actor_user_id="user_owner",
            audit_action="subagent_audience_changed",
        )
        assert teamwide is not None
        assert teamwide.version == 3
        active_after_teamwide = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert {(grant.effect, grant.principal_type, grant.principal_id) for grant in active_after_teamwide} == {
            ("allow", "team", subagent.team_id),
            ("deny", "user", "user_blocked"),
        }

        private = await store.replace_team_subagent_audience(
            subagent.team_id,
            subagent.id,
            expected_version=3,
            mode="private",
            access_store=access,
            actor_user_id="user_owner",
            audit_action="subagent_audience_changed",
        )
        assert private is not None
        assert private.version == 4
        active_after_private = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert [(grant.effect, grant.principal_type, grant.principal_id) for grant in active_after_private] == [("deny", "user", "user_blocked")]

    _run(go())


def test_exact_audience_rejects_invalid_principals_without_partial_write(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()
        await _seed_team_member(
            db_path,
            team_id="team_boundary",
            user_id="user_owner",
        )
        await _seed_team_member(
            db_path,
            team_id="team_other",
            user_id="user_other",
        )
        other_group = await access.create_group(
            team_id="team_other",
            name="Other team",
        )
        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
        )
        before_epoch = await access.get_policy_epoch(subagent.team_id)

        with pytest.raises(
            TeamSubagentAudienceInvalidError,
        ) as invalid:
            await store.replace_team_subagent_audience(
                subagent.team_id,
                subagent.id,
                expected_version=1,
                mode="custom",
                user_ids=["user_other"],
                group_ids=[other_group.id],
                access_store=access,
                actor_user_id="user_owner",
                audit_action="subagent_audience_changed",
            )
        assert invalid.value.invalid_user_ids == ("user_other",)
        assert invalid.value.invalid_group_ids == (other_group.id,)
        assert await access.get_policy_epoch(subagent.team_id) == before_epoch

        persisted = await _legacy_store(db_path)
        row = await persisted.get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert row is not None
        assert row.row_version == 1
        assert row.visibility == "team_wide"
        active = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert [(grant.effect, grant.principal_type, grant.principal_id) for grant in active] == [("allow", "team", subagent.team_id)]

    _run(go())


def test_exact_audience_audit_failure_rolls_back_grants_version_and_epoch(
    tmp_path,
    monkeypatch,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy = await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()
        await _seed_team_member(
            db_path,
            team_id="team_boundary",
            user_id="user_owner",
        )
        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
        )
        before_epoch = await access.get_policy_epoch(subagent.team_id)

        async def fail_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(store, "_write_audit_conn", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await store.replace_team_subagent_audience(
                subagent.team_id,
                subagent.id,
                expected_version=1,
                mode="private",
                access_store=access,
                actor_user_id="user_owner",
                audit_action="subagent_audience_changed",
            )

        restored = await legacy.get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert restored is not None
        assert restored.row_version == 1
        assert restored.visibility == "team_wide"
        assert await access.get_policy_epoch(subagent.team_id) == before_epoch
        active = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert [(grant.effect, grant.principal_type, grant.principal_id) for grant in active] == [("allow", "team", subagent.team_id)]

    _run(go())


def test_exact_audience_enforces_200_principal_limit_after_deduplication(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        access = AccessControlStore(db_path)
        await access.initialize()
        store = TeamSubagentStore(db_path)
        await store.initialize()
        await _seed_team_member(
            db_path,
            team_id="team_boundary",
            user_id="user_owner",
        )
        user_ids = [f"user_{index:03d}" for index in range(200)]
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                """
                INSERT INTO users(id, team_id, email, status, created_at)
                VALUES (?, 'team_boundary', ?, 'active', ?)
                """,
                [
                    (
                        user_id,
                        f"{user_id}@example.com",
                        _CREATED_AT,
                    )
                    for user_id in user_ids
                ],
            )
            await db.executemany(
                """
                INSERT INTO team_members(
                    id,
                    user_id,
                    team_id,
                    role,
                    status,
                    created_at
                )
                VALUES (?, ?, 'team_boundary', 'member', 'active', ?)
                """,
                [
                    (
                        f"tm_{user_id}",
                        user_id,
                        _CREATED_AT,
                    )
                    for user_id in user_ids
                ],
            )
            await db.commit()
        subagent = _subagent()
        await store.create_team_subagent(
            subagent,
            initial_audience_access_store=access,
        )

        accepted = await store.replace_team_subagent_audience(
            subagent.team_id,
            subagent.id,
            expected_version=1,
            mode="custom",
            user_ids=[*user_ids, user_ids[0]],
            access_store=access,
            actor_user_id="user_owner",
            audit_action="subagent_audience_changed",
        )
        assert accepted is not None
        assert accepted.version == 2
        assert len(accepted.user_ids) == 200
        active = await access.list_resource_grants(
            subagent.team_id,
            resource_kind="team_subagent",
            resource_id=subagent.id,
        )
        assert len(active) == 200

        with pytest.raises(
            TeamSubagentAudienceInvalidError,
            match="exceeds 200",
        ):
            await store.replace_team_subagent_audience(
                subagent.team_id,
                subagent.id,
                expected_version=2,
                mode="custom",
                user_ids=[*user_ids, "user_201"],
                access_store=access,
                actor_user_id="user_owner",
                audit_action="subagent_audience_changed",
            )
        persisted = await (await _legacy_store(db_path)).get_team_subagent(
            subagent.team_id,
            subagent.id,
        )
        assert persisted is not None
        assert persisted.row_version == 2

    _run(go())


def test_fresh_release_a_schema_is_additive_and_dormant(tmp_path) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        clock = _FixedClock(datetime(2026, 7, 28, 2, 3, 4, tzinfo=UTC))
        store = TeamSubagentStore(db_path, clock=clock)

        await store.initialize()

        columns = await _table_columns(db_path, "team_subagent")
        assert {
            "owner_user_id",
            "row_version",
            "audience_mode",
            "lifecycle_state",
            "quarantine_started_at",
            "quarantine_expires_at",
            "quarantine_reason",
            "quarantined_owner_user_id",
        } <= columns.keys()
        metadata = await store.get_schema_metadata()
        assert metadata.schema_version == RELEASE_A_SCHEMA_VERSION
        assert metadata.minimum_reader_version == RELEASE_A_COMPATIBILITY_VERSION
        assert metadata.minimum_writer_version == RELEASE_A_COMPATIBILITY_VERSION
        assert metadata.owner_semantics_activated_at is None
        assert metadata.lifecycle_semantics_activated_at is None
        assert metadata.audience_semantics_activated_at is None
        assert metadata.updated_at == "2026-07-28T02:03:04Z"
        assert "team_subagent_app_compatibility" in await _schema_names(
            db_path,
            "table",
        )
        assert {
            "idx_team_subagent_team_owner",
            "idx_team_subagent_team_updated",
            "idx_team_subagent_quarantine_expiry",
            "idx_team_subagent_team_audience",
        } <= await _schema_names(db_path, "index")
        assert {
            "team_subagent_invariant_guard_update",
            "team_subagent_version_guard_update",
            "team_subagent_creator_immutable_update",
            "team_subagent_invariant_guard_insert",
        } <= await _schema_names(db_path, "trigger")

    _run(go())


def test_compatible_application_registers_refreshes_and_unregisters(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        now = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
        clock = _FixedClock(now)
        await TeamSubagentStore(db_path, clock=clock).initialize()
        compatibility = TeamSubagentCompatibilityStore(
            db_path,
            clock=clock,
        )

        admitted = await compatibility.record_heartbeat(
            instance_id="gateway:test",
            reader_version="0001",
            writer_version="0001",
        )
        assert admitted.compatible is True
        assert admitted.minimum_reader_version == "0001"
        assert admitted.minimum_writer_version == "0001"

        clock._value = now + timedelta(seconds=30)
        await compatibility.record_heartbeat(
            instance_id="gateway:test",
            reader_version="0001",
            writer_version="0001",
        )
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT started_at, heartbeat_at
                FROM team_subagent_app_compatibility
                WHERE instance_id = 'gateway:test'
                """,
            ) as cursor,
        ):
            row = await cursor.fetchone()
        assert row == (
            "2026-07-28T04:00:00Z",
            "2026-07-28T04:00:30Z",
        )

        await compatibility.remove_heartbeat("gateway:test")
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT COUNT(*)
                FROM team_subagent_app_compatibility
                """,
            ) as cursor,
        ):
            assert await cursor.fetchone() == (0,)

    _run(go())


def test_application_below_metadata_floor_is_not_registered(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        await TeamSubagentStore(db_path).initialize()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE team_subagent_schema_meta
                SET minimum_reader_version = '0002',
                    minimum_writer_version = '0002'
                WHERE singleton = 1
                """,
            )
            await db.commit()

        compatibility = TeamSubagentCompatibilityStore(db_path)
        decision = await compatibility.check_application(
            reader_version="0001",
            writer_version="0001",
        )
        assert decision.compatible is False
        assert decision.reader_compatible is False
        assert decision.writer_compatible is False
        with pytest.raises(
            TeamSubagentApplicationIncompatibleError,
        ) as captured:
            await compatibility.record_heartbeat(
                instance_id="gateway:old",
                reader_version="0001",
                writer_version="0001",
            )
        assert captured.value.code == "team_subagent_application_incompatible"
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT COUNT(*)
                FROM team_subagent_app_compatibility
                """,
            ) as cursor,
        ):
            assert await cursor.fetchone() == (0,)

    _run(go())


def test_deployment_admission_reads_a_wal_database(tmp_path) -> None:
    """Production runs WAL; the gate must admit against it.

    A WAL database materialises a `-shm` sidecar on open, so this exercises the
    real filesystem interaction the admission container performs rather than the
    default journal mode tests otherwise get.
    """

    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        clock = _FixedClock(datetime(2026, 7, 29, 5, 0, tzinfo=UTC))
        await TeamSubagentStore(db_path, clock=clock).initialize()
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                assert (await cursor.fetchone())[0].lower() == "wal"
            await db.commit()

        report = await TeamSubagentCompatibilityStore(
            db_path,
            clock=clock,
        ).deployment_admission(
            reader_version=RELEASE_A_COMPATIBILITY_VERSION,
            writer_version=RELEASE_A_COMPATIBILITY_VERSION,
        )

        assert report.ready is True

    _run(go())


def test_deployment_admission_on_readonly_dir_does_not_claim_missing_schema(
    tmp_path,
) -> None:
    """The exact shape that blocked the 1.0.0 rollout.

    The admission container mounted the data directory read-only. A WAL database
    must create its `-shm` sidecar even for a `mode=ro` reader, so SQLite refused
    to open a database whose schema was in fact complete — and the driver error
    was reported as `schema_meta_missing`, pointing at a migration that did not
    exist. Whatever the outcome, it must not name the schema.
    """

    async def go() -> None:
        data_dir = tmp_path / "sqlite"
        data_dir.mkdir()
        db_path = data_dir / "bowhead.db"
        await _legacy_store(db_path)
        await TeamSubagentStore(db_path).initialize()
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.commit()

        store = TeamSubagentCompatibilityStore(db_path)
        data_dir.chmod(0o500)  # readable + executable, not writable
        try:
            if os.access(data_dir, os.W_OK):
                pytest.skip("running with write override (root); cannot deny the dir")
            try:
                report = await store.deployment_admission(
                    reader_version=RELEASE_A_COMPATIBILITY_VERSION,
                    writer_version=RELEASE_A_COMPATIBILITY_VERSION,
                )
            except TeamSubagentCompatibilityMetadataError as exc:  # pragma: no cover
                pytest.fail(f"unreadable database reported as a schema verdict: {exc}")
            except TeamSubagentCompatibilityDatabaseError:
                pass  # correct: an access fault, named as one
            else:
                assert report.ready is True  # or it simply read fine
        finally:
            data_dir.chmod(0o700)

    _run(go())


def test_deployment_admission_names_an_unreadable_database(tmp_path) -> None:
    """An unreadable database must not be reported as a missing schema.

    The two demand opposite operator responses — fix access vs. migrate — and
    conflating them cost a real deploy investigation.
    """

    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        await TeamSubagentStore(db_path).initialize()
        # Truncating the header keeps the file present (so the is_file() guard
        # passes) while making every read fail at the driver.
        db_path.write_bytes(b"not a sqlite database")

        store = TeamSubagentCompatibilityStore(db_path)
        with pytest.raises(TeamSubagentCompatibilityDatabaseError) as caught:
            await store.deployment_admission(
                reader_version=RELEASE_A_COMPATIBILITY_VERSION,
                writer_version=RELEASE_A_COMPATIBILITY_VERSION,
            )
        assert "unreadable" in str(caught.value)
        assert "schema_meta_missing" not in str(caught.value)

    _run(go())


def test_deployment_admission_classifies_live_stale_and_malformed_rows(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        checked_at = datetime(2026, 7, 28, 5, 0, tzinfo=UTC)
        clock = _FixedClock(checked_at)
        await TeamSubagentStore(db_path, clock=clock).initialize()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE team_subagent_schema_meta
                SET minimum_reader_version = '0002',
                    minimum_writer_version = '0002'
                WHERE singleton = 1
                """,
            )
            await db.executemany(
                """
                INSERT INTO team_subagent_app_compatibility (
                  instance_id,
                  reader_version,
                  writer_version,
                  started_at,
                  heartbeat_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        "gateway:new",
                        "0002",
                        "0002",
                        "2026-07-28T04:59:00Z",
                        "2026-07-28T05:00:00Z",
                    ),
                    (
                        "gateway:old",
                        "0001",
                        "0001",
                        "2026-07-28T04:59:00Z",
                        "2026-07-28T04:59:59Z",
                    ),
                    (
                        "gateway:bad-version",
                        "main",
                        "main",
                        "2026-07-28T04:59:00Z",
                        "2026-07-28T04:59:58Z",
                    ),
                    (
                        "gateway:stale",
                        "0001",
                        "0001",
                        "2026-07-28T04:00:00Z",
                        "2026-07-28T04:58:29Z",
                    ),
                    (
                        "gateway:bad-time",
                        "0002",
                        "0002",
                        "2026-07-28T04:59:00Z",
                        "not-a-time",
                    ),
                ],
            )
            await db.commit()

        compatibility = TeamSubagentCompatibilityStore(
            db_path,
            clock=clock,
        )
        blocked = await compatibility.deployment_admission(
            reader_version="0002",
            writer_version="0002",
        )
        assert blocked.candidate_compatible is True
        assert blocked.live_instances == 3
        assert blocked.compatible_live_instances == 1
        assert blocked.incompatible_live_instances == 2
        assert blocked.malformed_instances == 2
        assert blocked.stale_instances == 1
        assert blocked.migration_pending is False
        assert blocked.ready is False

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE team_subagent_app_compatibility
                SET heartbeat_at = '2026-07-28T04:58:29Z'
                WHERE instance_id = 'gateway:old'
                """,
            )
            await db.execute(
                """
                DELETE FROM team_subagent_app_compatibility
                WHERE instance_id IN ('gateway:bad-version', 'gateway:bad-time')
                """,
            )
            await db.commit()
        ready = await compatibility.deployment_admission(
            reader_version="0002",
            writer_version="0002",
        )
        assert ready.live_instances == 1
        assert ready.compatible_live_instances == 1
        assert ready.incompatible_live_instances == 0
        assert ready.malformed_instances == 0
        assert ready.stale_instances == 2
        assert ready.ready is True

    _run(go())


def test_heartbeat_cleanup_is_bounded_and_metadata_is_fail_closed(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        now = datetime(2026, 7, 28, 6, 0, tzinfo=UTC)
        clock = _FixedClock(now)
        await TeamSubagentStore(db_path, clock=clock).initialize()
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                """
                INSERT INTO team_subagent_app_compatibility (
                  instance_id,
                  reader_version,
                  writer_version,
                  started_at,
                  heartbeat_at
                ) VALUES (?, '0001', '0001', ?, ?)
                """,
                [
                    (
                        f"gateway:stale-{index:03d}",
                        "2026-07-26T00:00:00Z",
                        "2026-07-26T00:00:00Z",
                    )
                    for index in range(101)
                ],
            )
            await db.execute(
                """
                INSERT INTO team_subagent_app_compatibility (
                  instance_id,
                  reader_version,
                  writer_version,
                  started_at,
                  heartbeat_at
                ) VALUES (
                  'gateway:malformed',
                  '0001',
                  '0001',
                  '2026-07-26T00:00:00Z',
                  '0000-invalid'
                )
                """,
            )
            await db.commit()
        compatibility = TeamSubagentCompatibilityStore(
            db_path,
            clock=clock,
        )
        await compatibility.record_heartbeat(
            instance_id="gateway:current",
            reader_version="0001",
            writer_version="0001",
        )
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                """
                SELECT instance_id
                FROM team_subagent_app_compatibility
                ORDER BY instance_id
                """,
            ) as cursor,
        ):
            remaining = [row[0] for row in await cursor.fetchall()]
        assert len(remaining) == 3
        assert "gateway:current" in remaining
        assert "gateway:malformed" in remaining

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE team_subagent_schema_meta
                SET minimum_writer_version = 'release-a'
                WHERE singleton = 1
                """,
            )
            await db.commit()
        with pytest.raises(TeamSubagentCompatibilityMetadataError):
            await compatibility.record_heartbeat(
                instance_id="gateway:current",
                reader_version="0001",
                writer_version="0001",
            )

    _run(go())


def test_legacy_rows_and_writers_remain_compatible_before_activation(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        legacy_store = await _legacy_store(db_path)
        await _insert_legacy_row(db_path)
        await TeamSubagentStore(db_path).initialize()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM team_subagent WHERE id = 'osa_legacy'",
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        assert row["description"] == "legacy description"
        assert row["visibility"] == "team_wide"
        assert row["owner_user_id"] is None
        assert row["row_version"] == 1
        assert row["audience_mode"] == "private"
        assert row["lifecycle_state"] == "active"

        # The legacy writer neither supplies Owner nor bumps row_version.
        await legacy_store.update_team_subagent_meta(
            "team_legacy",
            "osa_legacy",
            description="legacy writer update",
        )
        await legacy_store.create_team_subagent(
            TeamSubagent(
                id="osa_legacy_created_after_release_a",
                team_id="team_legacy",
                name="legacy-created-after-release-a",
                created_by=None,
                created_at=_CREATED_AT,
                updated_at=_UPDATED_AT,
            ),
        )
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                """
                SELECT description, row_version
                FROM team_subagent
                WHERE id = 'osa_legacy'
                """,
            ) as cursor:
                updated = await cursor.fetchone()
            async with db.execute(
                """
                SELECT owner_user_id, row_version
                FROM team_subagent
                WHERE id = 'osa_legacy_created_after_release_a'
                """,
            ) as cursor:
                created = await cursor.fetchone()
        assert updated == ("legacy writer update", 1)
        assert created == (None, 1)

    _run(go())


def test_initialize_rejects_unsafe_private_quarantine_policy(
    tmp_path,
    monkeypatch,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        with pytest.raises(
            RuntimeError,
            match="BOWHEAD_SUBAGENT_PRIVATE_QUARANTINE_DAYS must be >= 30",
        ):
            await TeamSubagentStore(db_path).initialize()

    monkeypatch.setenv(
        "BOWHEAD_SUBAGENT_PRIVATE_QUARANTINE_DAYS",
        "29",
    )
    _run(go())


def test_partial_migration_retry_is_idempotent(tmp_path) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "ALTER TABLE team_subagent ADD COLUMN owner_user_id TEXT",
            )
            await db.commit()

        store = TeamSubagentStore(db_path)
        await store.initialize()
        await store.initialize()

        columns = await _table_columns(db_path, "team_subagent")
        assert sum(name == "owner_user_id" for name in columns) == 1
        assert columns["row_version"][4] == "1"
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM team_subagent_schema_meta",
            ) as cursor,
        ):
            metadata_rows = int((await cursor.fetchone())[0])
        assert metadata_rows == 1

    _run(go())


def test_initialize_never_lowers_future_metadata_or_clears_activation(
    tmp_path,
) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        store = TeamSubagentStore(db_path)
        await store.initialize()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE team_subagent_schema_meta
                SET schema_version = 3,
                    minimum_reader_version = '0002',
                    minimum_writer_version = '0002',
                    owner_semantics_activated_at = '2026-07-28T03:00:00Z',
                    lifecycle_semantics_activated_at = '2026-07-28T03:00:00Z',
                    audience_semantics_activated_at = '2026-07-28T03:00:00Z',
                    updated_at = '2026-07-28T03:00:00Z'
                WHERE singleton = 1
                """,
            )
            await db.commit()

        await TeamSubagentStore(
            db_path,
            clock=_FixedClock(datetime(2026, 7, 29, tzinfo=UTC)),
        ).initialize()

        metadata = await store.get_schema_metadata()
        assert metadata.schema_version == 3
        assert metadata.minimum_reader_version == "0002"
        assert metadata.minimum_writer_version == "0002"
        assert metadata.owner_semantics_activated_at == "2026-07-28T03:00:00Z"
        assert metadata.lifecycle_semantics_activated_at == "2026-07-28T03:00:00Z"
        assert metadata.audience_semantics_activated_at == "2026-07-28T03:00:00Z"
        assert metadata.updated_at == "2026-07-28T03:00:00Z"

    _run(go())


def test_incompatible_partial_column_fails_and_rolls_back(tmp_path) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "ALTER TABLE team_subagent ADD COLUMN row_version TEXT",
            )
            await db.commit()

        with pytest.raises(
            RuntimeError,
            match="team_subagent_schema_incompatible: column row_version",
        ):
            await TeamSubagentStore(db_path).initialize()

        # owner_user_id was attempted first in the same migration transaction.
        # The incompatible row_version must roll that ALTER back.
        columns = await _table_columns(db_path, "team_subagent")
        assert "owner_user_id" not in columns
        assert "team_subagent_schema_meta" not in await _schema_names(
            db_path,
            "table",
        )

    _run(go())


def test_missing_legacy_table_fails_without_creating_metadata(tmp_path) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        with pytest.raises(
            RuntimeError,
            match="team_subagent_base_schema_missing",
        ):
            await TeamSubagentStore(db_path).initialize()
        assert "team_subagent_schema_meta" not in await _schema_names(
            db_path,
            "table",
        )

    _run(go())


def test_activated_guards_enforce_raw_sql_contracts(tmp_path) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        await TeamSubagentStore(db_path).initialize()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT INTO team_subagent (
                  id,
                  team_id,
                  name,
                  description,
                  created_by,
                  status,
                  created_at,
                  updated_at,
                  owner_user_id,
                  row_version,
                  audience_mode,
                  lifecycle_state
                ) VALUES (
                  'osa_guarded',
                  'team_guarded',
                  'guarded',
                  'before',
                  'user_creator',
                  'enabled',
                  ?,
                  ?,
                  'user_owner',
                  7,
                  'private',
                  'active'
                )
                """,
                (_CREATED_AT, _UPDATED_AT),
            )
            await db.execute(
                """
                UPDATE team_subagent_schema_meta
                SET owner_semantics_activated_at = ?,
                    lifecycle_semantics_activated_at = ?,
                    audience_semantics_activated_at = ?
                WHERE singleton = 1
                """,
                (_UPDATED_AT, _UPDATED_AT, _UPDATED_AT),
            )
            await db.commit()

            with pytest.raises(
                aiosqlite.IntegrityError,
                match="team_subagent_version_required",
            ):
                await db.execute(
                    """
                    UPDATE team_subagent
                    SET description = 'without version'
                    WHERE id = 'osa_guarded'
                    """,
                )
            await db.rollback()

            with pytest.raises(
                aiosqlite.IntegrityError,
                match="team_subagent_invariant_violation",
            ):
                await db.execute(
                    """
                    UPDATE team_subagent
                    SET lifecycle_state = 'quarantined',
                        row_version = 8
                    WHERE id = 'osa_guarded'
                    """,
                )
            await db.rollback()

            with pytest.raises(
                aiosqlite.IntegrityError,
                match="team_subagent_created_by_immutable",
            ):
                await db.execute(
                    """
                    UPDATE team_subagent
                    SET created_by = 'user_other',
                        row_version = 8
                    WHERE id = 'osa_guarded'
                    """,
                )
            await db.rollback()

            await db.execute(
                """
                UPDATE team_subagent
                SET description = 'with exact version',
                    row_version = 8
                WHERE id = 'osa_guarded'
                """,
            )
            await db.commit()

            with pytest.raises(
                aiosqlite.IntegrityError,
                match="team_subagent_invariant_violation",
            ):
                await db.execute(
                    """
                    INSERT INTO team_subagent (
                      id,
                      team_id,
                      name,
                      created_at,
                      updated_at
                    ) VALUES (
                      'osa_ownerless',
                      'team_guarded',
                      'ownerless',
                      ?,
                      ?
                    )
                    """,
                    (_CREATED_AT, _UPDATED_AT),
                )
            await db.rollback()

            async with db.execute(
                """
                SELECT description, row_version
                FROM team_subagent
                WHERE id = 'osa_guarded'
                """,
            ) as cursor:
                row = await cursor.fetchone()
        assert row == ("with exact version", 8)

    _run(go())


def test_clock_must_be_timezone_aware(tmp_path) -> None:
    async def go() -> None:
        db_path = tmp_path / "bowhead.db"
        await _legacy_store(db_path)
        store = TeamSubagentStore(
            db_path,
            clock=_FixedClock(datetime(2026, 7, 28)),
        )
        with pytest.raises(
            ValueError,
            match="team_subagent_clock_must_return_aware_datetime",
        ):
            await store.initialize()
        columns = await _table_columns(db_path, "team_subagent")
        assert "owner_user_id" not in columns

    _run(go())
