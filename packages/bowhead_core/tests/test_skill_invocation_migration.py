"""0075 preserves read-only contracts and admits a separate invocation policy."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from test_chat_reference_storage import _head_without_trigger, _historical_fixture, _renew_dispatch_lease, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import facts, restore
from test_skill_reference_claim import legacy_skill_composition as _legacy_skill_composition
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case

claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
legacy_skill_composition = _legacy_skill_composition
pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0075_reference_skill_invocation")


def schema(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_preserve_rows_and_only_change_declared_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    if target == "core":
        assert [row for row in schema(path) if row[1] not in {MIGRATION.TRIGGER, MIGRATION.CONTEXT_TRIGGER}] == [
            row for row in previous if row[1] != MIGRATION.CONTEXT_TRIGGER
        ]
        assert len(schema(path)) == len(previous) + 1
    else:
        assert schema(path) == previous


@pytest.mark.parametrize("change", ["collision", "missing_context", "changed_context", "missing_baseline", "changed_baseline"])
def test_schema_drift_is_rejected_before_writing(tmp_path, change):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if change == "collision":
            db.execute(f"CREATE TABLE {MIGRATION.TRIGGER}(x TEXT)")
        elif change.endswith("baseline"):
            db.execute(f"DROP TRIGGER {MIGRATION.PREVIOUS.TRIGGER}")
            if change == "changed_baseline":
                db.execute(MIGRATION.PREVIOUS.DDL.replace("e.status='running'", "e.status='completed'"))
        else:
            db.execute(f"DROP TRIGGER {MIGRATION.CONTEXT_TRIGGER}")
            if change == "changed_context":
                db.execute(MIGRATION.PREVIOUS_CONTEXT.replace("t.status='running'", "t.status='completed'"))
    previous = schema(path)
    with pytest.raises(RuntimeError, match="reference skill invocation schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


def test_failed_replacement_restores_original_context_guard(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    previous = schema(path)
    execute = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        if statement == MIGRATION.CONTEXT_DDL:
            raise RuntimeError("injected replacement failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="injected replacement failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("legacy", [True, False])
async def test_read_only_compositions_and_ciphertext_survive_upgrade(claimed_case, request, legacy, tmp_path):
    if legacy:
        request.getfixturevalue("legacy_skill_composition")
    case = claimed_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    original = await restore(case)
    assert original[0].runtime_policy == ("initial-primary-skill-read.v1" if legacy else "initial-primary-skill-read.v2")
    assert (original[1].baseline is None) == legacy
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    # Current reads/writes run only at head (container startup runs `upgrade head`); assertions for this revision are already complete above.
    _upgrade(path)
    case.path = path
    for package in (case.fixture.root / "method", case.fixture.root / "support"):
        (package / "method.md").unlink()
    assert await restore(case) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_old_schema_denies_new_claim_then_upgrade_allows_it(claimed_case, tmp_path):
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    case = claimed_case
    sources = case.fixture.sources

    def use(path):
        case.path = path
        case.args["sources"] = SkillReadSourceResolver(
            path, access_factory=sources.access_factory, builtin_index=sources.builtin_index, builtin_root=sources.builtin_root
        )

    # Current readers/writers run only at head (container startup runs `upgrade head`), so the runtime gate is
    # proven on a head copy without only this revision's trigger; the actual previous schema then upgrades.
    gated = tmp_path / "gated.db"
    _head_without_trigger(case.path, gated, MIGRATION.TRIGGER)
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    use(gated)
    _renew_dispatch_lease(gated)
    before = facts(case)
    with pytest.raises(ValueError, match="reference_skill_invocation_migration_required"):
        await claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
    use(path)
    _upgrade(path, MIGRATION.revision)
    _upgrade(path)
    _renew_dispatch_lease(path)
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    composition, prepared, _ = await restore(case)
    assert composition.runtime_policy == "initial-primary-skill-baseline.v1"
    assert prepared.baseline is not None and len(prepared.baseline.packages) == 3
