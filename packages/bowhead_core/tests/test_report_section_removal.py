"""remove_report_section: a writer removes report text it wrote in this Turn.

Design: docs/sprints/report-own-section-removal-2026-11-24/tech-design.md. On 116 (`an observed task`) a writer
renamed a chapter while resending its body, so the new chapter was appended beside the old one; with no way to
remove a section it rewrote the old one with the new text and the report showed the chapter twice.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
from bowhead_core.agents.report_tools import _read_report, _remove_report_sections, _write_report
from bowhead_core.subagent import _canvas_sections_from_invocations

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path

_BODY = "导语一段。\n\n## 品类结构\n\n女装 36.8%。\n\n## 逐月趋势\n\n3 月 -20.2%。\n\n## 结论\n\n女装托底。\n"


class _Bus(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(events=[])

    async def emit(self, event: Any) -> None:
        self.events.append((event.event_type, dict(event.payload)))


@pytest.fixture
async def ctx(tmp_path: Path):
    store = CanvasStore(tmp_path / "t.db")
    await store.initialize()
    await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t1", workspace_id="w", user_id="u", parent_bus=_Bus())


async def _as(peer: str, call, *args):
    token = set_current_peer_name(peer)
    try:
        return await call(*args)
    finally:
        reset_current_peer_name(token)


async def _narrative(ctx) -> list[dict]:
    report = await _read_report({"include_full": True}, ctx)
    return [section for section in report["sections"] if section["kind"] == "narrative"]


async def test_a_renamed_chapter_is_named_by_the_resend_and_removed_by_its_writer(ctx) -> None:
    first = await _as("compute", _write_report, {"kind": "narrative", "content": _BODY}, ctx)
    ids = first["section_ids"]
    rev = next(item["rev"] for item in (await _read_report({}, ctx))["outline"] if item["section_id"] == ids[0])
    renamed = _BODY.replace("## 逐月趋势", "## 逐月走势")
    resend = await _as("compute", _write_report, {"kind": "narrative", "section_id": ids[0], "expected_rev": rev, "content": renamed}, ctx)

    assert resend["ok"] and len(resend["appended"]) == 1
    assert resend["not_in_body"] == [{"section_id": ids[2], "title": "逐月趋势"}]
    assert "remain in the report" in resend["not_in_body_note"]

    removed = await _as("compute", _remove_report_sections, {"section_ids": [ids[2]]}, ctx)

    assert removed["ok"] and removed["removed"] == [{"section_id": ids[2], "title": "逐月趋势"}] and not removed["rejected"]
    assert ids[2] not in {item["section_id"] for item in removed["your_sections"]}
    titles = [section["content"].split("\n")[0] for section in await _narrative(ctx)]
    assert "## 逐月趋势" not in titles and titles.count("## 逐月走势") == 1
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    assert ("canvas.section_removed", {"turn_id": "t1", "canvas_id": canvas.id, "section_id": ids[2]}) in ctx.parent_bus.events
    recorded = ctx.parent_bus._report_writer_sections[("compute", "narrative")]
    assert ids[2] not in ([recorded] if isinstance(recorded, str) else recorded)


async def test_a_continuation_resend_names_the_chapter_it_left_out(ctx) -> None:
    await _as("compute", _write_report, {"kind": "narrative", "content": _BODY}, ctx)
    shorter = "导语一段。\n\n## 品类结构\n\n女装 37.0%。\n\n## 结论\n\n女装托底。\n"

    resend = await _as("compute", _write_report, {"kind": "narrative", "content": shorter}, ctx)

    assert resend["ok"] and [item["title"] for item in resend["not_in_body"]] == ["逐月趋势"]


async def test_removing_a_section_takes_the_charts_only_it_references(ctx) -> None:
    store = ctx.canvas_store
    canvas = await store.get_canvas_by_session("s1")
    chart = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"title": "趋势", "spec": {}}, by_turn_id="t1")
    shared = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"title": "结构", "spec": {}}, by_turn_id="t1")
    doomed = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": f"## 趋势\n\n[figure:{chart.id}] [figure:{shared.id}]"},
        by_turn_id="t1",
        owner="compute",
    )
    await store.append_section(
        canvas_id=canvas.id, kind="narrative", payload={"markdown": f"## 结构\n\n[figure:{shared.id}]"}, by_turn_id="t1", owner="compute"
    )

    removed = await _as("compute", _remove_report_sections, {"section_ids": [doomed.id]}, ctx)

    assert removed["ok"] and removed["removed_figures"] == [chart.id]
    left = {section.id for section in await store.list_sections(canvas.id, include_hidden=True)}
    assert doomed.id not in left and chart.id not in left and shared.id in left
    assert {payload["section_id"] for event, payload in ctx.parent_bus.events if event == "canvas.section_removed"} == {doomed.id, chart.id}


async def test_only_the_writers_own_report_text_of_this_turn_is_removed(ctx) -> None:
    store = ctx.canvas_store
    canvas = await store.get_canvas_by_session("s1")
    earlier = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "## 上轮"}, by_turn_id="t0", owner="compute")
    others = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "## 别人"}, by_turn_id="t1", owner="analyst")
    card = await store.append_section(canvas_id=canvas.id, kind="insight_card", payload={"title": "卡片"}, by_turn_id="t1", owner="compute")
    mine = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "## 我的"}, by_turn_id="t1", owner="compute")
    await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "## 我的第二章"}, by_turn_id="t1", owner="compute")

    result = await _as("compute", _remove_report_sections, {"section_ids": [earlier.id, others.id, card.id, "sec_missing", mine.id]}, ctx)

    assert result["ok"] and [item["section_id"] for item in result["removed"]] == [mine.id]
    assert {item["section_id"]: item["reason"] for item in result["rejected"]} == {
        earlier.id: "earlier_turn",
        others.id: "not_yours",
        card.id: "not_report_text",
        "sec_missing": "not_found",
    }
    left = {section.id for section in await store.list_sections(canvas.id, include_hidden=True)}
    assert {earlier.id, others.id, card.id} <= left


async def test_a_writer_keeps_at_least_one_section(ctx) -> None:
    first = await _as("compute", _write_report, {"kind": "narrative", "content": "## 唯一一章\n\n内容。"}, ctx)

    result = await _as("compute", _remove_report_sections, {"section_ids": first["section_ids"]}, ctx)

    assert not result["ok"] and result["error"] == "keeps_none" and "write_report" in result["hint"]
    assert len(await _narrative(ctx)) == len(first["section_ids"])
    assert not [event for event, _ in ctx.parent_bus.events if event == "canvas.section_removed"]


async def test_a_write_after_a_removal_never_targets_the_removed_section(ctx) -> None:
    first = await _as("compute", _write_report, {"kind": "narrative", "content": _BODY}, ctx)
    ids = first["section_ids"]
    await _as("compute", _remove_report_sections, {"section_ids": [ids[2]]}, ctx)

    again = await _as("compute", _write_report, {"kind": "narrative", "content": _BODY.replace("36.8%", "37.0%")}, ctx)

    assert again["ok"] and ids[2] not in again["updated"]
    assert sum(section["content"].startswith("## 逐月趋势") for section in await _narrative(ctx)) == 1


def test_lead_is_not_offered_sections_a_helper_removed() -> None:
    def call(tool: str, result: dict, arguments: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(tool_name=tool, result=result, arguments=arguments or {})

    invocations = [
        call("add_chart", {"section_id": "sec_chart", "kind": "chart"}, {"title": "趋势"}),
        call("add_chart", {"section_id": "sec_kept", "kind": "chart"}, {"title": "结构"}),
        call("write_report", {"ok": True, "section_id": "sec_old", "kind": "narrative"}),
        call("remove_report_section", {"ok": True, "removed": [{"section_id": "sec_old", "title": "趋势"}], "removed_figures": ["sec_chart"]}),
    ]

    assert [entry["id"] for entry in _canvas_sections_from_invocations(invocations)] == ["sec_kept"]
