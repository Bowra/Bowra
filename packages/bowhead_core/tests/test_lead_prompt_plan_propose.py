"""graph-mode — Auto 模式的「Step 0 要不要先出计划」长在 Lead 决策树里。

**两轮真机才收敛的一条**：

1. 116/1.0.97：`plan_propose` 挂进了 lead.yaml 的 allow-list、注册进 registry、
   role/visible 与 `clarification_ask` 逐字段相同 —— 但 Lead **一次都没调过**，
   连用户当场打字「请立刻调用 plan_propose」都不调。根因 = 它在整份 prompt 里出现 0 次。
2. 116/1.1.0：补成**决策树旁边的兄弟块**后，注入链路证明完全正常
   （Auto 下 Lead 能把那段**逐字复述**，off 下答「没有」）—— **但它照样不执行**：
   「财务/业务运营/合规三方向各出专项再合并」这种请求，Lead 并行派了 3 个不同专家 peer
   （它自己的行为证明这就是多专家项目），Step 0 却没触发。
   ⇒ 模型执行的程序是**那棵树本身**（自称「分 N 步严格分类, 不允许跳步」），
   住在树外面的「Step 0」不在流程里。**必须长在树内**，步数也要跟着改。

这里钉四件事：①Step 0 在树内且排在 Step 1 之前 ②步数 3↔4 跟着切换
③非 Auto 逐字节不变 ④「是不是 Auto」的判据认 **None**（FE 约定 auto 不上 wire）。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import (
    build_lead_system_prompt,
    build_tool_use_guidance,
    planning_auto_from_mode,
)

_STEP0 = "**Step 0 — Propose a plan first?**"
_TREE_OPEN = "<query_classification_decision_tree>"
_STEP1 = "**Step 1 — Does the query contain a concrete topic identifier?**"


def _build(*, planning_auto: bool) -> str:
    return build_lead_system_prompt(
        "ws_test",
        facts=[],
        planning_auto=planning_auto,
    )


def test_absent_by_default() -> None:
    """默认 False —— 既有调用方（含全部老测试）拿到的 prompt 逐字节不变。"""
    assert _STEP0 not in build_lead_system_prompt("ws_test", facts=[])


def test_absent_when_not_auto() -> None:
    assert _STEP0 not in _build(planning_auto=False)


def test_present_when_auto() -> None:
    prompt = _build(planning_auto=True)
    assert _STEP0 in prompt
    # Name the tool itself: asking only for judgment without its name leaves Lead unsure what to call
    assert "plan_propose" in prompt
    # Before dispatch is crucial: proposing halfway through makes users wait, then approve again
    assert "before dispatching anything" in prompt


def test_step0_lives_inside_the_tree_before_step1() -> None:
    """★真机第二轮的正身：树外的 Step 0 不在模型执行的流程里。

    突变：把 Step 0 挪回树外（兄弟 section）→ 这条红。
    """
    tree = build_tool_use_guidance(planning_auto=True)
    assert tree.index(_TREE_OPEN) < tree.index(_STEP0) < tree.index(_STEP1)


def test_step_count_tracks_the_injection() -> None:
    """步数是这条指令的一部分：注入 Step 0 却仍写「分 3 步」会自相矛盾。"""
    assert "Classify strictly in 4 steps" in build_tool_use_guidance(planning_auto=True)
    assert "Classify strictly in 3 steps" in build_tool_use_guidance(planning_auto=False)


def test_non_auto_tree_differs_only_by_the_inserted_block() -> None:
    """非 Auto 路必须逐字节等于「原树」——除了步数那一处，不许有别的漂移。"""
    off = build_tool_use_guidance(planning_auto=False)
    on = build_tool_use_guidance(planning_auto=True)
    assert off == on.replace("Classify strictly in 4 steps", "Classify strictly in 3 steps").replace(on[on.index(_STEP0) : on.index(_STEP1)], "")


def test_dispatch_selfcheck_is_present() -> None:
    """真机上 Lead 是「一次派 2 个 peer」却不出卡，所以自检必须锚在这个可观测行为上。"""
    assert "dispatch 2 or more peers at once" in build_tool_use_guidance(planning_auto=True)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # ★ FE omits auto from the wire; actual Auto turns pass None.
        # Mutating the criterion to `mode == "auto"` immediately fails this case.
        (None, True),
        ("auto", True),
        ("planning", False),
        ("off", False),
    ],
)
def test_planning_auto_from_mode(mode: str | None, expected: bool) -> None:
    assert planning_auto_from_mode(mode) is expected
