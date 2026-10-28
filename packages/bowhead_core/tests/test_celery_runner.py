"""Sprint multi-agent-v2 PR-#1 — CeleryTaskRunner 单测.

DI: 单测 inject dispatch_fn (mock celery.AsyncResult-like) 不真启 broker.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pytest
from bowhead_core.agent_role_candidates import ExactAgentRoleChoice
from bowhead_core.agents.celery_runner import CeleryTaskRunner
from bowhead_core.agents.celery_worker import _restore_exact_agent_choice
from bowhead_core.agents.redis_health import RedisHealthMonitor
from bowhead_core.agents.task_runner import LocalTaskRunner
from bowhead_core.agents.task_spec import (
    BusRef,
    TaskSpec,
)
from bowhead_core.agents.turn_context import (
    RuntimeTurnContext,
    SerializableTurnContext,
)

# -- Helpers --


@dataclass
class _FakeManifest:
    name: str = "compute"
    max_iterations: int = 10
    max_output_tokens: int = 8000


@dataclass
class _FakeAsyncResult:
    """Stub matching the _AsyncResultLike Protocol."""

    id: str
    _ready: bool = True
    _failed: bool = False
    _result: Any = None
    revoked: bool = False

    def ready(self) -> bool:
        return self._ready

    def failed(self) -> bool:
        return self._failed

    @property
    def result(self) -> Any:
        return self._result

    def revoke(self, *, terminate: bool = False) -> None:
        self.revoked = True


def _spec(name: str = "compute", task_id: str = "t_1") -> TaskSpec:
    return TaskSpec(agent_name=name, user_question="q", task_id=task_id)


def _sctx() -> SerializableTurnContext:
    return SerializableTurnContext(
        user_id="u_1",
        workspace_id="ws_test",
        session_id="s_a",
        turn_id="t_b",
        role="analyst",
        bus_ref=BusRef(session_id="s_a", turn_id="t_b"),
    )


def _rf(s: SerializableTurnContext) -> RuntimeTurnContext:
    return RuntimeTurnContext(serializable=s)


def _manifest_lookup(name: str) -> _FakeManifest:
    return _FakeManifest(name=name)


async def _agent_runner_fn(
    _m: Any,
    _s: Any,
    _r: Any,
) -> tuple[dict[str, Any], int, int]:
    return {"ok": True}, 0, 0


# -- Empty / fallback paths --


@pytest.mark.asyncio
async def test_empty_tasks_returns_empty() -> None:
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)
    runner = CeleryTaskRunner(monitor)
    out = await runner.dispatch([], _sctx(), _rf, _agent_runner_fn, _manifest_lookup)
    assert out == []


@pytest.mark.asyncio
async def test_unhealthy_redis_falls_back_to_local() -> None:
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(False)  # unhealthy

    fallback = LocalTaskRunner()
    runner = CeleryTaskRunner(monitor, fallback=fallback)

    tasks = [_spec("compute", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].payload == {"ok": True}


# -- Happy path with mocked dispatch_fn --


@dataclass
class _RecordingDispatcher:
    """Records calls + returns canned AsyncResult."""

    next_results: dict[str, _FakeAsyncResult] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    def __call__(
        self,
        *,
        args: list[Any],
        expires: int,
        task_time_limit: int,
        soft_time_limit: int,
    ) -> _FakeAsyncResult:
        spec_dict = args[0]
        self.calls.append(
            {
                "spec_dict": spec_dict,
                "expires": expires,
                "task_time_limit": task_time_limit,
                "soft_time_limit": soft_time_limit,
            }
        )
        return self.next_results.get(
            spec_dict["task_id"],
            _FakeAsyncResult(
                id=f"celery_{spec_dict['task_id']}",
                _result=({"agent": spec_dict["agent_name"]}, 10, 20),
            ),
        )


@pytest.mark.asyncio
async def test_dispatch_routes_via_celery() -> None:
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)
    dispatcher = _RecordingDispatcher()
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    tasks = [_spec("compute", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].payload == {"agent": "compute"}
    assert results[0].tokens_input == 10
    assert results[0].tokens_output == 20
    assert len(dispatcher.calls) == 1
    # spec_dict is JSON-safe asdict
    assert dispatcher.calls[0]["spec_dict"]["agent_name"] == "compute"
    assert dispatcher.calls[0]["spec_dict"]["task_id"] == "t_1"


def test_exact_agent_choice_survives_celery_json_boundary() -> None:
    choice = ExactAgentRoleChoice(
        role_id="image",
        candidate_id="lrc_" + "a" * 32,
        reason_code="task_match",
        agent_id="system:marketing-analyst",
        runtime_profile_name="marketing-analyst",
        assignment_id=None,
        revision_ref="arv_image",
        release_id=None,
        behavior_fingerprint="sha256:" + "b" * 64,
    )
    payload = asdict(
        TaskSpec(
            agent_name="marketing-analyst",
            user_question="生成主视觉",
            agent_choice=choice,
        )
    )["agent_choice"]

    assert _restore_exact_agent_choice(payload) == choice


@pytest.mark.asyncio
async def test_unknown_agent_returns_failed_no_dispatch() -> None:
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)
    dispatcher = _RecordingDispatcher()
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    def _bad_lookup(name: str) -> _FakeManifest:
        raise KeyError(name)

    tasks = [_spec("nonexistent", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _bad_lookup,
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error.reason == "UnknownAgent"
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_worker_failure_wraps_taskfailure() -> None:
    """ar.failed() == True → TaskFailureError wrap."""
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)

    failing_ar = _FakeAsyncResult(
        id="celery_t_1",
        _ready=True,
        _failed=True,
        _result=ValueError("worker exploded"),
    )
    dispatcher = _RecordingDispatcher(
        next_results={"t_1": failing_ar},
    )
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    tasks = [_spec("compute", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert results[0].ok is False
    assert results[0].error.reason == "ValueError"


@pytest.mark.asyncio
async def test_result_shape_error_caught() -> None:
    """worker 返非 (payload, tin, tout) tuple → ResultShapeError."""
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)

    bad_ar = _FakeAsyncResult(
        id="celery_t_1",
        _ready=True,
        _failed=False,
        _result={"oops": "wrong shape"},
    )
    dispatcher = _RecordingDispatcher(next_results={"t_1": bad_ar})
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    tasks = [_spec("compute", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert results[0].ok is False
    assert results[0].error.reason == "ResultShapeError"


@pytest.mark.asyncio
async def test_dispatch_apply_async_failure_marks_failed() -> None:
    """celery apply_async raise → spec marked failed (CeleryDispatchError) + 不 stop 其他."""
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)

    def _broken_dispatcher(**_kwargs: object) -> _FakeAsyncResult:
        raise ConnectionError("broker down")

    runner = CeleryTaskRunner(
        monitor,
        dispatch_fn=_broken_dispatcher,
        poll_interval_s=0.0,
    )
    tasks = [_spec("compute", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert results[0].ok is False
    assert results[0].error.reason == "CeleryDispatchError"


@pytest.mark.asyncio
async def test_cancel_revokes_running() -> None:
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)
    ar = _FakeAsyncResult(id="celery_t_1", _ready=True, _result=({"k": 1}, 0, 0))
    dispatcher = _RecordingDispatcher(next_results={"t_1": ar})
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    # Manually plant an in-running entry
    runner._running["t_2"] = _FakeAsyncResult(id="celery_t_2", _ready=False)

    await runner.cancel(["t_2", "t_does_not_exist"])
    assert runner._running["t_2"].revoked is True


@pytest.mark.asyncio
async def test_timeout_passed_to_celery_apply_async() -> None:
    """compute_task_timeout(manifest) → expires/task_time_limit; soft = main - 10."""
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)
    dispatcher = _RecordingDispatcher()
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    # max_iterations=20 → timeout = 100s
    def _lookup_with_iters(name: str) -> _FakeManifest:
        return _FakeManifest(name=name, max_iterations=20)

    tasks = [_spec("compute", "t_1")]
    await runner.dispatch(tasks, _sctx(), _rf, _agent_runner_fn, _lookup_with_iters)
    call = dispatcher.calls[0]
    assert call["expires"] == 100
    assert call["task_time_limit"] == 100
    assert call["soft_time_limit"] == 90


@pytest.mark.asyncio
async def test_polls_until_ready() -> None:
    """ar.ready() False → keep polling, then True returns result."""
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)

    @dataclass
    class _PollingAR:
        id: str = "celery_t_1"
        revoked: bool = False
        _calls: int = 0

        def ready(self) -> bool:
            self._calls += 1
            return self._calls >= 3  # ready on 3rd check

        def failed(self) -> bool:
            return False

        @property
        def result(self) -> tuple[dict[str, Any], int, int]:
            return {"slow": True}, 1, 2

        def revoke(self, *, terminate: bool = False) -> None:
            self.revoked = True

    polling_ar = _PollingAR()
    dispatcher = _RecordingDispatcher(next_results={"t_1": polling_ar})  # type: ignore[dict-item]
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    tasks = [_spec("compute", "t_1")]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert results[0].ok is True
    assert results[0].payload == {"slow": True}
    assert polling_ar._calls >= 3


@pytest.mark.asyncio
async def test_multiple_tasks_dispatch_parallel() -> None:
    monitor = RedisHealthMonitor("redis://test")
    monitor.force_set(True)
    dispatcher = _RecordingDispatcher()
    runner = CeleryTaskRunner(monitor, dispatch_fn=dispatcher, poll_interval_s=0.0)

    tasks = [
        _spec("compute", "t_1"),
        _spec("financial-analyst", "t_2"),
        _spec("business-analyst", "t_3"),
    ]
    results = await runner.dispatch(
        tasks,
        _sctx(),
        _rf,
        _agent_runner_fn,
        _manifest_lookup,
    )
    assert len(results) == 3
    assert all(r.ok for r in results)
    assert {r.task_spec.task_id for r in results} == {"t_1", "t_2", "t_3"}
