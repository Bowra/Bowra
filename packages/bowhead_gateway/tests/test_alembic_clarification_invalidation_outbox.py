"""Migration gate for the durable clarification invalidation outbox."""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()


def _create_baseline(path, target: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema(target))
        connection.commit()


def _table_sql(connection: sqlite3.Connection, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return None if row is None else str(row[0])


def test_core_upgrade_adds_outbox_indexes_and_checks(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_baseline(db_path, "core")

    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        sql = _table_sql(
            connection,
            "clarification_invalidation_outbox",
        )
        assert sql is not None
        assert "target_status IN ('dismissed','expired')" in sql
        assert "state IN ('pending','delivered')" in sql
        indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(clarification_invalidation_outbox)",
            ).fetchall()
        }
        assert {
            "idx_clarification_invalidation_outbox_pending",
            "idx_clarification_invalidation_outbox_turn_claim",
        } <= indexes

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO clarification_invalidation_outbox(
                  request_id,turn_id,event_id,target_status,reason_code,
                  actor_kind,state,attempt_count,created_at,updated_at
                ) VALUES ('c1','t1','e1','resolved','bad','system',
                          'pending',0,'now','now')
                """,
            )


@pytest.mark.parametrize("target", sorted(set(TARGET_FILES) - {"core"}))
def test_outbox_revision_is_core_only(tmp_path, target: str) -> None:
    target_dir = tmp_path / target
    target_dir.mkdir()
    db_path = target_dir / TARGET_FILES[target]
    _create_baseline(db_path, target)

    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target=target) == (target,)

    with sqlite3.connect(db_path) as connection:
        assert (
            _table_sql(
                connection,
                "clarification_invalidation_outbox",
            )
            is None
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
