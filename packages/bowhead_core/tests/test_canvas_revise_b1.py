"""canvas-update-turn 批 B1（2026-09-14）——文档块原语与 B1 各项。

权威：docs/sprints/canvas-update-turn-2026-09-03/tech-design-b1.md。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.canvas_revise import (
    build_kpi_cells,
    document_block_for,
    insert_before_tail,
    is_document_h1,
    latest_document_block,
)

pytestmark = pytest.mark.unit


def _s(i: str, kind: str, seq: int, *, sup: bool = False, p: dict | None = None, owner: str | None = None, turn: str = "t1"):
    return SimpleNamespace(id=i, kind=kind, seq=seq, superseded=sup, payload=p or {}, owner=owner, turn_id=turn)


def _doc(with_second: bool = False) -> list:
    secs = [
        _s("e", "eyebrow", 0),
        _s("h", "h1", 1),
        _s("m", "meta", 2),
        _s("k", "kpi_row", 3, owner="sub_p1"),
        _s("n1", "narrative", 4, p={"markdown": "a", "style": "executive"}),
        _s("c1", "chart", 5),
        _s("src", "sources", 6),
        _s("cav", "narrative", 7, p={"markdown": "**数据局限性**\n- x", "style": "caveats"}),
        _s("prov", "narrative", 8, p={"markdown": "**数据可信度提示**\n- y", "style": "caveats"}),
        _s("rec", "recommendations", 9),
        _s("code", "code_block", 10),
    ]
    if with_second:
        secs += [_s("h2", "h1", 11, turn="t3"), _s("n3", "narrative", 12, turn="t3", p={"markdown": "b"})]
    return secs


# ─── B1-0 document blocks ──────────────────────────────────────


def test_document_block_shape_and_tail() -> None:
    secs = _doc()
    b = document_block_for(secs, "n1")
    assert b is not None and b.h1.id == "h" and b.eyebrow.id == "e" and b.meta.id == "m" and b.kpi_row.id == "k"  # kpi_row owner unrestricted.
    assert [s.id for s in b.body] == ["n1", "c1", "src", "cav", "prov", "rec", "code"]
    assert [s.id for s in b.tail] == ["src", "cav", "prov", "rec"] and b.tail_start_seq == 6  # Appendix code does not break tail area.
    assert b.sources.id == "src" and b.recommendations.id == "rec" and b.provenance.id == "prov"
    assert [c.id for c in b.caveats] == ["cav", "prov"]
    assert document_block_for(secs, "h").h1.id == "h"  # Anchor is h1 itself.
    assert document_block_for(secs, "nope") is None
    assert latest_document_block(secs).h1.id == "h"
    # Plan chapter h1 and folded h1 are no document boundary; eager card-document h1 (chapter=false) is.
    chapter = _s("ch", "h1", 20, p={"_meta": {"composed_by": "eager", "chapter": True}}, owner="sub_ps_1")
    assert not is_document_h1(chapter) and not is_document_h1(_s("x", "h1", 21, sup=True))
    assert is_document_h1(_s("eag", "h1", 22, p={"_meta": {"composed_by": "eager", "chapter": False}}, owner="sub_p9"))


def test_document_block_closes_at_next_document_h1_and_skips_superseded() -> None:
    secs = _doc(with_second=True)
    b1 = document_block_for(secs, "n1")
    assert "n3" not in {s.id for s in b1.body} and b1.h1.id == "h"
    b2 = document_block_for(secs, "n3")
    assert b2.h1.id == "h2" and [s.id for s in b2.body] == ["n3"] and b2.tail == []
    # Interposed folded prior report does not change ownership.
    secs2 = _doc() + [_s("oldh", "h1", 11, sup=True), _s("oldn", "narrative", 12, sup=True), _s("n9", "narrative", 13, turn="t2")]
    assert document_block_for(secs2, "n9").h1.id == "h"
    # Plan document: chapter h1 does not close block.
    plan = [_s("h", "h1", 1), _s("k", "kpi_row", 2)] + [
        _s("ch1", "h1", 3, p={"_meta": {"composed_by": "eager", "chapter": True}}, owner="sub_ps_1"),
        _s("cn1", "narrative", 4, p={"markdown": "c1", "_meta": {"composed_by": "eager", "chapter": True}}, owner="sub_ps_1"),
        _s("ch2", "h1", 5, p={"_meta": {"composed_by": "eager", "chapter": True}}, owner="sub_ps_2"),
        _s("cn2", "narrative", 6, p={"markdown": "c2", "_meta": {"composed_by": "eager", "chapter": True}}, owner="sub_ps_2"),
    ]
    assert document_block_for(plan, "cn2").h1.id == "h"


def test_insert_before_tail_moves_chapter_and_keeps_appendix() -> None:
    secs = _doc() + [_s("n2", "narrative", 11, turn="t2"), _s("c2", "chart", 12, turn="t2"), _s("code2", "code_block", 13, turn="t2")]
    b = document_block_for(secs, "n1")
    order = insert_before_tail(secs, b, ["n2", "c2"])
    assert order == ["e", "h", "m", "k", "n1", "c1", "n2", "c2", "src", "cav", "prov", "rec", "code", "code2"]
    assert insert_before_tail(secs, b, []) is None
    assert insert_before_tail(secs, b, ["ghost"]) is None
    no_tail = [_s("h", "h1", 1), _s("n1", "narrative", 2), _s("n2", "narrative", 3, turn="t2")]
    assert insert_before_tail(no_tail, document_block_for(no_tail, "n1"), ["n2"]) is None


def test_insert_before_tail_keeps_hidden_rows_in_place_and_ignores_them_in_moving() -> None:
    """chat-mode 隐藏段：既不参与落位（一个隐藏段不该把整轮落位废掉），也不许被挤到画布末尾。"""
    secs = [
        _s("h", "h1", 1),
        _s("n1", "narrative", 2),
        _s("hid", "chart", 3, turn="t0"),
        _s("src", "sources", 4),
        _s("new", "narrative", 5, turn="t2"),
        _s("newhid", "chart", 6, turn="t2"),
    ]
    for s in secs:
        s.hidden_for_chat_mode = s.id in ("hid", "newhid")
    order = insert_before_tail(secs, document_block_for(secs, "n1"), ["new", "newhid"])
    assert order == ["h", "n1", "hid", "new", "src", "newhid"]  # hid stays; newhid neither moved nor lost.


def test_insert_before_tail_keeps_folded_rows_in_their_slots() -> None:
    """review #2：返回整份序，折叠段原地不动（否则 reorder_sections 把折叠历史搬到当前报告后面）。"""
    secs = [
        _s("h", "h1", 1),
        _s("old1", "narrative", 2, sup=True),
        _s("n1", "narrative", 3),
        _s("src", "sources", 4),
        _s("old2", "h1", 5, sup=True),
        _s("new", "narrative", 6, turn="t2"),
    ]
    order = insert_before_tail(secs, document_block_for(secs, "n1"), ["new"])
    assert order == ["h", "old1", "n1", "new", "old2", "src"]


def test_document_block_excludes_next_report_eyebrow_and_finds_kpi_past_executive() -> None:
    """review #1：下一份报告的 eyebrow 不算进上一块（否则尾部区被打断、双重归属）；
    review #5：h1 → meta → narrative(executive) → kpi_row 的落序（未规范排序）也能认出 kpi_row。"""
    secs = _doc() + [_s("e2", "eyebrow", 11, turn="t3"), _s("h2", "h1", 12, turn="t3"), _s("n3", "narrative", 13, turn="t3")]
    b1 = document_block_for(secs, "n1")
    assert "e2" not in b1.section_ids and [s.id for s in b1.tail] == ["src", "cav", "prov", "rec"]
    b2 = document_block_for(secs, "n3")
    assert b2.eyebrow.id == "e2" and "e2" not in b1.section_ids
    assert insert_before_tail(secs, b1, ["n3"]) is not None
    raw = [_s("h", "h1", 1), _s("m", "meta", 2), _s("x", "narrative", 3, p={"style": "executive"}), _s("k", "kpi_row", 4), _s("n", "narrative", 5)]
    b = document_block_for(raw, "n")
    assert b.kpi_row.id == "k" and b.meta.id == "m" and [s.id for s in b.body] == ["x", "n"]


def test_provenance_section_recognised_in_both_languages() -> None:
    from bowhead_core.canvas_revise import is_provenance_section
    from bowhead_core.report_chrome_text import chrome_text

    for lang in ("zh-CN", "en-US"):
        heading = chrome_text("provenance_heading", lang)
        assert is_provenance_section(_s("p", "narrative", 1, p={"markdown": f"{heading}\n- x", "style": "caveats"})), lang
    assert is_provenance_section(_s("p", "narrative", 1, p={"markdown": "随便", "_meta": {"provenance": True}}))
    assert not is_provenance_section(_s("p", "narrative", 1, p={"markdown": "**数据局限性**\n- x", "style": "caveats"}))


def test_build_kpi_cells_shape() -> None:
    cells = build_kpi_cells(
        {
            "kpis": [
                {"label": "营收", "value": "1 亿", "delta": "-26.69%", "sentiment": "bad"},
                {"label": "客单", "value": "88", "unit": "元"},
                {"label": "坏", "value": ""},
                "junk",
            ]
        }
    )
    assert cells == [{"lbl": "营收", "val": "1 亿", "sub": "▼ -26.69%", "subTone": "neg"}, {"lbl": "客单", "val": "88", "sub": "元"}]
    assert build_kpi_cells(None) == []
    assert build_kpi_cells({"kpis": [{"label": "x", "value": "1", "delta": "+3%", "sentiment": "good"}]})[0]["subTone"] == "pos"


# ─── B1-0 store / events / marking ──────────────────────────────


@pytest.fixture
async def store(tmp_path):
    from pathlib import Path

    from bowhead_core.canvas_store import CanvasStore

    s = CanvasStore(Path(tmp_path) / "b1.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


async def test_list_revisions_by_turn_orders_same_second_patches_by_rowid(store) -> None:
    cv = await store.get_canvas_by_session("s1")
    n = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "v0"}, by_turn_id="t1", owner="sub_a")
    for i in range(1, 4):  # Multiple patches in same second.
        await store.patch_section(section_id=n.id, patch={"markdown": f"v{i}"}, expected_rev=i, by_turn_id="t2", caller=None)
    rows = await store.list_revisions_by_turn("t2")
    assert [r[0] for r in rows] == [n.id] * 3
    assert [r[1].get("markdown") for r in rows] == ["v0", "v1", "v2"]  # First = body before this turn edits.
    assert await store.list_revisions_by_turn("t1") == [] and await store.list_revisions_by_turn("") == []


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


async def test_event_helpers_shape(store) -> None:
    from bowhead_core.canvas_revise import emit_section_patched, emit_sections_superseded

    cv = await store.get_canvas_by_session("s1")
    n = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "x"}, by_turn_id="t1")
    bus = _Bus()
    await emit_section_patched(bus, turn_id="t2", canvas_id=cv.id, section=n, open_turn=True)
    assert [e.event_type for e in bus.events] == ["canvas.turn_streaming", "canvas.section_patched"]
    p = bus.events[1].payload
    assert p["section_id"] == n.id and p["kind"] == "narrative" and p["rev"] == 1 and p["seq"] == n.seq and p["patch"]["id"] == n.id
    bus2 = _Bus()
    await emit_sections_superseded(bus2, canvas_id=cv.id, turn_id="t2", section_ids=[n.id])
    assert bus2.events[0].event_type == "canvas.sections_superseded" and bus2.events[0].payload["superseded_section_ids"] == [n.id]
    await emit_sections_superseded(bus2, canvas_id=cv.id, turn_id="t2", section_ids=[])
    assert len(bus2.events) == 1


async def test_cross_turn_bake_mints_document_numbers_when_sources_are_canvas_scoped(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1-5：来源段是画布作用域（canvas-elements 合并，registry 从画布级来源段回灌）时，跨轮改段照铸文档编号；
    画布已分叉（两份来源段 ⇒ turn 作用域）时仍按 B0 剥号。"""
    from bowhead_core.report_writer_loop import bake_report_markdown
    from bowhead_core.sources_resolver import get_turn_sources_registry

    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "旧报告"}, by_turn_id="t1")
    n = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "旧文 [1]。"}, by_turn_id="t1", owner="sub_prev")
    await store.append_section(
        canvas_id=cv.id, kind="sources", payload={"entries": [{"label": "BI: 首轮", "tool": "bi_query", "artifact_id": "art_1"}]}, by_turn_id="t1"
    )
    bus = _Bus()
    ctx = SimpleNamespace(
        parent_bus=bus, canvas_store=store, session_id="s1", turn_id="t2", _report_bake_invocations=[], _cites_are_source_numbers=True
    )
    reg = await get_turn_sources_registry(bus, canvas_store=store, session_id="s1", turn_id="t2")
    assert reg.size() == 1  # Backfilled from canvas-level sources.
    reg.ingest_payload_sources([{"label": "BI: 本轮", "tool": "bi_query", "artifact_id": "art_2"}], [])
    md, _ = await bake_report_markdown("上半年 3.3 亿 [cite:2]，旧文 [1]。", ctx=ctx, is_update=True, cross_turn=True)
    assert "[2]" in md and "[1]" in md and "[cite:" not in md  # Continue document numbering, retain old numbers.
    # Forked canvas: second sources section ⇒ turn scope ⇒ no minting.
    await store.append_section(
        canvas_id=cv.id, kind="sources", payload={"entries": [{"label": "另一份", "tool": "bi_query", "artifact_id": "art_9"}]}, by_turn_id="t0"
    )
    bus2 = _Bus()
    ctx2 = SimpleNamespace(
        parent_bus=bus2, canvas_store=store, session_id="s1", turn_id="t2", _report_bake_invocations=[], _cites_are_source_numbers=True
    )
    reg2 = await get_turn_sources_registry(bus2, canvas_store=store, session_id="s1", turn_id="t2")
    reg2.ingest_payload_sources([{"label": "BI: 本轮", "tool": "bi_query", "artifact_id": "art_2"}], [])
    md2, _ = await bake_report_markdown("上半年 3.3 亿 [cite:1]。", ctx=ctx2, is_update=True, cross_turn=True)
    assert "[cite:" not in md2 and "[1]" not in md2
    assert n.id  # keep the narrative referenced


def _request(turn: str, language_context: Any = None) -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id=turn, text="q", language_context=language_context)


def _zh_language() -> Any:
    """The full-report fixture carries zh-CN chrome pills; revise it in the same language."""
    from bowhead_core.language_context import LanguageContext

    return LanguageContext(ui_language="zh-CN", output_language="zh-CN")


async def _seed_report(store) -> dict[str, str]:
    """t1：h1 / kpi_row / 正文 / sources / 溯源段。"""
    cv = await store.get_canvas_by_session("s1")
    ids = {}
    ids["h1"] = (await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "旧报告"}, by_turn_id="t1")).id
    ids["kpi"] = (
        await store.append_section(canvas_id=cv.id, kind="kpi_row", payload={"cells": [{"lbl": "营收", "val": "9.9 亿"}]}, by_turn_id="t1")
    ).id
    ids["n1"] = (
        await store.append_section(
            canvas_id=cv.id, kind="narrative", payload={"markdown": "## 旧正文", "style": "executive"}, by_turn_id="t1", owner="sub_prev"
        )
    ).id
    ids["src"] = (
        await store.append_section(
            canvas_id=cv.id, kind="sources", payload={"entries": [{"label": "BI", "tool": "bi_query", "artifact_id": "a1"}]}, by_turn_id="t1"
        )
    ).id
    ids["prov"] = (
        await store.append_section(
            canvas_id=cv.id,
            kind="narrative",
            payload={"markdown": "**数据可信度提示**\n\n- 旧的一条", "style": "caveats", "_meta": {"provenance": True}},
            by_turn_id="t1",
        )
    ).id
    ids["cv"] = cv.id
    return ids


async def test_append_mode_places_chapter_before_tail(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1-4：append 档本轮的章 + 图落到文档尾部区（sources/溯源段）之前，来源画布级合并不另立段。"""
    from bowhead_core.report_writer_loop import _maybe_finish_peer_authored_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_report(store)
    n2 = await store.append_section(
        canvas_id=ids["cv"], kind="narrative", payload={"markdown": "## 各省对比", "style": "executive"}, by_turn_id="t2", owner="sub_now"
    )
    c2 = await store.append_section(canvas_id=ids["cv"], kind="chart", payload={"spec": {}}, by_turn_id="t2")
    bus = _Bus()
    bus._lead_canvas_mode = "append"
    kinds = await _maybe_finish_peer_authored_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"exec_summary": "加了一节", "sources": [{"label": "BI2", "tool": "bi_query", "artifact_id": "a2"}]},
        tool_invocations=[],
    )
    assert kinds is not None and "h1" not in kinds
    order = [s.id for s in await store.list_sections(ids["cv"])]
    assert order == [ids["h1"], ids["kpi"], ids["n1"], n2.id, c2.id, ids["src"], ids["prov"]], order
    assert sum(1 for s in await store.list_sections(ids["cv"]) if s.kind == "sources") == 1  # Canvas-level merge.
    assert any(e.event_type == "canvas.reordered" and e.payload.get("reason") == "append_place" for e in bus.events)


async def test_append_mode_merges_provenance_instead_of_second_section(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 dogfood R10（`s_0a9c3b144419`）：加章轮带了造数守卫的溯源提示，亲笔收尾器另立了第二个「数据可信度提示」段。
    append 档必须并进文档块已有的溯源段（按行去重），画布上只留一个。"""
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX
    from bowhead_core.report_writer_loop import _maybe_finish_peer_authored_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_report(store)
    await store.append_section(
        canvas_id=ids["cv"], kind="narrative", payload={"markdown": "## 各品类月度趋势", "style": "executive"}, by_turn_id="t2", owner="sub_now"
    )
    bus = _Bus()
    bus._lead_canvas_mode = "append"
    line = f"{FABRICATION_CAVEAT_PREFIX}洞察 1 正文中的 73.3%"
    await _maybe_finish_peer_authored_report(
        canvas_store=store, request=_request("t2"), bus=bus, payload={"exec_summary": "加了一节", "caveats": [line]}, tool_invocations=[]
    )
    secs = await store.list_sections(ids["cv"])
    prov = [s for s in secs if s.kind == "narrative" and (s.payload.get("_meta") or {}).get("provenance")]
    assert [s.id for s in prov] == [ids["prov"]], [s.id for s in prov]  # Only original one remains.
    assert line in prov[0].payload["markdown"] and prov[0].rev == 2


async def test_revise_finisher_merges_provenance_into_existing_section(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1-5：修订轮的溯源提示合并进文档块已有的溯源段（按行去重、rev+1），不另立第二段。"""
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_report(store)
    await store.patch_section(
        section_id=ids["n1"], patch={"markdown": "## 新口径"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True
    )
    bus = _Bus()
    kinds = await compose_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"exec_summary": "改了", "caveats": [FABRICATION_CAVEAT_PREFIX + "7.7 亿"]},
        router=None,
    )
    assert "provenance_caveats" in kinds
    secs = await store.list_sections(ids["cv"])
    prov = [s for s in secs if s.kind == "narrative" and (s.payload or {}).get("style") == "caveats"]
    assert len(prov) == 1 and prov[0].id == ids["prov"] and prov[0].rev == 2
    assert "旧的一条" in prov[0].payload["markdown"] and "7.7 亿" in prov[0].payload["markdown"]
    # Merge same entry again ⇒ unchanged.
    await compose_report(
        canvas_store=store, request=_request("t3"), bus=_Bus(), payload={"caveats": [FABRICATION_CAVEAT_PREFIX + "7.7 亿"]}, router=None
    )


# ─── B1-1 in-place chrome patch / B1-7 revision integrity ────────


async def _seed_full_report(store) -> dict[str, str]:
    """t1 卡片路形状：eyebrow / h1(owner=peer) / meta(更新于) / kpi_row(owner=peer) / 导语 / 洞察段 / sources / 局限段。"""
    from bowhead_core.report_chrome_text import chrome_text

    cv = await store.get_canvas_by_session("s1")
    ids: dict[str, str] = {"cv": cv.id}

    async def add(key, kind, payload, owner=None):
        ids[key] = (await store.append_section(canvas_id=cv.id, kind=kind, payload=payload, by_turn_id="t1", owner=owner)).id

    await add("eyebrow", "eyebrow", {"text": "2026 全年"})
    await add("h1", "h1", {"text": "2026年各门店销售复盘", "_meta": {"composed_by": "eager", "chapter": False}}, owner="sub_p1")
    await add("meta", "meta", {"pills": [chrome_text("updated_on", "zh-CN", date="2026-01-01"), "1 个数据系统", "Bowhead 生成"]})
    await add(
        "kpi",
        "kpi_row",
        {"cells": [{"lbl": "Q3 总营业额", "val": "22.44 亿元", "sub": "环比 -42.99%"}, {"lbl": "中位数", "val": "-25.68%"}]},
        owner="sub_p1",
    )
    await add("intro", "narrative", {"markdown": "整体 QoQ -42.99%，40 家门店无一正增长。", "style": "executive"}, owner="sub_p1")
    await add("ins", "narrative", {"markdown": "## 口径\n\n整体 QoQ -42.99%。[figure:x]", "style": "executive"}, owner="sub_p1")
    await add("src", "sources", {"entries": [{"label": "BI", "tool": "bi_query", "artifact_id": "a1"}]})
    await add("cav", "narrative", {"markdown": "**数据局限性 / 注意事项**\n\n- 老局限", "style": "caveats"})
    return ids


async def _seed_projected_report(store) -> dict[str, str]:
    """**现产形状**（`FEATURE_CANVAS_DOC_PROJECTION` 默认开，文档极简化后）：
    h1 / kpi_row / 导语 / 洞察 / 建议=narrative(style=recommendations) / 局限 / sources。
    eyebrow 与 meta 已退役，建议不再是独立 kind——B1 的 chrome patch 与尾部区判定都必须认这套。"""
    cv = await store.get_canvas_by_session("s1")
    ids: dict[str, str] = {"cv": cv.id}

    async def add(key, kind, payload, owner=None, turn="t1"):
        ids[key] = (await store.append_section(canvas_id=cv.id, kind=kind, payload=payload, by_turn_id=turn, owner=owner)).id

    await add("h1", "h1", {"text": "2026 全年销售复盘", "_meta": {"composed_by": "eager", "chapter": False}}, owner="sub_p1")
    await add("kpi", "kpi_row", {"cells": [{"lbl": "总营业额", "val": "22.44 亿元", "sub": "环比 -42.99%"}]}, owner="sub_p1")
    await add("intro", "narrative", {"markdown": "整体 QoQ -42.99%。", "style": "executive"}, owner="sub_p1")
    await add("ins", "narrative", {"markdown": "## 口径\n\n整体 QoQ -42.99%。", "style": "executive"}, owner="sub_p1")
    await add("rec", "narrative", {"markdown": "## 建议\n\n1. 收缩门店", "style": "recommendations"})
    await add("cav", "narrative", {"markdown": "**数据局限性 / 注意事项**\n\n- 老局限", "style": "caveats"})
    await add("src", "sources", {"entries": [{"label": "BI", "tool": "bi_query", "artifact_id": "a1"}]})
    return ids


async def test_document_block_on_projected_shape(store) -> None:
    """建议段投影成 narrative 之后：它仍是 block.recommendations，也仍属尾部区（否则加章落在建议之后）。"""
    ids = await _seed_projected_report(store)
    secs = await store.list_sections(ids["cv"])
    block = document_block_for(secs, ids["ins"])
    assert block is not None and block.h1.id == ids["h1"] and block.kpi_row.id == ids["kpi"]
    assert block.meta is None and block.eyebrow is None  # Both retired.
    assert block.recommendations is not None and block.recommendations.id == ids["rec"]
    assert [s.id for s in block.tail] == [ids["rec"], ids["cav"], ids["src"]]


@pytest.mark.parametrize("lang", ["zh-CN", "en-US", "de-DE", "fr-FR"])
def test_legacy_provenance_section_is_recognized_in_every_language(lang: str) -> None:
    """没打 `_meta.provenance` 的存量溯源段按标题认：每种支持语言都要认得（review 2026-10-01：此前只认中英）。"""
    from types import SimpleNamespace as NS

    from bowhead_core.canvas_revise import is_provenance_section
    from bowhead_core.report_chrome_text import chrome_text

    assert is_provenance_section(NS(payload={"markdown": f"{chrome_text('provenance_heading', lang)}\n\n- x"}))
    assert not is_provenance_section(NS(payload={"markdown": f"{chrome_text('caveats_heading', lang)}\n\n- x"}))


@pytest.mark.parametrize("lang", ["zh", "en", "de", "fr"])
async def test_revise_keeps_guard_caveat_lines_in_every_language(store, monkeypatch: pytest.MonkeyPatch, lang: str) -> None:
    """作者重写整组局限时，造数守卫的指控行必须留下（不能交给写手转述，见 `provenance_caveats`）。
    review（2026-10-01）：修订这边此前手写了中英两个前缀，德 / 法报告里的指控行会被写手整组重写掉。"""
    from bowhead_core.report_writer_loop import compose_report

    from bowhead_core import analysis_payload as ap

    prefix = {
        "zh": ap.FABRICATION_CAVEAT_PREFIX,
        "en": ap.FABRICATION_CAVEAT_PREFIX_EN,
        "de": ap.FABRICATION_CAVEAT_PREFIX_DE,
        "fr": ap.FABRICATION_CAVEAT_PREFIX_FR,
    }[lang]
    guard = f"{prefix}7.7"
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_projected_report(store)
    await store.patch_section(
        section_id=ids["cav"],
        patch={"markdown": f"**数据局限性 / 注意事项**\n\n- 老局限\n- {guard}"},
        expected_rev=1,
        by_turn_id="t1",
        caller=None,
        skip_owner_check=True,
    )
    await store.patch_section(
        section_id=ids["ins"],
        patch={"markdown": "## 口径修正\n\n整体 QoQ -26.69%。"},
        expected_rev=1,
        by_turn_id="t2",
        caller="sub_now",
        skip_owner_check=True,
    )
    await compose_report(
        canvas_store=store, request=_request("t2"), bus=_Bus(), payload={"exec_summary": "口径改成 1-6 月", "caveats": ["新局限"]}, router=None
    )
    md = {s.id: s for s in await store.list_sections(ids["cv"])}[ids["cav"]].payload["markdown"]
    assert "新局限" in md and "老局限" not in md  # 作者那几条确实整组重写了（正对照）
    assert guard in md


async def test_revise_patches_projected_recommendations_and_keeps_system_caveat(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """现产形状下 B1-1 真的改得动建议段（patch markdown 不是 items）；系统生成的缺腿局限不被作者局限覆盖。"""
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_projected_report(store)
    gap = await store.append_section(
        canvas_id=ids["cv"],
        kind="narrative",
        payload={
            "markdown": "**数据局限性 / 注意事项**\n\n- 「compute」这步执行失败，本报告未包含其分析结果。",
            "style": "caveats",
            "_meta": {"gap": True},
        },
        by_turn_id="t1",
    )
    await store.patch_section(
        section_id=ids["ins"],
        patch={"markdown": "## 口径修正\n\n整体 QoQ -26.69%。"},
        expected_rev=1,
        by_turn_id="t2",
        caller="sub_now",
        skip_owner_check=True,
    )
    await compose_report(
        canvas_store=store,
        request=_request("t2"),
        bus=_Bus(),
        payload={
            "exec_summary": "口径改成 1-6 月",
            "kpis": [{"label": "总营业额", "value": "13.1 亿元", "delta": "-26.69%", "sentiment": "negative"}],
            "recommendations": [{"priority": "P0", "action": "按新口径重排门店优先级"}],
            "caveats": ["新局限"],
        },
        router=None,
    )
    secs = {s.id: s for s in await store.list_sections(ids["cv"])}
    assert "按新口径重排门店优先级" in secs[ids["rec"]].payload["markdown"] and secs[ids["rec"]].rev == 2  # Projected recommendations updated.
    assert secs[ids["rec"]].payload.get("items") is None  # No items inserted into projection.
    assert secs[ids["cav"]].payload["markdown"].endswith("- 新局限")
    assert "执行失败" in secs[gap.id].payload["markdown"] and secs[gap.id].rev == 1  # System missing-leg notice retained.


async def test_revise_finisher_patches_chrome_and_flags_incomplete(store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_full_report(store)
    # Only insight revised (-42.99% → -26.69%); intro/KPI row unchanged.
    await store.patch_section(
        section_id=ids["ins"],
        patch={"markdown": "## 口径修正\n\n整体 QoQ -26.69%。"},
        expected_rev=1,
        by_turn_id="t2",
        caller="sub_now",
        skip_owner_check=True,
    )
    bus = _Bus()
    kinds = await compose_report(
        canvas_store=store,
        request=_request("t2", _zh_language()),
        bus=bus,
        payload={
            "title": "2026年Q3 各门店销售复盘",
            "exec_summary": "改口径",
            "kpis": [
                {"label": "Q3 总营业额", "value": "22.44 亿元", "delta": "-26.69%", "sentiment": "bad"},
                {"label": "中位数", "value": "-25.68%"},
            ],
            "caveats": ["新局限"],
        },
        router=None,
    )
    # `kinds` contains **section kinds** only: caveats is narrative style, map to narrative, never insert new terms here.
    assert "h1" in kinds and "kpi_row" in kinds and "meta" in kinds and "provenance_caveats" in kinds
    assert "caveats" not in kinds and kinds.count("narrative") >= 2
    secs = {s.id: s for s in await store.list_sections(ids["cv"])}
    assert secs[ids["h1"]].payload["text"] == "2026年Q3 各门店销售复盘" and secs[ids["h1"]].rev == 2
    assert secs[ids["kpi"]].payload["cells"][0]["sub"] == "▼ -26.69%" and secs[ids["kpi"]].rev == 2
    assert secs[ids["meta"]].payload["pills"][0].startswith("更新于 ") and "2026-01-01" not in secs[ids["meta"]].payload["pills"][0]
    assert secs[ids["meta"]].payload["pills"][1:] == ["1 个数据系统", "Bowhead 生成"]  # Replace only updated-at pill.
    assert secs[ids["cav"]].payload["markdown"].endswith("- 新局限") and secs[ids["cav"]].payload["markdown"].startswith("**数据局限性")
    # B1-7: intro retains old -42.99% ⇒ create provenance notice naming it (KPI row updated, omitted).
    prov = [s for s in secs.values() if s.kind == "narrative" and (s.payload.get("_meta") or {}).get("provenance")]
    assert len(prov) == 1 and "-42.99%" in prov[0].payload["markdown"]
    # User copy: human section aliases, never internal kind.
    assert "正文" in prov[0].payload["markdown"] and "narrative" not in prov[0].payload["markdown"]
    assert "关键指标带" not in prov[0].payload["markdown"] and "kpi_row" not in prov[0].payload["markdown"]
    # Events: one section_patched per patch.
    patched = [e.payload["section_id"] for e in bus.events if e.event_type == "canvas.section_patched"]
    assert {ids["h1"], ids["kpi"], ids["meta"], ids["cav"]} <= set(patched)


async def test_revise_finisher_thin_payload_only_touches_meta(store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_full_report(store)
    await store.patch_section(
        section_id=ids["ins"],
        patch={"markdown": "## 口径\n\n整体 QoQ -42.99%（措辞收紧）。[figure:x]"},
        expected_rev=1,
        by_turn_id="t2",
        caller=None,
        skip_owner_check=True,
    )
    kinds = await compose_report(
        canvas_store=store, request=_request("t2", _zh_language()), bus=_Bus(), payload={"exec_summary": "收紧"}, router=None
    )
    assert "meta" in kinds and "h1" not in kinds and "kpi_row" not in kinds and "caveats" not in kinds
    secs = {s.id: s for s in await store.list_sections(ids["cv"])}
    assert secs[ids["h1"]].rev == 1 and secs[ids["kpi"]].rev == 1 and secs[ids["meta"]].rev == 2
    assert not any(s.kind == "narrative" and (s.payload.get("_meta") or {}).get("provenance") for s in secs.values())  # No old loss ⇒ no notice.


async def test_revise_in_another_language_still_refreshes_the_updated_pill(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """中文写成的报告，修订这一轮被判成英文（产品默认 en-US、用户没设偏好）：此前只按本轮语言的前缀「Updated 」找，
    找不到中文的「更新于 」⇒ 跳过，画布上的日期停在旧的（ede6e37f8 把上面两条用例改成用中文跑，绕开了这条路）。
    现在按 pill 自己的语言认、按它自己的语言换日期——不在中文报告里混进一枚英文 pill。"""
    import datetime as dt

    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_full_report(store)
    before = {s.id: s for s in await store.list_sections(ids["cv"])}[ids["meta"]].payload["pills"]
    await store.patch_section(
        section_id=ids["ins"],
        patch={"markdown": "## 口径\n\n整体 QoQ -42.99%（措辞收紧）。[figure:x]"},
        expected_rev=1,
        by_turn_id="t2",
        caller=None,
        skip_owner_check=True,
    )
    from bowhead_core.language_context import LanguageContext

    en = LanguageContext(ui_language="en-US", output_language="en-US")  # 显式英文轮：不依赖默认语言
    kinds = await compose_report(canvas_store=store, request=_request("t2", en), bus=_Bus(), payload={"exec_summary": "tightened"}, router=None)
    assert "meta" in kinds
    pills = {s.id: s for s in await store.list_sections(ids["cv"])}[ids["meta"]].payload["pills"]
    assert pills[0] == f"更新于 {dt.date.today().isoformat()}" and pills != before
    assert pills[1:] == before[1:]  # 只换了日期那一枚
    assert not any(isinstance(p, str) and p.startswith("Updated ") for p in pills)


async def _seed_plan_doc(store, chapters=(1, 2)):
    cv = await store.get_canvas_by_session("s1")
    meta_ch = {"composed_by": "eager", "chapter": True}
    ids = {"cv": cv.id}
    ids["h1"] = (await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "经营分析"}, by_turn_id="t1")).id
    ids["k_doc"] = (
        await store.append_section(canvas_id=cv.id, kind="kpi_row", payload={"cells": [{"lbl": "全年", "val": "116.23 亿元"}]}, by_turn_id="t1")
    ).id
    for n in chapters:
        own = f"sub_ps_{n}"
        ids[f"ch{n}_h"] = (
            await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": f"章{n}", "_meta": meta_ch}, by_turn_id="t1", owner=own)
        ).id
        ids[f"ch{n}_n"] = (
            await store.append_section(
                canvas_id=cv.id,
                kind="narrative",
                payload={"markdown": f"章{n} 全年 116.23 亿元，第{n}项 9.9 亿", "_meta": meta_ch},
                by_turn_id="t1",
                owner=own,
            )
        ).id
        ids[f"ch{n}_k"] = (
            await store.append_section(
                canvas_id=cv.id, kind="kpi_row", payload={"cells": [{"lbl": f"章{n}", "val": "9.9 亿"}], "_meta": meta_ch}, by_turn_id="t1", owner=own
            )
        ).id
    return ids


async def test_chapter_scoped_revise_patches_that_chapter_kpis_not_the_cover(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 dogfood R12：只改一章 ⇒ 新 KPI 写进那一章自己的带；封面 KPI 带、其它章、章 h1 都不动，也不折该章的带。"""
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_doc(store)
    cav = await store.append_section(
        canvas_id=ids["cv"],
        kind="narrative",
        payload={"markdown": "**数据局限性 / 注意事项**\n\n- 全文口径为全年", "style": "caveats"},
        by_turn_id="t1",
    )
    await store.patch_section(
        section_id=ids["ch2_n"], patch={"markdown": "章2 上半年 5.5 亿"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True
    )
    bus = _Bus()
    # Actual R12 payload lacked title (different-from-document title signals document scope; next test).
    await compose_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"exec_summary": "改章2", "kpis": [{"label": "上半年", "value": "5.5 亿"}], "caveats": ["第二章口径为 1-6 月"]},
        router=None,
    )
    secs = {s.id: s for s in await store.list_sections(ids["cv"])}
    assert secs[ids["k_doc"]].payload["cells"] == [{"lbl": "全年", "val": "116.23 亿元"}] and secs[ids["k_doc"]].rev == 1  # Cover unchanged.
    assert secs[ids["h1"]].rev == 1
    assert secs[cav.id].rev == 1 and "全文口径为全年" in secs[cav.id].payload["markdown"]  # Document caveats not replaced by chapter-2 caveats.
    assert secs[ids["ch2_k"]].payload["cells"] == [{"lbl": "上半年", "val": "5.5 亿"}] and not secs[ids["ch2_k"]].superseded
    assert secs[ids["ch1_k"]].rev == 1 and not secs[ids["ch1_k"]].superseded and secs[ids["ch2_h"]].rev == 1
    assert not any(e.event_type == "canvas.sections_superseded" for e in bus.events)
    # B1-7: chapter1 "116.23 亿元" correctly uses full-year scope ⇒ never mark stale.
    prov = [s for s in secs.values() if s.kind == "narrative" and (s.payload.get("_meta") or {}).get("provenance")]
    assert all("116.23" not in s.payload["markdown"] for s in prov), [s.payload["markdown"] for s in prov]


async def test_whole_document_revise_patches_cover_and_folds_chapter_kpis(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部章都改口径 ⇒ 新 KPI 是文档级：封面带换新，各章旧 KPI 带折叠（否则封面新、章里旧互相矛盾）。"""
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_doc(store)
    for n in (1, 2):
        await store.patch_section(
            section_id=ids[f"ch{n}_n"],
            patch={"markdown": f"章{n} 上半年 5.5 亿"},
            expected_rev=1,
            by_turn_id="t2",
            caller="sub_now",
            skip_owner_check=True,
        )
    bus = _Bus()
    await compose_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"exec_summary": "全改", "kpis": [{"label": "上半年", "value": "5.5 亿"}]},
        router=None,
    )
    secs = {s.id: s for s in await store.list_sections(ids["cv"])}
    assert secs[ids["k_doc"]].payload["cells"] == [{"lbl": "上半年", "val": "5.5 亿"}]
    for n in (1, 2):
        assert secs[ids[f"ch{n}_k"]].superseded and (secs[ids[f"ch{n}_k"]].payload.get("_meta") or {}).get("folded_by") == "revise"
    assert secs[ids["ch1_h"]].rev == 1 and secs[ids["ch2_h"]].rev == 1


async def test_unfinished_whole_document_revise_is_not_mistaken_for_chapter_scope(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """独立 review P1：写手想改整份文档（payload 带了与文档不同的新标题），只改了章1/章2、章3 还是全年。
    不能当成「用户只要改两章」：封面与标题要换新、被改章的旧 KPI 带折叠，章3 的残留要被完整性提示点名。"""
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_doc(store, chapters=(1, 2, 3))
    for n in (1, 2):
        await store.patch_section(
            section_id=ids[f"ch{n}_n"],
            patch={"markdown": f"章{n} 上半年 5.5 亿"},
            expected_rev=1,
            by_turn_id="t2",
            caller="sub_now",
            skip_owner_check=True,
        )
    bus = _Bus()
    await compose_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"exec_summary": "全文改上半年", "title": "上半年经营分析", "kpis": [{"label": "上半年", "value": "5.5 亿"}]},
        router=None,
    )
    secs = {s.id: s for s in await store.list_sections(ids["cv"])}
    assert secs[ids["h1"]].payload["text"] == "上半年经营分析"
    assert secs[ids["k_doc"]].payload["cells"] == [{"lbl": "上半年", "val": "5.5 亿"}]
    assert secs[ids["ch1_k"]].superseded and secs[ids["ch2_k"]].superseded and not secs[ids["ch3_k"]].superseded
    prov = [s for s in secs.values() if s.kind == "narrative" and (s.payload.get("_meta") or {}).get("provenance")]
    assert prov and "116.23" in prov[0].payload["markdown"], [s.payload["markdown"] for s in prov]  # Still flag chapter3 residue.


async def test_chapter_without_revisable_text_does_not_force_chapter_scope(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """独立 review P1（P3 探针）：某章只有 h1 + kpi_row、没有正文——它永远进不了 revised，不能算进「全部章」的分母，
    否则把两个有正文的章都改了也会被判成「部分章」，封面永远不更新。"""
    from bowhead_core.canvas_revise import chapter_scoped_revision, document_block_for

    ids = await _seed_plan_doc(store)
    meta_ch = {"composed_by": "eager", "chapter": True}
    await store.append_section(canvas_id=ids["cv"], kind="h1", payload={"text": "章3", "_meta": meta_ch}, by_turn_id="t1", owner="sub_ps_3")
    await store.append_section(
        canvas_id=ids["cv"], kind="kpi_row", payload={"cells": [{"lbl": "章3", "val": "1"}], "_meta": meta_ch}, by_turn_id="t1", owner="sub_ps_3"
    )
    block = document_block_for(await store.list_sections(ids["cv"]), ids["ch1_n"])
    assert chapter_scoped_revision(block, [ids["ch1_n"], ids["ch2_n"]], this_turn_id="t2") == []
    assert chapter_scoped_revision(block, [ids["ch2_n"]], this_turn_id="t2") == [("sub_ps_2", "t1")]
    # Document-level signal: title differs from document h1 ⇒ not chapter-scoped.
    assert chapter_scoped_revision(block, [ids["ch2_n"]], payload={"title": "上半年经营分析"}, this_turn_id="t2") == []
    assert chapter_scoped_revision(block, [ids["ch2_n"]], payload={"title": "经营分析"}, this_turn_id="t2") == [("sub_ps_2", "t1")]


async def test_stale_scan_scope_comes_from_caller_and_rc8_checks_chapter_band(store) -> None:
    """独立 review P2：扫描范围由调用方给（与 chrome patch 同一个结论）；整份文档修订（scope 为空）时封面带照扫。
    RC-8 的 KPI 面要带上被修订章自己的 kpi_row（章范围修订写的就是它）。"""
    from bowhead_core.canvas_revise import document_block_for, rc8_revised_inputs, stale_values_after_revision

    ids = await _seed_plan_doc(store)
    await store.patch_section(
        section_id=ids["ch2_n"], patch={"markdown": "章2 上半年 5.5 亿"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True
    )
    secs = await store.list_sections(ids["cv"])
    block = document_block_for(secs, ids["ch2_n"])
    revised = [s for s in secs if s.id == ids["ch2_n"]]
    prev = {ids["ch2_n"]: {"markdown": "章2 全年 116.23 亿元，第2项 9.9 亿"}}
    whole = {s.id for s, _ in stale_values_after_revision(block, revised, prev)}
    assert ids["k_doc"] in whole and ids["ch1_n"] in whole  # Whole document: scan cover and other chapters.
    scoped = {s.id for s, _ in stale_values_after_revision(block, revised, prev, chapter_scope=[("sub_ps_2", "t1")])}
    assert ids["k_doc"] not in scoped and ids["ch1_n"] not in scoped
    _, extra = await rc8_revised_inputs(store, session_id="s1", turn_id="t2", revised_ids=[ids["ch2_n"]])
    assert [s.id for s in extra] == [ids["k_doc"], ids["ch2_k"]]


async def test_provenance_upsert_dedupes_identical_new_lines(store) -> None:
    from bowhead_core.canvas_revise import upsert_provenance_section

    ids = await _seed_plan_doc(store)
    res = await upsert_provenance_section(
        store, _Bus(), canvas_id=ids["cv"], turn_id="t2", anchor_id=ids["ch1_n"], heading="**数据可信度提示**", lines=["a 9.9亿", "a 9.9亿", "b"]
    )
    assert res == "added"
    prov = [s for s in await store.list_sections(ids["cv"]) if (s.payload.get("_meta") or {}).get("provenance")]
    assert prov[0].payload["markdown"].count("- a 9.9亿") == 1


def test_number_tokens_decimal_before_date_unit_not_truncated() -> None:
    """独立 review P2：「45.2 日」不能回溯吐出截断的 `45`（会撞上无关的「45家」，还会被印进溯源行）。"""
    from bowhead_core.canvas_revise import number_tokens

    assert number_tokens("库存周转 45.2 日，回收期 12.5 年，1,405.5 月") == set()
    assert "45" in number_tokens("45家门店") and "12%" in number_tokens("grew 12%.")


def test_number_tokens_ignore_date_ranges() -> None:
    from bowhead_core.canvas_revise import number_tokens

    toks = number_tokens("2026年1-12月营业额116.23亿元，9月环比-46.75%，1-6月累计跌36.9%，12家门店")
    assert "-12" not in toks and "12" in toks  # 12 in "12家门店" is value; in "1-12月" is not.
    assert {"116.23亿元", "-46.75%", "36.9%"} <= toks
    assert "6" not in toks and "-6" not in toks


def test_stale_values_does_not_substring_match_other_numbers() -> None:
    """「40 家门店」的 40 不能子串命中别处的 1,405 万元 / KPI 4,012——这行文案是用户可见的，误报=给正确的段扣帽子。"""
    from bowhead_core.canvas_revise import stale_values_after_revision

    h = _s("h", "h1", 1)
    ins = _s("ins", "narrative", 2, turn="t1", p={"markdown": "整体 QoQ -26.69%，12 家门店正增长。"})
    intro = _s("intro", "narrative", 3, p={"markdown": "本季营收 1,405 万元，同比回落。"})
    kpi = _s("kpi", "kpi_row", 4, p={"cells": [{"lbl": "门店数", "val": "4,012"}]})
    still = _s("still", "narrative", 5, p={"markdown": "旧口径下整体 QoQ -42.99%。"})
    block = document_block_for([h, ins, intro, kpi, still], "ins")
    prev = {"ins": {"markdown": "整体 QoQ -42.99%，40 家门店无一正增长。"}}
    hits = stale_values_after_revision(block, [ins], prev)
    assert [(s.id, v) for s, v in hits] == [("still", ["-42.99%"])]  # intro / KPI not flagged for "40".


def test_number_tokens_and_stale_values() -> None:
    from bowhead_core.canvas_revise import DocumentBlock, number_tokens, stale_values_after_revision

    assert number_tokens("整体 QoQ -42.99%，22.44 亿元，2026年，1 家，1,234 万元") == {"-42.99%", "22.44亿元", "1234万元"}
    h1 = _s("h", "h1", 1)
    intro = _s("intro", "narrative", 2, p={"markdown": "整体 QoQ -42.99%。"})
    ins = _s("ins", "narrative", 3, p={"markdown": "整体 QoQ -26.69%。"})
    kpi = _s("k", "kpi_row", 4, p={"cells": [{"lbl": "x", "val": "22.44 亿元", "sub": "环比 -42.99%"}]})
    block = DocumentBlock(h1=h1, eyebrow=None, meta=None, kpi_row=kpi, body=[intro, ins])
    stale = stale_values_after_revision(block, [ins], {"ins": {"markdown": "整体 QoQ -42.99%。"}})
    assert [(s.id, v) for s, v in stale] == [("intro", ["-42.99%"]), ("k", ["-42.99%"])]
    assert stale_values_after_revision(block, [ins], {"ins": {"markdown": "整体 QoQ -26.69%。"}}) == []  # No lost values.


# ─── B1-2 fold prior charts ────────────────────────────────────


def test_dropped_figure_ids_guard_and_still_refs() -> None:
    from bowhead_core.canvas_revise import companion_code_ids, dropped_figure_ids

    old_a = _s("a", "chart", 5, p={"spec": {"data_artifact_id": "art_a"}})
    old_b = _s("b", "chart", 6, p={"spec": {"data_artifact_id": "art_b"}})
    new_c = _s("c", "chart", 20, turn="t2", p={"spec": {"data_artifact_id": "art_c"}})
    n1 = _s("n1", "narrative", 4, p={"markdown": "见 [figure:c]"})
    other = _s("n9", "narrative", 7, p={"markdown": "另一段仍引 [figure:b]"})
    card = _s("card", "insight_card", 8, p={"evidence": [{"chart_section_id": "b"}]})
    code_a = _s("code_a", "code_block", 30, p={"output_artifact_ids": ["art_a"]})
    code_b = _s("code_b", "code_block", 31, p={"output_artifact_ids": ["art_b"]})
    prev = {"n1": {"markdown": "见 [figure:a] 与 [figure:b]"}}
    secs = [n1, old_a, old_b, other, card, new_c, code_a, code_b]
    assert dropped_figure_ids(secs, [n1], prev, this_turn_id="t2") == ["a"]  # b still referenced by other section/card.
    assert companion_code_ids(secs, ["a"]) == ["code_a"]  # Visible b still uses code_b artifact.
    # Guard: no new chart this turn (anchor lost while rewording) ⇒ no folding.
    n1_lost = _s("n1", "narrative", 4, p={"markdown": "改写后没锚点"})
    assert dropped_figure_ids([n1_lost, old_a, old_b], [n1_lost], prev, this_turn_id="t2") == []
    # New body cites new chart but also old one ⇒ no folding.
    n1_keep = _s("n1", "narrative", 4, p={"markdown": "见 [figure:a] [figure:c]"})
    assert dropped_figure_ids([n1_keep, old_a, new_c], [n1_keep], prev, this_turn_id="t2") == []


async def test_revise_finisher_folds_dropped_figures_and_companions(store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    monkeypatch.delenv("BOWHEAD_CANVAS_REVISE_FOLD_FIGURES", raising=False)
    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "旧报告"}, by_turn_id="t1")
    old_chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"data_artifact_id": "art_old"}}, by_turn_id="t1")
    old_code = await store.append_section(
        canvas_id=cv.id, kind="code_block", payload={"code": "select", "output_artifact_ids": ["art_old"]}, by_turn_id="t1"
    )
    n = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"全年 [figure:{old_chart.id}]", "style": "executive"},
        by_turn_id="t1",
        owner="sub_prev",
    )
    new_chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"data_artifact_id": "art_new"}}, by_turn_id="t2")
    await store.patch_section(
        section_id=n.id,
        patch={"markdown": f"上半年 [figure:{new_chart.id}]"},
        expected_rev=1,
        by_turn_id="t2",
        caller="sub_now",
        skip_owner_check=True,
    )
    bus = _Bus()
    await compose_report(canvas_store=store, request=_request("t2"), bus=bus, payload={"exec_summary": "换图"}, router=None)
    secs = {s.id: s for s in await store.list_sections(cv.id)}
    assert secs[old_chart.id].superseded and secs[old_code.id].superseded and not secs[new_chart.id].superseded
    assert (secs[old_chart.id].payload.get("_meta") or {}).get("folded_by") == "revise"
    ev = next(e for e in bus.events if e.event_type == "canvas.sections_superseded")
    assert set(ev.payload["superseded_section_ids"]) == {old_chart.id, old_code.id}
    # Emit folding **reason**: section _meta alone leaves live frontend unable to distinguish replaced charts from older reports.
    assert ev.payload["folded_by"] == "revise"
    patched_ids = {e.payload["section_id"] for e in bus.events if e.event_type == "canvas.section_patched"}
    assert {old_chart.id, old_code.id} <= patched_ids  # _meta patch per folded section.
    # kill switch
    monkeypatch.setenv("BOWHEAD_CANVAS_REVISE_FOLD_FIGURES", "0")
    from bowhead_core.canvas_revise import fold_figures_enabled

    assert not fold_figures_enabled()


async def test_read_report_hides_folded_figures(store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents.report_tools import _read_report, _write_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    cv = await store.get_canvas_by_session("s1")
    ch = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    await store.mark_sections_superseded(section_ids=[ch.id], superseded_by_turn="t2")
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=_Bus())
    assert [f["id"] for f in (await _read_report({}, ctx))["figures"]] == []
    r = await _write_report({"kind": "narrative", "content": f"引一张折掉的图 [figure:{ch.id}]"}, ctx)
    assert r.get("ok") is True and f"[figure:{ch.id}]" not in (await store.list_sections(cv.id))[-1].payload["markdown"]  # Bake strips bad anchor.


# ─── B1-3 RC-8 covers revised sections ──────────────────────────


async def test_rc8_revised_inherited_vs_newly_referenced(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """① 沿用旧图 ⇒ 不标；② 新引一张上一轮旧图 ⇒ 标；③ cross-face 用文档块 kpi_row（annotate 档）。"""
    from bowhead_core.canvas_revise import rc8_revised_inputs
    from bowhead_core.report_final_gate import STALE_NOTE, run_final_consistency_pass

    monkeypatch.setenv("BOWHEAD_CROSSFACE_NUMBERS", "annotate")
    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "旧报告"}, by_turn_id="t1")
    kpi = await store.append_section(canvas_id=cv.id, kind="kpi_row", payload={"cells": [{"lbl": "金总增速", "val": "4.0%"}]}, by_turn_id="t1")
    fig_a = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"title": "A"}}, by_turn_id="t1")
    fig_b = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"title": "B"}}, by_turn_id="t1")
    n = await store.append_section(
        canvas_id=cv.id, kind="narrative", payload={"markdown": f"旧文 [figure:{fig_a.id}]"}, by_turn_id="t1", owner="sub_prev"
    )
    # Revision: reuse A, newly cite B, body numbers disagree with KPI row.
    await store.patch_section(
        section_id=n.id,
        patch={"markdown": f"金总增速达17.2%。\n\n[figure:{fig_a.id}]\n\n另见\n\n[figure:{fig_b.id}]"},
        expected_rev=1,
        by_turn_id="t2",
        caller="sub_now",
        skip_owner_check=True,
    )
    revised, extra = await rc8_revised_inputs(store, session_id="s1", turn_id="t2", revised_ids=[n.id])
    assert [s.id for s, _ in revised] == [n.id] and revised[0][1] == {fig_a.id} and [s.id for s in extra] == [kpi.id]
    bus = _Bus()
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=[], bus=bus, revised=revised, extra_kpi_sections=extra)
    md = {s.id: s for s in await store.list_sections(cv.id)}[n.id].payload["markdown"]
    a_pos, b_pos = md.index(f"[figure:{fig_a.id}]"), md.index(f"[figure:{fig_b.id}]")
    assert md.count(STALE_NOTE) == 1 and md.index(STALE_NOTE) > b_pos > a_pos  # Mark newly cited B only.
    assert "一致性提示" in md and "金总增速" in md  # KPIs from document-block kpi_row.
    # Nonrevision turn: both args default; empty appended returns immediately (legacy behavior).
    bus2 = _Bus()
    await run_final_consistency_pass(store=store, turn_id="t3", turn_sections=[], bus=bus2)
    assert not bus2.events


async def test_rc8_inputs_skip_missing_and_superseded(store) -> None:
    from bowhead_core.canvas_revise import rc8_revised_inputs

    cv = await store.get_canvas_by_session("s1")
    n = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "x"}, by_turn_id="t1")
    await store.mark_sections_superseded(section_ids=[n.id], superseded_by_turn="t2")
    assert await rc8_revised_inputs(store, session_id="s1", turn_id="t2", revised_ids=[n.id, "ghost"]) == ([], [])
    assert await rc8_revised_inputs(store, session_id="s1", turn_id="t2", revised_ids=[]) == ([], [])


async def test_lead_side_card_path_stamps_meta(store) -> None:
    """B1-0：peer_id=None 的卡片路也盖 _meta（composed_by=lead）——is_document_h1 靠 _meta.chapter 判边界。"""
    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.types import TurnRequest

    req = TurnRequest(turn_id="t1", session_id="s1", workspace_id="w", user_id="u", member_uid="m", text="q")
    payload = {"title": "报告", "exec_summary": "整体平稳。", "kpis": [{"label": "营收", "value": "1 亿"}], "insights": []}
    await compose_canvas_from_payload(canvas_store=store, request=req, bus=_Bus(), payload=payload, peer_id=None, emit_lifecycle=False)
    cv = await store.get_canvas_by_session("s1")
    h1 = next(s for s in await store.list_sections(cv.id) if s.kind == "h1")
    assert h1.payload["_meta"] == {"composed_by": "lead", "chapter": False}


async def test_write_report_update_strips_numbers_the_model_newly_wrote(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """接线：handler 把旧正文交给 bake——旧号留，模型新写的号剥（116 修订轮 [127] 落库、点不开）。"""
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _write_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    cv = await store.get_canvas_by_session("s1")
    n = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "旧文 [1]。"}, by_turn_id="t1", owner="sub_prev")
    await store.append_section(
        canvas_id=cv.id, kind="sources", payload={"entries": [{"label": "BI", "tool": "bi_query", "artifact_id": "a1"}]}, by_turn_id="t1"
    )
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=_Bus())
    tok = set_current_peer_name("compute")
    try:
        r = await _write_report(
            {"kind": "narrative", "section_id": n.id, "expected_rev": n.rev, "content": "旧文 [1]，新口径 12.46亿元 [127]。"}, ctx
        )
    finally:
        reset_current_peer_name(tok)
    assert r.get("ok") is True, r
    md = {s.id: s for s in await store.list_sections(cv.id)}[n.id].payload["markdown"]
    assert "[1]" in md and "[127]" not in md
