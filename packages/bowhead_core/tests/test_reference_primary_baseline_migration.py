"""0076 expands only the single-primary contract and preserves all prior bytes."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from test_chat_reference_storage import _head_without_trigger, _historical_fixture, _renew_dispatch_lease, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import facts, restore
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case

claimed_case, receiver_case, start_case = _claimed_case, _receiver_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0076_reference_primary_baseline")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_rows_and_all_previous_composition_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
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


@pytest.mark.parametrize("change", ["collision", "context", "invocation", "failure"])
def test_drift_and_failed_replacement_leave_the_previous_schema_intact(tmp_path, monkeypatch, change):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if change == "collision":
            db.execute(f"CREATE TABLE {MIGRATION.TRIGGER}(x TEXT)")
        elif change == "context":
            db.execute(f"DROP TRIGGER {MIGRATION.CONTEXT_TRIGGER}")
            db.execute(MIGRATION.PREVIOUS_CONTEXT.replace("t.status='running'", "t.status='completed'"))
        elif change == "invocation":
            db.execute(f"DROP TRIGGER {MIGRATION.PREVIOUS.TRIGGER}")
    if change == "failure":
        original = Connection.exec_driver_sql

        def fail(self, statement, *args, **kwargs):
            if statement == MIGRATION.CONTEXT_DDL:
                raise RuntimeError("injected replacement failure")
            return original(self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    before = schema(path)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission", "admission_unused_invoke"], indirect=True)
async def test_existing_mixed_baselines_are_not_upgraded_or_rewritten(claimed_case, tmp_path):
    case = claimed_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    expected = await restore(case)
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    # Current readers/writers run only at head (container startup first runs `upgrade head`); this revision was already asserted above.
    _upgrade(path)
    case.path = path
    for package in expected[1].baseline.packages:
        (case.fixture.root / package.proof.name / "SKILL.md").unlink()
    assert await restore(case) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_empty"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_missing_migration_prevents_claim_without_changing_owner(claimed_case, tmp_path):
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
    with pytest.raises(ValueError, match="reference_primary_baseline_migration_required"):
        await claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
    use(path)
    _upgrade(path, MIGRATION.revision)
    _upgrade(path)
    _renew_dispatch_lease(path)
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    assert (await restore(case))[0].runtime_policy == "initial-primary-baseline.v1"
