"""Only server-marked rejected responses are hidden from model requests."""

import json
from dataclasses import asdict

import pytest
from bowhead_router.parameter_preview import compile_request_body
from bowhead_router.runtime_notes import REJECTED_TOOL_SYNTAX_HISTORY_NAME, REJECTED_TOOL_SYNTAX_INSTRUCTION, model_messages
from bowhead_router.types import LLMRequest, Message, ToolCall

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind,surface", [("openai_compatible", None), ("openai_compatible", "responses"), ("anthropic_compatible", None)])
def test_rejected_body_survives_checkpoint_but_not_outbound_model_history(kind, surface):
    raw = '<clarification_ask"><questions>private-rejected-payload</questions></clarification_ask>'
    original = Message(role="assistant", name=REJECTED_TOOL_SYNTAX_HISTORY_NAME, content=raw)
    restored = Message(**json.loads(json.dumps(asdict(original))))
    request = LLMRequest(model="deployment", messages=[Message(role="user", content="Ask for missing input."), restored])
    model = {"id": "deployment", "context_window": 100_000, "max_output_tokens": 8192}
    if surface:
        model["api_surface"] = surface
    body = compile_request_body({"type": kind, "base_url": "https://example.com/v1", "models": [model]}, request)

    assert "private-rejected-payload" not in json.dumps(body)
    assert REJECTED_TOOL_SYNTAX_INSTRUCTION in json.dumps(body)
    assert REJECTED_TOOL_SYNTAX_HISTORY_NAME not in json.dumps(body)
    assert restored == original
    assert request.messages[-1].content == raw


def test_user_examples_ordinary_corrections_and_native_calls_are_not_rewritten():
    raw = '<clarification_ask questions="example"/>'
    messages = [
        Message(role="user", content="Explain this example: " + raw, name=REJECTED_TOOL_SYNTAX_HISTORY_NAME),
        Message(role="assistant", content="```xml\n" + raw + "\n```"),
        Message(role="assistant", content="The report needs more evidence."),
        Message(
            role="assistant", content=raw, name=REJECTED_TOOL_SYNTAX_HISTORY_NAME, tool_calls=[ToolCall(id="original", name="read", arguments={})]
        ),
        Message(role="tool", content="Original result.", name="read", tool_call_id="original"),
    ]
    assert model_messages(messages) == messages
