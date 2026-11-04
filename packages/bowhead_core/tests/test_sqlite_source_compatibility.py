"""Reviewed historical init shapes migrate without losing archived maintenance data."""

from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from bowhead_core.sqlite_domain_upgrade import (
    SOURCE_REVISION,
    SPLIT_REVISION,
    _copy_table,
    _source_identity,
    _source_schema_digest,
    run_domain_upgrade,
    schema_digest,
    schema_rows,
    table_evidence,
)
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, ORIGINAL_TARGET_FILES
from bowhead_core.sqlite_source_compatibility import canonical_source_rows
from bowhead_core.sqlite_upgrade import LAYOUT_FILE, SQLiteUpgradeError, database_head, run_legacy_upgrade

from bowhead_core import migrations

_METADATA = json.loads((Path(migrations.__file__).parent.parent / "sqlite_source_0219_compatibility.json").read_text())
PREVIOUS = importlib.import_module("bowhead_core.migrations.versions.0217_probe_diagnostics").down_revision


def _revision(name, path, revision, *, cleanup=False):
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = name
    config.attributes["bowhead_domain_coordinator"] = revision == SPLIT_REVISION
    config.attributes["bowhead_split_cleanup"] = cleanup
    command.upgrade(config, revision)


def _run(directory, *, fault=lambda phase: None):
    return run_domain_upgrade(directory, lambda name, path, cleanup: _revision(name, path, SPLIT_REVISION, cleanup=cleanup), fault=fault)


def _historical_core(path):
    variants = {(entry["source"][0], entry["source"][1]): tuple(entry["source"]) for entry in _METADATA["variants"]}
    temporary = path.with_name("historical-core.db")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as target:
        target.execute("PRAGMA foreign_keys=OFF")
        rows = [variants.get((row[0], row[1]), row) for row in schema_rows(source)]
        rows += [tuple(row) for row in _METADATA["retired_objects"]]
        for kind, name, _, sql in rows:
            if kind == "table" and not name.startswith("canvas_fts_"):
                target.execute(sql)
        for kind, name, _, _ in schema_rows(source):
            if kind == "table" and not name.startswith("canvas_fts_"):
                _copy_table(source, target, name)
        target.execute(
            "INSERT INTO citation_migration_backup(rowid,section_id,rev,old_payload,migrated_at) VALUES(41,'section_old',3,?, 't')",
            ('{ "old": "[num:18]" }',),
        )
        target.execute(
            "INSERT INTO citation_migration_backup_revisions(rowid,revision_id,old_prev_payload,old_new_payload,migrated_at) VALUES(47,'revision_old',NULL,?,'t')",
            (b"original bytes",),
        )
        target.execute(
            "INSERT INTO citation_migration_backup_turns(rowid,turn_id,old_final_answer,migrated_at) VALUES(59,'turn_old',?,'t')",
            ("Historical answer with [ref:old].",),
        )
        target.execute("INSERT INTO citation_migration_meta(rowid,key,value) VALUES(61,'legacy_cutoff','2026-07-15T00:00:00Z')")
        target.execute(
            "INSERT INTO memory_candidates(rowid,id,user_id,kind,key,value_json,created_at,updated_at,memory_type,supersedes_json,origin) VALUES(73,'candidate_old','user_old','preference','retained','{ \"kept\": true }','t','t','procedural','[\"original\"]','feedback_draft')"
        )
        target.execute(
            "INSERT INTO skill_instances(rowid,id,user_id,name,source_type,source_path,created_at,updated_at,verdict,import_status,import_report_json) VALUES(79,'skill_old','user_old','method','user_upload','original/path','t','t','pass','blocked','{ \"kept\": true }')"
        )
        target.execute(
            "INSERT INTO approvals(rowid,id,turn_id,workspace_id,member_uid,user_id,tool_name,arguments_json,created_at,updated_at,request_key,arguments_digest,datarc_workspace_id) VALUES(83,'approval_old','turn_old','ws_old','member_old','user_old','old_tool','{}','t','t','kept request','kept digest','project_old')"
        )
        target.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source,state,lease_epoch) "
            "VALUES('inventory_user','inventory_operation','2026-01-01','2026-03-01','request','purging',3)"
        )
        target.executemany(
            "INSERT INTO account_deletion_rows VALUES('inventory_operation',?,?)",
            [(row[1], '["original"]') for row in _METADATA["retired_objects"] if row[0] == "table"]
            + [("skill_instances", '["skill_old"]'), ("memory_candidates", '["candidate_old"]')],
        )
        for kind, _, _, sql in rows:
            if kind in {"index", "view", "trigger"}:
                target.execute(sql)
        target.commit()
    temporary.replace(path)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("historical-source") / "data"
    directory = root / "sqlite"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    revisions = frozenset(revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions())
    run_legacy_upgrade(directory, tuple(ORIGINAL_TARGET_FILES), PREVIOUS, revisions, lambda name, path: _revision(name, path, PREVIOUS))
    _historical_core(directory / "bowhead.db")
    run_legacy_upgrade(directory, tuple(ORIGINAL_TARGET_FILES), SOURCE_REVISION, revisions, lambda name, path: _revision(name, path, SOURCE_REVISION))
    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        assert _source_identity(source) == _source_schema_digest()
        assert schema_digest(source) != _source_schema_digest()
    return root


@pytest.fixture
def directory(baseline, tmp_path):
    root = tmp_path / "data"
    shutil.copytree(baseline, root)
    directory = root / "sqlite"
    path = directory / LAYOUT_FILE
    state = json.loads(path.read_text())
    state["backup"] = str(root / Path(state["backup"]).relative_to(baseline))
    path.write_text(json.dumps(state))
    return directory


def _state(directory):
    return json.loads((directory / LAYOUT_FILE).read_text())


def _archived_evidence(directory):
    archive = Path(_state(directory)["backup"]) / "sqlite/bowhead.db"
    assert database_head(archive) == PREVIOUS
    with closing(sqlite3.connect(archive)) as source:
        return {table: table_evidence(source, table) for table in _METADATA["archived_tables"]}


def _assert_complete(directory, archived):
    state = _state(directory)
    assert state["status"] == "ready" and state["phase"] == "complete"
    assert "legacy_retirement_evidence" not in state
    assert _archived_evidence(directory) == archived
    for filename in DOMAIN_TARGET_FILES.values():
        assert database_head(directory / filename) == SPLIT_REVISION
        with closing(sqlite3.connect(directory / filename)) as source:
            objects = {(row[0], row[1]) for row in schema_rows(source)}
            assert not objects.intersection((row[0], row[1]) for row in _METADATA["retired_objects"])
    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        assert source.execute("SELECT rowid,memory_type,supersedes_json,origin,value_json FROM memory_candidates").fetchall() == [
            (73, "procedural", '["original"]', "feedback_draft", '{ "kept": true }')
        ]
        assert source.execute("SELECT rowid,verdict,import_status,import_report_json FROM skill_instances").fetchall() == [
            (79, "pass", "blocked", '{ "kept": true }')
        ]
        assert source.execute("SELECT table_name,row_key FROM account_deletion_rows ORDER BY table_name").fetchall() == [
            ("memory_candidates", '["candidate_old"]'),
            ("skill_instances", '["skill_old"]'),
        ]
        assert source.execute("SELECT state,lease_epoch FROM account_deletions WHERE operation_id='inventory_operation'").fetchone() == ("purging", 3)
    with closing(sqlite3.connect(directory / "bowhead_execution.db")) as source:
        assert source.execute("SELECT rowid,request_key,arguments_digest,datarc_workspace_id FROM approvals").fetchall() == [
            (83, "kept request", "kept digest", "project_old")
        ]


def test_reviewed_historical_schema_and_original_sidecar_archive_migrate_exactly(directory):
    archived = _archived_evidence(directory)
    assert all(value["rows"] == 1 for value in archived.values())
    _run(directory)
    _assert_complete(directory, archived)


def test_runtime_schema_and_default_evidence_keep_the_original_raw_identity():
    from bowhead_core.sqlite_schema_identity import static_schema_digest

    variants = {entry["source"][1]: entry for entry in _METADATA["variants"] if entry["source"][0] == "table"}
    with closing(sqlite3.connect(":memory:")) as source:
        source.execute(variants["teams"]["source"][3])
        source.execute(variants["memory_candidates"]["source"][3])
        source.execute(
            "INSERT INTO memory_candidates(id,user_id,kind,key,value_json,created_at,updated_at,memory_type,origin) "
            "VALUES('candidate','user','preference','old','{}','t','t','procedural','feedback_draft')"
        )
        assert schema_digest(source) == static_schema_digest(schema_rows(source))
        assert schema_digest(source) != _source_identity(source)
        assert table_evidence(source, "memory_candidates") != table_evidence(source, "memory_candidates", canonical_source=True)


@pytest.mark.parametrize("variant", [entry for entry in _METADATA["variants"] if entry["source"][0] == "table"], ids=lambda entry: entry["source"][1])
def test_reviewed_table_variants_keep_columns_defaults_keys_and_foreign_keys(variant):
    table = variant["source"][1]
    with closing(sqlite3.connect(":memory:")) as legacy, closing(sqlite3.connect(":memory:")) as canonical:
        legacy.execute(variant["source"][3])
        canonical.execute(variant["canonical"][3])
        legacy_columns = {row[1]: tuple(row[2:]) for row in legacy.execute(f'PRAGMA table_xinfo("{table}")')}
        canonical_columns = {row[1]: tuple(row[2:]) for row in canonical.execute(f'PRAGMA table_xinfo("{table}")')}
        assert legacy_columns == canonical_columns
        assert variant["columns"] == [row[1] for row in canonical.execute(f'PRAGMA table_xinfo("{table}")') if row[6] == 0]
        assert {tuple(row[1:]) for row in legacy.execute(f'PRAGMA foreign_key_list("{table}")')} == {
            tuple(row[1:]) for row in canonical.execute(f'PRAGMA foreign_key_list("{table}")')
        }

        def keys(connection):
            return {
                (row[2], row[3], row[4], tuple(column[2] for column in connection.execute(f'PRAGMA index_info("{row[1]}")')))
                for row in connection.execute(f'PRAGMA index_list("{table}")')
            }

        assert keys(legacy) == keys(canonical)


@pytest.mark.parametrize("phase", ["after_commit:content", "all_domains_verified", "after_cleanup:core", "after_vacuum"])
def test_historical_retirement_recovers_each_committed_boundary_without_losing_archives(directory, phase):
    archived = _archived_evidence(directory)

    def interruption(actual):
        if actual == phase:
            raise RuntimeError("injected historical migration interruption")

    with pytest.raises(RuntimeError, match="historical migration interruption"):
        _run(directory, fault=interruption)
    assert _archived_evidence(directory) == archived
    assert set(_state(directory)["legacy_retirement_evidence"]) == {row[1] for row in _METADATA["retired_objects"] if row[0] == "table"}
    _run(directory)
    _assert_complete(directory, archived)


def test_relative_database_and_original_backup_paths_keep_same_retirement_proof(directory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state(directory)
    state["backup"] = str(Path(state["backup"]).relative_to(tmp_path))
    (directory / LAYOUT_FILE).write_text(json.dumps(state))
    relative = Path("data/sqlite")
    archived = _archived_evidence(relative)
    _run(relative)
    _assert_complete(relative, archived)


@pytest.mark.parametrize("table", ["messages", "skill_observations"])
def test_nonempty_legacy_reader_data_stops_before_any_retirement(directory, table):
    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        if table == "messages":
            source.execute("INSERT INTO messages VALUES('message_old','turn_old','user','retain original message','t')")
        else:
            source.execute("INSERT INTO skill_observations VALUES('observation_old','turn_old','intent','chain','sections','signature','{}','t')")
        source.commit()
    with pytest.raises(SQLiteUpgradeError, match=f"Historical table {table} contains retained data"):
        _run(directory)
    assert database_head(directory / "bowhead.db") == SOURCE_REVISION
    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        assert source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (1,)
        assert source.execute("SELECT COUNT(*) FROM citation_migration_backup").fetchone() == (1,)


def test_changed_original_sidecar_archive_never_admits_drop(directory):
    archive = Path(_state(directory)["backup"]) / "sqlite/bowhead.db"
    with closing(sqlite3.connect(archive)) as source:
        source.execute("UPDATE citation_migration_backup SET old_payload='changed archive'")
        source.commit()
    with pytest.raises(SQLiteUpgradeError, match="does not preserve historical table citation_migration_backup"):
        _run(directory)
    assert database_head(directory / "bowhead.db") == SOURCE_REVISION
    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        assert source.execute("SELECT old_payload FROM citation_migration_backup").fetchone() == ('{ "old": "[num:18]" }',)


def test_cleanup_failure_rolls_back_all_retired_objects_and_payloads(directory, monkeypatch):
    from bowhead_core import sqlite_domain_upgrade

    archived = _archived_evidence(directory)
    original = sqlite_domain_upgrade.cleanup_core

    def fail(source):
        original(source)
        raise RuntimeError("before historical cleanup commit")

    monkeypatch.setattr(sqlite_domain_upgrade, "cleanup_core", fail)
    with pytest.raises(RuntimeError, match="before historical cleanup commit"):
        _run(directory)
    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        assert {table: table_evidence(source, table) for table in archived} == archived
        assert all(source.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (row[1],)).fetchone() for row in _METADATA["retired_objects"])
        assert source.execute("SELECT COUNT(*) FROM account_deletion_rows").fetchone() == (8,)
    assert database_head(directory / "bowhead.db") == SOURCE_REVISION
    monkeypatch.setattr(sqlite_domain_upgrade, "cleanup_core", original)
    _run(directory)
    _assert_complete(directory, archived)


@pytest.mark.parametrize("drift", ["unknown_column", "unique_legacy_index", "unknown_trigger", "changed_check"])
def test_reviewed_variant_registration_does_not_admit_unknown_drift(directory, drift):
    from bowhead_core.sqlite_schema_identity import static_schema_digest

    with closing(sqlite3.connect(directory / "bowhead.db")) as source:
        if drift == "unknown_column":
            source.execute("ALTER TABLE memory_candidates ADD COLUMN unreviewed TEXT")
        elif drift == "unique_legacy_index":
            source.execute("DROP INDEX idx_orgs_domain")
            source.execute("CREATE UNIQUE INDEX idx_orgs_domain ON teams(domain)")
        elif drift == "unknown_trigger":
            source.execute("CREATE TRIGGER unreviewed BEFORE INSERT ON messages BEGIN SELECT 1; END")
        else:
            rows = schema_rows(source)
            changed = [
                (kind, name, owner, sql.replace("'credit_deduct_failed'", "'credit_deduct_failed','unreviewed'"))
                if name == "credit_exhaustion_events"
                else (kind, name, owner, sql)
                for kind, name, owner, sql in rows
            ]
            assert static_schema_digest(canonical_source_rows(changed)) != _source_schema_digest()
            return
        source.commit()
    with pytest.raises(SQLiteUpgradeError, match="source schema differs"):
        _run(directory)
    assert not (directory / "bowhead_execution.db").exists()
