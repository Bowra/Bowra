"""CI recovery preserves exact intent and stops when original authority expires."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.agent_automation_context import (
    AgentAutomationContextStore,
    AutomationAuthority,
    AutomationPreparation,
    comparison_intent_hash,
    operation_key_hash,
)
from bowhead_core.agent_automation_execution import AutomationExecutionGuard
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore
from bowhead_core.agent_eval_comparison import AgentEvalComparisonError, AgentEvalComparisonStore
from bowhead_core.agent_eval_product import AgentEvalReleaseGate
from bowhead_core.agent_revision_store import AgentRevisionConflictError, AgentRevisionStore
from bowhead_core.agent_version_automation import AgentVersionAutomationError, AgentVersionAutomationStore
from bowhead_core.openapi_store import OpenApiStore
from test_agent_change_proposal_store import _working
from test_agent_eval_policy_plan_store import _comparison_context, _model_hashes, _upgrade
from test_agent_eval_policy_plan_store import _seed as _seed_comparison
from test_agent_revision_store import _db, _seed, _seed_eval_workspace, _Validator

pytestmark = pytest.mark.integration


async def authority_for(db_path, *, team_id="team_p", agent_id="osa_policy", bound=True, workspace_id="ws_eval"):
    key, _ = await OpenApiStore(str(db_path)).create_key(
        team_id=team_id,
        name="CI",
        scopes=["agent_versions:read", "agent_versions:write"],
        expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
    )
    grant = await AgentVersionAutomationStore(str(db_path)).create_grant(
        team_id=team_id,
        agent_id=agent_id,
        actor_user_id="user_owner",
        principal_id=key["id"],
        scopes=["proposal:create", "candidate:prepare", "eval:run", "gate:read"],
        repositories=["acme/agent"],
        refs=["refs/heads/*"],
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        evaluation_workspace_id=workspace_id if bound else None,
    )
    return AutomationAuthority(key["id"], grant["id"], "acme/agent", "refs/heads/main", "user_owner")


async def comparison_fixture(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path)
    _seed_comparison(path)
    plan, control = await _comparison_context(path)
    authority = await authority_for(path)
    args = {
        "team_id": "team_p",
        "agent_id": "osa_policy",
        "candidate_id": "arc_policy",
        "actor_user_id": "user_owner",
        "candidate_row_version": 1,
        "expected_control_row_version": control.row_version,
        "candidate_hash": "sha256:" + "9" * 64,
        "plan_id": plan.id,
        "plan_hash": plan.plan_hash,
        "model_config_hashes": _model_hashes(),
        "idempotency_key": f"automation:{authority.principal_id}:compare",
        "actor_kind": "automation",
        "actor_ref": authority.principal_id,
        "automation_grant_id": authority.grant_id,
        "automation": authority,
    }
    store = AgentEvalComparisonStore(path)
    execution = await store.start_release(**args)
    return path, authority, store, args, execution


@pytest.mark.asyncio
async def test_comparison_retry_uses_original_intent_despite_changed_models(tmp_path, monkeypatch):
    path, authority, store, args, execution = await comparison_fixture(tmp_path, monkeypatch)
    assert (await store.start_release(**(args | {"model_config_hashes": {"removed": "sha256:changed"}}))).id == execution.id
    intent = comparison_intent_hash(
        team_id=args["team_id"],
        agent_id=args["agent_id"],
        authority=authority,
        **{
            key: args[key]
            for key in ("candidate_id", "candidate_hash", "candidate_row_version", "plan_id", "plan_hash", "expected_control_row_version")
        },
    )
    replay = await store.replay_automation(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        automation=authority,
        idempotency_key=args["idempotency_key"],
        client_intent_hash=intent,
    )
    assert replay.id == execution.id
    with pytest.raises(AgentEvalComparisonError, match="idempotency_conflict"):
        await store.start_release(**(args | {"expected_control_row_version": 99}))
    with pytest.raises(AgentVersionAutomationError, match="origin_mismatch"):
        await store.replay_automation(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            automation=replace(authority, ref="refs/heads/other"),
            idempotency_key=args["idempotency_key"],
            client_intent_hash=intent,
        )
    lookup = await AgentAutomationContextStore(str(path)).read_comparison_operation(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        authority=authority,
        operation_key_hash=operation_key_hash(args["idempotency_key"]),
    )
    assert lookup["execution"]["id"] == execution.id
    assert lookup["client_intent_verified"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["grant", "principal", "owner", "space", "member", "legacy"])
async def test_worker_revocation_cancels_all_runs_without_green_snapshot(tmp_path, monkeypatch, loss):
    path, authority, store, args, execution = await comparison_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        if loss == "grant":
            db.execute(
                "UPDATE agent_version_automation_grant SET revoked_at=?,row_version=row_version+1 WHERE id=?",
                (datetime.now(UTC).isoformat(), authority.grant_id),
            )
        elif loss == "principal":
            db.execute("UPDATE team_openapi_keys SET scopes_json='[]' WHERE id=?", (authority.principal_id,))
        elif loss == "owner":
            db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_policy'")
        elif loss == "space":
            db.execute("UPDATE workspace_members SET status='removed' WHERE workspace_id='ws_eval' AND user_id='user_owner'")
        elif loss == "member":
            db.execute("UPDATE team_members SET status='removed' WHERE user_id='user_owner'")
        else:
            # Simulate a historical queued row migrated without invented provenance.
            db.execute("DROP TRIGGER trg_agent_comparison_origin_immutable")
            db.execute("DROP TRIGGER trg_agent_eval_comparison_transition")
            db.execute("UPDATE agent_eval_comparison_execution SET automation_context_json=NULL,client_intent_hash=NULL WHERE id=?", (execution.id,))
    assert not await AutomationExecutionGuard(str(path), execution.runs[0].run_id).allowed()
    stopped = await store.get(execution.id)
    assert stopped.status == "cancelled"
    assert (await store.finalize(execution.id)).snapshot is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT readiness,active_snapshot_id FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'").fetchone() == (
            "unknown",
            None,
        )
        assert db.execute("SELECT COUNT(*) FROM agent_eval_run WHERE status IN ('queued','running')").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM agent_eval_snapshot").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_prepare_preserves_origin_and_recovers_after_command_receipt_expiry(tmp_path, monkeypatch):
    path = _db(tmp_path)
    _seed(path)
    _seed_eval_workspace(path)
    authority = await authority_for(path, team_id="team_release", agent_id="osa_release")
    working = await _working(path)
    revisions = AgentRevisionStore(str(path), validator=_Validator(), eval_gate=AgentEvalReleaseGate())
    proposals = AgentChangeProposalStore(str(path), revision_store=revisions)
    proposal = await proposals.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="git",
        source_ref="ci:prepare",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Candidate prompt."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0], "depends_on": []}],
        created_by_kind="automation",
        created_by_id=authority.principal_id,
        provenance={"automation_grant_id": authority.grant_id, "repository": authority.repository, "ref": authority.ref},
    )
    accepted = await proposals.apply(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        selected_group_ids=["behavior"],
        expected_proposal_row_version=proposal.row_version,
        expected_working_copy_row_version=working.row_version,
    )
    working = accepted["working_copy"]
    await revisions._prepare_gate_for_activation(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")
    contexts = AgentAutomationContextStore(str(path))
    context = await contexts.read_context(team_id="team_release", agent_id="osa_release", authority=authority)
    assert context["preparation_blockers"] == []
    assert context["automation_protocol_version"] == 2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_policy_revision").fetchone()[0] == 0
    await AgentVersionAutomationStore(str(path)).authorize(
        team_id="team_release",
        agent_id="osa_release",
        grant_id=authority.grant_id,
        principal_id=authority.principal_id,
        required_scope="gate:read",
        repository=authority.repository,
        ref=authority.ref,
    )
    assert (await contexts.read_context(team_id="team_release", agent_id="osa_release", authority=authority))["evaluation_context"][
        "context_hash"
    ] == context["evaluation_context"]["context_hash"]
    key = f"automation:{authority.principal_id}:prepare"
    args = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "actor_user_id": "user_owner",
        "expected_working_copy_id": working.id,
        "expected_working_copy_version": working.row_version,
        "expected_eval_policy_revision_id": None,
        "workspace_id": "ws_eval",
        "idempotency_key": key,
        "automation": AutomationPreparation(authority, proposal.id, context["evaluation_context"]["context_hash"], True),
    }
    with pytest.raises(AgentVersionAutomationError, match="context_conflict"):
        await revisions.prepare_working_copy(**(args | {"automation": replace(args["automation"], expected_context_hash="sha256:" + "0" * 64)}))
    with pytest.raises(AgentVersionAutomationError, match="context_required"):
        await revisions.prepare_working_copy(**(args | {"automation": replace(args["automation"], policy_expectation_provided=False)}))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM agent_eval_policy_revision").fetchone()[0] == 0
        db.execute(
            "CREATE TRIGGER test_preparation_storage_failure BEFORE INSERT ON agent_version_automation_preparation BEGIN SELECT RAISE(ABORT,'test unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test unavailable"):
        await revisions.prepare_working_copy(**args)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM agent_eval_policy_revision").fetchone()[0] == 0
        db.execute("DROP TRIGGER test_preparation_storage_failure")
    prepared = await revisions.prepare_working_copy(**args)
    assert (await revisions.prepare_working_copy(**args)).candidate_id == prepared.candidate_id
    # Preparing created the Policy, so the pipeline's Policy expectation is stale. The context hash covers
    # the Policy too, yet the documented Policy conflict (with the current revision) must win.
    fresh = (await contexts.read_context(team_id="team_release", agent_id="osa_release", authority=authority))["evaluation_context"]
    assert fresh["policy"]["revision_id"]
    stale_key = f"automation:{authority.principal_id}:prepare-stale-policy"
    with pytest.raises(AgentVersionAutomationError, match="agent_eval_policy_version_conflict") as stale_policy:
        await revisions.prepare_working_copy(**(args | {"idempotency_key": stale_key}))
    assert stale_policy.value.details == {"current_policy_revision_id": fresh["policy"]["revision_id"]}
    current_args = args | {
        "idempotency_key": stale_key,
        "expected_eval_policy_revision_id": fresh["policy"]["revision_id"],
        "automation": replace(args["automation"], expected_context_hash=fresh["context_hash"]),
    }
    with pytest.raises(AgentRevisionConflictError, match="agent_release_candidate_already_active") as already_active:
        await revisions.prepare_working_copy(**current_args)
    assert already_active.value.details["active_candidate_id"] == prepared.candidate_id
    assert already_active.value.details["active_candidate_state"] == "prepared"
    assert "Prepare from latest draft" in already_active.value.details["next_step"]
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT prepared_actor_kind,prepared_actor_ref,prepared_automation_grant_id FROM agent_eval_candidate_control WHERE candidate_id=?",
            (prepared.candidate_id,),
        ).fetchone() == ("automation", authority.principal_id, authority.grant_id)
        db.execute("DELETE FROM agent_command_receipt")
    result = await contexts.read_automation_state(
        team_id="team_release", agent_id="osa_release", proposal_id=proposal.id, authority=authority, operation_key_hash=operation_key_hash(key)
    )
    assert result["preparation"]["candidate_id"] == prepared.candidate_id
    assert result["candidate"]["plan_id"] == prepared.plan_id
    assert result["proposal"]["selected_group_ids"] == ["behavior"]
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM agent_version_automation_preparation")


@pytest.mark.asyncio
async def test_lost_authority_fails_finalizing_execution_and_is_recoverable(tmp_path, monkeypatch):
    path, authority, store, _args, execution = await comparison_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agent_eval_comparison_execution SET status='running',started_at=requested_at,row_version=row_version+1 WHERE id=?",
            (execution.id,),
        )
        db.execute(
            "UPDATE agent_eval_comparison_execution SET status='finalizing',finalizing_at=requested_at,row_version=row_version+1 WHERE id=?",
            (execution.id,),
        )
        db.execute(
            "UPDATE agent_version_automation_grant SET revoked_at=?,row_version=row_version+1 WHERE id=?",
            (datetime.now(UTC).isoformat(), authority.grant_id),
        )
    result = await store.finalize(execution.id)
    assert result.execution.status == "failed"
    assert result.execution.failure_code == "agent_automation_grant_denied"
    assert result.snapshot is None


@pytest.mark.asyncio
async def test_claim_and_physical_dispatch_reject_revocation_before_new_work(tmp_path, monkeypatch):
    from bowhead_core.agent_eval_job_store import AgentEvalJobStore
    from bowhead_router.invocation import PhysicalInvocationSpec

    path, authority, _store, _args, execution = await comparison_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agent_version_automation_grant SET revoked_at=?,row_version=row_version+1 WHERE id=?",
            (datetime.now(UTC).isoformat(), authority.grant_id),
        )
    run_id = execution.runs[0].run_id
    assert await AgentEvalJobStore(str(path)).claim_next_job(run_id=run_id, worker_id="denied-worker") is None
    with pytest.raises(AgentVersionAutomationError, match="authority_lost"):
        await AutomationExecutionGuard(str(path), run_id).begin(
            PhysicalInvocationSpec(provider="test", model="test", adapter_contract_key="test:v1", mode="invoke", request_digest="a" * 64)
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_scratch_turn").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_inflight_monitor_cancels_subject_when_grant_is_revoked(tmp_path, monkeypatch):
    import asyncio
    import time

    from bowhead_core.agent_eval_job_store import AgentEvalJobStore
    from bowhead_core.agent_eval_service import AgentEvalService, _EvalCancellationRequested
    from bowhead_core.agent_eval_store import AgentEvalStore

    path, authority, store, _args, execution = await comparison_fixture(tmp_path, monkeypatch)
    run_id = execution.runs[0].run_id
    service = AgentEvalService.__new__(AgentEvalService)
    service.eval_store = AgentEvalStore(str(path))
    service.job_store = AgentEvalJobStore(str(path))
    service.runtime_authority_check = None
    await service.eval_store.mark_run_running(run_id)
    claim = await service.job_store.claim_next_job(run_id=run_id, worker_id="inflight-worker")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def subject():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    operation = asyncio.create_task(
        service._run_with_claim_controls(
            operation=subject(), claim=claim, run_id=run_id, lease_seconds=120, started=time.perf_counter(), max_duration_ms=10000
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    await AgentVersionAutomationStore(str(path)).revoke_grant(
        team_id="team_p", agent_id="osa_policy", grant_id=authority.grant_id, actor_user_id="user_owner", expected_row_version=1
    )
    with pytest.raises(_EvalCancellationRequested):
        await asyncio.wait_for(operation, timeout=2)
    assert cancelled.is_set()
    assert (await store.get(execution.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_bound_grant_cannot_run_owner_prepared_candidate_in_another_space(tmp_path, monkeypatch):
    path, authority, store, args, _execution = await comparison_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO workspace_members(workspace_id,user_id,role,invited_by,joined_at,last_active_at,status)
            SELECT 'ws_denied',user_id,role,invited_by,joined_at,last_active_at,status FROM workspace_members
            WHERE workspace_id='ws_eval' AND user_id='user_owner'""")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE agent_version_automation_grant SET evaluation_workspace_id='ws_denied',row_version=row_version+1 WHERE id=?",
                (authority.grant_id,),
            )
    other = await authority_for(path, workspace_id="ws_denied")
    with pytest.raises(AgentVersionAutomationError, match="workspace_mismatch"):
        await store.start_release(
            **(
                args
                | {"automation": other, "actor_ref": other.principal_id, "automation_grant_id": other.grant_id, "idempotency_key": "different-space"}
            )
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_comparison_execution").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_changed_space_revision_keeps_evidence_stale_classification(tmp_path, monkeypatch):
    path, _authority, store, _args, execution = await comparison_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE workspaces SET revision=revision+1 WHERE id='ws_eval'")
    assert not await AutomationExecutionGuard(str(path), execution.runs[0].run_id).allowed()
    assert (await store.get(execution.id)).failure_code == "agent_eval_workspace_scope_changed"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT readiness,stale_subject FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'").fetchone() == (
            "stale",
            "evidence",
        )


@pytest.mark.asyncio
async def test_grant_with_evaluation_scopes_requires_a_space(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    key, _ = await OpenApiStore(str(path)).create_key(
        team_id="team_release",
        name="CI",
        scopes=["agent_versions:read", "agent_versions:write"],
        expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
    )
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_workspace_required"):
        await AgentVersionAutomationStore(str(path)).create_grant(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            principal_id=key["id"],
            scopes=["proposal:create", "candidate:prepare", "eval:run", "gate:read"],
            repositories=["acme/agent"],
            refs=["refs/heads/*"],
            expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )


@pytest.mark.asyncio
async def test_legacy_grant_without_space_keeps_gate_access_but_must_be_replaced_to_prepare(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    _seed_eval_workspace(path)
    key, _ = await OpenApiStore(str(path)).create_key(
        team_id="team_release",
        name="CI",
        scopes=["agent_versions:read", "agent_versions:write"],
        expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
    )
    automation = AgentVersionAutomationStore(str(path))
    grant = await automation.create_grant(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        principal_id=key["id"],
        scopes=["proposal:create", "gate:read"],
        repositories=["acme/agent"],
        refs=["refs/heads/*"],
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    # Grants created before protocol v2 hold evaluation scopes without a Space; scopes are immutable, so copy the row.
    legacy_id = "avag_" + "1" * 32
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM agent_version_automation_grant WHERE id=?", (grant["id"],)).fetchone())
        row |= {"id": legacy_id, "scopes_json": '["candidate:prepare","eval:run","gate:read","proposal:create"]'}
        db.execute(
            f"INSERT INTO agent_version_automation_grant({','.join(row)}) VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    authority = AutomationAuthority(key["id"], legacy_id, "acme/agent", "refs/heads/main", "user_owner")
    listed = await automation.list_grants(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")
    assert {item["id"]: item["needs_replacement"] for item in listed} == {grant["id"]: False, legacy_id: True}

    context = await AgentAutomationContextStore(str(path)).read_context(team_id="team_release", agent_id="osa_release", authority=authority)
    assert context["automation_protocol_version"] == 2
    assert "agent_automation_workspace_required" in context["preparation_blockers"]

    working = await _working(path)
    revisions = AgentRevisionStore(str(path), validator=_Validator(), eval_gate=AgentEvalReleaseGate())
    proposals = AgentChangeProposalStore(str(path), revision_store=revisions)
    proposal = await proposals.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="git",
        source_ref="ci:legacy",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Candidate prompt."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0], "depends_on": []}],
        created_by_kind="automation",
        created_by_id=authority.principal_id,
        provenance={"automation_grant_id": authority.grant_id, "repository": authority.repository, "ref": authority.ref},
    )
    accepted = await proposals.apply(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        selected_group_ids=["behavior"],
        expected_proposal_row_version=proposal.row_version,
        expected_working_copy_row_version=working.row_version,
    )
    working = accepted["working_copy"]
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_workspace_required"):
        await revisions.prepare_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            idempotency_key=f"automation:{authority.principal_id}:legacy",
            automation=AutomationPreparation(authority, proposal.id, None, False),
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone()[0] == 0
