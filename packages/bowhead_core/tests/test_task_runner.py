"""Sprint multi-agent-v1 PR-#5 — TaskRunner protocol + LocalTaskRunner 单测.

覆盖:
- LocalTaskRunner.dispatch 单 task / 多 task 顺序保持
- Fail-soft (1 task fail 不影响其他)
- Per-task timeout (T-BE-P2.1)
- Per-task cancel (T-CTO-P1.4)
- 错误统一 wrap TaskFailureError (T-BE-P1.4)
- compute_task_timeout 边界
- TaskResult.succeeded / failed / agent_name property
- SerializableTurnContext.with_handoff (T-SEC-P0.2)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from bowhead_core.agents import task_runner as tr_module
from bowhead_core.agents.manifest import AgentManifest
from bowhead_core.agents.task_runner import (
    LocalTaskRunner,
    compute_task_timeout,
)
from bowhead_core.agents.task_spec import (
    BusRef,
    TaskFailureError,
    TaskResult,
    TaskSpec,
)
from bowhead_core.agents.turn_context import (
    RuntimeTurnContext,
    SerializableTurnContext,
)


def _mk_manifest(name: str = "test-agent", max_iter: int = 10) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "name": name,
            "model": "analyst",
            "trust_class": "read-mostly",
            "max_iterations": max_iter,
            "max_output_tokens": 1000,
            "description": "test",
            "system": {"file": "x.md"},
            "tools": {"default_enabled": False, "allow": ["bi_query"]},
        }
    )


def _mk_sctx(**overrides: Any) -> SerializableTurnContext:
    base = {
        "user_id": "u_1",
        "workspace_id": "ws_1",
        "session_id": "s_abc",
        "turn_id": "t_xyz",
        "role": "analyst",
        "bus_ref": BusRef(session_id="s_abc", turn_id="t_xyz"),
    }
    base.update(overrides)
    return SerializableTurnContext(**base)


def _mk_runtime_factory():
    """LocalTaskRunner 的 runtime factory: 共享 in-process refs."""

    def _factory(sctx: SerializableTurnContext) -> RuntimeTurnContext:
        return RuntimeTurnContext(serializable=sctx)

    return _factory


# -- compute_task_timeout --


def test_timeout_no_manifest() -> None:
    assert compute_task_timeout(None) == 300


def test_timeout_small_manifest() -> None:
    m = _mk_manifest(max_iter=10)  # 10 * 5 = 50 < 600 cap
    assert compute_task_timeout(m) == 50


def test_timeout_capped() -> None:
    m = _mk_manifest(max_iter=200)  # 200 * 5 = 1000 > 600 cap
    assert compute_task_timeout(m) == 600


# -- TaskResult helpers --


def test_task_result_succeeded() -> None:
    spec = TaskSpec(agent_name="compute", user_question="q")
    r = TaskResult.succeeded(spec, payload={"k": "v"}, tokens_input=10, tokens_output=20)
    assert r.ok is True
    assert r.payload == {"k": "v"}
    assert r.error is None
    assert r.tokens_input == 10
    assert r.tokens_output == 20
    assert r.agent_name == "compute"


def test_task_result_failed() -> None:
    spec = TaskSpec(agent_name="compute", user_question="q")
    err = TaskFailureError("Timeout", TimeoutError())
    r = TaskResult.failed(spec, err, duration_ms=100)
    assert r.ok is False
    assert r.payload is None
    assert r.error is err
    assert r.duration_ms == 100
    assert r.agent_name == "compute"


def test_task_failure_error_str() -> None:
    err = TaskFailureError("Timeout", ValueError("oops"))
    assert "Timeout" in str(err)
    assert "oops" in str(err)
    assert err.reason == "Timeout"
    assert isinstance(err.original, ValueError)


# -- SerializableTurnContext --


def test_sctx_with_handoff() -> None:
    sctx = _mk_sctx()
    new = sctx.with_handoff("ws_2", "market-agent")
    assert new.handoff_chain == (("ws_2", "market-agent"),)
    assert new.handoff_depth == 1
    # Original unchanged (frozen).
    assert sctx.handoff_chain == ()
    assert sctx.handoff_depth == 0


def test_sctx_with_handoff_chain_grows() -> None:
    sctx = _mk_sctx()
    s1 = sctx.with_handoff("ws_1", "financial-agent")
    s2 = s1.with_handoff("ws_2", "market-agent")
    assert len(s2.handoff_chain) == 2
    assert s2.handoff_depth == 2


# -- LocalTaskRunner.dispatch --


def _make_ok_runner(payload: dict | None = None) -> Any:
    payload = payload or {"ok": True}

    async def _runner(manifest, spec, rctx):
        return payload, 100, 200

    return _runner


def _make_failing_runner(exc: Exception) -> Any:
    async def _runner(manifest, spec, rctx):
        raise exc

    return _runner


def _make_slow_runner(seconds: float) -> Any:
    async def _runner(manifest, spec, rctx):
        await asyncio.sleep(seconds)
        return {"ok": True}, 0, 0

    return _runner


@pytest.mark.asyncio
async def test_dispatch_single_task() -> None:
    runner = LocalTaskRunner()
    sctx = _mk_sctx()
    spec = TaskSpec(agent_name="compute", user_question="q")
    results = await runner.dispatch(
        [spec],
        sctx,
        _mk_runtime_factory(),
        _make_ok_runner({"finding": "x"}),
        manifest_lookup=lambda _: _mk_manifest(),
    )
    assert len(results) == 1
    r = results[0]
    assert r.ok is True
    assert r.payload == {"finding": "x"}


@pytest.mark.asyncio
async def test_dispatch_empty() -> None:
    runner = LocalTaskRunner()
    results = await runner.dispatch(
        [],
        _mk_sctx(),
        _mk_runtime_factory(),
        _make_ok_runner(),
        manifest_lookup=lambda _: _mk_manifest(),
    )
    assert results == []


@pytest.mark.asyncio
async def test_dispatch_order_preserved() -> None:
    """gather 默认: 返回顺序保持 input 顺序 (T-CTO-P1.2 上游契约)."""
    runner = LocalTaskRunner()
    specs = [TaskSpec(agent_name=f"a{i}", user_question=f"q{i}") for i in range(5)]
    results = await runner.dispatch(
        specs,
        _mk_sctx(),
        _mk_runtime_factory(),
        _make_ok_runner(),
        manifest_lookup=lambda _: _mk_manifest(),
    )
    assert [r.task_spec.agent_name for r in results] == [
        "a0",
        "a1",
        "a2",
        "a3",
        "a4",
    ]


@pytest.mark.asyncio
async def test_dispatch_fail_soft() -> None:
    """1 task fail 不影响其他 (核心 fail-soft 契约)."""
    runner = LocalTaskRunner()
    specs = [
        TaskSpec(agent_name="ok-a", user_question="q1"),
        TaskSpec(agent_name="bad-b", user_question="q2"),
        TaskSpec(agent_name="ok-c", user_question="q3"),
    ]

    async def _conditional(manifest, spec, rctx):
        if spec.agent_name == "bad-b":
            raise ValueError("simulated failure")
        return {"name": spec.agent_name}, 0, 0

    results = await runner.dispatch(
        specs,
        _mk_sctx(),
        _mk_runtime_factory(),
        _conditional,
        manifest_lookup=lambda _: _mk_manifest(),
    )
    assert len(results) == 3
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[1].error is not None
    assert results[1].error.reason == "ValueError"
    assert results[2].ok is True


@pytest.mark.asyncio
async def test_dispatch_unknown_agent() -> None:
    """manifest_lookup raises KeyError → TaskResult.failed 'UnknownAgent'."""
    runner = LocalTaskRunner()
    spec = TaskSpec(agent_name="missing", user_question="q")

    def _no_manifest(_name: str) -> AgentManifest:
        raise KeyError("missing")

    results = await runner.dispatch(
        [spec],
        _mk_sctx(),
        _mk_runtime_factory(),
        _make_ok_runner(),
        manifest_lookup=_no_manifest,
    )
    assert results[0].ok is False
    assert results[0].error is not None
    assert results[0].error.reason == "UnknownAgent"


@pytest.mark.asyncio
async def test_dispatch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-task timeout 兜底 (T-BE-P2.1).

    用 monkeypatch 把 default timeout 砍小, 测 wait_for 触发.
    """
    # Reduce timeout (max_iter=1 × 5s = 5s, still too long; multiplier is constant and cannot change).
    # Instead mock manifest max_iter=1 →5s; runner lasts 10s → timeout.
    runner = LocalTaskRunner()
    spec = TaskSpec(agent_name="slow", user_question="q")
    # Directly patch module compute_task_timeout to return 0s and trigger timeout.
    monkeypatch.setattr(tr_module, "compute_task_timeout", lambda _m: 0)

    # Note: timeout=0 immediately times out; runner does not actually sleep.
    results = await runner.dispatch(
        [spec],
        _mk_sctx(),
        _mk_runtime_factory(),
        _make_slow_runner(1.0),
        manifest_lookup=lambda _: _mk_manifest(max_iter=1),
    )
    assert results[0].ok is False
    assert results[0].error is not None
    assert results[0].error.reason == "Timeout"


@pytest.mark.asyncio
async def test_dispatch_cancel() -> None:
    """T-CTO-P1.4: cancel 中途 task."""
    runner = LocalTaskRunner()
    spec = TaskSpec(
        agent_name="slow",
        user_question="q",
        task_id="t_cancel_me",
    )

    async def _slow(_m, _s, _r):
        await asyncio.sleep(10)
        return {"ok": True}, 0, 0

    # dispatch is async; trigger cancellation separately.
    dispatch_task = asyncio.create_task(
        runner.dispatch(
            [spec],
            _mk_sctx(),
            _mk_runtime_factory(),
            _slow,
            manifest_lookup=lambda _: _mk_manifest(),
        )
    )
    # Wait 0.1s for dispatch to enter _running.
    await asyncio.sleep(0.1)
    await runner.cancel(["t_cancel_me"])
    results = await dispatch_task
    assert results[0].ok is False
    assert results[0].error is not None
    assert results[0].error.reason == "Cancelled"


@pytest.mark.asyncio
async def test_dispatch_cancel_unknown_id_no_op() -> None:
    """cancel 不存在的 task_id 不应 raise."""
    runner = LocalTaskRunner()
    await runner.cancel(["t_doesnt_exist"])  # No raise = pass.


@pytest.mark.asyncio
async def test_dispatch_concurrent() -> None:
    """gather 真并行: 3 个 task 各 sleep 0.2s, 总耗时 ~0.2s 不 0.6s."""
    runner = LocalTaskRunner()
    specs = [TaskSpec(agent_name=f"a{i}", user_question=f"q{i}") for i in range(3)]
    start = time.perf_counter()
    results = await runner.dispatch(
        specs,
        _mk_sctx(),
        _mk_runtime_factory(),
        _make_slow_runner(0.2),
        manifest_lookup=lambda _: _mk_manifest(),
    )
    elapsed = time.perf_counter() - start
    assert all(r.ok for r in results)
    # Actual parallelism: 0.2s ×3 =0.6s serial, ~0.2s parallel; allow 0.4s buffer.
    assert elapsed < 0.4, f"expected parallel, elapsed={elapsed:.3f}s"


# ── 2026-07 planning-ux-reliability: spec.timeout_s override ─────
# Plan execution steps are deliberately long tasks; driver explicitly passes remaining wall-clock budget,
# bypassing compute_task_timeout chat-mode fallback (production 500s/150s tasks all failed).


@pytest.mark.asyncio
async def test_spec_timeout_override_wins_over_manifest() -> None:
    """timeout_s=1 覆盖 manifest 默认(50s): 睡 1.3s 的 task 必须 Timeout."""
    runner = LocalTaskRunner()

    async def slow_fn(manifest, spec, ctx):
        await asyncio.sleep(1.3)
        return {}, 0, 0

    spec = TaskSpec(agent_name="test-agent", user_question="q", timeout_s=1)
    results = await runner.dispatch(
        [spec],
        _mk_sctx(),
        _mk_runtime_factory(),
        slow_fn,
        manifest_lookup=lambda _: _mk_manifest(max_iter=10),
    )
    assert not results[0].ok
    assert results[0].error.reason == "Timeout"
    # Error includes seconds (debug readability; previously only "Timeout: ").
    assert "1s" in str(results[0].error)


@pytest.mark.asyncio
async def test_spec_timeout_none_falls_back_to_manifest() -> None:
    """timeout_s=None → 沿用 compute_task_timeout, 快 task 正常成功."""
    runner = LocalTaskRunner()

    async def fast_fn(manifest, spec, ctx):
        return {"ok": 1}, 1, 2

    spec = TaskSpec(agent_name="test-agent", user_question="q")
    results = await runner.dispatch(
        [spec],
        _mk_sctx(),
        _mk_runtime_factory(),
        fast_fn,
        manifest_lookup=lambda _: _mk_manifest(max_iter=10),
    )
    assert results[0].ok
