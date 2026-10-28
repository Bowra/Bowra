"""Per-attempt Agent Eval usage capture: subject plus Judge, metering attribution, spent usage on failure."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.integration


async def test_attempt_usage_sums_subject_and_judge_calls_and_attributes_metering(tmp_path: Path) -> None:
    from bowhead_core.agent_eval_service import EvalAttemptOutput, EvalExecutionLimits
    from bowhead_core.agent_eval_store import EvalCaseDraft
    from test_agent_eval_execution import _ctx, _fixture, _registry, _service

    from bowhead_router import usage as router_usage

    db_path, _users, _team, owner, workspace_id, eval_store, source_revision, source_run = await _fixture(tmp_path)
    source_case = source_revision.cases[0]
    judge_revision = await eval_store.create_suite_revision(
        suite_id=source_revision.suite_id,
        definition={"purpose": "usage capture"},
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
        idempotency_key="manual:usage-capture",
        actor_user_id=owner.id,
        judge_role_ref="judge",
        judge_model_fingerprint="sha256:" + "a" * 64,
    )

    def _call(tokens_input: int, tokens_output: int, tokens_cached: int = 0) -> None:
        router_usage.record_usage(
            agent_type="test",
            provider="fixture",
            model="fixture-model",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_cached=tokens_cached,
        )

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            await request.safe_tool_registry.execute("read_fixture", {"metric": "revenue"}, _ctx())
            _call(100, 40, 30)
            _call(50, 10)
            return EvalAttemptOutput(output="Revenue is 500", terminal_status="completed", duration_ms=10, cost_microusd=0)

    class Judge:
        async def judge(self, **_kwargs: Any) -> Any:
            _call(20, 5)
            return SimpleNamespace(status="passed", evidence={}, failure_code=None, cost_microusd=0, duration_ms=1)

    recorded: list[Any] = []
    previous = router_usage.set_usage_sink(recorded.append)
    try:
        service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=_registry([]))
        service.judge = Judge()  # type: ignore[assignment]
        await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.usage", limits=EvalExecutionLimits())
    finally:
        router_usage.set_usage_sink(previous)
    [result] = await eval_store.list_case_results(run.run_id)
    assert (result.tokens_input, result.tokens_output, result.tokens_cached, result.model_call_count) == (170, 55, 30, 3)
    assert {record.context.reason for record in recorded} == {"agent_eval"}
    assert {record.context.turn_id for record in recorded} == {result.trace_evidence_ref.removeprefix("turn:")}
    assert {record.context.team_id for record in recorded} == {run.subject.team_id}
    assert {record.context.workspace_id for record in recorded} == {workspace_id}


async def test_infrastructure_failure_still_records_spent_usage(tmp_path: Path) -> None:
    from bowhead_core.agent_eval_service import EvalExecutionLimits
    from test_agent_eval_execution import _fixture, _registry, _service

    from bowhead_router import usage as router_usage

    db_path, _users, _team, _owner, workspace_id, eval_store, _revision, run = await _fixture(tmp_path)

    class Executor:
        async def execute(self, request: Any) -> Any:
            router_usage.record_usage(agent_type="test", provider="p", model="m", tokens_input=70, tokens_output=7)
            raise RuntimeError("provider exploded")

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=_registry([]))
    await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.infra", limits=EvalExecutionLimits())
    [result] = await eval_store.list_case_results(run.run_id)
    assert result.status == "infra_failed"
    assert (result.tokens_input, result.tokens_output, result.tokens_cached, result.model_call_count) == (70, 7, 0, 1)
