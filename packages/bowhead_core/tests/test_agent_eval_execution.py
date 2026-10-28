"""M3-10 Agent Eval execution, lease, effect, and scorer contracts."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_eval_effects import (
    EvalEffectAdapterRegistry,
    EvalEffectOutcome,
    EvalEffectRouter,
    EvalTraceCollector,
)
from bowhead_core.agent_eval_job_store import AgentEvalJobStore, AgentEvalJobStoreError
from bowhead_core.agent_eval_judge import TeamRouterEvalJudge
from bowhead_core.agent_eval_scorers import (
    BudgetTimeoutScorer,
    EffectPolicyScorer,
    deterministic_result,
    required_tools_not_run_in_eval,
    tools_not_run_in_eval,
)
from bowhead_core.agent_eval_service import (
    AgentEvalService,
    AgentEvalServiceError,
    EvalAttemptOutput,
    EvalExecutionLimits,
)
from bowhead_core.agent_eval_store import AgentEvalStore, EvalCaseDraft, EvalRunSubject
from bowhead_core.agent_eval_subagent_executor import ExactSubagentEvalExecutor
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    AgentExecutionBindingStore,
)
from bowhead_core.evalkit.cases import EvalCase
from bowhead_core.evalkit.scorers import ScoreResult
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentResult
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.terminal_effect_contract import (
    TOOL_EFFECT_READ_ONLY,
    ToolEffectIdempotency,
    ToolEffectSpec,
)
from bowhead_core.terminal_protocol import EffectClass
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.user_store import UserStore

from bowhead_core import agent_eval_service as agent_eval_service_module
from bowhead_router import LLMResponse, TokenUsage, ToolSchema


def _content_hash(definition_json: str, listing_json: str) -> str:
    """Safety reports hash {definition, listing} -- the writer's formula.

    Seeding `_hash_text(definition)` here matched a join condition no production
    writer could satisfy, so the fixture agreed with the reader instead of with
    what the code actually stores.
    """
    import json as _json

    return _hash_text(
        _json.dumps(
            {"definition": _json.loads(definition_json), "listing": _json.loads(listing_json)},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


_HASH_A = "sha256:" + "1" * 64
_HASH_B = "sha256:" + "2" * 64
_HASH_C = "sha256:" + "3" * 64


async def _fixture(tmp_path: Path, *, plugin_tool_ref: str | None = None):
    db_path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    users = UserStore(db_path)
    team = await users.create_team(name="Eval execution", max_seats=3)
    owner = await users.create_user(team_id=team.id, email="eval-execution@example.com")
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Eval scratch",
        creator_user_id=owner.id,
    )
    timestamp = "2026-09-05T00:00:00+00:00"
    agent_id = "osa_eval_execution"
    revision_id = "arv_eval_execution"
    release_id = "arl_eval_execution"
    definition = '{"allowed_tools":["read_fixture"],"max_iterations":3,"name":"eval-agent","system_prompt":"Analyze the fixture."}'
    if plugin_tool_ref is not None:
        import json

        plugin = await TeamPluginStore(str(db_path)).create(
            team_id=team.id,
            actor_user_id=owner.id,
            name="Eval resources",
            description="Shared resources",
            tool_refs=[plugin_tool_ref],
            skill_grants=[],
        )
        definition_value = json.loads(definition)
        definition_value["capability_ids"] = [plugin["id"]]
        definition = json.dumps(definition_value, sort_keys=True, separators=(",", ":"))
    listing = '{"description":"Eval fixture","display_name":"Eval Agent","name":"eval-agent"}'
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,'[]','[]','team_wide',?,'enabled',?,?)
            """,
            (
                agent_id,
                team.id,
                "eval-agent",
                "Eval Agent",
                "Version-bound Eval fixture",
                owner.id,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,
              listing_json,definition_hash,dependency_fingerprint,
              capability_fingerprint,risk_compiler_version,risk_json,
              created_by,created_at
            ) VALUES (?,?,?,1,'authored',?,?,?,?,?,'risk-v1','{}',?,?)
            """,
            (
                revision_id,
                team.id,
                agent_id,
                definition,
                listing,
                _hash_text(definition),
                _HASH_B,
                _HASH_C,
                owner.id,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,
              status,deterministic_json,semantic_json,safe_summary_json,
              created_by,created_at
            ) VALUES ('asr_eval_execution',?,?,'revision',?,?,?,'risk-v1','semantic-v1',
                      'pass','{}','{}','{}',?,?)
            """,
            (
                team.id,
                agent_id,
                revision_id,
                _content_hash(definition, listing),
                _HASH_B,
                owner.id,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,
              activated_at
            ) VALUES (?,?,?,1,?,'{}',?,?,?,'{}','asr_eval_execution',?,?)
            """,
            (
                release_id,
                team.id,
                agent_id,
                revision_id,
                _HASH_A,
                _HASH_C,
                _HASH_B,
                owner.id,
                timestamp,
            ),
        )
        connection.commit()
    subject = EvalRunSubject(
        team_id=team.id,
        agent_id=agent_id,
        agent_source_kind="team",
        revision_ref=revision_id,
        release_id=release_id,
        capability_fingerprint=_HASH_C,
        dependency_fingerprint=_HASH_B,
        model_config_hash=_HASH_A,
    )
    eval_store = AgentEvalStore(db_path)
    suite = await eval_store.create_suite(
        team_id=team.id,
        agent_id=agent_id,
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Fixture quality",
        visibility="owner",
        actor_user_id=owner.id,
    )
    revision = await eval_store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "grounded fixture answer"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key="grounded",
                input={"task": "Read revenue from the fixture"},
                fixture_ref={
                    "kind": "dataset",
                    "ref": "dataset_eval",
                    "version": "v1",
                    "content_hash": _HASH_A,
                    "classification": "internal",
                },
                expected_contract={"required_citations": 1},
                scorer_config={
                    "required_tools": ["read_fixture"],
                    "max_tool_calls": 2,
                },
                effect_policy={"mode": "read_only_fixture"},
            ),
        ),
        actor_user_id=owner.id,
    )
    await eval_store.update_suite(
        suite_id=suite.suite_id,
        actor_user_id=owner.id,
        expected_row_version=2,
        state="active",
    )
    run = await eval_store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:execution:v1",
        actor_user_id=owner.id,
    )
    return db_path, users, team, owner, workspace_id, eval_store, revision, run


def _registry(production_calls: list[str]) -> ToolRegistry:
    async def _production_read(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        production_calls.append("read_fixture")
        return {"revenue": 999}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="read_fixture",
                description="Read fixture",
                input_schema={"type": "object"},
            ),
            handler=_production_read,
            effect_spec=TOOL_EFFECT_READ_ONLY,
        )
    )
    return registry


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_eval",
        member_uid="",
        user_id="usr_eval",
        turn_id="turn_eval",
        datarc=None,  # type: ignore[arg-type]
    )


async def _start_eval_scratch(
    *,
    db_path: Path,
    eval_store: AgentEvalStore,
    run: Any,
    workspace_id: str,
    suffix: str,
) -> Any:
    jobs = AgentEvalJobStore(db_path)
    await jobs.materialize_jobs(
        run_id=run.run_id,
        workspace_id=workspace_id,
        attempts=1,
        max_concurrency=1,
        max_duration_ms=120_000,
        max_cost_microusd=5_000_000,
    )
    await eval_store.mark_run_running(run.run_id)
    claim = await jobs.claim_next_job(
        run_id=run.run_id,
        worker_id=f"test.{suffix}",
        lease_seconds=30,
    )
    assert claim is not None
    return await jobs.start_scratch_turn(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        session_id=f"s_eval_{suffix}",
        turn_id=f"t_eval_{suffix}",
        user_text="test",
    )


@pytest.mark.anyio
async def test_effect_router_never_calls_production_handlers() -> None:
    production_calls: list[str] = []

    class Fixture:
        async def resolve(self, **kwargs: Any) -> EvalEffectOutcome:
            return EvalEffectOutcome(
                result={"revenue": 500},
                evidence_ref="fixture:dataset_eval:v1",
                authorized_numbers=(500,),
                citations=("dataset_eval",),
            )

    trace = EvalTraceCollector(max_tool_calls=3)
    router = EvalEffectRouter(
        effect_policy={"mode": "read_only_fixture"},
        fixture_ref={"kind": "dataset", "ref": "dataset_eval", "version": "v1"},
        fixture_resolver=Fixture(),
        adapter_registry=EvalEffectAdapterRegistry(),
        trace=trace,
    )
    invocation = await router.build_safe_view(_registry(production_calls)).execute(
        "read_fixture",
        {"query": "revenue"},
        _ctx(),
    )
    assert invocation.result == {"revenue": 500}
    assert production_calls == []
    assert trace.to_scorer_trace()["production_handler_called"] is False
    assert trace.to_scorer_trace()["authorized_numbers"] == [500.0]


@pytest.mark.anyio
async def test_read_only_tool_without_fixture_runs_live_and_authorizes_its_numbers() -> None:
    # Owner cases have no fixtures, so a read-only Tool must run for real or no data Agent can be evaluated.
    production_calls: list[str] = []
    trace = EvalTraceCollector(max_tool_calls=3)
    router = EvalEffectRouter(
        effect_policy={"mode": "no_effects"},
        fixture_ref={},
        fixture_resolver=None,
        adapter_registry=EvalEffectAdapterRegistry(),
        trace=trace,
    )
    invocation = await router.build_safe_view(_registry(production_calls)).execute("read_fixture", {"query": "revenue"}, _ctx())

    assert production_calls == ["read_fixture"]
    assert invocation.result == {"revenue": 999}
    scorer_trace = trace.to_scorer_trace()
    assert scorer_trace["tool_calls"][0]["route"] == "live_read_only"
    assert 999.0 in scorer_trace["authorized_numbers"]
    case = EvalCase(id="live", input="x", meta={"scorer_config": {}})
    assert EffectPolicyScorer().score(case, "answer", trace=scorer_trace).passed is True


def test_effect_policy_reports_tools_not_run_in_eval_without_failing() -> None:
    case = EvalCase(id="env", input="x", meta={"scorer_config": {}})
    not_run = {"tool_name": "send_mail", "route": "denied", "denial_code": "eval_effect_adapter_missing"}
    result = EffectPolicyScorer().score(case, "answer", trace={"tool_calls": [not_run]})
    assert result.passed is True
    assert result.data["not_run_in_eval"] == ["send_mail"]
    # A real permission denial is still the Agent's behavior and still fails.
    denied = {"tool_name": "restricted_read", "route": "denied", "denial_code": "tool_permission_denied"}
    assert EffectPolicyScorer().score(case, "answer", trace={"tool_calls": [not_run, denied]}).passed is False


@pytest.mark.anyio
async def test_write_tool_requires_exact_fake_adapter() -> None:
    production_calls: list[str] = []

    async def _production_write(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        production_calls.append("send")
        return {"sent": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(name="send", description="Send", input_schema={"type": "object"}),
            handler=_production_write,
            needs_approval=True,
            effect_spec=ToolEffectSpec(
                effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                idempotency=ToolEffectIdempotency.STABLE_TOKEN,
                effect_contract_key="tool_external/v1",
            ),
        )
    )
    trace = EvalTraceCollector()
    router = EvalEffectRouter(
        effect_policy={"mode": "fake_only"},
        fixture_ref={},
        fixture_resolver=None,
        adapter_registry=EvalEffectAdapterRegistry(),
        trace=trace,
    )
    invocation = await router.build_safe_view(registry).execute("send", {"to": "nobody"}, _ctx())
    assert invocation.result == {
        "error": "eval_effect_adapter_missing",
        "error_code": "eval_effect_adapter_missing",
        "ok": False,
    }
    assert production_calls == []
    assert trace.approval_attempts == 1


@pytest.mark.anyio
async def test_effect_view_records_registry_permission_denial() -> None:
    async def _production(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        raise AssertionError("permission-denied Eval must not call the handler")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="restricted_read",
                description="Restricted",
                input_schema={"type": "object"},
            ),
            handler=_production,
            effect_spec=TOOL_EFFECT_READ_ONLY,
            allowed_for_roles=frozenset({"lead"}),
        )
    )
    trace = EvalTraceCollector()
    router = EvalEffectRouter(
        effect_policy={"mode": "read_only_fixture"},
        fixture_ref={"kind": "dataset", "ref": "dataset_eval", "version": "v1"},
        fixture_resolver=None,
        adapter_registry=EvalEffectAdapterRegistry(),
        trace=trace,
    )
    context = _ctx()
    context.role = "analyst"
    invocation = await router.build_safe_view(registry).execute(
        "restricted_read",
        {},
        context,
    )
    assert invocation.error is not None
    assert trace.tool_calls[0]["denial_code"] == "tool_permission_denied"


@pytest.mark.anyio
async def test_effect_view_converts_broken_adapter_to_finite_denial() -> None:
    adapters = EvalEffectAdapterRegistry()

    async def _broken_adapter(
        tool_name: str,
        arguments: Any,
        ctx: ToolExecutionContext,
    ) -> EvalEffectOutcome:
        raise RuntimeError("adapter leaked an internal failure")

    adapters.register("fixture.read@1", _broken_adapter)
    trace = EvalTraceCollector()
    router = EvalEffectRouter(
        effect_policy={
            "mode": "fake_only",
            "adapter_refs": {"read_fixture": "fixture.read@1"},
        },
        fixture_ref={},
        fixture_resolver=None,
        adapter_registry=adapters,
        trace=trace,
    )
    invocation = await router.build_safe_view(_registry([])).execute(
        "read_fixture",
        {},
        _ctx(),
    )
    assert invocation.result == {
        "error": "eval_effect_adapter_failed",
        "error_code": "eval_effect_adapter_failed",
        "ok": False,
    }
    assert trace.tool_calls[0]["denial_code"] == "eval_effect_adapter_failed"


def test_subagent_return_declares_scratch_local_no_effect_contract() -> None:
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    spec = registry.get("subagent_return")
    assert spec.effect_spec is not None
    assert spec.effect_spec.effect_class is EffectClass.NONE


def test_suite_config_cannot_remove_mandatory_critical_scorers() -> None:
    result = deterministic_result(
        [
            ScoreResult(name="effect_policy", passed=False, score=0.0),
            ScoreResult(name="citation", passed=False, score=0.0),
        ],
        scorer_config={"critical": ["citation"]},
    )
    assert result["critical_failure"] is True
    by_name = {row["name"]: row for row in result["scorers"]}
    assert by_name["effect_policy"]["critical"] is True


def test_scratch_control_return_does_not_consume_zero_tool_budget() -> None:
    trace = EvalTraceCollector(max_tool_calls=0)
    trace.record(
        tool_name="subagent_return",
        arguments={"payload": {}},
        effect_class=EffectClass.NONE.value,
        route="scratch_control",
        duration_ms=1,
    )
    assert trace.tool_budget_exceeded is False
    # A call the environment could not run did nothing, so it is free as well.
    trace.record(
        tool_name="send_mail",
        arguments={},
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE.value,
        route="denied",
        duration_ms=1,
        denial_code="eval_effect_adapter_missing",
    )
    assert trace.tool_budget_exceeded is False
    # A Tool call that actually ran still trips the zero budget, so the contract case keeps its meaning.
    with pytest.raises(Exception, match="eval_tool_budget_exceeded"):
        trace.record(
            tool_name="bi_query",
            arguments={},
            effect_class=EffectClass.READ_ONLY.value,
            route="live_read_only",
            duration_ms=1,
        )
    assert trace.tool_budget_exceeded is True

    case = EvalCase(id="zero", input="x", meta={"scorer_config": {"max_tool_calls": 0}})
    ok = BudgetTimeoutScorer().score(
        case,
        "answer",
        trace={"tool_calls": [{"tool_name": "subagent_return", "route": "scratch_control"}]},
    )
    assert ok.passed is True
    bad = BudgetTimeoutScorer().score(
        case,
        "answer",
        trace={"tool_calls": [{"tool_name": "bi_query", "route": "live_read_only"}]},
    )
    assert bad.passed is False
    not_run = BudgetTimeoutScorer().score(
        case,
        "answer",
        trace={"tool_calls": [{"tool_name": "send_mail", "route": "denied", "denial_code": "eval_effect_adapter_missing"}]},
    )
    assert not_run.passed is True


def test_budget_scorer_preserves_explicit_zero_tool_limit() -> None:
    result = BudgetTimeoutScorer().score(
        EvalCase(
            id="no-tools",
            input="Answer without tools",
            meta={"scorer_config": {"max_tool_calls": 0}},
        ),
        "answer",
        trace={"tool_calls": [{"tool_name": "unexpected"}]},
    )

    assert result.passed is False
    assert result.data["limits"]["tool_calls"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "500"])
def test_effect_outcome_rejects_non_finite_or_non_numeric_evidence(value: Any) -> None:
    with pytest.raises(ValueError, match="authorized number"):
        EvalEffectOutcome(
            result={},
            evidence_ref="fixture:test:v1",
            authorized_numbers=(value,),  # type: ignore[arg-type]
        )


@pytest.mark.anyio
async def test_job_lease_scratch_scope_and_settlement_are_durable(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, revision, run = await _fixture(tmp_path)
    jobs = AgentEvalJobStore(db_path)
    materialized = await jobs.materialize_jobs(
        run_id=run.run_id,
        workspace_id=workspace_id,
        attempts=2,
        max_concurrency=1,
        max_duration_ms=120_000,
        max_cost_microusd=5_000_000,
    )
    assert len(materialized) == 2
    await eval_store.mark_run_running(run.run_id)
    claim = await jobs.claim_next_job(run_id=run.run_id, worker_id="worker.1", lease_seconds=30)
    assert claim is not None
    assert await jobs.claim_next_job(run_id=run.run_id, worker_id="worker.2", lease_seconds=30) is None
    sessions = SessionStore(str(db_path))
    turn_id = "t_eval_job"
    link = await jobs.start_scratch_turn(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        session_id="s_eval_job",
        turn_id=turn_id,
        user_text="test",
    )
    assert link.revision_ref == run.subject.revision_ref
    scratch = await sessions.get_session(link.session_id)
    assert scratch is not None and scratch.origin == "agent_eval"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT terminal_protocol_version FROM turns WHERE id=?", (turn_id,)).fetchone() == (None,)
    with pytest.raises(AgentEvalJobStoreError, match="agent_eval_job_lease_fenced"):
        await jobs.settle_job(
            job_id=claim.job.job_id,
            lease_token="wrong-token-that-is-long-enough",
            status="passed",
            deterministic={"critical_failure": False, "passed": True, "scorers": []},
        )
    settled = await jobs.settle_job(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        status="passed",
        deterministic={"critical_failure": False, "passed": True, "scorers": []},
        trace_evidence_ref=f"turn:{turn_id}",
    )
    assert settled.status == "completed"
    results = await eval_store.list_case_results(run.run_id)
    assert results[0].case_revision_id == revision.cases[0].case_revision_id
    assert await sessions.list_sessions(user_id=owner.id, workspace_id=workspace_id) == []


@pytest.mark.anyio
async def test_older_run_cannot_replace_newer_evidence_projection(tmp_path: Path) -> None:
    _db_path, _users, _team, owner, _workspace_id, eval_store, revision, older = await _fixture(tmp_path)
    await eval_store.mark_run_running(older.run_id)
    await eval_store.record_case_result(
        run_id=older.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="passed",
        deterministic={"critical_failure": False, "passed": True, "scorers": []},
    )
    await eval_store.mark_run_terminal(
        older.run_id,
        status="passed",
        summary={"critical_failure": False},
    )
    newer = await eval_store.create_run(
        subject=older.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:execution:newer",
        actor_user_id=owner.id,
    )
    await eval_store.mark_run_running(newer.run_id)
    current = await eval_store.project_evidence(
        team_id=older.subject.team_id,
        agent_source_kind="team",
        agent_id=older.subject.agent_id,
        suite_id=revision.suite_id,
        subject_fingerprint=newer.subject_fingerprint,
        state="running",
        current_run_id=newer.run_id,
    )
    unchanged = await eval_store.project_evidence(
        team_id=older.subject.team_id,
        agent_source_kind="team",
        agent_id=older.subject.agent_id,
        suite_id=revision.suite_id,
        subject_fingerprint=older.subject_fingerprint,
        state="valid",
        current_run_id=older.run_id,
        expected_row_version=current.row_version,
    )
    assert unchanged.current_run_id == newer.run_id
    assert unchanged.state == "running"
    assert unchanged.row_version == current.row_version


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["normal", "agent_eval"])
async def test_eval_binding_rejects_unlinked_agent_eval_turn(tmp_path: Path, origin: str) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)
    await AgentEvalJobStore(db_path).materialize_jobs(
        run_id=run.run_id,
        workspace_id=workspace_id,
        attempts=1,
        max_concurrency=1,
        max_duration_ms=120_000,
        max_cost_microusd=5_000_000,
    )
    await eval_store.mark_run_running(run.run_id)
    sessions = SessionStore(str(db_path))
    ad_hoc = await sessions.ensure_session(
        session_id=None,
        workspace_id=workspace_id,
        user_id=owner.id,
        origin=origin,
    )
    await sessions.record_turn(
        turn_id="t_eval_unlinked",
        session_id=ad_hoc.id,
        user_text="not linked to an Eval job",
    )
    with pytest.raises(AgentExecutionBindingError, match="agent_eval_turn_scope_invalid"):
        await AgentExecutionBindingStore(db_path).admit_eval_execution(
            eval_run_id=run.run_id,
            user_id=owner.id,
            turn_id="t_eval_unlinked",
            dispatch_key="eval:unlinked",
        )


@pytest.mark.anyio
async def test_eval_uses_latest_plugin_on_new_execution(tmp_path: Path) -> None:
    db_path, _users, team, owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path, plugin_tool_ref="web_search")
    scratch = await _start_eval_scratch(
        db_path=db_path,
        eval_store=eval_store,
        run=run,
        workspace_id=workspace_id,
        suffix="capability",
    )
    bindings = AgentExecutionBindingStore(db_path)
    first = await bindings.admit_eval_execution(
        eval_run_id=run.run_id,
        user_id=owner.id,
        turn_id=scratch.turn_id,
        dispatch_key="eval:capability:first",
    )
    assert "web_search" in first.definition["tool_refs"]
    catalog = TeamPluginStore(str(db_path))
    plugin = (await catalog.list(team.id))[0]
    await catalog.update(
        team_id=team.id,
        plugin_id=plugin["id"],
        actor_user_id=owner.id,
        expected_row_version=plugin["row_version"],
        name="Eval resources",
        description="Updated resources",
        tool_refs=["web_fetch"],
        skill_grants=[],
    )
    replay = await bindings.admit_eval_execution(
        eval_run_id=run.run_id,
        user_id=owner.id,
        turn_id=scratch.turn_id,
        dispatch_key="eval:capability:first",
    )
    second = await bindings.admit_eval_execution(
        eval_run_id=run.run_id,
        user_id=owner.id,
        turn_id=scratch.turn_id,
        dispatch_key="eval:capability:second",
    )
    assert "web_search" in replay.definition["tool_refs"]
    assert "web_fetch" in second.definition["tool_refs"]
    assert "web_search" not in second.definition["tool_refs"]


@pytest.mark.anyio
async def test_eval_exact_materializer_rechecks_latest_safety(tmp_path: Path) -> None:
    db_path, _users, team, owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)
    scratch = await _start_eval_scratch(
        db_path=db_path,
        eval_store=eval_store,
        run=run,
        workspace_id=workspace_id,
        suffix="materialize",
    )
    bindings = AgentExecutionBindingStore(db_path)
    bound = await bindings.admit_eval_execution(
        eval_run_id=run.run_id,
        user_id=owner.id,
        turn_id=scratch.turn_id,
        dispatch_key="eval:first",
    )
    assert bound.revision_ref == run.subject.revision_ref
    assert bound.release_id == run.subject.release_id

    timestamp = "2026-09-05T00:01:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,
              status,deterministic_json,semantic_json,safe_summary_json,
              created_by,created_at
            ) VALUES ('asr_eval_execution_blocked',?,?,'revision',?,?,?,
                      'risk-v1','semantic-v2','blocked','{}','{}','{}',?,?)
            """,
            (
                team.id,
                run.subject.agent_id,
                run.subject.revision_ref,
                _content_hash(
                    '{"allowed_tools":["read_fixture"],"max_iterations":3,"name":"eval-agent","system_prompt":"Analyze the fixture."}',
                    '{"description":"Eval fixture","display_name":"Eval Agent","name":"eval-agent"}',
                ),
                _HASH_B,
                owner.id,
                timestamp,
            ),
        )
        connection.commit()
    with pytest.raises(AgentExecutionBindingError, match="agent_safety_blocked"):
        await bindings.admit_eval_execution(
            eval_run_id=run.run_id,
            user_id=owner.id,
            turn_id=scratch.turn_id,
            dispatch_key="eval:second",
        )


@pytest.mark.anyio
async def test_exact_executor_uses_pinned_binding_and_sanitized_context(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, revision, run = await _fixture(tmp_path)
    scratch = await _start_eval_scratch(
        db_path=db_path,
        eval_store=eval_store,
        run=run,
        workspace_id=workspace_id,
        suffix="exact_executor",
    )
    turn_id = scratch.turn_id
    observed: dict[str, Any] = {}

    class AccessControlStore:
        def __init__(self) -> None:
            self.denials = 0

        async def explain_decision(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"allowed": True}

        async def record_runtime_denied(self, *args: Any, **kwargs: Any) -> None:
            self.denials += 1

    access_store = AccessControlStore()
    access_context = {"principal": {"user_id": owner.id}}

    class ContextFactory:
        async def build(self, request: Any) -> ToolExecutionContext:
            context = _ctx()
            context.canvas_store = object()
            context.artifact_store = object()
            context.scheduled_store = object()
            context.vector_client = object()
            context.memory_pack = object()
            context.router = object()
            context.subagent_runner = object()
            context.kb_retriever = object()
            context.subagent_draft_service = object()
            context.metrics_log = object()
            context.preferred_provider = "live-provider"
            context.preferred_model = "live-model"
            context.language_context = object()
            context.unconfigured_capability_tools = frozenset({"bi_query"})
            context.access_control_store = access_store
            context.access_context = access_context
            return context

    class Runner:
        async def run(self, **kwargs: Any) -> SubagentResult:
            observed.update(kwargs)
            context = kwargs["parent_ctx"]
            assert context.workspace_id == workspace_id
            assert context.team_id == run.subject.team_id
            assert context.user_id == owner.id
            assert context.turn_id == turn_id
            assert context.canvas_store is None
            assert context.artifact_store is None
            assert context.scheduled_store is None
            assert context.vector_client is None
            assert context.memory_pack is None
            assert context.router is None
            assert context.subagent_runner is None
            assert context.kb_retriever is None
            assert context.subagent_draft_service is None
            assert context.metrics_log is None
            assert context.preferred_provider is None
            assert context.preferred_model is None
            assert context.language_context is None
            assert context.unconfigured_capability_tools == frozenset()
            assert context.datarc is not None
            assert context.pbi is context.datarc
            assert context.active_datarc_project == "eval_fixture"
            assert context.role == "analyst"
            assert context.user_text == "Read revenue from the fixture"
            assert context.access_context == access_context
            assert context.access_context is not access_context
            await context.access_control_store.record_runtime_denied()
            assert access_store.denials == 0
            assert await context.access_control_store.explain_decision() == {
                "allowed": True,
            }
            invocation = await context.tool_view.execute(
                "bi_query",
                {},
                context,
            )
            assert invocation.result == {"rows": []}
            return SubagentResult(
                profile=kwargs["profile_name"],
                subagent_id=kwargs["pre_minted_subagent_id"],
                ok=True,
                final_answer="Revenue is 500",
                tokens_input=100,
                tokens_output=20,
                duration_ms=12,
            )

    events: list[Any] = []

    async def _sink(event: Any) -> None:
        events.append(event)

    registry = _registry([])

    async def _eval_bi(
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        return {"rows": []}

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="bi_query",
                description="Eval BI fixture",
                input_schema={"type": "object"},
            ),
            handler=_eval_bi,
            effect_spec=TOOL_EFFECT_READ_ONLY,
        )
    )
    executor = ExactSubagentEvalExecutor(
        binding_store=AgentExecutionBindingStore(db_path),
        subagent_runner=Runner(),
        context_factory=ContextFactory(),
        ordered_turn_log_sink=_sink,
    )
    output = await executor.execute(
        type(
            "Request",
            (),
            {
                "execution_mode": "eval",
                "run": run,
                "case": revision.cases[0],
                "attempt": 1,
                "workspace_id": workspace_id,
                "session_id": scratch.session_id,
                "turn_id": turn_id,
                "safe_tool_registry": registry,
            },
        )()
    )
    assert output.output == "Revenue is 500"
    assert observed["exact_binding"].revision_ref == run.subject.revision_ref
    assert [event.event_type for event in events] == ["final_answer", "done"]
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT revision_ref,status FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
    assert row == (run.subject.revision_ref, "completed")


@pytest.mark.parametrize("error_code", ["subagent_internal_error", "peer_llm_deadline_exceeded", "subagent_execution_uncertain"])
@pytest.mark.anyio
async def test_platform_model_failure_settles_as_infrastructure_not_a_failed_answer(tmp_path: Path, error_code: str) -> None:
    # A refused or failed model call (116: physical_observation_unavailable) says nothing about
    # Agent quality; scoring it as a failed answer blocked release as "required case failed".
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class ContextFactory:
        async def build(self, request: Any) -> ToolExecutionContext:
            return _ctx()

    class Runner:
        async def run(self, **kwargs: Any) -> SubagentResult:
            return SubagentResult(
                profile=kwargs["profile_name"],
                subagent_id=kwargs["pre_minted_subagent_id"],
                ok=False,
                final_answer="",
                error=error_code,
                duration_ms=60,
            )

    async def _sink(event: Any) -> None:
        return None

    executor = ExactSubagentEvalExecutor(
        binding_store=AgentExecutionBindingStore(db_path),
        subagent_runner=Runner(),
        context_factory=ContextFactory(),
        ordered_turn_log_sink=_sink,
    )
    service = _service(db_path=db_path, eval_store=eval_store, executor=executor, registry=_registry([]))
    finished = await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.platform")
    assert finished.status != "failed"
    with sqlite3.connect(db_path) as connection:
        jobs = connection.execute("SELECT status FROM agent_eval_case_result WHERE run_id=?", (run.run_id,)).fetchall()
        executions = connection.execute(
            "SELECT status,failure_code FROM turn_agent_execution WHERE turn_id LIKE 't_eval_%'",
        ).fetchall()
    assert jobs and {status for (status,) in jobs} == {"infra_failed"}
    assert executions and set(executions) == {("failed", "agent_eval_platform_failed")}


@pytest.mark.anyio
async def test_an_agent_stop_without_its_own_duration_keeps_the_time_it_ran(tmp_path: Path) -> None:
    # The failure breaker returns no duration; the attempt still ran and must not read "0 s".
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class ContextFactory:
        async def build(self, request: Any) -> ToolExecutionContext:
            return _ctx()

    class Runner:
        async def run(self, **kwargs: Any) -> SubagentResult:
            await asyncio.sleep(0.02)
            return SubagentResult(
                profile=kwargs["profile_name"],
                subagent_id=kwargs["pre_minted_subagent_id"],
                ok=False,
                final_answer="insufficient_data: peer_total_failures=8 ≥ 8.",
                error="peer_total_failures=8 ≥ 8.",
            )

    async def _sink(event: Any) -> None:
        return None

    executor = ExactSubagentEvalExecutor(
        binding_store=AgentExecutionBindingStore(db_path),
        subagent_runner=Runner(),
        context_factory=ContextFactory(),
        ordered_turn_log_sink=_sink,
    )
    service = _service(db_path=db_path, eval_store=eval_store, executor=executor, registry=_registry([]))
    await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.duration")

    results = await eval_store.list_case_results(run.run_id)
    assert results and all((item.duration_ms or 0) >= 20 for item in results)


@pytest.mark.parametrize(
    ("llm_reason_code", "failure_code", "cause"),
    [
        ("llm_provider_rate_limited", "agent_eval_agent_model_rate_limited", "rate_limited"),
        ("llm_provider_auth_failed", "agent_eval_agent_model_auth_failed", "auth_failed"),
        ("llm_provider_timeout", "agent_eval_agent_model_timeout", "timeout"),
        ("team_model_role_unbound", "agent_eval_model_unavailable", "role_unassigned"),
        (None, "agent_eval_attempt_failed", None),
    ],
)
@pytest.mark.anyio
async def test_a_failed_model_call_keeps_its_cause_and_a_diagnostic_reference(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, llm_reason_code: str | None, failure_code: str, cause: str | None
) -> None:
    # The Owner reads why the attempt has no verdict (rate limit, credentials, no model...), never one
    # "environment problem"; an unclassified error carries the reference the server logs it under.
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class ContextFactory:
        async def build(self, request: Any) -> ToolExecutionContext:
            return _ctx()

    class Runner:
        async def run(self, **kwargs: Any) -> SubagentResult:
            return SubagentResult(
                profile=kwargs["profile_name"],
                subagent_id=kwargs["pre_minted_subagent_id"],
                ok=False,
                final_answer="",
                error="subagent_internal_error",
                llm_reason_code=llm_reason_code,
                duration_ms=60,
            )

    async def _sink(event: Any) -> None:
        return None

    executor = ExactSubagentEvalExecutor(
        binding_store=AgentExecutionBindingStore(db_path),
        subagent_runner=Runner(),
        context_factory=ContextFactory(),
        ordered_turn_log_sink=_sink,
    )
    service = _service(db_path=db_path, eval_store=eval_store, executor=executor, registry=_registry([]))
    with caplog.at_level("ERROR", logger="bowhead_core.agent_eval_service"):
        await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.cause")

    results = await eval_store.list_case_results(run.run_id)
    assert results and {item.status for item in results} == {"infra_failed"}
    assert {item.failure_code for item in results} == {failure_code}
    failure = results[0].deterministic["failure"]
    assert failure.get("cause") == cause
    assert failure["category"] == {None: "platform", "role_unassigned": "model_unavailable"}.get(cause, "agent_model")
    with sqlite3.connect(db_path) as connection:
        job_ids = {row[0] for row in connection.execute("SELECT id FROM agent_eval_case_job WHERE run_id=?", (run.run_id,))}
    assert failure["reference"] in job_ids
    # The same reference is in the server log with the failure code, so an admin can find the error.
    logged = [record.getMessage() for record in caplog.records if "agent_eval.attempt_failed" in record.getMessage()]
    assert any(f"reference={failure['reference']}" in line and f"failure_code={failure_code}" in line for line in logged)


@pytest.mark.anyio
async def test_a_tool_that_raises_is_named_on_the_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A sandbox or data connection that raises out of the Tool call ends the attempt; the Owner is told which Tool.
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    async def broken_execute(self: ToolRegistry, name: str, arguments: dict[str, Any], ctx: Any, **kwargs: Any) -> Any:
        raise ConnectionError("upstream closed")

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            await request.safe_tool_registry.execute("read_fixture", {"metric": "revenue"}, _ctx())
            raise AssertionError("unreachable")

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=_registry([]))
    monkeypatch.setattr(ToolRegistry, "execute", broken_execute)
    await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.tool")

    results = await eval_store.list_case_results(run.run_id)
    assert results and {item.status for item in results} == {"infra_failed"}
    assert {(item.failure_code, item.deterministic["failure"]["category"], item.deterministic["failure"]["tool"]) for item in results} == {
        ("agent_eval_tool_failed", "tool", "read_fixture")
    }


@pytest.mark.anyio
async def test_agent_eval_service_runs_fixture_scoring_and_projects_evidence(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)
    production_calls: list[str] = []
    registry = _registry(production_calls)

    class Fixture:
        async def resolve(self, **kwargs: Any) -> EvalEffectOutcome:
            return EvalEffectOutcome(
                result={"revenue": 500},
                evidence_ref="fixture:dataset_eval:v1",
                authorized_numbers=(500,),
                citations=("dataset_eval",),
            )

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            invocation = await request.safe_tool_registry.execute(
                "read_fixture",
                {"metric": "revenue"},
                _ctx(),
            )
            return EvalAttemptOutput(
                output="Revenue is 500",
                terminal_status="completed",
                duration_ms=15,
                cost_microusd=100,
                trace={"fixture_result_ok": invocation.error is None},
            )

    class Provider:
        @asynccontextmanager
        async def open_tool_view(self, **kwargs: Any):
            yield registry

    class Safety:
        async def require_current(self, run: Any) -> None:
            return None

    class Admission:
        async def authorize(self, **kwargs: Any) -> bool:
            return True

    service = AgentEvalService(
        eval_store=eval_store,
        job_store=AgentEvalJobStore(db_path),
        session_store=SessionStore(str(db_path)),
        executor=Executor(),
        tool_view_provider=Provider(),
        fixture_resolver=Fixture(),
        adapter_registry=EvalEffectAdapterRegistry(),
        safety_gate=Safety(),
        model_config_gate=Safety(),
        admission_gate=Admission(),
    )
    finished = await service.execute_run(
        run_id=run.run_id,
        workspace_id=workspace_id,
        worker_id="eval.worker",
        limits=EvalExecutionLimits(),
    )
    assert finished.status == "passed"
    assert finished.summary is not None and finished.summary["pass@k_cases"] == 1
    evidence = await eval_store.read_evidence(
        team_id=run.subject.team_id,
        agent_source_kind="team",
        agent_id=run.subject.agent_id,
        suite_id=(await eval_store.read_suite_revision(run.suite_revision_id)).suite_id,  # type: ignore[union-attr]
    )
    assert evidence is not None and evidence.state == "valid"
    assert production_calls == []
    assert await service.session_store.list_sessions(user_id=owner.id, workspace_id=workspace_id) == []


@pytest.mark.parametrize(("output", "unsourced"), [("Revenue 800, profit 120", []), ("Revenue 999, profit 120", [999.0])])
@pytest.mark.anyio
async def test_eval_numeric_sources_include_input_but_exclude_expected_answer(tmp_path: Path, output: str, unsourced: list[float]) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, original, previous = await _fixture(tmp_path)
    revision = await eval_store.create_suite_revision(
        suite_id=original.suite_id,
        definition={},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key="input.figures",
                input={"task": "Report synthetic revenue 800 and profit 120. Do not call Tools."},
                fixture_ref={},
                expected_contract={"output_schema": {"type": "string", "enum": ["Revenue 800, profit 120", "Revenue 999, profit 120"]}},
                scorer_config={"critical": ["no_fabrication"], "max_tool_calls": 0},
                effect_policy={"mode": "no_effects"},
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=previous.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="input.figures",
        actor_user_id=owner.id,
    )

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            return EvalAttemptOutput(output=output, terminal_status="completed", duration_ms=10, cost_microusd=100)

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=_registry([]))
    finished = await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.input")
    # The number check is advisory even when a stored case lists it as critical: it never decides the run.
    assert finished.status == "passed"
    [result] = await eval_store.list_case_results(run.run_id)
    [row] = [item for item in result.deterministic["scorers"] if item["name"] == "no_fabrication"]
    assert row["advisory"] is True and row["critical"] is False
    # Task figures are a source; the expected answer's figures are not.
    assert row["data"].get("violations", []) == unsourced


@pytest.mark.anyio
async def test_eval_service_cancels_active_attempt_and_stops_claiming(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)
    started = asyncio.Event()
    interrupted = asyncio.Event()

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                interrupted.set()
                raise
            raise AssertionError("unreachable")

    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=Executor(),
        registry=_registry([]),
    )
    running = asyncio.create_task(
        service.execute_run(
            run_id=run.run_id,
            workspace_id=workspace_id,
            worker_id="eval.cancel",
            limits=EvalExecutionLimits(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    await service.job_store.request_cancel(run.run_id)
    finished = await asyncio.wait_for(running, timeout=4)
    assert finished.status == "cancelled"
    assert interrupted.is_set()
    jobs = await service.job_store.list_jobs(run.run_id)
    assert [job.status for job in jobs] == ["cancelled"]
    results = await eval_store.list_case_results(run.run_id)
    assert [result.status for result in results] == ["cancelled"]


@pytest.mark.anyio
async def test_eval_service_renews_lease_during_long_attempt(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class TrackingJobs(AgentEvalJobStore):
        renewals = 0

        async def renew_lease(self, **kwargs: Any):
            self.renewals += 1
            return await super().renew_lease(**kwargs)

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            invocation = await request.safe_tool_registry.execute(
                "read_fixture",
                {"metric": "revenue"},
                _ctx(),
            )
            await asyncio.sleep(1.2)
            return EvalAttemptOutput(
                output="Revenue is 500",
                terminal_status="completed",
                duration_ms=1_200,
                cost_microusd=100,
                trace={"fixture_result_ok": invocation.error is None},
            )

    jobs = TrackingJobs(db_path)
    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=Executor(),
        registry=_registry([]),
        job_store=jobs,
    )
    finished = await service.execute_run(
        run_id=run.run_id,
        workspace_id=workspace_id,
        worker_id="eval.renew",
        limits=EvalExecutionLimits(lease_seconds=10),
    )
    assert finished.status == "passed"
    assert jobs.renewals >= 1


@pytest.mark.anyio
async def test_eval_service_total_duration_budget_interrupts_judge(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, source_revision, source_run = await _fixture(tmp_path)
    source_case = source_revision.cases[0]
    judge_revision = await eval_store.create_suite_revision(
        suite_id=source_revision.suite_id,
        definition={"purpose": "bounded Judge"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": True, "rubric_ref": "quality.rubric@1"},
        cases=(
            EvalCaseDraft(
                case_key=source_case.case_key,
                input=source_case.input,
                fixture_ref=source_case.fixture_ref,
                expected_contract=source_case.expected_contract,
                scorer_config=source_case.scorer_config,
                effect_policy=source_case.effect_policy,
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=source_run.subject,
        suite_revision_id=judge_revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:judge:duration-budget",
        actor_user_id=owner.id,
        judge_role_ref="judge",
        judge_model_fingerprint=_HASH_A,
    )
    judge_started = asyncio.Event()
    judge_interrupted = asyncio.Event()

    class Router:
        async def invoke(self, role: str, request: Any) -> LLMResponse:
            judge_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                judge_interrupted.set()
                raise
            raise AssertionError("unreachable")

    class Rubric:
        async def resolve(self, exact_ref: str) -> str:
            return "Pass grounded answers."

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            await request.safe_tool_registry.execute(
                "read_fixture",
                {"metric": "revenue"},
                _ctx(),
            )
            return EvalAttemptOutput(
                output="Revenue is 500",
                terminal_status="completed",
                duration_ms=10,
                cost_microusd=100,
            )

    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=Executor(),
        registry=_registry([]),
    )
    service.judge = TeamRouterEvalJudge(
        router=Router(),
        rubric_resolver=Rubric(),
        timeout_seconds=45,
    )
    finished = await asyncio.wait_for(
        service.execute_run(
            run_id=run.run_id,
            workspace_id=workspace_id,
            worker_id="eval.duration",
            limits=EvalExecutionLimits(max_duration_ms=1_000),
        ),
        timeout=4,
    )
    assert judge_started.is_set()
    assert judge_interrupted.is_set()
    assert finished.status == "incomplete"
    assert finished.summary is not None
    assert finished.summary["stop_reason"] == "agent_eval_duration_budget_exceeded"
    results = await eval_store.list_case_results(run.run_id)
    assert results[0].failure_code == "agent_eval_duration_budget_exceeded"
    # The run's budget is what ended it, and the result says so with the limit and the time spent.
    assert results[0].deterministic["time_limit"]["scope"] == "run"
    assert results[0].deterministic["time_limit"]["limit_ms"] == 1_000
    assert results[0].duration_ms == results[0].deterministic["time_limit"]["observed_ms"]


@pytest.mark.anyio
async def test_attempt_over_its_own_limit_is_cut_alone_and_the_run_continues(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, source_revision, source_run = await _fixture(tmp_path)
    source_case = source_revision.cases[0]
    revision = await eval_store.create_suite_revision(
        suite_id=source_revision.suite_id,
        definition={"purpose": "attempt time limit"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key=source_case.case_key,
                input=source_case.input,
                fixture_ref=source_case.fixture_ref,
                expected_contract=source_case.expected_contract,
                scorer_config={**source_case.scorer_config, "max_duration_ms": 1_000},
                effect_policy=source_case.effect_policy,
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=source_run.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:attempt-time-limit",
        actor_user_id=owner.id,
    )

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            if request.attempt == 1:
                await asyncio.sleep(30)
            await request.safe_tool_registry.execute("read_fixture", {"metric": "revenue"}, _ctx())
            return EvalAttemptOutput(output="Revenue is 500", terminal_status="completed", duration_ms=10, cost_microusd=100)

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=_registry([]))
    finished = await asyncio.wait_for(
        service.execute_run(
            run_id=run.run_id,
            workspace_id=workspace_id,
            worker_id="eval.attempt-limit",
            limits=EvalExecutionLimits(attempts=2, max_concurrency=1, max_duration_ms=60_000),
        ),
        timeout=8,
    )

    results = sorted(await eval_store.list_case_results(run.run_id), key=lambda item: item.attempt)
    assert [item.status for item in results] == ["infra_failed", "passed"]
    assert results[0].failure_code == "agent_eval_attempt_timeout"
    limit = results[0].deterministic["time_limit"]
    assert limit["scope"] == "attempt" and limit["limit_ms"] == 1_000
    assert 1_000 <= limit["observed_ms"] < 8_000 and results[0].duration_ms == limit["observed_ms"]
    assert finished.status == "incomplete"
    assert finished.summary is not None
    assert finished.summary["time_limit_failures"] == 1
    assert finished.summary["infra_failures"] == 0
    assert "stop_reason" not in finished.summary


@pytest.mark.anyio
async def test_custom_scorer_bundle_builds_fresh_attempt_instances(tmp_path: Path) -> None:
    db_path, _users, _team, _owner, _workspace_id, eval_store, _revision, _run = await _fixture(tmp_path)
    created: list[Any] = []

    class StatefulScorer:
        name = "custom_quality"

        def score(self, case: Any, output: Any, trace: Any = None) -> ScoreResult:
            return ScoreResult(name=self.name, passed=True, score=1.0)

    def _factory() -> list[Any]:
        scorer = StatefulScorer()
        created.append(scorer)
        return [scorer]

    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=object(),
        registry=_registry([]),
    )
    service.scorer_bundle_factories = {"custom.quality@1": _factory}
    first = service._scorers_for_bundle("custom.quality@1")
    second = service._scorers_for_bundle("custom.quality@1")
    assert first[-1] is created[0]
    assert second[-1] is created[1]
    assert first[-1] is not second[-1]


@pytest.mark.anyio
async def test_eval_service_reports_pass_at_k_pass_power_k_and_flaky(tmp_path: Path) -> None:
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            if request.attempt == 1:
                await request.safe_tool_registry.execute(
                    "read_fixture",
                    {"metric": "revenue"},
                    _ctx(),
                )
                return EvalAttemptOutput(
                    output="Revenue is 500",
                    terminal_status="completed",
                    duration_ms=10,
                    cost_microusd=100,
                )
            return EvalAttemptOutput(
                output="No answer",
                terminal_status="failed",
                duration_ms=10,
                cost_microusd=100,
            )

    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=Executor(),
        registry=_registry([]),
    )
    finished = await service.execute_run(
        run_id=run.run_id,
        workspace_id=workspace_id,
        worker_id="eval.flaky",
        limits=EvalExecutionLimits(attempts=2, max_concurrency=2),
    )
    assert finished.status == "failed"
    assert finished.summary is not None
    assert finished.summary["pass@k_cases"] == 1
    assert finished.summary["pass^k_cases"] == 0
    assert finished.summary["flaky_cases"] == 1


@pytest.mark.anyio
async def test_duplicate_run_worker_waits_for_active_lease_instead_of_finishing_early(tmp_path: Path) -> None:
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            await request.safe_tool_registry.execute(
                "read_fixture",
                {"metric": "revenue"},
                _ctx(),
            )
            started.set()
            await release.wait()
            return EvalAttemptOutput(
                output="Revenue is 500",
                terminal_status="completed",
                duration_ms=10,
                cost_microusd=100,
            )

    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=Executor(),
        registry=_registry([]),
    )
    first = asyncio.create_task(
        service.execute_run(
            run_id=run.run_id,
            workspace_id=workspace_id,
            worker_id="eval.primary",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    second = asyncio.create_task(
        service.execute_run(
            run_id=run.run_id,
            workspace_id=workspace_id,
            worker_id="eval.duplicate",
        )
    )
    await asyncio.sleep(0.2)
    assert not second.done()
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.status == "passed"
    assert second_result.status == "passed"


@pytest.mark.anyio
async def test_judge_records_route_usage_and_fails_closed_on_invalid_json() -> None:
    class Rubric:
        async def resolve(self, exact_ref: str) -> str:
            return "Pass when the answer is grounded."

    class Router:
        async def invoke(self, role: str, request: Any) -> LLMResponse:
            assert role == "judge"
            return LLMResponse(
                content='```json\n{"passed":true,"score":1,"reason":"ok","reason_codes":[]}\n```',
                provider="fixture-provider",
                model="fixture-model",
                usage=TokenUsage(input_tokens=100, output_tokens=20),
            )

    result = await TeamRouterEvalJudge(router=Router(), rubric_resolver=Rubric()).judge(
        task="Analyze revenue",
        output="Revenue is 500",
        rubric_ref="quality.rubric@1",
        role_ref="judge",
        expected_model_fingerprint=_HASH_A,
    )
    assert result.status == "unknown"
    assert result.failure_code == "agent_eval_judge_parse_error"
    assert result.cost_microusd > 0
    assert result.evidence["provider"] == "fixture-provider"


@pytest.mark.parametrize(
    ("case_change", "failure_code"),
    [
        ({"scorer_config": {"max_tool_calls": "many"}}, "agent_eval_case_scorer_config_invalid"),
        ({"valid_until": "2020-01-01T00:00:00+00:00"}, "agent_eval_fixture_expired"),
        ({"scorer_config": {"type": "exact_match"}}, "agent_eval_scoring_contract_invalid"),
        ({"expected_contract": {"assertions": ["Revenue must match"]}}, "agent_eval_scoring_contract_invalid"),
    ],
)
@pytest.mark.anyio
async def test_eval_service_rejects_invalid_or_expired_case_before_job_materialization(
    tmp_path: Path,
    case_change: dict[str, Any],
    failure_code: str,
) -> None:
    db_path, _users, _team, _owner, workspace_id, eval_store, revision, run = await _fixture(tmp_path)
    changed_revision = replace(
        revision,
        cases=(replace(revision.cases[0], **case_change),),
    )

    class EvalStoreProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(eval_store, name)

        async def read_suite_revision(self, revision_id: str) -> Any:
            assert revision_id == revision.suite_revision_id
            return changed_revision

    service = _service(
        db_path=db_path,
        eval_store=EvalStoreProxy(),  # type: ignore[arg-type]
        executor=object(),
        registry=_registry([]),
    )
    result = await service.execute_run(
        run_id=run.run_id,
        workspace_id=workspace_id,
        worker_id="eval.preflight",
    )
    assert result.status == "cancelled"
    assert result.summary["failure_code"] == failure_code
    assert await AgentEvalJobStore(db_path).read_execution(run.run_id) is None


@pytest.mark.anyio
async def test_eval_service_rejects_changed_model_before_job_materialization(
    tmp_path: Path,
) -> None:
    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class ChangedModel:
        async def require_current(self, current_run: Any) -> None:
            raise AgentEvalServiceError("agent_eval_model_config_changed")

    service = _service(
        db_path=db_path,
        eval_store=eval_store,
        executor=object(),
        registry=_registry([]),
    )
    service.model_config_gate = ChangedModel()
    result = await service.execute_run(
        run_id=run.run_id,
        workspace_id=workspace_id,
        worker_id="eval.model-preflight",
    )
    assert result.status == "cancelled"
    assert result.summary["failure_code"] == "agent_eval_model_config_changed"
    assert await AgentEvalJobStore(db_path).read_execution(run.run_id) is None


@pytest.mark.anyio
async def test_judge_treats_malformed_rubric_and_usage_as_unknown() -> None:
    class EmptyRubric:
        async def resolve(self, exact_ref: str) -> str:
            return ""

    class UnusedRouter:
        async def invoke(self, role: str, request: Any) -> Any:
            raise AssertionError("empty rubric must not invoke the Judge model")

    empty = await TeamRouterEvalJudge(
        router=UnusedRouter(),
        rubric_resolver=EmptyRubric(),
    ).judge(
        task="Analyze revenue",
        output="Revenue is 500",
        rubric_ref="quality.rubric@1",
        role_ref="judge",
        expected_model_fingerprint=_HASH_A,
    )
    assert empty.status == "unknown"
    assert empty.failure_code == "agent_eval_judge_rubric_unavailable"

    class Rubric:
        async def resolve(self, exact_ref: str) -> str:
            return "Pass when grounded."

    class Usage:
        input_tokens = "invalid"
        output_tokens = 1

    class Response:
        content = '{"passed":true,"score":1,"reason":"ok","reason_codes":[]}'
        provider = "fixture-provider"
        model = "fixture-model"
        usage = Usage()

    class Router:
        async def invoke(self, role: str, request: Any) -> Any:
            return Response()

    malformed = await TeamRouterEvalJudge(
        router=Router(),
        rubric_resolver=Rubric(),
    ).judge(
        task="Analyze revenue",
        output="Revenue is 500",
        rubric_ref="quality.rubric@1",
        role_ref="judge",
        expected_model_fingerprint=_HASH_A,
    )
    assert malformed.status == "unknown"
    assert malformed.failure_code == "agent_eval_judge_parse_error"


def _service(
    *,
    db_path: Path,
    eval_store: AgentEvalStore,
    executor: Any,
    registry: ToolRegistry,
    job_store: AgentEvalJobStore | None = None,
) -> AgentEvalService:
    class Fixture:
        async def resolve(self, **kwargs: Any) -> EvalEffectOutcome:
            return EvalEffectOutcome(
                result={"revenue": 500},
                evidence_ref="fixture:dataset_eval:v1",
                authorized_numbers=(500,),
                citations=("dataset_eval",),
            )

    class Provider:
        @asynccontextmanager
        async def open_tool_view(self, **kwargs: Any):
            yield registry

    class Safety:
        async def require_current(self, run: Any) -> None:
            return None

    class Admission:
        async def authorize(self, **kwargs: Any) -> bool:
            return True

    return AgentEvalService(
        eval_store=eval_store,
        job_store=job_store or AgentEvalJobStore(db_path),
        session_store=SessionStore(str(db_path)),
        executor=executor,
        tool_view_provider=Provider(),
        fixture_resolver=Fixture(),
        adapter_registry=EvalEffectAdapterRegistry(),
        safety_gate=Safety(),
        model_config_gate=Safety(),
        admission_gate=Admission(),
    )


def _hash_text(value: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def test_checks_with_nothing_to_check_do_not_vote_on_the_case() -> None:
    from bowhead_core.agent_eval_scorers import build_mandatory_scorers

    case = EvalCase(id="na", input="x", meta={"scorer_config": {}})
    # "1200" can't be checked: no Tool returned data, so there is nothing to compare it with.
    trace = {"terminal_status": "completed", "tool_calls": [], "authorized_numbers": [], "citations": [], "data_read": False, "numeric_input": False}
    scores = [scorer.score(case, "Answers run 30-1200 characters.", trace) for scorer in build_mandatory_scorers()]
    result = deterministic_result(scores, scorer_config={"critical": ["no_fabrication"]})
    applicable = {row["name"]: row["applicable"] for row in result["scorers"]}
    assert applicable == {
        "terminal_contract": True,
        "output_schema": False,
        "tool_contract": False,
        "permission_denial": False,
        "effect_policy": True,
        "citation": False,
        "no_fabrication": False,
        "budget_timeout": True,
    }
    assert result["passed"] is True and result["critical_failure"] is False


def test_fabrication_is_checked_once_data_was_read() -> None:
    from bowhead_core.agent_eval_scorers import DataGroundedNoFabricationScorer

    case = EvalCase(id="data", input="x", meta={"scorer_config": {}})
    trace = {"tool_calls": [], "authorized_numbers": [500.0], "data_read": True}
    assert DataGroundedNoFabricationScorer().score(case, "Revenue was 500.", trace).passed is True
    failed = DataGroundedNoFabricationScorer().score(case, "Revenue was 1200.", trace)
    assert failed.passed is False and failed.data.get("applicable", True) is True
    # Figures given in the task are a source too, even with no Tool call.
    task_only = {"tool_calls": [], "authorized_numbers": [800.0], "data_read": False, "numeric_input": True}
    assert DataGroundedNoFabricationScorer().score(case, "Revenue was 999.", task_only).passed is False


def test_a_not_applicable_critical_check_cannot_fail_and_old_rows_still_count() -> None:
    from bowhead_core.evalkit.scorers import ScoreResult

    not_applicable = ScoreResult("citation", False, 0.0, "", {"applicable": False})
    assert deterministic_result([not_applicable], scorer_config={"critical": ["citation"]}) == {
        "critical_failure": False,
        "passed": True,
        "scorers": [
            {
                "advisory": False,
                "applicable": False,
                "critical": True,
                "data": {"applicable": False},
                "detail": "",
                "name": "citation",
                "passed": False,
                "score": 0.0,
            }
        ],
    }
    # A result recorded before `applicable` existed has no key and keeps counting.
    legacy = ScoreResult("citation", False, 0.0, "", {})
    assert deterministic_result([legacy], scorer_config={})["passed"] is False


def test_unsourced_figures_are_recorded_but_never_fail_the_case() -> None:
    from bowhead_core.evalkit.scorers import ScoreResult

    # "Roughly 1 to 14" for values 0.99-13.75: a derived figure has no literal source.
    unsourced = ScoreResult("no_fabrication", False, 0.5, "unauthorized numbers: [14.0]", {"violations": [14.0]})
    result = deterministic_result([unsourced], scorer_config={"critical": ["no_fabrication"]})
    assert result["passed"] is True and result["critical_failure"] is False
    [row] = result["scorers"]
    assert row["advisory"] is True and row["critical"] is False and row["passed"] is False
    assert row["data"]["violations"] == [14.0]


@pytest.mark.anyio
async def test_reading_data_marks_the_trace_but_a_not_run_denial_does_not() -> None:
    trace = EvalTraceCollector(max_tool_calls=5)
    router = EvalEffectRouter(
        effect_policy={"mode": "no_effects"},
        fixture_ref={},
        fixture_resolver=None,
        adapter_registry=EvalEffectAdapterRegistry(),
        trace=trace,
    )
    trace.record(
        tool_name="send_mail",
        arguments={},
        effect_class="side_effect_possible",
        route="denied",
        duration_ms=1,
        denial_code="eval_effect_adapter_missing",
    )
    assert trace.to_scorer_trace()["data_read"] is False
    await router.build_safe_view(_registry([])).execute("read_fixture", {}, _ctx())
    assert trace.to_scorer_trace()["data_read"] is True


def test_required_tool_counts_as_not_run_only_when_no_call_of_it_ran() -> None:
    denied = {"tool_name": "python_exec", "denial_code": "eval_effect_adapter_missing"}
    ran = {"tool_name": "python_exec", "denial_code": None}
    config = {"required_tools": ["python_sandbox"]}
    # python_exec is the flat entrance of the python_sandbox Tool.
    assert required_tools_not_run_in_eval(scorer_config=config, expected={}, tool_calls=[denied]) == ["python_sandbox"]
    assert required_tools_not_run_in_eval(scorer_config=config, expected={}, tool_calls=[denied, ran]) == []
    # Never called at all is the Agent's miss, left to the tool contract scorer.
    assert required_tools_not_run_in_eval(scorer_config=config, expected={}, tool_calls=[]) == []


def test_answer_depends_on_a_tool_not_run_when_it_brings_data_or_the_agent_stopped() -> None:
    def denied(name: str) -> dict[str, Any]:
        return {"tool_name": name, "denial_code": "eval_effect_adapter_missing"}

    completed = {"terminal_status": "completed", "output": "Sales were 500"}
    # A Datarc query or a Power BI operation brings the Agent its data: the answer can't be right without it.
    assert tools_not_run_in_eval(tool_calls=[denied("bi_query"), denied("bi_schema")], **completed) == ["bi_query", "bi_schema"]
    assert tools_not_run_in_eval(tool_calls=[denied("teammcp.pbi_sales.pbi_execute_dax")], **completed) == ["teammcp.pbi_sales.pbi_execute_dax"]
    # Another Tool the evaluation can't run (sending mail) doesn't make a finished answer untestable...
    assert tools_not_run_in_eval(tool_calls=[denied("send_email")], **completed) == []
    # ...unless the Agent then stopped: its run failed or it answered with the failure breaker's stop.
    assert tools_not_run_in_eval(tool_calls=[denied("send_email")], terminal_status="failed", output="") == ["send_email"]
    stopped = "insufficient_data: peer_total_failures=8 ≥ 8. Too many Tool failures; return insufficient_data instead of retrying further."
    assert tools_not_run_in_eval(tool_calls=[denied("send_email")], terminal_status="completed", output=stopped) == ["send_email"]
    # A Tool that ran on another call did run; other denials (permissions) are the Agent's to handle.
    assert tools_not_run_in_eval(tool_calls=[denied("bi_query"), {"tool_name": "bi_query", "denial_code": None}], **completed) == []
    assert tools_not_run_in_eval(tool_calls=[{"tool_name": "bi_query", "denial_code": "tool_permission_denied"}], **completed) == []
    assert tools_not_run_in_eval(tool_calls=[], terminal_status="failed", output=stopped) == []


@pytest.mark.parametrize(
    ("passes", "platform_case", "verdict"),
    [
        (False, False, "infra_failed"),
        # An answer without the data proves nothing about a data question, even when it passes.
        (True, False, "infra_failed"),
        # The platform safety case only checks the Agent stays within limits: a pass keeps its pass.
        (True, True, "passed"),
    ],
)
@pytest.mark.anyio
async def test_attempt_whose_data_query_could_not_run_has_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passes: bool, platform_case: bool, verdict: str
) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, source_revision, source_run = await _fixture(tmp_path)
    source_case = source_revision.cases[0]
    revision = await eval_store.create_suite_revision(
        suite_id=source_revision.suite_id,
        definition={"purpose": "data"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key=source_case.case_key,
                input={"task": "2026年8月销售额明细"},
                fixture_ref=source_case.fixture_ref,
                # The case names no required Tool, so only the general rule can catch it.
                expected_contract={},
                scorer_config={"max_tool_calls": 10},
                effect_policy=source_case.effect_policy,
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=source_run.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:data-tool-not-run",
        actor_user_id=owner.id,
    )
    if platform_case:
        # The platform suite is managed by the platform (its rows can't be rewritten); its kind is what matters.
        async def _platform(db_path: str, suite_revision_id: str) -> bool:
            return suite_revision_id == revision.suite_revision_id

        monkeypatch.setattr(agent_eval_service_module, "_platform_contract_case", _platform)
    registry = _registry([])

    async def _query(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        raise AssertionError("the production Datarc handler must not run")

    # Like the real bi_query: no effect contract, so the evaluation has no route for it.
    registry.register(ToolSpec(schema=ToolSchema(name="bi_query", description="Query data", input_schema={"type": "object"}), handler=_query))

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            # As in the evaluation context: an inert Datarc handle and project pass the Registry's
            # connection gates, so the call reaches the effect router, which has no route for it.
            ctx = replace(_ctx(), datarc=object(), active_datarc_project="eval_fixture")
            for _ in range(3):
                await request.safe_tool_registry.execute("bi_query", {"queries": ["2026年8月销售额"]}, ctx)
            if passes:
                # An answer that passes without the data keeps its pass.
                return EvalAttemptOutput(
                    output="I can report monthly sales once data is available.", terminal_status="completed", duration_ms=4200, cost_microusd=100
                )
            return EvalAttemptOutput(
                output="insufficient_data: peer_total_failures=8 ≥ 8. Too many Tool failures; return insufficient_data instead of retrying further.",
                terminal_status="failed",
                duration_ms=4200,
                cost_microusd=100,
            )

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=registry)
    finished = await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.data-not-run", limits=EvalExecutionLimits())

    results = await eval_store.list_case_results(run.run_id)
    if verdict == "passed":
        assert [(item.status, item.failure_code) for item in results] == [("passed", None)]
        return
    # Not the Agent's failure and not a pass: the case can't be tested in evaluation yet.
    assert [item.status for item in results] == ["infra_failed"]
    assert results[0].failure_code == "agent_eval_tool_not_run_in_eval"
    assert results[0].deterministic["tools_not_run"] == ["bi_query"]
    assert results[0].duration_ms == 4200
    assert results[0].cost_microusd == 100
    assert finished.status == "incomplete"


@pytest.mark.anyio
async def test_attempt_whose_required_tool_could_not_run_is_never_a_pass(tmp_path: Path) -> None:
    db_path, _users, _team, owner, workspace_id, eval_store, source_revision, source_run = await _fixture(tmp_path)
    source_case = source_revision.cases[0]
    revision = await eval_store.create_suite_revision(
        suite_id=source_revision.suite_id,
        definition={"purpose": "computation"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key=source_case.case_key,
                input=source_case.input,
                fixture_ref=source_case.fixture_ref,
                expected_contract=source_case.expected_contract,
                scorer_config={"required_tools": ["read_fixture", "python_exec"], "max_tool_calls": 3},
                effect_policy=source_case.effect_policy,
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=source_run.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:required-tool-not-run",
        actor_user_id=owner.id,
    )
    registry = _registry([])

    async def _sandbox(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        raise AssertionError("the production sandbox handler must not run")

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="python_exec", description="Run Python", input_schema={"type": "object"}),
            handler=_sandbox,
        )
    )

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            await request.safe_tool_registry.execute("read_fixture", {"metric": "revenue"}, _ctx())
            await request.safe_tool_registry.execute("python_exec", {"code": "print(500)"}, _ctx())
            # Answers as if it had computed, which a scorer alone would accept.
            return EvalAttemptOutput(output="Revenue is 500", terminal_status="completed", duration_ms=10, cost_microusd=100)

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=registry)
    finished = await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.not-run", limits=EvalExecutionLimits())

    results = await eval_store.list_case_results(run.run_id)
    assert [item.status for item in results] == ["infra_failed"]
    assert results[0].failure_code == "agent_eval_required_tool_not_run"
    assert results[0].deterministic["required_tools_not_run"] == ["python_exec"]
    assert finished.status == "incomplete"


async def _data_case_run(tmp_path: Path, *, key: str, attempts_case: str = "data") -> tuple[Any, ...]:
    db_path, _users, _team, owner, workspace_id, eval_store, source_revision, source_run = await _fixture(tmp_path)
    source_case = source_revision.cases[0]
    revision = await eval_store.create_suite_revision(
        suite_id=source_revision.suite_id,
        definition={"purpose": attempts_case},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key=source_case.case_key,
                input={"task": "2026年8月销售额"},
                fixture_ref=source_case.fixture_ref,
                expected_contract={},
                scorer_config={"required_tools": ["bi_query"], "max_tool_calls": 10},
                effect_policy=source_case.effect_policy,
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=source_run.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key=f"manual:{key}",
        actor_user_id=owner.id,
    )
    registry = _registry([])

    async def _query(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        result = await ctx.datarc.download_data(datarc_workspace_id=ctx.resolve_datarc_project(), member_uid=ctx.member_uid, text="2026年8月销售额")
        return {"rows": result.rows, "row_count": result.total_rows, "rows_shown": len(result.rows)}

    registry.register(ToolSpec(schema=ToolSchema(name="bi_query", description="Query data", input_schema={"type": "object"}), handler=_query))
    return db_path, workspace_id, eval_store, run, registry


class _LiveDatarc:
    def __init__(self) -> None:
        self.members: list[str] = []

    async def download_data(self, datarc_workspace_id: str, member_uid: str, text: str, **kwargs: Any) -> Any:
        from bowhead_bi_bridge.types import DownloadDataResult

        self.members.append(member_uid)
        return DownloadDataResult(
            columns=["月份", "销售额"], rows=[{"月份": "2026-08", "销售额": 500}], total_rows=1, explain_json={"measures": ["销售额"]}
        )


class _Resolver:
    def __init__(self, client: Any = None, failure: Any = None) -> None:
        self.client = client
        self.failure = failure
        self.calls = 0

    async def resolve(self, run: Any) -> Any:
        from bowhead_core.agent_eval_datarc import ResolvedDatarcIdentity

        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return ResolvedDatarcIdentity(client=self.client, vector_client=None, member_uid="member_owner", workspace_id="21", workspace_name="Retail")


@pytest.mark.anyio
async def test_a_live_data_query_gets_a_verdict_and_counts_as_run(tmp_path: Path) -> None:
    from bowhead_core.agent_eval_datarc import EvalDatarcLiveRun

    db_path, workspace_id, eval_store, run, registry = await _data_case_run(tmp_path, key="datarc-live")
    client = _LiveDatarc()

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            ctx = replace(_ctx(), datarc=object(), active_datarc_project="eval_fixture")
            await request.safe_tool_registry.execute("bi_query", {"queries": ["2026年8月销售额"]}, ctx)
            return EvalAttemptOutput(output="2026年8月销售额为 500", terminal_status="completed", duration_ms=10, cost_microusd=100)

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=registry)
    service.datarc_live = EvalDatarcLiveRun(resolver=_Resolver(client), run=run, digest_key=b"k" * 32)
    await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.datarc-live", limits=EvalExecutionLimits())

    results = await eval_store.list_case_results(run.run_id)
    assert [(item.status, item.failure_code) for item in results] == [("passed", None)]
    deterministic = results[0].deterministic
    # No "can't be tested in evaluation" classification: the data Tool ran live.
    assert "tools_not_run" not in deterministic and "required_tools_not_run" not in deterministic
    assert deterministic["datarc_calls"] == 1
    [entry] = deterministic["datarc"]
    assert entry["op"] == "query" and entry["row_count"] == 1 and entry["semantics_digest"] and entry["result_digest"]
    assert client.members == ["member_owner"]


@pytest.mark.anyio
async def test_an_environment_cause_ends_the_attempt_at_once_and_latches_for_the_run(tmp_path: Path) -> None:
    from bowhead_core.agent_eval_datarc import EvalDatarcLiveRun, EvalDatarcUnavailable

    db_path, workspace_id, eval_store, run, registry = await _data_case_run(tmp_path, key="datarc-env")
    resolver = _Resolver(failure=EvalDatarcUnavailable("no_connection"))
    continued: list[str] = []

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            ctx = replace(_ctx(), datarc=object(), active_datarc_project="eval_fixture")
            await request.safe_tool_registry.execute("bi_query", {"queries": ["2026年8月销售额"]}, ctx)
            # The Agent would work around the error; the attempt must end before it can.
            await asyncio.sleep(30)
            continued.append(request.turn_id)
            return EvalAttemptOutput(output="no data", terminal_status="completed", duration_ms=10, cost_microusd=100)

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=registry)
    service.datarc_live = EvalDatarcLiveRun(resolver=resolver, run=run, digest_key=None)
    started = asyncio.get_running_loop().time()
    finished = await service.execute_run(
        run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.datarc-env", limits=EvalExecutionLimits(attempts=2)
    )

    assert asyncio.get_running_loop().time() - started < 10
    assert continued == []
    results = await eval_store.list_case_results(run.run_id)
    assert [(item.status, item.failure_code) for item in results] == [("infra_failed", "agent_eval_datarc_unavailable")] * 2
    failure = results[0].deterministic["failure"]
    assert failure["category"] == "data_connection" and failure["cause"] == "no_connection" and failure["tool"] == "bi_query"
    # The run-level cause latched: the second attempt failed at its first call without resolving again.
    assert resolver.calls == 1
    assert finished.status == "incomplete"
