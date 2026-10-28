"""Celery worker task definition (v2 PR-#1).

Worker process imports this module to register `run_task_celery`.

⚠️ Cross-process invariants:
- args 必须 JSON 可序列化 (TaskSpec dict + SerializableTurnContext dict)
- 真 agent_runner_fn 不可 pickle, worker boot 内 register一次 via _bind_runner_factory
- BusRef 在 worker 内 reconstruct (RuntimeTurnContext.reconstruct 调真 bus 连接)

PR-#1 ship-ready: 此 task body 在 worker 内调 worker-side 注册的
agent_runner_factory; 真集成留 v2 #11 - 此 module 仅 task 签名 + 工厂注册口.
单测 mock CeleryTaskRunner.dispatch_fn, 不进 worker.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .manifest import AgentManifest
    from .task_runner import AgentRunnerFn

logger = logging.getLogger(__name__)

_runner_factory: Any = None


def _restore_exact_agent_choice(value: Any) -> Any:
    """Restore the typed Plan identity flattened by ``dataclasses.asdict``."""
    if value is None:
        return None
    from bowhead_core.agent_role_candidates import ExactAgentRoleChoice

    if isinstance(value, ExactAgentRoleChoice):
        return value
    if not isinstance(value, dict):
        raise ValueError("invalid exact Agent choice")
    wire = dict(value)
    wire.setdefault("schema_version", 1)
    return ExactAgentRoleChoice.from_json(
        json.dumps(wire, ensure_ascii=False, separators=(",", ":")),
    )


def bind_runner_factory(factory: Any) -> None:
    """Worker boot 调一次, 注册 agent_runner_factory (sub-runner 工厂).

    factory(profile_name, sctx) → AgentRunnerFn — worker 内 reconstruct
    runtime ctx + 真 sub_runner.
    """
    global _runner_factory  # noqa: PLW0603
    _runner_factory = factory


def get_runner_factory() -> Any:
    if _runner_factory is None:
        raise RuntimeError(
            "celery worker has no agent_runner_factory bound; call bind_runner_factory() in worker boot",
        )
    return _runner_factory


def _build_celery_task() -> Any:
    """Lazy build celery task (cely import deferred).

    `run_task_celery.apply_async(...)` is called from CeleryTaskRunner main process;
    worker side runs the body. Returns (payload_dict, tokens_in, tokens_out) tuple.
    """
    from .celery_app import get_celery_app

    app = get_celery_app()

    @app.task(name="bowhead.run_task", bind=True)
    def run_task_celery(
        self: Any,
        spec_dict: dict[str, Any],
        sctx_dict: dict[str, Any],
    ) -> tuple[dict[str, Any], int, int]:
        from .registry import AgentRegistry
        from .task_spec import BusRef, TaskSpec
        from .turn_context import (
            RuntimeTurnContext,
            SerializableTurnContext,
        )

        # Reconstruct typed objects from JSON dicts
        bus_ref_d = spec_dict.get("bus_ref") or sctx_dict.get("bus_ref")
        bus_ref = (
            BusRef(
                session_id=bus_ref_d["session_id"],
                turn_id=bus_ref_d["turn_id"],
            )
            if bus_ref_d
            else None
        )
        spec_kwargs = {k: v for k, v in spec_dict.items() if k != "bus_ref"}
        if "upstream_payloads" in spec_kwargs:
            spec_kwargs["upstream_payloads"] = tuple(
                spec_kwargs["upstream_payloads"] or [],
            )
        if "depends_on" in spec_kwargs:
            spec_kwargs["depends_on"] = tuple(spec_kwargs["depends_on"] or [])
        if "agent_choice" in spec_kwargs:
            spec_kwargs["agent_choice"] = _restore_exact_agent_choice(
                spec_kwargs["agent_choice"],
            )
        spec = TaskSpec(**spec_kwargs)

        sctx_kwargs = {k: v for k, v in sctx_dict.items() if k != "bus_ref"}
        for tup_key in (
            "upstream_payloads",
            "handoff_chain",
        ):
            if tup_key in sctx_kwargs and sctx_kwargs[tup_key] is not None:
                if tup_key == "handoff_chain":
                    sctx_kwargs[tup_key] = tuple(tuple(pair) for pair in sctx_kwargs[tup_key])
                else:
                    sctx_kwargs[tup_key] = tuple(sctx_kwargs[tup_key])
        sctx = SerializableTurnContext(bus_ref=bus_ref, **sctx_kwargs)

        runtime_ctx = RuntimeTurnContext.reconstruct(sctx)

        # subagent-unification Phase 1: the worker is a SEPARATE process from the
        # gateway, so it must bootstrap the registry AND register system manifests
        # into its own module-level PROFILES dict (mirroring
        # turn_runner_v2_entry.bootstrap_multi_agent). Without this, the worker's
        # PROFILES lacks system-manifest profiles. We run the two registry steps
        # directly (NOT bootstrap_multi_agent, which also requires a ToolRegistry
        # for canvas-writer tool registration that the worker doesn't have here).
        # Wrapped in try/except so a bad spec never crashes the worker (mirrors
        # gateway main.py:738) -- registry.get below still serves the requested
        # manifest even if registration partially failed.
        registry = AgentRegistry.bootstrap()
        try:
            from .agent_runner_adapter import (
                register_all_system_manifests,
            )

            register_all_system_manifests(registry)
        except Exception as exc:  # never let registration crash worker
            logger.warning(
                "celery worker register_all_system_manifests failed (%s); PROFILES may lack system agents",
                exc,
            )
        manifest: AgentManifest = registry.get(spec.agent_name)

        factory = get_runner_factory()
        agent_runner_fn: AgentRunnerFn = factory(spec.agent_name, sctx)

        # Run synchronously inside celery (worker is sync). Use asyncio.run
        # if agent_runner_fn is coroutine factory.
        import asyncio

        coro = agent_runner_fn(manifest, spec, runtime_ctx)
        result = asyncio.run(coro)
        # result expected as (payload_dict, tokens_in, tokens_out)
        return result

    return run_task_celery


# Lazy: import-time fail OK if celery missing — only worker boot needs this
try:
    run_task_celery = _build_celery_task()
except ImportError:
    logger.debug("celery not installed; run_task_celery unavailable in this process")
    run_task_celery = None  # type: ignore[assignment]
