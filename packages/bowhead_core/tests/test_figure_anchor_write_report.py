"""上层写路的图锚归一：`write_report` 追加 / 更新 / 按章切分都必须落成段首锚。

这几条走的是 `bake_report_markdown`（老 F4 闸）→ `CanvasStore` 写入口。F4 对句中锚
不做任何事、对行尾锚的「归一」在画布上也不成立，所以这里证明的是：**闸下沉到存储层之后，
经由写手工具写进来的正文同样被归一**。断言的是库里的行。
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
from bowhead_core.agents.report_tools import _write_report
from bowhead_core.canvas_store import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def ctx(tmp_path: Path):
    store = CanvasStore(tmp_path / "t.db")
    await store.initialize()
    await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return SimpleNamespace(
        canvas_store=store,
        session_id="s1",
        turn_id="t1",
        workspace_id="w",
        user_id="u",
        parent_bus=SimpleNamespace(),
    )


def _row_md(db_path: Path, section_id: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT payload FROM canvas_sections WHERE id=?", (section_id,)).fetchone()
    finally:
        con.close()
    return str((json.loads(row[0]) if row else {}).get("markdown") or "")


async def _chart(ctx, title: str = "各门店营业额排名") -> str:
    canvas = await ctx.canvas_store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    sec = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"kind": "hbar", "title": title}},
        by_turn_id="t1",
    )
    return str(sec.id)


async def test_write_report_append_normalizes_mid_anchor(ctx, tmp_path: Path) -> None:
    fid = await _chart(ctx)
    tok = set_current_peer_name("compute")
    try:
        res = await _write_report(
            {"kind": "narrative", "content": f"## 门店排名\n\n对照全年排名[figure:{fid}]可见，头部稳固。"},
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert res["ok"]
    stored = _row_md(tmp_path / "t.db", res["section_id"])
    assert "「各门店营业额排名」" in stored
    assert stored.endswith(f"\n\n[figure:{fid}]")
    assert f"排名[figure:{fid}]可见" not in stored


async def test_write_report_update_normalizes_f4_shape(ctx, tmp_path: Path) -> None:
    """行尾锚经 F4 会变成 `正文\\n[figure:x]`——那个形态在画布上照样漏，存储层要补上空行。"""
    fid = await _chart(ctx)
    tok = set_current_peer_name("compute")
    try:
        first = await _write_report({"kind": "narrative", "content": "初稿正文。"}, ctx)
        res = await _write_report(
            {
                "kind": "narrative",
                "section_id": first["section_id"],
                "expected_rev": first["rev"],  # Update needs rev or handler returns rev_required.
                "content": f"前文说明见图。[figure:{fid}]",
            },
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert res["ok"]
    stored = _row_md(tmp_path / "t.db", first["section_id"])
    assert stored == f"前文说明见图。\n\n[figure:{fid}]"


async def test_write_report_chapters_normalize_each_chapter(ctx, tmp_path: Path) -> None:
    """按章切分是在 bake 之后做的，每章各自落库——归一必须逐章生效。"""
    fid = await _chart(ctx)
    tok = set_current_peer_name("compute")
    try:
        res = await _write_report(
            {
                "kind": "narrative",
                "content": ("## 第一章\n\n正常正文，没有锚。\n\n## 第二章\n\n按门店拆解（[figure:" + fid + "]），头部集中。"),
            },
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert res["ok"]
    ids = res.get("section_ids") or [res["section_id"]]
    texts = [_row_md(tmp_path / "t.db", sid) for sid in ids]
    with_anchor = [t for t in texts if "[figure:" in t]
    assert with_anchor, "锚不见了"
    for text in with_anchor:
        assert "「各门店营业额排名」" in text
        assert text.endswith(f"\n\n[figure:{fid}]")
