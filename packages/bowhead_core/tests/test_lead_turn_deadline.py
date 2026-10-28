"""One turn deadline: retries, fallbacks and confirmation windows use the Lead-loop deadline."""

import time
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents import lead_orchestrator
from bowhead_core.agents._lead_context import get_turn_deadline
from bowhead_core.agents.lead_orchestrator import LeadRuntime, run_turn
from bowhead_core.subagent import _TurnRetryObserver
from bowhead_core.types import IntentType, TurnResult, TurnStatus

from bowhead_core import send_email_tool

pytestmark = pytest.mark.unit


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _runtime(ctx: Any) -> LeadRuntime:
    return LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=_Bus(),
        parent_ctx=ctx,
        subagent_runner=SimpleNamespace(),
    )


def _sctx(turn_id: str) -> Any:
    return SimpleNamespace(turn_id=turn_id, user_id="u_1", session_id="s_1", plan_execution_id=None)


async def _run_with_probe(monkeypatch: pytest.MonkeyPatch, ctx: Any) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def _probe_loop(sctx: Any, user_text: str, runtime: LeadRuntime) -> TurnResult:
        now = time.monotonic()
        seen["deadline_in"] = get_turn_deadline() - now
        seen["matches_loop_deadline"] = get_turn_deadline() == runtime.parent_ctx._ordinary_lead_deadline
        # A retry that starts 200 s from now and fits its 600 s call budget.
        observer = _TurnRetryObserver(bus=runtime.parent_bus, turn_id=sctx.turn_id, subagent_id=None, profile="lead", call_deadline=now + 600)
        seen["late_retry_admitted"] = observer.attempt_fits(delay_s=200.0, attempt_timeout_s=60.0)
        seen["confirmation_window"] = send_email_tool.confirmation_window()
        from bowhead_core.html_preview_policy import html_preview_limit_bytes
        from bowhead_skill.execution_policy import sandbox_default_timeout_s, sandbox_max_timeout_s

        seen["execution_limits"] = (sandbox_default_timeout_s(), sandbox_max_timeout_s(), html_preview_limit_bytes())
        return TurnResult(turn_id=sctx.turn_id, status=TurnStatus.COMPLETED, intent=IntentType.AMBIGUOUS, final_answer="ok")

    monkeypatch.setattr(lead_orchestrator, "_lead_loop", _probe_loop)
    result = await run_turn(_sctx("t_deadline"), "hello", runtime=_runtime(ctx))
    assert result.status == TurnStatus.COMPLETED
    seen["after_turn"] = get_turn_deadline()
    return seen


@pytest.mark.asyncio
async def test_turn_deadline_is_the_lead_loop_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", "1800")
    seen = await _run_with_probe(monkeypatch, SimpleNamespace())

    assert seen["matches_loop_deadline"] is True
    assert 1790 < seen["deadline_in"] <= 1800
    assert seen["after_turn"] is None


@pytest.mark.asyncio
async def test_retries_and_confirmations_stay_available_after_three_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient provider error 200 s into a turn is still retried; confirmations get a real window."""
    monkeypatch.setenv("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", "1800")
    seen = await _run_with_probe(monkeypatch, SimpleNamespace())

    assert seen["late_retry_admitted"] is True
    assert seen["confirmation_window"] is not None
    assert seen["confirmation_window"] > 1700


@pytest.mark.asyncio
async def test_resumed_turn_uses_its_remaining_turn_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkpoint's legacy action deadline of 0 no longer removes the resumed turn's retries."""
    monkeypatch.setenv("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", "1800")
    checkpoint = SimpleNamespace(remaining_wallclock_s=600.0, remaining_action_deadline_s=0.0)
    ctx = SimpleNamespace(ordinary_lead_state=SimpleNamespace(checkpoint=checkpoint))
    seen = await _run_with_probe(monkeypatch, ctx)

    assert 590 < seen["deadline_in"] <= 600
    assert seen["late_retry_admitted"] is True


@pytest.mark.asyncio
async def test_lead_binds_and_releases_the_team_execution_snapshot(monkeypatch):
    from bowhead_core.execution_limits import ExecutionLimits
    from bowhead_skill.execution_policy import sandbox_execution_policy

    ctx = SimpleNamespace(execution_limits=ExecutionLimits(45, 90, 8388608))
    seen = await _run_with_probe(monkeypatch, ctx)
    assert seen["execution_limits"] == (45, 90, 8388608)
    assert sandbox_execution_policy.get() is None
