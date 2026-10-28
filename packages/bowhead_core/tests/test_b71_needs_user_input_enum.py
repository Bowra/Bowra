"""bi-hitl-cleanup P3.5 / B7.1 tests — needs_user_input enum + error type widen.

Per tech-design §P3.5 + PRD review P0-CTO-1:
- _VALID_BAIL_KINDS adds `needs_user_input`
- AnalysisPayload.error type `dict[str, str]` → `dict[str, Any]` (allows nested
  `clarification_payload: dict` sub-key per B5 schema)
- validate_payload accepts new kind + rejects unknown
- _fix_bail_kind (in payload_auto_fix) auto-propagates via _VALID_BAIL_KINDS set;
  no longer coerces needs_user_input to tool_failure

Prep for P3.6: Lead bail path will branch on this kind to emit clarification_ask.
"""

from __future__ import annotations

from bowhead_core.analysis_payload import (
    _VALID_BAIL_KINDS,
    AnalysisPayload,
    validate_payload,
)

# ── Enum membership ─────────────────────────────────────────────────────


def test_b71_needs_user_input_in_valid_bail_kinds() -> None:
    """B7.1 close (P0-CTO-1): `needs_user_input` added to _VALID_BAIL_KINDS."""
    assert "needs_user_input" in _VALID_BAIL_KINDS


def test_b71_existing_kinds_preserved() -> None:
    """Pre-B7.1 5 kinds untouched."""
    pre_b71 = {
        "domain_mismatch",
        "ambiguity",
        "transient",
        "schema_invalid",
        "tool_failure",
    }
    assert pre_b71.issubset(_VALID_BAIL_KINDS)
    # And total count is 6 (5 old + 1 new)
    assert len(_VALID_BAIL_KINDS) == 6


# ── Validator behavior ──────────────────────────────────────────────────


def test_b71_validator_accepts_needs_user_input() -> None:
    """Bail envelope with kind=needs_user_input + message passes validator."""
    payload = {
        "error": {
            "kind": "needs_user_input",
            "message": "Which formula for 客单价?",
        },
    }
    ok, errs = validate_payload(payload)
    assert ok, f"Should accept needs_user_input, got errs: {errs}"


def test_b71_validator_accepts_needs_user_input_with_nested_clarification_payload() -> None:
    """B7.1 type widen: error dict may carry nested `clarification_payload: dict`
    sub-key (B5 schema: questions/options/type). Validator must NOT reject
    untyped nested fields — type was widened from `dict[str, str]` to
    `dict[str, Any]`."""
    payload = {
        "error": {
            "kind": "needs_user_input",
            "message": "Which formula for 客单价?",
            "clarification_payload": {
                "questions": [
                    {
                        "question": "指标 '客单价' 有多种可能公式, 哪个对?",
                        "options": ["GMV/订单数", "净销售额/订单数"],
                        "type": "single_select",
                    },
                ],
            },
        },
    }
    ok, errs = validate_payload(payload)
    assert ok, f"Should accept nested clarification_payload, got errs: {errs}"


def test_b71_validator_rejects_unknown_kind() -> None:
    """Unknown kinds still rejected (defensive: typo guard)."""
    payload = {
        "error": {
            "kind": "totally_made_up_kind",
            "message": "x",
        },
    }
    ok, errs = validate_payload(payload)
    assert not ok
    # Error msg mentions the rejected kind + lists valid kinds
    assert any("totally_made_up_kind" in e for e in errs)
    assert any("needs_user_input" in e for e in errs), "validator error msg should list `needs_user_input` as one of the valid kinds (close P0-CTO-1)"


def test_b71_validator_existing_kinds_still_accepted() -> None:
    """Regression: all 5 pre-B7.1 kinds still accepted."""
    for kind in ("domain_mismatch", "ambiguity", "transient", "schema_invalid", "tool_failure"):
        payload = {"error": {"kind": kind, "message": "test"}}
        ok, errs = validate_payload(payload)
        assert ok, f"Kind {kind!r} should be accepted, got errs: {errs}"


# ── AnalysisPayload type widen ──────────────────────────────────────────


def test_b71_analysis_payload_error_accepts_nested_dict() -> None:
    """AnalysisPayload dataclass field accepts dict with nested values
    (post-widen). The dataclass itself doesn't enforce field types at runtime,
    but we verify the annotation is updated (compatible with 类型检查器/pylance).
    """
    # Construct with nested clarification_payload
    p = AnalysisPayload(
        title="bail",
        exec_summary="",
        kpis=[],
        insights=[],
        recommendations=[],
        error={
            "kind": "needs_user_input",
            "message": "pick one",
            "clarification_payload": {
                "questions": [{"question": "q?", "options": ["a", "b"], "type": "single_select"}],
            },
        },
    )
    assert p.error is not None
    assert p.error["kind"] == "needs_user_input"
    # The nested clarification_payload is a dict (Any value, not str)
    assert isinstance(p.error.get("clarification_payload"), dict)
    nested = p.error["clarification_payload"]
    assert isinstance(nested["questions"], list)


def test_b71_analysis_payload_error_type_annotation_widened() -> None:
    """Confirm `error` field's annotation widened from `dict[str, str]`
    to `dict[str, Any]`. Inspects the source-level dataclass field annotation."""
    import inspect

    src = inspect.getsource(AnalysisPayload)
    # 2026-07-14: annotations no longer use string quotes (equivalent under future annotations); update the pin to stop requiring quotes.
    # The old (narrow) annotation should be gone
    assert "dict[str, str] | None" not in src
    # And the widened annotation present
    assert "dict[str, Any] | None" in src


# ── Cross-module propagation ────────────────────────────────────────────


def test_b71_payload_auto_fix_imports_valid_bail_kinds() -> None:
    """payload_auto_fix.py imports _VALID_BAIL_KINDS — automatically picks up
    the new kind via the set (no separate hardcoded list there)."""
    # Verify the module imports _VALID_BAIL_KINDS at module level
    import inspect

    from bowhead_core.payload_auto_fix import _fix_bail_kind  # noqa: F401

    from bowhead_core import payload_auto_fix

    src = inspect.getsource(payload_auto_fix)
    assert "from bowhead_core.analysis_payload import (" in src
    assert "_VALID_BAIL_KINDS" in src


def test_b71_fix_bail_kind_does_not_coerce_needs_user_input() -> None:
    """`_fix_bail_kind` should leave `needs_user_input` alone (it's in the
    valid set; R4 only coerces UNKNOWN kinds to `tool_failure`)."""
    from bowhead_core.payload_auto_fix import FixRecord, _fix_bail_kind

    payload = {"error": {"kind": "needs_user_input", "message": "ok"}}
    fixes: list[FixRecord] = []
    _fix_bail_kind(payload, fixes)
    # No coercion: kind stays
    assert payload["error"]["kind"] == "needs_user_input"
    # No fix record emitted (kind is valid)
    assert len(fixes) == 0


def test_b71_fix_bail_kind_still_coerces_unknown() -> None:
    """Regression: `_fix_bail_kind` still coerces TRULY unknown kinds to
    `tool_failure`. Only `needs_user_input` (now valid) is exempt."""
    from bowhead_core.payload_auto_fix import FixRecord, _fix_bail_kind

    payload = {"error": {"kind": "made_up_xyz", "message": "ok"}}
    fixes: list[FixRecord] = []
    _fix_bail_kind(payload, fixes)
    assert payload["error"]["kind"] == "tool_failure"
    assert len(fixes) == 1
    assert fixes[0].rule == "R4"
