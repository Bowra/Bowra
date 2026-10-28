"""Isolated Stage 0A spike for Subagent mixed-version SQLite guards."""

from __future__ import annotations

import platform
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_NOW = "2026-07-27T12:00:00Z"
_STALE_HEARTBEAT = "2026-07-27T11:58:29Z"
_TEAM_ID = "team_trigger_spike"
_SUBAGENT_ID = "sa_trigger_spike"
_RELEASE_A_VERSION = "0001"
_RELEASE_B_VERSION = "0002"


@dataclass(frozen=True)
class CompatibilityDecision:
    compatible: bool
    reader_compatible: bool
    writer_compatible: bool
    minimum_reader_version: str
    minimum_writer_version: str


@dataclass(frozen=True)
class SubagentTriggerSpikeReport:
    schema_version: int
    scope: str
    methodology: dict[str, str]
    environment: dict[str, str]
    release_a_scenarios: list[dict[str, Any]]
    activation: dict[str, Any]
    release_b_scenarios: list[dict[str, Any]]
    rollback_fence: dict[str, Any]
    checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActivationBlockedError(RuntimeError):
    """Release B cannot activate while an incompatible live process exists."""

    def __init__(self, *, incompatible_live_instances: int) -> None:
        self.code = "team_subagent_incompatible_live_writer"
        self.incompatible_live_instances = incompatible_live_instances
        super().__init__(self.code)


def initialize_trigger_spike_fixture(connection: sqlite3.Connection) -> None:
    """Create the planned additive schema and dormant Release A triggers."""
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE team_subagent (
          id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          name TEXT NOT NULL,
          display_name TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          system_prompt TEXT NOT NULL DEFAULT '',
          tool_refs_json TEXT NOT NULL DEFAULT '[]',
          model TEXT,
          max_iterations INTEGER NOT NULL DEFAULT 10,
          skills_json TEXT NOT NULL DEFAULT '[]',
          output_schema_json TEXT,
          visibility TEXT NOT NULL DEFAULT 'creator_only',
          created_by TEXT,
          status TEXT NOT NULL DEFAULT 'enabled',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          owner_user_id TEXT,
          row_version INTEGER NOT NULL DEFAULT 1,
          audience_mode TEXT NOT NULL DEFAULT 'private',
          lifecycle_state TEXT NOT NULL DEFAULT 'active',
          quarantine_started_at TEXT,
          quarantine_expires_at TEXT,
          quarantine_reason TEXT,
          quarantined_owner_user_id TEXT
        );

        CREATE TABLE team_subagent_schema_meta (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          schema_version INTEGER NOT NULL,
          minimum_reader_version TEXT NOT NULL,
          minimum_writer_version TEXT NOT NULL,
          owner_semantics_activated_at TEXT,
          lifecycle_semantics_activated_at TEXT,
          audience_semantics_activated_at TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE team_subagent_app_compatibility (
          instance_id TEXT PRIMARY KEY,
          reader_version TEXT NOT NULL,
          writer_version TEXT NOT NULL,
          started_at TEXT NOT NULL,
          heartbeat_at TEXT NOT NULL
        );

        INSERT INTO team_subagent_schema_meta (
          singleton,
          schema_version,
          minimum_reader_version,
          minimum_writer_version,
          owner_semantics_activated_at,
          lifecycle_semantics_activated_at,
          audience_semantics_activated_at,
          updated_at
        ) VALUES (1, 2, '0001', '0001', NULL, NULL, NULL, '2026-07-27T11:55:00Z');

        INSERT INTO team_subagent (
          id,
          team_id,
          name,
          display_name,
          description,
          system_prompt,
          tool_refs_json,
          model,
          max_iterations,
          skills_json,
          output_schema_json,
          visibility,
          created_by,
          status,
          created_at,
          updated_at,
          owner_user_id,
          row_version,
          audience_mode,
          lifecycle_state,
          quarantine_started_at,
          quarantine_expires_at,
          quarantine_reason,
          quarantined_owner_user_id
        ) VALUES (
          'sa_trigger_spike',
          'team_trigger_spike',
          'finance-reviewer',
          'Finance reviewer',
          'Reviews finance data',
          'Synthetic prompt',
          '["native.sql"]',
          NULL,
          10,
          '[]',
          NULL,
          'creator_only',
          'user_creator',
          'disabled',
          '2026-07-27T11:00:00Z',
          '2026-07-27T11:30:00Z',
          'user_owner',
          7,
          'private',
          'quarantined',
          '2026-07-27T11:30:00Z',
          '2026-08-27T11:30:00Z',
          'owner_inactive',
          'user_owner'
        );

        INSERT INTO team_subagent (
          id,
          team_id,
          name,
          created_by,
          status,
          created_at,
          updated_at,
          owner_user_id,
          row_version,
          audience_mode,
          lifecycle_state
        ) VALUES (
          'sa_orphaned_disabled',
          'team_trigger_spike',
          'orphaned-disabled',
          NULL,
          'disabled',
          '2026-07-27T11:00:00Z',
          '2026-07-27T11:30:00Z',
          NULL,
          1,
          'private',
          'active'
        );

        CREATE TRIGGER team_subagent_invariant_guard_update
        BEFORE UPDATE ON team_subagent
        WHEN (
          (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND audience_semantics_activated_at IS NOT NULL
            )
            AND NEW.audience_mode NOT IN ('private', 'custom', 'teamwide')
          )
          OR (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND lifecycle_semantics_activated_at IS NOT NULL
            )
            AND (
              NEW.lifecycle_state NOT IN ('active', 'quarantined')
              OR (
                NEW.lifecycle_state = 'quarantined'
                AND NEW.status <> 'disabled'
              )
              OR NEW.row_version < OLD.row_version
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_invariant_violation');
        END;

        CREATE TRIGGER team_subagent_version_guard_update
        BEFORE UPDATE ON team_subagent
        WHEN EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND owner_semantics_activated_at IS NOT NULL
        )
        AND (
          NEW.owner_user_id IS NOT OLD.owner_user_id
          OR NEW.status IS NOT OLD.status
          OR NEW.audience_mode IS NOT OLD.audience_mode
          OR NEW.visibility IS NOT OLD.visibility
          OR NEW.lifecycle_state IS NOT OLD.lifecycle_state
          OR NEW.quarantine_started_at IS NOT OLD.quarantine_started_at
          OR NEW.quarantine_expires_at IS NOT OLD.quarantine_expires_at
          OR NEW.quarantine_reason IS NOT OLD.quarantine_reason
          OR NEW.quarantined_owner_user_id IS NOT OLD.quarantined_owner_user_id
          OR NEW.name IS NOT OLD.name
          OR NEW.display_name IS NOT OLD.display_name
          OR NEW.description IS NOT OLD.description
          OR NEW.system_prompt IS NOT OLD.system_prompt
          OR NEW.tool_refs_json IS NOT OLD.tool_refs_json
          OR NEW.model IS NOT OLD.model
          OR NEW.max_iterations IS NOT OLD.max_iterations
          OR NEW.skills_json IS NOT OLD.skills_json
          OR NEW.output_schema_json IS NOT OLD.output_schema_json
        )
        AND NOT (
          (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND audience_semantics_activated_at IS NOT NULL
            )
            AND NEW.audience_mode NOT IN ('private', 'custom', 'teamwide')
          )
          OR (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND lifecycle_semantics_activated_at IS NOT NULL
            )
            AND (
              NEW.lifecycle_state NOT IN ('active', 'quarantined')
              OR (
                NEW.lifecycle_state = 'quarantined'
                AND NEW.status <> 'disabled'
              )
              OR NEW.row_version < OLD.row_version
            )
          )
        )
        AND NEW.row_version <> OLD.row_version + 1
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_version_required');
        END;

        CREATE TRIGGER team_subagent_creator_immutable_update
        BEFORE UPDATE ON team_subagent
        WHEN EXISTS (
          SELECT 1
          FROM team_subagent_schema_meta
          WHERE singleton = 1
            AND owner_semantics_activated_at IS NOT NULL
        )
        AND NEW.created_by IS NOT OLD.created_by
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_created_by_immutable');
        END;

        CREATE TRIGGER team_subagent_invariant_guard_insert
        BEFORE INSERT ON team_subagent
        WHEN (
          (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND owner_semantics_activated_at IS NOT NULL
            )
            AND NEW.owner_user_id IS NULL
          )
          OR (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND audience_semantics_activated_at IS NOT NULL
            )
            AND NEW.audience_mode NOT IN ('private', 'custom', 'teamwide')
          )
          OR (
            EXISTS (
              SELECT 1
              FROM team_subagent_schema_meta
              WHERE singleton = 1
                AND lifecycle_semantics_activated_at IS NOT NULL
            )
            AND (
              NEW.lifecycle_state NOT IN ('active', 'quarantined')
              OR (
                NEW.lifecycle_state = 'quarantined'
                AND NEW.status <> 'disabled'
              )
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_invariant_violation');
        END;
        """
    )


def activate_release_b(
    connection: sqlite3.Connection,
    *,
    activated_at: str = _NOW,
    minimum_reader_version: str = _RELEASE_B_VERSION,
    minimum_writer_version: str = _RELEASE_B_VERSION,
    live_window_seconds: int = 90,
) -> None:
    """Atomically validate compatibility and activate all Release B semantics."""
    _validate_compatibility_version(minimum_reader_version)
    _validate_compatibility_version(minimum_writer_version)
    cutoff = _format_timestamp(
        _parse_timestamp(activated_at) - timedelta(seconds=live_window_seconds),
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        current_metadata = _metadata_snapshot(connection)
        if minimum_reader_version < current_metadata["minimum_reader_version"] or minimum_writer_version < current_metadata["minimum_writer_version"]:
            raise ValueError("team_subagent_minimum_version_regression")
        incompatible_live_instances = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM team_subagent_app_compatibility
                WHERE heartbeat_at >= ?
                  AND (
                    reader_version NOT GLOB '[0-9][0-9][0-9][0-9]'
                    OR writer_version NOT GLOB '[0-9][0-9][0-9][0-9]'
                    OR reader_version < ?
                    OR writer_version < ?
                  )
                """,
                (
                    cutoff,
                    minimum_reader_version,
                    minimum_writer_version,
                ),
            ).fetchone()[0]
        )
        if incompatible_live_instances:
            raise ActivationBlockedError(
                incompatible_live_instances=incompatible_live_instances,
            )
        invalid_rows = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM team_subagent
                WHERE (
                       owner_user_id IS NULL
                       AND status <> 'disabled'
                      )
                   OR audience_mode NOT IN ('private', 'custom', 'teamwide')
                   OR lifecycle_state NOT IN ('active', 'quarantined')
                   OR (
                     lifecycle_state = 'quarantined'
                     AND status <> 'disabled'
                   )
                """,
            ).fetchone()[0]
        )
        if invalid_rows:
            raise ValueError("team_subagent_release_b_invariant_violation")
        connection.execute(
            """
            UPDATE team_subagent_schema_meta
            SET minimum_reader_version = ?,
                minimum_writer_version = ?,
                owner_semantics_activated_at = COALESCE(
                  owner_semantics_activated_at,
                  ?
                ),
                lifecycle_semantics_activated_at = COALESCE(
                  lifecycle_semantics_activated_at,
                  ?
                ),
                audience_semantics_activated_at = COALESCE(
                  audience_semantics_activated_at,
                  ?
                ),
                updated_at = ?
            WHERE singleton = 1
            """,
            (
                minimum_reader_version,
                minimum_writer_version,
                activated_at,
                activated_at,
                activated_at,
                activated_at,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def check_application_compatibility(
    connection: sqlite3.Connection,
    *,
    reader_version: str,
    writer_version: str,
) -> CompatibilityDecision:
    """Evaluate the startup/readiness fence without registering a heartbeat."""
    _validate_compatibility_version(reader_version)
    _validate_compatibility_version(writer_version)
    row = connection.execute(
        """
        SELECT minimum_reader_version, minimum_writer_version
        FROM team_subagent_schema_meta
        WHERE singleton = 1
        """,
    ).fetchone()
    if row is None:
        raise ValueError("team_subagent_schema_meta_missing")
    minimum_reader_version = str(row[0])
    minimum_writer_version = str(row[1])
    reader_compatible = reader_version >= minimum_reader_version
    writer_compatible = writer_version >= minimum_writer_version
    return CompatibilityDecision(
        compatible=reader_compatible and writer_compatible,
        reader_compatible=reader_compatible,
        writer_compatible=writer_compatible,
        minimum_reader_version=minimum_reader_version,
        minimum_writer_version=minimum_writer_version,
    )


def run_subagent_trigger_spike() -> SubagentTriggerSpikeReport:
    """Execute the deterministic Release A/B and rollback-fence matrix."""
    with tempfile.TemporaryDirectory(prefix="bowhead-subagent-trigger-spike-") as temp_dir:
        db_path = Path(temp_dir) / "trigger-spike.db"
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            initialize_trigger_spike_fixture(connection)
            release_a_scenarios = _run_release_a_scenarios(connection)
            activation = _run_activation_scenarios(connection)
            release_b_scenarios = _run_release_b_scenarios(connection)
            rollback_fence = _run_rollback_fence(connection)
        finally:
            connection.close()

    release_a_by_name = {item["name"]: item for item in release_a_scenarios}
    release_b_by_name = {item["name"]: item for item in release_b_scenarios}
    checks = {
        "release_a_triggers_are_dormant": all(
            release_a_by_name[name]["passed"]
            for name in (
                "legacy_unversioned_definition_update",
                "legacy_insert_without_owner",
            )
        ),
        "live_old_writer_blocks_activation": (
            not activation["live_old_writer_attempt"]["accepted"]
            and activation["live_old_writer_attempt"]["error_code"] == "team_subagent_incompatible_live_writer"
        ),
        "blocked_activation_is_atomic": activation["blocked_attempt_left_metadata_unchanged"],
        "malformed_live_version_fails_closed": (
            not activation["malformed_version_attempt"]["accepted"]
            and activation["malformed_version_attempt"]["error_code"] == "team_subagent_incompatible_live_writer"
            and activation["malformed_attempt_left_metadata_unchanged"]
        ),
        "stale_old_writer_does_not_block_activation": activation["drained_attempt"]["accepted"],
        "release_b_metadata_activated_atomically": activation["metadata_activated_atomically"],
        "disabled_orphan_does_not_gain_a_fake_owner": activation["disabled_orphan_preserved"],
        "old_writer_raw_sql_is_blocked": all(
            release_b_by_name[name]["passed"]
            for name in (
                "old_writer_definition_without_version",
                "old_writer_owner_rollback_without_version",
                "old_writer_quarantine_metadata_without_version",
                "old_writer_reenables_quarantine",
            )
        ),
        "protected_writes_require_exact_version": all(
            release_b_by_name[name]["passed"]
            for name in (
                "old_writer_definition_without_version",
                "old_writer_quarantine_metadata_without_version",
                "version_jump_is_rejected",
                "version_regression_is_rejected",
            )
        ),
        "insert_and_lifecycle_invariants_are_enforced": all(
            release_b_by_name[name]["passed"]
            for name in (
                "old_writer_reenables_quarantine",
                "invalid_audience_is_rejected",
                "invalid_lifecycle_is_rejected",
                "insert_without_owner_is_rejected",
            )
        ),
        "created_by_is_immutable": release_b_by_name["creator_rewrite_is_rejected"]["passed"],
        "legal_new_writer_mutations_are_allowed": all(
            release_b_by_name[name]["passed"]
            for name in (
                "new_writer_exact_version_update",
                "no_op_update_without_version",
                "valid_insert_with_owner",
            )
        ),
        "rollback_floor_is_not_lowered": (
            not rollback_fence["pre_release_b_candidate"]["compatible"]
            and rollback_fence["release_b_candidate"]["compatible"]
            and rollback_fence["minimum_writer_version"] == _RELEASE_B_VERSION
            and not rollback_fence["lowering_attempt"]["accepted"]
            and rollback_fence["lowering_attempt"]["error_code"] == "team_subagent_minimum_version_regression"
            and rollback_fence["lowering_attempt_left_metadata_unchanged"]
            and rollback_fence["triggers_remain_active"]
        ),
    }
    return SubagentTriggerSpikeReport(
        schema_version=1,
        scope="synthetic_isolated_trigger_compatibility_spike",
        methodology={
            "fixture": "deterministic temporary SQLite database; no production data",
            "release_a": "additive columns and triggers exist while activation timestamps are null",
            "release_b": "explicit transaction checks live versions and invariants before atomic activation",
            "bypass": "mutations execute raw SQL without Store or service helpers",
            "rollback": "minimum versions and active database guards remain in force",
            "production_activation": "none",
        },
        environment={
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
        release_a_scenarios=release_a_scenarios,
        activation=activation,
        release_b_scenarios=release_b_scenarios,
        rollback_fence=rollback_fence,
        checks=checks,
    )


def _run_release_a_scenarios(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return [
        _run_mutation_scenario(
            connection,
            name="legacy_unversioned_definition_update",
            sql="UPDATE team_subagent SET display_name = ? WHERE id = ?",
            params=("Legacy edit", _SUBAGENT_ID),
            target_id=_SUBAGENT_ID,
            expected_accepted=True,
            expected_state_changed=True,
        ),
        _run_mutation_scenario(
            connection,
            name="legacy_insert_without_owner",
            sql="""
                INSERT INTO team_subagent (
                  id, team_id, name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
            """,
            params=(
                "sa_legacy_insert",
                _TEAM_ID,
                "legacy-insert",
                _NOW,
                _NOW,
            ),
            target_id="sa_legacy_insert",
            expected_accepted=True,
            expected_state_changed=True,
        ),
    ]


def _run_activation_scenarios(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    connection.executemany(
        """
        INSERT INTO team_subagent_app_compatibility (
          instance_id,
          reader_version,
          writer_version,
          started_at,
          heartbeat_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (
                "instance_pre_a",
                _RELEASE_A_VERSION,
                _RELEASE_A_VERSION,
                "2026-07-27T11:00:00Z",
                _NOW,
            ),
            (
                "instance_release_b",
                _RELEASE_B_VERSION,
                _RELEASE_B_VERSION,
                "2026-07-27T11:55:00Z",
                _NOW,
            ),
        ),
    )
    connection.commit()
    metadata_before = _metadata_snapshot(connection)
    try:
        activate_release_b(connection)
    except ActivationBlockedError as exc:
        live_old_writer_attempt = {
            "accepted": False,
            "error_code": exc.code,
            "incompatible_live_instances": exc.incompatible_live_instances,
        }
    else:
        live_old_writer_attempt = {
            "accepted": True,
            "error_code": None,
            "incompatible_live_instances": 0,
        }
    metadata_after_block = _metadata_snapshot(connection)

    connection.execute(
        """
        INSERT INTO team_subagent_app_compatibility (
          instance_id,
          reader_version,
          writer_version,
          started_at,
          heartbeat_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "instance_malformed",
            "release-b",
            "release-b",
            "2026-07-27T11:55:00Z",
            _NOW,
        ),
    )
    connection.execute(
        """
        UPDATE team_subagent_app_compatibility
        SET heartbeat_at = ?
        WHERE instance_id = 'instance_pre_a'
        """,
        (_STALE_HEARTBEAT,),
    )
    connection.commit()
    metadata_before_malformed_attempt = _metadata_snapshot(connection)
    try:
        activate_release_b(connection)
    except ActivationBlockedError as exc:
        malformed_version_attempt = {
            "accepted": False,
            "error_code": exc.code,
            "incompatible_live_instances": exc.incompatible_live_instances,
        }
    else:
        malformed_version_attempt = {
            "accepted": True,
            "error_code": None,
            "incompatible_live_instances": 0,
        }
    metadata_after_malformed_attempt = _metadata_snapshot(connection)
    connection.execute(
        """
        UPDATE team_subagent_app_compatibility
        SET heartbeat_at = ?
        WHERE instance_id = 'instance_malformed'
        """,
        (_STALE_HEARTBEAT,),
    )
    connection.commit()
    try:
        activate_release_b(connection)
    except (ActivationBlockedError, ValueError) as exc:
        drained_attempt = {
            "accepted": False,
            "error_code": getattr(exc, "code", str(exc)),
        }
    else:
        drained_attempt = {
            "accepted": True,
            "error_code": None,
        }
    metadata_after_activation = _metadata_snapshot(connection)
    orphaned_row = connection.execute(
        """
        SELECT owner_user_id, status
        FROM team_subagent
        WHERE id = 'sa_orphaned_disabled'
        """,
    ).fetchone()
    activation_times = {
        metadata_after_activation["owner_semantics_activated_at"],
        metadata_after_activation["lifecycle_semantics_activated_at"],
        metadata_after_activation["audience_semantics_activated_at"],
    }
    return {
        "live_window_seconds": 90,
        "live_old_writer_attempt": live_old_writer_attempt,
        "blocked_attempt_left_metadata_unchanged": metadata_before == metadata_after_block,
        "malformed_version_attempt": malformed_version_attempt,
        "malformed_attempt_left_metadata_unchanged": (metadata_before_malformed_attempt == metadata_after_malformed_attempt),
        "drained_attempt": drained_attempt,
        "metadata_activated_atomically": (
            activation_times == {_NOW}
            and metadata_after_activation["minimum_reader_version"] == _RELEASE_B_VERSION
            and metadata_after_activation["minimum_writer_version"] == _RELEASE_B_VERSION
        ),
        "disabled_orphan_preserved": (orphaned_row is not None and tuple(orphaned_row) == (None, "disabled")),
    }


def _run_release_b_scenarios(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return [
        _run_mutation_scenario(
            connection,
            name="old_writer_definition_without_version",
            sql="UPDATE team_subagent SET display_name = ? WHERE id = ?",
            params=("Bypass attempt", _SUBAGENT_ID),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_version_required",
        ),
        _run_mutation_scenario(
            connection,
            name="old_writer_owner_rollback_without_version",
            sql="UPDATE team_subagent SET owner_user_id = created_by WHERE id = ?",
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_version_required",
        ),
        _run_mutation_scenario(
            connection,
            name="old_writer_reenables_quarantine",
            sql="UPDATE team_subagent SET status = 'enabled' WHERE id = ?",
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_invariant_violation",
        ),
        _run_mutation_scenario(
            connection,
            name="old_writer_quarantine_metadata_without_version",
            sql="""
                UPDATE team_subagent
                SET quarantine_reason = 'legacy overwrite'
                WHERE id = ?
            """,
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_version_required",
        ),
        _run_mutation_scenario(
            connection,
            name="version_jump_is_rejected",
            sql="""
                UPDATE team_subagent
                SET description = ?, row_version = row_version + 2
                WHERE id = ?
            """,
            params=("Version jump", _SUBAGENT_ID),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_version_required",
        ),
        _run_mutation_scenario(
            connection,
            name="version_regression_is_rejected",
            sql="UPDATE team_subagent SET row_version = row_version - 1 WHERE id = ?",
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_invariant_violation",
        ),
        _run_mutation_scenario(
            connection,
            name="new_writer_exact_version_update",
            sql="""
                UPDATE team_subagent
                SET description = ?, row_version = row_version + 1
                WHERE id = ?
            """,
            params=("Compatible edit", _SUBAGENT_ID),
            target_id=_SUBAGENT_ID,
            expected_accepted=True,
            expected_state_changed=True,
        ),
        _run_mutation_scenario(
            connection,
            name="no_op_update_without_version",
            sql="UPDATE team_subagent SET description = description WHERE id = ?",
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=True,
            expected_state_changed=False,
        ),
        _run_mutation_scenario(
            connection,
            name="invalid_audience_is_rejected",
            sql="""
                UPDATE team_subagent
                SET audience_mode = 'legacy', row_version = row_version + 1
                WHERE id = ?
            """,
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_invariant_violation",
        ),
        _run_mutation_scenario(
            connection,
            name="invalid_lifecycle_is_rejected",
            sql="""
                UPDATE team_subagent
                SET lifecycle_state = 'deleted', row_version = row_version + 1
                WHERE id = ?
            """,
            params=(_SUBAGENT_ID,),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_invariant_violation",
        ),
        _run_mutation_scenario(
            connection,
            name="insert_without_owner_is_rejected",
            sql="""
                INSERT INTO team_subagent (
                  id, team_id, name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
            """,
            params=(
                "sa_ownerless_insert",
                _TEAM_ID,
                "ownerless-insert",
                _NOW,
                _NOW,
            ),
            target_id="sa_ownerless_insert",
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_invariant_violation",
        ),
        _run_mutation_scenario(
            connection,
            name="valid_insert_with_owner",
            sql="""
                INSERT INTO team_subagent (
                  id,
                  team_id,
                  name,
                  created_by,
                  owner_user_id,
                  created_at,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                "sa_valid_insert",
                _TEAM_ID,
                "valid-insert",
                "user_creator",
                "user_owner",
                _NOW,
                _NOW,
            ),
            target_id="sa_valid_insert",
            expected_accepted=True,
            expected_state_changed=True,
        ),
        _run_mutation_scenario(
            connection,
            name="creator_rewrite_is_rejected",
            sql="""
                UPDATE team_subagent
                SET created_by = ?, row_version = row_version + 1
                WHERE id = ?
            """,
            params=("user_other", _SUBAGENT_ID),
            target_id=_SUBAGENT_ID,
            expected_accepted=False,
            expected_state_changed=False,
            expected_error_code="team_subagent_created_by_immutable",
        ),
    ]


def _run_rollback_fence(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    metadata_before = _metadata_snapshot(connection)
    try:
        activate_release_b(
            connection,
            minimum_reader_version=_RELEASE_A_VERSION,
            minimum_writer_version=_RELEASE_A_VERSION,
        )
    except ValueError as exc:
        lowering_attempt = {
            "accepted": False,
            "error_code": str(exc),
        }
    else:
        lowering_attempt = {
            "accepted": True,
            "error_code": None,
        }
    metadata_after = _metadata_snapshot(connection)
    pre_release_b = check_application_compatibility(
        connection,
        reader_version=_RELEASE_A_VERSION,
        writer_version=_RELEASE_A_VERSION,
    )
    release_b = check_application_compatibility(
        connection,
        reader_version=_RELEASE_B_VERSION,
        writer_version=_RELEASE_B_VERSION,
    )
    metadata = _metadata_snapshot(connection)
    trigger_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name LIKE 'team_subagent_%'
            """,
        ).fetchone()[0]
    )
    return {
        "pre_release_b_candidate": asdict(pre_release_b),
        "release_b_candidate": asdict(release_b),
        "lowering_attempt": lowering_attempt,
        "lowering_attempt_left_metadata_unchanged": metadata_before == metadata_after,
        "minimum_writer_version": metadata["minimum_writer_version"],
        "activation_timestamps_remain_set": all(
            metadata[field] is not None
            for field in (
                "owner_semantics_activated_at",
                "lifecycle_semantics_activated_at",
                "audience_semantics_activated_at",
            )
        ),
        "triggers_remain_active": trigger_count == 4,
        "policy": "rollback_only_to_compatible_release; never deactivate guards or lower minimum writer",
    }


def _run_mutation_scenario(
    connection: sqlite3.Connection,
    *,
    name: str,
    sql: str,
    params: tuple[Any, ...],
    target_id: str,
    expected_accepted: bool,
    expected_state_changed: bool,
    expected_error_code: str | None = None,
) -> dict[str, Any]:
    connection.execute("SAVEPOINT mutation_scenario")
    before = _row_fingerprint(connection, target_id)
    error_code: str | None = None
    try:
        connection.execute(sql, params)
        accepted = True
    except sqlite3.IntegrityError as exc:
        accepted = False
        error_code = str(exc)
    after = _row_fingerprint(connection, target_id)
    state_changed = before != after
    connection.execute("ROLLBACK TO mutation_scenario")
    connection.execute("RELEASE mutation_scenario")
    passed = accepted is expected_accepted and state_changed is expected_state_changed and error_code == expected_error_code
    return {
        "name": name,
        "expected_accepted": expected_accepted,
        "accepted": accepted,
        "expected_error_code": expected_error_code,
        "error_code": error_code,
        "expected_state_changed": expected_state_changed,
        "state_changed": state_changed,
        "passed": passed,
    }


def _row_fingerprint(
    connection: sqlite3.Connection,
    target_id: str,
) -> tuple[Any, ...] | None:
    row = connection.execute(
        """
        SELECT
          owner_user_id,
          created_by,
          row_version,
          audience_mode,
          visibility,
          status,
          lifecycle_state,
          quarantine_started_at,
          quarantine_expires_at,
          quarantine_reason,
          quarantined_owner_user_id,
          name,
          display_name,
          description,
          system_prompt,
          tool_refs_json,
          model,
          max_iterations,
          skills_json,
          output_schema_json
        FROM team_subagent
        WHERE id = ?
        """,
        (target_id,),
    ).fetchone()
    return tuple(row) if row is not None else None


def _metadata_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
          schema_version,
          minimum_reader_version,
          minimum_writer_version,
          owner_semantics_activated_at,
          lifecycle_semantics_activated_at,
          audience_semantics_activated_at,
          updated_at
        FROM team_subagent_schema_meta
        WHERE singleton = 1
        """,
    ).fetchone()
    if row is None:
        raise ValueError("team_subagent_schema_meta_missing")
    return {
        "schema_version": int(row[0]),
        "minimum_reader_version": str(row[1]),
        "minimum_writer_version": str(row[2]),
        "owner_semantics_activated_at": row[3],
        "lifecycle_semantics_activated_at": row[4],
        "audience_semantics_activated_at": row[5],
        "updated_at": str(row[6]),
    }


def _validate_compatibility_version(version: str) -> None:
    if len(version) != 4 or not version.isascii() or not version.isdigit():
        raise ValueError("compatibility version must be a four-digit sortable value")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("activation timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
