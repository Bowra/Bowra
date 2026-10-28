"""P1a — tool_category domain primitive: lookup_domain + classify_turn_source (D1).

The cross-turn digest needs to know whether a turn answered from the connected workspace
dataset/KB (closed world) or from web/external (open world), so a topical follow-up continues
the same world instead of defaulting to bi_query. open_world mirrors MCP openWorldHint.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bowhead_core.tool_category import classify_turn_source, lookup_domain


def _inv(tool_name: str, *, source: Any = None, profile: str | None = None, nested: Any = None) -> Any:
    return SimpleNamespace(
        tool_name=tool_name,
        source=source,
        arguments=({"profile": profile} if profile else {}),
        nested=nested or [],
    )


# ── lookup_domain (the by-name correctness floor) ──
def test_lookup_domain_prefixes() -> None:
    assert lookup_domain("bi_query") == "dataset"
    assert lookup_domain("bi_schema") == "dataset"
    assert lookup_domain("kb_search") == "kb"
    assert lookup_domain("web_search") == "web"
    assert lookup_domain("web_fetch") == "web"
    assert lookup_domain("teammcp.acme.get_orders") == "external"
    assert lookup_domain("mcp.server.tool") == "external"
    assert lookup_domain("send_email") == "external"
    assert lookup_domain("add_chart") == "compute"
    assert lookup_domain("skill_invoke") == "compute"
    assert lookup_domain("python_sandbox") == "compute"
    assert lookup_domain("clarification_ask") == "compute"  # unknown → closed compute


# ── classify_turn_source ──
def test_empty_turn_is_model() -> None:
    assert classify_turn_source([]) == {"source": "model", "open_world": False}
    assert classify_turn_source(None) == {"source": "model", "open_world": False}


def test_web_only_is_open_world() -> None:  # the GDP repro signal
    assert classify_turn_source([_inv("web_search")]) == {"source": "web", "open_world": True}


def test_dataset_and_kb_are_closed() -> None:
    assert classify_turn_source([_inv("bi_query")]) == {"source": "dataset", "open_world": False}
    assert classify_turn_source([_inv("kb_search")]) == {"source": "kb", "open_world": False}


def test_stamped_source_preferred_over_name() -> None:
    # an teammcp tool stamped 'external' off the per-turn view, even with an unknown name
    assert classify_turn_source([_inv("weirdname", source="external")]) == {"source": "external", "open_world": True}


def test_mixed_web_and_dataset_prefers_dataset() -> None:
    out = classify_turn_source([_inv("web_search"), _inv("bi_query")])
    assert out == {"source": "dataset", "open_world": False}  # touched dataset → continue it


def test_spawn_nested_bi_is_dataset() -> None:
    spawn = _inv("subagent_spawn", profile="compute", nested=[_inv("bi_query")])
    assert classify_turn_source([spawn])["source"] == "dataset"


def test_spawn_web_only_peer_is_web_not_dataset() -> None:
    # nested speaks over the dataset-default profile: a peer that ONLY web-searched → web
    spawn = _inv("subagent_spawn", profile="marketing-analyst", nested=[_inv("web_search")])
    assert classify_turn_source([spawn]) == {"source": "web", "open_world": True}


def test_spawn_empty_nested_falls_back_to_profile() -> None:
    assert classify_turn_source([_inv("subagent_spawn", profile="kb-analyst")])["source"] == "kb"
    # specialist / unknown → dataset default (safe)
    assert classify_turn_source([_inv("subagent_spawn", profile="financial-analyst")])["source"] == "dataset"
    assert classify_turn_source([_inv("subagent_spawn", profile="canvas-writer")])["source"] == "compute"
    assert classify_turn_source([_inv("subagent_spawn", profile="brand-new-peer")])["source"] == "dataset"
