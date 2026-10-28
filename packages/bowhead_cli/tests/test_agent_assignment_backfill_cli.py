from __future__ import annotations

import sqlite3

from bowhead_cli.main import app
from bowhead_cli.migrate import upgrade
from typer.testing import CliRunner


def test_assignment_backfill_cli_is_one_time_after_cutover(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    runner = CliRunner()

    applied = runner.invoke(
        app,
        [
            "admin",
            "backfill-agent-assignments",
            "--db",
            str(db_path),
            "--apply",
            "--batch-size",
            "1",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert '"expected": 0' in applied.output
    assert '"cutover_completed_at": null' not in applied.output

    repeated = runner.invoke(
        app,
        [
            "admin",
            "backfill-agent-assignments",
            "--db",
            str(db_path),
            "--apply",
        ],
    )
    assert repeated.exit_code == 1
    assert "already complete" in repeated.output


def test_assignment_backfill_cli_missing_database_does_not_create_file(tmp_path) -> None:
    db_path = tmp_path / "missing.db"

    result = CliRunner().invoke(
        app,
        ["admin", "backfill-agent-assignments", "--db", str(db_path), "--json"],
    )

    assert result.exit_code == 1
    assert "database not found" in result.output
    assert not db_path.exists()


def test_assignment_backfill_dry_run_is_read_only(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        before = "\n".join(connection.iterdump())

    result = CliRunner().invoke(
        app,
        ["admin", "backfill-agent-assignments", "--db", str(db_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    with sqlite3.connect(db_path) as connection:
        after = "\n".join(connection.iterdump())
    assert after == before


def test_ensure_marketplace_cutover_is_idempotent_and_verify_only(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["admin", "ensure-agent-marketplace-cutover", "--db", str(db_path), "--json"],
    )
    repeated = runner.invoke(
        app,
        ["admin", "ensure-agent-marketplace-cutover", "--db", str(db_path), "--json"],
    )
    verified = runner.invoke(
        app,
        [
            "admin",
            "ensure-agent-marketplace-cutover",
            "--db",
            str(db_path),
            "--verify-only",
            "--json",
        ],
    )

    assert first.exit_code == repeated.exit_code == verified.exit_code == 0
    assert '"ready": true' in first.output
    assert '"ready": true' in repeated.output
    assert '"verify_only": true' in verified.output
