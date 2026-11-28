"""The source binding and read policy for the Notion Team Knowledge Plugin."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from bowhead_core.openapi_tool_source import TeamToolEntry, op_tool_name

NOTION_MCP_URL = "https://mcp.notion.com/mcp"
NOTION_KNOWLEDGE_SKILL = "notion-research-documentation"
NOTION_KNOWLEDGE_TEMPLATE = "notion_knowledge"
NOTION_RESEARCH_OPERATIONS = ("notion-search", "notion-fetch", "notion-query-data-sources")

_UUID = r"[a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}"
_PAGE_ID = re.compile(rf"(?:^|-)({_UUID})$", re.IGNORECASE)
_DATA_SOURCE = re.compile(rf"collection://{_UUID}", re.IGNORECASE)
_TOOL_NAMES = dict(zip(NOTION_RESEARCH_OPERATIONS, ("search", "fetch", "query"), strict=True))
NOTION_KNOWLEDGE_TOOL_LABELS = {
    "notion-search": "Search Notion content",
    "notion-fetch": "Read a Notion page",
    "notion-query-data-sources": "Query a Notion database",
}
_DESCRIPTIONS = {
    "notion-search": "Search content in the bound Notion workspace. Read matching pages before citing their conclusions.",
    "notion-fetch": "Read a Notion page, database, or data source by its Notion URL or ID.",
    "notion-query-data-sources": "Read rows from one Notion data source. Fetch its schema and collection URL first.",
}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "notion-search": {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 512}, "title_only": {"type": "boolean"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "notion-fetch": {
        "type": "object",
        "properties": {"id": {"type": "string", "maxLength": 2048}},
        "required": ["id"],
        "additionalProperties": False,
    },
    "notion-query-data-sources": {
        "type": "object",
        "properties": {
            "data_source_url": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["data_source_url"],
        "additionalProperties": False,
    },
}


def is_notion_oauth_source(kind: str, auth_type: str, base_url: str) -> bool:
    return kind == "mcp" and auth_type == "oauth" and base_url.rstrip("/") == NOTION_MCP_URL


def notion_knowledge_tool_ref(source_id: str, operation: str, binding: str) -> str:
    return op_tool_name(source_id, f"notion_knowledge_{_TOOL_NAMES[operation]}_{binding}")


def notion_knowledge_entry(entry: TeamToolEntry, binding: str) -> TeamToolEntry | None:
    operation = entry.dispatch_name
    if operation not in _SCHEMAS or entry.read_only_hint is not True or entry.destructive_hint is not False:
        return None
    return replace(
        entry,
        advertised_name=notion_knowledge_tool_ref(entry.source_id, operation, binding),
        description=_DESCRIPTIONS[operation],
        input_schema=_SCHEMAS[operation],
        admin_category=None,
        notion_knowledge_binding=binding,
    )


def _notion_content_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\x7f]", value):
        return None
    if re.fullmatch(_UUID, value, re.IGNORECASE) or _DATA_SOURCE.fullmatch(value):
        return value
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if url.scheme != "https" or url.username or url.password or url.port is not None:
            return None
    except ValueError:
        return None
    if host not in {"notion.so", "notion.site", "notion.com"} and not host.endswith((".notion.so", ".notion.site")):
        return None
    page = url.path.rstrip("/").rsplit("/", 1)[-1]
    match = _PAGE_ID.search(page)
    return match[1] if match else None


def notion_knowledge_arguments(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("Supply the Notion Tool arguments as an object.")
    if operation == "notion-search":
        query = args.get("query")
        title_only = args.get("title_only", False)
        if set(args) <= {"query", "title_only"} and isinstance(query, str) and 0 < len(query.strip()) <= 512 and isinstance(title_only, bool):
            return {"query": query.strip(), "query_type": "internal", "filters": {"title_only": True} if title_only else {}}
        raise ValueError("Enter a Notion content search query and use a boolean for title-only search.")
    if operation == "notion-fetch":
        target = _notion_content_id(args.get("id"))
        if set(args) == {"id"} and target is not None:
            return {"id": target}
        raise ValueError("Use a Notion page, database, or data-source link or ID.")
    if operation != "notion-query-data-sources":
        raise ValueError("Choose an approved Notion research Tool.")
    target = args.get("data_source_url")
    limit = args.get("limit", 50)
    if (
        set(args) <= {"data_source_url", "limit"}
        and isinstance(target, str)
        and _DATA_SOURCE.fullmatch(target)
        and type(limit) is int
        and 1 <= limit <= 100
    ):
        return {"data": {"mode": "rows", "data_source_url": target, "limit": limit}}
    raise ValueError("Use the collection URL returned by Notion and request between 1 and 100 rows.")


def notion_knowledge_search_result(result: dict[str, Any]) -> dict[str, Any]:
    """Admit only workspace search results, including notices about dropped filters."""
    data = result.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = None
    if not isinstance(data, dict) or data.get("type") != "workspace_search" or not isinstance(data.get("results"), list):
        return {
            "ok": False,
            "error_kind": "notion_search_scope",
            "error": "Notion did not return a workspace search. Refine the search in the bound Notion workspace.",
        }
    matches = [item for item in data["results"] if isinstance(item, dict) and _notion_content_id(item.get("url"))]
    return {"ok": True, "data": {"type": "workspace_search", "results": matches, "notices": data.get("notices", [])}}
