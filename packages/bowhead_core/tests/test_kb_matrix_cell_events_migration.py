"""0240 adds cell history and the matrix visibility setting to the matrix target only."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from bowhead_core.kb import matrix_store
from bowhead_core.sqlite_domains import APPENDED_COLUMNS, APPENDED_TABLES, DOMAIN_TABLES
from core_legacy_database import TARGET_FILES
from sqlalchemy import create_engine
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0240_matrix_cell_events")
#: The frozen `1.0.0` baseline revision that existing deployments adopt before later revisions.
BASELINE = import_module("bowhead_core.migrations.versions.0001_initial")
NEW_OBJECTS = {"kb_matrix_cell_events", "idx_kb_matrix_cell_events_cell"}
COLUMN_SQL = ", visibility TEXT NOT NULL DEFAULT 'space' CHECK(visibility IN ('space','private')))"


def _schema(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as db:
        return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def _seed(path: Path) -> None:
    """Two matrices (one soft-deleted) and a verified cell, in the columns every released schema has."""
    with closing(sqlite3.connect(path)) as db:
        for matrix_id, deleted in (("mx_live", None), ("mx_gone", "2026-11-02T00:00:00+00:00")):
            db.execute(
                "INSERT INTO kb_matrices(matrix_id,name,workspace_id,team_id,files_json,columns_json,last_seq,structure_seq,created_by,"
                "created_at,updated_at,soft_deleted_at) VALUES (?,?,?,?,?,?,1,0,'u_1','2026-11-01T00:00:00+00:00','2026-11-01T00:00:00+00:00',?)",
                (matrix_id, "Contracts", "ws_1", "team_1", '[{"file_id": "kbf_1", "char_count": 10}]', '[{"column_id": "mxcol_1"}]', deleted),
            )
        db.execute(
            "INSERT INTO kb_matrix_cells(matrix_id,file_id,column_id,workspace_id,team_id,status,variants_json,verified,prompt_version,seq,"
            "created_at,updated_at) VALUES ('mx_live','kbf_1','mxcol_1','ws_1','team_1','done','[]',1,1,1,'2026-11-01','2026-11-01')"
        )
        db.commit()


# Core passes the 0220 split only through the domain coordinator; `test_kb_matrix_cell_events_layout.py`
# upgrades the twelve-file layout and checks that only the matrix file changes there too.
@pytest.mark.parametrize("target", [target for target in TARGET_FILES if target != "core"])
def test_upgrade_adds_history_only_to_the_matrix_target(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "matrix":
        _seed(path)
    before = _schema(path)
    command.upgrade(config, MIGRATION.revision)
    after = _schema(path)
    command.upgrade(config, MIGRATION.revision)
    assert _schema(path) == after
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    if target != "matrix":
        assert after == before
        return
    assert set(after) - set(before) == NEW_OBJECTS
    # kb_matrices gains the column in place: no rebuild, every other object keeps its SQL.
    assert after.pop("kb_matrices") == before.pop("kb_matrices")[:-1] + COLUMN_SQL
    assert {name: sql for name, sql in after.items() if name in before} == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT matrix_id,visibility FROM kb_matrices ORDER BY matrix_id").fetchall() == [
            ("mx_gone", "space"),
            ("mx_live", "space"),
        ]
        assert db.execute("SELECT matrix_id,file_id,verified FROM kb_matrix_cells").fetchall() == [("mx_live", "kbf_1", 1)]
        # No history is invented for cells written before the revision.
        assert db.execute("SELECT COUNT(*) FROM kb_matrix_cell_events").fetchone() == (0,)
        assert [row[2] for row in db.execute("PRAGMA index_info(idx_kb_matrix_cell_events_cell)")] == [
            "matrix_id",
            "file_id",
            "column_id",
            "created_at",
        ]


def test_empty_database_and_the_1_0_0_baseline_both_reach_the_revision(tmp_path):
    for name, start in (("empty.db", None), ("baseline.db", BASELINE.revision)):
        path = tmp_path / name
        config = _config(path, "matrix")
        if start is not None:
            command.upgrade(config, start)
            _seed(path)
        command.upgrade(config, MIGRATION.revision)
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
            assert set(_schema(path)) >= NEW_OBJECTS
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            visibility = db.execute("SELECT DISTINCT visibility FROM kb_matrices").fetchall()
        assert visibility == ([] if start is None else [("space",)])


def test_checks_refuse_unknown_values(tmp_path):
    path = tmp_path / "kb_matrix.db"
    command.upgrade(_config(path, "matrix"), MIGRATION.revision)
    _seed(path)
    event = "INSERT INTO kb_matrix_cell_events VALUES ('mxev_1','mx_live','kbf_1','mxcol_1','ws_1','team_1',?,NULL,NULL,?,NULL,'t')"
    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute("UPDATE kb_matrices SET visibility='team' WHERE matrix_id='mx_live'")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(event, ("deleted", 1))
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            db.execute(event, ("verified", None))
        db.execute("UPDATE kb_matrices SET visibility='private' WHERE matrix_id='mx_live'")
        for index, kind in enumerate(matrix_store.CELL_EVENT_KINDS):
            db.execute(event.replace("'mxev_1'", f"'mxev_{index}'"), (kind, index))


def test_the_store_kinds_match_the_revision():
    """The runtime list and the frozen CHECK name the same kinds (a revision must not import application code)."""
    ddl = next(statement for statement in MIGRATION.STATEMENTS if "CREATE TABLE kb_matrix_cell_events" in statement)
    checked = re.search(r"kind IN \(([^)]*)\)", ddl)
    assert checked is not None
    assert tuple(re.findall(r"'([a-z_]+)'", checked.group(1))) == matrix_store.CELL_EVENT_KINDS


def test_failed_installation_leaves_no_table_or_column(tmp_path, monkeypatch):
    path = tmp_path / "kb_matrix.db"
    command.upgrade(_config(path, "matrix"), MIGRATION.down_revision)
    _seed(path)
    before = _schema(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:

        class Interrupted:
            def exec_driver_sql(self, sql, *args):
                if sql.startswith("CREATE INDEX idx_kb_matrix_cell_events_cell"):
                    raise RuntimeError("installation interrupted")
                return db.exec_driver_sql(sql, *args)

        monkeypatch.setattr(
            MIGRATION, "context", SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": "matrix"}), get_bind=lambda: Interrupted())
        )
        with pytest.raises(RuntimeError, match="installation interrupted"):
            MIGRATION.upgrade()
        db.commit()
    engine.dispose()
    assert _schema(path) == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM kb_matrices").fetchone() == (2,)


def test_registries_and_the_erasure_policy_match_the_migrated_schema(tmp_path):
    assert APPENDED_TABLES["kb_matrix_cell_events"] == ("matrix", MIGRATION.revision)
    assert "kb_matrix_cell_events" in DOMAIN_TABLES["matrix"]
    assert APPENDED_COLUMNS["kb_matrices"] == (("visibility",), MIGRATION.revision)
    path = tmp_path / "kb_matrix.db"
    command.upgrade(_config(path, "matrix"), MIGRATION.revision)
    policy_path = Path(matrix_store.__file__).parents[1] / "account_erasure_policy.json"
    policy = json.loads(policy_path.read_text())["secondary"]["kb_matrix.db"]
    with closing(sqlite3.connect(path)) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('alembic_version','_phase0_probe')"
            )
        }
        assert tables == set(policy) == set(DOMAIN_TABLES["matrix"])
        for table, columns in policy.items():
            assert [row[1] for row in db.execute(f"PRAGMA table_info({table})")] == columns
