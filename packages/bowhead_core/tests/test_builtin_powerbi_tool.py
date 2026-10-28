"""Power BI tool calls stay bound to their selected team connection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.builtin_tool_providers import active_by_provider
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import TeamBuiltinTool

from bowhead_core import builtin_powerbi_tool as powerbi


class Store:
    def __init__(self, rows):
        self.rows = {row.id: row for row in rows}
        self.logged = []

    async def user_has_active_team_membership(self, user_id, team_id):
        return user_id == "user_1" and team_id == "team_1"

    async def get_team_builtin_tool_instance(self, team_id, tool_id):
        return self.rows.get(tool_id) if team_id == "team_1" else None

    async def get_team_builtin_tool_instance_auth_decrypted(self, team_id, tool_id):
        return {
            "tenant_id": "tenant",
            "client_id": "client",
            "client_secret": tool_id,
            "workspace_id": f"workspace-{tool_id}",
            "dataset_id": f"dataset-{tool_id}",
        }

    async def list_builtin_pbi_templates(self, team_id, tool_id):
        return [{"id": f"tpl-{tool_id}", "name": "test", "dax_template": 'EVALUATE ROW("v", 1)'}]

    async def log_tool_call(self, **kwargs):
        self.logged.append(kwargs)


@pytest.mark.asyncio
async def test_two_powerbi_instances_use_separate_credentials_and_recheck_status(monkeypatch):
    rows = [
        TeamBuiltinTool(id=tool_id, team_id="team_1", provider="powerbi", display_name=tool_id, endpoint="", status="enabled", auth_ref="cipher")
        for tool_id in ("pbi_one", "pbi_two")
    ]
    store = Store(rows)
    seen = []

    def build_handle(*, credential, workspace_id, dataset_id, catalog):
        return SimpleNamespace(credential=credential, workspace_id=workspace_id, dataset_id=dataset_id, catalog=catalog)

    async def execute(args, ctx):
        seen.append((ctx.pbi.credential["client_secret"], ctx.pbi.workspace_id, args["dax"]))
        return {"rows": [{"value": 1}]}

    monkeypatch.setattr(powerbi, "build_pbi_turn_handle", build_handle)
    monkeypatch.setitem(powerbi._HANDLERS, "pbi_execute_dax", execute)
    specs = powerbi.build_builtin_powerbi_specs(team_id="team_1", tools=rows, store=store, base_registry=None)
    ctx = ToolExecutionContext(workspace_id="ws_1", member_uid="", user_id="user_1", turn_id="turn_1", datarc=None, team_id="team_1")
    selected = {spec.schema.name: spec for spec in specs if spec.schema.name.endswith("pbi_execute_dax")}
    assert set(selected) == {"teammcp.pbi_one.pbi_execute_dax", "teammcp.pbi_two.pbi_execute_dax"}
    for spec in selected.values():
        assert await spec.handler({"dax": "EVALUATE X"}, ctx) == {"rows": [{"value": 1}]}
    assert seen == [("pbi_one", "workspace-pbi_one", "EVALUATE X"), ("pbi_two", "workspace-pbi_two", "EVALUATE X")]
    store.rows["pbi_one"].status = "disabled"
    assert (await selected["teammcp.pbi_one.pbi_execute_dax"].handler({"dax": "EVALUATE X"}, ctx))["error_kind"] == "pbi_connection_unavailable"
    assert len(seen) == 2
    active = active_by_provider(rows, None)
    assert [(provider.name, [tool.id for tool in tools]) for provider, tools in active] == [("powerbi", ["pbi_two"])]
