"""Sprint θ Phase 0d Spike #1: callable_peers e2e (no LLM).

**Purpose**: Verify that the callable_peers framework — manifest schema +
registry validation + dispatch_parallel + agent_runner_fn closure — runs
end-to-end with stub manifests, BEFORE building real Lead orchestrator.

**Pass criteria** (per PRD r2.5 §4.0):
- AgentManifest with callable_peers: list[str] validates
- dispatch_parallel(tasks=[peer_task], ...) returns TaskResult OK
- parent_ctx (SerializableTurnContext) is passed through to peer's runtime
- I4 invariant: workspace_id propagated to peer parent_ctx
- I2 invariant: peer manifest's callable_peers is empty (1-hop)

**Known gap exposed by this spike** (track for Phase 8b-4):
- SerializableTurnContext lacks `project_id` and `kb_role` fields. PRD §4.0.1
  I4 requires both propagated. Phase 8b-4 must add these fields.

Run: `cd packages/bowhead_core && uv run pytest tests/spike/test_callable_peers_e2e.py -v`
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.agents.dispatcher import dispatch_parallel
from bowhead_core.agents.manifest import AgentManifest
from bowhead_core.agents.task_runner import LocalTaskRunner
from bowhead_core.agents.task_spec import BusRef, TaskSpec
from bowhead_core.agents.turn_context import (
    RuntimeTurnContext,
    SerializableTurnContext,
)


def _make_lead_stub_manifest() -> AgentManifest:
    """Lead manifest with callable_peers: [kb-analyst-stub]."""
    return AgentManifest.model_validate(
        {
            "name": "lead-stub",
            "model": "lead",
            "trust_class": "read-only",
            "max_iterations": 10,
            "max_output_tokens": 1000,
            "description": "Lead orchestrator stub for spike #1",
            "system": {"file": "stub.md"},
            "tools": {"default_enabled": False, "allow": ["kb_search"]},
            "callable_peers": ["kb-analyst-stub"],
        }
    )


def _make_kb_analyst_stub_manifest() -> AgentManifest:
    """kb-analyst manifest with callable_peers: [] (1-hop invariant I2)."""
    return AgentManifest.model_validate(
        {
            "name": "kb-analyst-stub",
            "model": "analyst",
            "trust_class": "read-only",
            "max_iterations": 5,
            "max_output_tokens": 1000,
            "description": "KB analyst stub for spike #1",
            "system": {"file": "stub.md"},
            "tools": {"default_enabled": False, "allow": ["kb_search"]},
            "callable_peers": [],  # I2: peers must have empty callable_peers
        }
    )


def _make_sctx() -> SerializableTurnContext:
    """Make SerializableTurnContext with workspace_id (PRD I4 needs).

    Known gap: SerializableTurnContext currently lacks `project_id` and `kb_role`.
    PRD §4.0.1 I4 requires both propagated to peer. Phase 8b-4 must add these
    fields to SerializableTurnContext + dispatcher passes them through.
    """
    return SerializableTurnContext(
        user_id="u_spike",
        workspace_id="ws_spike_1",
        session_id="s_spike",
        turn_id="t_spike",
        role="orchestrator",
        bus_ref=BusRef(session_id="s_spike", turn_id="t_spike"),
    )


def _make_runtime_factory() -> Any:
    def _f(sctx: SerializableTurnContext) -> RuntimeTurnContext:
        return RuntimeTurnContext(serializable=sctx)

    return _f


# ===== Spike tests =====


def test_spike1_pass1_manifest_with_callable_peers_validates() -> None:
    """Pass 1: AgentManifest with callable_peers list validates per Pydantic."""
    lead = _make_lead_stub_manifest()
    assert lead.callable_peers == ["kb-analyst-stub"]
    assert len(lead.callable_peers) == 1


def test_spike1_pass2_kb_analyst_callable_peers_empty_is_one_hop() -> None:
    """Pass 2: kb-analyst-stub manifest has callable_peers=[] (I2 1-hop)."""
    kb = _make_kb_analyst_stub_manifest()
    assert kb.callable_peers == []


@pytest.mark.asyncio
async def test_spike1_pass3_dispatch_parallel_runs_e2e_with_lead_calling_peer() -> None:
    """Pass 3: dispatch_parallel completes end-to-end with stub Lead+peer manifests.

    Mocks agent_runner_fn (no real LLM); tests framework path:
    Lead manifests → dispatch_parallel(kb_task) → LocalTaskRunner → mock runner_fn
    """
    captured_runtime_ctxs: list[RuntimeTurnContext] = []

    async def _mock_kb_runner(
        manifest: AgentManifest,
        spec: TaskSpec,
        runtime_ctx: RuntimeTurnContext,
    ) -> tuple[dict[str, Any], int, int]:
        """Mock kb-analyst runner — captures runtime_ctx, returns fixed chunks."""
        captured_runtime_ctxs.append(runtime_ctx)
        return (
            {
                "chunks": [
                    {"id": "c1", "text": "Stub chunk A"},
                    {"id": "c2", "text": "Stub chunk B"},
                ],
                "source_refs": ["kb_chunk_id:c1", "kb_chunk_id:c2"],
            },
            150,  # tokens_used
            0,  # cost_usd_micros (mock)
        )

    kb_manifest = _make_kb_analyst_stub_manifest()
    sctx = _make_sctx()

    spec = TaskSpec(
        agent_name="kb-analyst-stub",
        user_question="查询合同 X",
        routing_reason="Spike #1: Lead callable_peer dispatch test",
    )

    results = await dispatch_parallel(
        [spec],
        sctx,
        _make_runtime_factory(),
        _mock_kb_runner,
        manifest_lookup=lambda _name: kb_manifest,
        runner=LocalTaskRunner(),
    )

    # Framework end-to-end: 1 task in, 1 result out
    assert len(results) == 1
    result = results[0]
    assert result.ok is True
    assert "chunks" in result.payload
    assert len(result.payload["chunks"]) == 2
    assert result.payload["source_refs"][0] == "kb_chunk_id:c1"


@pytest.mark.asyncio
async def test_spike1_pass4_workspace_id_propagated_to_peer_runtime_ctx() -> None:
    """Pass 4 (I4 partial): workspace_id propagates from sctx to peer's runtime_ctx."""
    captured_workspace_ids: list[str] = []

    async def _capture_runner(
        manifest: AgentManifest,
        spec: TaskSpec,
        runtime_ctx: RuntimeTurnContext,
    ) -> tuple[dict[str, Any], int, int]:
        captured_workspace_ids.append(runtime_ctx.serializable.workspace_id)
        return ({"ok": True}, 0, 0)

    spec = TaskSpec(agent_name="kb-analyst-stub", user_question="q")
    await dispatch_parallel(
        [spec],
        _make_sctx(),
        _make_runtime_factory(),
        _capture_runner,
        manifest_lookup=lambda _: _make_kb_analyst_stub_manifest(),
        runner=LocalTaskRunner(),
    )

    assert captured_workspace_ids == ["ws_spike_1"]


def test_spike1_pass5_i4_invariant_now_supported() -> None:
    """Pass 5: I4 invariant now supported — Phase 8b-4 added project_id + kb_role.

    PRD §4.0.1 I4 requires dispatcher propagate workspace_id + project_id + kb_role
    to peer parent_ctx. Spike #1 originally documented the gap; Phase 8b-4 closed it
    by adding fields to SerializableTurnContext.
    """
    sctx = _make_sctx()
    assert hasattr(sctx, "workspace_id")
    assert hasattr(sctx, "project_id")  # Phase 8b-4 added
    assert hasattr(sctx, "kb_role")  # Phase 8b-4 added


@pytest.mark.asyncio
async def test_spike1_pass6_parallel_dispatch_returns_in_order() -> None:
    """Pass 6: dispatch_parallel with N=3 tasks returns results in input order."""

    async def _runner_with_id(
        manifest: AgentManifest,
        spec: TaskSpec,
        runtime_ctx: RuntimeTurnContext,
    ) -> tuple[dict[str, Any], int, int]:
        return ({"task_id": spec.user_question}, 0, 0)

    specs = [TaskSpec(agent_name="kb-analyst-stub", user_question=f"task_{i}") for i in range(3)]
    results = await dispatch_parallel(
        specs,
        _make_sctx(),
        _make_runtime_factory(),
        _runner_with_id,
        manifest_lookup=lambda _: _make_kb_analyst_stub_manifest(),
        runner=LocalTaskRunner(),
    )

    assert [r.payload["task_id"] for r in results] == ["task_0", "task_1", "task_2"]
