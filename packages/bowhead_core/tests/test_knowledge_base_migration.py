"""0239 adds knowledge base containers to the files target only and points every file at its home's default."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from bowhead_core.kb import knowledge_base_store
from bowhead_core.sqlite_domains import APPENDED_COLUMNS, APPENDED_TABLES, DOMAIN_TABLES
from core_legacy_database import TARGET_FILES
from sqlalchemy import create_engine
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0239_knowledge_bases")
#: The frozen `1.0.0` baseline revision that existing deployments adopt before later revisions.
BASELINE = import_module("bowhead_core.migrations.versions.0001_initial")
NEW_OBJECTS = {
    "kb_knowledge_bases",
    "uq_kb_knowledge_bases_default",
    "idx_kb_knowledge_bases_home",
    "kb_knowledge_base_members",
    "idx_kb_knowledge_base_members_principal",
    "idx_kb_files_knowledge_base",
}
COLUMN_SQL = ", knowledge_base_id TEXT REFERENCES kb_knowledge_bases(id))"


def _connect(path: Path) -> sqlite3.Connection:
    """Raw connection with a stand-in for the runtime's 0238 retention guard (nothing is trashed)."""
    db = sqlite3.connect(path)
    db.create_function("bowhead_file_retention_unavailable_v1", 1, lambda _artifact_id: 0)
    return db


def _insert(db, file_id, scope, scope_id, workspace_id, uploaded_by, *, team="team_a", uploaded_at="2026-11-01T00:00:00+00:00", deleted=False):
    db.execute(
        "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,"
        "uploaded_at,parse_status,soft_deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            file_id,
            f"art_{file_id}",
            f"{file_id}.pdf",
            "application/pdf",
            10,
            scope,
            scope_id,
            workspace_id,
            team,
            uploaded_by,
            uploaded_at,
            "done",
            "2026-11-02T00:00:00+00:00" if deleted else None,
        ),
    )


def _seed(db) -> None:
    """116-shaped rows: personal files over several Spaces (one the legacy empty Space), soft-deleted rows, a second Team."""
    for index, workspace in enumerate(("ws_1", "ws_2", "")):
        _insert(db, f"kbf_mem_{index}", "personal", "u_mem", workspace, "u_mem", uploaded_at=f"2026-11-0{index + 1}T00:00:00+00:00")
    _insert(db, "kbf_view_1", "personal", "u_view", "ws_1", "u_view")
    _insert(db, "kbf_view_4", "personal", "u_view", "ws_4", "u_view", deleted=True)
    # An administrator moved someone else's file into their own personal scope: the owner is scope_id.
    _insert(db, "kbf_moved", "personal", "u_admin", "ws_1", "u_mem")
    _insert(db, "kbf_space_1", "space", "ws_1", "ws_1", "u_mem")
    _insert(db, "kbf_space_legacy", "workspace", "ws_1", "ws_1", "u_view")
    _insert(db, "kbf_space_2", "space", "ws_2", "ws_2", "u_mem", deleted=True)
    _insert(db, "kbf_team_live", "team", "team_a", "", "u_admin")
    _insert(db, "kbf_team_deleted", "team", "team_a", "", "u_admin", deleted=True)
    # The same person in a second Team gets a second My knowledge.
    _insert(db, "kbf_mem_b", "personal", "u_mem", "ws_b", "u_mem", team="team_b")
    _insert(db, "kbf_team_b", "team", "team_b", "", "u_mem", team="team_b")


def _schema(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as db:
        return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def _expected_homes(rows) -> dict[tuple[str, str, str], str]:
    homes = {}
    for team, scope, scope_id, uploaded_at in rows:
        home = (team, "space" if scope == "workspace" else scope, scope_id)
        homes[home] = min(uploaded_at, homes.get(home, uploaded_at))
    return homes


def _assert_backfilled(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        files = db.execute("SELECT file_id,team_id,scope,scope_id,uploaded_at,knowledge_base_id FROM kb_files").fetchall()
        bases = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT id,team_id,home,home_id,is_default,name,audience,audience_level,owner_user_id,created_at FROM kb_knowledge_bases"
            )
        }
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert files and all(row[5] for row in files)
    homes = _expected_homes([(team, scope, scope_id, uploaded) for _, team, scope, scope_id, uploaded, _ in files])
    assert len(bases) == len(homes)
    for (team, home, home_id), first_upload in homes.items():
        kb_id = knowledge_base_store.default_knowledge_base_id(team, home, home_id)
        audience, level = knowledge_base_store.DEFAULT_AUDIENCE[home]
        owner = home_id if home == "personal" else None
        assert bases[kb_id] == (team, home, home_id, 1, None, audience, level, owner, first_upload)
    for _, team, scope, scope_id, _, kb_id in files:
        assert kb_id == knowledge_base_store.default_for_scope(scope, scope_id, team)


# Core passes the 0220 split only through the domain coordinator; the split-layout test in
# `test_knowledge_base_dual_write.py` covers it with the four domain files.
@pytest.mark.parametrize("target", [target for target in TARGET_FILES if target != "core"])
def test_upgrade_adds_containers_only_to_files_and_points_every_row_at_its_default(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "files":
        with closing(_connect(path)) as db:
            _seed(db)
            db.commit()
    before = _schema(path)
    command.upgrade(config, MIGRATION.revision)
    after = _schema(path)
    command.upgrade(config, MIGRATION.revision)
    assert _schema(path) == after
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    if target != "files":
        assert after == before
        return
    assert set(after) - set(before) == NEW_OBJECTS
    # kb_files gains the column in place: no rebuild, so its 0179/0238 triggers and indexes stay.
    assert after.pop("kb_files") == before.pop("kb_files")[:-1] + COLUMN_SQL
    assert {name: sql for name, sql in after.items() if name in before} == before
    assert {"trg_account_retention_kb_delete", "trg_retention_kb_files_insert", "trg_retention_kb_files_update"} <= set(after)
    _assert_backfilled(path)
    with closing(sqlite3.connect(path)) as db:
        personal = db.execute(
            "SELECT team_id,scope_id,COUNT(DISTINCT workspace_id),COUNT(DISTINCT knowledge_base_id) FROM kb_files WHERE scope='personal' GROUP BY 1,2"
        ).fetchall()
        # One My knowledge per (Team, person), whatever Spaces the files were uploaded in.
        assert ("team_a", "u_mem", 3, 1) in personal and ("team_b", "u_mem", 1, 1) in personal
        assert all(row[3] == 1 for row in personal)
        mine = {row[0] for row in db.execute("SELECT knowledge_base_id FROM kb_files WHERE scope='personal' AND scope_id='u_mem'")}
        assert len(mine) == 2
    # The backfill is reentrant: a second pass creates and points nothing.
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:
        assert MIGRATION.backfill(db) == {"homes": 8, "created": 0, "pointed": 0}
    engine.dispose()


def test_empty_database_and_the_1_0_0_baseline_both_reach_the_revision(tmp_path):
    for name, start in (("empty.db", None), ("baseline.db", BASELINE.revision)):
        path = tmp_path / name
        config = _config(path, "files")
        if start is not None:
            command.upgrade(config, start)
            # 1.0.0 rows predate the retention triggers and every later column.
            with closing(sqlite3.connect(path)) as db:
                _seed(db)
                db.commit()
        command.upgrade(config, MIGRATION.revision)
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
            assert set(_schema(path)) >= NEW_OBJECTS
        if start is None:
            with closing(sqlite3.connect(path)) as db:
                assert db.execute("SELECT COUNT(*) FROM kb_knowledge_bases").fetchone() == (0,)
        else:
            _assert_backfilled(path)


def test_failed_installation_leaves_no_containers_or_column(tmp_path, monkeypatch):
    path = tmp_path / "kb_files.db"
    command.upgrade(_config(path, "files"), MIGRATION.down_revision)
    with closing(_connect(path)) as db:
        _seed(db)
        db.commit()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:

        class Interrupted:
            def exec_driver_sql(self, sql, *args):
                if sql.startswith("UPDATE kb_files SET knowledge_base_id"):
                    raise RuntimeError("backfill interrupted")
                return db.exec_driver_sql(sql, *args)

        monkeypatch.setattr(
            MIGRATION, "context", SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": "files"}), get_bind=lambda: Interrupted())
        )
        with pytest.raises(RuntimeError, match="backfill interrupted"):
            MIGRATION.upgrade()
        assert db.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '%knowledge_base%'").fetchall() == []
        assert "knowledge_base_id" not in {row[1] for row in db.exec_driver_sql("PRAGMA table_info(kb_files)")}
        assert db.exec_driver_sql("SELECT COUNT(*) FROM kb_files").scalar() == 13
    engine.dispose()


def test_backfill_refuses_a_row_left_in_another_home(tmp_path):
    path = tmp_path / "kb_files.db"
    config = _config(path, "files")
    command.upgrade(config, MIGRATION.down_revision)
    with closing(_connect(path)) as db:
        _seed(db)
        db.commit()
    command.upgrade(config, MIGRATION.revision)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:
        wrong = knowledge_base_store.default_knowledge_base_id("team_a", "space", "ws_2")
        db.exec_driver_sql("UPDATE kb_files SET knowledge_base_id=? WHERE file_id='kbf_space_1'", (wrong,))
        with pytest.raises(RuntimeError, match="0 knowledge files without a knowledge base and 1 pointing at another home"):
            MIGRATION.backfill(db)
        db.rollback()
    engine.dispose()


def test_default_ids_are_deterministic_and_shared_with_the_runtime():
    expected = "kbase_" + hashlib.sha256(b"default|team_a|space|ws_1").hexdigest()[:24]
    assert knowledge_base_store.default_knowledge_base_id("team_a", "space", "ws_1") == expected
    for args in (("team_a", "personal", "u_1"), ("team_a", "space", ""), ("", "team", "team_a")):
        assert MIGRATION.default_knowledge_base_id(*args) == knowledge_base_store.default_knowledge_base_id(*args)
    assert MIGRATION.DEFAULT_AUDIENCE == knowledge_base_store.DEFAULT_AUDIENCE
    assert knowledge_base_store.default_for_scope("workspace", "ws_1", "team_a") == expected
    # My knowledge is one per (Team, person); a person in two Teams has two.
    assert knowledge_base_store.default_for_scope("personal", "u_1", "team_a") != knowledge_base_store.default_for_scope("personal", "u_1", "team_b")
    with pytest.raises(ValueError, match="Unknown knowledge file scope"):
        knowledge_base_store.default_for_scope("org", "team_a", "team_a")


def test_registries_and_the_erasure_policy_match_the_migrated_schema(tmp_path):
    for table in ("kb_knowledge_bases", "kb_knowledge_base_members"):
        assert APPENDED_TABLES[table] == ("files", MIGRATION.revision)
        assert table in DOMAIN_TABLES["files"]
    assert APPENDED_COLUMNS["kb_files"] == (("knowledge_base_id",), MIGRATION.revision)
    path = tmp_path / "kb_files.db"
    command.upgrade(_config(path, "files"), MIGRATION.revision)
    policy_path = Path(knowledge_base_store.__file__).parents[1] / "account_erasure_policy.json"
    policy = json.loads(policy_path.read_text())["secondary"]["kb_files.db"]
    with closing(sqlite3.connect(path)) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('alembic_version','_phase0_probe')"
            )
        }
        assert tables == set(policy)
        for table, columns in policy.items():
            assert [row[1] for row in db.execute(f"PRAGMA table_info({table})")] == columns
