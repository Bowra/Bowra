"""Current-user lookup uses server identity and exposes only minimal account data."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bowhead_core.current_user_tool import register_current_user_tool
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.user_store import User

pytestmark = pytest.mark.unit


def _context(user_id: str = "usr_requester", role: str = "lead") -> ToolExecutionContext:
    return ToolExecutionContext(workspace_id="ws_1", member_uid="m_1", user_id=user_id, turn_id="t_1", datarc=None, role=role)


async def test_reads_authenticated_account_without_exposing_secrets() -> None:
    user = User(id="usr_requester", team_id="team_1", email="me@example.com", display_name="Chris", email_verified=True, password_hash="secret")
    store = AsyncMock()
    store.get_user.return_value = user
    registry = ToolRegistry()
    register_current_user_tool(registry, store_provider=lambda: store)
    ctx = _context()
    ctx.user_profile = {"display_name": "Forged client name", "email": "other@example.com"}
    invocation = await registry.execute("get_current_user", {}, ctx)
    assert invocation.error is None
    assert invocation.result == {"ok": True, "display_name": "Chris", "email": "me@example.com", "email_verified": True}
    store.get_user.assert_awaited_once_with("usr_requester")
    assert registry.get("get_current_user").effect_spec == TOOL_EFFECT_READ_ONLY


async def test_cannot_select_another_account() -> None:
    store = AsyncMock()
    registry = ToolRegistry()
    register_current_user_tool(registry, store_provider=lambda: store)
    result = await registry.execute("get_current_user", {"user_id": "usr_other"}, _context())
    assert result.result["ok"] is False
    store.get_user.assert_not_awaited()


@pytest.mark.parametrize("state", ["missing_identity", "missing_store", "missing_user", "inactive", "storage_failure"])
async def test_unavailable_account_does_not_invent_details(state: str) -> None:
    store = AsyncMock()
    store.get_user.return_value = (
        None if state == "missing_user" else User(id="usr_requester", team_id="team_1", email="me@example.com", status="inactive")
    )
    if state == "storage_failure":
        store.get_user.side_effect = RuntimeError("private storage diagnostics")
    registry = ToolRegistry()
    register_current_user_tool(registry, store_provider=lambda: None if state == "missing_store" else store)
    result = await registry.execute("get_current_user", {}, _context("" if state == "missing_identity" else "usr_requester"))
    assert result.result["ok"] is False
    assert "Ask the user" in result.result["message"]
    assert "email" not in result.result


async def test_missing_email_and_unverified_status_remain_explicit() -> None:
    store = AsyncMock()
    store.get_user.return_value = User(id="usr_requester", team_id="team_1", email="")
    registry = ToolRegistry()
    register_current_user_tool(registry, store_provider=lambda: store)
    result = await registry.execute("get_current_user", {}, _context())
    assert result.result == {"ok": True, "display_name": None, "email": None, "email_verified": False}


async def test_specialist_cannot_read_account_profile() -> None:
    store = AsyncMock()
    registry = ToolRegistry()
    register_current_user_tool(registry, store_provider=lambda: store)
    result = await registry.execute("get_current_user", {}, _context(role="analyst"))
    assert result.error
    store.get_user.assert_not_awaited()
