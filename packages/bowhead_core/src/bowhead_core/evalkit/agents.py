"""内建 agent —— 仅供 CLI 自测。

生产 chat 不得在 eval harness 直接调 `orchestrator.run_turn`；必须先经
Gateway `admit_legacy_chat_execution` + `spawn_turn` 建立持久化与 dispatch 围栏。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.evalkit.cases import EvalCase


async def echo_agent(case: EvalCase) -> tuple[Any, dict[str, Any] | None]:
    """回显 case.input —— 验 CLI pipeline / 写 expected=input 的冒烟测试."""
    return case.input, None
