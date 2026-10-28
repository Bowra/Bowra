from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

from bowhead_core import migrations

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

EXPECTED_TABLES = {
    "team_subagent_authoring_context",
    "team_subagent_draft_request",
    "team_subagent_draft_usage",
    "team_subagent_draft",
    "team_subagent_draft_suggestion",
}

EXPECTED_INDEXES = {
    "idx_team_subagent_authoring_context_expiry",
    "idx_team_subagent_draft_request_expiry",
    "idx_team_subagent_draft_usage_lease",
    "uq_team_subagent_draft_idempotency",
    "idx_subagent_draft_suggestion_draft",
    "idx_subagent_draft_suggestion_expiry",
    "idx_team_subagent_draft_owner_status_updated",
    "idx_team_subagent_draft_expiry",
    "idx_team_subagent_draft_source_turn",
    "uq_team_subagent_source_draft",
}


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
            (
                "team_legacy",
                "Legacy Team",
                "active",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
                id, team_id, name, tool_refs_json, visibility,
                status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "osa_legacy",
                "team_legacy",
                "legacy-agent",
                '["bi_query"]',
                "team_wide",
                "enabled",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()


def _objects(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=?",
            (kind,),
        )
    }


def test_subagent_draft_revision_upgrades_all_eight_targets(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    for filename in TARGET_FILES.values():
        with sqlite3.connect(tmp_path / filename) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version",
            ).fetchone() == (HEAD_REVISION,)

    with sqlite3.connect(core_path) as connection:
        assert _objects(connection, "table") >= EXPECTED_TABLES
        assert _objects(connection, "index") >= EXPECTED_INDEXES
        assert "trg_team_subagent_attribution_immutable" in _objects(connection, "trigger")
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(team_subagent)",
            )
        }
        assert {"origin_mode", "source_draft_id"} <= columns
        assert connection.execute(
            """
            SELECT origin_mode, source_draft_id
            FROM team_subagent
            WHERE id='osa_legacy'
            """,
        ).fetchone() == ("manual", None)


def test_manual_draft_migration_preserves_existing_ai_draft(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)
    config = Config()
    config.set_main_option("script_location", str(migrations.__path__[0]))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{core_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "0117_working_copy_history_refs")
    with sqlite3.connect(core_path) as connection:
        connection.execute(
            """
            INSERT INTO team_subagent_draft(
                id, team_id, owner_user_id, source_mode, status,
                validation_status, locale, purpose_text, idempotency_key_hash,
                mutation_id, expires_at, content_scrub_at, purge_at,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "sad_legacy",
                "team_legacy",
                "user_a",
                "intent",
                "generating",
                "unknown",
                "zh-CN",
                "已有 AI 目标",
                "legacy-key",
                "00000000-0000-4000-8000-000000000000",
                "2026-10-01T00:00:00+00:00",
                "2026-10-08T00:00:00+00:00",
                "2026-10-31T00:00:00+00:00",
                "2026-09-27T00:00:00+00:00",
                "2026-09-27T00:00:00+00:00",
            ),
        )
        cursor = connection.execute("SELECT * FROM team_subagent_draft WHERE id='sad_legacy'")
        old_columns = [column[0] for column in cursor.description]
        before = cursor.fetchone()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(core_path) as connection:
        after = connection.execute(
            f"SELECT {', '.join(old_columns)} FROM team_subagent_draft WHERE id='sad_legacy'",
        ).fetchone()
        assert after == before
        assert connection.execute(
            "SELECT workspace_id FROM team_subagent_draft WHERE id='sad_legacy'",
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT create_payload_hash FROM team_subagent_draft WHERE id='sad_legacy'",
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT version_num FROM alembic_version",
        ).fetchone() == (HEAD_REVISION,)


def test_subagent_draft_constraints_and_attribution_guard(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_subagent_draft(
                    id, team_id, owner_user_id, source_mode, source_turn_id,
                    status, validation_status, locale, idempotency_key_hash,
                    mutation_id, expires_at, content_scrub_at, purge_at,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "sad_bad",
                    "team_legacy",
                    "user_a",
                    "intent",
                    "turn_should_be_null",
                    "ready",
                    "valid",
                    "zh-CN",
                    "hash",
                    "00000000-0000-4000-8000-000000000000",
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-07T00:00:00+00:00",
                    "2026-08-30T00:00:00+00:00",
                    "2026-07-31T00:00:00+00:00",
                    "2026-07-31T00:00:00+00:00",
                ),
            )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_subagent attribution is immutable",
        ):
            connection.execute(
                """
                UPDATE team_subagent
                SET origin_mode='intent_draft'
                WHERE id='osa_legacy'
                """
            )
