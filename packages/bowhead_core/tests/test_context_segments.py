"""Encrypted context chunks preserve messages without widening checkpoint limits."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.context_segments import CONTEXT_SEGMENT_BYTES, ContextSegmentError, ContextSnapshot, open_context_messages, seal_context_messages
from bowhead_router.types import ImageRef, Message, ToolCall
from cryptography.fernet import Fernet

pytestmark = pytest.mark.unit


def scope(session="session_one"):
    return ReferenceScope(team_id="team_one", workspace_id="ws_one", user_id="user_one", session_id=session, policy_epoch=0)


def fixture():
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    messages = [
        Message(role="user", content="Keep invoice_001 and EUR 12.50 exactly."),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="read_1", name="read", arguments={"file": "invoice_001"})]),
        Message(role="tool", name="read", tool_call_id="read_1", content="é汉" * 200_000),
        Message(role="assistant", content="Confirmed EUR 12.50."),
        Message(role="user", content="Inspect this image.", images=[ImageRef(base64="aGVsbG8=", mime="image/png")]),
    ]
    snapshot, parts = seal_context_messages(messages, cipher=cipher, scope=scope(), owner="execution_one:ordinary")
    return cipher, messages, snapshot, {part.reference.digest: part.ciphertext for part in parts}


def test_large_unicode_tool_projection_round_trips_without_splitting_protocol_records():
    cipher, messages, snapshot, parts = fixture()
    assert snapshot.byte_count > CONTEXT_SEGMENT_BYTES
    assert len(snapshot.segments) > 1
    assert len(snapshot.model_dump_json().encode()) < 2048
    # No source text or image payload appears in the compact checkpoint reference.
    assert "invoice_001" not in snapshot.model_dump_json()
    assert all("invoice_001" not in body for body in parts.values())
    decoded = ContextSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = open_context_messages(decoded, parts, cipher=cipher, scope=scope(), owner="execution_one:ordinary")
    assert [asdict(message) for message in restored] == [asdict(message) for message in messages]


@pytest.mark.parametrize(
    "owner,session",
    [("execution_two:ordinary", "session_one"), ("execution_one:coordination", "session_one"), ("execution_one:ordinary", "session_two")],
)
def test_context_cannot_be_opened_as_another_execution_lane_or_session(owner, session):
    cipher, _, snapshot, parts = fixture()
    with pytest.raises(ContextSegmentError, match="unavailable"):
        open_context_messages(snapshot, parts, cipher=cipher, scope=scope(session), owner=owner)


def test_missing_or_substituted_segment_is_unavailable_without_partial_projection():
    cipher, _, snapshot, parts = fixture()
    first, second = snapshot.segments[:2]
    missing = {key: value for key, value in parts.items() if key != first.digest}
    with pytest.raises(ContextSegmentError, match="unavailable"):
        open_context_messages(snapshot, missing, cipher=cipher, scope=scope(), owner="execution_one:ordinary")
    substituted = {**parts, first.digest: parts[second.digest]}
    with pytest.raises(ContextSegmentError, match="unavailable"):
        open_context_messages(snapshot, substituted, cipher=cipher, scope=scope(), owner="execution_one:ordinary")


def test_reordered_segments_cannot_change_the_transcript():
    cipher, _, snapshot, parts = fixture()
    changed = snapshot.model_copy(update={"segments": tuple(reversed(snapshot.segments))})
    with pytest.raises(ContextSegmentError, match="unavailable"):
        open_context_messages(changed, parts, cipher=cipher, scope=scope(), owner="execution_one:ordinary")


def test_unsupported_snapshot_version_does_not_enter_the_reader():
    from pydantic import ValidationError

    _, _, snapshot, _ = fixture()
    with pytest.raises(ValidationError):
        ContextSnapshot.model_validate_json(snapshot.model_dump_json().replace('"version":1', '"version":2'))
