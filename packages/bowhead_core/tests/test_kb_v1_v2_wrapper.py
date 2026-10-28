"""kb-knowledge-tables v2 Phase A Step 2: V1 → v2 wrapper UT.

Per docs/kb-knowledge-tables-v2-tech-design.md §A.2 + §A.7. Covers:
  - _v1_value_to_normalized: numeric / None / Chinese-currency string / unparseable
  - _wrap_v1_to_v2: single value → 1 variant, alternative_value → 2 variants, dedup
  - wrap_extractor_result_to_variants: fresh extract → 1-variant primary, tier1 (not _legacy)
  - Idempotency under repeated wrap, partial-shape robustness
"""

from __future__ import annotations

from bowhead_core.kb.doc_type_store import ExtractField
from bowhead_core.kb.extracted_record_store import (
    _build_field_type_map,
    _v1_value_to_normalized,
    _wrap_v1_to_v2,
    wrap_extractor_result_to_variants,
)

# ─── _v1_value_to_normalized ─────────


def test_v1_value_to_normalized_none_returns_none():
    """None value → normalized=None."""
    assert _v1_value_to_normalized(None, "currency") is None
    assert _v1_value_to_normalized(None, "string") is None


def test_v1_value_to_normalized_numeric_yields_amount_string():
    """int/float value + currency type → {amount: str, currency: None}.
    Decimal-as-string per PRD §A.1 (avoid float precision loss)."""
    assert _v1_value_to_normalized(5_000_000, "currency") == {
        "amount": "5000000",
        "currency": None,
    }
    # Float input gets coerced through _coerce_value, integer-valued floats
    # come back as int (per _coerce_value:184-186 `n.is_integer() → int(n)`).
    assert _v1_value_to_normalized(123.45, "number") == {
        "amount": "123.45",
        "currency": None,
    }


def test_v1_value_to_normalized_chinese_currency_via_coerce_value():
    """V1 string "人民币伍佰万元" / "5,000,000元" + currency schema → numeric coerce
    via _coerce_value reuse (not raw_string fallback). I-A6: no destructive
    normalize, but successful parse should produce {amount, currency}."""
    # _coerce_value strips ¥/RMB/,/元/万 then parses; "万元" →×10000.
    result = _v1_value_to_normalized("500万元", "currency")
    assert result == {"amount": "5000000", "currency": None}, f"expected amount=5000000 (500×10000), got {result!r}"
    # Plain comma-separated should also coerce
    result2 = _v1_value_to_normalized("5,000,000", "number")
    assert result2 == {"amount": "5000000", "currency": None}


def test_v1_value_to_normalized_unparseable_string_falls_back_to_raw_string():
    """Per I-A6 — never destructive normalize. String that _coerce_value can't
    parse stays as raw_string."""
    result = _v1_value_to_normalized("完全无法识别的内容", "currency")
    assert result == {"raw_string": "完全无法识别的内容"}, f"expected raw_string fallback, got {result!r}"


def test_v1_value_to_normalized_partial_numeric_string_falls_back_to_raw_string():
    """Per I-A6 — partial-numeric strings (LLM mid-emit "约100元" / "大约 5000")
    must NOT become confirmed amounts. _coerce_value's _NUM_RE happily grabs the
    digit run, but the residual non-numeric chars ("约" / "大约") signal
    uncertainty. Wrapper must refuse the coerce + fall back to raw_string.

    Step 2 r2 review caught this (Eng-P0): without residual-guard, "约100元"
    becomes {amount: "100"} — a destructive normalize that violates I-A6.
    """
    cases = [
        "约100元",  # Estimate prefix.
        "大约 5000",  # Space + estimate prefix.
        "100 (估算)",  # parenthetical suffix
        "RMB 500万 含税",  # Extra "含税" suffix.
    ]
    for val in cases:
        result = _v1_value_to_normalized(val, "currency")
        assert result == {"raw_string": val}, f"{val!r}: expected raw_string fallback, got {result!r}"


def test_v1_value_to_normalized_string_type_keeps_value():
    """type=string → {value: str(val)}, not amount/raw_string."""
    assert _v1_value_to_normalized("ACME Corp", "string") == {"value": "ACME Corp"}
    assert _v1_value_to_normalized("2024-01-15", "date") == {"value": "2024-01-15"}


# ─── _build_field_type_map ─────────


def test_build_field_type_map_handles_dataclass_and_dict():
    """Helper accepts both ExtractField objects and plain dicts."""
    fields = [
        ExtractField(name="合同金额", type="currency"),
        {"name": "起始日", "type": "date"},
        ExtractField(name="客户名"),  # no type → default "string"
    ]
    m = _build_field_type_map(fields)
    assert m == {"合同金额": "currency", "起始日": "date", "客户名": "string"}


def test_build_field_type_map_empty_or_none():
    assert _build_field_type_map(None) == {}
    assert _build_field_type_map([]) == {}


# ─── _wrap_v1_to_v2 (legacy lazy-migrate read path) ─────────


def test_v1_single_value_wraps_to_one_variant():
    """Basic V1 dict[str, dict] → dict[str, list[variant]] with primary=true."""
    v1 = {
        "合同金额": {
            "value": 5_000_000,
            "source_snippet": "总金额 5,000,000",
            "source_page": 3,
            "confidence": 0.92,
        }
    }
    fields = [ExtractField(name="合同金额", type="currency")]
    out = _wrap_v1_to_v2(v1, fields)
    assert "合同金额" in out
    variants = out["合同金额"]
    assert len(variants) == 1
    v = variants[0]
    assert v["is_primary"] is True
    assert v["extracted_by"] == "tier1_legacy"
    assert v["normalized"] == {"amount": "5000000", "currency": None}
    assert v["confidence"] == 0.92
    assert v["source_excerpt"] == "总金额 5,000,000"
    assert v["page"] == 3
    assert v["chunk_id"] is None  # §A.3 deferred


def test_v1_alternative_preserved_as_second_variant_when_distinct():
    """V1 alternative_value ≠ value → 2 variants, alt is_primary=false,
    extracted_by=tier1_legacy_alt."""
    v1 = {
        "合同金额": {
            "value": 5_000_000,
            "alternative_value": "5,000,000.00",  # different string repr
            "source_snippet": "...",
            "source_page": 1,
            "confidence": 0.85,
        }
    }
    fields = [ExtractField(name="合同金额", type="currency")]
    out = _wrap_v1_to_v2(v1, fields)
    variants = out["合同金额"]
    assert len(variants) == 2, f"expected 2 variants, got {len(variants)}"
    primary, alt = variants
    assert primary["is_primary"] is True
    assert primary["normalized"] == {"amount": "5000000", "currency": None}
    assert alt["is_primary"] is False
    assert alt["extracted_by"] == "tier1_legacy_alt"
    # "5,000,000.00" through _coerce_value → strips comma + .00 → 5000000
    assert alt["normalized"] == {"amount": "5000000", "currency": None}


def test_v1_alternative_dedup_when_equal_to_value():
    """If alternative_value == value, do not duplicate as 2nd variant."""
    v1 = {
        "合同金额": {
            "value": 5_000_000,
            "alternative_value": 5_000_000,  # identical
            "confidence": 0.9,
        }
    }
    out = _wrap_v1_to_v2(v1, [ExtractField(name="合同金额", type="currency")])
    assert len(out["合同金额"]) == 1


def test_v1_string_in_currency_schema_falls_back_safe():
    """V1 stored a string value (legacy LLM mishap) under a currency schema.
    Wrapper must not crash + must preserve the value via _coerce_value or
    raw_string per I-A6."""
    v1 = {
        "合同金额": {
            "value": "RMB 500万",  # Chinese currency string
            "source_snippet": "...",
            "confidence": 0.7,
        }
    }
    out = _wrap_v1_to_v2(v1, [ExtractField(name="合同金额", type="currency")])
    variant = out["合同金额"][0]
    # "RMB 500万" through _coerce_value strips RMB + 万 multiplier → 5,000,000
    assert variant["normalized"] == {"amount": "5000000", "currency": None}
    assert variant["extracted_by"] == "tier1_legacy"


def test_v1_orphan_field_no_schema_default_string():
    """V1 row had a field that's no longer in current schema → type_hint defaults
    to 'string', no destructive numeric coerce."""
    v1 = {
        "已废弃字段": {
            "value": "some text",
            "confidence": 0.6,
        }
    }
    # doc_type_fields=None — no schema lookup
    out = _wrap_v1_to_v2(v1, None)
    variant = out["已废弃字段"][0]
    assert variant["normalized"] == {"value": "some text"}
    assert variant["extracted_by"] == "tier1_legacy"


def test_v1_malformed_field_value_skipped():
    """V1 row with non-dict field value (e.g., raw int) gets skipped silently
    (defensive — never crash)."""
    v1 = {
        "正常字段": {"value": 100, "confidence": 0.9},
        "畸形字段": "this should be a dict",  # malformed
    }
    out = _wrap_v1_to_v2(v1, [ExtractField(name="正常字段", type="number")])
    assert "正常字段" in out
    assert "畸形字段" not in out


# ─── wrap_extractor_result_to_variants (fresh extract / worker boundary) ──


def test_extractor_result_wraps_to_one_variant_tier1_not_legacy():
    """Fresh extract output → 1 variant per field, primary=true, extracted_by=tier1
    (NOT tier1_legacy — that's for V1 lazy-migrate path only).
    §A.7 spec."""
    extractor_out = {
        "合同金额": {
            "value": 5_000_000,
            "source_snippet": "总金额 5,000,000",
            "source_page": 2,
            "confidence": 0.95,
        },
        "起始日": {
            "value": "2024-01-15",
            "source_snippet": "自 2024-01-15 起",
            "source_page": 1,
            "confidence": 0.90,
        },
    }
    fields = [
        ExtractField(name="合同金额", type="currency"),
        ExtractField(name="起始日", type="date"),
    ]
    out = wrap_extractor_result_to_variants(extractor_out, fields)
    assert set(out.keys()) == {"合同金额", "起始日"}
    for fname in ("合同金额", "起始日"):
        variants = out[fname]
        assert len(variants) == 1, f"{fname}: expected 1 variant"
        v = variants[0]
        assert v["is_primary"] is True
        assert v["extracted_by"] == "tier1", f"{fname}: fresh extract must be tier1, got {v['extracted_by']!r}"
        assert v["chunk_id"] is None  # §A.3 deferred Sprint 1


def test_extractor_result_normalized_uses_doc_type_type_hint():
    """Worker-boundary wrapper passes doc_type_fields → type_hint controls
    numeric vs string normalization. Currency field gets {amount} shape;
    same value under string field gets {value} shape."""
    out_currency = wrap_extractor_result_to_variants(
        {"x": {"value": 5_000_000, "confidence": 0.9}},
        [ExtractField(name="x", type="currency")],
    )
    out_string = wrap_extractor_result_to_variants(
        {"x": {"value": 5_000_000, "confidence": 0.9}},
        [ExtractField(name="x", type="string")],
    )
    assert out_currency["x"][0]["normalized"] == {"amount": "5000000", "currency": None}
    # String type_hint with numeric val → str(val) under .value
    assert out_string["x"][0]["normalized"] == {"value": "5000000"}


# ─── idempotency / robustness ─────────


def test_wrap_v1_empty_input_returns_empty():
    """Empty in → empty out, no crash."""
    assert _wrap_v1_to_v2({}, []) == {}
    assert wrap_extractor_result_to_variants({}, []) == {}


def test_wrap_v1_partial_null_value_filtered_correctly():
    """V1 row with {value: None} (partial LLM emit) → variant.normalized=None.
    Downstream COALESCE filters this out for numeric WHERE clauses (verified in
    Step 4 query_by_field UT)."""
    v1 = {"合同金额": {"value": None, "confidence": 0.0}}
    out = _wrap_v1_to_v2(v1, [ExtractField(name="合同金额", type="currency")])
    variant = out["合同金额"][0]
    assert variant["normalized"] is None
    assert variant["confidence"] == 0.0
