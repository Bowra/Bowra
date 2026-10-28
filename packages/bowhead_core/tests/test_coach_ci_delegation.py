"""Internal CI consent and physical budgets survive ordinary recovery paths."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.agent_automation_execution import AutomationExecutionGuard
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore
from bowhead_core.agent_eval_comparison import AgentEvalComparisonStore
from bowhead_core.agent_eval_job_store import AgentEvalJobStore
from bowhead_core.agent_eval_product import AgentEvalReleaseGate
from bowhead_core.agent_eval_store import AgentEvalStore
from bowhead_core.agent_revision_store import AgentRevisionStore
from bowhead_core.coach_ci_delegation import CoachCIDelegations, CoachCIError
from bowhead_router.invocation import PhysicalInvocationSpec, begin_physical_invocation, enclosing_physical_guard_scope, physical_invocation_scope
from bowhead_router.types import TokenUsage
from test_agent_change_proposal_store import _working
from test_agent_revision_store import _db, _seed, _seed_eval_workspace, _Validator

pytestmark = pytest.mark.integration
OWNER = {"team_id": "team_release", "agent_id": "osa_release", "actor_user_id": "user_owner"}
SPEC = PhysicalInvocationSpec("test", "model", "test:v1", "invoke", "a" * 64)


async def allow(_db):
    pass


async def fixture(tmp_path, monkeypatch, *, calls=2):
    monkeypatch.setenv("FEATURE_AGENT_EVAL", "1")
    path = _db(tmp_path)
    _seed(path)
    _seed_eval_workspace(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES('s_coach','user_owner','ws_eval','2026-10-03','2026-10-03')"
        )
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_coach','s_coach',1,'Improve this Agent','2026-10-03')")
    working = await _working(path)
    revisions = AgentRevisionStore(str(path), validator=_Validator(), eval_gate=AgentEvalReleaseGate())
    proposals = AgentChangeProposalStore(str(path), revision_store=revisions)
    proposal = await proposals.create(
        team_id=OWNER["team_id"],
        agent_id=OWNER["agent_id"],
        effective_owner_user_id=OWNER["actor_user_id"],
        source_type="coach",
        source_ref="coach:t_coach:one",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Improved instructions."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
    )
    await proposals.apply(
        **OWNER,
        proposal_id=proposal.id,
        selected_group_ids=["behavior"],
        expected_proposal_row_version=proposal.row_version,
        expected_working_copy_row_version=working.row_version,
    )
    store = CoachCIDelegations(str(path))
    args = {
        "team_id": OWNER["team_id"],
        "agent_id": OWNER["agent_id"],
        "owner_user_id": OWNER["actor_user_id"],
        "workspace_id": "ws_eval",
        "session_id": "s_coach",
        "turn_id": "t_coach",
        "proposal_id": proposal.id,
        "request_id": "one",
        "max_provider_calls": calls,
        "max_comparisons": 1,
        "duration_seconds": 600,
        "authorize": allow,
    }
    pending = await store.request(**args)
    return path, store, revisions, args, pending


async def prepare(store, revisions, pending):
    authorized = await store.authorize(**OWNER, delegation_id=pending["delegation_id"], expected_version=1, authorize=allow)
    context = authorized["context"]
    args = dict(
        **OWNER,
        expected_working_copy_id=context["working_copy_id"],
        expected_working_copy_version=context["working_copy_version"],
        expected_eval_policy_revision_id=context["policy"]["id"] if context["policy"] else None,
        workspace_id="ws_eval",
        idempotency_key="coach:prepare",
        coach_delegation_id=pending["delegation_id"],
    )
    prepared = await revisions.prepare_working_copy(**args)
    assert (await revisions.prepare_working_copy(**args)).candidate_id == prepared.candidate_id
    return prepared


async def start(path, pending, prepared):
    with sqlite3.connect(path) as db:
        control_version = db.execute(
            "SELECT row_version FROM agent_eval_candidate_control WHERE candidate_id=?", (prepared.candidate_id,)
        ).fetchone()[0]
        revision_ids = [r[0] for r in db.execute("SELECT id FROM team_subagent_revision WHERE subagent_id='osa_release'")]
    store = AgentEvalComparisonStore(path, runtime_check=allow)
    args = dict(
        **OWNER,
        candidate_id=prepared.candidate_id,
        candidate_row_version=prepared.candidate_row_version,
        candidate_hash=prepared.candidate_hash,
        plan_id=prepared.plan_id,
        plan_hash=prepared.plan_hash,
        expected_control_row_version=control_version,
        model_config_hashes=dict.fromkeys(revision_ids, "sha256:" + "a" * 64),
        idempotency_key="coach:compare",
        actor_ref=OWNER["actor_user_id"],
        coach_delegation_id=pending["delegation_id"],
    )
    execution = await store.start_release(**args)
    return store, args, execution


async def guard_for(path, execution):
    run_id = execution.runs[0].run_id
    await AgentEvalStore(str(path)).mark_run_running(run_id)
    jobs = AgentEvalJobStore(path)
    claim = await jobs.claim_next_job(run_id=run_id, worker_id="test")
    assert claim is not None
    await jobs.start_scratch_turn(
        job_id=claim.job.job_id, lease_token=claim.lease_token, session_id="s_eval_test", turn_id="t_eval_test", user_text="Test"
    )
    return AutomationExecutionGuard(str(path), run_id, job_id=claim.job.job_id, lease_generation=claim.job.lease_generation, runtime_check=allow)


async def test_consent_does_not_start_work_and_prepare_binds_exact_accepted_proposal(tmp_path, monkeypatch):
    path, store, revisions, args, pending = await fixture(tmp_path, monkeypatch)
    assert pending["state"] == "requested" and pending["executions"] == [] and pending["preparation"] is None
    assert (await store.request(**args))["delegation_id"] == pending["delegation_id"]
    with pytest.raises(CoachCIError, match="idempotency_conflict"):
        await store.request(**(args | {"max_provider_calls": 3}))
    prepared = await prepare(store, revisions, pending)
    recovered = await store.get(**OWNER, delegation_id=pending["delegation_id"])
    assert recovered["preparation"]["candidate_id"] == prepared.candidate_id
    assert recovered["preparation"]["accepted_working_copy_hash"] == pending["context"]["working_copy_hash"]
    assert not recovered["release_authorized"]
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE coach_ci_delegation SET max_provider_calls=100,row_version=row_version+1")


async def test_budget_is_shared_across_concurrent_dispatches_and_not_reset_by_replay(tmp_path, monkeypatch):
    path, store, revisions, _args, pending = await fixture(tmp_path, monkeypatch, calls=1)
    prepared = await prepare(store, revisions, pending)
    comparisons, args, execution = await start(path, pending, prepared)
    replay = await comparisons.start_release(**(args | {"model_config_hashes": {"removed": "sha256:changed"}}))
    assert replay.id == execution.id and replay.coach_delegation_id == pending["delegation_id"]
    with pytest.raises(CoachCIError, match="comparison_budget_exhausted"):
        await comparisons.start_release(**(args | {"idempotency_key": "coach:new-comparison"}))
    guard = await guard_for(path, execution)
    receipts = await asyncio.gather(guard.begin(SPEC), guard.begin(SPEC), return_exceptions=True)
    tickets = [item for item in receipts if not isinstance(item, BaseException)]
    assert len(tickets) == 1
    assert sum(isinstance(item, CoachCIError) for item in receipts) == 1
    await tickets[0].complete(usage=TokenUsage(input_tokens=7, output_tokens=3), response_digest="b" * 64)
    recovered = await store.get(**OWNER, delegation_id=pending["delegation_id"])
    assert recovered["state"] == "exhausted" and recovered["remaining_provider_calls"] == 0
    assert recovered["usage"][0]["tokens_input"] == 7
    assert not await guard.allowed()
    assert (await comparisons.finalize(execution.id)).snapshot is None


@pytest.mark.parametrize("loss", ["owner", "space", "chat", "cancel", "unknown", "lease", "reclaimed", "runtime", "expiry"])
async def test_authority_loss_and_unknown_dispatch_never_produce_green(tmp_path, monkeypatch, loss):
    path, store, revisions, _args, pending = await fixture(tmp_path, monkeypatch)
    prepared = await prepare(store, revisions, pending)
    comparisons, _args, execution = await start(path, pending, prepared)
    guard = await guard_for(path, execution)
    ticket = await guard.begin(SPEC)
    if loss == "cancel":
        await store.revoke(**OWNER, delegation_id=pending["delegation_id"])
    elif loss == "unknown":
        await ticket.unknown(reason_code="transport_interrupted")
    elif loss == "runtime":

        async def denied(_run_id):
            raise CoachCIError("coach_ci_entitlement_denied")

        guard.runtime_check = denied
    elif loss in {"lease", "expiry"}:
        import bowhead_core.agent_automation_execution as guards

        class Clock:
            @staticmethod
            def now(_tz):
                return datetime.now(UTC) + timedelta(seconds=180 if loss == "lease" else 601)

        monkeypatch.setattr(guards, "datetime", Clock)
    else:
        with sqlite3.connect(path) as db:
            if loss == "owner":
                db.execute("UPDATE team_subagent SET owner_user_id='user_admin' WHERE id='osa_release'")
            elif loss == "space":
                db.execute("UPDATE workspace_members SET status='removed' WHERE workspace_id='ws_eval'")
            elif loss == "chat":
                db.execute("UPDATE sessions SET deleted_at='2026-10-03' WHERE id='s_coach'")
            else:
                db.execute("UPDATE agent_eval_case_job SET status='leased',lease_generation=lease_generation+1 WHERE id=?", (guard.job_id,))
    assert not await guard.allowed()
    assert (await comparisons.finalize(execution.id)).snapshot is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM coach_ci_invocation").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM agent_eval_snapshot").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_eval_run WHERE status IN ('queued','running')").fetchone()[0] == 0
    if loss != "unknown":
        assert await ticket.complete(usage=TokenUsage(input_tokens=9, output_tokens=1), response_digest="c" * 64)


async def test_transport_hooks_share_budget_after_safe_rejection_and_restart(tmp_path, monkeypatch):
    path, store, revisions, request_args, pending = await fixture(tmp_path, monkeypatch)
    prepared = await prepare(store, revisions, pending)
    _comparisons, _args, execution = await start(path, pending, prepared)
    guard = await guard_for(path, execution)
    with enclosing_physical_guard_scope(guard), physical_invocation_scope(None):
        first = await begin_physical_invocation(
            provider="test", model="primary", adapter_contract_key="test:v1", mode="invoke", request={"prompt": "test"}
        )
    await first.confirmed_not_applied(evidence_code="provider_rejected", evidence_digest="a" * 64)
    restarted = AutomationExecutionGuard(str(path), guard.run_id, job_id=guard.job_id, lease_generation=guard.lease_generation, runtime_check=allow)

    class ChildHook:
        async def begin(self, _spec):
            return self

        async def complete(self, **_kwargs):
            return True

    with enclosing_physical_guard_scope(restarted), physical_invocation_scope(ChildHook()):
        second = await begin_physical_invocation(
            provider="test", model="fallback", adapter_contract_key="test:v1", mode="stream", request={"prompt": "test"}
        )
        await second.complete(usage=TokenUsage(input_tokens=3, output_tokens=2), response_digest="c" * 64)
        with pytest.raises(CoachCIError, match="provider_budget_exhausted"):
            await begin_physical_invocation(
                provider="test", model="judge", adapter_contract_key="test:v1", mode="invoke", request={"prompt": "score"}
            )
    saved = await CoachCIDelegations(str(path)).get(**OWNER, delegation_id=pending["delegation_id"])
    assert saved["remaining_provider_calls"] == 0
    assert {item["state"] for item in saved["usage"]} == {"not_applied", "completed"}
    assert (await store.request(**request_args))["remaining_provider_calls"] == 0


async def test_unconsented_and_changed_baselines_cannot_prepare(tmp_path, monkeypatch):
    path, store, revisions, _args, pending = await fixture(tmp_path, monkeypatch)
    context = pending["context"]
    with pytest.raises(CoachCIError, match="not_authorized"):
        await revisions.prepare_working_copy(
            **OWNER,
            expected_working_copy_id=context["working_copy_id"],
            expected_working_copy_version=context["working_copy_version"],
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            idempotency_key="unconsented",
            coach_delegation_id=pending["delegation_id"],
        )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1 WHERE id=?", (context["working_copy_id"],))
    with pytest.raises(CoachCIError, match="context_changed"):
        await store.authorize(**OWNER, delegation_id=pending["delegation_id"], expected_version=1, authorize=allow)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent_release_candidate").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_eval_comparison_execution").fetchone()[0] == 0


@pytest.mark.parametrize("runtime_available", [True, False])
async def test_finalization_rechecks_runtime_after_all_results_are_recorded(tmp_path, monkeypatch, runtime_available):
    path, _store, revisions, _args, pending = await fixture(tmp_path, monkeypatch)
    prepared = await prepare(_store, revisions, pending)
    comparisons, _args, execution = await start(path, pending, prepared)
    evaluation = AgentEvalStore(str(path))
    for link in execution.runs:
        await evaluation.mark_run_running(link.run_id)
        with sqlite3.connect(path) as db:
            jobs = db.execute("SELECT case_revision_id,attempt FROM agent_eval_case_job WHERE run_id=?", (link.run_id,)).fetchall()
        for case_id, attempt in jobs:
            await evaluation.record_case_result(
                run_id=link.run_id, case_revision_id=case_id, attempt=attempt, status="passed", deterministic={"passed": True}
            )
            with sqlite3.connect(path) as db:
                db.execute(
                    """INSERT INTO agent_eval_attempt_identity(run_id,case_revision_id,attempt,subject_provider_ref,
                           subject_provider_hash,reproducibility,created_at) VALUES(?,?,?,'model-config',?,'exact',?)""",
                    (link.run_id, case_id, attempt, "sha256:" + "a" * 64, datetime.now(UTC).isoformat()),
                )
        await evaluation.mark_run_terminal(link.run_id, status="passed", summary={"schema_version": 1})
    calls = []

    async def runtime_check(run_id):
        calls.append(run_id)
        if not runtime_available:
            raise CoachCIError("coach_ci_entitlement_denied")

    comparisons.runtime_check = runtime_check
    result = await comparisons.finalize(execution.id)
    assert calls
    if runtime_available:
        assert result.execution.status == "completed" and result.snapshot is not None
        assert set(calls) == {item.run_id for item in execution.runs}
    else:
        assert result.execution.status == "cancelled" and result.snapshot is None


@pytest.mark.parametrize("change", ["validation", "definition", "listing", "audience"])
async def test_content_receipt_distinguishes_safety_bookkeeping_from_edits(tmp_path, monkeypatch, change):
    path, store, _revisions, args, _pending = await fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        if change == "validation":
            db.execute('UPDATE team_subagent_working_copy SET row_version=row_version+1,validation_json=\'{"status":"pass"}\'')
        elif change == "definition":
            db.execute(
                "UPDATE team_subagent_working_copy SET row_version=row_version+1,definition_json=json_set(definition_json,'$.system_prompt','Different instructions')"
            )
        elif change == "listing":
            db.execute(
                "UPDATE team_subagent_working_copy SET row_version=row_version+1,listing_json=json_set(listing_json,'$.display_name','Different name')"
            )
        else:
            db.execute(
                'UPDATE team_subagent_working_copy SET row_version=row_version+1,audience_json=\'{"schema_version":1,"effective_mode":"teamwide","user_ids":[],"group_ids":[]}\''
            )
    if change == "validation":
        fresh = await store.request(**(args | {"request_id": "new-consent"}))
        assert fresh["current_admission"] == "consent_required"
        assert fresh["context"]["working_copy_version"] > fresh["context"]["proposal"]["result_working_copy_version"]
    else:
        with pytest.raises(CoachCIError, match="accepted_baseline_changed"):
            await store.request(**(args | {"request_id": "new-consent"}))
