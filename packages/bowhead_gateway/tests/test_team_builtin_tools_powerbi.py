"""Power BI administration keeps instances independent and secrets private."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.user_store import TeamBuiltinTool
from bowhead_gateway.routes import team_builtin_tools as routes


class Store:
    def __init__(self) -> None:
        self.tools: dict[str, TeamBuiltinTool] = {}
        self.auth: dict[str, dict[str, str]] = {}

    async def create_team_builtin_tool_instance(self, **kwargs):
        tool_id = f"ibt_{len(self.tools) + 1}"
        tool = TeamBuiltinTool(
            id=tool_id,
            team_id=kwargs["team_id"],
            provider=kwargs["provider"],
            display_name=kwargs["display_name"],
            endpoint="",
            status=kwargs["status"],
            auth_ref="ciphertext",
            key_hint=kwargs["key_hint"],
        )
        self.tools[tool_id] = tool
        self.auth[tool_id] = kwargs["auth"]
        return tool

    async def get_team_builtin_tool_instance_auth_decrypted(self, team_id, tool_id):
        assert self.tools[tool_id].team_id == team_id
        return self.auth[tool_id]

    async def list_team_builtin_tools(self, team_id):
        return [tool for tool in self.tools.values() if tool.team_id == team_id]


@pytest.mark.asyncio
async def test_powerbi_create_two_connections_lists_no_secret(monkeypatch):
    store = Store()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=store)))
    user = SimpleNamespace(id="user_1", team_id="team_1")

    async def verified(config):
        return {**config, "workspace_id": "workspace", "dataset_id": "dataset"}

    async def audit(*_args):
        return None

    monkeypatch.setattr(routes, "_verified_powerbi", verified)
    monkeypatch.setattr(routes, "_audit_builtin_tool", audit)
    for name, secret in (("Finance", "secret-one"), ("Sales", "secret-two")):
        result = await routes.create_powerbi(
            routes.ConfigurePowerBiBody(name=name, tenant_id="tenant", client_id="client", client_secret=secret),
            request,
            user,
        )
        assert result["name"] == name
        assert secret not in repr(result)

    listed = await routes.list_builtin_tools(request, user)
    assert {row["id"] for row in listed} == {"ibt_1", "ibt_2"}
    assert all("client_secret" not in row and "auth_ref" not in row for row in listed)
    assert store.auth["ibt_1"]["client_secret"] == "secret-one"
    assert store.auth["ibt_2"]["client_secret"] == "secret-two"
