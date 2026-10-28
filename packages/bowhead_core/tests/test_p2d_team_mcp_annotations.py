"""P2d — team tools self-declare source + MCP-aligned annotations."""

from __future__ import annotations

from bowhead_core.openapi_tool_source import openapi_entry, parse_openapi_ops
from bowhead_core.team_tool_registry_view import build_team_specs

_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/metrics/{mid}": {
            "get": {
                "operationId": "getMetric",
                "summary": "read",
                "parameters": [{"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/query": {
            "post": {
                "operationId": "runQuery",
                "summary": "write",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def test_team_specs_self_declare_external_and_write_destructive() -> None:
    entries = [openapi_entry("ots1", op) for op in parse_openapi_ops(_SPEC)]
    specs = build_team_specs("team_a", entries, client=None)  # client only used by the handler
    assert specs
    for s in specs:
        assert s.source == "external"
        assert s.annotations is not None
        assert s.annotations.open_world is True
    write = next(s for s in specs if s.needs_approval)  # POST /query → is_write
    read = next(s for s in specs if not s.needs_approval)  # GET /metrics → read
    assert write.annotations.destructive is True
    assert read.annotations.destructive is False
    assert read.annotations.read_only is True
