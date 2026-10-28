"""A2 (report-edit-audit) — 级联删除的三重过滤 + 单事务 revision.

口径（PRD r3 已拍）：删含内联图的宿主时连带删图；子集由服务端解析，且必须
① 同 canvas ② kind ∈ {chart,image} ③ 未被本 canvas 其他存活 section 引用。
"""

from __future__ import annotations

import json

import pytest

from bowhead_core import CanvasStore


@pytest.fixture
async def store(tmp_path):
    s = CanvasStore(tmp_path / "cascade.db")
    await s.initialize()
    return s


async def _canvas(store: CanvasStore, session: str = "s_a", user: str = "usr_1"):
    return await store.ensure_canvas(session_id=session, workspace_id="ws_1", user_id=user)


async def _revisions(store: CanvasStore, canvas_id: str, op: str = "delete") -> list[str]:
    from bowhead_core._db import open_shared_ctx

    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute(
            "SELECT section_id FROM canvas_revisions WHERE canvas_id=? AND op=? ORDER BY rowid",
            (canvas_id, op),
        ) as cur,
    ):
        return [str(r[0]) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_cascade_deletes_host_and_exclusive_inline_figures(store) -> None:
    cv = await _canvas(store)
    c1 = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"t": 1}})
    c2 = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"t": 2}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"正文\n\n[figure:{c1.id}]\n\n更多\n\n[figure:{c2.id}]"},
    )
    keep = await store.append_section(canvas_id=cv.id, kind="code_block", payload={"code": "print(1)"})

    deleted = await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)

    assert set(deleted) == {host.id, c1.id, c2.id}
    remaining = {s.id for s in await store.list_sections(cv.id)}
    assert remaining == {keep.id}
    # Write one delete revision per deleted section, all in one transaction.
    assert set(await _revisions(store, cv.id)) == {host.id, c1.id, c2.id}


@pytest.mark.asyncio
async def test_cascade_skips_figure_referenced_by_another_section(store) -> None:
    """③ 双引用图只删独占的。"""
    cv = await _canvas(store)
    shared = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}})
    exclusive = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"[figure:{shared.id}]\n\n[figure:{exclusive.id}]"},
    )
    other = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"别处也引用 [figure:{shared.id}]"},
    )

    deleted = await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)

    assert set(deleted) == {host.id, exclusive.id}
    remaining = {s.id for s in await store.list_sections(cv.id)}
    assert remaining == {shared.id, other.id}


@pytest.mark.asyncio
async def test_cascade_cannot_reach_other_canvas_sections(store) -> None:
    """① 同 canvas —— 宿主 markdown 是用户可编辑内容, 写入他人 sid 不得越权删除."""
    mine = await _canvas(store, session="s_mine")
    victim_cv = await _canvas(store, session="s_victim", user="usr_victim")
    victim_chart = await store.append_section(canvas_id=victim_cv.id, kind="chart", payload={"spec": {}})
    host = await store.append_section(
        canvas_id=mine.id,
        kind="narrative",
        payload={"markdown": f"恶意引用 [figure:{victim_chart.id}]"},
    )

    deleted = await store.delete_section(section_id=host.id, canvas_id=mine.id, cascade=True)

    assert deleted == [host.id]
    assert {s.id for s in await store.list_sections(victim_cv.id)} == {victim_chart.id}


@pytest.mark.asyncio
async def test_cascade_kind_whitelist_blocks_non_visual_targets(store) -> None:
    """② kind 白名单 —— 引用了 narrative/sources 的 id 也不许连带删."""
    cv = await _canvas(store)
    other_text = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "另一段正文"})
    src = await store.append_section(canvas_id=cv.id, kind="sources", payload={"entries": []})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"[figure:{other_text.id}]\n\n[figure:{src.id}]"},
    )

    deleted = await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)

    assert deleted == [host.id]
    assert {s.id for s in await store.list_sections(cv.id)} == {other_text.id, src.id}


@pytest.mark.asyncio
async def test_insight_card_evidence_refs_cascade(store) -> None:
    cv = await _canvas(store)
    chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="insight_card",
        payload={"title": "t", "evidence": [{"chart_section_id": chart.id}]},
    )

    deleted = await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)

    assert set(deleted) == {host.id, chart.id}


@pytest.mark.asyncio
async def test_without_cascade_only_host_is_deleted(store) -> None:
    cv = await _canvas(store)
    chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"[figure:{chart.id}]"},
    )

    deleted = await store.delete_section(section_id=host.id, canvas_id=cv.id)

    assert deleted == [host.id]
    assert {s.id for s in await store.list_sections(cv.id)} == {chart.id}


@pytest.mark.asyncio
async def test_delete_returns_empty_list_when_missing_or_foreign(store) -> None:
    """空列表 falsy —— 既有 `if not ok:` 调用点语义不变."""
    cv = await _canvas(store)
    other = await _canvas(store, session="s_other", user="usr_2")
    sec = await store.append_section(canvas_id=other.id, kind="narrative", payload={"markdown": "x"})

    assert await store.delete_section(section_id="sec_missing", canvas_id=cv.id) == []
    assert await store.delete_section(section_id=sec.id, canvas_id=cv.id) == []
    assert {s.id for s in await store.list_sections(other.id)} == {sec.id}


@pytest.mark.asyncio
async def test_cascade_revision_payload_is_recoverable(store) -> None:
    """恢复路径依赖 prev_payload —— 级联子块的 payload 必须完整入 revision."""
    cv = await _canvas(store)
    chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"series": [1, 2, 3]}})
    host = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"[figure:{chart.id}]"},
    )
    await store.delete_section(section_id=host.id, canvas_id=cv.id, cascade=True)

    from bowhead_core._db import open_shared_ctx

    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute(
            "SELECT section_id, prev_payload FROM canvas_revisions WHERE canvas_id=? AND op='delete'",
            (cv.id,),
        ) as cur,
    ):
        rows = {str(r[0]): json.loads(r[1]) for r in await cur.fetchall()}
    # Plan A: prev_payload becomes a restoration envelope (restore_schema/kind/seq/batch + payload).
    assert rows[chart.id]["restore_schema"] == 1
    assert rows[chart.id]["kind"] == "chart"
    assert rows[chart.id]["payload"]["spec"]["series"] == [1, 2, 3]
    assert f"[figure:{chart.id}]" in rows[host.id]["payload"]["markdown"]
    # Host and cascading child visuals share batch; host has batch_host, the key for group restoration.
    assert rows[chart.id]["batch"] == rows[host.id]["batch"]
    assert rows[host.id]["batch_host"] == host.id
