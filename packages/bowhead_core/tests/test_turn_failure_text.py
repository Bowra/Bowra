"""失败时用户看到的是人话，还是一串给模型的指令。

116 dogfood 2026-09-24 `t_7f66c0d681c6`（Lead 跑 1171s 撞上 900s 的 wallclock 闸）：
对话里「Bowhead 牵头 / compute」下面渲出来的终答是——

    insufficient_data: peer_wallclock_exhausted: 1171s > 900s.
    Stop iterating — return what we have. 已停止迭代以节省用户时间.

那是写给 Lead 看的协议串。peer 用它是对的（Lead 会读），可 **Lead 自己的
`final_answer` 上面没有消费者**，它直接就是用户看到的那句话。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.lead_orchestrator import _public_turn_failure_reason
from bowhead_core.report_chrome_text import chrome_text
from bowhead_core.turn_failure_text import (
    INSUFFICIENT_DATA_PREFIX,
    cap_bail_final_answer,
    choose_silent_done_text,
)

_CAP = "peer_wallclock_exhausted: 1171s > 900s. Stop iterating — return what we have."


@pytest.mark.unit
def test_the_lead_never_hands_the_model_instruction_to_the_user() -> None:
    assert cap_bail_final_answer("lead", _CAP) == ""


@pytest.mark.unit
def test_a_peer_keeps_the_protocol_prefix_because_the_lead_reads_it() -> None:
    """★ 负对照：别把整条改掉。peer 这段有 Lead 读，`insufficient_data:` 是工具描述里
    写明的约定，也是 `_public_turn_failure_reason` 的分类依据。"""
    out = cap_bail_final_answer("compute", _CAP)

    assert out.startswith(INSUFFICIENT_DATA_PREFIX)
    assert _CAP in out


@pytest.mark.unit
def test_the_machine_reason_still_reaches_classification() -> None:
    """清空 `final_answer` 不能把失败分类一起清掉——它读的是 `error`，不是这里。
    真跑一遍分类函数，别只读代码。"""
    assert _public_turn_failure_reason(_CAP) == "peer_dispatch_timeout"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["error", "failed"])
def test_a_failure_with_a_public_reason_publishes_no_system_answer(status: str) -> None:
    """Chat turn feedback D3: the failure card is the only failure statement.

    Neither the cap-bail empty string nor a carried system failure sentence becomes the answer.
    """
    empty = chrome_text("turn_empty_hint", "zh-CN")

    assert choose_silent_done_text(cap_bail_final_answer("lead", _CAP), status=status, empty_hint=empty, has_public_failure=True) is None
    assert choose_silent_done_text("本次分析未能完成。请根据错误提示重试。", status=status, empty_hint=empty, has_public_failure=True) is None


@pytest.mark.unit
def test_a_failure_without_a_public_reason_writes_no_generic_failure_prose() -> None:
    """No public reason: a carried sentence still survives, but nothing generic is invented."""
    empty = chrome_text("turn_empty_hint", "zh-CN")
    carried = "已产出的图表见画布。"

    assert choose_silent_done_text(carried, status="error", empty_hint=empty, has_public_failure=False) == carried
    assert choose_silent_done_text("  ", status="error", empty_hint=empty, has_public_failure=False) is None


@pytest.mark.unit
def test_a_carried_user_facing_text_still_wins_on_a_completed_turn() -> None:
    """Negative control: an upstream may deliberately place a user-facing sentence on a completed turn."""
    carried = "这是本轮的回答。"

    text = choose_silent_done_text(
        carried,
        status="completed",
        empty_hint=chrome_text("turn_empty_hint", "zh-CN"),
        has_public_failure=False,
    )

    assert text == carried


@pytest.mark.unit
@pytest.mark.parametrize("lang", ["zh-CN", "en-US", "de-DE", "fr-FR"])
def test_an_empty_but_successful_turn_gets_the_localized_empty_line(lang: str) -> None:
    """No output is not a failure: completed and empty still gets ``turn_empty_hint``."""
    empty = chrome_text("turn_empty_hint", lang)

    assert choose_silent_done_text("  ", status="completed", empty_hint=empty, has_public_failure=False) == empty


@pytest.mark.unit
def test_the_zh_cn_empty_line_uses_full_width_punctuation() -> None:
    text = chrome_text("turn_empty_hint", "zh-CN")

    assert text == "本次没有产出可展示的内容。请再描述一下你想看的数据，或者换个更具体的问法（例如时间范围、维度）。"
    for half_width in (". ", ", ", "(", ")", "e.g."):
        assert half_width not in text


@pytest.mark.unit
def test_both_call_sites_actually_go_through_these_functions() -> None:
    """★ 接线守卫（不是行为测试）。

    上面几条测的是纯函数本身；`run_subagent` / `_lead_emit` 的完整 harness 太重，
    没有任何用例能跑到调用点——突变实测证实了这一点：把调用点改回原来的内联机器串，
    **七条测试全绿**。纯函数再对，没人调也等于没修
    （[[feedback_correct_but_out_of_reach]]）。所以这里直接钉住接线。
    """
    import inspect

    from bowhead_core.agents import lead_orchestrator

    from bowhead_core import subagent

    sub_src = inspect.getsource(subagent)
    assert "cap_bail_final_answer(profile.name, _cap_tripped)" in sub_src
    assert '"insufficient_data: " + _cap_tripped' not in sub_src, "撞闸分支又内联回机器串了"

    lead_src = inspect.getsource(lead_orchestrator)
    assert "choose_silent_done_text(" in lead_src
    assert "has_public_failure=failure_event_reason_code(payload) is not None" in lead_src
    assert "_silent_done_fallback = _persisted_fallback" not in lead_src, "兜底分支又内联回去了"
