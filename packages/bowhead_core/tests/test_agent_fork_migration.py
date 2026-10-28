"""M4-C Fork schema upgrade, drift, CAS policy, and immutable lineage."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

pytestmark = pytest.mark.integration
_MIGRATION = import_module("bowhead_core.migrations.versions.0045_agent_fork")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _seed_team(db: sqlite3.Connection) -> None:
    timestamp = "2026-09-06T00:00:00+00:00"
    db.execute(
        "INSERT INTO teams(id,name,status,created_at,created_by_user_id) VALUES ('team_1','Team','active',?,'owner_1')",
        (timestamp,),
    )
    for user_id in ("owner_1", "member_1"):
        db.execute(
            "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            (user_id, "team_1", f"{user_id}@example.com", "active", timestamp),
        )
        db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            (f"tm_{user_id}", user_id, "team_1", "member", "active", timestamp),
        )
    for agent_id, name, owner_id in (
        ("osa_source", "source-agent", "owner_1"),
        ("osa_fork", "fork-agent", "member_1"),
    ):
        db.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              skills_json,visibility,created_by,status,created_at,updated_at,
              owner_user_id,audience_mode,definition_mode,live_revision_id,live_release_id
            ) VALUES (?,?,?,?,?,'prompt','[]','[]','creator_only',?,'enabled',?,?,?,'private','revisioned',?,?)
            """,
            (
                agent_id,
                "team_1",
                name,
                name,
                "description",
                owner_id,
                timestamp,
                timestamp,
                owner_id,
                f"arv_{agent_id}",
                f"arl_{agent_id}",
            ),
        )


def _insert_lineage(
    db: sqlite3.Connection,
    *,
    policy_snapshot_json: str = '{"policy":"recipients","row_version":1}',
    fork_revision_ref: str = "arv_osa_fork",
) -> None:
    timestamp = "2026-09-06T00:00:00+00:00"
    db.execute(
        """
        INSERT INTO team_subagent_fork_lineage(
          id,team_id,fork_agent_id,source_agent_id,source_kind,source_revision_ref,
          source_release_id,source_owner_user_id,source_display_name,policy_snapshot_json,
          source_snapshot_hash,fork_revision_ref,dependency_gaps_json,idempotency_hash,
          request_hash,created_by,created_at
        ) VALUES (
          'afk_1234567890abcdef','team_1','osa_fork','osa_source','team','arv_osa_source',
          'arl_osa_source','owner_1','Source',?,?,?,'[]',?,?,'member_1',?
        )
        """,
        (
            policy_snapshot_json,
            f"sha256:{'a' * 64}",
            fork_revision_ref,
            f"sha256:{'b' * 64}",
            f"sha256:{'c' * 64}",
            timestamp,
        ),
    )


def test_existing_agents_survive_upgrade_without_implicit_policy_or_lineage(tmp_path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, _MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        _seed_team(db)
        before = db.execute("SELECT id,name FROM team_subagent ORDER BY id").fetchall()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,name FROM team_subagent ORDER BY id").fetchall() == before
        assert db.execute("SELECT count(*) FROM team_subagent_fork_policy").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM team_subagent_fork_lineage").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["table", "index", "trigger"])
def test_same_name_fork_schema_drift_fails_closed(tmp_path, kind) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, _MIGRATION.revision)
    command.stamp(config, _MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if kind == "table":
            db.execute("ALTER TABLE team_subagent_fork_policy ADD COLUMN unrelated TEXT")
        elif kind == "index":
            db.execute("DROP INDEX idx_team_subagent_fork_source")
            db.execute("CREATE INDEX idx_team_subagent_fork_source ON team_subagent_fork_lineage(fork_agent_id)")
        else:
            db.execute("DROP TRIGGER trg_team_subagent_fork_lineage_update")
            db.execute(
                "CREATE TRIGGER trg_team_subagent_fork_lineage_update BEFORE UPDATE ON team_subagent_fork_lineage WHEN 0 BEGIN SELECT RAISE(ABORT, 'disabled'); END",
            )
    with pytest.raises(RuntimeError, match="0045 found incompatible"):
        command.upgrade(config, "head")


def test_non_core_target_does_not_gain_fork_tables(tmp_path) -> None:
    path = tmp_path / "files.db"
    command.upgrade(_config(path, "files"), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='team_subagent_fork_policy'").fetchone() is None


def test_policy_is_owner_cas_and_lineage_is_immutable(tmp_path) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    timestamp = "2026-09-06T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        _seed_team(db)
        db.execute(
            """
            INSERT INTO team_subagent_fork_policy(
              team_id,source_agent_id,policy,row_version,updated_by,created_at,updated_at
            ) VALUES ('team_1','osa_source','recipients',1,'owner_1',?,?)
            """,
            (timestamp, timestamp),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                UPDATE team_subagent_fork_policy
                SET policy='team_members',row_version=2,updated_by='member_1',updated_at='2026-09-06T00:00:01+00:00'
                WHERE team_id='team_1' AND source_agent_id='osa_source'
                """,
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM team_subagent_fork_policy WHERE source_agent_id='osa_source'")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_lineage(db, policy_snapshot_json="not-json")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_lineage(db, policy_snapshot_json='{"policy":"recipients","row_version":2}')
        with pytest.raises(sqlite3.IntegrityError):
            _insert_lineage(db, fork_revision_ref="arv_wrong")
        _insert_lineage(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE team_subagent_fork_lineage SET source_display_name='Changed'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM team_subagent_fork_lineage")
