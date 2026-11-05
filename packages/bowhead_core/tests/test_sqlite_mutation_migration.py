"""Append the mutation protocol without changing data or historical split bytes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.sqlite_domain_upgrade import SPLIT_REVISION, run_domain_upgrade, table_evidence
from bowhead_core.sqlite_domains import DOMAIN_TABLES, DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import database_head
from test_sqlite_domain_upgrade import _advance
from test_sqlite_domain_upgrade import directory as directory
from test_sqlite_domain_upgrade import legacy_source as legacy_source


def evidence(directory):
    result = {}
    for domain, filename in DOMAIN_TARGET_FILES.items():
        with closing(sqlite3.connect(directory / filename)) as db:
            result[domain] = {table: table_evidence(db, table) for table in DOMAIN_TABLES[domain]}
    return result


def test_append_preserves_every_domain_row_and_restart_is_idempotent(directory):
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    before = evidence(directory)
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert evidence(directory) == before
    assert all(database_head(directory / filename) == chain_head() for filename in DOMAIN_TARGET_FILES.values())
    with closing(sqlite3.connect(directory / "bowhead_execution.db")) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='trg_domain_mutation_turns_delete'").fetchone() == (1,)
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='trg_domain_mutation_document_executions_update'").fetchone()[0]
        assert "NOT EXISTS (SELECT 1 FROM document_draft_files WHERE job_id = OLD.job_id)" in sql
    state = (directory / "sqlite-layout.json").read_bytes()
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert (directory / "sqlite-layout.json").read_bytes() == state


def test_interrupted_trigger_replacement_rolls_back_and_resumes(directory, monkeypatch):
    from bowhead_core import sqlite_mutation_guards

    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    before = evidence(directory)
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        old_schema = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' ORDER BY name").fetchall()
    original = sqlite_mutation_guards.mutation_triggers

    def interrupted(domain):
        for statement in original(domain):
            yield statement
            raise RuntimeError("interrupted mutation trigger replacement")

    monkeypatch.setattr(sqlite_mutation_guards, "mutation_triggers", interrupted)
    with pytest.raises(RuntimeError, match="interrupted mutation trigger replacement"):
        upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert database_head(directory / "bowhead.db") == SPLIT_REVISION
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        assert db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' ORDER BY name").fetchall() == old_schema
    assert evidence(directory) == before
    monkeypatch.setattr(sqlite_mutation_guards, "mutation_triggers", original)
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert json.loads((directory / "sqlite-layout.json").read_text())["status"] == "ready"
    assert evidence(directory) == before
