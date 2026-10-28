"""A Skill read may be retained across one structured question, never replayed."""

import json
from types import SimpleNamespace

import pytest
from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.reference_read_question import READ_QUESTION_REPAIR_PROMPT, ReferenceReadQuestion
from bowhead_core.types import ToolInvocation
from bowhead_router.types import ImageRef, Message, ToolCall

pytestmark = pytest.mark.unit


def _case():
    ctx = SimpleNamespace(
        turn_id="t1",
        session_id="s1",
        team_id="team1",
        user_id="u1",
        workspace_id="w1",
        reference_skill_required=True,
        reference_skill_access=object(),
    )
    binding = SimpleNamespace(execution_id="e1", policy_fingerprint="policy", capability_fingerprint="capability")
    read = ToolCall(id="read1", name="skill_read", arguments={"name": "dupont"})
    question = ToolCall(id="ask1", name="subagent_return", arguments={"payload": {"error": {"kind": "needs_user_input"}}})
    messages = [
        Message(role="user", content="计算 ROE"),
        Message(role="assistant", content="", tool_calls=[read]),
        Message(role="tool", name="skill_read", tool_call_id="read1", content='{"ok":true,"content":"ROE=净利率×周转率×权益乘数"}'),
        Message(role="assistant", content="", tool_calls=[question]),
        Message(role="tool", name="subagent_return", tool_call_id="ask1", content='{"ok":true,"__terminal":true}'),
    ]
    invocations = [
        ToolInvocation(tool_name="skill_read", arguments=read.arguments, result={"name": "dupont", "body": "ROE method"}),
        ToolInvocation(tool_name="subagent_return", arguments=question.arguments, result={"ok": True, "__terminal": True}),
    ]
    args = {
        "binding": binding,
        "ctx": ctx,
        "task": "计算 ROE",
        "extra_instructions": "按原任务计算",
        "subagent_id": "sub1",
        "profile": "analyst",
        "tool_deny": frozenset(),
        "effective_tools": frozenset({"skill_read", "subagent_return"}),
        "max_iterations": 8,
        "language_override": "zh-CN",
    }
    return messages, invocations, args


def test_read_question_resumes_exact_transcript_and_rejects_changed_authority():
    messages, invocations, args = _case()
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args)
    assert checkpoint is not None
    resumed = checkpoint.resume_messages(**args, answer="权益 50")
    assert resumed[:-1] == messages
    assert resumed[-1] == Message(role="user", content="权益 50")
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        checkpoint.resume_messages(**{**args, "language_override": "en-US"}, answer="权益 50")
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        checkpoint.resume_messages(**{**args, "effective_tools": frozenset({"subagent_return"})}, answer="权益 50")
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        checkpoint.resume_messages(
            **{**args, "binding": SimpleNamespace(**{**vars(args["binding"]), "capability_fingerprint": "changed"})}, answer="权益 50"
        )


def test_office_read_checkpoint_retains_exact_result_and_rejects_effects():
    messages, invocations, args = _case()
    arguments = {"artifact_id": "art_original"}
    result = {"ok": True, "artifact_id": "art_original", "content_hash": "sha256:" + "a" * 64, "text": "Original paragraph."}
    messages[1].tool_calls = [ToolCall(id="read1", name="attachment_read", arguments=arguments)]
    messages[2] = Message(role="tool", name="attachment_read", tool_call_id="read1", content=json.dumps(result))
    invocations[0] = ToolInvocation(tool_name="attachment_read", arguments=arguments, result=result)
    args["effective_tools"] = frozenset({"attachment_read", "subagent_return"})
    args["attachment_context"] = "Original metadata only."
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args)
    assert checkpoint is not None and checkpoint.schema_version == 5
    assert checkpoint.attachment_reads() == [("art_original", "sha256:" + "a" * 64)]
    resumed = checkpoint.resume_messages(**args, answer="FY2026")
    assert resumed[:-1] == messages
    assert checkpoint.matches_reads(invocations[:-1])
    saved = json.loads(checkpoint.messages_json)
    saved["read_tools"] = ["skill_invoke"]
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        ReferenceReadQuestion.model_validate({**checkpoint.model_dump(), "messages_json": json.dumps(saved)})
    result["artifact_id"] = "art_other"
    messages[2].content = json.dumps(result)
    assert ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args) is None


def test_one_prose_repair_keeps_the_read_without_replaying_the_prose():
    messages, invocations, args = _case()
    messages[-2:-2] = [
        Message(role="assistant", content="请提供股东权益"),
        Message(role="user", content=READ_QUESTION_REPAIR_PROMPT),
    ]
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=3, **args)
    assert checkpoint is not None
    assert checkpoint.schema_version == 2
    assert checkpoint.next_iteration == 3
    resumed = checkpoint.resume_messages(**args, answer="股东权益 50")
    assert len(resumed) == 6
    assert resumed[-1].content == "股东权益 50"
    assert all("请提供股东权益" not in message.content and message.content != READ_QUESTION_REPAIR_PROMPT for message in resumed)
    messages[-3].content = "unrelated instruction"
    assert ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=3, **args) is None


def test_read_question_rejects_failed_or_effectful_history():
    messages, invocations, args = _case()
    invocations[0].error = "read failed"
    assert ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args) is None
    invocations[0].error = None
    invocations[0].tool_name = "skill_invoke"
    assert ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args) is None


def test_existing_checkpoint_without_language_remains_readable():
    messages, invocations, args = _case()
    args["language_override"] = None
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args)
    assert checkpoint is not None
    old_shape = checkpoint.model_dump_json(exclude={"language_override"})
    restored = ReferenceReadQuestion.model_validate_json(old_shape)
    assert restored.resume_messages(**args, answer="权益 50")[-1].content == "权益 50"


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("material", ["text", "image", "both"])
def test_read_question_reopens_original_attachments_without_copying_them(repair, material):
    messages, invocations, args = _case()
    context = "Original attachment text" if material != "image" else None
    images = [ImageRef(base64="cGljdHVyZQ==", mime="image/png")] if material != "text" else []
    messages[0].images = images
    if repair:
        messages[-2:-2] = [Message(role="assistant", content="Provide equity."), Message(role="user", content=READ_QUESTION_REPAIR_PROMPT)]
    checkpoint = ReferenceReadQuestion.capture(
        messages=messages, invocations=invocations, model_turn_count=2 + int(repair), attachment_context=context, **args
    )
    assert checkpoint is not None
    assert checkpoint.schema_version == 3
    assert "Original attachment text" not in checkpoint.messages_json and "cGljdHVyZQ==" not in checkpoint.messages_json
    checkpoint = ReferenceReadQuestion.model_validate_json(checkpoint.model_dump_json())
    resumed = checkpoint.resume_messages(**args, answer="50", attachment_context=context, message_images=images)
    assert resumed[0] == messages[0] and resumed[2] == messages[2]
    assert resumed[-1].content == "50"
    assert len(resumed) == 6
    assert checkpoint.next_iteration == 2 + int(repair)


@pytest.mark.parametrize("change", ["text", "bytes", "mime", "order", "missing", "extra"])
def test_read_question_rejects_replaced_attachment_material(change):
    messages, invocations, args = _case()
    context = "Original text"
    images = [ImageRef(base64="b25l", mime="image/png"), ImageRef(base64="dHdv", mime="image/jpeg")]
    messages[0].images = images
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, attachment_context=context, **args)
    assert checkpoint is not None
    if change == "text":
        context = "Replaced text"
    elif change == "bytes":
        images[0].base64 = "dGhyZWU="
    elif change == "mime":
        images[0].mime = "image/webp"
    elif change == "order":
        images.reverse()
    elif change == "missing":
        images.pop()
    else:
        images.append(images[0])
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        checkpoint.resume_messages(**args, answer="50", attachment_context=context, message_images=images)


def test_existing_text_only_checkpoint_cannot_gain_an_attachment_on_resume():
    messages, invocations, args = _case()
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args)
    assert checkpoint is not None
    with pytest.raises(ValueError, match="reference_continuation_changed"):
        checkpoint.resume_messages(**args, answer="50", message_images=[ImageRef(base64="b25l", mime="image/png")])


@pytest.mark.parametrize("change", [None, "arguments", "result", "missing_receipt", "failed", "nested"])
def test_mcp_read_checkpoint_requires_delivered_receipts(change):
    messages, invocations, args = _case()
    name = "teammcp.finance.getMargin"
    args["ctx"].reference_skill_required = False
    args["ctx"].reference_skill_access = None
    args["effective_tools"] = frozenset({name, "subagent_return"})
    invocations[0].tool_name = name
    invocations[0].result = {"ok": True, "margin": 42}
    messages[1].tool_calls[0].name = messages[2].name = name
    messages[2].content = '{"ok":true,"margin":42}'
    args["ctx"].reference_mcp_read_receipts = [
        {
            "call_id": "mcp_original",
            "operation_name": name,
            "parameter_digest": canonical_fingerprint(invocations[0].arguments),
            "result_hash": canonical_fingerprint(invocations[0].result),
        }
    ]
    if change == "arguments":
        invocations[0].arguments = {"name": "another"}
    elif change == "result":
        invocations[0].result = {"ok": True, "margin": 100}
    elif change == "missing_receipt":
        args["ctx"].reference_mcp_read_receipts.clear()
    elif change == "failed":
        invocations[0].error = "read failed"
    elif change == "nested":
        invocations[0].nested = [invocations[-1]]
    checkpoint = ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **args)
    if change:
        assert checkpoint is None
        return
    assert checkpoint is not None and checkpoint.schema_version == 4
    checkpoint = ReferenceReadQuestion.model_validate_json(checkpoint.model_dump_json())
    assert checkpoint.mcp_receipts()[0]["call_id"] == "mcp_original"
    assert checkpoint.resume_messages(**args, answer="All teams")[:-1] == messages
    assert checkpoint.next_iteration == 2
    assert ReferenceReadQuestion.capture(messages=messages, invocations=invocations, model_turn_count=2, **{**args, "max_iterations": 2}) is None
    unknown = checkpoint.model_dump(mode="json")
    unknown["schema_version"] = 5
    with pytest.raises(ValueError):
        ReferenceReadQuestion.model_validate(unknown)
