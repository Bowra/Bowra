"""Stage 0A Subagent dependency reverse-index spike contract tests."""

from __future__ import annotations

import json
import sqlite3

import pytest
from bowhead_cli.main import app
from bowhead_cli.subagent_dependency_spike import (
    DependencyRef,
    apply_dependency_change_hook,
    apply_global_deny_hook,
    initialize_dependency_spike_fixture,
    invalidate_validator_version,
    mutate_dependency_source_without_hook,
    process_validation_batch,
    reconcile_dependency_catalog,
    replace_validated_dependency_refs,
    resolve_runtime_dependency_gate,
    run_subagent_dependency_spike,
)
from typer.testing import CliRunner

runner = CliRunner()


def test_spike_proves_reverse_index_contract_on_5000_agents() -> None:
    report = run_subagent_dependency_spike()

    assert report.scope == "synthetic_isolated_dependency_reverse_index"
    assert report.dataset == {
        "agents": 5000,
        "control_team_agents": 1,
        "dependency_refs": 5023,
        "native_ref_agents": 20,
        "shared_tool_ref_agents": 3,
        "missed_skill_ref_agents": 2,
        "queue_limit": 64,
    }
    assert all(report.checks.values())
    assert report.targeted_invalidation["unreferenced_change"]["matched_agents"] == 0
    assert report.targeted_invalidation["referenced_disable"]["matched_agents"] == 3
    assert report.global_rule_invalidation["native_deny"]["matched_agents"] == 20
    assert report.reconciler["repair"]["matched_agents"] == 2
    assert report.methodology["production_mutation"] == "none"


def test_unreferenced_change_bumps_watermark_without_staling_agents() -> None:
    connection = _connection()
    try:
        result = apply_dependency_change_hook(
            connection,
            team_id="team_dependency_spike",
            dependency_kind="team_tool",
            dependency_id="tool.unused",
            dependency_version="v2",
            status="active",
            queue_limit=8,
        )

        assert result.applied is True
        assert result.catalog_revision == 2
        assert result.matched_agents == 0
        assert result.newly_stale_agents == 0
        assert result.queue_depth == 0
        assert (
            _count(
                connection,
                "SELECT COUNT(*) FROM team_subagent_validation_stale",
            )
            == 0
        )
    finally:
        connection.close()


def test_referenced_disable_is_exact_and_runtime_gate_is_immediate() -> None:
    connection = _connection()
    try:
        result = apply_dependency_change_hook(
            connection,
            team_id="team_dependency_spike",
            dependency_kind="team_tool",
            dependency_id="tool.shared",
            dependency_version="v2",
            status="disabled",
            queue_limit=8,
        )

        assert result.matched_agents == 3
        assert result.newly_stale_agents == 3
        assert result.queue_depth == 3
        assert (
            resolve_runtime_dependency_gate(
                connection,
                subagent_id="sa_dependency_00001",
            ).denial_reason
            == "dependency_disabled"
        )
        assert resolve_runtime_dependency_gate(
            connection,
            subagent_id="sa_dependency_00030",
        ).allowed
        assert resolve_runtime_dependency_gate(
            connection,
            subagent_id="sa_dependency_control",
        ).allowed
    finally:
        connection.close()


def test_global_deny_only_targets_matching_native_refs() -> None:
    connection = _connection()
    try:
        result = apply_global_deny_hook(
            connection,
            team_id="team_dependency_spike",
            target_kind="native_tool",
            target_id="native.sql",
            denied=True,
            rule_version="deny-v2",
            queue_limit=32,
        )

        assert result.matched_agents == 20
        assert result.newly_stale_agents == 20
        assert (
            resolve_runtime_dependency_gate(
                connection,
                subagent_id="sa_dependency_00005",
            ).denial_reason
            == "blocked_by_security_policy"
        )
        assert resolve_runtime_dependency_gate(
            connection,
            subagent_id="sa_dependency_00030",
        ).allowed
    finally:
        connection.close()


def test_reconciler_repairs_missed_hook_once_and_then_is_noop() -> None:
    connection = _connection()
    try:
        mutate_dependency_source_without_hook(
            connection,
            team_id="team_dependency_spike",
            dependency_kind="team_skill",
            dependency_id="skill.missed",
            dependency_version="v2",
            status="disabled",
        )
        assert resolve_runtime_dependency_gate(
            connection,
            subagent_id="sa_dependency_00010",
        ).allowed

        repaired = reconcile_dependency_catalog(
            connection,
            team_id="team_dependency_spike",
            queue_limit=8,
        )
        repeated = reconcile_dependency_catalog(
            connection,
            team_id="team_dependency_spike",
            queue_limit=8,
        )

        assert repaired.applied is True
        assert repaired.matched_agents == 2
        assert repaired.newly_stale_agents == 2
        assert (
            resolve_runtime_dependency_gate(
                connection,
                subagent_id="sa_dependency_00010",
            ).denial_reason
            == "dependency_disabled"
        )
        assert repeated.applied is False
        assert repeated.catalog_revision == repaired.catalog_revision
        assert (
            _count(
                connection,
                """
            SELECT reconciler_repairs
            FROM subagent_dependency_spike_metrics
            WHERE team_id = 'team_dependency_spike'
            """,
            )
            == 1
        )
    finally:
        connection.close()


def test_reconciler_converts_missing_source_row_to_deleted_tombstone() -> None:
    connection = _connection()
    try:
        connection.execute(
            """
            DELETE FROM dependency_catalog_source
            WHERE team_id = 'team_dependency_spike'
              AND dependency_kind = 'team_skill'
              AND dependency_id = 'skill.missed'
            """
        )
        connection.commit()

        repaired = reconcile_dependency_catalog(
            connection,
            team_id="team_dependency_spike",
            queue_limit=8,
        )

        assert repaired.matched_agents == 2
        assert repaired.reason == "dependency_deleted"
        assert (
            resolve_runtime_dependency_gate(
                connection,
                subagent_id="sa_dependency_00010",
            ).denial_reason
            == "dependency_deleted"
        )
        assert connection.execute(
            """
            SELECT status
            FROM team_subagent_dependency_catalog
            WHERE team_id = 'team_dependency_spike'
              AND dependency_kind = 'team_skill'
              AND dependency_id = 'skill.missed'
            """
        ).fetchone() == ("deleted",)

        mutate_dependency_source_without_hook(
            connection,
            team_id="team_dependency_spike",
            dependency_kind="team_tool",
            dependency_id="tool.unused",
            dependency_version="v2",
            status="active",
        )
        unrelated_repair = reconcile_dependency_catalog(
            connection,
            team_id="team_dependency_spike",
            queue_limit=8,
        )
        assert unrelated_repair.matched_agents == 0
    finally:
        connection.close()


def test_stale_ledger_preserves_work_beyond_bounded_queue() -> None:
    connection = _connection()
    try:
        invalidation = invalidate_validator_version(
            connection,
            team_id="team_dependency_spike",
            validator_version=2,
            queue_limit=8,
        )
        batch = process_validation_batch(
            connection,
            team_id="team_dependency_spike",
            batch_size=3,
            queue_limit=8,
        )

        assert invalidation.matched_agents == 50
        assert invalidation.queue_depth == 8
        assert invalidation.deferred_stale_agents == 42
        assert batch["processed"] == 3
        assert batch["processed_priorities"] == sorted(batch["processed_priorities"])
        assert batch["processed_priorities"][0] == 10
        assert batch["queue_depth"] == 8
        assert batch["deferred_stale_agents"] == 39
        assert (
            _count(
                connection,
                """
            SELECT COUNT(*)
            FROM team_subagent_validation_stale
            WHERE team_id = 'team_dependency_spike'
            """,
            )
            == 47
        )
    finally:
        connection.close()


def test_security_deny_promotes_already_queued_validation() -> None:
    connection = _connection()
    try:
        invalidate_validator_version(
            connection,
            team_id="team_dependency_spike",
            validator_version=2,
            queue_limit=50,
        )
        before = connection.execute(
            """
            SELECT priority
            FROM team_subagent_validation_queue
            WHERE subagent_id = 'sa_dependency_00005'
            """
        ).fetchone()

        result = apply_global_deny_hook(
            connection,
            team_id="team_dependency_spike",
            target_kind="native_tool",
            target_id="native.sql",
            denied=True,
            rule_version="deny-v2",
            queue_limit=50,
        )
        after = connection.execute(
            """
            SELECT priority, reason
            FROM team_subagent_validation_queue
            WHERE subagent_id = 'sa_dependency_00005'
            """
        ).fetchone()

        assert before is not None and before[0] > 0
        assert result.matched_agents == 20
        assert result.newly_stale_agents == 0
        assert after == (0, "security_deny")
    finally:
        connection.close()


def test_successful_validation_replaces_refs_atomically() -> None:
    connection = _connection()
    try:
        before = _refs(connection, "sa_dependency_00025")
        digest = replace_validated_dependency_refs(
            connection,
            subagent_id="sa_dependency_00025",
            refs=(
                DependencyRef("team_tool", "tool.stable", "v1"),
                DependencyRef("team_tool", "tool.swap_new", "v1"),
            ),
        )
        after = _refs(connection, "sa_dependency_00025")

        assert ("team_tool", "tool.swap_old", "v1") in before
        assert ("team_tool", "tool.swap_old", "v1") not in after
        assert ("team_tool", "tool.swap_new", "v1") in after
        assert digest.startswith("sha256:")
        assert (
            apply_dependency_change_hook(
                connection,
                team_id="team_dependency_spike",
                dependency_kind="team_tool",
                dependency_id="tool.swap_old",
                dependency_version="v2",
                status="disabled",
                queue_limit=8,
            ).matched_agents
            == 0
        )
        assert (
            apply_dependency_change_hook(
                connection,
                team_id="team_dependency_spike",
                dependency_kind="team_tool",
                dependency_id="tool.swap_new",
                dependency_version="v2",
                status="disabled",
                queue_limit=8,
            ).matched_agents
            == 1
        )
    finally:
        connection.close()


def test_invalid_validation_snapshot_rolls_back_ref_replacement() -> None:
    connection = _connection()
    try:
        before = _refs(connection, "sa_dependency_00025")

        with pytest.raises(ValueError, match="dependency_snapshot_not_current"):
            replace_validated_dependency_refs(
                connection,
                subagent_id="sa_dependency_00025",
                refs=(DependencyRef("team_tool", "tool.swap_new", "v999"),),
            )

        assert _refs(connection, "sa_dependency_00025") == before
    finally:
        connection.close()


def test_spike_is_deterministic_and_reverse_lookup_is_indexed() -> None:
    first = run_subagent_dependency_spike()
    second = run_subagent_dependency_spike()

    assert first.to_dict() == second.to_dict()
    assert any("idx_team_subagent_dependency_reverse" in step for step in first.query_plan)
    assert not any("SCAN ref" in step for step in first.query_plan)


def test_cli_json_is_safe_and_reports_bounded_evidence() -> None:
    result = runner.invoke(
        app,
        ["admin", "spike-subagent-dependencies", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "synthetic_isolated_dependency_reverse_index"
    assert payload["dataset"]["agents"] == 5000
    assert payload["backpressure"]["global_invalidation"]["queue_depth"] == 64
    assert all(payload["checks"].values())
    assert payload["methodology"]["production_activation"] == "none"
    assert "sa_dependency_" not in result.output
    assert "tool.shared" not in result.output
    assert ".db" not in result.output


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    initialize_dependency_spike_fixture(connection, agent_count=50)
    return connection


def _count(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


def _refs(
    connection: sqlite3.Connection,
    subagent_id: str,
) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT
              dependency_kind,
              dependency_id,
              dependency_version
            FROM team_subagent_dependency_ref
            WHERE subagent_id = ?
            ORDER BY dependency_kind, dependency_id
            """,
            (subagent_id,),
        ).fetchall()
    ]
