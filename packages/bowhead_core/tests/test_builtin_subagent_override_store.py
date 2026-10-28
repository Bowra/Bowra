"""内置子代理 team override store 测试（Phase A）.

覆盖：NULL vs '[]' 的「不覆盖 / 覆盖为空」语义、row_version CAS、audit 与业务写同事务、
跨团队隔离、恢复默认（删行）幂等。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from bowhead_core.audit_actions import AuditAction
from bowhead_core.builtin_subagent_override_store import (
    BuiltinSubagentOverride,
    BuiltinSubagentOverrideStore,
    BuiltinSubagentOverrideVersionConflictError,
)
from bowhead_core.migrations.baseline_schema import initial_schema

pytestmark = pytest.mark.anyio

_UPDATED = AuditAction.BUILTIN_SUBAGENT_OVERRIDE_UPDATED
_CLEARED = AuditAction.BUILTIN_SUBAGENT_OVERRIDE_CLEARED


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db_path(tmp_path) -> str:
    """1.0.0 基线 + 0009 建的表。直接建表（不跑 alembic）保持单测轻量；

    迁移本身由 test_alembic_builtin_subagent_override.py 独立门禁。
    """
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(initial_schema("core"))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS team_builtin_subagent_override (
              team_id         TEXT NOT NULL,
              name            TEXT NOT NULL,
              enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
              display_name    TEXT,
              description     TEXT,
              model           TEXT,
              skills_json     TEXT,
              prompt_append   TEXT,
              max_iterations  INTEGER CHECK (max_iterations IS NULL OR max_iterations >= 1),
              primary_capability TEXT,
              row_version     INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
              updated_by      TEXT,
              created_at      TEXT NOT NULL,
              updated_at      TEXT NOT NULL,
              PRIMARY KEY (team_id, name)
            )
            """,
        )
        conn.commit()
    return str(path)


def _store(db_path: str) -> BuiltinSubagentOverrideStore:
    return BuiltinSubagentOverrideStore(
        db_path,
        clock=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )


async def test_upsert_then_read_roundtrip(db_path: str) -> None:
    store = _store(db_path)
    saved = await store.upsert(
        BuiltinSubagentOverride(
            team_id="team_1",
            name="consultant",
            enabled=False,
            description="团队口径：只接尽调 deck",
            model="narrative",
            skills=["compliance-sop"],
            prompt_append="所有金额以人民币计。",
            max_iterations=20,
            primary_capability="cap_presentation",
        ),
        expected_version=None,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    assert saved.row_version == 1
    got = await store.get("team_1", "consultant")
    assert got is not None
    assert got.enabled is False
    assert got.description == "团队口径：只接尽调 deck"
    assert got.model == "narrative"
    assert got.skills == ["compliance-sop"]
    assert got.prompt_append == "所有金额以人民币计。"
    assert got.max_iterations == 20
    assert got.primary_capability == "cap_presentation"
    assert got.updated_by == "u_admin"


async def test_first_write_starts_at_version_one(db_path: str) -> None:
    store = _store(db_path)
    saved = await store.upsert(
        BuiltinSubagentOverride(team_id="team_1", name="compute", description="x"),
        expected_version=None,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    assert saved.row_version == 1
    assert (await store.get("team_1", "compute")).row_version == 1


async def test_none_skills_means_not_overridden_empty_list_means_cleared(db_path: str) -> None:
    store = _store(db_path)
    await store.upsert(
        BuiltinSubagentOverride(team_id="team_1", name="kb-analyst", skills=None),
        expected_version=None,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    assert (await store.get("team_1", "kb-analyst")).skills is None

    await store.upsert(
        BuiltinSubagentOverride(team_id="team_1", name="kb-analyst", skills=[]),
        expected_version=1,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    # '[]' ≠ NULL: explicit empty override; reads back as an empty list, not None.
    assert (await store.get("team_1", "kb-analyst")).skills == []


async def test_version_conflict_on_stale_expected_version(db_path: str) -> None:
    store = _store(db_path)
    await store.upsert(
        BuiltinSubagentOverride(team_id="team_1", name="consultant", description="v1"),
        expected_version=None,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    with pytest.raises(BuiltinSubagentOverrideVersionConflictError) as exc:
        await store.upsert(
            BuiltinSubagentOverride(team_id="team_1", name="consultant", description="v2"),
            expected_version=None,  # Assumes first write, but a row already exists
            actor_user_id="u_admin",
            audit_action=_UPDATED,
        )
    assert exc.value.current_version == 1
    # Conflict must leave no partial writes.
    assert (await store.get("team_1", "consultant")).description == "v1"


async def test_upsert_writes_audit_in_same_transaction(db_path: str) -> None:
    store = _store(db_path)
    await store.upsert(
        BuiltinSubagentOverride(team_id="team_1", name="consultant", enabled=False),
        expected_version=None,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT action, target_kind, target_id, team_id FROM admin_audit_log",
        ).fetchall()
    assert rows == [(_UPDATED, "builtin_subagent_override", "consultant", "team_1")]


async def test_delete_restores_default_and_is_idempotent(db_path: str) -> None:
    store = _store(db_path)
    await store.upsert(
        BuiltinSubagentOverride(team_id="team_1", name="consultant", enabled=False),
        expected_version=None,
        actor_user_id="u_admin",
        audit_action=_UPDATED,
    )
    assert await store.delete("team_1", "consultant", actor_user_id="u_admin", audit_action=_CLEARED) is True
    assert await store.get("team_1", "consultant") is None
    # Second delete: no row → False; do not write another audit.
    assert await store.delete("team_1", "consultant", actor_user_id="u_admin", audit_action=_CLEARED) is False
    with sqlite3.connect(db_path) as conn:
        cleared = conn.execute(
            "SELECT COUNT(*) FROM admin_audit_log WHERE action=?",
            (_CLEARED,),
        ).fetchone()[0]
    assert cleared == 1


async def test_cross_team_isolation(db_path: str) -> None:
    store = _store(db_path)
    await store.upsert(
        BuiltinSubagentOverride(team_id="team_a", name="consultant", enabled=False),
        expected_version=None,
        actor_user_id="u_a",
        audit_action=_UPDATED,
    )
    assert await store.get("team_b", "consultant") is None
    assert await store.list_for_team("team_b") == {}
    assert set(await store.list_for_team("team_a")) == {"consultant"}


async def test_list_for_team_indexes_by_name(db_path: str) -> None:
    store = _store(db_path)
    for name in ("consultant", "compute"):
        await store.upsert(
            BuiltinSubagentOverride(team_id="team_1", name=name, enabled=False),
            expected_version=None,
            actor_user_id="u_admin",
            audit_action=_UPDATED,
        )
    got = await store.list_for_team("team_1")
    assert set(got) == {"consultant", "compute"}
    assert all(not ov.enabled for ov in got.values())


async def test_empty_team_id_reads_are_inert(db_path: str) -> None:
    store = _store(db_path)
    assert await store.list_for_team("") == {}
    assert await store.get("", "consultant") is None


def test_overridden_fields_and_is_default() -> None:
    assert BuiltinSubagentOverride(team_id="t", name="n").is_default is True
    assert BuiltinSubagentOverride(team_id="t", name="n").overridden_fields == []
    ov = BuiltinSubagentOverride(team_id="t", name="n", enabled=False, model="narrative")
    assert ov.overridden_fields == ["enabled", "model"]
    assert ov.is_default is False
    # skills=[] is an explicit override and counts as a change (unlike None).
    assert BuiltinSubagentOverride(team_id="t", name="n", skills=[]).overridden_fields == ["skills"]
