from __future__ import annotations

import sqlite3

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

EXPECTED_TABLES = {
    "team_llm_provider_identities",
    "team_llm_verification_plans",
    "team_llm_verification_batches",
    "team_llm_verification_items",
    "team_llm_capability_evidence",
    "team_llm_risk_acceptances",
    "team_llm_risk_acceptance_retirements",
    "team_llm_v2_enrollments",
    "team_llm_v2_migration_baselines",
    "team_llm_v2_migration_baseline_invalidations",
    "team_llm_apply_snapshots",
}

IMMUTABLE_TABLES = {
    "team_llm_capability_evidence",
    "team_llm_risk_acceptances",
    "team_llm_risk_acceptance_retirements",
    "team_llm_v2_enrollments",
    "team_llm_v2_migration_baselines",
    "team_llm_v2_migration_baseline_invalidations",
    "team_llm_apply_snapshots",
}


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
            ("team_legacy", "Legacy Team", "active", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()


def _schema_objects(
    connection: sqlite3.Connection,
    object_type: str,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ?",
            (object_type,),
        ).fetchall()
    }


def test_team_llm_verification_upgrades_1_0_core_schema(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    assert upgrade(db_url, target="core") == ("core",)
    assert upgrade(db_url, target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert _schema_objects(connection, "table") >= EXPECTED_TABLES
        indexes = _schema_objects(connection, "index")
        assert {
            "uq_team_llm_provider_active_key",
            "idx_llm_evidence_lookup",
            "idx_llm_evidence_provider_history",
            "idx_llm_items_team_slots",
            "idx_llm_items_batch_revision",
            "idx_llm_risk_acceptance_scope",
        } <= indexes
        triggers = _schema_objects(connection, "trigger")
        assert {f"trg_{table}_immutable" for table in IMMUTABLE_TABLES} <= triggers
        assert connection.execute(
            "SELECT name FROM teams WHERE id = 'team_legacy'",
        ).fetchone() == ("Legacy Team",)


def test_team_llm_verification_constraints_and_immutability(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO team_llm_provider_identities(
                team_id, provider_id, runtime_key, state,
                creation_source, created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                "team_legacy",
                "llmp_active",
                "main",
                "active",
                "legacy_migration",
                "2026-07-30T00:00:00+00:00",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_llm_provider_identities(
                    team_id, provider_id, runtime_key, state,
                    creation_source, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    "team_legacy",
                    "llmp_duplicate",
                    "main",
                    "reserved",
                    "user",
                    "2026-07-30T00:00:01+00:00",
                ),
            )

        connection.execute(
            """
            INSERT INTO team_llm_v2_enrollments(
                team_id, enrolled_config_version, enrolled_semantic_hash,
                assignments_json, rollout_generation, enrollment_source,
                enrolled_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                "team_legacy",
                1,
                "semantic-hash",
                "[]",
                "model-discovery-v2-r1",
                "first_v2_read",
                "2026-07-30T00:00:00+00:00",
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_llm_v2_enrollments is immutable",
        ):
            connection.execute(
                """
                UPDATE team_llm_v2_enrollments
                SET enrolled_config_version = 2
                WHERE team_id = 'team_legacy'
                """
            )
