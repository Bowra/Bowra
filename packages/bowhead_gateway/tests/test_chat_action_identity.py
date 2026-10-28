from __future__ import annotations

import pytest
from bowhead_core.terminal_protocol import WEB_ACTION_MAX_EXECUTION_GENERATION
from bowhead_gateway.chat_action_identity import (
    parse_chat_action_client_info,
    parse_chat_action_id,
)

START_SUFFIX = "0123456789abcdef0123456789abcdef"
START_ACTION_ID = f"act_start_{START_SUFFIX}"
STEER_SUFFIX = "fedcba9876543210fedcba9876543210"
STEER_ACTION_ID = f"act_steer_{STEER_SUFFIX}"


def test_start_action_derives_canonical_home_identities() -> None:
    parsed = parse_chat_action_client_info(
        {
            "action_kind": "start",
            "composer_surface": "home",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
        session_id=None,
    )

    assert parsed is not None
    assert parsed.action.action_id == START_ACTION_ID
    assert parsed.action.kind == "start"
    assert parsed.action.suffix == START_SUFFIX
    assert parsed.action.home_session_id == f"s_{START_SUFFIX}"
    assert parsed.action.start_turn_id == f"t_{START_SUFFIX}"
    assert parsed.action.user_message_id == f"u_s_{START_SUFFIX}"
    assert parsed.session_id is None
    assert parsed.expected_turn_id is None
    assert parsed.expected_execution_generation is None


def test_steer_action_preserves_exact_target_and_derives_message_identity() -> None:
    parsed = parse_chat_action_client_info(
        {
            "action_kind": "steer",
            "composer_surface": "workbench",
            "expected_execution_generation": "42",
            "expected_turn_id": "t_target",
            "request_id": STEER_ACTION_ID,
            "user_msg_id": f"u_r_{STEER_SUFFIX}",
        },
        session_id="s_target",
    )

    assert parsed is not None
    assert parsed.action.action_id == STEER_ACTION_ID
    assert parsed.action.kind == "steer"
    assert parsed.action.suffix == STEER_SUFFIX
    assert parsed.action.user_message_id == f"u_r_{STEER_SUFFIX}"
    assert parsed.session_id == "s_target"
    assert parsed.expected_turn_id == "t_target"
    assert parsed.expected_execution_generation == 42


@pytest.mark.parametrize(
    "action_id",
    (
        f"act_start_{START_SUFFIX.upper()}",
        f"act_start_{START_SUFFIX[:-1]}",
        f"act_start_{START_SUFFIX}0",
        f"act_replay_{START_SUFFIX}",
        f"act_START_{START_SUFFIX}",
    ),
)
def test_action_id_rejects_noncanonical_case_length_and_kind(action_id: str) -> None:
    with pytest.raises(ValueError, match="chat action id is invalid"):
        parse_chat_action_id(action_id)


def test_client_declared_kind_must_match_action_id() -> None:
    with pytest.raises(ValueError, match="kind does not match"):
        parse_chat_action_client_info(
            {
                "action_kind": "steer",
                "expected_execution_generation": "1",
                "expected_turn_id": "t_target",
                "request_id": START_ACTION_ID,
            },
            session_id="s_target",
        )


def test_action_client_info_rejects_extra_fields() -> None:
    with pytest.raises(ValueError, match="unsupported fields"):
        parse_chat_action_client_info(
            {
                "action_kind": "start",
                "message_id": "legacy-message-id",
                "request_id": START_ACTION_ID,
            },
            session_id=None,
        )


def test_action_client_info_rejects_unknown_composer_surface() -> None:
    with pytest.raises(ValueError, match="composer surface is invalid"):
        parse_chat_action_client_info(
            {
                "action_kind": "start",
                "composer_surface": "unknown",
                "request_id": START_ACTION_ID,
            },
            session_id=None,
        )


@pytest.mark.parametrize(
    ("action_id", "user_message_id"),
    (
        (START_ACTION_ID, f"u_r_{START_SUFFIX}"),
        (STEER_ACTION_ID, f"u_s_{STEER_SUFFIX}"),
    ),
)
def test_action_rejects_noncanonical_user_message_identity(
    action_id: str,
    user_message_id: str,
) -> None:
    kind = "start" if action_id == START_ACTION_ID else "steer"
    client_info = {
        "action_kind": kind,
        "request_id": action_id,
        "user_msg_id": user_message_id,
    }
    if kind == "steer":
        client_info.update(
            {
                "expected_execution_generation": "1",
                "expected_turn_id": "t_target",
            }
        )

    with pytest.raises(ValueError, match="user message id does not match"):
        parse_chat_action_client_info(
            client_info,
            session_id="s_target" if kind == "steer" else None,
        )


@pytest.mark.parametrize("generation", ("0", "01"))
def test_steer_action_rejects_noncanonical_generation(generation: str) -> None:
    with pytest.raises(ValueError, match="generation is invalid"):
        parse_chat_action_client_info(
            {
                "action_kind": "steer",
                "expected_execution_generation": generation,
                "expected_turn_id": "t_target",
                "request_id": STEER_ACTION_ID,
            },
            session_id="s_target",
        )


def test_steer_action_accepts_js_safe_generation_maximum() -> None:
    parsed = parse_chat_action_client_info(
        {
            "action_kind": "steer",
            "expected_execution_generation": str(WEB_ACTION_MAX_EXECUTION_GENERATION),
            "expected_turn_id": "t_target",
            "request_id": STEER_ACTION_ID,
        },
        session_id="s_target",
    )

    assert parsed is not None
    assert parsed.expected_execution_generation == WEB_ACTION_MAX_EXECUTION_GENERATION


def test_steer_action_rejects_generation_above_js_safe_maximum() -> None:
    with pytest.raises(ValueError, match="generation is invalid"):
        parse_chat_action_client_info(
            {
                "action_kind": "steer",
                "expected_execution_generation": str(WEB_ACTION_MAX_EXECUTION_GENERATION + 1),
                "expected_turn_id": "t_target",
                "request_id": STEER_ACTION_ID,
            },
            session_id="s_target",
        )


def test_start_action_rejects_steer_only_fields() -> None:
    with pytest.raises(ValueError, match="steer-only identity"):
        parse_chat_action_client_info(
            {
                "action_kind": "start",
                "expected_execution_generation": "1",
                "expected_turn_id": "t_target",
                "request_id": START_ACTION_ID,
            },
            session_id=None,
        )


def test_steer_identity_rejects_start_only_derived_properties() -> None:
    action = parse_chat_action_id(STEER_ACTION_ID)

    with pytest.raises(ValueError, match="only start actions derive a Home Session"):
        _ = action.home_session_id
    with pytest.raises(ValueError, match="only start actions derive a Turn"):
        _ = action.start_turn_id


def test_legacy_client_info_is_not_claimed_as_an_action() -> None:
    assert (
        parse_chat_action_client_info(
            {"request_id": "legacy-request-id"},
            session_id="s_existing",
        )
        is None
    )
