"""`_scanner_gated_stream_cb` 的接线 UT —— 跑**真的** `_lead_loop`，不复刻闭包。

⚠ 这个文件重写过一次（canvas-summon，2026-08-04），原因值得记：

原版自己造了一个 `_StubSD` 和一个 `_build_gated_cb()`，注释写着「Mirrors the real
wire byte-for-byte where it matters」——**它测的是复刻件，不是产品代码**。于是当
「Lead 散文自动切段」被摘掉、真代码里 `sd.feed` 那条路默认不再存在之后，这个文件
**一行不改照样全绿**，却什么都不再守着。正是本仓那条「测试替调用方补齐了它不做的事」。

现在改成走完整 `TurnRunner.run`，断言 marker 有没有漏进**用户真正看见的那个面**：
`narrative.delta`（聊天气泡的逐字流）。这也是自动切段摘掉之后 scanner 的唯一下游。
"""

from __future__ import annotations

from typing import Any

import pytest
from test_turn_runner_contract import _make_fake_orch, _req  # noqa: F401  (harness in the same directory)


class _NoCanvasStore:
    async def get_canvas_by_session(self, session_id: str) -> Any:
        return None


async def _run(orch: Any, req: Any) -> tuple[str, dict]:
    """跑一轮，返回（拼起来的 narrative 文本, done 的 payload）。"""
    from bowhead_core.turn_runner import TurnRunner

    await TurnRunner(orch).run(req)
    bus = orch.event_registry.buses[req.turn_id]
    text = "".join(
        str((e.payload or {}).get("text") or (e.payload or {}).get("delta") or "") for e in bus.events if e.event_type == "narrative.delta"
    )
    done = next((dict(e.payload or {}) for e in reversed(bus.events) if e.event_type == "done"), {})
    return text, done


async def _stream(
    fake_subagent_runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str,
    flag: str | None,
    turn: str,
) -> tuple[str, dict]:
    if flag is None:
        monkeypatch.delenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", raising=False)
    else:
        monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", flag)
    monkeypatch.delenv("BOWHEAD_LEAD_CANVAS_AUTOCHOP", raising=False)
    fake_subagent_runner.stream_text = text
    orch = _make_fake_orch(subagent_runner=fake_subagent_runner, canvas_store=_NoCanvasStore())
    return await _run(orch, _req(turn_id=f"t_{turn}"))


@pytest.mark.asyncio
async def test_marker_never_reaches_the_user_visible_stream(fake_subagent_runner, monkeypatch) -> None:
    """默认（flag 未设 = ON）：`<response_kind>` 被剥掉，用户只看到正文。"""
    text, _done = await _stream(
        fake_subagent_runner,
        monkeypatch,
        text="<response_kind>chat</response_kind>hello",
        flag=None,
        turn="rk_default",
    )
    assert "response_kind" not in text.lower(), f"内部语法漏进了聊天气泡：{text!r}"
    assert "hello" in text, text


@pytest.mark.asyncio
async def test_marker_split_across_chunks_still_stripped(fake_subagent_runner, monkeypatch) -> None:
    """真实形状：marker 被切成很多小 delta 到达（Q3 实测）。逐块剥必漏，靠的是有状态扫描。"""
    text, _done = await _stream(
        fake_subagent_runner,
        monkeypatch,
        text="<response_kind>canvas</response_kind>after",
        flag="1",
        turn="rk_split",
    )
    assert "response_kind" not in text.lower(), f"分块到达时漏了：{text!r}"
    assert text.strip() == "after", text


@pytest.mark.asyncio
async def test_absent_marker_prose_passes_through_whole(fake_subagent_runner, monkeypatch) -> None:
    """【实测】116 上 92% 的轮次根本不发 marker。scanner 判 absent 后必须把缓冲原样放行，
    一个字都不能吞——否则那 92% 的聊天答案会被截头。"""
    prose = "这是没有任何标记的纯散文回答。" * 8
    text, _done = await _stream(fake_subagent_runner, monkeypatch, text=prose, flag="1", turn="rk_absent")
    assert text == prose, f"absent 路径吞字了：{text[:60]!r}"


@pytest.mark.asyncio
async def test_flag_is_read_at_call_time_not_import(fake_subagent_runner, monkeypatch) -> None:
    """flag 必须每次调用现读 —— 模块顶层读会把 env 冻在 import 时刻，翻不动。

    显式 `=0` 时 scanner 不介入；自动切段也已摘掉，所以这条 legacy 路径**什么都不发**。
    这正是它今天的真实形态（`_emit_narrative_delta` 全文只有 scanner-ON 那一个调用点），
    写在这里是为了让下一个人别把 `BOWHEAD_OMEGA_RESPONSE_KIND_V3=0` 当成流式的回滚路。
    """
    text, _done = await _stream(
        fake_subagent_runner,
        monkeypatch,
        text="<response_kind>chat</response_kind>hello",
        flag="0",
        turn="rk_off",
    )
    assert text == "", f"legacy 分支不该发 narrative.delta：{text!r}"
