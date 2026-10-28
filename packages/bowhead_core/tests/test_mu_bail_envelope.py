"""Sprint µ — bail_kind structured payload tests (P0-3).

Verifies validate_payload accepts the bail envelope:
    payload={"error": {"kind": "<kind>", "message": "..."}}

skipping the rest of the schema. Per discovery.md design fix #5.
"""

from __future__ import annotations

import pytest
from bowhead_core.analysis_payload import validate_payload


@pytest.mark.unit
def test_bail_envelope_domain_mismatch_accepted() -> None:
    """domain_mismatch is the canonical case (t_32f0b221 dev session)."""
    ok, errs = validate_payload(
        {
            "error": {
                "kind": "domain_mismatch",
                "message": "当前 workspace 无医院运营 dim, 现有 X/Y/Z, 建议切换 ws.",
            },
        }
    )
    assert ok is True, f"validation failed: {errs}"


@pytest.mark.unit
def test_bail_envelope_ambiguity_accepted() -> None:
    ok, errs = validate_payload(
        {
            "error": {"kind": "ambiguity", "message": "query 不清楚, 是问 A 还是 B?"},
        }
    )
    assert ok is True, errs


@pytest.mark.unit
def test_bail_envelope_all_5_kinds() -> None:
    for kind in ("domain_mismatch", "ambiguity", "transient", "schema_invalid", "tool_failure"):
        ok, errs = validate_payload(
            {
                "error": {"kind": kind, "message": "test reason"},
            }
        )
        assert ok is True, f"kind={kind}: {errs}"


@pytest.mark.unit
def test_bail_envelope_invalid_kind_rejected() -> None:
    ok, errs = validate_payload(
        {
            "error": {"kind": "fancy_kind", "message": "x"},
        }
    )
    assert ok is False
    assert any("not in" in e for e in errs)


@pytest.mark.unit
def test_bail_envelope_missing_message_rejected() -> None:
    ok, errs = validate_payload(
        {
            "error": {"kind": "domain_mismatch"},
        }
    )
    assert ok is False
    assert any("message" in e for e in errs)


@pytest.mark.unit
def test_bail_envelope_empty_message_rejected() -> None:
    ok, _errs = validate_payload(
        {
            "error": {"kind": "domain_mismatch", "message": "   "},
        }
    )
    assert ok is False


@pytest.mark.unit
def test_bail_skips_required_top_level_fields() -> None:
    """When error envelope set, title/exec_summary/etc not required."""
    # No title, no kpis, no insights — but error.kind valid → ok.
    ok, errs = validate_payload(
        {
            "error": {"kind": "transient", "message": "BI failed"},
        }
    )
    assert ok is True, errs


@pytest.mark.unit
def test_no_bail_normal_validation_runs() -> None:
    """Without error envelope, normal required-fields validation kicks in."""
    ok, errs = validate_payload({})  # empty
    assert ok is False
    assert any("missing/empty top-level" in e for e in errs)


@pytest.mark.unit
def test_bail_with_extra_fields_silently_ignored() -> None:
    """Bail envelope is the gate; extra fields don't trigger validation."""
    ok, errs = validate_payload(
        {
            "error": {"kind": "domain_mismatch", "message": "x"},
            "title": "ignored",
            "kpis": [{"label": "L", "value": "1.618 ratio"}],  # would normally be banned
        }
    )
    assert ok is True, errs  # bail envelope short-circuits ratio check
