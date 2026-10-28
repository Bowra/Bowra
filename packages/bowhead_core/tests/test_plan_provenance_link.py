"""批 4 S1: USED 边 derive 链接键 (batch4-provenance-design D-A).

铁纪律(review P2-9): 钉在真 seam——真 `make_legacy_agent_runner_fn` 闭包 + fake
SubagentRunner **真消费 kwargs["pre_minted_subagent_id"]**(与真签名同形, 先例
test_subagent_peer_dispatched_v4)。dispatch 级 fake 摸不到这条 seam=假绿。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents.agent_runner_adapter import make_legacy_agent_runner_fn
from bowhead_core.agents.task_spec import TaskSpec
from bowhead_core.subagent import SubagentResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _CapturingRunner:
    """与真 SubagentRunner.run 同形的 keyword-only 消费(含 pre_minted_subagent_id)."""

    def __init__(self) -> None:
        self.seen_kwargs: list[dict] = []

    async def run(self, **kwargs) -> SubagentResult:
        self.seen_kwargs.append(kwargs)
        return SubagentResult(
            profile=str(kwargs["profile_name"]),
            subagent_id=str(kwargs.get("pre_minted_subagent_id") or "sub_selfminted"),
            ok=True,
            final_answer="ok",
            payload={"exec_summary": "ok", "fallback_markdown": "# ok\n内容"},
        )


def _manifest(name: str = "compute"):
    return SimpleNamespace(name=name, prompt_file=None, max_iterations=6)


def _ctx():
    # Minimal duck ctx: adapter reads only getattr.
    return SimpleNamespace(active_datarc_project="", accessible_datarc_workspaces=(), kb_role=None)


async def _invoke(spec: TaskSpec):
    runner = _CapturingRunner()
    fn = make_legacy_agent_runner_fn(
        subagent_runner=runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=None,  # type: ignore[arg-type]
        parent_ctx=_ctx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    runtime_ctx = SimpleNamespace(serializable=SimpleNamespace(kb_role=None, workspace_id="w", turn_id="t_test"))
    await fn(_manifest(), spec, runtime_ctx)  # type: ignore[arg-type]
    return runner


async def test_plan_step_task_derives_subagent_id() -> None:
    """plan 步(ps_ 前缀 task_id) → run() 必须收到 sub_{step_id}."""
    runner = await _invoke(TaskSpec(agent_name="compute", user_question="q", task_id="ps_ab12cd34ef56"))
    assert runner.seen_kwargs[0]["pre_minted_subagent_id"] == "sub_ps_ab12cd34ef56"


async def test_non_plan_task_does_not_premint() -> None:
    """普通 multi-agent 派发(自动默认 t_*) → 不预铸, runner 自铸(id 形状不变)."""
    runner = await _invoke(TaskSpec(agent_name="compute", user_question="q"))
    assert runner.seen_kwargs[0]["pre_minted_subagent_id"] is None


async def test_legacy_backend_forwards_pre_minted() -> None:
    """批4 P1-4：唯一的执行分支必须转发 pre_minted_subagent_id（丢了 → provenance
    的 USED 边 join 全空）。原先这条同时钉 langgraph 分支，该 backend 已于 2026-08
    下线（Agent SDK 调研 A2），只剩 legacy 一支要钉。"""
    import inspect

    import bowhead_core.subagent as subagent_mod

    src = inspect.getsource(subagent_mod.SubagentRunner)
    assert src.count("pre_minted_subagent_id=pre_minted_subagent_id") >= 1, "legacy 分支必须转发"


async def test_derive_through_local_task_runner_run_one() -> None:
    """设计 S1 铁纪律全链形状: LocalTaskRunner._run_one → 真 _runner_fn →
    fake SubagentRunner 消费 pre_minted(dispatch seam 那半也钉住, review P2-4)."""
    from bowhead_core.agents.task_runner import LocalTaskRunner

    runner = _CapturingRunner()
    fn = make_legacy_agent_runner_fn(
        subagent_runner=runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=None,  # type: ignore[arg-type]
        parent_ctx=_ctx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    spec = TaskSpec(agent_name="compute", user_question="q", task_id="ps_ff00ff00ff00")
    sctx = SimpleNamespace(kb_role=None, workspace_id="w", turn_id="t_test")
    runtime_ctx = SimpleNamespace(serializable=sctx)
    result = await LocalTaskRunner()._run_one(spec, sctx, lambda s: runtime_ctx, fn, lambda name: _manifest())  # type: ignore[arg-type]
    assert result.ok
    assert runner.seen_kwargs[0]["pre_minted_subagent_id"] == "sub_ps_ff00ff00ff00"
