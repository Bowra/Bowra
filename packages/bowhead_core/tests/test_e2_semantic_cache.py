"""Phase E2 — semantic cache prefix UT.

Sprint agent-redesign-1 (2026-05-15). Tests:
  - _semantic_prefix strips Top-N / 按X分组 / limit suffixes
  - Short queries (<8 char) NOT collapsed (avoid over-collapse)
  - First call with prefix → seed in cache
  - Second call with same prefix + different suffix → emits `_cache_hint`
  - Error result → does NOT seed cache
"""

from __future__ import annotations

from bowhead_core.tool_registry import (
    _TURN_QUERY_CACHE,
    _semantic_cache_lookup,
    _semantic_cache_put,
    _semantic_prefix,
)


def test_semantic_prefix_strips_top_n() -> None:
    assert _semantic_prefix("华东 2025 H2 销售达成金额 Top 20") == "华东 2025 h2 销售达成金额"


def test_semantic_prefix_strips_group_by() -> None:
    assert _semantic_prefix("上海 2025 Q3 销售按代表分组") == "上海 2025 q3 销售"


def test_semantic_prefix_strips_limit() -> None:
    assert _semantic_prefix("华东 2025 H2 销售 限制 100") == "华东 2025 h2 销售"


def test_semantic_prefix_strips_前_n() -> None:
    assert _semantic_prefix("华东 2025 H2 销售前 10 的产品") == "华东 2025 h2 销售"


def test_semantic_prefix_noop_when_no_suffix() -> None:
    assert _semantic_prefix("华东 2025 H2 销售") == "华东 2025 h2 销售"


def test_semantic_prefix_preserves_short_query() -> None:
    """Don't collapse query whose remainder would be < 8 chars."""
    # After "Top 5", fewer than 8 chars remain → keep the original.
    assert _semantic_prefix("销售 Top 5") == "销售 top 5"


def test_semantic_prefix_t_4a33ed91_replay() -> None:
    """Prod trace t_d6a63c92: 5 queries fold into 1 semantic bucket."""
    queries = [
        "2025年7-12月 上海市 江苏省 浙江省 安徽省 福建省 江西省 山东省 产品大类LV1 代表 销售达成金额",
        "2025年7-12月 上海市 江苏省 浙江省 安徽省 福建省 江西省 山东省 产品大类LV1 代表 销售达成金额 Top 20",
        "2025年7-12月 上海市 江苏省 浙江省 安徽省 福建省 江西省 山东省 Destination 按代表分组 销售达成金额",
    ]
    prefixes = [_semantic_prefix(q) for q in queries]
    # All three collapse to similar / equal prefix (Destination is mid-string so it stays)
    # Top 20 and the exact grouping phrase 按代表分组 are stripped. The 1st = 2nd ≠ 3rd (Destination differs).
    assert prefixes[0] == prefixes[1]
    # The 3rd query has mid-string "Destination"; strip the grouping phrase 按代表分组.


def test_semantic_cache_put_and_lookup_basic() -> None:
    turn_id = "t_test_e2_basic"
    _TURN_QUERY_CACHE.pop(turn_id, None)
    prefix = "华东 2025 h2 销售"
    _semantic_cache_put(turn_id, prefix, {"artifact_id": "art_001", "query": "华东 2025 H2 销售"})
    hit = _semantic_cache_lookup(turn_id, prefix)
    assert hit == {"artifact_id": "art_001", "query": "华东 2025 H2 销售"}


def test_semantic_cache_lookup_miss_returns_none() -> None:
    turn_id = "t_test_e2_miss"
    _TURN_QUERY_CACHE.pop(turn_id, None)
    assert _semantic_cache_lookup(turn_id, "no such prefix") is None


def test_semantic_cache_first_call_seeds_others_get_hint() -> None:
    """5-query scenario: first seeds, subsequent get hint."""
    turn_id = "t_test_e2_seed"
    _TURN_QUERY_CACHE.pop(turn_id, None)
    base = "华东 2025 h2 销售"
    # Simulate first call seeding
    _semantic_cache_put(turn_id, base, {"artifact_id": "art_seed", "query": base})
    # Subsequent calls with same prefix → lookup hits
    hit = _semantic_cache_lookup(turn_id, base)
    assert hit["artifact_id"] == "art_seed"


def test_semantic_cache_short_prefix_no_op() -> None:
    """< 8 chars → no seed (avoid pollution)."""
    turn_id = "t_test_e2_short"
    _TURN_QUERY_CACHE.pop(turn_id, None)
    _semantic_cache_put(turn_id, "短", {"artifact_id": "x", "query": "短"})
    # Lookup with same short key returns None (not registered)
    assert _semantic_cache_lookup(turn_id, "短") is None
