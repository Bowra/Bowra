"""CCA P3 · S4.3 — output_schema gate in subagent_return (tech-design §5.3).

`_apply_output_schema_gate(ctx, payload, subagent_id)` validates a returning payload against
the per-tc team-subagent output_schema the runner stamped on `ctx._running_output_schema`.
ADDITIVE on top of validate_payload (AnalysisPayload shape) — extra constraints, never a
bypass. Capped retries (`_schema_retry`, a non-tool-failure flag) then DEGRADE-accept;
`format_checker=None` + try/except means a malformed/hostile schema degrades, never hangs.

review fix: the retry counter is a DICT keyed by subagent_id on the SHARED ctx, so a sibling
subagent can't inherit another's exhausted count, and the count accumulates across a peer's
retries even on the legacy copy.copy(ctx) role-swap path.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from bowhead_core.subagent_return_tool import (
    _OSCHEMA_MAX_RETRIES,
    CTX_OUTPUT_SCHEMA_RETRY_ATTR,
    _apply_output_schema_gate,
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _ctx(schema=None):
    # mirror the stamp sites: schema + the seeded retry-count dict on the shared ctx
    return SimpleNamespace(_running_output_schema=schema, _p3_output_schema_retry_counts={}, turn_id="t")


def _count(ctx, sid=""):
    return getattr(ctx, CTX_OUTPUT_SCHEMA_RETRY_ATTR).get(sid, 0)


def test_attr_name_is_pinned() -> None:
    # the stamp sites (subagent.py / subagent_graph.py) hardcode this literal; pin it so a
    # rename of the constant without updating those seeds is caught here.
    assert CTX_OUTPUT_SCHEMA_RETRY_ATTR == "_p3_output_schema_retry_counts"


def test_no_schema_proceeds() -> None:
    assert _apply_output_schema_gate(_ctx(None), {"anything": 1}) is None


def test_passing_payload_proceeds() -> None:
    assert _apply_output_schema_gate(_ctx(_SCHEMA), {"summary": "ok"}) is None


def test_failing_payload_returns_capped_retry_then_degrades() -> None:
    ctx = _ctx(_SCHEMA)
    # retries 0,1 → schema_retry error; the sid-keyed counter increments each time
    for expected_next in (1, 2):
        out = _apply_output_schema_gate(ctx, {"summary": 123}, "sub_a")  # wrong type
        assert out is not None and out["ok"] is False
        assert out["_schema_retry"] is True
        assert out["validation_errors"]
        assert _count(ctx, "sub_a") == expected_next
    assert _count(ctx, "sub_a") == _OSCHEMA_MAX_RETRIES
    # retries exhausted → DEGRADE: proceed (None), don't loop to the timeout
    assert _apply_output_schema_gate(ctx, {"summary": 123}, "sub_a") is None


def test_sibling_subagents_have_independent_retry_counts() -> None:
    """review fix: subagent A exhausting its retries must NOT make sibling B degrade-accept on
    its FIRST failure — counts are keyed by subagent_id on the shared ctx."""
    ctx = _ctx(_SCHEMA)
    for _ in range(_OSCHEMA_MAX_RETRIES):
        assert _apply_output_schema_gate(ctx, {"summary": 1}, "sub_a")["_schema_retry"] is True
    assert _apply_output_schema_gate(ctx, {"summary": 1}, "sub_a") is None  # A degrades
    # B (same shared ctx) still gets its FULL retry budget — no cross-pollution
    assert _count(ctx, "sub_b") == 0
    out = _apply_output_schema_gate(ctx, {"summary": 1}, "sub_b")
    assert out is not None and out["_schema_retry"] is True
    assert _count(ctx, "sub_b") == 1


def test_count_accumulates_across_legacy_per_call_ctx_copies() -> None:
    """review fix (legacy path): each subagent_return runs on a fresh copy.copy(parent_ctx)
    (the role-swap). Because the retry dict is SEEDED on the shared parent and copied by
    reference, the count accumulates across copies → the degrade path is reachable on legacy
    (pre-fix the flat-int setattr landed on the throwaway copy and never accumulated)."""
    parent = _ctx(_SCHEMA)
    out1 = _apply_output_schema_gate(copy.copy(parent), {"summary": 1}, "sub_a")
    out2 = _apply_output_schema_gate(copy.copy(parent), {"summary": 1}, "sub_a")
    assert out1["_schema_retry"] and out2["_schema_retry"]
    # the shared parent dict accumulated (not reset to 1 on each copy)
    assert _count(parent, "sub_a") == _OSCHEMA_MAX_RETRIES
    # a third copy now degrades (count exhausted on the shared dict)
    assert _apply_output_schema_gate(copy.copy(parent), {"summary": 1}, "sub_a") is None


def test_lazy_inits_dict_when_unseeded() -> None:
    """defense: if the stamp seed is somehow absent, the gate lazily creates the dict rather
    than crashing on a ctx that lacks the attr."""
    ctx = SimpleNamespace(_running_output_schema=_SCHEMA, turn_id="t")
    out = _apply_output_schema_gate(ctx, {"summary": 1}, "sub_a")
    assert out is not None and out["_schema_retry"] is True
    assert isinstance(getattr(ctx, CTX_OUTPUT_SCHEMA_RETRY_ATTR), dict)


def test_malformed_schema_degrades_not_crash() -> None:
    # a hostile/malformed schema that would make the validator raise → degrade to a retry
    # (errs=['output_schema_validation_error']), never propagate the exception.
    ctx = _ctx({"type": "object", "properties": {"x": {"$ref": "urn:nonexistent#/nope"}}})
    out = _apply_output_schema_gate(ctx, {"x": 1}, "sub_a")
    assert out is None or (out["ok"] is False and out["_schema_retry"] is True)


def test_required_field_missing_is_caught() -> None:
    out = _apply_output_schema_gate(_ctx(_SCHEMA), {}, "sub_a")  # missing 'summary'
    assert out is not None and out["_schema_retry"] is True


def test_oversize_payload_skips_gate() -> None:
    """S4 review P2: an oversized payload skips the (additive) gate rather than burning
    synchronous CPU on the event loop — degrade-accept (return None)."""
    big = {"summary": "x" * 300_000}  # > 256 KiB serialized
    assert _apply_output_schema_gate(_ctx(_SCHEMA), big, "sub_a") is None
