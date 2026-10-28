"""批 4 S2/S3 → delivery-surface M-D 改版: fallback sources + 并集守卫 — 行为级.

多写手/editor 编排（含 writer-{prof}-{step_id} 槽位种子）随 M-D 删除——
per-domain 授权移进 peer 本人（owner=sub_*, report_tools 服务端盖章）。
本文件保留仍然活着的两块: ①_plan_fallback_report_payload 聚合/去重;
②_do_update 对 writer-* 槽的 source_refs 并集守卫（handler 逻辑仍在, 直接
单元级驱动——若未来重新出现带种子的 writer-* 槽, 守卫仍受测）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.agents._lead_context import (
    reset_current_peer_name,
    set_current_peer_name,
)
from bowhead_core.agents.lead_orchestrator import _plan_fallback_report_payload
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.report_writer_loop import compose_report

from bowhead_router import ToolCall

pytestmark = pytest.mark.asyncio


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


def _resp(content="", tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cached_tokens=0),
    )


class _Router:
    """域写手写本域正文;编辑不写。"""

    def __init__(self, refs_for: dict[str, list[str]] | None = None) -> None:
        self._n = 0
        self._refs_for = refs_for or {}

    async def invoke(self, agent_type, req, **kw):
        self._n += 1
        text = "".join(m.content or "" for m in req.messages)
        if "总编辑" in text or "请直接输出整篇报告" in text:
            return _resp()
        for prof in ("compute", "financial-analyst"):
            if f"「{prof}」" in text:
                args = {"kind": "narrative", "content": f"{prof} 章节正文"}
                if prof in self._refs_for:
                    args["source_refs"] = self._refs_for[prof]
                return _resp(tool_calls=[ToolCall(id=f"c{self._n}", name="write_report", arguments=args)])
        return _resp()


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "w.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


def _request() -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id="t1", text="写一份综合分析报告")


def _plan_peers() -> list[dict]:
    """plan 腿形状: 带 step_id + sources(lead_orchestrator._peer_payloads 同形)."""
    return [
        {
            "profile": "compute",
            "step_id": "ps_aaa000000001",
            "payload": {
                "title": "compute专题",
                "exec_summary": "算" * 30,
                "insights": [{"thesis": "要点"}],
                "sources": [{"label": "统计局", "tool": "web_search", "url": "https://stats.gov.cn/x"}],
            },
        },
        {
            "profile": "financial-analyst",
            "step_id": "ps_bbb000000002",
            "payload": {
                "title": "financial-analyst专题",
                "exec_summary": "财" * 30,
                "insights": [{"thesis": "要点"}],
                "sources": [{"label": "BI 数仓", "tool": "bi_query", "artifact_id": "art_9"}],
            },
        },
    ]


async def _run(store, router, peers) -> None:
    tok = set_current_peer_name("lead")
    try:
        await compose_report(
            canvas_store=store,
            request=_request(),
            bus=_Bus(),
            payload={"title": "T"},
            peer_payloads=peers,
            router=router,
        )
    finally:
        reset_current_peer_name(tok)


async def test_non_writer_section_keeps_llm_refs_verbatim(store: CanvasStore) -> None:
    """P2-1 收窄的另一半: 非 writer-* 段(chat 路/编辑器)不并集——LLM 传什么就
    是什么, 用户修订段落时能移除错引来源。批4 挂账的漏测: 只断言过并集生效,
    没断言过"该失效的地方确实失效"(否则收窄等于没做)."""
    from bowhead_core.agents.report_tools import _do_update

    canvas = await store.get_canvas_by_session("s1")
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "正文", "source_refs": ["art_old"]},
        by_turn_id="t1",
        owner="report-editor",
    )
    res = await _do_update(
        store,
        sec.id,
        {"markdown": "正文2", "source_refs": ["art_new"]},
        sec.rev,
        "report-editor",
        "t1",
        canvas_id=canvas.id,
    )
    assert "error" not in res, res
    secs = await store.list_sections(canvas.id, include_hidden=True)
    got = next(s for s in secs if s.id == sec.id)
    assert got.payload.get("source_refs") == ["art_new"]  # Replace whole key, no union.


async def test_llm_refs_union_with_seed_on_writer_slot(store: CanvasStore) -> None:
    """P2-6/P2-1 守卫仍活: writer-* 槽上 LLM 传 source_refs 不得整键覆写种子——
    并集保序。M-D 后多写手编排已删, 本测试直驱 _do_update（守卫的真实位置）。"""
    from bowhead_core.agents.report_tools import _do_update

    canvas = await store.get_canvas_by_session("s1")
    seeded = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "草稿", "source_refs": ["https://stats.gov.cn/x"]},
        by_turn_id="t1",
        owner="writer-compute-ps_aaa000000001",
    )
    res = await _do_update(
        store,
        seeded.id,
        {"markdown": "compute 章节正文", "source_refs": ["art_llm"]},
        seeded.rev,
        "writer-compute-ps_aaa000000001",
        "t1",
        ctx=None,
        canvas_id=canvas.id,
    )
    assert res.get("ok"), res
    secs = await store.list_sections(canvas.id, include_hidden=True)
    slot = next(s for s in secs if s.owner == "writer-compute-ps_aaa000000001")
    assert slot.payload.get("source_refs") == ["https://stats.gov.cn/x", "art_llm"]


async def test_fallback_payload_aggregates_sources_dedup() -> None:
    import json

    class _S:
        def __init__(self, agent, payload):
            self.agent_name = agent
            self.status = "done"
            self.payload_json = json.dumps(payload)
            self.objective = "obj"

    legs = [
        _S("compute", {"exec_summary": "a", "sources": [{"label": "统计局", "tool": "web_search"}, {"label": "统计局", "tool": "web_search"}]}),
        _S("financial-analyst", {"exec_summary": "b", "sources": [{"label": "BI 数仓", "tool": "bi_query", "artifact_id": "art_1"}]}),
    ]
    # ⚠ `plan_title` parameter **deliberately removed** by 2026-08-25 closing review (decision #6.6:
    # plans.title truncates user wording, never becomes report title; removing parameter structurally guarantees it).
    # Test kept passing it ⇒ always TypeError since then, **zero** dedup criterion coverage.
    out = _plan_fallback_report_payload(legs)
    assert out is not None
    assert [s["label"] for s in out["sources"]] == ["统计局", "BI 数仓"]  # Deduplicate in order.


async def test_peer_payloads_step_id_injection_pin() -> None:
    """lead_orchestrator 注入点钉(消费侧已被上面行为测试盖住;此钉防生产侧
    重构时静默丢 step_id 键——owner 编码整链会无声死亡)。"""
    src = (Path(__file__).parent.parent / "src/bowhead_core/agents/lead_orchestrator.py").read_text()
    assert '"step_id": getattr(s, "id", "")' in src
