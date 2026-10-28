"""WP5 Skill quarantine cleanup-ledger migration contract."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core._db import open_shared_ctx
from bowhead_core.resource_file_quarantine import quarantine_paths
from bowhead_core.resource_impact import read_change_receipt
from bowhead_core.user_store import UserStore

from bowhead_core import migrations
from bowhead_core import resource_file_quarantine as cleanup

pytestmark = pytest.mark.integration
MIGRATION = import_module(
    "bowhead_core.migrations.versions.0055_resource_file_cleanup_receipt",
)


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _run(coro):
    return asyncio.run(coro)


def test_0055_upgrade_is_repeatable_and_at_head(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later historical revisions (e.g. 0063 table creation) may not rerun on upgraded DBs.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is always true and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        assert {row[1] for row in db.execute("PRAGMA table_info(resource_file_cleanup_receipt)")} == {
            "cleanup_id",
            "team_id",
            "actor_user_id",
            "idempotency_key",
            "resource_kind",
            "resource_id",
            "original_path",
            "quarantine_path",
            "state",
            "error_code",
            "created_at",
            "updated_at",
        }


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "matrix"])
def test_0055_is_noop_for_non_core_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='resource_file_cleanup_receipt'").fetchone() is None


def test_0055_rejects_same_name_table_drift(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE resource_file_cleanup_receipt ADD COLUMN unrelated TEXT")

    with pytest.raises(RuntimeError, match="Resource file cleanup schema drift"):
        command.upgrade(config, "head")


def test_quarantine_restores_prior_moves_when_a_later_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    first = root / "first"
    second = root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    original_rename = Path.rename

    def flaky_rename(path: Path, target: Path) -> Path:
        if path.resolve() == second.resolve():
            raise OSError("synthetic move failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="synthetic move failure"):
        quarantine_paths(
            [first, second],
            root=root,
            command_identity="team:user:command",
        )

    assert first.is_dir()
    assert second.is_dir()


def test_quarantine_rejects_paths_outside_owner_root(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="outside its owner root"):
        quarantine_paths(
            [outside],
            root=root,
            command_identity="team:user:command",
        )
    assert outside.is_dir()


def test_pending_cleanup_remains_retryable_after_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    users = UserStore(str(path))
    _run(users.initialize())
    team = _run(users.create_team(name="Cleanup", max_seats=2))
    user = _run(users.create_user(team_id=team.id, email="cleanup@example.com"))
    quarantined = tmp_path / ".bowhead-quarantine" / "bundle"
    quarantined.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO resource_change_receipt VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                team.id,
                user.id,
                "cleanup-retry-command-0001",
                "skill",
                "sk_cleanup",
                "delete",
                "sha256:request",
                "sha256:impact",
                "{}",
                now,
            ),
        )
        db.execute(
            "INSERT INTO resource_file_cleanup_receipt VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "rfc_cleanup",
                team.id,
                user.id,
                "cleanup-retry-command-0001",
                "skill",
                "sk_cleanup",
                str(tmp_path / "skills" / "bundle"),
                str(quarantined),
                "pending",
                None,
                now,
                now,
            ),
        )

    original_remove = cleanup._remove_path

    def fail_remove(_path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(cleanup, "_remove_path", fail_remove)
    _run(cleanup.retry_pending_cleanup(str(path)))
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT state,error_code FROM resource_file_cleanup_receipt WHERE cleanup_id='rfc_cleanup'",
        ).fetchone() == ("pending", "OSError")

    monkeypatch.setattr(cleanup, "_remove_path", original_remove)
    _run(cleanup.retry_pending_cleanup(str(path)))
    assert not quarantined.exists()
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT state,error_code FROM resource_file_cleanup_receipt WHERE cleanup_id='rfc_cleanup'",
        ).fetchone() == ("completed", None)

        db.execute(
            "UPDATE resource_change_receipt SET created_at='2020-01-01T00:00:00+00:00'",
        )

    async def read_expired_receipt() -> dict[str, object] | None:
        async with open_shared_ctx(str(path)) as db:
            value = await read_change_receipt(
                db,
                team_id=team.id,
                actor_user_id=user.id,
                idempotency_key="cleanup-retry-command-0001",
                request_digest="sha256:request",
            )
            await db.commit()
            return value

    assert _run(read_expired_receipt()) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM resource_change_receipt").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM resource_file_cleanup_receipt").fetchone() == (0,)
