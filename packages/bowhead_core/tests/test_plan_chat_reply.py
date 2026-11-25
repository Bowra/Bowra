"""The chat reply of a Plan Turn that saved a report (docs/sprints/plan-chat-reply-2026-11-24/design.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.plan_chat_reply import generate_plan_reply, plan_reply_facts


def _section(kind: str, payload: dict[str, Any], *, superseded: bool = False) -> Any:
    return SimpleNamespace(kind=kind, payload=payload, superseded=superseded)


def test_facts_name_the_report_each_step_and_the_saved_files() -> None:
    sections = [
        _section("h1", {"text": "2025 经营分析"}),
        _section("kpi_row", {"cells": [{"lbl": "营业额", "val": "163.42亿元", "delta": "+0.02%"}]}),
        _section("narrative", {"markdown": "## 门店维度：Top10 占 55.26% [7]\n\n正文", "_meta": {}}),
        _section("narrative", {"markdown": "正文", "_meta": {"chapter_title": "品牌维度"}}),
        _section("narrative", {"markdown": "## 被替换的旧章"}, superseded=True),
        _section("code_block", {"code": "print(1)"}),
    ]
    steps = [
        {
            "agent": "business-analyst",
            "status": "done",
            "objective": "门店维度",
            "payload": json.dumps({"exec_summary": "营业额 163.42亿元 [cite:1]", "caveats": ["未扣退款"]}),
        },
        {
            "agent": "business-analyst",
            "status": "done",
            "objective": "品类维度",
            "payload": {"partial_completion": {"stop_reason": "no_progress", "remaining": "排名未算出"}},
        },
    ]
    facts = plan_reply_facts(objective="做一份 2025 经营分析", sections=sections, steps=steps, files=["简报.html"])
    assert "Report title: 2025 经营分析" in facts
    assert "Key figures: 营业额: 163.42亿元 (+0.02%)" in facts
    assert "Report chapters: 门店维度：Top10 占 55.26% | 品牌维度" in facts
    assert "被替换的旧章" not in facts and "print(1)" not in facts
    assert "Summary: 营业额 163.42亿元\n  Caveat: 未扣退款" in facts
    assert "- business-analyst (finished only part of its work): 品类维度\n  Not done: 排名未算出" in facts
    assert facts.endswith("Files saved in this Turn: 简报.html")


def test_facts_are_bounded_and_say_when_no_file_was_saved() -> None:
    steps = [{"agent": "compute", "status": "done", "objective": "o", "payload": {"exec_summary": "x" * 5000}} for _ in range(20)]
    facts = plan_reply_facts(objective="q", sections=[], steps=steps, files=[])
    assert len(facts) <= 12_001
    assert facts.count("- compute (done)") == 12
    assert "Files saved in this Turn: none" in plan_reply_facts(objective="q", sections=[], steps=[], files=[])


class _Router:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content, self.error, self.requests = content, error, []

    async def invoke(self, agent_type: str, req: Any) -> Any:
        self.requests.append((agent_type, req))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content, usage=SimpleNamespace(input_tokens=900, output_tokens=120, cached_tokens=10))


@pytest.mark.asyncio
async def test_reply_is_requested_in_the_answer_language_and_billed() -> None:
    router = _Router("  Der Bericht zeigt …  ")
    usage: list[tuple[int, int, int]] = []
    reply = await generate_plan_reply(router, facts="Request: q", language="de-DE", trace_id="t_1", usage_out=usage)
    assert reply == "Der Bericht zeigt …"
    ((agent_type, request),) = router.requests
    assert agent_type == "narrative" and request.trace_id == "plan_chat_reply:t_1"
    assert "in German" in request.system
    assert request.messages[0].content == "Request: q\n\nWrite the reply in German, even if the facts above are in another language."
    assert usage == [(900, 120, 10)]
    other = _Router("x")
    await generate_plan_reply(other, facts="Request: q", language="ja-JP")
    assert "the language with code ja-JP" in other.requests[0][1].system


@pytest.mark.asyncio
@pytest.mark.parametrize("router", [None, _Router(error=RuntimeError("down")), _Router(error=TimeoutError())])
async def test_a_reply_that_cannot_be_written_is_empty(router: Any) -> None:
    assert await generate_plan_reply(router, facts="Request: q", language="zh-CN") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "partial", "expected"),
    [
        ("", 1, "计划已执行完成：2 步完成，1 步只完成了一部分，缺口见报告末尾。\n\n完整报告见画布。"),
        ("", 0, "计划已执行完成，共 3 步。\n\n完整报告见画布。"),
        ("报告显示利润率 12%。完整报告见画布。", 0, "报告显示利润率 12%。\n\n完整报告见画布。"),
    ],
)
async def test_plan_chat_text_appends_one_pointer_after_the_reply_or_the_completion(content: str, partial: int, expected: str) -> None:
    from bowhead_core.agents.lead_orchestrator import _plan_report_chat_text

    runtime = SimpleNamespace(
        router=_Router(content),
        parent_bus=SimpleNamespace(),
        parent_ctx=SimpleNamespace(canvas_store=None, artifact_store=None, language_context=SimpleNamespace(output_language="zh-CN")),
    )
    text = await _plan_report_chat_text(runtime, SimpleNamespace(turn_id="t_1"), objective="q", final_steps=[], done=3, partial=partial, usage_out=[])
    assert text == expected
