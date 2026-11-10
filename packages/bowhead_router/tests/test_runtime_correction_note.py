"""A server-authored final-answer correction is never presented as the user's reply."""

import pytest
from bowhead_router.runtime_notes import RUNTIME_CORRECTION_NOTE_NAME, RUNTIME_CORRECTION_PREFIX, model_messages
from bowhead_router.types import Message

pytestmark = pytest.mark.unit


def test_correction_note_is_framed_and_ordinary_user_text_is_unchanged() -> None:
    history = [
        Message(role="user", content="Make the chart green."),
        Message(role="assistant", content="Should I add a green copy instead?"),
        Message(role="user", name=RUNTIME_CORRECTION_NOTE_NAME, content="Call review_delivery alone."),
    ]
    projected = model_messages(history)
    assert projected[0] == history[0]
    assert projected[2].role == "user" and projected[2].name is None
    assert projected[2].content == RUNTIME_CORRECTION_PREFIX + "Call review_delivery alone."
    assert "not a message from the user" in projected[2].content
