"""Canonical Web Chat action identities for Terminal admission replay."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from bowhead_core.terminal_protocol import WEB_ACTION_MAX_EXECUTION_GENERATION

ChatActionKind = Literal["start", "steer"]

_ACTION_ID_RE = re.compile(r"\Aact_(start|steer)_([0-9a-f]{32})\Z")
_POSITIVE_DECIMAL_RE = re.compile(r"\A[1-9][0-9]{0,18}\Z")
_ACTION_CLIENT_INFO_KEYS = frozenset(
    {
        "action_kind",
        "agent_draft_id",
        "agent_draft_version",
        "agent_draft_dirty",
        "composer_surface",
        "expected_execution_generation",
        "expected_turn_id",
        "request_id",
        "user_msg_id",
    }
)


@dataclass(frozen=True, slots=True)
class ChatActionIdentity:
    action_id: str
    kind: ChatActionKind
    suffix: str

    @property
    def home_session_id(self) -> str:
        if self.kind != "start":
            raise ValueError("only start actions derive a Home Session")
        return f"s_{self.suffix}"

    @property
    def start_turn_id(self) -> str:
        if self.kind != "start":
            raise ValueError("only start actions derive a Turn")
        return f"t_{self.suffix}"

    @property
    def user_message_id(self) -> str:
        prefix = "u_s" if self.kind == "start" else "u_r"
        return f"{prefix}_{self.suffix}"


@dataclass(frozen=True, slots=True)
class ChatActionRequestIdentity:
    action: ChatActionIdentity
    session_id: str | None = None
    expected_turn_id: str | None = None
    expected_execution_generation: int | None = None


def parse_chat_action_id(
    action_id: str,
    *,
    expected_kind: ChatActionKind | None = None,
) -> ChatActionIdentity:
    if not isinstance(action_id, str):
        raise ValueError("chat action id must be a string")
    matched = _ACTION_ID_RE.fullmatch(action_id)
    if matched is None:
        raise ValueError("chat action id is invalid")
    kind = cast(ChatActionKind, matched.group(1))
    if expected_kind is not None and kind != expected_kind:
        raise ValueError("chat action kind does not match its id")
    return ChatActionIdentity(
        action_id=action_id,
        kind=kind,
        suffix=matched.group(2),
    )


def parse_chat_action_client_info(
    client_info: Mapping[str, str],
    *,
    session_id: str | None,
) -> ChatActionRequestIdentity | None:
    """Parse the optional action-aware request without claiming legacy IDs."""

    request_id = client_info.get("request_id")
    raw_kind = client_info.get("action_kind")
    action_aware = raw_kind is not None or (isinstance(request_id, str) and request_id.startswith("act_"))
    if not action_aware:
        return None
    if set(client_info) - _ACTION_CLIENT_INFO_KEYS:
        raise ValueError("chat action client info contains unsupported fields")
    composer_surface = client_info.get("composer_surface")
    if composer_surface is not None and composer_surface not in {"home", "workbench"}:
        raise ValueError("chat action composer surface is invalid")
    if raw_kind not in {"start", "steer"} or request_id is None:
        raise ValueError("chat action identity is incomplete")
    action = parse_chat_action_id(
        request_id,
        expected_kind=cast(ChatActionKind, raw_kind),
    )
    supplied_user_message_id = client_info.get("user_msg_id")
    if supplied_user_message_id is not None and supplied_user_message_id != action.user_message_id:
        raise ValueError("chat action user message id does not match")

    expected_turn_id = client_info.get("expected_turn_id")
    expected_generation_text = client_info.get("expected_execution_generation")
    if action.kind == "start":
        if session_id is not None and (not session_id or len(session_id) > 128):
            raise ValueError("start action Session identity is invalid")
        if expected_turn_id is not None or expected_generation_text is not None:
            raise ValueError("start action contains steer-only identity")
        return ChatActionRequestIdentity(action=action, session_id=session_id)

    if any(key.startswith("agent_draft_") for key in client_info):
        raise ValueError("draft targets require a start action")
    if not session_id or not expected_turn_id or expected_generation_text is None:
        raise ValueError("steer action identity is incomplete")
    if len(session_id) > 128 or len(expected_turn_id) > 128:
        raise ValueError("steer action target identity is too long")
    if _POSITIVE_DECIMAL_RE.fullmatch(expected_generation_text) is None:
        raise ValueError("steer action generation is invalid")
    generation = int(expected_generation_text)
    if generation > WEB_ACTION_MAX_EXECUTION_GENERATION:
        raise ValueError("steer action generation is invalid")
    return ChatActionRequestIdentity(
        action=action,
        session_id=session_id,
        expected_turn_id=expected_turn_id,
        expected_execution_generation=generation,
    )
