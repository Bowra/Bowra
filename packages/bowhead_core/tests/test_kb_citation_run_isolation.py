"""知识库引用审计：并行 peer 共用 parent_ctx 时，编号表 / 调用快照 / nested 交接必须按 run 隔离。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core.agents._lead_context import (
    publish_nested_invocations,
    reset_current_ref_allowlist,
    set_current_ref_allowlist,
    take_nested_invocations,
)
from bowhead_core.agents.lead_orchestrator import _resolve_citations
from bowhead_core.citations import ensure_registry
from bowhead_core.subagent import chat_direct_blockers
from bowhead_core.subagent_return_tool import CTX_PAYLOAD_ATTR, register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.types import ToolInvocation

pytestmark = pytest.mark.unit

CHUNK_A = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"
CHUNK_B = "chunk_bbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_REF_VALIDATE_STRICT", "0")


def _ctx() -> ToolExecutionContext:
    ctx = ToolExecutionContext(workspace_id="ws_t", member_uid="m", user_id="u", turn_id="t_1", datarc=None, session_id="s_1")  # type: ignore[arg-type]
    ctx._current_subagent_invocations = []
    ctx._current_subagent_id = "sub_A"
    return ctx


async def test_subagent_return_translates_with_this_runs_table_not_the_shared_attr() -> None:
    """A、B 两个 peer 并行：共享 attr 上是后启动的 B 的表，A 交稿时必须用 A 自己的表翻 [ref:1]。"""
    mine, sibling = RefAllowlist(), RefAllowlist()
    mine.add("doc", CHUNK_A)  # A 1.
    sibling.add("doc", CHUNK_B)  # B 1.
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _ctx()
    ctx._ν15_allowlist = sibling
    token = set_current_ref_allowlist(mine)
    try:
        payload = {
            "title": "",
            "exec_summary": "定价 6499 元起 [ref:1]",
            "kpis": [],
            "insights": [],
            "recommendations": [],
            "delivery_surface": "chat",
        }
        inv = await reg.execute("subagent_return", {"payload": payload}, ctx)
    finally:
        reset_current_ref_allowlist(token)
    assert inv.error is None, inv.error
    stored = getattr(ctx, CTX_PAYLOAD_ATTR)
    assert f"[ref:{CHUNK_A}]" in stored["exec_summary"]
    assert CHUNK_B not in stored["exec_summary"]


async def test_parallel_spawns_keep_their_own_nested_invocations() -> None:
    ctx = SimpleNamespace()
    a_list = [ToolInvocation(tool_name="kb_search", arguments={"q": "a"})]
    b_list = [ToolInvocation(tool_name="bi_query", arguments={"q": "b"})]
    a_published = asyncio.Event()

    async def spawn(mine: list[Any], *, wait_for_sibling: bool) -> list[Any] | None:
        publish_nested_invocations(ctx, mine)
        if wait_for_sibling:
            a_published.set()
            await asyncio.sleep(0.01)  # Sibling publishes and overwrites shared attr.
        else:
            await a_published.wait()
        return take_nested_invocations(ctx)

    got_a, got_b = await asyncio.gather(spawn(a_list, wait_for_sibling=True), spawn(b_list, wait_for_sibling=False))
    assert got_a is a_list
    assert got_b is b_list


async def test_spawn_that_published_nothing_does_not_take_a_siblings_list() -> None:
    ctx = SimpleNamespace()

    async def sibling() -> None:
        publish_nested_invocations(ctx, [ToolInvocation(tool_name="kb_search", arguments={})])
        await asyncio.sleep(0.02)
        take_nested_invocations(ctx)

    async def failed_spawn() -> list[Any] | None:
        await asyncio.sleep(0.01)
        return take_nested_invocations(ctx)

    _, got = await asyncio.gather(sibling(), failed_spawn())
    assert got is None


def test_backticked_markers_still_become_chips() -> None:
    ctx = SimpleNamespace()
    inv = [
        ToolInvocation(tool_name="kb_search", arguments={}, result={"chunks": [{"chunk_id": CHUNK_A, "file_id": "kbf_1", "file_name": "手册.md"}]})
    ]
    out = _resolve_citations(f"付款周期 45 天 `[ref:{CHUNK_A}]`。", ctx, invocations=inv)
    assert out == "付款周期 45 天 [cite:1]。"
    assert ensure_registry(ctx).resolve(1) is not None


def test_chat_direct_blockers_ignore_calls_that_yielded_nothing() -> None:
    empty_kb = ToolInvocation(tool_name="kb_search", arguments={}, result={"chunks": []})
    failed = ToolInvocation(tool_name="web_search", arguments={}, error="timeout")
    hit = ToolInvocation(tool_name="kb_search", arguments={}, result={"chunks": [{"chunk_id": CHUNK_A}]})
    spawn = ToolInvocation(tool_name="subagent_spawn", arguments={})
    assert chat_direct_blockers([empty_kb, failed, spawn]) == []
    assert chat_direct_blockers([hit, spawn]) == ["kb_search"]


def test_stripped_marker_leaves_no_space_before_cjk_punct_outside_code() -> None:
    ctx = SimpleNamespace()
    out = _resolve_citations("营业额 1.36亿元 [cite:9]。\n```\nprint( x ) 。\n```", ctx)
    assert out == "营业额 1.36亿元。\n```\nprint( x ) 。\n```"
