"""报告正文里的知识库引用：write_report 写路把 `[ref:N]`（doc）翻成片段 id 并登记成来源编号 [N]。

知识库回答引用批 3：此前 write_report 只翻图的号，bake 第一步 strip_legacy 把 `[ref:…]` 全删——
peer 按指令写的知识库引用在报告正文里一个都留不下。
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core.agents._lead_context import (
    reset_current_peer_name,
    reset_current_ref_allowlist,
    set_current_peer_name,
    set_current_ref_allowlist,
)
from bowhead_core.agents.report_tools import _write_report
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.report_writer_loop import bake_report_markdown
from bowhead_core.sources_resolver import ensure_turn_sources_registry

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

CHUNK_A = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"
CHUNK_B = "chunk_bbbbbbbbbbbbbbbbbbbbbbbb"


def _kb_search(*chunks: tuple[str, str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name="kb_search",
        error=None,
        arguments={"query": "华东区三季度"},
        result={"chunks": [{"chunk_id": c, "file_id": f, "file_name": n, "content": "营业额同比增长"} for c, f, n in chunks]},
    )


async def test_real_chunk_ref_becomes_numbered_source_with_file_name() -> None:
    bus = SimpleNamespace()
    invs = [_kb_search((CHUNK_A, "kbf_1", "华东月报.md"))]
    md, _ = await bake_report_markdown(
        f"华东区营业额同比增长 7.3%[ref:{CHUNK_A}]，再次提到 [ref:{CHUNK_A}]。",
        ctx=SimpleNamespace(parent_bus=bus, _report_bake_invocations=invs),
    )
    reg = ensure_turn_sources_registry(bus)
    assert md == "华东区营业额同比增长 7.3% [1]，再次提到 [1]。"
    assert reg.sources == [{"label": "华东月报.md", "tool": "kb_search", "artifact_id": CHUNK_A, "query": None}]


async def test_chunk_not_returned_this_turn_is_dropped() -> None:
    bus = SimpleNamespace()
    invs = [_kb_search((CHUNK_A, "kbf_1", "华东月报.md"))]
    md, _ = await bake_report_markdown(f"编造的出处 [ref:{CHUNK_B}]。", ctx=SimpleNamespace(parent_bus=bus, _report_bake_invocations=invs))
    assert md == "编造的出处。"
    assert ensure_turn_sources_registry(bus).is_empty()


async def test_without_registry_the_marker_is_dropped_not_leaked() -> None:
    md, _ = await bake_report_markdown(f"正文 [ref:{CHUNK_A}]。", ctx=SimpleNamespace())
    assert md == "正文。"


@pytest.fixture
async def ctx(tmp_path: Path) -> SimpleNamespace:
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
        _current_subagent_invocations=[_kb_search((CHUNK_A, "kbf_1", "华东月报.md"), (CHUNK_B, "kbf_2", "合同.docx"))],
    )


def _row_md(db_path: Path, section_id: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT payload FROM canvas_sections WHERE id=?", (section_id,)).fetchone()
    finally:
        con.close()
    return str((json.loads(row[0]) if row else {}).get("markdown") or "")


async def test_peer_write_report_translates_its_own_doc_numbers(ctx: SimpleNamespace, tmp_path: Path) -> None:
    """peer 按指令写 `[ref:N]`（N 取自**本 run 自己的**清单）⇒ 报告正文留下来源编号，不再被剥光。"""
    al = RefAllowlist()
    al.add("data", "art_abc")
    n_doc = al.add("doc", CHUNK_B)
    peer_tok = set_current_peer_name("kb-analyst")
    al_tok = set_current_ref_allowlist(al)
    try:
        res = await _write_report({"kind": "narrative", "content": f"## 付款\n\n合同约定付款周期为 45 天[ref:{n_doc}]。"}, ctx)
    finally:
        reset_current_ref_allowlist(al_tok)
        reset_current_peer_name(peer_tok)
    assert res["ok"]
    stored = _row_md(tmp_path / "t.db", res["section_id"])
    assert "45 天 [1]。" in stored
    assert "[ref:" not in stored
    assert ensure_turn_sources_registry(ctx.parent_bus).sources[0]["artifact_id"] == CHUNK_B


async def test_peer_report_can_cite_chunks_the_lead_retrieved() -> None:
    """116 1.3.56：Lead 检索、compute 写报告——peer 自己没检索，但上级已拿到的片段必须能登记。"""
    from bowhead_core.agents._lead_context import (
        inherit_parent_kb_invocations,
        reset_parent_kb_invocations,
        set_current_subagent_invocations,
    )

    lead_invs = [_kb_search((CHUNK_A, "kbf_1", "华东月报.md")), SimpleNamespace(tool_name="kb_search", result={"chunks": []})]
    set_current_subagent_invocations(lead_invs)  # Lead-run snapshot inherited by peer task.
    token = inherit_parent_kb_invocations()
    try:
        bus = SimpleNamespace()
        md, _ = await bake_report_markdown(
            f"华东区营业额同比增长 7.3%[ref:{CHUNK_A}]。",
            ctx=SimpleNamespace(parent_bus=bus, _report_bake_invocations=[]),  # Peer made no own calls.
        )
    finally:
        reset_parent_kb_invocations(token)
    assert md == "华东区营业额同比增长 7.3% [1]。"


async def test_peer_table_numbers_parent_kb_hits() -> None:
    from bowhead_core.agents._lead_context import (
        inherit_parent_kb_invocations,
        parent_kb_invocations,
        reset_parent_kb_invocations,
        set_current_subagent_invocations,
    )

    spawn = SimpleNamespace(tool_name="subagent_spawn", result={}, nested=[_kb_search((CHUNK_B, "kbf_2", "合同.pdf"))])
    set_current_subagent_invocations([_kb_search((CHUNK_A, "kbf_1", "月报.md")), spawn])
    token = inherit_parent_kb_invocations()
    try:
        al = RefAllowlist()
        await al.refresh_from_stores(
            canvas_store=None, artifact_store=None, session_id=None, user_id=None, canvas_id=None, tool_invocations=parent_kb_invocations()
        )
    finally:
        reset_parent_kb_invocations(token)
    docs = {al.resolve(str(i)) for i in range(1, 5) if al.kind_of(i) == "doc"}
    assert docs == {CHUNK_A, CHUNK_B}
