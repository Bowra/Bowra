from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = REPOSITORY_ROOT / "deploy" / "start-app.sh"


def _fake_command(path: Path, name: str) -> Path:
    command = path / name
    command.write_text(
        """#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >> "$STARTUP_LOG"
case "${FAIL_MATCH:-}" in
  '') ;;
  *) printf '%s' "$*" | grep -F -- "$FAIL_MATCH" >/dev/null && exit 23 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return command


@pytest.mark.parametrize("scenario", ["fresh", "upgrade", "already_cutover"])
def test_startup_orders_candidate_migration_cutover_and_server(tmp_path: Path, scenario: str) -> None:
    log = tmp_path / "startup.log"
    bowhead = _fake_command(tmp_path, "bowhead")
    uvicorn = _fake_command(tmp_path, "uvicorn")
    python = _fake_command(tmp_path, "python")
    environment = os.environ.copy()
    environment.update(
        {
            "STARTUP_LOG": str(log),
            "BOWHEAD_STARTUP_BOWHEAD_BIN": str(bowhead),
            "BOWHEAD_STARTUP_PYTHON_BIN": str(python),
            "BOWHEAD_STARTUP_UVICORN_BIN": str(uvicorn),
            "BOWHEAD_VERSION": scenario,
            "BOWHEAD_DB_URL": "sqlite+aiosqlite:////data/bowhead.db",
        },
    )

    result = subprocess.run(
        ["/bin/sh", str(START_SCRIPT)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"python -m bowhead_gateway.installation prepare --db-url sqlite+aiosqlite:////data/bowhead.db --migration-command {bowhead}"
    assert "backfill-agent-assignments" in calls[1] and "--apply" not in calls[1]
    assert "ensure-agent-marketplace-cutover" in calls[2] and "--verify-only" not in calls[2]
    assert "ensure-agent-marketplace-cutover" in calls[3] and "--verify-only" in calls[3]
    # M2 release baseline runs in the same writer-stop window, after M1 is verified.
    assert "ensure-agent-release-baseline" in calls[4]
    assert calls[5].startswith("uvicorn bowhead_gateway.main:app")


@pytest.mark.parametrize(
    "failure",
    [
        "bowhead_gateway.installation prepare",
        "backfill-agent-assignments",
        "ensure-agent-marketplace-cutover --db sqlite+aiosqlite:////data/bowhead.db --source-build",
        "--verify-only",
        "ensure-agent-release-baseline",
    ],
)
def test_startup_failure_never_starts_server(tmp_path: Path, failure: str) -> None:
    log = tmp_path / "startup.log"
    bowhead = _fake_command(tmp_path, "bowhead")
    uvicorn = _fake_command(tmp_path, "uvicorn")
    python = _fake_command(tmp_path, "python")
    environment = os.environ.copy()
    environment.update(
        {
            "STARTUP_LOG": str(log),
            "FAIL_MATCH": failure,
            "BOWHEAD_STARTUP_BOWHEAD_BIN": str(bowhead),
            "BOWHEAD_STARTUP_PYTHON_BIN": str(python),
            "BOWHEAD_STARTUP_UVICORN_BIN": str(uvicorn),
            "BOWHEAD_DB_URL": "sqlite+aiosqlite:////data/bowhead.db",
        },
    )

    result = subprocess.run(
        ["/bin/sh", str(START_SCRIPT)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 23
    assert not any(line.startswith("uvicorn ") for line in log.read_text(encoding="utf-8").splitlines())
