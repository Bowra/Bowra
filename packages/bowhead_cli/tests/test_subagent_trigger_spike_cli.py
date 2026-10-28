"""Stage 0A Subagent mixed-version trigger spike contract tests."""

from __future__ import annotations

import json
import sqlite3

import pytest
from bowhead_cli.main import app
from bowhead_cli.subagent_trigger_spike import (
    ActivationBlockedError,
    activate_release_b,
    check_application_compatibility,
    initialize_trigger_spike_fixture,
    run_subagent_trigger_spike,
)
from typer.testing import CliRunner

runner = CliRunner()


def test_release_a_triggers_exist_but_allow_legacy_writes() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        initialize_trigger_spike_fixture(connection)

        trigger_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name LIKE 'team_subagent_%'
            """,
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE team_subagent
            SET display_name = 'legacy edit'
            WHERE id = 'sa_trigger_spike'
            """,
        )
        connection.execute(
            """
            INSERT INTO team_subagent (
              id, team_id, name, created_at, updated_at
            ) VALUES (
              'sa_legacy', 'team_trigger_spike', 'legacy', 'now', 'now'
            )
            """,
        )

        assert trigger_count == 4
        assert (
            connection.execute(
                "SELECT display_name FROM team_subagent WHERE id = 'sa_trigger_spike'",
            ).fetchone()[0]
            == "legacy edit"
        )
        assert (
            connection.execute(
                "SELECT owner_user_id FROM team_subagent WHERE id = 'sa_legacy'",
            ).fetchone()[0]
            is None
        )
    finally:
        connection.close()


def test_release_b_activation_is_blocked_atomically_by_live_old_writer() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        initialize_trigger_spike_fixture(connection)
        connection.execute(
            """
            INSERT INTO team_subagent_app_compatibility (
              instance_id, reader_version, writer_version, started_at, heartbeat_at
            ) VALUES ('pre-a', '0001', '0001', '2026-07-27T11:00:00Z', '2026-07-27T12:00:00Z')
            """,
        )
        connection.commit()

        with pytest.raises(ActivationBlockedError) as captured:
            activate_release_b(connection)

        assert captured.value.incompatible_live_instances == 1
        metadata = connection.execute(
            """
            SELECT
              minimum_writer_version,
              owner_semantics_activated_at,
              lifecycle_semantics_activated_at,
              audience_semantics_activated_at
            FROM team_subagent_schema_meta
            WHERE singleton = 1
            """,
        ).fetchone()
        assert metadata == ("0001", None, None, None)
    finally:
        connection.close()


def test_release_b_activation_fails_closed_for_malformed_live_version() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        initialize_trigger_spike_fixture(connection)
        connection.execute(
            """
            INSERT INTO team_subagent_app_compatibility (
              instance_id, reader_version, writer_version, started_at, heartbeat_at
            ) VALUES (
              'malformed',
              'release-b',
              'release-b',
              '2026-07-27T11:00:00Z',
              '2026-07-27T12:00:00Z'
            )
            """,
        )
        connection.commit()

        with pytest.raises(ActivationBlockedError) as captured:
            activate_release_b(connection)

        assert captured.value.incompatible_live_instances == 1
        assert (
            connection.execute(
                """
                SELECT owner_semantics_activated_at
                FROM team_subagent_schema_meta
                WHERE singleton = 1
                """,
            ).fetchone()[0]
            is None
        )
    finally:
        connection.close()


def test_release_b_raw_sql_cannot_bypass_version_owner_or_quarantine() -> None:
    connection = _activated_connection()
    try:
        before = connection.execute(
            """
            SELECT
              owner_user_id,
              row_version,
              status,
              lifecycle_state,
              display_name,
              quarantine_reason
            FROM team_subagent
            WHERE id = 'sa_trigger_spike'
            """,
        ).fetchone()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_subagent_version_required",
        ):
            connection.execute(
                """
                UPDATE team_subagent
                SET display_name = 'old writer edit'
                WHERE id = 'sa_trigger_spike'
                """,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_subagent_version_required",
        ):
            connection.execute(
                """
                UPDATE team_subagent
                SET owner_user_id = created_by
                WHERE id = 'sa_trigger_spike'
                """,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_subagent_invariant_violation",
        ):
            connection.execute(
                """
                UPDATE team_subagent
                SET status = 'enabled'
                WHERE id = 'sa_trigger_spike'
                """,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_subagent_version_required",
        ):
            connection.execute(
                """
                UPDATE team_subagent
                SET quarantine_reason = 'legacy overwrite'
                WHERE id = 'sa_trigger_spike'
                """,
            )

        after = connection.execute(
            """
            SELECT
              owner_user_id,
              row_version,
              status,
              lifecycle_state,
              display_name,
              quarantine_reason
            FROM team_subagent
            WHERE id = 'sa_trigger_spike'
            """,
        ).fetchone()
        assert after == before
    finally:
        connection.close()


def test_release_b_allows_exact_versioned_write_and_rejects_jump() -> None:
    connection = _activated_connection()
    try:
        connection.execute(
            """
            UPDATE team_subagent
            SET description = 'compatible edit',
                row_version = row_version + 1
            WHERE id = 'sa_trigger_spike'
            """,
        )
        assert connection.execute(
            """
                SELECT description, row_version
                FROM team_subagent
                WHERE id = 'sa_trigger_spike'
                """,
        ).fetchone() == ("compatible edit", 8)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="team_subagent_version_required",
        ):
            connection.execute(
                """
                UPDATE team_subagent
                SET description = 'jump',
                    row_version = row_version + 2
                WHERE id = 'sa_trigger_spike'
                """,
            )
        assert (
            connection.execute(
                "SELECT row_version FROM team_subagent WHERE id = 'sa_trigger_spike'",
            ).fetchone()[0]
            == 8
        )
    finally:
        connection.close()


def test_release_b_rollback_keeps_minimum_version_and_triggers_active() -> None:
    connection = _activated_connection()
    try:
        old_candidate = check_application_compatibility(
            connection,
            reader_version="0001",
            writer_version="0001",
        )
        compatible_candidate = check_application_compatibility(
            connection,
            reader_version="0002",
            writer_version="0002",
        )
        with pytest.raises(
            ValueError,
            match="team_subagent_minimum_version_regression",
        ):
            activate_release_b(
                connection,
                minimum_reader_version="0001",
                minimum_writer_version="0001",
            )

        assert old_candidate.compatible is False
        assert old_candidate.minimum_writer_version == "0002"
        assert compatible_candidate.compatible is True
        assert (
            connection.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name LIKE 'team_subagent_%'
                """,
            ).fetchone()[0]
            == 4
        )
    finally:
        connection.close()


def test_spike_report_is_deterministic_and_all_contracts_pass() -> None:
    first = run_subagent_trigger_spike()
    second = run_subagent_trigger_spike()

    assert first.to_dict() == second.to_dict()
    assert first.scope == "synthetic_isolated_trigger_compatibility_spike"
    assert all(first.checks.values())
    assert first.activation["live_old_writer_attempt"] == {
        "accepted": False,
        "error_code": "team_subagent_incompatible_live_writer",
        "incompatible_live_instances": 1,
    }
    assert first.rollback_fence["policy"] == ("rollback_only_to_compatible_release; never deactivate guards or lower minimum writer")


def test_cli_json_emits_safe_structured_trigger_evidence() -> None:
    result = runner.invoke(
        app,
        ["admin", "spike-subagent-triggers", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "synthetic_isolated_trigger_compatibility_spike"
    assert all(payload["checks"].values())
    assert payload["methodology"]["production_activation"] == "none"
    assert "trigger-spike.db" not in result.output
    assert "Synthetic prompt" not in result.output


def test_compatibility_version_must_be_fixed_width_and_sortable() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        initialize_trigger_spike_fixture(connection)

        with pytest.raises(
            ValueError,
            match="four-digit sortable",
        ):
            check_application_compatibility(
                connection,
                reader_version="release-b",
                writer_version="2",
            )
    finally:
        connection.close()


def _activated_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    initialize_trigger_spike_fixture(connection)
    activate_release_b(connection)
    return connection
