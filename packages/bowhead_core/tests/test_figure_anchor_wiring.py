"""图锚归一的**接线**闸：三个存储写入口都必须真的过这道闸。

断言的是**库里的行**（不是返回值），并断言发出去的 payload 与库行一致——
纯函数全绿而整条链空转，是本仓栽过不止一次的形态。

突变判据：摘掉任一挂载点的 `_normalized_payload(...)` 调用，对应用例必须变红。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.canvas_store import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path

MID_ANCHOR = "对照全年排名[figure:{fid}]可见，**头部门店地位稳固**"


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(db_path=tmp_path / "canvas.db")
    await s.initialize()
    return s


def _row_payload(db_path: Path, section_id: str) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT payload FROM canvas_sections WHERE id=?", (section_id,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else {}


def _row_md(db_path: Path, section_id: str) -> str:
    return str(_row_payload(db_path, section_id).get("markdown") or "")


def _force_md(db_path: Path, section_id: str, markdown: str) -> None:
    """绕过写入口直接改库——模拟**存量**泄漏数据（本次修复之前落库的那批）。

    只有存量泄漏才能构造出「库里已经有错位锚」的局面：经过写入口的内容都已归一。
    用干净数据写的用例摘掉挂载点照样全绿（突变实测过），等于没钉住。
    """
    con = sqlite3.connect(db_path)
    try:
        payload = _row_payload(db_path, section_id)
        payload["markdown"] = markdown
        con.execute("UPDATE canvas_sections SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), section_id))
        con.commit()
    finally:
        con.close()


async def _seed(store: CanvasStore, session_id: str = "s1") -> tuple[str, str]:
    canvas = await store.ensure_canvas(session_id=session_id, workspace_id="w1", user_id="u1")
    chart = await store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"kind": "hbar", "title": "各门店营业额排名"}},
        by_turn_id="t1",
    )
    return canvas.id, chart.id


async def test_append_section_normalizes_and_returns_stored_bytes(store: CanvasStore, tmp_path: Path) -> None:
    """write_report 追加 / composer 投影 / 收尾器系统段都走这条。"""
    canvas_id, fid = await _seed(store)
    sec = await store.append_section(
        canvas_id=canvas_id,
        kind="narrative",
        payload={"markdown": MID_ANCHOR.format(fid=fid)},
        by_turn_id="t2",
    )
    stored = _row_md(tmp_path / "canvas.db", sec.id)
    assert stored == f"对照全年排名「各门店营业额排名」可见，**头部门店地位稳固**\n\n[figure:{fid}]"
    # live == persisted: caller SSE uses `stored.to_report_section()`, requiring normalized bytes.
    assert sec.payload["markdown"] == stored


async def test_patch_section_normalizes_f4_shape(store: CanvasStore, tmp_path: Path) -> None:
    """write_report update / 修订 chrome / RC-8 回写 / 前端编辑保存都走这条。"""
    canvas_id, fid = await _seed(store)
    sec = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "占位"}, by_turn_id="t2")
    patched = await store.patch_section(
        section_id=sec.id,
        patch={"markdown": f"前文说明见图。\n[figure:{fid}]"},
        by_turn_id="t3",
    )
    stored = _row_md(tmp_path / "canvas.db", sec.id)
    assert stored == f"前文说明见图。\n\n[figure:{fid}]"
    assert patched is not None
    assert patched.payload["markdown"] == stored


async def test_structural_ops_do_not_touch_prose(store: CanvasStore, tmp_path: Path) -> None:
    """拆段/合段**一个字节都不碰正文**。

    它们是结构操作，调用方没带新正文。早先这里也归一，于是用户在编辑器里按一下「拆段」，
    句子里的锚会被换成「图标题」、图被搬走——一次没要求过的改写。位置自愈交给真正
    带正文的写入和存量作业（review 三轮 #2）。
    """
    db = tmp_path / "canvas.db"
    canvas_id, fid = await _seed(store)
    sec = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "占位"}, by_turn_id="t2")
    head = "第一段。\n\n"
    leaked = head + f"后半段见[figure:{fid}]与[figure:sec_gone]。"
    _force_md(db, sec.id, leaked)

    pair = await store.split_section(section_id=sec.id, offset=len(head), by_turn_id="t3")
    assert pair is not None
    _, created = pair
    assert _row_md(db, created.id) == f"后半段见[figure:{fid}]与[figure:sec_gone]。"

    merged = await store.merge_with_prev(section_id=created.id, by_turn_id="t4")
    assert merged is not None
    assert _row_md(db, merged.id) == leaked


async def test_non_markdown_kind_is_untouched(store: CanvasStore, tmp_path: Path) -> None:
    """代码块里的方括号不是引用——闸按 kind 收窄，不碰结构化 payload。"""
    canvas_id, fid = await _seed(store)
    code = f"x = '[figure:{fid}]'"
    sec = await store.append_section(canvas_id=canvas_id, kind="code_block", payload={"code": code, "lang": "python"}, by_turn_id="t2")
    assert _row_payload(tmp_path / "canvas.db", sec.id)["code"] == code


async def test_anchor_from_another_canvas_is_stripped(store: CanvasStore, tmp_path: Path) -> None:
    """有效集是**本画布**作用域：指向别的画布的图，前端只会渲一个 not-found 占位。"""
    _, fid = await _seed(store, session_id="s1")
    other = await store.ensure_canvas(session_id="s2", workspace_id="w1", user_id="u1")
    sec = await store.append_section(
        canvas_id=other.id,
        kind="narrative",
        payload={"markdown": f"引用别的画布的图[figure:{fid}]试试。"},
        by_turn_id="t2",
    )
    assert _row_md(tmp_path / "canvas.db", sec.id) == "引用别的画布的图试试。"


async def test_clean_markdown_is_byte_identical(store: CanvasStore, tmp_path: Path) -> None:
    """反向闸：没有错位锚时一个字节都不许动（含行尾空白）。"""
    canvas_id, fid = await _seed(store)
    clean = f"前文。\n\n[figure:{fid}]\n**图1**：说明\n\n后文。   \n"
    sec = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": clean}, by_turn_id="t2")
    assert _row_md(tmp_path / "canvas.db", sec.id) == clean

    plain = "系统生成的局限说明。\n\n- 第一条\n- 第二条"
    sec2 = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": plain}, by_turn_id="t2")
    assert _row_md(tmp_path / "canvas.db", sec2.id) == plain


async def test_metadata_only_patch_never_deletes_content(store: CanvasStore, tmp_path: Path) -> None:
    """只改 `_meta` 的写入不许删正文里的东西（独立 review #7）。

    图被删掉之后，正文里指向它的锚就成了「无效 id」。若一次无关的元数据 patch
    （例如 `canvas_revise` 打溯源标记）顺手把无效锚连同「图N：」图注剥掉，
    用户会在没有任何信号的情况下丢掉内容，而且前像已经没了、事后查不出来。
    """
    db = tmp_path / "canvas.db"
    canvas_id, _fid = await _seed(store)
    sec = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "占位"}, by_turn_id="t2")
    # Both invalid and misplaced-valid anchors present: neither may change.
    leaked = f"前文。\n\n[figure:sec_gone]\n**图1**：说明\n\n又见[figure:{_fid}]可知。"
    _force_md(db, sec.id, leaked)

    await store.patch_section(section_id=sec.id, patch={"_meta": {"provenance": True}}, by_turn_id="t3")
    assert _row_md(db, sec.id) == leaked  # Byte-identical: no deletion, movement, or rewriting.

    # Actual body writes still strip invalid anchors and orphan captions.
    await store.patch_section(section_id=sec.id, patch={"markdown": leaked}, by_turn_id="t4")
    assert "[figure:sec_gone]" not in _row_md(db, sec.id)
    assert "**图1**：说明" not in _row_md(db, sec.id)


async def test_backfill_job_skips_rows_edited_after_the_scan(store: CanvasStore, tmp_path: Path) -> None:
    """洗存量作业必须带 CAS：它算好的整段正文是**扫描那一刻**的快照。

    作业是对着在跑的生产库执行的，扫描与写入之间任何一次写入（用户编辑、RC-8 回写、
    正在跑的一轮）若被无条件整段覆盖，丢的就不只是锚，而是别人刚写进去的正文。
    """
    from bowhead_core.scripts.figure_anchor_normalize import apply as job_apply
    from bowhead_core.scripts.figure_anchor_normalize import collect as job_collect

    db = tmp_path / "canvas.db"
    canvas_id, fid = await _seed(store)
    sec = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "占位"}, by_turn_id="t2")
    _force_md(db, sec.id, f"对照[figure:{fid}]可见。")

    todo = job_collect(db)
    assert len(todo) == 1

    # Another writer changes section after scan, before write.
    await store.patch_section(section_id=sec.id, patch={"markdown": "别人刚改过的正文。"}, by_turn_id="t9")

    ok, failed, skipped = await job_apply(db, todo)
    # Conflict means **skip**, not failure: routine rerun case must not make job appear broken.
    assert (ok, failed, skipped) == (0, 0, 1)
    assert _row_md(db, sec.id) == "别人刚改过的正文。"  # Snapshot never overwrote it.


async def test_repeated_write_is_idempotent(store: CanvasStore, tmp_path: Path) -> None:
    canvas_id, fid = await _seed(store)
    sec = await store.append_section(
        canvas_id=canvas_id,
        kind="narrative",
        payload={"markdown": MID_ANCHOR.format(fid=fid)},
        by_turn_id="t2",
    )
    once = _row_md(tmp_path / "canvas.db", sec.id)
    await store.patch_section(section_id=sec.id, patch={"markdown": once}, by_turn_id="t3")
    assert _row_md(tmp_path / "canvas.db", sec.id) == once


async def test_backfill_plan_equals_what_actually_lands(store: CanvasStore, tmp_path: Path) -> None:
    """作业 dry-run 展示的 `after` 必须**逐字节等于**最终落库的内容。

    早先 `collect()` 与写入口用了不同的 strip 策略：操作员看到的 diff 说「不删内容」，
    而 `apply()` 经 `patch_section` 再归一时把无效锚连同「图N：」图注删掉了——正是「盲签」。
    更要命的是我原来的用例只断言 `todo[0]["after"]`、从不看库行，所以这条一直是绿的
    （独立 review 四轮 #1）。现在断言的是**计划与落库一致**。
    """
    from bowhead_core.scripts.figure_anchor_normalize import apply as job_apply
    from bowhead_core.scripts.figure_anchor_normalize import collect as job_collect

    db = tmp_path / "canvas.db"
    canvas_id, fid = await _seed(store)
    sec = await store.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "占位"}, by_turn_id="t2")
    _force_md(db, sec.id, f"对照[figure:{fid}]可见[figure:sec_gone]。\n**图1**：已被删掉的图的说明\n\n尾段。")

    todo = job_collect(db)
    assert len(todo) == 1
    planned = todo[0]["after"]

    ok, failed, skipped = await job_apply(db, todo)
    assert (ok, failed, skipped) == (1, 0, 0)
    assert _row_md(db, sec.id) == planned
