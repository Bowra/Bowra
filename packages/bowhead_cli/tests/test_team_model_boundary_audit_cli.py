from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bowhead_cli.main import app
from bowhead_cli.team_model_boundary_audit import audit_team_model_boundary
from typer.testing import CliRunner


def _config() -> dict:
    provider = "team_provider"
    model = "team-model"
    binding = {
        "primary": {"provider": provider, "model": model},
        "fallback": [],
    }
    return {
        "schema_version": 2,
        "providers": {
            provider: {
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "team-secret-that-must-never-be-output",
                "models": [model],
            },
        },
        "agents": {
            "lead": binding,
            "modeler": binding,
            "analyst": {"default": binding, "profiles": {}},
            "narrative": binding,
            "echo": binding,
            "judge": binding,
        },
    }


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE teams (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                llm_config_encrypted TEXT NOT NULL DEFAULT '',
                llm_config_version INTEGER NOT NULL DEFAULT 0
            )
            """,
        )
        connection.executemany(
            "INSERT INTO teams VALUES (?,?,?,?)",
            (
                ("team_good", "active", "PLAIN:" + json.dumps(_config()), 7),
                ("team_missing", "active", "", 0),
                ("team_invalid", "active", 'PLAIN:{"schema_version":2}', 3),
                ("team_inactive", "inactive", "", 0),
            ),
        )
    return path


def test_audit_is_team_owned_read_only_and_secret_free(tmp_path: Path) -> None:
    db = _database(tmp_path)

    report = audit_team_model_boundary(db)
    payload = json.dumps(report.to_dict(), sort_keys=True)

    assert report.read_only is True
    assert report.writes_performed == 0
    assert report.provider_calls_performed == 0
    assert report.teams_checked == 3
    assert report.results == {"team_safe_success": 1, "team_unavailable": 2}
    assert report.reasons == {
        "config_invalid": 1,
        "config_missing": 1,
        "resolved": 1,
    }
    assert "team-secret-that-must-never-be-output" not in payload
    assert "team_provider" not in payload
    assert "team-model" not in payload

    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 4


def test_audit_can_target_one_team(tmp_path: Path) -> None:
    report = audit_team_model_boundary(_database(tmp_path), team_id="team_good")

    assert report.teams_checked == 1
    assert report.teams[0].result == "team_safe_success"
    assert report.teams[0].catalog_resolvable is True
    assert report.teams[0].router_resolvable is True
    assert report.teams[0].provider_count == 1
    assert report.teams[0].model_count == 1


def test_cli_json_is_stable_and_returns_non_success_rows(tmp_path: Path) -> None:
    db = _database(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "audit-team-model-boundary",
            "--db",
            str(db),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["teams_checked"] == 3
    assert payload["writes_performed"] == 0
    assert {row["reason"] for row in payload["teams"]} == {
        "resolved",
        "config_missing",
        "config_invalid",
    }
    assert "team-secret-that-must-never-be-output" not in result.output
