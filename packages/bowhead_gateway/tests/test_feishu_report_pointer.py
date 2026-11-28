"""Feishu keeps the report pointer sentence (chat running state R2.2).

The web answer of a report Turn no longer ends with "完整报告见画布。": the web shows a report card. Feishu has no
report card, so its answer card appends the localized sentence once, live and when the mirror never saw the events.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_gateway.feishu import bridge as feishu_bridge
from bowhead_gateway.feishu.bridge import answer_text_for_card, with_report_pointer
from bowhead_gateway.im.turn_render import NeutralTurnState, apply_event


@dataclass
class _Ev:
    event_type: str
    payload: dict[str, Any]


def _report_turn() -> NeutralTurnState:
    state = NeutralTurnState(turn_id="t_report")
    apply_event(state, _Ev("canvas.section_added", {"section": {"id": "sec_1", "kind": "narrative", "markdown": "x"}}))
    return state


def test_a_report_answer_gets_the_pointer_in_the_answer_language_once() -> None:
    state = _report_turn()
    assert answer_text_for_card(state, "Sales fell 8%.", answer_locale="en-US") == "Sales fell 8%.\n\nThe full report is on the canvas."
    assert answer_text_for_card(state, "华东下滑 8%。", answer_locale="zh-CN") == "华东下滑 8%。\n\n完整报告见画布。"
    # A Planning Mode answer or a Turn from before the report card already carries it, in any language.
    assert answer_text_for_card(state, "计划完成。\n\n完整报告见画布。", answer_locale="en-US") == "计划完成。\n\n完整报告见画布。"


def test_a_chat_answer_and_an_empty_answer_stay_as_they_are() -> None:
    assert answer_text_for_card(NeutralTurnState(turn_id="t_chat"), "华东下滑 8%。", answer_locale="zh-CN") == "华东下滑 8%。"
    assert with_report_pointer("  ", "zh-CN") == "  "
    # An unknown locale falls back to English, as report chrome does.
    assert with_report_pointer("Done.", "xx-XX") == "Done.\n\nThe full report is on the canvas."


class _Store:
    def __init__(self, payloads: list[dict[str, Any]] | Exception) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    async def list_events(self, turn_id: str, *, event_types: tuple[str, ...] | None = None) -> list[Any]:
        self.calls.append((turn_id, event_types))
        if isinstance(self.payloads, Exception):
            raise self.payloads
        return [SimpleNamespace(payload=payload) for payload in self.payloads]


async def _changed(store: _Store | None) -> bool:
    fake = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(turn_log_store=store)))
    return await feishu_bridge.FeishuTurnBridge._turn_changed_report(fake, "t_1")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_a_turn_without_live_events_reads_report_changes_from_its_stored_payload() -> None:
    store = _Store(
        [{"report_changes": {"added": [], "updated": []}}, {"report_changes": {"added": [{"section_id": "s", "title": "概览"}], "updated": []}}]
    )
    assert await _changed(store) is True
    assert store.calls == [("t_1", ("lead.final.payload",))]
    assert await _changed(_Store([{"outputs": [{"kind": "presentation", "count": 1}]}])) is False
    assert await _changed(_Store([])) is False
    assert await _changed(_Store(RuntimeError("db locked"))) is False
    assert await _changed(None) is False
