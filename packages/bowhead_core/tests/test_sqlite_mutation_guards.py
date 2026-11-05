"""Ordinary concurrent writes coexist with snapshots; destructive races stay fenced."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import ExitStack, closing

import pytest
from bowhead_core.sqlite_domain_runtime import _AUTHORITY, DomainConnection, guard_connection
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.sqlite_mutation_guards import mutation_triggers
from test_sqlite_domain_runtime import domain_files as domain_files


@pytest.fixture
def mutation_files(domain_files):
    for domain, filename in DOMAIN_TARGET_FILES.items():
        with closing(sqlite3.connect(domain_files.with_name(filename))) as db:
            if domain == "execution":
                db.execute("CREATE TABLE document_executions(job_id TEXT PRIMARY KEY,state TEXT NOT NULL,draft_json TEXT)")
                db.execute("CREATE TABLE document_draft_files(job_id TEXT NOT NULL,artifact_id TEXT NOT NULL)")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND name GLOB 'trg_domain_authority_*'").fetchall():
                db.execute(f'DROP TRIGGER "{name}"')
            for table, columns in _AUTHORITY.get(domain, {}).items():
                if table not in tables:
                    continue
                present = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                for column in columns:
                    if column not in present:
                        db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" TEXT')
            for statement in mutation_triggers(domain):
                if any(f' ON "{table}"' in statement for table in tables):
                    db.execute(statement)
            if domain == "execution":
                migration = importlib.import_module("bowhead_core.migrations.versions.0223_document_no_draft_state")
                db.execute("DROP TRIGGER trg_domain_mutation_document_executions_update")
                db.execute(migration.DOCUMENT_EXECUTION_UPDATE_TRIGGER)
            if domain == "content":
                migration = importlib.import_module("bowhead_core.migrations.versions.0222_artifact_producer_metadata")
                db.execute('DROP TRIGGER "trg_domain_mutation_artifacts_update"')
                db.execute(migration.ARTIFACT_UPDATE_TRIGGER)
            db.commit()
    return domain_files


def managed(path, domain):
    db = sqlite3.connect(path.with_name(DOMAIN_TARGET_FILES[domain]), factory=DomainConnection, timeout=0, uri=True)
    guard_connection(db, path, domain)
    return closing(db)


def test_sessions_and_turn_completion_progress_while_foreign_snapshot_is_open(mutation_files):
    with ExitStack() as stack:
        core, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("core", "execution"))
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.execute("INSERT INTO turns(id,session_id,status) VALUES('t','s','running')").close()
        execution.commit()
        core.execute("BEGIN").close()
        with core.execute("SELECT id,status FROM turns") as cursor:
            assert cursor.fetchall() == [("t", "running")]
        execution.execute("BEGIN IMMEDIATE").close()
        execution.execute("INSERT OR IGNORE INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('other','other-user')").close()
        execution.execute("UPDATE turns SET status='completed' WHERE id='t'").close()
        execution.commit()
        assert core.in_transaction
        core.rollback()
        with core.execute("SELECT status FROM turns WHERE id='t'") as cursor:
            assert cursor.fetchone()[0] == "completed"


@pytest.mark.parametrize("state", ["question", "failed"])
def test_document_question_and_failure_progress_while_content_snapshot_is_open(mutation_files, state):
    with managed(mutation_files, "execution") as execution, managed(mutation_files, "content") as content:
        execution.execute("INSERT INTO document_executions(job_id,state) VALUES('empty','rendering'),('with-draft','rendering')").close()
        execution.execute("INSERT INTO document_draft_files(job_id,artifact_id) VALUES('with-draft','artifact')").close()
        execution.commit()

        content.execute("BEGIN").close()
        with content.execute("SELECT state FROM document_executions WHERE job_id='with-draft'") as cursor:
            assert cursor.fetchone() == ("rendering",)

        execution.execute("BEGIN IMMEDIATE").close()
        execution.execute("UPDATE document_executions SET state=? WHERE job_id='empty'", (state,)).close()
        execution.commit()
        with execution.execute("SELECT state FROM document_executions WHERE job_id='empty'") as cursor:
            assert cursor.fetchone() == (state,)

        execution.execute("BEGIN IMMEDIATE").close()
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            execution.execute("UPDATE document_executions SET state=? WHERE job_id='with-draft'", (state,)).close()
        execution.rollback()
        with execution.execute("SELECT state FROM document_executions WHERE job_id='with-draft'") as cursor:
            assert cursor.fetchone() == ("rendering",)
        content.rollback()


@pytest.mark.parametrize("warm_cache", [False, True])
@pytest.mark.parametrize("disable_query_only", ["PRAGMA query_only=OFF", "/* Comment */ ; PRAGMA query_only(OFF)"])
def test_read_only_history_does_not_prevent_account_retirement(mutation_files, warm_cache, disable_query_only):
    with ExitStack() as stack:
        core, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("core", "execution"))
        core.execute("INSERT INTO users(id,status) VALUES('u','active')").close()
        core.commit()
        execution.execute("PRAGMA query_only=ON").close()
        sql = "SELECT status FROM users WHERE id='u'"
        if warm_cache:
            with execution.execute(sql) as cursor:
                assert cursor.fetchone()[0] == "active"
        execution.execute("BEGIN").close()
        with execution.execute(sql) as cursor:
            assert cursor.fetchone()[0] == "active"
        core.execute("INSERT INTO account_deletions(user_id,operation_id) VALUES('u','retire')").close()
        core.commit()
        assert not execution._leases
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            execution.execute(disable_query_only).close()
        with pytest.raises(sqlite3.OperationalError):
            execution.execute("INSERT INTO sessions(id,user_id) VALUES('late','u')").close()
        execution.rollback()
        execution.execute("PRAGMA query_only=OFF").close()
        with pytest.raises(sqlite3.IntegrityError):
            execution.execute("INSERT INTO sessions(id,user_id) VALUES('late','u')").close()
        execution.rollback()


def test_cached_read_after_switch_to_writable_protects_authority(mutation_files):
    with managed(mutation_files, "core") as core, managed(mutation_files, "execution") as execution:
        core.execute("INSERT INTO users(id,status) VALUES('u','active')").close()
        core.commit()
        execution.execute("PRAGMA query_only=ON").close()
        sql = "SELECT status FROM users WHERE id='u'"
        with execution.execute(sql) as cursor:
            assert cursor.fetchone()[0] == "active"
        execution.execute("PRAGMA query_only=OFF").close()
        execution.execute("BEGIN").close()
        with execution.execute(sql) as cursor:
            assert cursor.fetchone()[0] == "active"
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("INSERT INTO account_deletions(user_id,operation_id) VALUES('u','retire')").close()
        core.rollback()
        execution.rollback()


def test_account_retirement_waits_for_protected_admission_and_blocks_late_insert(mutation_files):
    with ExitStack() as stack:
        core, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("core", "execution"))
        core.execute("INSERT INTO users(id,status) VALUES('u','active')").close()
        core.commit()
        execution.execute("BEGIN IMMEDIATE").close()
        with execution.execute("SELECT user_id FROM account_deletions WHERE user_id='u'") as cursor:
            assert cursor.fetchone() is None
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("INSERT INTO account_deletions(user_id,operation_id) VALUES('u','retire')").close()
        core.rollback()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('before','u')").close()
        execution.commit()
        core.execute("INSERT INTO account_deletions(user_id,operation_id) VALUES('u','retire')").close()
        core.commit()
        with pytest.raises(sqlite3.IntegrityError):
            execution.execute("INSERT INTO sessions(id,user_id) VALUES('after','u')").close()
        execution.rollback()


def test_file_purge_cannot_overtake_reference_admission(mutation_files):
    with ExitStack() as stack:
        content, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("content", "execution"))
        content.execute("INSERT INTO artifacts(id) VALUES('a')").close()
        content.commit()
        execution.execute("BEGIN IMMEDIATE").close()
        execution.execute("INSERT INTO turn_attachments(artifact_id) VALUES('a')").close()
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            content.execute("INSERT INTO artifact_purge_claims(artifact_id) VALUES('a')").close()
        content.rollback()
        execution.rollback()
        content.execute("INSERT INTO artifact_purge_claims(artifact_id) VALUES('a')").close()
        content.commit()
        with pytest.raises(sqlite3.IntegrityError):
            execution.execute("INSERT INTO turn_attachments(artifact_id) VALUES('a')").close()
        execution.rollback()


def test_profile_edits_and_noop_authority_updates_do_not_take_exclusive_gate(mutation_files):
    with ExitStack() as stack:
        core, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("core", "execution"))
        core.execute("INSERT INTO users(id,status) VALUES('u','active')").close()
        core.commit()
        execution.execute("BEGIN IMMEDIATE").close()
        with execution.execute("SELECT status FROM users WHERE id='u'") as cursor:
            assert cursor.fetchone()[0] == "active"
        core.execute("UPDATE users SET display_name='Renamed',status=status WHERE id='u'").close()
        core.commit()
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("UPDATE users SET status='deleted' WHERE id='u'").close()
        core.rollback()
        execution.rollback()


def test_unmanaged_retirement_writer_cannot_bypass_fence(mutation_files):
    with managed(mutation_files, "core") as core:
        core.execute("INSERT INTO users(id,status) VALUES('u','active')").close()
        core.commit()
    with closing(sqlite3.connect(mutation_files)) as raw, pytest.raises(sqlite3.OperationalError, match="bowhead_domain_mutation_v2"):
        raw.execute("UPDATE users SET status='deleted' WHERE id='u'")


def test_retention_pin_cannot_overtake_purge_snapshot(mutation_files):
    from bowhead_core.sqlite_domain_runtime import _TRIGGERS, compile_domain_trigger

    execution_path = mutation_files.with_name(DOMAIN_TARGET_FILES["execution"])
    with closing(sqlite3.connect(execution_path)) as raw:
        raw.execute("CREATE TABLE document_artifact_refs(artifact_id TEXT,released_at TEXT)")
        for sql in mutation_triggers("execution"):
            if ' ON "document_artifact_refs"' in sql:
                raw.execute(sql)
        raw.commit()
    with closing(sqlite3.connect(mutation_files.with_name(DOMAIN_TARGET_FILES["content"]))) as raw:
        name = "trg_document_ref_purge"
        raw.execute(compile_domain_trigger(name, _TRIGGERS[name]["source"]))
        raw.commit()
    with ExitStack() as stack:
        content, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("content", "execution"))
        content.execute("BEGIN IMMEDIATE").close()
        with content.execute("SELECT artifact_id FROM document_artifact_refs") as cursor:
            assert cursor.fetchall() == []
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            execution.execute("INSERT INTO document_artifact_refs(artifact_id) VALUES('a')").close()
        execution.rollback()
        content.rollback()
        execution.execute("INSERT INTO document_artifact_refs(artifact_id) VALUES('a')").close()
        execution.commit()
        with pytest.raises(sqlite3.IntegrityError):
            content.execute("INSERT INTO artifact_purge_claims(artifact_id) VALUES('a')").close()
        content.rollback()


def test_sandbox_producer_classification_does_not_fence_foreign_snapshot(mutation_files):
    with ExitStack() as stack:
        content, execution = (stack.enter_context(managed(mutation_files, domain)) for domain in ("content", "execution"))
        content.execute("INSERT INTO artifacts(id) VALUES('stdout')").close()
        content.commit()
        execution.execute("BEGIN").close()
        execution.execute("SELECT id FROM artifacts WHERE id='stdout'").fetchone()
        content.execute("UPDATE artifacts SET producer_kind='sandbox_stdout' WHERE id='stdout'").close()
        content.commit()
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            content.execute("UPDATE artifacts SET deleted_at='now' WHERE id='stdout'").close()
        content.rollback()
        execution.rollback()
