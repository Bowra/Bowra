from __future__ import annotations

import sqlite3

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()


def _seed_scope_rows(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO teams(id,name,created_at) VALUES(?,?,?)",
        (
            ("team_branding_one", "One", "2026-09-05T00:00:00+00:00"),
            ("team_branding_two", "Two", "2026-09-05T00:00:00+00:00"),
        ),
    )
    connection.execute(
        "INSERT INTO users(id,team_id,email,created_at) VALUES(?,?,?,?)",
        (
            "usr_branding_admin",
            "team_branding_one",
            "branding@example.test",
            "2026-09-05T00:00:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO team_branding_revisions(
          id,team_id,revision_number,parent_revision_id,operation,configuration_type,
          schema_version,source_json,compiled_json,defaults_json,behavior,
          application_name,logo_alt,logo_light_artifact_id,logo_dark_artifact_id,
          mark_artifact_id,favicon_artifact_id,generator_id,generator_version,
          validation_json,checksum,release_note,published_by,published_at
        ) VALUES(
          'tbr_one','team_branding_one',1,NULL,'legacy_baseline','bowhead',2,
          '{}','{}','{}','default','Bowhead',NULL,NULL,NULL,NULL,NULL,
          'bowhead','1','{}','checksum',NULL,'usr_branding_admin',
          '2026-09-05T00:00:00+00:00'
        )
        """,
    )


def _copy_values(*, copy_id: str, team_id: str) -> tuple[object, ...]:
    return (
        copy_id,
        team_id,
        1,
        "tbr_one",
        2,
        "{}",
        "{}",
        "valid",
        "{}",
        "default",
        "Bowhead",
        None,
        None,
        None,
        None,
        None,
        None,
        "bowhead",
        "1",
        "{}",
        "checksum",
        "usr_branding_admin",
        "2026-09-05T00:00:00+00:00",
        "usr_branding_admin",
        "2026-09-05T00:00:01+00:00",
    )


_INSERT_COPY = """
INSERT INTO team_branding_draft_copies(
  id,team_id,source_draft_version,original_base_revision_id,schema_version,
  source_json,compiled_json,compile_status,defaults_json,behavior,application_name,
  logo_alt,logo_light_artifact_id,logo_dark_artifact_id,mark_artifact_id,
  favicon_artifact_id,release_note,generator_id,generator_version,validation_json,
  checksum,source_updated_by,source_updated_at,preserved_by,preserved_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def test_team_branding_draft_copy_migration_is_strict_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)

    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (chain_head(),)
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'",
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'",
            )
        }
        assert "idx_team_branding_draft_copies_team" in indexes
        assert {
            "trg_team_branding_draft_copy_scope",
            "trg_team_branding_draft_copy_no_update",
        } <= triggers

        _seed_scope_rows(connection)
        connection.execute(
            _INSERT_COPY,
            _copy_values(copy_id="tbc_valid", team_id="team_branding_one"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE team_branding_draft_copies SET application_name='Changed' WHERE id='tbc_valid'",
            )
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            connection.execute(
                _INSERT_COPY,
                _copy_values(copy_id="tbc_cross_team", team_id="team_branding_two"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            values = list(_copy_values(copy_id="tbc_invalid_state", team_id="team_branding_one"))
            values[7] = "invalid"
            connection.execute(_INSERT_COPY, values)
