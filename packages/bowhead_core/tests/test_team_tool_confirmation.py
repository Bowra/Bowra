"""Requester confirmation for team MCP tool calls (FEATURE_TEAM_TOOL_CONFIRMATION)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.openapi_tool_source import mcp_entry, openapi_entry, parse_openapi_ops
from bowhead_core.team_tool_confirmation import TeamToolConfirmations, team_mcp_category
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.tool_registry import ToolExecutionContext

from bowhead_core import team_tool_confirmation as confirmation_mod

_TEAM = "team_a"
_SRC = "ots_crm"


def test_category_follows_explicit_annotations_and_keeps_conflicts_unknown() -> None:
    assert team_mcp_category(read_only=True, destructive=False) == ("read", "mcp_annotation")
    assert team_mcp_category(read_only=False, destructive=True) == ("destructive", "mcp_annotation")
    assert team_mcp_category(read_only=False, destructive=False) == ("external_write", "mcp_annotation")
    assert team_mcp_category(read_only=True, destructive=True) == ("unknown", "unknown_default")
    assert team_mcp_category(read_only=None, destructive=None) == ("unknown", "unknown_default")
    assert team_mcp_category(read_only=None, destructive=False) == ("unknown", "unknown_default")


def _request(queue: TeamToolConfirmations, *, owner: str = "usr_a", session: str = "s_1", digest: str = "d1", **overrides):
    values = {
        "owner_user_id": owner,
        "turn_id": "t_1",
        "session_id": session,
        "team_id": _TEAM,
        "source_name": "CRM",
        "tool_ref": f"teammcp.{_SRC}.create_note",
        "tool_name": "create_note",
        "description": "Create a note",
        "schema_digest": digest,
        "arguments": {"text": "hello"},
    }
    values.update(overrides)
    return queue.request(**values)


async def _first_pending(queue: TeamToolConfirmations, owner: str = "usr_a") -> dict:
    for _ in range(100):
        items = queue.pending(owner)
        if items:
            return items[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending confirmation")


@pytest.mark.asyncio
async def test_requester_sees_exact_call_and_session_allowance_is_bound_to_the_schema() -> None:
    queue = TeamToolConfirmations(timeout_seconds=5)
    waiting = asyncio.create_task(_request(queue))
    item = await _first_pending(queue)
    assert item["arguments"] == {"text": "hello"}
    assert item["source_name"] == "CRM" and item["tool_name"] == "create_note"
    assert "schema_digest" not in item
    assert queue.pending("usr_b") == []
    assert queue.approve_for_session("usr_b", item["id"]) is False
    assert queue.approve_for_session("usr_a", item["id"]) is True
    assert await waiting == "approved"
    # The same tool definition in the same session no longer asks.
    assert await _request(queue) == "approved"
    assert queue.pending("usr_a") == []
    # A changed definition or another session asks again.
    changed = asyncio.create_task(_request(queue, digest="d2"))
    item = await _first_pending(queue)
    assert queue.decide("usr_a", item["id"], False) is True
    assert await changed == "rejected"
    other_session = asyncio.create_task(_request(queue, session="s_2"))
    item = await _first_pending(queue)
    assert queue.decide("usr_a", item["id"], True) is True
    assert await other_session == "approved"


@pytest.mark.asyncio
async def test_unanswered_and_unattributed_calls_do_not_run() -> None:
    queue = TeamToolConfirmations(timeout_seconds=0.05)
    assert await _request(queue) == "expired"
    assert await _request(queue, owner="") == "unavailable"
    assert queue.pending("usr_a") == []


def _ctx() -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="usr_a",
        turn_id="t_1",
        datarc=None,  # type: ignore[arg-type]
        team_id=_TEAM,
        session_id="s_1",
    )
    ctx.team_tool_source_labels = {f"teammcp.{_SRC}.create_note": "CRM"}
    return ctx


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content={"done": True}, content=[])


def _mcp(name: str, annotations: dict | None) -> object:
    meta = {"name": name, "description": f"{name} tool", "input_schema": {"type": "object"}}
    if annotations is not None:
        meta["annotations"] = annotations
        meta["destructive"] = bool(annotations.get("destructiveHint"))
    return mcp_entry(_SRC, meta)


def _entries() -> list:
    return [
        _mcp("lookup", {"readOnlyHint": True, "destructiveHint": False}),
        _mcp("create_note", {"readOnlyHint": False, "destructiveHint": False}),
        _mcp("delete_note", {"readOnlyHint": False, "destructiveHint": True}),
        _mcp("unlabeled", None),
    ]


def test_without_the_switch_team_specs_keep_their_current_approval_shape(monkeypatch) -> None:
    monkeypatch.delenv("FEATURE_TEAM_TOOL_CONFIRMATION", raising=False)
    specs = {spec.schema.name.rsplit(".", 1)[-1]: spec for spec in build_team_specs(_TEAM, _entries(), _Client())}
    assert specs["delete_note"].needs_approval is True
    assert specs["unlabeled"].needs_approval is False
    assert specs["unlabeled"].annotations.read_only is True


def test_with_the_switch_only_read_mcp_tools_skip_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    specs = {spec.schema.name.rsplit(".", 1)[-1]: spec for spec in build_team_specs(_TEAM, _entries(), _Client())}
    assert all(spec.needs_approval is False for spec in specs.values())
    assert specs["lookup"].annotations.read_only is True and specs["lookup"].risk_level == "low"
    for name in ("create_note", "delete_note", "unlabeled"):
        assert specs[name].annotations.read_only is False
        assert specs[name].risk_level == "medium"
    assert specs["delete_note"].annotations.destructive is True
    assert specs["create_note"].annotations.destructive is False


def test_openapi_team_tools_are_unchanged_by_the_switch(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/q": {"post": {"operationId": "runQuery", "responses": {"200": {"description": "ok"}}}}},
    }
    (write,) = build_team_specs(_TEAM, [openapi_entry("ots_api", op) for op in parse_openapi_ops(spec)], _Client())
    assert write.needs_approval is True


@pytest.mark.asyncio
async def test_write_call_runs_only_after_the_requester_approves_the_shown_arguments(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    queue = TeamToolConfirmations(timeout_seconds=5)
    monkeypatch.setattr(confirmation_mod, "_default", queue)
    client = _Client()
    specs = {spec.schema.name.rsplit(".", 1)[-1]: spec for spec in build_team_specs(_TEAM, _entries(), client)}

    assert (await specs["lookup"].handler({"id": 1}, _ctx()))["ok"] is True
    assert client.calls == [("lookup", {"id": 1})]

    args = {"text": "hello"}
    running = asyncio.create_task(specs["create_note"].handler(args, _ctx()))
    item = await _first_pending(queue)
    assert item["source_name"] == "CRM" and item["tool_ref"] == f"teammcp.{_SRC}.create_note"
    args["text"] = "changed after review"
    assert queue.decide("usr_a", item["id"], True)
    result = await running
    assert result == {"ok": True, "data": {"done": True}}
    assert client.calls[-1] == ("create_note", {"text": "hello"})


@pytest.mark.asyncio
async def test_rejected_or_expired_write_calls_never_reach_the_server(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    queue = TeamToolConfirmations(timeout_seconds=5)
    monkeypatch.setattr(confirmation_mod, "_default", queue)
    client = _Client()
    specs = {spec.schema.name.rsplit(".", 1)[-1]: spec for spec in build_team_specs(_TEAM, _entries(), client)}

    running = asyncio.create_task(specs["delete_note"].handler({"id": 7}, _ctx()))
    item = await _first_pending(queue)
    assert queue.decide("usr_a", item["id"], False)
    rejected = await running
    assert rejected["ok"] is False and rejected["error_kind"] == "team_tool_confirmation_rejected"

    monkeypatch.setattr(confirmation_mod, "_default", TeamToolConfirmations(timeout_seconds=0.05))
    expired = await specs["unlabeled"].handler({}, _ctx())
    assert expired["error_kind"] == "team_tool_confirmation_expired"
    assert client.calls == []
