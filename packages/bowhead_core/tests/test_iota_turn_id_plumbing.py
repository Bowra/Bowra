"""Sprint ι r2 P1 #3 (PRD §6.2): turn_id flows from ComposeContext → MapperInput
→ code_block payload, end-to-end on both compose path and realtime stream path.
"""

from __future__ import annotations

from bowhead_core.canvas_composers.realtime_stream import (
    build_realtime_sections_for_inv,
)
from bowhead_core.types import ToolInvocation


def test_realtime_stream_carries_turn_id_python_sandbox() -> None:
    """realtime_stream.build_realtime_sections_for_inv writes turn_id."""
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "y = 2"},
        result={"stdout": "", "outputs": []},
    )
    payloads = build_realtime_sections_for_inv(inv, turn_id="t_abc123def456")
    assert payloads, "expected ≥1 code_block payload"
    cb = [p for p in payloads if p.get("kind") == "code_block"]
    assert cb, f"expected code_block kind, got {[p.get('kind') for p in payloads]}"
    for p in cb:
        assert p.get("turn_id") == "t_abc123def456", f"code_block.turn_id should match passed turn_id; got {p.get('turn_id')!r}"


def test_realtime_stream_carries_turn_id_bi_ask_question() -> None:
    inv = ToolInvocation(
        tool_name="bi_ask_question",
        arguments={"text": "Q"},
        result={"explanations_sql": ["SELECT 1"]},
    )
    payloads = build_realtime_sections_for_inv(inv, turn_id="t_xyz")
    assert payloads
    for p in payloads:
        assert p.get("turn_id") == "t_xyz"


def test_realtime_stream_default_turn_id_empty() -> None:
    """Backward compat: missing turn_id defaults to empty string."""
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "z = 3"},
        result={"stdout": "", "outputs": []},
    )
    payloads = build_realtime_sections_for_inv(inv)
    assert payloads
    for p in payloads:
        assert p.get("turn_id") == ""
