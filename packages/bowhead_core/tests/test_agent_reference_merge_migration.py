"""The production `upgrade head` path joins both independently shipped leaves."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from test_chat_reference_storage import _historical_fixture, _upgrade, assert_rows_survive_upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case

claimed_case, receiver_case, start_case = _claimed_case, _receiver_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0077_merge_agent_reference")
LEAVES = ("0075_agent_version_operations", "0076_reference_primary_baseline")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
@pytest.mark.parametrize("previous", ["0001_initial", *LEAVES, "both"])
def test_each_existing_leaf_reaches_the_same_single_head(tmp_path, target, previous):
    path = tmp_path / TARGET_FILES[target]
    for revision in LEAVES if previous == "both" else (previous,):
        _upgrade(path, revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    actual = snapshot(path)
    # Earlier releases intentionally retire tables. Preservation here concerns
    # the two live leaves; the 1.0.0 path verifies the full historical chain.
    if previous != "0001_initial":
        assert {name: actual[name] for name in before} == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [(MIGRATION.revision,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    final_schema = schema(path)
    if previous == "both":
        assert actual == before and final_schema == old_schema
    empty_path = tmp_path / "empty.db"
    _upgrade(empty_path, MIGRATION.revision, target)
    assert schema(empty_path) == final_schema
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == actual and schema(path) == final_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_reference_leaf_keeps_its_original_encrypted_baseline(claimed_case, tmp_path):
    case = claimed_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    expected = await restore(case)
    path = tmp_path / "reference-leaf.db"
    _historical_fixture(case.path, path, "0076_reference_primary_baseline")
    before = snapshot(path)
    _upgrade(path)
    after = snapshot(path)
    assert_rows_survive_upgrade(before, after)
    case.path = path
    for package in expected[1].baseline.packages:
        (case.fixture.root / package.proof.name / "SKILL.md").unlink()
    assert await restore(case) == expected


@pytest.mark.asyncio
async def test_agent_operations_leaf_keeps_its_original_proposal(tmp_path):
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore
    from test_agent_change_proposal_store import _working
    from test_agent_revision_store import _db, _seed

    original = _db(tmp_path)
    _seed(original)
    await _working(original)
    store = AgentChangeProposalStore(str(original))
    proposal = await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="feedback",
        source_ref="feedback:merge-test",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Proposed."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0], "depends_on": []}],
    )
    path = tmp_path / "operations-leaf.db"
    _historical_fixture(original, path, "0075_agent_version_operations")
    before = snapshot(path)
    _upgrade(path)
    after = snapshot(path)
    assert_rows_survive_upgrade(before, after)
    restored = await AgentChangeProposalStore(str(path)).get(
        team_id="team_release", agent_id="osa_release", proposal_id=proposal.id, actor_user_id="user_owner"
    )
    assert restored == proposal
