"""0070 is additive, preserves actual prior materials, and rejects drift."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from bowhead_router.input_admission import LLMInputAdmissionError
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_skill_model_context import access_case as _access_case
from test_skill_model_context import claimed_case as _claimed_case
from test_skill_model_context import contexts, dispatch, receiver
from test_skill_model_context import model_case as _model_case
from test_skill_model_context import receiver_case as _receiver_case
from test_skill_model_context import start_case as _start_case
from test_skill_reference_claim import legacy_skill_composition as _legacy_skill_composition
from test_skill_reference_claim import restore

access_case = _access_case
claimed_case = _claimed_case
model_case = _model_case
receiver_case = _receiver_case
start_case = _start_case
legacy_skill_composition = _legacy_skill_composition

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("legacy_skill_composition")]
MIGRATION = import_module("bowhead_core.migrations.versions.0070_reference_model_context")


def snapshot(path):
    with sqlite3.connect(path) as db:
        names = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'alembic_version' AND name NOT LIKE 'sqlite_%'")
        ]
        return {n: db.execute(f"SELECT * FROM {n}").fetchall() for n in names}


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_upgrade_repeat_without_rewriting_previous_schema_or_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    with sqlite3.connect(path) as db:
        schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        assert all(row in after for row in schema)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    actual = snapshot(path)
    assert {name: actual[name] for name in before} == before
    if target == "core":
        assert actual["chat_reference_model_contexts"] == []
    else:
        assert actual == before and schema == after


@pytest.mark.parametrize("name", ["chat_reference_model_contexts", "idx_reference_model_context_execution", "trg_reference_model_context_purge"])
def test_drift_rejected_without_partial_schema_or_revision(tmp_path, name):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {name}(x TEXT)")
        before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with pytest.raises(RuntimeError, match="reference model context schema drift"):
        _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


def test_failure_after_first_ddl_rolls_back_entire_expansion(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    from sqlalchemy.engine import Connection

    original = Connection.exec_driver_sql

    def fail_index(self, statement, *args, **kwargs):
        if statement.startswith("CREATE INDEX idx_reference_model_context_execution"):
            raise RuntimeError("injected migration failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_index)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='chat_reference_model_contexts'").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_old_schema_rejects_producer_then_upgrade_preserves_original_closure_and_accepts(model_case, tmp_path):
    case = model_case
    original = await restore(case.case.case)
    path = tmp_path / "previous.db"
    _historical_fixture(case.case.case.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    sources = case.case.access.sources
    access = replace(
        case.case.access,
        sources=SkillReadSourceResolver(
            path, access_factory=sources.access_factory, builtin_index=sources.builtin_index, builtin_root=sources.builtin_root
        ),
    )
    case.case.ctx.session_store = SessionStore(path)
    case.case.ctx.reference_skill_access = access
    case.case.access = access
    case.policy = replace(case.policy, access=access)
    with receiver(case.case.handle.subagent_id):
        with pytest.raises(LLMInputAdmissionError):
            await dispatch(case)
        assert snapshot(path) == before
        _upgrade(path, MIGRATION.revision)
        assert {name: snapshot(path)[name] for name in before} == before
        # Current reads/writes run only at head (container startup runs `upgrade head`); assertions for this revision are already complete above.
        _upgrade(path)
        case.case.case.path = path
        restored = await restore(case.case.case)
        assert restored == original
        await dispatch(case)
    assert len(contexts(case)) == 1
