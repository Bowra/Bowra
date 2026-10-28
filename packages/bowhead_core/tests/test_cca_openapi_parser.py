"""cca block ④a — OpenAPI parse → inputSchema / types.Tool (tech-design §2.1/§7.2).

Asserts the deterministic OpenAPI-op → merged-JSON-Schema mapping, full $ref
de-referencing ($ref-free LLM-facing schema), the circular-$ref DoS guard, stable
teammcp.<src>.<op> naming, and types.Tool synthesis.
"""

from __future__ import annotations

import json

import pytest
from bowhead_core.openapi_tool_source import (
    OpenApiParseError,
    op_to_mcp_tool,
    op_tool_name,
    parse_openapi_ops,
    validate_openapi_spec,
)
from bowhead_router.tool_names import ToolNameMap, to_wire_tool_name  # GRND-2: the REAL adapter transform


def _spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "datarc", "version": "1"},
        "paths": {
            "/metrics/{mid}": {
                # path-level shared param (must merge into the GET op)
                "parameters": [
                    {"name": "tenant", "in": "header", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "operationId": "getMetric",
                    "summary": "Fetch one metric",
                    "tags": ["metrics"],
                    "parameters": [
                        {"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "window", "in": "query", "required": False, "schema": {"type": "string", "enum": ["7d", "30d"]}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            },
            "/query": {
                "post": {
                    "operationId": "runQuery",
                    "tags": ["query"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Q"}}},
                    },
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
        "components": {
            "schemas": {
                "Q": {
                    "type": "object",
                    "required": ["metric"],
                    "properties": {
                        "metric": {"type": "string"},
                        "filt": {"$ref": "#/components/schemas/Filter"},
                    },
                },
                "Filter": {"type": "object", "properties": {"field": {"type": "string"}}},
            }
        },
    }


def _by_id(ops):
    return {o.operation_id: o for o in ops}


@pytest.mark.unit
def test_parse_enumerates_ops_with_method_and_path() -> None:
    ops = _by_id(parse_openapi_ops(_spec()))
    assert set(ops) == {"getMetric", "runQuery"}
    assert ops["getMetric"].method == "get"
    assert ops["getMetric"].path == "/metrics/{mid}"
    assert ops["runQuery"].method == "post"
    assert ops["runQuery"].is_write is True
    assert ops["getMetric"].is_write is False


@pytest.mark.unit
def test_get_input_schema_merges_path_query_and_shared_header() -> None:
    op = _by_id(parse_openapi_ops(_spec()))["getMetric"]
    props = op.input_schema["properties"]
    assert set(props) == {"mid", "window", "tenant"}  # path + query + path-level header
    assert props["window"]["enum"] == ["7d", "30d"]
    assert op.input_schema["additionalProperties"] is False
    # required: path param + the required shared header; optional query excluded
    assert set(op.input_schema["required"]) == {"mid", "tenant"}
    locs = {p.name: p.location for p in op.params}
    assert locs == {"mid": "path", "window": "query", "tenant": "header"}


@pytest.mark.unit
def test_post_body_flattened_with_nested_ref_fully_dereferenced() -> None:
    op = _by_id(parse_openapi_ops(_spec()))["runQuery"]
    props = op.input_schema["properties"]
    assert set(props) == {"metric", "filt"}  # body object props flattened
    assert op.body_prop_names == {"metric", "filt"}
    assert "metric" in op.input_schema["required"]  # body required prop
    # nested $ref (Q.filt → Filter) is fully resolved — NO $ref anywhere
    assert props["filt"] == {"type": "object", "properties": {"field": {"type": "string"}}}
    assert "$ref" not in json.dumps(op.input_schema)


@pytest.mark.unit
def test_circular_ref_does_not_hang_and_is_bounded() -> None:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "post": {
                    "operationId": "cyc",
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}},
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},  # cycle
                }
            }
        },
    }
    ops = parse_openapi_ops(spec)  # must return (bounded), not hang / recurse forever
    assert len(ops) == 1
    # bounded output is still valid JSON (no $ref, terminates at the depth cap)
    assert "$ref" not in json.dumps(ops[0].input_schema)


@pytest.mark.unit
def test_selected_op_ids_filter() -> None:
    ops = parse_openapi_ops(_spec(), selected_op_ids=["runQuery"])
    assert [o.operation_id for o in ops] == ["runQuery"]


@pytest.mark.unit
def test_operation_without_operation_id_skipped() -> None:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},  # no operationId
    }
    assert parse_openapi_ops(spec) == []


@pytest.mark.unit
def test_malformed_spec_raises() -> None:
    with pytest.raises(OpenApiParseError):
        parse_openapi_ops({"no_paths": True})
    with pytest.raises(OpenApiParseError):
        parse_openapi_ops("not a dict")  # type: ignore[arg-type]


@pytest.mark.unit
def test_op_tool_name_stable_compliant_and_id_derived() -> None:
    n = op_tool_name("ots_abc123", "getMetric")
    assert n == "teammcp.ots_abc123.getMetric"
    # MCP-compliant char set
    import re as _re

    assert _re.fullmatch(r"[A-Za-z0-9_.-]+", n)
    assert len(n) <= 128
    # derived from immutable id, NOT a display label → illegal chars slugged
    weird = op_tool_name("ots_abc", "Run Query!/v2")
    assert weird.startswith("teammcp.ots_abc.")
    assert _re.fullmatch(r"[A-Za-z0-9_.-]+", weird)


@pytest.mark.unit
def test_op_to_mcp_tool_emits_types_tool_with_input_schema() -> None:
    import mcp.types

    op = _by_id(parse_openapi_ops(_spec()))["getMetric"]
    tool = op_to_mcp_tool("ots_abc", op)
    assert isinstance(tool, mcp.types.Tool)
    assert tool.name == "teammcp.ots_abc.getMetric"
    assert tool.input_schema["type"] == "object"
    assert "mid" in tool.input_schema["properties"]
    assert tool.description  # non-empty (summary or method+path fallback)


@pytest.mark.unit
def test_validate_openapi_spec_accepts_valid_rejects_invalid() -> None:
    validate_openapi_spec(_spec())  # valid → no raise
    with pytest.raises(OpenApiParseError):
        validate_openapi_spec({"paths": {}})  # missing openapi/info → invalid


# ── C8: op-level tags (group-by-tag data source) ──────────────────────────────
@pytest.mark.unit
def test_parse_captures_op_tags() -> None:
    ops = _by_id(parse_openapi_ops(_spec()))
    assert ops["getMetric"].tags == ("metrics",)
    assert ops["runQuery"].tags == ("query",)
    # An operation with no tags → empty tuple (the "未分组" bucket).
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"get": {"operationId": "noTag", "responses": {"200": {"description": "ok"}}}}},
    }
    assert _by_id(parse_openapi_ops(spec))["noTag"].tags == ()
    # CORR-5: duplicate tags are de-duped, order preserved
    dup = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/y": {"get": {"operationId": "dupTags", "tags": ["a", "a", "b"], "responses": {"200": {"description": "ok"}}}}},
    }
    assert _by_id(parse_openapi_ops(dup))["dupTags"].tags == ("a", "b")


# ── C11 / MCP-1: tool name survives the DeepSeek/Doubao sanitize_tool_dots round-trip ──
def _dots_roundtrip(name: str) -> str:
    # GRND-2: the REAL adapter transform (sanitize_tool_dots, on by default for every provider),
    # imported — not a hand-copy — so the test tracks the adapters if it ever changes.
    return ToolNameMap([name]).to_canonical(to_wire_tool_name(name))


@pytest.mark.unit
def test_op_tool_name_survives_sanitize_tool_dots_roundtrip() -> None:
    import re as _re

    # A '__' anywhere in a segment would be eaten by the reverse replace('__','.'),
    # so the primary dev/prod model (DeepSeek) could not dispatch the tool.
    for sid, op_id in [
        ("ots_abc", "get__latest__rate"),
        ("ots__weird", "doThing"),
        ("ots_abc", "a_____b"),
        ("ots_abc", "trailingUnderscore_"),
    ]:
        n = op_tool_name(sid, op_id)
        assert "__" not in n, f"{n!r} contains '__' → sanitize round-trip corrupts it"
        assert _dots_roundtrip(n) == n, f"{n!r} did not survive the sanitize round-trip"
        # the on-wire (sanitized) form must be a name DeepSeek/Anthropic accepts
        assert _re.fullmatch(r"[A-Za-z0-9_-]+", to_wire_tool_name(n)), f"{n!r} → on-wire name the provider would reject"


@pytest.mark.unit
def test_op_tool_name_truncation_disambiguates() -> None:
    # MCP-3: two distinct long operationIds sharing a >48-char prefix must not
    # collide to the same tool name.
    long_a = "getReallyLongOperation" + "X" * 60 + "Alpha"
    long_b = "getReallyLongOperation" + "X" * 60 + "Beta"
    na, nb = op_tool_name("ots_abc", long_a), op_tool_name("ots_abc", long_b)
    assert na != nb, "truncated long operationIds collided to the same name"
    assert len(na) <= 128 and len(nb) <= 128
    assert _dots_roundtrip(na) == na and _dots_roundtrip(nb) == nb


def test_fat_op_does_not_starve_a_sibling_op_budget() -> None:
    """review regression: the parser used to deep-resolve a whole path-item under ONE shared budget
    BEFORE the selected filter, so a fat (or unselected) op could exhaust it and silently DROP a
    sibling op. Each op's body must deref with its own fresh budget."""
    big = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(6000)}}  # > _MAX_DEREF_NODES
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "fatOp",
                    "requestBody": {"content": {"application/json": {"schema": big}}},
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "operationId": "smallOp",
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"q": {"type": "string"}}}}}},
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }
    ids = {o.operation_id for o in parse_openapi_ops(spec)}
    assert ids == {"fatOp", "smallOp"}, ids  # smallOp NOT dropped by fatOp's budget
    # selecting only the small op must still yield it (was empty → 422 before the fix)
    assert {o.operation_id for o in parse_openapi_ops(spec, selected_op_ids=["smallOp"])} == {"smallOp"}
    # smallOp's body schema is intact ($ref-free, its own budget)
    small = next(o for o in parse_openapi_ops(spec) if o.operation_id == "smallOp")
    assert "q" in json.dumps(small.input_schema)


def test_json_pointer_rejects_non_rfc6901_array_index() -> None:
    """_json_pointer array tokens follow RFC 6901 (0 | [1-9][0-9]*); int() was too lenient."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/y": {"get": {"operationId": "op", "parameters": [{"$ref": "#/x/-1"}], "responses": {"200": {"description": "ok"}}}}},
        "x": [{"name": "a", "in": "query", "schema": {"type": "string"}}],
    }
    # "#/x/-1" must NOT wrap to a valid element → the bad param resolves to {} → skipped (0 params), no crash
    op = next(o for o in parse_openapi_ops(spec) if o.operation_id == "op")
    assert op.params == ()


def test_tool_name_conflicts_group_operations_that_share_a_tool_name():
    from bowhead_core.openapi_tool_source import tool_name_conflicts

    ids = ["查询订单", "删除订单", "orders.list", "orders-list", "ping"]
    conflicts = tool_name_conflicts("ots_1", ids)
    assert [(name, sorted(group)) for name, group in conflicts] == [
        (op_tool_name("ots_1", "orders-list"), ["orders-list", "orders.list"]),
        (op_tool_name("ots_1", "查询订单"), ["删除订单", "查询订单"]),
    ]
    # Existing, non-colliding names are an interface and stay unchanged.
    assert op_tool_name("ots_1", "orders.list") == "teammcp.ots_1.orders-list"
    assert tool_name_conflicts("ots_1", ["orders.list", "ping"]) == []


@pytest.mark.asyncio
async def test_loader_fails_closed_for_tools_that_share_a_name():
    """A stored source whose operations collide must never dispatch one tool to another endpoint."""
    from types import SimpleNamespace

    from bowhead_core.team_tool_loader import load_team_tool_entries

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {"get": {"operationId": "查询订单", "responses": {"200": {"description": "ok"}}}},
            "/b": {"delete": {"operationId": "删除订单", "responses": {"200": {"description": "ok"}}}},
            "/c": {"get": {"operationId": "ping", "responses": {"200": {"description": "ok"}}}},
        },
    }
    source = SimpleNamespace(
        id="ots_1", kind="openapi", openapi_spec_json=json.dumps(spec, ensure_ascii=False), selected_op_ids=[], tool_overrides={}
    )

    class Store:
        async def list_team_tool_sources(self, team_id, *, only_enabled=True):
            return [source]

    entries, _ = await load_team_tool_entries("team_a", Store())
    assert [entry.advertised_name for entry in entries] == [op_tool_name("ots_1", "ping")]
