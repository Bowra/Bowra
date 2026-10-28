from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.openapi_tool_source import _mcp_tool_to_cache, mcp_entry


def test_mcp_cache_preserves_authoring_risk_annotations() -> None:
    tool = SimpleNamespace(
        name="lookup",
        description="Lookup",
        input_schema={"type": "object"},
        annotations=SimpleNamespace(
            read_only_hint=True,
            destructive_hint=False,
        ),
    )

    cached = _mcp_tool_to_cache(tool)
    assert cached["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }

    entry = mcp_entry("src_1", cached)
    assert entry.read_only_hint is True
    assert entry.destructive_hint is False
