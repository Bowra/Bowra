"""The evaluation worker delivers Owner version notifications next to failure notifications."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.agent_version_notifications import AgentVersionNoticeOutcome
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime

from bowhead_gateway import agent_eval_runtime

pytestmark = pytest.mark.unit


class _Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.claims = [SimpleNamespace(notice=SimpleNamespace(outbox_id="aevn_1", event_type="proposal.created", attempt_count=1))]
        self.settled: list[tuple[str, str]] = []
        self.retried: list[str] = []

    async def claim_version_notification(self, *, worker_id: str):
        return self.claims.pop() if self.claims else None

    async def project_version_notification(self, claim, *, notification_store):
        if self.fail:
            raise RuntimeError("inbox down")
        return AgentVersionNoticeOutcome("delivered", "notification_projected")

    async def settle_version_notification(self, claim, *, status: str, outcome_code: str) -> None:
        self.settled.append((status, outcome_code))

    async def retry_version_notification(self, claim, *, outcome_code: str) -> None:
        self.retried.append(outcome_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_one_version_notification_is_settled_or_retried(monkeypatch, fail) -> None:
    store = _Store(fail=fail)
    monkeypatch.setattr(agent_eval_runtime, "AgentVersionNotificationStore", lambda _db_path: store)
    runtime = AgentEvalRuntime(db_path="unused", state=SimpleNamespace(), notification_store=object())

    assert await runtime.process_one_version_notification(worker_id="worker:1") is True
    assert await runtime.process_one_version_notification(worker_id="worker:1") is False
    if fail:
        assert store.retried == ["agent_version_notification_projection_failed"] and store.settled == []
    else:
        assert store.settled == [("delivered", "notification_projected")] and store.retried == []


@pytest.mark.asyncio
async def test_without_an_inbox_nothing_is_claimed(monkeypatch) -> None:
    store = _Store()
    monkeypatch.setattr(agent_eval_runtime, "AgentVersionNotificationStore", lambda _db_path: store)
    runtime = AgentEvalRuntime(db_path="unused", state=SimpleNamespace())
    assert await runtime.process_one_version_notification(worker_id="worker:1") is False
    assert len(store.claims) == 1


@pytest.mark.asyncio
async def test_one_worker_tick_drains_both_owner_outboxes() -> None:
    calls: list[str] = []
    tick = asyncio.Event()

    async def _consumed(name: str) -> bool:
        calls.append(name)
        return False

    async def _version_notification(*, worker_id: str) -> bool:
        calls.append("version_notification")
        tick.set()
        return False

    runtime = SimpleNamespace(
        recover_running=lambda: _consumed("recover"),
        process_one_regression=lambda *, worker_id: _consumed("regression"),
        process_one_failure_notification=lambda *, worker_id: _consumed("failure_notification"),
        process_one_version_notification=_version_notification,
    )
    task = asyncio.create_task(agent_eval_runtime.run_agent_eval_worker(runtime, poll_interval=60))
    await asyncio.wait_for(tick.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["recover", "regression", "failure_notification", "version_notification"]
