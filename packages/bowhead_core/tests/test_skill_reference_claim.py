"""Real mixed admission, atomic ownership/materials and retained recovery.

No model/scheduler is invoked. Fault triggers leave production immutable guards
intact; preparation races change real preferences after reading real packages.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection
from bowhead_core.chat_reference_scope import require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.skill_read_primary import prepare_reference_primary_skill_reads
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker, restore_skill_primary_composition_in_connection
from cryptography.fernet import Fernet
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case
from test_skill_start_admission import candidate

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.fixture
async def claimed_case(receiver_case, request):
    fixture = receiver_case
    if getattr(request, "param", None) in {"canonical_input", "agent_baseline", "extension"}:
        from bowhead_core.chat_reference_preparation import prepare_reference_team_start_admission
        from test_reference_start_preparation import activate, raw_document

        activate(fixture)
        document = raw_document(fixture, skills=request.param not in {"agent_baseline", "extension"})
        if request.param == "extension":
            from uuid import uuid4

            from bowhead_core.chat_references import ReferenceNode

            document.nodes.insert(
                0,
                ReferenceNode(type="reference", node_id=str(uuid4()), resource=fixture.optional, label="Optional method", parameters={"year": 2026}),
            )
            document.nodes.insert(
                1,
                ReferenceNode(
                    type="reference", node_id=str(uuid4()), resource=fixture.child, label="Original supporting method", parameters={"year": 2025}
                ),
            )
        prepared = await prepare_reference_team_start_admission(
            fixture.case.fixture.db_path, input_document=document, scope=fixture.scope, sources=fixture.sources
        )
        fixture.case.args.update(reference_start=prepared, user_text=document.display_text())
    else:
        await candidate(
            fixture,
            agent_first=getattr(request, "param", False),
            implicit=fixture.scope.session_id is None and "optional" in request.node.callspec.params["receiver_case"],
        )
    await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    args = {
        "sources": fixture.sources,
        "scope": scope,
        "turn_id": "t_reference",
        "cipher": cipher,
        "expected_generation": 1,
        "owner_id": "mixed-worker",
    }
    return SimpleNamespace(fixture=fixture, scope=scope, cipher=cipher, args=args, path=fixture.case.fixture.db_path)


def facts(case):
    with sqlite3.connect(case.path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} WHERE turn_id='t_reference'").fetchall()
            for table in (
                "chat_turn_spawn_dispatch",
                "turn_agent_execution",
                "chat_reference_resolutions",
                "chat_reference_content_snapshots",
                "chat_reference_evidence",
            )
        }


async def restore(case):
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        try:
            epoch = await require_reference_request_scope(db, case.scope)
            scope = ReferenceScope(**case.scope.model_dump(), policy_epoch=epoch)
            primary = await read_reference_skill_primary_in_connection(db, scope=scope, turn_id="t_reference", bindings=case.fixture.case.bindings)
            return await restore_skill_primary_composition_in_connection(db, primary=primary, scope=scope, generation=1, cipher=case.cipher)
        finally:
            await db.rollback()


@pytest.mark.parametrize(
    "receiver_case", ["source", "source_unused_read", "source_unused_sop", "source_selected_invoke", "source_unused_invoke"], indirect=True
)
async def test_historical_admission_retains_complete_supported_baseline(receiver_case, request):
    # Real immutable source/plan/binding producers seed a historical accepted
    # shape. Old versions could accept broader baselines; no guard is disabled.
    fixture = receiver_case
    now = datetime.now(UTC)
    path = fixture.case.fixture.db_path
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
            "VALUES('t_reference','s_reference','start','claimed',?,?,?)",
            ((now + timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat()),
        )
    case = SimpleNamespace(path=path, fixture=fixture, scope=fixture.scope, cipher=ReferenceCipher(Fernet.generate_key().decode()))
    args = {
        "sources": fixture.sources,
        "cipher": case.cipher,
        "scope": fixture.scope,
        "turn_id": "t_reference",
        "owner_id": "mixed-worker",
        "expected_generation": 1,
    }
    prepared, _ = await prepare_reference_primary_skill_reads(fixture.sources, scope=fixture.scope, turn_id="t_reference")
    result = await claim_reference_skill_primary_for_worker(**args)
    assert result.outcome == "claimed" and result.primary.binding.status == "running"
    composition, retained, _ = await restore(case)
    assert retained.receiver.grants == prepared.receiver.grants
    assert len(composition.members) == 2
    expected_policy = (
        "initial-primary-skill-baseline.v1" if "invoke" in request.node.callspec.params["receiver_case"] else "initial-primary-skill-read.v2"
    )
    assert composition.runtime_policy == expected_policy


@pytest.mark.parametrize("receiver_case", ["admission", "admission_optional"], indirect=True)
@pytest.mark.parametrize("claimed_case", [False, True], indirect=True)
async def test_real_claim_retains_all_members_parameters_and_original_receiver(claimed_case, monkeypatch):
    case = claimed_case
    first = await claim_reference_skill_primary_for_worker(**case.args)
    assert first.outcome == "claimed" and first.primary.binding.status == "running"
    composition, prepared, rendered = await restore(case)
    assert composition.runtime_policy == "initial-primary-skill-read.v2"
    assert composition.receiver_status == prepared.receiver.status == "admitted"
    assert composition.receiver_digest == prepared.receiver.digest
    assert composition.rendered_content_hash == rendered.rendered_content_hash
    assert composition.selection_digest == rendered.selection_digest
    assert composition.preparation_digest == prepared.preparation_digest
    assert [member.resource.id for member in composition.members] == ["builtin:support", "builtin:method"]
    assert composition.content.resource == composition.resource
    assert composition.content.content_kind == "skill_baseline"
    assert prepared.baseline is not None
    assert len(facts(case)["chat_reference_content_snapshots"]) == len(facts(case)["chat_reference_resolutions"]) == 1
    assert facts(case)["chat_reference_evidence"] == []
    values = {item["resource"]["id"]: item["parameters"] for item in rendered.document()["skills"]}
    if composition.members[0].node_id is None:
        assert values == {"builtin:method": {}, "builtin:support": {}}
    else:
        assert values == {"builtin:method": {"year": 2026}, "builtin:support": {"year": 2025}}
    for name in ("method", "support"):
        (case.fixture.root / name / "method.md").unlink()

    async def forbidden(*args, **kwargs):
        raise AssertionError("retry must use retained identity, never prepare again")

    monkeypatch.setattr("bowhead_core.skill_reference_execution.prepare_reference_primary_skill_reads", forbidden)
    before = facts(case)
    for owner in ("mixed-worker", "other-worker"):
        retry = await claim_reference_skill_primary_for_worker(**(case.args | {"owner_id": owner}))
        assert retry.outcome == "already_running" and retry.primary.binding.execution_id == first.primary.binding.execution_id
        assert facts(case) == before
    assert await restore(case) == (composition, prepared, rendered)


@pytest.fixture
def legacy_skill_composition(monkeypatch):
    # Produce the still-supported historical v1 contract for upgrade fixtures.
    # The actual v1 composition/material writer and all DB guards remain active.
    import bowhead_core.skill_reference_execution as execution

    original = execution._composition

    def legacy(primary, prepared, scope):
        return original(primary, replace(prepared, baseline=None), scope)

    monkeypatch.setattr(execution, "_composition", legacy)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("table", ["chat_reference_content_snapshots", "chat_reference_resolutions"])
async def test_failed_material_or_composition_write_rolls_back_ownership_and_retries(claimed_case, table):
    case = claimed_case
    before = facts(case)
    with sqlite3.connect(case.path) as db:
        db.execute(f"CREATE TRIGGER inject_claim_failure BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected claim failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected claim failure"):
        await claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
    with sqlite3.connect(case.path) as db:
        db.execute("DROP TRIGGER inject_claim_failure")
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["method", "support", "epoch", "cancel", "lease"])
async def test_actual_change_after_preparation_prevents_any_partial_claim(claimed_case, monkeypatch, change):
    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution.prepare_reference_primary_skill_reads
    after = None

    async def changed(*args, **kwargs):
        nonlocal after
        prepared = await original(*args, **kwargs)
        if change in {"method", "support"}:
            await case.fixture.sources.instances.upsert(
                user_id=case.scope.user_id, name=change, source_type="builtin", source_path=str(case.fixture.root / change), enabled=False
            )
        else:
            with sqlite3.connect(case.path) as db:
                if change == "epoch":
                    db.execute("UPDATE access_policy_state SET epoch=epoch+1 WHERE team_id=?", (case.scope.team_id,))
                elif change == "cancel":
                    db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id='t_reference'", (datetime.now(UTC).isoformat(),)
                    )
                else:
                    db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
                        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
                    )
        after = facts(case)
        return prepared

    monkeypatch.setattr(execution, "prepare_reference_primary_skill_reads", changed)
    with pytest.raises(ValueError):
        await execution.claim_reference_skill_primary_for_worker(**case.args)
    assert after is not None and facts(case) == after


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_two_prepared_claimers_produce_one_durable_winner(claimed_case, monkeypatch):
    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution.prepare_reference_primary_skill_reads
    ready = asyncio.Event()
    arrived = 0

    async def rendezvous(*args, **kwargs):
        nonlocal arrived
        result = await original(*args, **kwargs)
        arrived += 1
        if arrived == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return result

    monkeypatch.setattr(execution, "prepare_reference_primary_skill_reads", rendezvous)
    results = await asyncio.gather(*(execution.claim_reference_skill_primary_for_worker(**(case.args | {"owner_id": owner})) for owner in ("a", "b")))
    assert sorted(result.outcome for result in results) == ["already_running", "claimed"]
    assert len(facts(case)["chat_reference_resolutions"]) == len(facts(case)["chat_reference_content_snapshots"]) == 1
    await restore(case)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["expired", "cancelled", "purged"])
async def test_claimed_mixed_reservation_settles_without_reexecuting_methods(claimed_case, change):
    case = claimed_case
    await claim_reference_skill_primary_for_worker(**case.args)
    before = facts(case)
    if change == "cancelled":
        await case.fixture.case.store.request_cancel_turn(
            team_id=case.scope.team_id,
            user_id=case.scope.user_id,
            workspace_id=case.scope.workspace_id,
            turn_id="t_reference",
            expected_execution_generation=1,
            cancel_request_id="cancel-mixed-claim",
        )
    with sqlite3.connect(case.path) as db:
        if change == "purged":
            db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (
            "cancelled" if change == "cancelled" else "failed",
        )
        assert db.execute("SELECT COUNT(*) FROM chat_reference_evidence").fetchone() == (0,)
    assert facts(case)["chat_reference_resolutions"] == before["chat_reference_resolutions"]


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["generation", "cancel", "lease"])
async def test_final_fence_failure_rolls_back_complete_materials_and_claim(claimed_case, monkeypatch, change):
    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution._put_primary_composition_in_connection
    before = facts(case)

    async def injected(db, **kwargs):
        result = await original(db, **kwargs)
        if change == "generation":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2 WHERE turn_id='t_reference'")
        elif change == "cancel":
            await db.execute(
                "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id='t_reference'", (datetime.now(UTC).isoformat(),)
            )
        else:
            await db.execute(
                "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        return result

    # Transaction-local fault injection after real writes, not a claim that
    # concurrent Stop can acquire the SQLite writer lock during this transaction.
    monkeypatch.setattr(execution, "_put_primary_composition_in_connection", injected)
    with pytest.raises(ValueError, match="reference_execution_fence_lost"):
        await execution.claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_previous_schema_cannot_commit_new_composition(claimed_case, tmp_path):
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver
    from test_chat_reference_storage import _head_without_trigger

    # Current readers/writers run only at head (container startup runs `upgrade head`; e.g. chat sources gained
    # document_revision_target_json in 0147), so the 0069 gate is proven on a head copy without only its trigger.
    case = claimed_case
    path = tmp_path / "previous.db"
    _head_without_trigger(case.path, path, "trg_reference_skill_composition_material")
    with sqlite3.connect(path) as db:
        version = db.execute("SELECT version_num FROM alembic_version").fetchone()
    sources = case.fixture.sources
    case.args["sources"] = SkillReadSourceResolver(
        path, access_factory=sources.access_factory, builtin_index=sources.builtin_index, builtin_root=sources.builtin_root
    )
    case.path = path
    before = facts(case)
    with pytest.raises(ValueError, match="reference_skill_composition_migration_required"):
        await claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == version
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='trg_reference_skill_composition_material'").fetchone() is None


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["descriptor", "parameter_source", "operation", "agent_node", "receiver"])
async def test_database_guard_rejects_unbound_or_unsupported_composition(claimed_case, monkeypatch, change):
    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution._put_primary_composition_in_connection
    before = facts(case)

    async def corrupted(db, *, composition, generation):
        if change == "descriptor":
            composition = composition.model_copy(update={"content": composition.content.model_copy(update={"content_hash": "sha256:" + "f" * 64})})
        elif change == "parameter_source":
            composition = composition.model_copy(update={"parameter_source": "unconfirmed"})
        elif change == "agent_node":
            composition = composition.model_copy(update={"node_id": composition.members[0].node_id})
        elif change == "receiver":
            composition = composition.model_copy(update={"receiver_digest": "sha256:" + "f" * 64})
        else:
            composition = composition.model_copy(
                update={"members": (composition.members[0].model_copy(update={"operation": "invoke"}), *composition.members[1:])}
            )
        return await original(db, composition=composition, generation=generation)

    monkeypatch.setattr(execution, "_put_primary_composition_in_connection", corrupted)
    with pytest.raises(sqlite3.IntegrityError, match="reference skill composition material mismatch"):
        await execution.claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before


@pytest.mark.parametrize("receiver_case", ["admission_package_assets"], indirect=True)
async def test_v2_original_package_bytes_survive_files_removed_without_entering_method_input(claimed_case):
    import shutil

    case = claimed_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    original = await restore(case)
    composition, prepared, rendered = original
    package = next(item.material for item in prepared.baseline.packages if item.proof.resource == case.fixture.selected)
    assert package.files()["asset.bin"] == bytes(range(256)) * 1280
    assert package.files()["scripts/runner.py"] == (case.fixture.root / "method/scripts/runner.py").read_bytes()
    assert "NOT_SELECTED_MANIFEST" not in rendered.content.decode()
    assert "fixture script" not in rendered.content.decode()
    assert len(prepared.baseline.content) > 256 * 1024
    assert composition.content.byte_count == len(prepared.baseline.content)
    before = facts(case)
    assert "BODY_method" not in str(before) and "NOT_SELECTED_MANIFEST" not in str(before)
    shutil.rmtree(case.fixture.root)
    assert await restore(case) == original
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "already_running"
    assert facts(case) == before
    case.cipher = ReferenceCipher(Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="reference_composition_changed"):
        await restore(case)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_v2_purge_erases_raw_packages_and_denies_restore(claimed_case):
    case = claimed_case
    await claim_reference_skill_primary_for_worker(**case.args)
    assert (await restore(case))[1].baseline is not None
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
        assert db.execute("SELECT ciphertext,retention_state FROM chat_reference_content_snapshots").fetchall() == [(None, "purged")]
    with pytest.raises(ValueError):
        await restore(case)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_retained_exact_bytes_are_committed_without_file_reads_under_writer_lock(claimed_case, monkeypatch):
    from pathlib import Path

    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution.prepare_reference_primary_skill_reads
    expected = None

    async def prepare(*args, **kwargs):
        nonlocal expected
        expected, rendered = await original(*args, **kwargs)
        for name in ("method", "support"):
            (case.fixture.root / name / "method.md").unlink()

        def forbidden(*args, **kwargs):
            raise AssertionError("no file reads inside claim")

        monkeypatch.setattr(Path, "read_bytes", forbidden)
        monkeypatch.setattr(Path, "read_text", forbidden)
        return expected, rendered

    monkeypatch.setattr(execution, "prepare_reference_primary_skill_reads", prepare)
    assert (await execution.claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    _, prepared, _ = await restore(case)
    assert prepared == expected


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_cancellation_during_persistence_rolls_back_both_material_and_owner(claimed_case, monkeypatch):
    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution._put_primary_composition_in_connection
    before = facts(case)

    async def cancelled(db, **kwargs):
        await original(db, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(execution, "_put_primary_composition_in_connection", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await execution.claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_original_accepted_deadline_is_rechecked_after_material_writes(claimed_case, monkeypatch):
    case = claimed_case
    import bowhead_core.skill_reference_execution as execution

    original = execution._put_primary_composition_in_connection
    before = facts(case)
    with sqlite3.connect(case.path) as db:
        deadline = datetime.fromisoformat(
            db.execute("SELECT lease_expires_at FROM chat_turn_spawn_dispatch WHERE turn_id='t_reference'").fetchone()[0]
        )

    async def elapsed(db, **kwargs):
        result = await original(db, **kwargs)
        monkeypatch.setattr("bowhead_core.chat_reference_execution._now", lambda: deadline + timedelta(seconds=1))
        return result

    monkeypatch.setattr(execution, "_put_primary_composition_in_connection", elapsed)
    with pytest.raises(ValueError, match="reference_execution_fence_lost"):
        await execution.claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
