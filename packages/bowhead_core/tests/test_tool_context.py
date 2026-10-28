"""Tests for ToolExecutionContext + TurnIdentity (#104)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest
from bowhead_core.tool_registry import ToolExecutionContext, TurnIdentity


def test_identity_immutable_and_tags() -> None:
    """TurnIdentity is immutable and as_log_tags returns structured log tags."""

    ident = TurnIdentity(
        workspace_id="w1",
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        session_id="s1",
    )
    tags = ident.as_log_tags()
    assert tags == {"workspace_id": "w1", "turn_id": "t1", "session_id": "s1"}

    with pytest.raises(FrozenInstanceError):
        ident.workspace_id = "w2"  # type: ignore[misc]


def test_ctx_identity_property_matches_flat_fields() -> None:
    """ctx.identity fields match flat ctx fields."""

    ctx = ToolExecutionContext(
        workspace_id="w1",
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        session_id="s1",
        datarc=MagicMock(),
    )
    assert ctx.identity.workspace_id == ctx.workspace_id
    assert ctx.identity.member_uid == ctx.member_uid
    assert ctx.identity.turn_id == ctx.turn_id
    assert ctx.identity.session_id == ctx.session_id


def test_ctx_resolve_datarc_project_prefers_active_project() -> None:
    """BI tools use active_datarc_project while KB/canvas keep workspace_id."""

    ctx = ToolExecutionContext(
        workspace_id="ws_123",
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        session_id="s1",
        datarc=MagicMock(),
        active_datarc_project="datarc_ws_456",
    )

    assert ctx.workspace_id == "ws_123"
    assert ctx.resolve_datarc_project() == "datarc_ws_456"

    legacy_ctx = ToolExecutionContext(
        workspace_id="legacy_ws",
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        datarc=MagicMock(),
    )
    # Batch 1/c3: a non-canonical workspace_id no longer stands in as a datarc workspace.
    assert legacy_ctx.resolve_datarc_project() == ""


def test_ctx_identity_each_read_is_fresh_instance() -> None:
    """Each identity property read returns an equivalent value."""

    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=MagicMock(),
    )
    a = ctx.identity
    b = ctx.identity
    assert a == b
