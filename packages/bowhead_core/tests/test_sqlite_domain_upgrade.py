"""Whole-domain imports preserve data and resume before exposing the new layout."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.sqlite_domain_runtime import DomainConnection, guard_connection
from bowhead_core.sqlite_domain_upgrade import (
    SOURCE_REVISION,
    SPLIT_REVISION,
    domain_table_sql,
    run_domain_upgrade,
    table_evidence,
)
from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, DOMAIN_TABLES, DOMAIN_TARGET_FILES, ORIGINAL_TARGET_FILES
from bowhead_core.sqlite_upgrade import LAYOUT_FILE, SQLiteUpgradeError, database_head, run_legacy_upgrade, runtime_shared_lease

from bowhead_core import migrations


def _advance(name: str, path: Path, cleanup: bool) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = name
    config.attributes["bowhead_split_cleanup"] = cleanup
    config.attributes["bowhead_domain_coordinator"] = True
    command.upgrade(config, SPLIT_REVISION)


@pytest.fixture(scope="module")
def legacy_source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("split-source") / "sqlite"

    def source_revision(name: str, path: Path) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).parent))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        config.attributes["bowhead_target"] = name
        command.upgrade(config, SOURCE_REVISION)

    run_legacy_upgrade(directory, tuple(ORIGINAL_TARGET_FILES), SOURCE_REVISION, frozenset({SOURCE_REVISION}), source_revision)
    with sqlite3.connect(directory / "bowhead.db") as source:
        source.execute(
            "INSERT INTO notification_items(id,team_id,category,type,title,body,dedupe_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("n-retained", "team-orphan", "system", "test", "Retained", "bytes and JSON remain unchanged", "unique", "2026-01-02", "2026-01-02"),
        )
        source.execute(
            "INSERT INTO notification_recipients(notification_id,user_id,delivered_at,read_at) VALUES (?,?,?,?)",
            ("n-retained", "orphan-user", "2026-01-02", "2026-01-03"),
        )
        source.execute(
            "INSERT INTO metrics_event_log(rowid,id,name,attrs_json,created_at) VALUES (?,?,?,?,?)",
            (91, "metric-original", "kept", '{ "z": 1, "a": null }', "2026-01-02"),
        )
        source.execute(
            "INSERT INTO canvas_fts(rowid,canvas_id,section_id,body) VALUES (?,?,?,?)", (27, "c-source", "s-source", "retained searchable document")
        )
        source.executemany("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", [("canvas_export_job_events", 123), ("ref_audit_log", 801)])
        source.executemany(
            "INSERT INTO terminal_auth_scope_epochs(rowid,scope_kind,team_id,scope_ref,epoch,row_version,updated_at) VALUES (?,?,?,?,?,?,?)",
            [
                (11, "session_visibility", "team-orphan", "session-original", 7, 3, "2026-01-02"),
                (17, "user_account", "__global__", "user-original", 12, 8, "2026-01-02"),
                (31, "team_membership", "team-orphan", "member-original", 9, 5, "2026-01-02"),
            ],
        )
    return directory


@pytest.fixture
def directory(legacy_source, tmp_path):
    target = tmp_path / "sqlite"
    shutil.copytree(legacy_source, target)
    return target


def _state(directory: Path) -> dict:
    return json.loads((directory / LAYOUT_FILE).read_text())


def _fail_at(expected: str):
    def failure(phase: str) -> None:
        if phase == expected:
            raise RuntimeError("injected interruption " + phase)

    return failure


def test_cross_domain_fk_removal_retains_local_constraints_and_comments() -> None:
    sql = "CREATE TABLE turn_attachments (turn_id TEXT REFERENCES turns(id), artifact_id TEXT REFERENCES artifacts(id) ON DELETE CASCADE, seq INTEGER CHECK(seq > 0), PRIMARY KEY(turn_id, artifact_id))"
    changed = domain_table_sql("turn_attachments", sql)
    assert "REFERENCES turns(id)" in changed
    assert "REFERENCES artifacts" not in changed
    assert "CHECK(seq > 0)" in changed
    assert "PRIMARY KEY(turn_id, artifact_id)" in changed


def test_typed_evidence_distinguishes_storage_type_and_row_identity() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE sample(value)")
        connection.execute("INSERT INTO sample(rowid,value) VALUES (7,?)", (b"1",))
        first = table_evidence(connection, "sample")
        connection.execute("UPDATE sample SET value=?", ("1",))
        second = table_evidence(connection, "sample")
        connection.execute("UPDATE sample SET rowid=8")
        assert first != second != table_evidence(connection, "sample")


def test_populated_layout_migrates_exactly_and_normal_restart_keeps_live_changes(directory, monkeypatch) -> None:
    from bowhead_core.sqlite_schema_identity import static_schema_digest

    from bowhead_core import sqlite_domain_upgrade

    assert run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION) == tuple(DOMAIN_TARGET_FILES)
    state = _state(directory)
    assert state["layout"] == DOMAIN_LAYOUT
    assert state["status"] == "ready"
    for domain, filename in DOMAIN_TARGET_FILES.items():
        assert database_head(directory / filename) == SPLIT_REVISION
        assert len(state["targets"][domain]["schema_digest"]) == 64
        with sqlite3.connect(directory / filename) as connection:
            actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            assert actual - {"alembic_version", "_phase0_probe"} == set(DOMAIN_TABLES[domain])
            assert sqlite_domain_upgrade.schema_digest(connection) == state["targets"][domain]["schema_digest"]
            assert sqlite_domain_upgrade.schema_digest(connection) == static_schema_digest(sqlite_domain_upgrade.schema_rows(connection))
    with sqlite3.connect(directory / "bowhead_observability.db") as connection:
        assert connection.execute("SELECT rowid,attrs_json FROM metrics_event_log").fetchall() == [(91, '{ "z": 1, "a": null }')]
        connection.execute("DELETE FROM metrics_event_log")
    with sqlite3.connect(directory / "bowhead_content.db") as connection:
        assert connection.execute("SELECT rowid FROM canvas_fts WHERE canvas_fts MATCH 'searchable'").fetchall() == [(27,)]
        assert connection.execute("SELECT seq FROM sqlite_sequence WHERE name='canvas_export_job_events'").fetchone() == (123,)
    with sqlite3.connect(directory / "bowhead_execution.db") as connection:
        assert connection.execute("SELECT rowid,scope_kind,epoch,row_version FROM terminal_auth_scope_epochs").fetchall() == [
            (11, "session_visibility", 7, 3)
        ]
    with sqlite3.connect(directory / "bowhead.db", factory=DomainConnection, uri=True) as connection:
        guard_connection(connection, directory / "bowhead.db", "core")
        assert connection.execute("SELECT rowid,scope_kind,epoch,row_version FROM terminal_authority_scope_epochs ORDER BY rowid").fetchall() == [
            (17, "user_account", 12, 8),
            (31, "team_membership", 9, 5),
        ]
        assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name='metrics_event_log'").fetchone() is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT seq FROM sqlite_sequence WHERE name='ref_audit_log'").fetchone() == (801,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE terminal_authority_scope_epochs SET epoch=1")
    original = (directory / LAYOUT_FILE).read_bytes()
    monkeypatch.setattr(sqlite_domain_upgrade, "backup_before_upgrade", lambda *_: pytest.fail("A ready layout must not create another backup"))
    assert run_domain_upgrade(
        directory, lambda *_: pytest.fail("A ready restart must not replay schema revisions"), target_head=SPLIT_REVISION
    ) == tuple(DOMAIN_TARGET_FILES)
    assert (directory / LAYOUT_FILE).read_bytes() == original
    with runtime_shared_lease(directory, expected_head=SPLIT_REVISION):
        pass


@pytest.mark.parametrize("phase", ["before_commit:execution", "after_commit:content", "all_domains_verified", "after_cleanup:core", "after_vacuum"])
def test_interrupted_stages_resume_forward_without_losing_data(directory, phase) -> None:
    with pytest.raises(RuntimeError, match="injected interruption"):
        run_domain_upgrade(directory, _advance, fault=_fail_at(phase), target_head=SPLIT_REVISION)
    checkpoint = _state(directory)
    assert checkpoint["status"] == "in_progress"
    if phase.startswith(("before_commit", "after_commit")) or phase == "all_domains_verified":
        with sqlite3.connect(directory / "bowhead.db") as source:
            assert source.execute("SELECT COUNT(*) FROM notification_items").fetchone() == (1,)
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert _state(directory)["status"] == "ready"
    with sqlite3.connect(directory / "bowhead_notifications.db") as target:
        assert target.execute("SELECT notification_id,user_id,read_at FROM notification_recipients").fetchall() == [
            ("n-retained", "orphan-user", "2026-01-03")
        ]
    assert _state(directory)["backup"] == checkpoint["backup"]


def test_killed_domain_copy_recovers_its_hot_journal_and_reimports(directory) -> None:
    with pytest.raises(RuntimeError, match="injected interruption"):
        run_domain_upgrade(directory, _advance, fault=_fail_at("before_commit:execution"), target_head=SPLIT_REVISION)
    path = directory / DOMAIN_TARGET_FILES["execution"]
    path.unlink()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys\n"
            "from bowhead_core.sqlite_domain_upgrade import create_and_copy_domain\n"
            "from pathlib import Path\n"
            "original=sqlite3.connect\n"
            "def small_cache(*args,**kwargs):\n"
            " db=original(*args,**kwargs); db.execute('PRAGMA cache_size=1'); return db\n"
            "sqlite3.connect=small_cache\n"
            "source=original(sys.argv[1])\n"
            "create_and_copy_domain(source,Path(sys.argv[2]),'execution',before_commit=lambda:os._exit(0))\n",
            str(directory / "bowhead.db"),
            str(path),
        ],
        check=True,
    )
    assert path.with_name(path.name + "-journal").is_file()
    with pytest.raises(SQLiteUpgradeError, match="Cannot read migration ledger"):
        database_head(path)
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert _state(directory)["status"] == "ready"
    with sqlite3.connect(directory / DOMAIN_TARGET_FILES["notifications"]) as connection:
        assert connection.execute("SELECT id FROM notification_items").fetchall() == [("n-retained",)]


def test_corrupt_committed_copy_is_rejected_before_source_cleanup(directory) -> None:
    with pytest.raises(RuntimeError):
        run_domain_upgrade(directory, _advance, fault=_fail_at("after_commit:notifications"), target_head=SPLIT_REVISION)
    with sqlite3.connect(directory / "bowhead_notifications.db") as target:
        target.execute("DELETE FROM notification_recipients")
    with pytest.raises(SQLiteUpgradeError, match="data changed"):
        run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    with sqlite3.connect(directory / "bowhead.db") as source:
        assert source.execute("SELECT COUNT(*) FROM notification_recipients").fetchone() == (1,)
        assert database_head(directory / "bowhead.db") == SOURCE_REVISION


def test_unknown_source_schema_is_rejected_before_creating_domain_files(directory) -> None:
    with sqlite3.connect(directory / "bowhead.db") as source:
        source.execute("CREATE TABLE unreviewed_extension(value)")
    with pytest.raises(SQLiteUpgradeError, match="schema differs"):
        run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert not (directory / "bowhead_execution.db").exists()


def test_missing_committed_target_stops_recovery_without_empty_replacement(directory) -> None:
    with pytest.raises(RuntimeError):
        run_domain_upgrade(directory, _advance, fault=_fail_at("all_domains_verified"), target_head=SPLIT_REVISION)
    missing = directory / "bowhead_notifications.db"
    missing.unlink()
    with pytest.raises(SQLiteUpgradeError, match="missing"):
        run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert not missing.exists()


def test_cleanup_failure_rolls_back_source_ddl_data_and_ledger(directory, monkeypatch) -> None:
    from bowhead_core import sqlite_domain_upgrade

    original = sqlite_domain_upgrade.cleanup_core

    def interrupted(connection):
        original(connection)
        raise RuntimeError("injected failure before cleanup revision commit")

    monkeypatch.setattr(sqlite_domain_upgrade, "cleanup_core", interrupted)
    with pytest.raises(RuntimeError, match="before cleanup revision commit"):
        run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert database_head(directory / "bowhead.db") == SOURCE_REVISION
    with sqlite3.connect(directory / "bowhead.db") as source:
        assert source.execute("SELECT COUNT(*) FROM notification_items").fetchone() == (1,)
        assert source.execute("SELECT COUNT(*) FROM terminal_auth_scope_epochs").fetchone() == (3,)
        assert source.execute("SELECT 1 FROM sqlite_schema WHERE name='terminal_authority_scope_epochs'").fetchone() is None
    monkeypatch.setattr(sqlite_domain_upgrade, "cleanup_core", original)
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert _state(directory)["status"] == "ready"


def test_fresh_cli_initialization_and_restart_keep_domain_layout(tmp_path) -> None:
    from bowhead_cli.migrate import upgrade

    directory = tmp_path / "sqlite"
    url = f"sqlite:///{directory / 'bowhead.db'}"
    assert upgrade(url) == tuple(DOMAIN_TARGET_FILES)
    state = _state(directory)
    assert state["layout"] == DOMAIN_LAYOUT
    assert state["phase"] == "complete"
    assert all(database_head(directory / filename) == state["target_head"] for filename in DOMAIN_TARGET_FILES.values())
    assert upgrade(url) == tuple(DOMAIN_TARGET_FILES)
    assert _state(directory) == state
    with runtime_shared_lease(directory), pytest.raises(SQLiteUpgradeError, match="already owns this data directory"):
        upgrade(url)


def test_incomplete_import_evidence_cannot_admit_source_cleanup(directory) -> None:
    with pytest.raises(RuntimeError):
        run_domain_upgrade(directory, _advance, fault=_fail_at("all_domains_verified"), target_head=SPLIT_REVISION)
    state = _state(directory)
    del state["targets"]["notifications"]["evidence"]["notification_recipients"]
    (directory / LAYOUT_FILE).write_text(json.dumps(state))
    with pytest.raises(SQLiteUpgradeError, match="complete import evidence"):
        run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    assert database_head(directory / "bowhead.db") == SOURCE_REVISION
    with sqlite3.connect(directory / "bowhead.db") as source:
        assert source.execute("SELECT COUNT(*) FROM notification_recipients").fetchone() == (1,)


def test_final_wal_files_allow_execution_and_content_writes_while_read_snapshots_remain_open(directory):
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    for domain in ("core", "execution", "content", "notifications", "observability"):
        with sqlite3.connect(directory / DOMAIN_TARGET_FILES[domain]) as connection:
            assert connection.execute("PRAGMA main.journal_mode").fetchone() == ("wal",)

    def managed(domain):
        connection = sqlite3.connect(directory / DOMAIN_TARGET_FILES[domain], factory=DomainConnection, uri=True, timeout=0.05)
        guard_connection(connection, directory / "bowhead.db", domain)
        return connection

    with closing(managed("core")) as aggregator, closing(managed("execution")) as execution_reader, closing(managed("content")) as content_reader:
        for reader, table in (
            (aggregator, "terminal_auth_scope_epochs"),
            (execution_reader, "terminal_auth_scope_epochs"),
            (content_reader, "canvas_fts"),
        ):
            reader.execute("BEGIN")
            with reader.execute(f'SELECT COUNT(*) FROM "{table}"') as cursor:
                cursor.fetchall()
        # Startup probes are independent writes in these same physical files;
        # rollback journaling would block COMMIT behind the open read snapshots.
        for domain in ("execution", "content"):
            with closing(managed(domain)) as writer:
                writer.execute("INSERT INTO _phase0_probe(k,ts) VALUES (?,?)", ("concurrent-write", 1.0))
                writer.commit()
        assert aggregator.in_transaction and execution_reader.in_transaction and content_reader.in_transaction
        for reader in (aggregator, execution_reader, content_reader):
            reader.rollback()


def test_ready_archive_restore_reenables_wal_without_reimport_or_layout_changes(directory):
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    recorded = (directory / LAYOUT_FILE).read_bytes()
    for domain in ("core", "execution", "content", "notifications", "observability"):
        with closing(sqlite3.connect(directory / DOMAIN_TARGET_FILES[domain])) as connection:
            assert connection.execute("PRAGMA main.journal_mode=DELETE").fetchone() == ("delete",)
    run_domain_upgrade(directory, lambda *_: pytest.fail("A valid archive restore must not reimport data"), target_head=SPLIT_REVISION)
    assert (directory / LAYOUT_FILE).read_bytes() == recorded
    for domain in ("core", "execution", "content", "notifications", "observability"):
        with closing(sqlite3.connect(directory / DOMAIN_TARGET_FILES[domain])) as connection:
            assert connection.execute("PRAGMA main.journal_mode").fetchone() == ("wal",)


def test_ready_schema_drift_is_rejected_before_repairing_archive_journal_mode(directory):
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    with closing(sqlite3.connect(directory / "bowhead_content.db")) as connection:
        connection.execute("PRAGMA main.journal_mode=DELETE")
        connection.execute("CREATE TABLE unknown_drift(value)")
    with pytest.raises(SQLiteUpgradeError, match="schema verification failed"):
        run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    with closing(sqlite3.connect(directory / "bowhead_content.db")) as connection:
        assert connection.execute("PRAGMA main.journal_mode").fetchone() == ("delete",)
