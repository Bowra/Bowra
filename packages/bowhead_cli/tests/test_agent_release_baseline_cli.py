from __future__ import annotations

import json

from bowhead_cli.main import app
from bowhead_cli.migrate import upgrade
from typer.testing import CliRunner


def test_agent_release_baseline_cli_requires_explicit_apply_confirmation(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    runner = CliRunner()

    preview = runner.invoke(
        app,
        ["admin", "import-agent-release-baselines", "--db", str(db_path), "--json"],
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["state"] == "preview"

    staged = runner.invoke(
        app,
        [
            "admin",
            "import-agent-release-baselines",
            "--db",
            str(db_path),
            "--stage",
            "--json",
        ],
    )
    assert staged.exit_code == 0, staged.output
    payload = json.loads(staged.stdout)
    assert payload["state"] == "staged"

    inspected = runner.invoke(
        app,
        [
            "admin",
            "import-agent-release-baselines",
            "--db",
            str(db_path),
            "--inspect",
            "--run-id",
            payload["run_id"],
            "--json",
        ],
    )
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.stdout)["issues"] == []

    refused = runner.invoke(
        app,
        [
            "admin",
            "import-agent-release-baselines",
            "--db",
            str(db_path),
            "--apply",
            "--run-id",
            payload["run_id"],
            "--expected-digest",
            payload["digest"],
            "--json",
        ],
    )
    assert refused.exit_code == 1
    assert "writer-stopped" in refused.stdout and "confirmation" in refused.stdout

    applied = runner.invoke(
        app,
        [
            "admin",
            "import-agent-release-baselines",
            "--db",
            str(db_path),
            "--apply",
            "--run-id",
            payload["run_id"],
            "--expected-digest",
            payload["digest"],
            "--confirm-writer-stopped",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["state"] == "completed"
