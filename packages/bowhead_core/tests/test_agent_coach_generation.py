"""Owner proposal retries retain one generation and the original working copy."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, AgentChangeProposalStore
from bowhead_core.agent_coach_generation import CoachGenerationStore
from bowhead_core.agent_revision_store import AgentRevisionStore
from test_agent_change_proposal_store import _working
from test_agent_revision_store import _db, _seed, _Validator
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0138_agent_coach_requests")
pytestmark = pytest.mark.unit


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_generation_request_migration_preserves_all_existing_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: after[name] for name in before} == before
    assert ("agent_coach_generation_request" in after) == (target == "core")
    structure = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == structure
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision


async def setup(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    await _working(path)
    return (
        path,
        CoachGenerationStore(str(path)),
        {
            "team_id": "team_release",
            "agent_id": "osa_release",
            "owner_user_id": "user_owner",
            "idempotency_key": "original",
            "intent": {"goal": "Improve the prompt", "evidence_excerpts": [], "source_ref": None},
            "timeout_seconds": 240,
        },
    )


async def test_generation_commit_and_replay_share_one_exact_durable_receipt(tmp_path):
    path, requests, intent = await setup(tmp_path)
    claim = await requests.begin(**intent)
    assert claim.new and claim.config_surface["definition"]["system_prompt"] == "Base prompt."
    pending = await requests.begin(**intent)
    assert not pending.new and pending.id == claim.id and pending.state == "running"
    await requests.authorize_invocation(claim)
    with pytest.raises(AgentChangeProposalError, match="invocation_conflict"):
        await requests.authorize_invocation(claim)
    proposals = AgentChangeProposalStore(str(path), revision_store=AgentRevisionStore(str(path), validator=_Validator()))
    proposal = await proposals.create(
        team_id=claim.team_id,
        agent_id=claim.agent_id,
        effective_owner_user_id=claim.owner_user_id,
        source_type="coach",
        source_ref=claim.source_ref,
        expected_baseline=claim.baseline,
        strict_replay=True,
        generation=claim,
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Improved prompt."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
        provenance={"generation_request_id": claim.id},
    )
    recovered = await CoachGenerationStore(str(path)).begin(**intent)
    assert not recovered.new and recovered.result_proposal_id == proposal.id and recovered.state == "succeeded"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_coach_generation_request SET request_hash=?", ("sha256:" + "0" * 64,))
    with pytest.raises(AgentChangeProposalError, match="idempotency_conflict"):
        await requests.begin(**{**intent, "intent": {**intent["intent"], "goal": "Different goal"}})


async def test_unknown_generation_survives_restart_without_another_claim(tmp_path):
    path, requests, intent = await setup(tmp_path)
    claim = await requests.begin(**intent)
    await requests.fail(claim, unknown=True, code="agent_coach_generation_unknown")
    recovered = await CoachGenerationStore(str(path)).begin(**intent)
    assert recovered.state == "unknown" and not recovered.new and recovered.id == claim.id
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError, match="cannot restart"):
        db.execute("UPDATE agent_coach_generation_request SET state='running',failure_code=NULL")


async def test_expired_generation_is_unknown_and_cannot_start_provider_work(tmp_path):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    _, requests, intent = await setup(tmp_path)
    claim = await requests.begin(**intent)
    future = datetime.now(UTC) + timedelta(hours=1)
    with patch("bowhead_core.agent_coach_generation.datetime") as clock:
        clock.now.return_value = future
        recovered = await requests.begin(**intent)
        assert recovered.state == "unknown" and not recovered.new
        with pytest.raises(AgentChangeProposalError, match="invocation_conflict"):
            await requests.authorize_invocation(claim)


def test_generation_migration_rolls_back_a_partial_schema(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:2], "invalid sql"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute)
            ),
        )
        with pytest.raises(sqlite3.OperationalError, match="syntax error"):
            MIGRATION.upgrade()
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision


async def test_new_working_copy_during_generation_cannot_relabel_the_proposal(tmp_path):
    path, requests, intent = await setup(tmp_path)
    claim = await requests.begin(**intent)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
    proposals = AgentChangeProposalStore(str(path))
    with pytest.raises(AgentChangeProposalError, match="version_conflict"):
        await proposals.create(
            team_id=claim.team_id,
            agent_id=claim.agent_id,
            effective_owner_user_id=claim.owner_user_id,
            source_type="coach",
            source_ref=claim.source_ref,
            expected_baseline=claim.baseline,
            strict_replay=True,
            generation=claim,
            operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Old model response."}],
            atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
            provenance={"generation_request_id": claim.id},
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
        assert db.execute("SELECT state FROM agent_coach_generation_request").fetchone()[0] == "running"
    assert not (await requests.begin(**intent)).new
