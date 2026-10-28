"""Gateway retries recover committed comparisons without current model services."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agent_eval_comparison import ComparisonExecution
from bowhead_gateway.routes import agents, team_openapi
from fastapi import HTTPException


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution_failed", [False, True])
async def test_committed_execution_is_recovered_before_or_after_model_resolution_failure(monkeypatch, resolution_failed):
    execution = ComparisonExecution(
        id="aex_original",
        team_id="team_1",
        agent_id="osa_1",
        candidate_id="arc_1",
        plan_id="aep_1",
        run_kind="release",
        status="completed",
        selection_hash="sha256:selection",
        request_hash="sha256:environment",
        actor_kind="automation",
        actor_ref="oak_1",
        generation=1,
        row_version=4,
        requested_at="2026-10-02",
        finished_at="2026-10-02",
        failure_code=None,
        runs=(),
    )
    replay = AsyncMock(side_effect=[None, execution] if resolution_failed else [execution])
    store = SimpleNamespace(replay_automation=replay, start_release=AsyncMock())
    monkeypatch.setattr(team_openapi, "AgentEvalComparisonStore", lambda _path: store)
    monkeypatch.setattr(team_openapi, "_automation_actor", AsyncMock(return_value=(None, {"effective_owner_user_id": "owner"})))
    monkeypatch.setattr(team_openapi, "_log_agent_version_call", AsyncMock())
    resolve = AsyncMock(side_effect=HTTPException(503, {"code": "model_route_removed"}))
    monkeypatch.setattr(agents, "_comparison_model_hashes", resolve)
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: object())
    schedule = AsyncMock()
    monkeypatch.setattr(agents, "_schedule_comparison", schedule)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(user_store=SimpleNamespace(db_path="unused", get_user=AsyncMock(return_value=SimpleNamespace(id="owner"))))
        )
    )
    body = team_openapi.AutomationComparisonBody(
        grant_id="avag_1",
        repository="acme/agent",
        ref="refs/heads/main",
        candidate_row_version=1,
        expected_control_row_version=1,
        candidate_hash="sha256:" + "a" * 64,
        plan_id="aep_1",
        plan_hash="sha256:" + "b" * 64,
    )
    result = await team_openapi.start_automation_agent_comparison(
        "osa_1", "arc_1", body, request, "original-key", {"id": "oak_1", "team_id": "team_1"}
    )
    assert result["execution_id"] == "aex_original"
    assert result["status"] == "completed"
    assert resolve.await_count == int(resolution_failed)
    store.start_release.assert_not_awaited()
    schedule.assert_not_awaited()
    assert replay.call_args.kwargs["idempotency_key"] == "automation:oak_1:original-key"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("agent_automation_context_conflict", 409),
        ("agent_automation_context_required", 422),
        ("agent_automation_workspace_required", 422),
        ("agent_automation_workspace_mismatch", 403),
        ("agent_automation_workspace_unavailable", 403),
        ("agent_automation_origin_mismatch", 403),
        ("agent_automation_preparation_not_found", 404),
    ],
)
def test_context_errors_preserve_retry_and_authority_distinctions(code, status):
    from bowhead_core.agent_version_automation import AgentVersionAutomationError

    assert team_openapi._automation_http(AgentVersionAutomationError(code)).status_code == status
