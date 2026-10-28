"""Canvas store CRUD + revision 审计."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.canvas_store import CanvasSplitDegenerateError

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path

    from bowhead_core.canvas_store import Revision


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "t.db")
    await s.initialize()
    return s


async def test_ensure_canvas_idempotent(store: CanvasStore) -> None:
    c1 = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    c2 = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    assert c1.id == c2.id


async def test_ensure_canvas_concurrent_first_create_single_winner(store: CanvasStore) -> None:
    """T7 review P2: get→INSERT 有 TOCTOU 窗口——并发首建(如 multi-writer 无槽降级下
    N 个写手同时 ensure)全部 get 到 None → 全部 INSERT → UNIQUE(session_id) 只放行一个。
    输家必须重读赢家的行,而不是把 IntegrityError 抛给调用方(抛=该写手静默丢章)。"""
    import asyncio

    results = await asyncio.gather(*[store.ensure_canvas(session_id="race", workspace_id="w", user_id="u") for _ in range(5)])
    ids = {c.id for c in results}
    assert len(ids) == 1, f"并发首建必须收敛到同一个 canvas; got {ids}"
    assert (await store.get_canvas_by_session("race")).id == results[0].id


async def test_append_and_patch_section(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    s1 = await store.append_section(canvas_id=c.id, kind="h1", payload={"text": "Hello"})
    assert s1.seq == 1
    assert s1.rev == 1
    assert s1.to_report_section()["rev"] == 1

    s2 = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "xxx"})
    assert s2.seq == 2

    patched = await store.patch_section(section_id=s1.id, patch={"text": "Hello 2"})
    assert patched is not None
    assert patched.rev == 2
    assert patched.payload == {"text": "Hello 2"}


async def test_patch_section_strips_protected_fields(store: CanvasStore) -> None:
    # WP3 review #7: PATCH structural/identity fields must be stripped; only content fields apply.
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    s = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "orig"})
    patched = await store.patch_section(
        section_id=s.id,
        patch={"markdown": "new", "kind": "chart", "id": "evil", "rev": 99, "seq": 7},
    )
    assert patched is not None
    assert patched.payload.get("markdown") == "new"  # Content fields apply normally.
    for protected in ("kind", "id", "rev", "seq"):
        assert protected not in patched.payload  # Structural fields never contaminate payload.
    assert patched.rev == 2  # rev increments normally, unaffected by patched 99.


async def test_reorder_and_delete(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    a = await store.append_section(canvas_id=c.id, kind="eyebrow", payload={"text": "A"})
    b = await store.append_section(canvas_id=c.id, kind="eyebrow", payload={"text": "B"})
    await store.reorder_sections(canvas_id=c.id, ordered_ids=[b.id, a.id])
    sections = await store.list_sections(c.id)
    assert [s.id for s in sections] == [b.id, a.id]

    assert await store.delete_section(section_id=b.id)
    sections2 = await store.list_sections(c.id)
    assert [s.id for s in sections2] == [a.id]


async def test_revision_audit(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    s = await store.append_section(canvas_id=c.id, kind="h1", payload={"text": "T"})
    await store.patch_section(section_id=s.id, patch={"text": "T2"})
    await store.delete_section(section_id=s.id)
    revs = await store.list_revisions(canvas_id=c.id)
    ops = sorted(r.op for r in revs)
    assert ops == ["append", "delete", "patch"]


async def test_split_narrative_at_offset(store: CanvasStore) -> None:
    """B-7: split narrative section 在 char offset 处分两段."""
    c = await store.ensure_canvas(session_id="s_split", workspace_id="w", user_id="u")
    a = await store.append_section(
        canvas_id=c.id,
        kind="eyebrow",
        payload={"text": "A"},
    )
    narr = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "前半部分。后半部分。", "source_refs": ["s_001"]},
    )
    b = await store.append_section(
        canvas_id=c.id,
        kind="eyebrow",
        payload={"text": "B"},
    )
    # Split at 5 characters (前半部分。 =5 including period).
    pair = await store.split_section(section_id=narr.id, offset=5)
    assert pair is not None
    updated, new_sec = pair
    assert updated.payload["markdown"] == "前半部分。"
    assert new_sec.payload["markdown"] == "后半部分。"
    # W11-9: no [num/ref:..] tokens in Markdown ⇒ source_refs removed cleanly.
    assert "source_refs" not in updated.payload
    assert "source_refs" not in new_sec.payload
    # Order: a / updated / new / b.
    secs = await store.list_sections(c.id)
    assert [s.id for s in secs] == [a.id, updated.id, new_sec.id, b.id]


async def test_split_numbered_cite_inherits_refs(store: CanvasStore) -> None:
    """split 的 source_refs 归属：gen3 `[N]` 提不出 source id（下标不含身份）→
    有编号引用的半段**整份继承**，无引用的半段干净移除。

    citation-audit M3-5 记档的**能力差**：gen2 时代 `[num:…:src:S]`/`[ref:S]` 把
    id 写在正文里，split 能按「这半段真用了谁」精确切分（原
    `test_split_source_refs_precise` / `test_split_smushed_marker_source_refs_kept`
    两条锁的就是它，随 gen2 退役删除）。gen3 下正文只有下标，做不到精确切分 →
    保守整份继承（宁可多声明，不丢链；飞书07.06 #11 的裁决）。代价=split 后两段
    可能都挂着对方的来源条目，属可接受的过度声明。
    """
    c = await store.ensure_canvas(session_id="s_split_gen3", workspace_id="w", user_id="u")
    md = "同比增长 4.18% [1],为全年最高。后半段纯结论无引用。"
    narr = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": md, "source_refs": ["art_yoy02", "art_branch01"]},
    )
    boundary = md.find("。") + 1
    pair = await store.split_section(section_id=narr.id, offset=boundary)
    assert pair is not None
    updated, new_sec = pair
    # First half has [1] ⇒ inherit all (cannot map N→id from text; conservatively retain).
    assert updated.payload.get("source_refs") == ["art_yoy02", "art_branch01"]
    # Second half no citations ⇒ remove cleanly.
    assert "source_refs" not in new_sec.payload


async def test_split_no_refs_at_all(store: CanvasStore) -> None:
    """W11-9: source_refs 数组里全无在 markdown 出现 → 移除 key."""
    c = await store.ensure_canvas(session_id="s_split_noref", workspace_id="w", user_id="u")
    narr = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "纯文本无引用。", "source_refs": ["s_xxx"]},
    )
    pair = await store.split_section(section_id=narr.id, offset=4)
    assert pair is not None
    updated, new_sec = pair
    assert "source_refs" not in updated.payload
    assert "source_refs" not in new_sec.payload


async def test_split_atomic_kind_returns_none(store: CanvasStore) -> None:
    """B-7: atomic kind (h1/eyebrow/etc) 不支持 split, 返 None."""
    c = await store.ensure_canvas(session_id="s_split2", workspace_id="w", user_id="u")
    h = await store.append_section(canvas_id=c.id, kind="h1", payload={"text": "T"})
    res = await store.split_section(section_id=h.id, offset=1)
    assert res is None


async def test_split_unknown_section_returns_none(store: CanvasStore) -> None:
    res = await store.split_section(section_id="sec_not_exist", offset=0)
    assert res is None


async def test_split_degenerate_offset_rejected(store: CanvasStore) -> None:
    """affordance r3 §5.2: 边界 offset (<=0 / >=len) 会产出一半空壳——原钳制行为
    曾配合前端夺焦 bug 以 offset=0 连环制造空 section, 改为显式拒绝且不留痕
    (无 revision, section 原样)."""
    c = await store.ensure_canvas(session_id="s_split3", workspace_id="w", user_id="u")
    narr = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "abc"},
    )
    for bad in (0, -1, 3, 999):
        with pytest.raises(CanvasSplitDegenerateError):
            await store.split_section(section_id=narr.id, offset=bad)
    # Section untouched, no extra revision.
    secs = await store.list_sections(canvas_id=c.id)
    assert [s.payload["markdown"] for s in secs if s.kind == "narrative"] == ["abc"]
    revs = await store.list_revisions(canvas_id=c.id)
    assert all(r.op != "patch" for r in revs)


# ─── W12-6 merge_with_prev (atomic) ───────────────────────────


async def test_merge_with_prev_basic(store: CanvasStore) -> None:
    """W12-6: 两个相邻 narrative section 合并 → markdown 拼接 + dedupe source_refs."""
    c = await store.ensure_canvas(session_id="s_m1", workspace_id="w", user_id="u")
    a = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "段一。", "source_refs": ["s_001", "s_002"]},
    )
    b = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "段二。", "source_refs": ["s_002", "s_003"]},
    )
    survivor = await store.merge_with_prev(section_id=b.id)
    assert survivor is not None
    assert survivor.id == a.id  # prev survives.
    assert survivor.payload["markdown"] == "段一。\n\n段二。"
    # source_refs dedupe: s_001 + s_002 + s_003
    assert survivor.payload["source_refs"] == ["s_001", "s_002", "s_003"]
    # b deleted.
    secs = await store.list_sections(c.id)
    assert [s.id for s in secs] == [a.id]


async def test_merge_with_prev_revision_atomic(store: CanvasStore) -> None:
    """W12-6: 单次 merge 写 patch + delete 两条 revision."""
    c = await store.ensure_canvas(session_id="s_m2", workspace_id="w", user_id="u")
    await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "X"},
    )
    b = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "Y"},
    )
    await store.merge_with_prev(section_id=b.id)
    revs = await store.list_revisions(canvas_id=c.id)
    ops = sorted(r.op for r in revs)
    # append a / append b / patch a / delete b
    assert ops == ["append", "append", "delete", "patch"]


async def test_merge_with_prev_kind_mismatch_returns_none(store: CanvasStore) -> None:
    """W12-6: prev 不同 kind (eyebrow vs narrative) → 拒合并."""
    c = await store.ensure_canvas(session_id="s_m3", workspace_id="w", user_id="u")
    await store.append_section(
        canvas_id=c.id,
        kind="eyebrow",
        payload={"text": "EYEBROW"},
    )
    narr = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "narr"},
    )
    res = await store.merge_with_prev(section_id=narr.id)
    assert res is None
    # narr must remain.
    secs = await store.list_sections(c.id)
    assert any(s.id == narr.id for s in secs)


async def test_merge_with_prev_no_prev_returns_none(store: CanvasStore) -> None:
    """W12-6: 首条 (无 prev) → None."""
    c = await store.ensure_canvas(session_id="s_m4", workspace_id="w", user_id="u")
    a = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "first"},
    )
    res = await store.merge_with_prev(section_id=a.id)
    assert res is None


async def test_merge_with_prev_atomic_kind_returns_none(store: CanvasStore) -> None:
    """W12-6: target 不是 text-based (h1/eyebrow/...) → None."""
    c = await store.ensure_canvas(session_id="s_m5", workspace_id="w", user_id="u")
    await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "narr"},
    )
    h = await store.append_section(canvas_id=c.id, kind="h1", payload={"text": "T"})
    res = await store.merge_with_prev(section_id=h.id)
    assert res is None


# ν1 Phase 1a — list_section_ids_by_kind wrapper


async def test_list_section_ids_by_kind_empty(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s_n1", workspace_id="w", user_id="u")
    ids = await store.list_section_ids_by_kind(c.id, kind="chart")
    assert ids == set()


async def test_list_section_ids_by_kind_filter(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s_n2", workspace_id="w", user_id="u")
    ch1 = await store.append_section(canvas_id=c.id, kind="chart", payload={"x": 1})
    cb1 = await store.append_section(canvas_id=c.id, kind="code_block", payload={"y": 2})
    ch2 = await store.append_section(canvas_id=c.id, kind="chart", payload={"x": 3})
    eyebrow = await store.append_section(canvas_id=c.id, kind="eyebrow", payload={"text": "e"})

    chart_ids = await store.list_section_ids_by_kind(c.id, kind="chart")
    assert chart_ids == {ch1.id, ch2.id}
    assert cb1.id not in chart_ids
    assert eyebrow.id not in chart_ids

    code_ids = await store.list_section_ids_by_kind(c.id, kind="code_block")
    assert code_ids == {cb1.id}

    none_ids = await store.list_section_ids_by_kind(c.id, kind="nonexistent_kind")
    assert none_ids == set()


async def test_list_section_ids_by_kind_isolates_canvas(store: CanvasStore) -> None:
    c1 = await store.ensure_canvas(session_id="s_n3a", workspace_id="w", user_id="u")
    c2 = await store.ensure_canvas(session_id="s_n3b", workspace_id="w", user_id="u")
    s1 = await store.append_section(canvas_id=c1.id, kind="chart", payload={"x": 1})
    s2 = await store.append_section(canvas_id=c2.id, kind="chart", payload={"x": 2})
    ids1 = await store.list_section_ids_by_kind(c1.id, kind="chart")
    ids2 = await store.list_section_ids_by_kind(c2.id, kind="chart")
    assert ids1 == {s1.id}
    assert ids2 == {s2.id}


# ─── Phase 1.6 G4 — list_sections_appended_by_turn ────────────────────


async def test_list_sections_appended_by_turn_filters_by_turn(
    store: CanvasStore,
) -> None:
    c = await store.ensure_canvas(session_id="s_g4a", workspace_id="w", user_id="u")
    # Turn A appends 2 sections
    sa1 = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {"x": 1}},
        by_turn_id="tA",
    )
    sa2 = await store.append_section(
        canvas_id=c.id,
        kind="data_table",
        payload={"columns": []},
        by_turn_id="tA",
    )
    # Turn B appends 1 section
    sb1 = await store.append_section(
        canvas_id=c.id,
        kind="kpi_row",
        payload={"cells": []},
        by_turn_id="tB",
    )

    secs_a = await store.list_sections_appended_by_turn("tA")
    secs_b = await store.list_sections_appended_by_turn("tB")
    assert {s.id for s in secs_a} == {sa1.id, sa2.id}
    assert {s.id for s in secs_b} == {sb1.id}


async def test_list_sections_appended_by_turn_filters_by_kind(
    store: CanvasStore,
) -> None:
    c = await store.ensure_canvas(session_id="s_g4b", workspace_id="w", user_id="u")
    chart = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t1",
    )
    kpi = await store.append_section(
        canvas_id=c.id,
        kind="kpi_row",
        payload={"cells": []},
        by_turn_id="t1",
    )
    await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "x"},
        by_turn_id="t1",
    )
    # Only chart + kpi_row, narrative excluded
    secs = await store.list_sections_appended_by_turn(
        "t1",
        kinds=["chart", "kpi_row"],
    )
    assert {s.id for s in secs} == {chart.id, kpi.id}


async def test_list_sections_appended_by_turn_preserves_seq_order(
    store: CanvasStore,
) -> None:
    c = await store.ensure_canvas(session_id="s_g4c", workspace_id="w", user_id="u")
    s1 = await store.append_section(canvas_id=c.id, kind="chart", payload={}, by_turn_id="t")
    s2 = await store.append_section(canvas_id=c.id, kind="kpi_row", payload={}, by_turn_id="t")
    s3 = await store.append_section(canvas_id=c.id, kind="data_table", payload={}, by_turn_id="t")
    secs = await store.list_sections_appended_by_turn("t")
    assert [s.id for s in secs] == [s1.id, s2.id, s3.id]


async def test_list_sections_appended_by_turn_empty_id_returns_empty(
    store: CanvasStore,
) -> None:
    secs = await store.list_sections_appended_by_turn("")
    assert secs == []


async def test_list_sections_appended_by_turn_excludes_patches(
    store: CanvasStore,
) -> None:
    """Only `op='append'` revisions count — patches by another turn don't
    re-attribute the section.
    """
    c = await store.ensure_canvas(session_id="s_g4d", workspace_id="w", user_id="u")
    s = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {}},
        by_turn_id="t_creator",
    )
    # Different turn patches the section
    await store.patch_section(
        section_id=s.id,
        patch={"spec": {"updated": True}},
        by_turn_id="t_patcher",
    )
    # Patcher turn doesn't claim the section as an appended artifact
    secs_creator = await store.list_sections_appended_by_turn("t_creator")
    secs_patcher = await store.list_sections_appended_by_turn("t_patcher")
    assert [x.id for x in secs_creator] == [s.id]
    assert secs_patcher == []


# ─── chat-page-polish B1: list_for_user first_section_title fallback ───


async def test_list_for_user_returns_first_section_title_h1(store: CanvasStore) -> None:
    """B1: list_for_user 必须返 first_section_title — 取最早 section 的 h1."""
    c = await store.ensure_canvas(session_id="s_b1a", workspace_id="w", user_id="u_b1")
    await store.append_section(
        canvas_id=c.id,
        kind="section",
        payload={"h1": "2026年 Q1 GMV 同比分析", "label": ""},
    )
    items = await store.list_for_user(user_id="u_b1")
    assert len(items) == 1
    assert items[0]["first_section_title"] == "2026年 Q1 GMV 同比分析"


async def test_list_for_user_picks_earliest_section_by_seq(store: CanvasStore) -> None:
    """B1: 多 section 时取 seq=1, 不是最新或最长的."""
    c = await store.ensure_canvas(session_id="s_b1b", workspace_id="w", user_id="u_b1b")
    await store.append_section(canvas_id=c.id, kind="section", payload={"h1": "第一"})
    await store.append_section(canvas_id=c.id, kind="section", payload={"h1": "第二"})
    await store.append_section(canvas_id=c.id, kind="section", payload={"h1": "第三"})
    items = await store.list_for_user(user_id="u_b1b")
    assert items[0]["first_section_title"] == "第一"


async def test_list_for_user_falls_through_section_without_title(store: CanvasStore) -> None:
    """B1: 第一 section 没 title-ish 字段时跳到第二 section."""
    c = await store.ensure_canvas(session_id="s_b1c", workspace_id="w", user_id="u_b1c")
    # First section: only label, no h1/h2/h3/title/heading/text
    await store.append_section(canvas_id=c.id, kind="chart", payload={"label": "spec"})
    await store.append_section(
        canvas_id=c.id,
        kind="section",
        payload={"h2": "实际标题"},
    )
    items = await store.list_for_user(user_id="u_b1c")
    assert items[0]["first_section_title"] == "实际标题"


async def test_list_for_user_returns_none_when_no_sections(store: CanvasStore) -> None:
    """B1: 0 section canvas → first_section_title=None (FE 走时间 fallback)."""
    await store.ensure_canvas(session_id="s_b1d", workspace_id="w", user_id="u_b1d")
    items = await store.list_for_user(user_id="u_b1d")
    assert len(items) == 1
    assert items[0]["first_section_title"] is None
    assert items[0]["sections_count"] == 0


async def test_list_for_user_truncates_long_title(store: CanvasStore) -> None:
    """B1: title 太长 (>200 char) 截断, 防 UI 失控."""
    long = "a" * 500
    c = await store.ensure_canvas(session_id="s_b1e", workspace_id="w", user_id="u_b1e")
    await store.append_section(canvas_id=c.id, kind="section", payload={"h1": long})
    items = await store.list_for_user(user_id="u_b1e")
    assert items[0]["first_section_title"] is not None
    assert len(items[0]["first_section_title"]) <= 200


async def test_list_for_user_skips_empty_whitespace_title(store: CanvasStore) -> None:
    """B1: payload h1='   ' 视为无 title, 跳到下一 section."""
    c = await store.ensure_canvas(session_id="s_b1f", workspace_id="w", user_id="u_b1f")
    await store.append_section(canvas_id=c.id, kind="section", payload={"h1": "   "})
    await store.append_section(canvas_id=c.id, kind="section", payload={"title": "OK"})
    items = await store.list_for_user(user_id="u_b1f")
    assert items[0]["first_section_title"] == "OK"


async def test_list_for_user_min_sections_filters_before_limit(tmp_path: Path) -> None:
    """empty-canvas 清理批: 空壳不得占 limit 名额。

    首页最近产出此前是「先取 limit 条再过滤 sections_count>0」, 空壳把名额吃光就少显
    卡片(116 实测最重空间 8 取 5)。判据必须落在 SQL 的 LIMIT 之前。
    """
    import sqlite3

    db = tmp_path / "min_sections.db"
    s = CanvasStore(db)
    await s.initialize()
    real = await s.ensure_canvas(session_id="s_real", workspace_id="w", user_id="u_ms")
    await s.append_section(canvas_id=real.id, kind="h1", payload={"text": "Real report"})
    for i in range(8):
        await s.ensure_canvas(session_id=f"s_shell_{i}", workspace_id="w", user_id="u_ms")
    # _now() second precision gives uncertain same-second ordering; explicitly put empty shells before real reports.
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE canvases SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (real.id,))
        con.execute("UPDATE canvases SET updated_at='2026-02-01T00:00:00+00:00' WHERE id!=?", (real.id,))
        con.commit()
    finally:
        con.close()

    starved = await s.list_for_user(user_id="u_ms", limit=8)
    assert real.id not in {r["id"] for r in starved}, "8 个空壳应吃光 limit 名额(被修的形状)"

    fixed = await s.list_for_user(user_id="u_ms", limit=8, min_sections=1)
    assert [r["id"] for r in fixed] == [real.id]


async def test_list_for_user_visible_sections_filter_fills_limit_with_reports(tmp_path: Path) -> None:
    """报告库默认态：chat-only 空壳必须在 LIMIT 前过滤，不能挤掉真报告。

    与 ``min_sections`` 不同，这个口径只要求 detail 可见：superseded、quarantined、
    meta 仍属于详情集合，不能被报告库擅自排除。
    """
    import sqlite3

    db = tmp_path / "visible_sections_limit.db"
    s = CanvasStore(db)
    await s.initialize()
    real = await s.ensure_canvas(session_id="s_visible_real", workspace_id="w", user_id="u_visible")
    visible = await s.append_section(
        canvas_id=real.id,
        kind="meta",
        payload={"title": "Visible detail row"},
    )
    await s.set_quarantine(section_id=visible.id, quarantined=True, reason="review")
    chat_only = await s.ensure_canvas(
        session_id="s_visible_chat",
        workspace_id="w",
        user_id="u_visible",
    )
    await s.append_section(
        canvas_id=chat_only.id,
        kind="chart",
        payload={"title": "Chat only"},
        by_turn_id="t_visible_chat",
    )
    await s.mark_turn_hidden_for_chat_mode("t_visible_chat", "declared: chat")

    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE canvases SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (real.id,),
        )
        con.execute(
            "UPDATE canvases SET updated_at='2026-02-01T00:00:00+00:00' WHERE id=?",
            (chat_only.id,),
        )
        con.commit()
    finally:
        con.close()

    unfiltered = await s.list_for_user(user_id="u_visible", limit=1)
    assert [row["id"] for row in unfiltered] == [chat_only.id]

    filtered = await s.list_for_user(
        user_id="u_visible",
        limit=1,
        min_visible_sections=1,
    )
    assert [row["id"] for row in filtered] == [real.id]
    assert filtered[0]["sections_count"] == 1


async def test_list_for_user_min_sections_ignores_hidden_and_superseded(tmp_path: Path) -> None:
    """★116 dogfood 1.0.93 P3: chat-mode 轮的 section 全 hidden_for_chat_mode=1
    （用户面没有报告）——裸计数让纯 chat 会话在「最近产出」挂出 Untitled report
    空壳条目。min_sections 只认可见的报告内容(非 hidden/superseded/quarantined/meta)。"""
    s = CanvasStore(tmp_path / "ms_hidden.db")
    await s.initialize()
    chat_only = await s.ensure_canvas(session_id="s_chatonly", workspace_id="w", user_id="u_mh")
    await s.append_section(canvas_id=chat_only.id, kind="chart", payload={"spec": {}}, by_turn_id="t_c1")
    await s.append_section(
        canvas_id=chat_only.id,
        kind="narrative",
        payload={"markdown": "x", "title": "仅供聊天内联的内部标题"},
        by_turn_id="t_c1",
    )
    await s.mark_turn_hidden_for_chat_mode("t_c1", "chat_mode")
    real = await s.ensure_canvas(session_id="s_real2", workspace_id="w", user_id="u_mh")
    await s.append_section(canvas_id=real.id, kind="h1", payload={"text": "真报告"}, by_turn_id="t_r1")

    all_rows = await s.list_for_user(user_id="u_mh")
    chat_row = next(row for row in all_rows if row["id"] == chat_only.id)
    assert chat_row["sections_count"] == 0
    assert chat_row["first_section_title"] is None

    got = await s.list_for_user(user_id="u_mh", min_sections=1)
    ids = [r["id"] for r in got]
    assert real.id in ids
    assert chat_only.id not in ids, "全 hidden 的 chat 会话画布不得进报告列表"


# ─── sql_block read alias (2026-09-05 section-kind retirement) ───


@pytest.mark.unit
def test_legacy_sql_block_reads_back_as_code_block() -> None:
    """存量 `sql_block` 在**读**的时候归一成 `code_block`，不是降级成 unknown。

    它一直只是 `lang="sql"` 的 code_block（Sprint ι 注释：legacy alias，90 天 grace）。
    老数据本来就是代码，渲染成代码块是更准的结果 —— 这条和 `unknown` 兜底是两种不同的
    处置，别混。前端有对称的一份（`canvas-to-pm.normalizeLegacyKind`）。
    """
    from bowhead_core.canvas_store import _normalize_legacy_kind

    kind, payload = _normalize_legacy_kind("sql_block", {"sql": "SELECT 1 FROM t"}, "sec_old")
    assert kind == "code_block"
    assert payload["code"] == "SELECT 1 FROM t"
    assert payload["lang"] == "sql"
    # code_block needs call_id; synthesize traceable one if absent in legacy data.
    assert payload["call_id"] == "legacy_sql_sec_old"


@pytest.mark.unit
def test_legacy_sql_block_prefers_real_call_id_from_meta() -> None:
    from bowhead_core.canvas_store import _normalize_legacy_kind

    _, payload = _normalize_legacy_kind(
        "sql_block",
        {"sql": "SELECT 1", "meta": {"call_id": "tl_real_7", "source": "bi.ask_question"}},
        "sec_old2",
    )
    assert payload["call_id"] == "tl_real_7"
    assert payload["meta"]["source"] == "bi.ask_question"


@pytest.mark.unit
def test_non_legacy_kinds_pass_through_untouched() -> None:
    """负对照：别名只认 sql_block，别的 kind 一个字节都不能动。"""
    from bowhead_core.canvas_store import _normalize_legacy_kind

    original = {"call_id": "tl_1", "lang": "python", "code": "print(1)"}
    kind, payload = _normalize_legacy_kind("code_block", original, "sec_x")
    assert kind == "code_block"
    assert payload is original


# ─── Appearance refresh batch 4b: historical whole-report AI-turn ordinals ──


async def _seed_revisions(store: CanvasStore, canvas_id: str, rows: list[tuple[str | None, str]]) -> None:
    """按 (by_turn_id, created_at) 直接写修改记录, 时间戳由测试掌控。"""
    import aiosqlite

    async with aiosqlite.connect(store.db_path) as db:
        await db.executemany(
            "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
            "VALUES (?, ?, NULL, 'patch', NULL, NULL, ?, ?)",
            [(f"rev_{canvas_id}_{i:04d}", canvas_id, turn, at) for i, (turn, at) in enumerate(rows)],
        )
        await db.commit()


def _ordinals_by_turn(revs: list[Revision]) -> dict[str | None, set[int | None]]:
    out: dict[str | None, set[int | None]] = {}
    for r in revs:
        out.setdefault(r.by_turn_id, set()).add(r.turn_ordinal)
    return out


async def test_revision_turn_ordinal_numbers_turns_by_first_write(store: CanvasStore) -> None:
    """多轮按每轮最早一条升序编号; 同轮多条同号; 手动与 machine: 为 None 且不占号。"""
    c = await store.ensure_canvas(session_id="s_ord1", workspace_id="w", user_id="u")
    await _seed_revisions(
        store,
        c.id,
        [
            (None, "2026-09-01T00:00:00+00:00"),  # Earliest manual edit, no ordinal.
            ("machine:reorder", "2026-09-01T00:00:01+00:00"),  # Machine reordering, no ordinal.
            ("turn_b", "2026-09-01T00:00:02+00:00"),
            ("", "2026-09-01T00:00:03+00:00"),  # Empty string = manual.
            ("turn_a", "2026-09-01T00:00:04+00:00"),
            ("turn_b", "2026-09-01T00:00:05+00:00"),  # Later writes in same turn remain turn 1.
            ("machine:canonicalize", "2026-09-01T00:00:06+00:00"),
            ("turn_c", "2026-09-01T00:00:07+00:00"),
            ("turn_a", "2026-09-01T00:00:08+00:00"),
        ],
    )
    revs = await store.list_revisions(canvas_id=c.id)
    assert _ordinals_by_turn(revs) == {
        "turn_b": {1},
        "turn_a": {2},
        "turn_c": {3},
        None: {None},
        "": {None},
        "machine:reorder": {None},
        "machine:canonicalize": {None},
    }


async def test_revision_turn_ordinal_is_global_beyond_window(store: CanvasStore) -> None:
    """只回最近 100 条: 第 1 轮 60 条 + 第 2 轮 60 条时, 第 2 轮仍标 2; 别的报告的轮次不计入。"""
    c = await store.ensure_canvas(session_id="s_ord2", workspace_id="w", user_id="u")
    rows: list[tuple[str | None, str]] = [("turn_1", f"2026-09-01T00:00:{i:02d}+00:00") for i in range(60)]
    rows += [("turn_2", f"2026-09-01T00:01:{i:02d}+00:00") for i in range(60)]
    await _seed_revisions(store, c.id, rows)
    other = await store.ensure_canvas(session_id="s_ord2_other", workspace_id="w", user_id="u")
    await _seed_revisions(store, other.id, [("turn_0", "2026-08-01T00:00:00+00:00")])

    revs = await store.list_revisions(canvas_id=c.id)
    assert len(revs) == 100
    assert _ordinals_by_turn(revs) == {"turn_2": {2}, "turn_1": {1}}
    only_latest = await store.list_revisions(canvas_id=c.id, limit=10)
    assert _ordinals_by_turn(only_latest) == {"turn_2": {2}}


async def test_revision_turn_ordinal_tie_break_is_stable(store: CanvasStore) -> None:
    """两轮首写同刻时按 by_turn_id 定序, 与写入顺序无关。"""
    same = "2026-09-01T00:00:00+00:00"
    for session, order in (("s_ord3a", ["turn_z", "turn_a"]), ("s_ord3b", ["turn_a", "turn_z"])):
        c = await store.ensure_canvas(session_id=session, workspace_id="w", user_id="u")
        await _seed_revisions(store, c.id, [(t, same) for t in order])
        revs = await store.list_revisions(canvas_id=c.id)
        assert _ordinals_by_turn(revs) == {"turn_a": {1}, "turn_z": {2}}


def test_turn_ordinals_tie_break_ignores_input_order() -> None:
    """created_at 只到秒: 同刻的轮次按 by_turn_id 定序, 不依赖聚合结果的返回顺序。"""
    from bowhead_core.canvas_store import _turn_ordinals

    same = "2026-09-01T00:00:00+00:00"
    later = "2026-09-01T00:00:01+00:00"
    expected = {"turn_a": 1, "turn_m": 2, "turn_z": 3, "turn_b": 4}
    rows = [("turn_a", same), ("turn_m", same), ("turn_z", same), ("turn_b", later)]
    assert _turn_ordinals(rows) == expected
    assert _turn_ordinals(reversed(rows)) == expected
