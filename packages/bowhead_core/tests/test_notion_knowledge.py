from types import SimpleNamespace

from bowhead_core.notion_knowledge import notion_knowledge_entry
from bowhead_core.openapi_tool_source import mcp_entry
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.tool_registry import ToolExecutionContext


class NotionClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.response = {
            "type": "workspace_search",
            "results": [
                {"url": "https://www.notion.so/Atlas-f0faccb8ade2499994a1f33131261a9b", "title": "Atlas decision"},
                {"url": "https://external.example/item", "title": "External result"},
            ],
            "notices": [{"field": "filters.title_only"}],
        }

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content=self.response)


async def test_notion_knowledge_research_searches_workspace_and_preserves_notices() -> None:
    client = NotionClient()
    entry = notion_knowledge_entry(
        mcp_entry("ots_notion", {"name": "notion-search", "annotations": {"readOnlyHint": True, "destructiveHint": False}}),
        "initial",
    )
    assert entry is not None
    spec = build_team_specs("team", [entry], client)[0]
    context = ToolExecutionContext(workspace_id="ws", member_uid="member", user_id="user", turn_id="turn", datarc=None, team_id="team")
    rejected = await spec.handler({"query": "Team member", "query_type": "user"}, context)
    assert rejected["error_kind"] == "invalid_arguments"
    assert not client.calls
    result = await spec.handler({"query": "Atlas", "title_only": True}, context)
    assert client.calls == [("notion-search", {"query": "Atlas", "query_type": "internal", "filters": {"title_only": True}})]
    assert [row["title"] for row in result["data"]["results"]] == ["Atlas decision"]
    assert result["data"]["notices"] == [{"field": "filters.title_only"}]
    client.response["type"] = "ai_search"
    assert (await spec.handler({"query": "Atlas"}, context))["error_kind"] == "notion_search_scope"


async def test_notion_knowledge_fetch_limits_targets_before_dispatch() -> None:
    client = NotionClient()
    entry = notion_knowledge_entry(
        mcp_entry("ots_notion", {"name": "notion-fetch", "annotations": {"readOnlyHint": True, "destructiveHint": False}}),
        "initial",
    )
    assert entry is not None
    spec = build_team_specs("team", [entry], client)[0]
    context = ToolExecutionContext(workspace_id="ws", member_uid="member", user_id="user", turn_id="turn", datarc=None, team_id="team")
    for target in ("memory", "https://www.notion.so.external.example/Atlas-f0faccb8ade2499994a1f33131261a9b"):
        assert (await spec.handler({"id": target}, context))["error_kind"] == "invalid_arguments"
    await spec.handler({"id": "https://www.notion.so/Atlas-f0faccb8ade2499994a1f33131261a9b"}, context)
    assert client.calls == [("notion-fetch", {"id": "f0faccb8ade2499994a1f33131261a9b"})]
