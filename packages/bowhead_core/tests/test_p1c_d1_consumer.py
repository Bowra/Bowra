"""P1c — D1 consumer: digest answered_via + render 来源 line + source-continuity rule.

Routing efficacy itself is LLM-driven → real-LLM dogfood; these assert the digest carries the
source signal and the renderer surfaces it + the continuity rule (gated), composing with the
0.65.0 final_answer_full rendering.
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.cross_turn_digest import build_cross_turn_summary, render_recent_turns_block


def _inv(name: str, source: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(tool_name=name, source=source, arguments={}, nested=[])


def test_summary_stores_answered_via_web() -> None:  # the GDP repro turn
    s = build_cross_turn_summary(
        turn_id="t1",
        user_text="去年中国GDP",
        intent="chat",
        final_answer="约18万亿美元",
        tool_invocations=[_inv("web_search")],
    )
    assert s["answered_via"] == {"source": "web", "open_world": True}


def test_summary_dataset_turn_closed() -> None:
    s = build_cross_turn_summary(
        turn_id="t1",
        user_text="各品牌营业额",
        intent="data",
        final_answer="…",
        tool_invocations=[_inv("bi_query")],
    )
    assert s["answered_via"] == {"source": "dataset", "open_world": False}


def test_summary_no_answered_via_when_invocations_absent() -> None:
    s = build_cross_turn_summary(turn_id="t1", user_text="hi", intent=None, final_answer="hi")
    assert "answered_via" not in s


def test_render_emits_source_line_and_rule_for_open_world() -> None:
    digests = [{"user_text": "去年中国GDP", "final_answer_full": "约18万亿美元", "answered_via": {"source": "web", "open_world": True}}]
    block = render_recent_turns_block(digests, source_continuity=True)
    assert "source: web / external knowledge" in block
    assert "Source continuity" in block
    assert "do not switch to bi_query" in block


def test_render_omits_everything_when_flag_off() -> None:
    digests = [{"user_text": "x", "final_answer_full": "y", "answered_via": {"source": "web", "open_world": True}}]
    block = render_recent_turns_block(digests, source_continuity=False)
    assert "Source continuity" not in block
    assert "- source:" not in block


def test_render_dataset_turn_shows_source_but_no_rule() -> None:
    digests = [{"user_text": "营业额", "final_answer_full": "…", "answered_via": {"source": "dataset", "open_world": False}}]
    block = render_recent_turns_block(digests, source_continuity=True)
    assert "source: the Datarc workspace dataset" in block
    assert "Source continuity" not in block  # rule only fires when an open-world turn exists


def test_lead_tree_has_source_continuity_crossref() -> None:
    p = build_lead_system_prompt("ws", facts=[])
    assert "keep using that source" in p
