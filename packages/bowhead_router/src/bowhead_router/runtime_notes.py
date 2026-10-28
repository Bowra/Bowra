"""Project trusted private runtime notes into model-facing instructions."""

from __future__ import annotations

import json
from dataclasses import replace

from bowhead_router.types import Message

OUTPUT_RECOVERY_STATE_NAME = "bowhead_output_recovery_v1"


def model_messages(messages: list[Message]) -> list[Message]:
    result = []
    for message in messages:
        if message.role == "user" and message.name == OUTPUT_RECOVERY_STATE_NAME:
            instruction = json.loads(message.content)["instruction"]
            if not isinstance(instruction, str):
                raise ValueError("Invalid runtime recovery instruction")
            result.append(replace(message, name=None, content=instruction))
        else:
            result.append(message)
    return result
