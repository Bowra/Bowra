"""0058 feedback_candidates 迁移契约（B 线批 2a′）。"""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module(
    "bowhead_core.migrations.versions.0059_feedback_candidates",
)

_CANDIDATE_COLUMNS = {
    "id",
    "team_id",
    "target_kind",
    "target_ref",
    "base_fingerprint",
    "proposed",
    "rubric_text",
    "evidence",
    "semantic_sig",
    "evidence_count",
    "cluster_id",
    "state",
    "proposer_id",
    "decided_by",
    "decided_at",
    "decide_note",
    "expires_at",
    "created_at",
    "updated_at",
}


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(migrations.__file__).resolve().parent),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _insert(db: sqlite3.Connection, cid: str, sig: str, state: str) -> None:
    db.execute(
        """
        INSERT INTO feedback_candidates (
            id, team_id, target_kind, target_ref, base_fingerprint, proposed,
            rubric_text, evidence, semantic_sig, evidence_count, state,
            proposer_id, expires_at, created_at, updated_at
        ) VALUES (?, 't1', 'semantic', '', '', '{}', '', '[]', ?, 1, ?, 'u1',
                  '2027-01-01T00:00:00+00:00', '2026-09-09T00:00:00+00:00',
                  '2026-09-09T00:00:00+00:00')
        """,
        (cid, sig, state),
    )


def test_0059_upgrade_is_repeatable_and_creates_contract(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later historical revisions (e.g. 0063 table creation) may not be rerunnable on an upgraded database.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is tautological and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(feedback_candidates)")}
        assert cols == _CANDIDATE_COLUMNS
        sup_cols = {row[1] for row in db.execute("PRAGMA table_info(feedback_candidate_suppressions)")}
        assert sup_cols == {
            "team_id",
            "semantic_sig",
            "target_kind",
            "target_ref",
            "last_state",
            "last_evidence_count",
            "pending_contributors",
            "suppressed_until",
            "created_at",
            "updated_at",
        }


def test_0059_partial_unique_index_guards_open_only(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _insert(db, "fbc_a", "sig1", "open")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(db, "fbc_b", "sig1", "open")
        # Non-open states are outside the unique constraint; historical candidates with the same signature may coexist.
        _insert(db, "fbc_c", "sig1", "rejected")
        _insert(db, "fbc_d", "sig1", "expired")


def test_0059_noops_on_non_core_targets(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    command.upgrade(_config(path, target="files"), "head")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "feedback_candidates" not in tables


def test_0059_rejects_impostor_index_on_rerun(tmp_path: Path) -> None:
    """同名假索引（非 partial/非 unique）重升级必须报 drift——拒绝路径的 positive control。"""
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX uq_fbc_open_sig")
        db.execute(
            "CREATE INDEX uq_fbc_open_sig ON feedback_candidates(team_id, semantic_sig)",
        )
    command.stamp(config, MIGRATION.down_revision)
    with pytest.raises(Exception, match="feedback index drift"):
        command.upgrade(config, "head")
