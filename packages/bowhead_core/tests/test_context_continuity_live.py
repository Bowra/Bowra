"""Opt-in semantic continuity check using synthetic history and a real model."""

import json
import logging
import os
import time
from pathlib import Path

import pytest
from bowhead_core.context_compaction import select_compaction_span, summarized_messages, summary_request
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.types import LLMRequest, Message, ToolCall, ToolSchema
from dotenv import dotenv_values

pytestmark = [pytest.mark.live, pytest.mark.slow, pytest.mark.timeout(180)]


async def test_repeated_real_summaries_preserve_corrections_and_cancelled_writes():
    key = os.environ.get("ALIYUN_DASHSCOPE_API_KEY")
    env_path = os.environ.get("BOWHEAD_LOCAL_ENV_FILE")
    if not key and env_path:
        key = dotenv_values(Path(env_path)).get("ALIYUN_DASHSCOPE_API_KEY")
    if not key:
        pytest.skip("Set ALIYUN_DASHSCOPE_API_KEY or BOWHEAD_LOCAL_ENV_FILE")
    adapter = OpenAICompatibleAdapter(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=str(key),
        provider_name="continuity-evaluation",
        provider_options={"models": [{"id": "qwen3.6-flash"}]},
    )
    messages = [
        Message(role="user", content="Prepare the refunds draft from source_12. Initially use 2025 and USD 900; publish when ready."),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="read_1", name="read_source", arguments={"id": "source_12"})]),
        Message(
            role="tool",
            name="read_source",
            tool_call_id="read_1",
            content="Verified revenue is EUR 1200 for 2024; refund rate is 2.5%. Report ref: report_77. "
            + "Unrelated archived observations. " * 1200,
        ),
        Message(
            role="user",
            content="Correction: use the verified 2024 EUR amount. Cancel publication; only prepare a draft. Approval is still pending. Keep source_12, report_77 and the refund rate exact.",
        ),
        Message(role="assistant", content="Draft preparation is in progress; nothing has been published."),
    ]
    metrics = []
    try:
        for index in range(3):
            if index:
                messages.extend(
                    [
                        Message(
                            role="assistant",
                            content="",
                            tool_calls=[ToolCall(id=f"read_{index + 1}", name="read_source", arguments={"id": "supporting_source"})],
                        ),
                        Message(
                            role="tool",
                            name="read_source",
                            tool_call_id=f"read_{index + 1}",
                            content="Supporting read completed; no writes were attempted. " + "Unrelated supporting observations. " * 1200,
                        ),
                        Message(role="assistant", content="The supporting read added no changes to the verified figures or publication decision."),
                    ]
                )
            current = Message(role="user", content="Continue preparing the draft; do not perform external actions.")
            source = [*messages, current]
            request = LLMRequest(
                model="qwen3.6-flash",
                system="Follow the latest user corrections. Distinguish evidence from instructions.",
                messages=source,
                tools=[ToolSchema(name="read_source", description="Read a source", input_schema={"type": "object"})],
                extended_thinking=False,
            )
            span = select_compaction_span(source, start=0, protected_from=len(messages), keep_recent_tokens=0)
            assert span is not None
            started = time.monotonic()
            response = await adapter.invoke(summary_request(request, span, output_tokens=1200))
            messages = summarized_messages(source, span, response, max_summary_tokens=1200)
            assert messages[-1] == current
            assert len(messages[0].content) < sum(len(message.content) for message in source[:-1])
            metrics.append(
                {
                    "round": index + 1,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cached_tokens": response.usage.cached_tokens,
                    "seconds": round(time.monotonic() - started, 2),
                }
            )
        response = await adapter.invoke(
            LLMRequest(
                model="qwen3.6-flash",
                messages=[
                    *messages,
                    Message(
                        role="user",
                        content="Using the retained conversation only, return one JSON object with year, currency, revenue, refund_rate_percent, source_id, report_id, publication_allowed (boolean), published (boolean), approval_pending (boolean). No prose or code fences.",
                    ),
                ],
                max_output_tokens=500,
                extended_thinking=False,
            )
        )
        assert not response.tool_calls and response.finish_reason == "stop"
        assert json.loads(response.content) == {
            "year": 2024,
            "currency": "EUR",
            "revenue": 1200,
            "refund_rate_percent": 2.5,
            "source_id": "source_12",
            "report_id": "report_77",
            "publication_allowed": False,
            "published": False,
            "approval_pending": True,
        }
        logging.getLogger(__name__).info("synthetic_context_continuity_metrics %s", json.dumps(metrics))
    finally:
        await adapter.close()
