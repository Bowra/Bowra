"""`0126_access_request_lifecycle`: access-request lifecycle columns (dual-owned table)."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.access_request_store import AccessRequestStore
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0126_access_request_lifecycle")
pytestmark = pytest.mark.integration

_NEW_COLUMNS = [
    "decision_note",
    "expires_at",
    "requested_role",
    "granted_permission",
    "withdrawn_at",
    "target",
    "shadow",
    "grant_expires_at",
    "cooldown_until",
    "escalated_at",
    "release_candidate_id",
]


def _columns(path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute("PRAGMA table_info(access_requests)")]


def test_upgrade_adds_lifecycle_columns_keeps_rows_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO access_requests(id,team_id,kind,resource_id,requester_id,owner_id,message,status,created_at,updated_at) "
            "VALUES ('acr_old','team_1','report','ses_1','u_bob','u_alice','hi','denied','2026-09-01T00:00:00+00:00','2026-09-02T00:00:00+00:00')"
        )
    _upgrade(path, MIGRATION.revision)
    assert _columns(path)[-len(_NEW_COLUMNS) :] == _NEW_COLUMNS
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM access_requests WHERE id='acr_old'").fetchone())
        indexes = {r[1] for r in db.execute("PRAGMA index_list(access_requests)")}
    assert row["status"] == "denied" and row["message"] == "hi"
    assert row["shadow"] == 0 and all(row[c] is None for c in _NEW_COLUMNS if c != "shadow")
    assert "idx_acr_pending_expiry" in indexes

    before_schema, before_rows = schema(path), snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE alembic_version SET version_num=?", (MIGRATION.down_revision,))
    _upgrade(path, MIGRATION.revision)
    assert schema(path) == before_schema and snapshot(path) == before_rows


@pytest.mark.parametrize("target", [t for t in TARGET_FILES if t != "core"])
def test_other_targets_are_unchanged(tmp_path, target) -> None:
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == before


async def test_store_initialize_and_migration_agree_and_store_reads_migrated_rows(tmp_path) -> None:
    migrated = tmp_path / "core.db"
    _upgrade(migrated, "head")
    before = schema(migrated)
    store = AccessRequestStore(str(migrated))
    await store.initialize()  # dual ownership: the Store guard is a no-op at head
    assert schema(migrated) == before

    fresh = tmp_path / "fresh.db"
    await AccessRequestStore(str(fresh)).initialize()
    assert _columns(fresh) == _columns(migrated)

    row, created = await store.enqueue_once(
        team_id="team_1",
        kind="team_skill",
        resource_id="sk_1",
        requester_id="u_bob",
        owner_id=None,
        message=None,
        shadow=True,
        expires_at="2026-10-14T00:00:00+00:00",
    )
    assert created
    reread = await store.get(row.id)
    assert reread.shadow is True and reread.expires_at == "2026-10-14T00:00:00+00:00"


LEGACY_ORIGIN = import_module("bowhead_core.migrations.versions.0127_access_request_legacy_origin")


def test_legacy_origin_marks_only_legacy_intake_rows_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, LEGACY_ORIGIN.down_revision)
    rows = (
        ("acr_legacy", None, 0),  # legacy intake: no expiry, real
        ("acr_flow", "2026-10-14T00:00:00+00:00", 0),  # expansion real row
        ("acr_shadow", "2026-10-14T00:00:00+00:00", 1),  # expansion placeholder
    )
    with sqlite3.connect(path) as db:
        for request_id, expires_at, shadow in rows:
            db.execute(
                "INSERT INTO access_requests(id,team_id,kind,resource_id,requester_id,status,created_at,updated_at,expires_at,shadow) "
                "VALUES (?,'team_1','report','ses_1',?,'pending','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00',?,?)",
                (request_id, request_id, expires_at, shadow),
            )
    _upgrade(path, LEGACY_ORIGIN.revision)

    def marks():
        with sqlite3.connect(path) as db:
            return dict(db.execute("SELECT id, legacy_origin FROM access_requests").fetchall())

    assert marks() == {"acr_legacy": 1, "acr_flow": 0, "acr_shadow": 0}
    before = schema(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE alembic_version SET version_num=?", (LEGACY_ORIGIN.down_revision,))
    _upgrade(path, LEGACY_ORIGIN.revision)
    assert schema(path) == before and marks() == {"acr_legacy": 1, "acr_flow": 0, "acr_shadow": 0}


async def test_store_marks_new_legacy_intake_rows(tmp_path) -> None:
    store = AccessRequestStore(str(tmp_path / "fresh.db"))
    await store.initialize()
    legacy, _ = await store.enqueue_once(team_id="t", kind="report", resource_id="s1", requester_id="u", owner_id="o", message=None)
    flow, _ = await store.enqueue_once(
        team_id="t",
        kind="report",
        resource_id="s2",
        requester_id="u",
        owner_id=None,
        message=None,
        shadow=True,
        expires_at="2026-10-14T00:00:00+00:00",
    )
    assert (await store.get(legacy.id)).legacy_origin is True
    assert (await store.get(flow.id)).legacy_origin is False
