"""credit-billing gap 7 —— chat turn 的用量归属接线 pin。

`_run_turn_and_cleanup` 在 `orchestrator.run_turn` 外面套了 `usage_context(reason="turn")`，
这样 turn 内部**所有** LLM 调用（Lead、compose、派生的 peer / subagent task）都被记成
`reason="turn"` 且带上 team/user/workspace/turn。

这条接线只能在 gateway 侧验：in-process 直接调 `orchestrator.run_turn` 绕过了它，会得到
`unattributed`。所以这里直接驱动真的 worker 函数，用一个在 run_turn 里读环境上下文的
stub orchestrator 断言归属真的生效。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.events import EventBusRegistry
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.turn_service import admit_legacy_chat_execution
from bowhead_router.usage import UNATTRIBUTED, current_usage_context

if TYPE_CHECKING:
    from bowhead_router.usage import UsageContext


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


class _CapturingOrchestrator:
    """在 run_turn 里读当时生效的 usage context —— 模拟 Lead 发起 LLM 调用那一刻。"""

    def __init__(self) -> None:
        self.seen: UsageContext | None = None
        self.session_store = SimpleNamespace()

    async def run_turn(self, req):  # noqa: ANN001, ANN202
        self.seen = current_usage_context()
        assert await self.session_store.mark_completed(
            turn_id=req.turn_id,
            intent="lookup",
            final_answer="ok",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
        )
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            tokens_input=0,
            tokens_output=0,
            final_answer="ok",
            tool_invocations=[],
        )


def _req(**over):  # noqa: ANN003, ANN202
    base = {
        "turn_id": "t_abc",
        "session_id": "s_1",
        "user_id": "u_1",
        "workspace_id": "ws_1",
        "text": "hi",
        # Real TurnRequests always carry these; the worker's physical-usage receipt observer
        # (default on since 9c6fe1654) reads them.
        "execution_generation": 1,
        "plan_execution_id": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


async def _run_worker(orch, req, *, team_id, tmp_path):  # noqa: ANN001, ANN202
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    session_store = SessionStore(core_path)
    await session_store.initialize()
    await session_store.ensure_session(
        session_id=req.session_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
    )
    await session_store.record_turn(
        turn_id=req.turn_id,
        session_id=req.session_id,
        user_text=req.text,
    )
    turn_log_store = TurnLogStore(str(core_path))
    await turn_log_store.initialize()
    orch.session_store = session_store
    orch.event_registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(turn_log_store),
    )
    admitted_bus = await admit_legacy_chat_execution(orch, req)
    await chat_routes._run_turn_and_cleanup(  # noqa: SLF001
        orch,
        req,
        user_store=None,
        team_id=team_id,
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )


@pytest.mark.asyncio
async def test_turn_worker_attributes_all_llm_calls_to_the_turn(tmp_path) -> None:  # noqa: ANN001
    """★ turn 内的调用必须带 reason="turn" + 完整归属。"""
    orch = _CapturingOrchestrator()

    await _run_worker(orch, _req(), team_id="team_a", tmp_path=tmp_path)

    ctx = orch.seen
    assert ctx is not None
    assert ctx.reason == "turn"
    assert (ctx.team_id, ctx.user_id, ctx.workspace_id, ctx.turn_id) == ("team_a", "u_1", "ws_1", "t_abc")


@pytest.mark.asyncio
async def test_context_does_not_leak_past_the_turn(tmp_path) -> None:  # noqa: ANN001
    """归属只在 turn 期间生效 —— 退出后不能污染后续无关调用。"""
    orch = _CapturingOrchestrator()

    await _run_worker(orch, _req(), team_id="team_a", tmp_path=tmp_path)

    assert current_usage_context().reason == UNATTRIBUTED
    assert current_usage_context().turn_id is None


@pytest.mark.asyncio
async def test_stub_user_without_team_still_attributed_by_turn(tmp_path) -> None:  # noqa: ANN001
    """team_id 为空（stub 用户、跳过扣费）时仍要标出 reason 与 turn_id, 否则这批用量无从归因。"""
    orch = _CapturingOrchestrator()

    await _run_worker(orch, _req(), team_id=None, tmp_path=tmp_path)

    ctx = orch.seen
    assert ctx is not None
    assert ctx.reason == "turn"
    assert ctx.turn_id == "t_abc"
    assert ctx.team_id is None
