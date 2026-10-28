from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.builtin_datarc_tool import build_builtin_datarc_specs, datarc_tool_ref
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema


class _Client:
    closed = 0

    def __init__(self, base_url: str, auth_key: str, **_: Any) -> None:
        assert base_url == "https://datarc.example"
        assert auth_key == "secret"

    async def fetch_account(self, email: str) -> dict[str, str]:
        assert email == "member@example.com"
        return {"uid": "account_1"}

    async def list_workspaces(self):
        return [SimpleNamespace(id="1", name="财务"), SimpleNamespace(id="2", name="销售")]

    async def list_account_projects(self, account_uid: str, *, offset: int, limit: int):
        assert account_uid == "account_1" and offset == 0 and limit == 100
        return [{"id": 2, "name": "销售"}]

    async def fetch_workspace_member(self, workspace_id: str, email: str):
        assert (workspace_id, email) == ("2", "member@example.com")
        return {"uid": "member_2"}

    async def aclose(self) -> None:
        type(self).closed += 1


class _Store:
    calls: list[dict[str, Any]]

    def __init__(self) -> None:
        self.calls = []

    async def get_user(self, user_id: str):
        return SimpleNamespace(email="member@example.com") if user_id == "user_1" else None

    async def user_has_active_team_membership(self, user_id: str, team_id: str) -> bool:
        return (user_id, team_id) == ("user_1", "team_1")

    async def get_team_builtin_tool_instance_auth_decrypted(self, team_id: str, tool_id: str):
        assert (team_id, tool_id) == ("team_1", "ibt_1")
        return {"connection_key": "secret"}

    async def get_team_builtin_tool_instance(self, team_id: str, tool_id: str):
        assert (team_id, tool_id) == ("team_1", "ibt_1")
        return SimpleNamespace(
            id="ibt_1",
            provider="datarc",
            display_name="销售数据",
            endpoint="https://datarc.example",
            status="enabled",
            scope_state="verified",
            scope={"permissions": [], "workspaces": [{"id": "1", "name": "财务"}, {"id": "2", "name": "销售"}]},
        )

    async def log_tool_call(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.anyio
async def test_builtin_datarc_tool_binds_instance_identity_and_workspace(monkeypatch) -> None:
    import bowhead_core.builtin_datarc_tool as module

    monkeypatch.setattr(module, "DatarcClient", _Client)
    captured: dict[str, Any] = {}

    async def _bi_query(args: dict[str, Any], ctx: ToolExecutionContext):
        captured.update(args=args, member_uid=ctx.member_uid, workspace=ctx.active_datarc_project)
        return {"rows": [{"营业额": 10}]}

    base = ToolRegistry()
    base.register(ToolSpec(schema=ToolSchema(name="bi_query", description="query", input_schema={"type": "object"}), handler=_bi_query))
    base.register(ToolSpec(schema=ToolSchema(name="bi_schema", description="schema", input_schema={"type": "object"}), handler=_bi_query))
    store = _Store()
    tool = SimpleNamespace(
        id="ibt_1",
        provider="datarc",
        display_name="销售数据",
        endpoint="https://datarc.example",
        status="enabled",
        scope_state="verified",
        scope={"permissions": [], "workspaces": [{"id": "1", "name": "财务"}, {"id": "2", "name": "销售"}]},
    )
    specs = build_builtin_datarc_specs(team_id="team_1", tools=[tool], store=store, base_registry=base)
    spec = next(item for item in specs if item.schema.name == datarc_tool_ref("ibt_1"))
    ctx = ToolExecutionContext(workspace_id="ws_local", member_uid="", user_id="user_1", turn_id="turn_1", datarc=object(), team_id="team_1")

    result = await spec.handler({"question": "今年销售额"}, ctx)

    assert spec.schema.name == datarc_tool_ref("ibt_1")
    assert captured == {"args": {"queries": ["今年销售额"]}, "member_uid": "member_2", "workspace": "2"}
    assert result["connection"] == "Datarc - 销售数据"
    assert result["workspace"] == {"id": "2", "name": "销售"}
    assert store.calls[0]["source_id"] == "ibt_1"
    assert store.calls[0]["status"] == "ok"
    assert _Client.closed == 1


@pytest.mark.anyio
async def test_builtin_datarc_tool_fails_closed_across_team(monkeypatch) -> None:
    import bowhead_core.builtin_datarc_tool as module

    monkeypatch.setattr(module, "DatarcClient", _Client)
    base = ToolRegistry()

    async def _unused(args, ctx):
        raise AssertionError("must not dispatch")

    base.register(ToolSpec(schema=ToolSchema(name="bi_query", description="query", input_schema={"type": "object"}), handler=_unused))
    base.register(ToolSpec(schema=ToolSchema(name="bi_schema", description="schema", input_schema={"type": "object"}), handler=_unused))
    store = _Store()
    tool = SimpleNamespace(
        id="ibt_1",
        provider="datarc",
        display_name="销售数据",
        endpoint="https://datarc.example",
        status="enabled",
        scope_state="verified",
        scope={"permissions": [], "workspaces": [{"id": "2", "name": "销售"}]},
    )
    specs = build_builtin_datarc_specs(team_id="team_1", tools=[tool], store=store, base_registry=base)
    spec = next(item for item in specs if item.schema.name == datarc_tool_ref("ibt_1"))
    ctx = ToolExecutionContext(workspace_id="ws_local", member_uid="", user_id="user_1", turn_id="turn_1", datarc=object(), team_id="team_2")

    assert await spec.handler({"question": "今年销售额"}, ctx) == {"ok": False, "error_kind": "team_scope_mismatch"}
