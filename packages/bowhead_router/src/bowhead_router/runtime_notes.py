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
# Opens a correction that rejects a final answer. The Runner withdraws that answer from the
# user's view (Lead) or never returns it (helper), and context hygiene drops it from later
# Turns; without this fact models pointed the user at "my previous reply" nobody could see.
WITHDRAWN_ANSWER_FACT = (
    "Your previous answer was not delivered: the user does not see it, and the answer you write next replaces it in full, "
    "including any question it asked.\n"
)

# Visual self-check: renders of saved files for the next request only (never persisted).
# The framing keeps a user-role render from reading as a user instruction.
VISUAL_VIEW_NOTE_NAME = "bowhead_visual_view_v1"
RUNTIME_NOTE_PREFIX = "[Bowhead runtime note — not a message from the user.]\n"
# After a helper's result: the final reply waits for the delivery review, with its evidence IDs (Lead only,
# this Turn only). docs/sprints/answer-written-twice-2026-11-25.
FINAL_REPLY_REVIEW_NOTE_NAME = "bowhead_final_reply_review_v1"
# After a save: the run's result files and work files so far (this Turn only). See bowhead_core.run_files.
RUN_FILES_NOTE_NAME = "bowhead_run_files_v1"


def model_messages(messages: list[Message]) -> list[Message]:
    result = []
    for message in messages:
        if message.role == "assistant" and message.name == REJECTED_TOOL_SYNTAX_HISTORY_NAME and not message.tool_calls:
            result.append(replace(message, name=None, content=REJECTED_TOOL_SYNTAX_INSTRUCTION))
        elif message.role == "user" and message.name == RUNTIME_CORRECTION_NOTE_NAME:
            result.append(replace(message, name=None, content=RUNTIME_CORRECTION_PREFIX + message.content))
        elif message.role == "user" and message.name in {VISUAL_VIEW_NOTE_NAME, FINAL_REPLY_REVIEW_NOTE_NAME, RUN_FILES_NOTE_NAME}:
            result.append(replace(message, name=None, content=RUNTIME_NOTE_PREFIX + message.content))
        elif message.role == "user" and message.name == OUTPUT_RECOVERY_STATE_NAME:
            instruction = json.loads(message.content)["instruction"]
            if not isinstance(instruction, str):
                raise ValueError("Invalid runtime recovery instruction")
            result.append(replace(message, name=None, content=instruction))
        else:
            result.append(message)
    return result
