"""Feishu wiring tests for marker resolution + trust/sources on the answer card.

Covers the two prior Feishu defects now fixed by routing the answer card through resolve_markers +
the im/turn_render neutral state: (1) no raw [cite:N]/[num:…] marker leaks into a card; (2) a
report turn's card carries a trust line + sources + caveat, while a chat turn stays silent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bowhead_gateway.feishu import renderer
from bowhead_gateway.feishu.bridge import _neutral_trust_parts
from bowhead_gateway.im.turn_render import NeutralTurnState, apply_event


@dataclass
class _Ev:
    event_type: str
    payload: dict[str, Any]


def _drive(events: list[_Ev]) -> NeutralTurnState:
    state = NeutralTurnState(turn_id="t1")
    for ev in events:
        apply_event(state, ev)
    return state


# ─── answer_card: R-1 marker resolution (defect #1) ──────────────────────────


def test_answer_card_resolves_markers() -> None:
    card = renderer.answer_card("营收见 [cite:1]，增长 [num:0.123:percent]")
    blob = json.dumps(card, ensure_ascii=False)
    assert "[cite:" not in blob and "[num:" not in blob  # no raw markers leak
    assert "[1]" in blob  # citation became [N]
    assert "12.3%" in blob  # number rendered


def test_answer_card_defaults_have_no_trust_note() -> None:
    card = renderer.answer_card("普通回答，没有 marker")
    blob = json.dumps(card, ensure_ascii=False)
    assert "已验证" not in blob  # chat-style card: no trust line when not supplied


# ─── answer_card: trust line + caveat + sources (defect #2) ──────────────────


def test_answer_card_renders_linkage_lifecycle_caveat_and_generic_sources() -> None:
    card = renderer.answer_card(
        "报告正文",
        trust_line="2 处来源引用 · 1 个数字待核对",
        lifecycle_line="报告状态：所有者已标记为已核对",
        caveat="⚠ 数字待核对：毛估 X",
        sources_heading="来源",
        sources_lines=["[1] 来源", "[2] 来源"],
    )
    blob = json.dumps(card, ensure_ascii=False)
    assert "2 处来源引用 · 1 个数字待核对" in blob
    assert "报告状态：所有者已标记为已核对" in blob
    assert "⚠ 数字待核对：毛估 X" in blob
    assert "[1] 来源" in blob and "[2] 来源" in blob
    assert "**来源**" in blob
    assert "67%" not in blob and "✓" not in blob


# ─── _neutral_trust_parts: report vs chat ────────────────────────────────────


def test_neutral_trust_parts_report_turn() -> None:
    neutral = _drive(
        [
            _Ev("canvas.opened", {"canvas_id": "c1"}),
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s1",
                        "kind": "narrative",
                        "markdown": "营收 [num:347100:CNY:src:tl_a]",
                        "unverified_numbers": ["毛估 X"],
                    }
                },
            ),
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s2",
                        "kind": "sources",
                        "entries": [{"label": "datarc 查询"}, {"label": "KB 文档"}],
                    }
                },
            ),
            _Ev("final_answer", {"text": "见报告"}),
        ]
    )
    source_linkage, lifecycle, caveat, sources_heading, sources_lines = _neutral_trust_parts(neutral, locale="zh-CN")
    assert source_linkage is not None and "1 个数字待核对" in source_linkage and "2 条来源" in source_linkage
    assert "100%" not in source_linkage and "✓" not in source_linkage
    assert lifecycle is None
    assert caveat == "⚠ 数字待核对：毛估 X"
    assert sources_heading == "来源"
    assert sources_lines == ["[1] 来源", "[2] 来源"]


def test_neutral_parts_respect_english_locale_and_hide_source_details() -> None:
    neutral = _drive(
        [
            _Ev(
                "canvas.section_added",
                {
                    "section": {
                        "id": "s1",
                        "kind": "sources",
                        "entries": [{"label": "Private BI query", "artifact_id": "art_secret"}],
                    }
                },
            ),
            _Ev("canvas.done", {"trust": {"verified": 0, "unverified": 0, "sources": 1, "verified_pct": None}}),
        ]
    )
    source_linkage, lifecycle, caveat, heading, lines = _neutral_trust_parts(neutral, locale="en-GB")
    assert source_linkage is not None and "1 source" in source_linkage
    assert lifecycle is None and caveat is None and heading == "Sources"
    assert lines == ["[1] Source"]
    assert "Private BI" not in json.dumps((source_linkage, lines))
    assert "art_secret" not in json.dumps((source_linkage, lines))


def test_neutral_trust_parts_chat_turn_is_silent() -> None:
    neutral = _drive([_Ev("final_answer", {"text": "你好"})])
    assert _neutral_trust_parts(neutral) == (None, None, None, None, None)


def test_affected_feishu_output_obeys_shared_semantic_deny_fixture() -> None:
    fixture = json.loads((Path(__file__).resolve().parents[3] / "eval/fixtures/report_trust/b1a_copy_contract_v1.json").read_text(encoding="utf-8"))
    card = renderer.answer_card(
        "Report",
        trust_line="2 source citations · 1 number to check · Source citations do not mean the numbers were numerically verified.",
        lifecycle_line="Report status: marked reviewed by owner",
        caveat="⚠ Numbers to check: 12%",
        sources_heading="Sources",
        sources_lines=["[1] Source", "[2] Source"],
    )
    rendered = json.dumps(card, ensure_ascii=False).casefold()
    assert [phrase for phrase in fixture["semantic_deny"] if phrase.casefold() in rendered] == []
