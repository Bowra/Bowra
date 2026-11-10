"""Project trusted private runtime notes into model-facing instructions."""

from __future__ import annotations

import json
from dataclasses import replace

from bowhead_router.types import Message

OUTPUT_RECOVERY_STATE_NAME = "bowhead_output_recovery_v1"
REJECTED_TOOL_SYNTAX_HISTORY_NAME = "bowhead_rejected_tool_syntax_v1"
REJECTED_TOOL_SYNTAX_INSTRUCTION = (
    "The previous assistant response was rejected: the required Tool action was not executed. "
    "Its text is omitted from model context to avoid repeating an invalid call format. "
    "Keep earlier completed work and follow the runtime correction."
)
# A server-authored correction of the previous final answer. Providers only accept
# user-role turns here, so the projection states that it is not the user's reply:
# a question that answer asked remains unanswered.
RUNTIME_CORRECTION_NOTE_NAME = "bowhead_runtime_correction_v1"
RUNTIME_CORRECTION_PREFIX = (
    "[Bowhead runtime note — not a message from the user. The user has not replied since your previous answer; "
    "any question or choice you asked them is still unanswered.]\n"
)


def model_messages(messages: list[Message]) -> list[Message]:
    result = []
    for message in messages:
        if message.role == "assistant" and message.name == REJECTED_TOOL_SYNTAX_HISTORY_NAME and not message.tool_calls:
            result.append(replace(message, name=None, content=REJECTED_TOOL_SYNTAX_INSTRUCTION))
        elif message.role == "user" and message.name == RUNTIME_CORRECTION_NOTE_NAME:
            result.append(replace(message, name=None, content=RUNTIME_CORRECTION_PREFIX + message.content))
        elif message.role == "user" and message.name == OUTPUT_RECOVERY_STATE_NAME:
            instruction = json.loads(message.content)["instruction"]
            if not isinstance(instruction, str):
                raise ValueError("Invalid runtime recovery instruction")
            result.append(replace(message, name=None, content=instruction))
        else:
            result.append(message)
    return result
