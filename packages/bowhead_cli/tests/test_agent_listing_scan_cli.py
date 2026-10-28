from __future__ import annotations

import sqlite3

from bowhead_cli.main import app
from bowhead_cli.migrate import upgrade
from typer.testing import CliRunner


def test_agent_listing_scan_cli_defaults_to_read_only_and_can_apply(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    timestamp = "2026-09-02T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_1','team_1','agent-one','Agent One','owner@example.com','[]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.commit()

    runner = CliRunner()
    preview = runner.invoke(
        app,
        ["admin", "scan-agent-listings", "--db", str(db_path), "--json"],
    )
    assert preview.exit_code == 0, preview.output
    assert '"blocked": 1' in preview.output
    assert '"dry_run": true' in preview.output
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_listing_safety_report").fetchone() == (0,)

    applied = runner.invoke(
        app,
        [
            "admin",
            "scan-agent-listings",
            "--db",
            str(db_path),
            "--apply",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert '"dry_run": false' in applied.output
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_listing_safety_report").fetchone() == (1,)


def test_agent_listing_scan_cli_missing_database_does_not_create_file(tmp_path) -> None:
    db_path = tmp_path / "missing.db"
    result = CliRunner().invoke(
        app,
        ["admin", "scan-agent-listings", "--db", str(db_path), "--json"],
    )
    assert result.exit_code == 1
    assert "database not found" in result.output
    assert not db_path.exists()
