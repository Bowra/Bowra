"""D15 — spawned-peer token folding.

A subagent_spawn invocation runs a nested LLM loop whose token usage is returned in
its tool result but was never added to the parent loop's counters, so peer spend (often
the bulk of a turn) was billed to nobody (~5x undercount in prod). `_spawn_token_delta`
extracts that usage so the parent loop can fold it into result.tokens_* → turn persist
+ credit deduct + D7b session rollup.
"""

from __future__ import annotations

from bowhead_core.subagent import _spawn_token_delta
from bowhead_core.types import ToolInvocation


def test_spawn_invocation_yields_peer_tokens() -> None:
    spawn = ToolInvocation(
        tool_name="subagent_spawn",
        arguments={},
        result={"ok": True, "tokens_input": 143314, "tokens_output": 7403},
    )
    assert _spawn_token_delta(spawn) == (143314, 7403, 0)  # No cached key → 0.


def test_spawn_invocation_yields_peer_cached_tokens() -> None:
    # perf WP-1 P1-b: fold peer cache hits into Lead/Turn-level tokens_cached too.
    spawn = ToolInvocation(
        tool_name="subagent_spawn",
        arguments={},
        result={"ok": True, "tokens_input": 83, "tokens_output": 40, "tokens_cached": 6400},
    )
    assert _spawn_token_delta(spawn) == (83, 40, 6400)


def test_non_spawn_tool_yields_zero_even_with_tokens_key() -> None:
    # Only subagent_spawn is summed — guards against a tool whose result happens to
    # carry a 'tokens_input' key meaning something else.
    other = ToolInvocation(
        tool_name="bi_query",
        arguments={},
        result={"results": [], "tokens_input": 999},
    )
    assert _spawn_token_delta(other) == (0, 0, 0)


def test_spawn_with_no_token_fields_is_zero() -> None:
    assert _spawn_token_delta(ToolInvocation(tool_name="subagent_spawn", arguments={}, result=None)) == (0, 0, 0)
    assert _spawn_token_delta(ToolInvocation(tool_name="subagent_spawn", arguments={}, result={"ok": True})) == (0, 0, 0)


def test_lead_plus_peer_total_matches_prod_repro() -> None:
    # prod s_7ea966: Lead loop 36,464 in / 751 out; compute peer 143,314 / 7,403.
    # The turn (and credit_ledger) billed only the Lead total — the bug.
    lead_in, lead_out = 36_464, 751
    peer = ToolInvocation(
        tool_name="subagent_spawn",
        arguments={},
        result={"tokens_input": 143_314, "tokens_output": 7_403},
    )
    d_in, d_out, _d_cached = _spawn_token_delta(peer)
    assert (lead_in + d_in, lead_out + d_out) == (179_778, 8_154)
