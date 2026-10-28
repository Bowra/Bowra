"""Stage 0A synthetic Subagent catalog benchmark contract tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bowhead_cli.main import app
from bowhead_cli.subagent_benchmark import (
    generate_subagent_benchmark_fixture,
    run_subagent_catalog_benchmark,
)
from typer.testing import CliRunner

runner = CliRunner()


def _database_dump(db_path: Path) -> str:
    connection = sqlite3.connect(db_path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


@pytest.mark.parametrize("agent_count", [1, 50, 500, 5000])
def test_fixture_supports_capacity_ladder_and_large_roster(
    tmp_path: Path,
    agent_count: int,
) -> None:
    db_path = tmp_path / f"fixture-{agent_count}.db"

    stats = generate_subagent_benchmark_fixture(
        db_path,
        agent_count=agent_count,
        member_count=1,
    )

    connection = sqlite3.connect(db_path)
    try:
        stored_agents = connection.execute(
            "SELECT COUNT(*) FROM team_subagent",
        ).fetchone()[0]
        stored_members = connection.execute(
            "SELECT COUNT(*) FROM team_members",
        ).fetchone()[0]
        maximum_roster = connection.execute(
            """
            SELECT COALESCE(MAX(roster_size),0)
            FROM (
              SELECT resource_id,COUNT(*) AS roster_size
              FROM resource_grants
              WHERE effect='allow'
              GROUP BY resource_id
            )
            """,
        ).fetchone()[0]
    finally:
        connection.close()

    assert stored_agents == agent_count
    assert stored_members == 1
    assert stats["profile"] == "synthetic_provisional_not_production"
    assert sum(stats["audience"].values()) == agent_count
    if agent_count == 5000:
        assert maximum_roster == 200
        assert stats["custom_rosters"]["actual_counts"].keys() == {
            "1",
            "10",
            "50",
            "200",
        }


def test_fixture_generation_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"

    first_stats = generate_subagent_benchmark_fixture(
        first,
        agent_count=500,
        member_count=100,
    )
    second_stats = generate_subagent_benchmark_fixture(
        second,
        agent_count=500,
        member_count=100,
    )

    assert first_stats == second_stats
    assert _database_dump(first) == _database_dump(second)


def test_5000_agent_benchmark_covers_page_50_and_index_contract() -> None:
    report = run_subagent_catalog_benchmark(
        agent_count=5000,
        member_count=1000,
        iterations=5,
    )

    assert report.scope == "synthetic_isolated_catalog_read"
    assert report.methodology["production_slo"] == ("not established by this synthetic benchmark")
    assert report.checks == {
        "all_query_plans_indexed": True,
        "first_two_pages_unique": True,
        "page_50_available": True,
        "page_50_requirement_met": True,
        "fixture_counts_match": True,
    }
    assert report.dataset["agents"] == 5000
    assert report.dataset["members"] == 1000

    results = {result["name"]: result for result in report.workloads}
    assert results["team_directory_page_50"]["row_count"] == 50
    assert all(result["plan_ok"] for result in results.values())
    assert all(result["query_plan"] for result in results.values())
    assert all(result[mode]["p95"] >= result[mode]["min"] for result in results.values() for mode in ("new_connection_ms", "warm_connection_ms"))


def test_cli_json_labels_synthetic_scope_and_allows_small_dataset() -> None:
    result = runner.invoke(
        app,
        [
            "admin",
            "benchmark-subagent-catalog",
            "--agents",
            "50",
            "--members",
            "100",
            "--iterations",
            "5",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "synthetic_isolated_catalog_read"
    assert payload["dataset"]["agents"] == 50
    assert payload["checks"]["page_50_available"] is False
    assert payload["checks"]["page_50_requirement_met"] is True
    assert payload["methodology"]["fixture"].endswith("no production data")
    assert "benchmark.db" not in result.output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--agents", "49"], "agents must be one of"),
        (["--members", "2"], "members must be one of"),
        (["--iterations", "4"], "iterations must be between"),
    ],
)
def test_cli_rejects_dimensions_outside_benchmark_contract(
    arguments: list[str],
    message: str,
) -> None:
    result = runner.invoke(
        app,
        ["admin", "benchmark-subagent-catalog", *arguments, "--json"],
    )

    assert result.exit_code == 1
    assert message in result.output
