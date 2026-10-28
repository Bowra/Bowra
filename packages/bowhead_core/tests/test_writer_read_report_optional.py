"""report-streaming 批 R —— `read_report` 从"强制先调"降为"可选工具"。

116 prod 实测（docs/sprints/report-streaming/discovery.md §8.1）：
`read` 类调用（output<100，就是这次纯探查）占写手全部调用的 **56.1%**、占写手
**input token 的 54.5%**，read:write 比 p50 1.5 / 均值 2.3 —— 一半的 prompt 成本
花在只换回 30 个 token 的往返上。而它探查的东西（figures 连真实 id、KPI、建议、
caveats）**在用户消息的【证据】里已经给全**。

四条锁：
 1. 写手**直接 write_report、零 read_report** 也能产出完整报告（这是新的默认路）。
 2. **措辞漂移守卫**：三处 prompt / 工具描述里不得再出现"先 read_report 再写"这类强制。
    （原来有两处叠加：`WRITER_SYSTEM` 铁律 + `write_report` 工具 schema 铁律。）
 3. `read_report` **工具本身仍注册可用**——这批是去掉强制，不是删能力。
 4. 每次 `router.invoke` 有**墙钟观测**日志：`llm_usage_events.created_at` 是每 30s
    周期性 flush 的落库时刻（§8.2），拿它算耗时得到的是 flush 假象，只能在这里记。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.agents.report_tools import _read_report_schema
from bowhead_core.canvas_store import CanvasStore

from bowhead_router import ToolCall

# Mark only async cases; the next two are synchronous prompt/schema guards.

# Variants of mandatory read-before-write wording. Any match means the mandate has returned.
_MANDATE_PHRASES = (
    "先调用 read_report",
    "先 read_report",
    "read_report 再写",
)


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


class _StraightToWriteRouter:
    """新的默认形态：一次调用直接 write_report，全程不碰 read_report。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, agent_type, req, **kw):
        self.calls.append("invoke")
        return SimpleNamespace(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="write_report",
                    arguments={"kind": "narrative", "content": "## 结论\n\n华东区 GMV 环比增长 8.3%。"},
                )
            ],
            usage=SimpleNamespace(input_tokens=100, output_tokens=200, cached_tokens=0),
        )

    # Real Router supports both invoke and stream; an invoke-only fake is more permissive,
    # silently missing paths such as switching the writer to streaming (feedback_fake_looser_than_real).
    # Split invoke results into the **real stream shape**: first emit fragments of tool-argument JSON
    # (tool_call_partial), then a complete tool_call_delta, matching the openai_compat /
    # anthropic_compat adapters.
    async def stream(self, agent_type, req, **kw):
        import json as _json

        resp = await self.invoke(agent_type, req, **kw)
        if resp.content:
            yield SimpleNamespace(delta=resp.content, tool_call_delta=None, tool_call_partial=None, usage=None)
        for tc in resp.tool_calls or []:
            raw = _json.dumps(tc.arguments, ensure_ascii=False)
            # Small chunks force escapes/key names across boundaries, as in real streams.
            for i in range(0, len(raw), 7):
                yield SimpleNamespace(
                    delta="",
                    tool_call_delta=None,
                    tool_call_partial={
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "partial_args_json": raw[i : i + 7],
                    },
                    usage=None,
                )
            yield SimpleNamespace(delta="", tool_call_delta=tc, tool_call_partial=None, usage=None)
        yield SimpleNamespace(delta="", tool_call_delta=None, tool_call_partial=None, usage=resp.usage)


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "r.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


def _request() -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id="t1", text="写一份销售分析报告")


def test_read_report_tool_still_registered() -> None:
    """这批去掉的是**强制**，不是能力——写手确有需要（如更新已有段拿 rev）仍可调。"""
    schema = _read_report_schema()
    assert schema.name == "read_report"
    assert schema.description, "工具描述不能空"
