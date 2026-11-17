"""History hygiene through the real publication, completion and next-Turn load."""

import json
from dataclasses import replace

import pytest
from bowhead_core.context_history import load_conversation_context
from bowhead_core.context_hygiene import is_cleared_result
from bowhead_core.session_store import SessionStore
from bowhead_router.runtime_notes import RUNTIME_CORRECTION_NOTE_NAME
from bowhead_router.types import Message, ToolCall
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collaboration_fixture
from test_context_execution import owner, prepare
from test_context_history import completion, next_context
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


def exchange(identity: str, content: str) -> list[Message]:
    return [
        Message(role="assistant", content="", tool_calls=[ToolCall(id=identity, name="read_source", arguments={})]),
        Message(role="tool", name="read_source", tool_call_id=identity, content=content),
    ]


def body(label: str) -> str:
    return json.dumps({"ok": True, "artifact_id": f"art_{label}", "text": f"{label} detail " * 250})


async def publish_and_reload(collab, context_case, monkeypatch, request, closeout=(), before_reload=None):
    from bowhead_core.chat_reference_storage import ReferenceCipher

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key)
    collab.store.cipher = ReferenceCipher(key)
    execution = owner(collab, execution_id=collab.lead)
    result = await prepare(execution, context_case, request=request)
    assert result.candidate is None  # No summary: the published view is the transcript itself.
    publication = await execution.conversation_publication([*request.messages, *closeout])
    store = SessionStore(collab.path)
    assert await store.mark_completed(**completion(collab, publication))
    await store.finalize_turn(session_id=collab.worker.session_id, current_turn_id=collab.worker.turn_id, next_turn_id=None)
    if before_reload is not None:
        before_reload()
    return await load_conversation_context(await next_context(collab))


def three_turns(context_case):
    messages = [
        Message(role="user", content="First request."),
        *exchange("a1", body("first")),
        Message(role="assistant", content="First answer."),
        Message(role="user", content="Second request."),
        *exchange("a2", body("second")),
        Message(role="assistant", content="Second answer."),
        Message(role="user", content="Third request."),
        *exchange("c1", body("third")),
    ]
    return replace(context_case.request, messages=messages)


CLOSEOUT = (
    Message(role="assistant", content="Done. Shall I continue?"),
    Message(role="user", name=RUNTIME_CORRECTION_NOTE_NAME, content="Review the delivery before finishing."),
    Message(role="assistant", content="", tool_calls=[ToolCall(id="r1", name="review_delivery", arguments={"payload": {}})]),
    Message(role="tool", name="review_delivery", tool_call_id="r1", content=json.dumps({"ok": True, "reviewed_completion": "complete"})),
)


async def test_the_next_turn_reads_the_conversation_without_turn_local_protocol(collab, context_case, monkeypatch):
    monkeypatch.setenv("FEATURE_CONTEXT_HISTORY_HYGIENE", "1")
    request = three_turns(context_case)
    restored = await publish_and_reload(collab, context_case, monkeypatch, request, CLOSEOUT)
    assert restored.messages[:-1] == request.messages
    assert restored.messages[-1] == Message(role="assistant", content="Verified final answer.")


async def test_old_results_become_stubs_once_a_batch_pays_and_recent_turns_stay_verbatim(collab, context_case, monkeypatch):
    monkeypatch.setenv("FEATURE_CONTEXT_HISTORY_HYGIENE", "1")
    monkeypatch.setenv("BOWHEAD_CTX_HYGIENE_MIN_SAVING_TOKENS", "100")
    request = three_turns(context_case)
    restored = await publish_and_reload(collab, context_case, monkeypatch, request)
    results = {message.tool_call_id: message.content for message in restored.messages if message.role == "tool"}
    assert is_cleared_result(results["a1"])
    stub = json.loads(results["a1"])
    assert stub["tool"] == "read_source" and stub["kept"]["artifact_id"] == "art_first" and stub["original_chars"] == len(body("first"))
    assert results["a2"] == body("second") and results["c1"] == body("third")
    assert [m for m in restored.messages[:-1] if m.role != "tool"] == [m for m in request.messages if m.role != "tool"]


async def test_a_cleaned_history_is_read_as_stored_after_the_switch_is_turned_off(collab, context_case, monkeypatch):
    """Rollback: stubs and omissions already published stay as they are; nothing needs repair."""
    monkeypatch.setenv("FEATURE_CONTEXT_HISTORY_HYGIENE", "1")
    monkeypatch.setenv("BOWHEAD_CTX_HYGIENE_MIN_SAVING_TOKENS", "100")
    request = three_turns(context_case)
    restored = await publish_and_reload(
        collab, context_case, monkeypatch, request, CLOSEOUT, before_reload=lambda: monkeypatch.setenv("FEATURE_CONTEXT_HISTORY_HYGIENE", "0")
    )
    results = {message.tool_call_id: message.content for message in restored.messages if message.role == "tool"}
    assert is_cleared_result(results["a1"]) and results["a2"] == body("second") and results["c1"] == body("third")
    assert all(message.name != RUNTIME_CORRECTION_NOTE_NAME for message in restored.messages)
    assert restored.messages[-1] == Message(role="assistant", content="Verified final answer.")


async def test_publication_is_unchanged_while_the_switch_is_off(collab, context_case, monkeypatch):
    monkeypatch.setenv("FEATURE_CONTEXT_HISTORY_HYGIENE", "0")
    monkeypatch.setenv("BOWHEAD_CTX_HYGIENE_MIN_SAVING_TOKENS", "100")
    request = three_turns(context_case)
    restored = await publish_and_reload(collab, context_case, monkeypatch, request, CLOSEOUT)
    assert restored.messages[:-1] == [*request.messages, *CLOSEOUT]
