"""P1b — apply_tool_annotations seeds source + MCP-aligned annotations on built-ins.

The per-tool source mapping itself is exhaustively covered by test_p1a (lookup_domain); this
verifies the post-registration pass wires it onto every spec, derives the MCP annotation hints
from the rendering kind, and does not clobber an team/MCP self-declared source.
"""

from __future__ import annotations

from bowhead_core.canvas_tools import register_canvas_tools
from bowhead_core.tool_registry import apply_tool_annotations, build_default_registry
from bowhead_core.types import ToolInvocation
from bowhead_core.web_search import register_web_search_tool


def test_bi_query_annotated_dataset_closed_readonly() -> None:
    reg = build_default_registry()
    apply_tool_annotations(reg)
    spec = reg.get("bi_query")
    assert spec.source == "dataset"
    assert spec.annotations is not None
    assert spec.annotations.open_world is False
    assert spec.annotations.read_only is True
    assert spec.annotations.destructive is False


def test_every_default_spec_gets_source_and_annotations() -> None:
    reg = build_default_registry()
    apply_tool_annotations(reg)
    for name, spec in reg._specs.items():
        assert spec.source is not None, name
        assert spec.annotations is not None, name
        # open_world must agree with the source axis
        assert spec.annotations.open_world == (spec.source in ("web", "external")), name


def test_does_not_clobber_self_declared_source() -> None:
    reg = build_default_registry()
    reg.get("bi_query").source = "external"  # simulate an team/MCP self-declared spec
    apply_tool_annotations(reg)
    assert reg.get("bi_query").source == "external"


def test_web_open_world_and_canvas_write_destructive() -> None:
    # exercise the web→open_world and write-kind→destructive branches (build_default_registry
    # is BI-only, so register the web + canvas tools first).
    reg = build_default_registry()
    register_canvas_tools(reg)
    register_web_search_tool(reg)
    apply_tool_annotations(reg)
    web = reg._specs.get("web_search")
    if web is not None:  # registration may be config-gated in some envs
        assert web.source == "web"
        assert web.annotations.open_world is True
        assert web.annotations.read_only is True
    ins = reg.get("canvas_add_insight")  # write kind
    assert ins.source == "compute"
    assert ins.annotations.destructive is True
    assert ins.annotations.open_world is False


def test_tool_invocation_carries_domain_fields_default_none() -> None:
    # the stamp fields are additive + default None (back-compat for every constructor)
    inv = ToolInvocation(tool_name="bi_query", arguments={})
    assert inv.source is None
    assert inv.open_world is None
    inv2 = ToolInvocation(tool_name="web_search", arguments={}, source="web", open_world=True)
    assert (inv2.source, inv2.open_world) == ("web", True)
