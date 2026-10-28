"""ξ A1 + A3 — datarc_client defensive cast + actionable error msgs."""

from __future__ import annotations

from bowhead_bi_bridge.datarc_client import DatarcClient


def test_a1_parse_attribute_skips_malformed_row() -> None:
    """A1: datarc 返 string in numeric slot → 跳过该 row, 不 ValueError crash."""
    data = {
        "title": "test",
        "measureDelta": 100.0,
        "measureDimensionMaxDelta": 50.0,
        "rows": [
            # row 0: valid
            {"rank": 1, "dimension": "公司A", "attr": "饮料", "measurePrevious": 100, "measureCurrent": 110},
            # row 1: measureCurrent is a string (datarc bug: dim name leaked into numeric slot)
            {"rank": 2, "dimension": "公司B", "attr": "肉类", "measurePrevious": 200, "measureCurrent": "光明国际饮料有限公司"},
            # row 2: also valid
            {"rank": 3, "dimension": "公司C", "attr": "乳制品", "measurePrevious": 300, "measureCurrent": 320},
        ],
    }
    result = DatarcClient._parse_attribute(data)
    # 2 valid rows kept, 1 malformed skipped (no crash)
    assert len(result.rows) == 2
    assert result.rows[0].dimension == "公司A"
    assert result.rows[1].dimension == "公司C"


def test_a1_parse_attribute_safe_cast_top_level() -> None:
    """A1: measureDelta string also safe-casts."""
    data = {
        "title": "test",
        "measureDelta": "not a number",  # garbage from datarc
        "measureDimensionMaxDelta": None,
        "rows": [],
    }
    # Should NOT raise
    result = DatarcClient._parse_attribute(data)
    assert result.measure_delta == 0.0
    assert result.measure_dimension_max_delta == 0.0


def test_a1_parse_attribute_none_handled() -> None:
    """Empty/None data safely returns empty result."""
    result = DatarcClient._parse_attribute({"rows": []})
    assert result.rows == []
    assert result.measure_delta == 0.0
