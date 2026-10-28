"""report-consistency-gate v3 — GroundFact 台账 schema round-trip + 轻量校验。

Phase 1（schema）：dataclass + payload_from_dict 映射 + validate_payload 结构校验。
formula 重算 / unit-aware 对账是后续 phase（reconcile 层），不在此测。
"""

from bowhead_core.analysis_payload import (
    GroundFact,
    payload_from_dict,
    validate_payload,
)


def _min() -> dict:
    # No `error` key → non-bail path reaches ground_facts validation.
    return {"title": "t", "exec_summary": "s", "kpis": [], "insights": [], "recommendations": []}


def _gf_errors(d: dict) -> list[str]:
    _ok, errs = validate_payload(d)
    return [e for e in errs if "ground_facts" in e]


def test_round_trip_given_and_derived():
    d = {
        **_min(),
        "ground_facts": [
            {"key": "dep_inc", "label": "存款增量", "value": 8.26, "unit": "亿", "source": "given"},
            {"key": "avg", "label": "户均", "value": 4.23, "unit": "万元", "formula": "3.57*10000/8438"},
        ],
    }
    p = payload_from_dict(d)
    assert len(p.ground_facts) == 2
    assert isinstance(p.ground_facts[0], GroundFact)
    g0, g1 = p.ground_facts
    assert g0.value == 8.26 and g0.source == "given" and g0.formula == ""
    assert g1.formula == "3.57*10000/8438" and g1.unit == "万元"


def test_value_coercion():
    d = {
        **_min(),
        "ground_facts": [
            {"key": "a", "label": "x", "value": "4.23"},
            {"key": "b", "label": "y", "value": "4.23万"},  # Unit mixed into value string
            {"key": "c", "label": "z", "value": 1200},
            {"key": "d", "label": "w", "value": "n/a"},  # Unparseable → 0.0
            {"key": "e", "label": "v", "value": "1,234.5"},  # Thousands separator
        ],
    }
    vals = [g.value for g in payload_from_dict(d).ground_facts]
    assert vals == [4.23, 4.23, 1200.0, 0.0, 1234.5]


def test_backward_compat_absent():
    p = payload_from_dict(_min())  # Old payload lacks this field
    assert p.ground_facts == []
    assert _gf_errors(_min()) == []  # Missing is allowed (optional)


def test_validate_structural():
    base = _min()
    # F5: missing 'key' is **nonfatal** (payload_from_dict falls back to label); do not hard-fail the entire report.
    assert _gf_errors({**base, "ground_facts": [{"label": "x", "value": 1}]}) == []
    # Non-dict entries still trigger structural errors.
    assert any("must be an object" in e for e in _gf_errors({**base, "ground_facts": [{"key": "a", "value": 1}, "junk"]}))
    assert any("must be a list" in e for e in _gf_errors({**base, "ground_facts": {"key": "a"}}))
    assert any("too many" in e for e in _gf_errors({**base, "ground_facts": [{"key": f"k{i}", "value": 1} for i in range(65)]}))
    # Valid → no gf errors
    assert _gf_errors({**base, "ground_facts": [{"key": "a", "label": "x", "value": 1.0, "source": "given"}]}) == []


def test_non_dict_entries_dropped_in_round_trip():
    d = {**_min(), "ground_facts": [{"key": "a", "value": 1}, "junk", 42, None]}
    p = payload_from_dict(d)
    assert len(p.ground_facts) == 1 and p.ground_facts[0].key == "a"
