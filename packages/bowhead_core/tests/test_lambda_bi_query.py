"""Sprint λ — bi_query F1 (cache) + F3 (fast resolver) + F2 (schema) tests.

Closes PRD r1 dogfood gates 1-5:
    sub-query unique rate ≥ 95% / `fast=True` count 0 / `text=` count 0 / no error poisoning
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_registry import (
    _TURN_QUERY_CACHE,
    _TURN_QUERY_CACHE_LRU,
    _bi_cache_get,
    _bi_cache_key,
    _bi_cache_put,
    _bi_result_is_error,
    _normalize_query_for_cache,
    cleanup_turn_query_cache,
)

# bi-hitl-cleanup P13.5 (2026-05-21): _has_group_by_hint / _resolve_fast removed
# (Fast path removed; unified _fetch_rows). F3 tests below also deleted.


@pytest.fixture(autouse=True)
def _clean_cache():
    """Reset module-level cache between tests."""
    _TURN_QUERY_CACHE.clear()
    _TURN_QUERY_CACHE_LRU.clear()
    yield
    _TURN_QUERY_CACHE.clear()
    _TURN_QUERY_CACHE_LRU.clear()


# ─────────────────────────────────────────────────────────────────
# F1 — _normalize_query_for_cache
# ─────────────────────────────────────────────────────────────────


def test_normalize_collapses_whitespace_and_punct():
    assert _normalize_query_for_cache("  今年  库存  周转率 同比  ?") == "今年 库存 周转率 同比"


def test_normalize_strip_trailing_question_mark():
    assert _normalize_query_for_cache("今年净营业额?") == "今年净营业额"
    assert _normalize_query_for_cache("今年净营业额？") == "今年净营业额"
    assert _normalize_query_for_cache("今年净营业额。") == "今年净营业额"


def test_normalize_full_width_space_collapsed():
    # full-width 　 should normalize to single ASCII space
    assert _normalize_query_for_cache("a　b") == "a b"


def test_normalize_lowercase_for_case_insensitive_match():
    assert _normalize_query_for_cache("Top 10") == _normalize_query_for_cache("top 10")


# ─────────────────────────────────────────────────────────────────
# F1 — _bi_cache_key
# ─────────────────────────────────────────────────────────────────


def test_cache_key_differs_on_fast():
    k1 = _bi_cache_key("today gmv", True, None, None)
    k2 = _bi_cache_key("today gmv", False, None, None)
    assert k1 != k2


def test_cache_key_differs_on_limit():
    k1 = _bi_cache_key("rows q", False, 10, None)
    k2 = _bi_cache_key("rows q", False, 20, None)
    assert k1 != k2


def test_cache_key_differs_on_columns():
    k1 = _bi_cache_key("q", False, None, ["a", "b"])
    k2 = _bi_cache_key("q", False, None, ["a", "c"])
    assert k1 != k2


def test_cache_key_columns_order_invariant():
    k1 = _bi_cache_key("q", False, None, ["a", "b"])
    k2 = _bi_cache_key("q", False, None, ["b", "a"])
    assert k1 == k2  # sorted in key


def test_cache_key_differs_on_inline_max_rows():
    """P1-Eng-1: cache key must distinguish inline_max_rows (different LLM-visible truncation)."""
    k1 = _bi_cache_key("q", False, None, None, inline_max_rows=10)
    k2 = _bi_cache_key("q", False, None, None, inline_max_rows=30)
    assert k1 != k2


def test_cache_key_differs_on_mode():
    """P1-Eng-1: cache key must distinguish mode (different datarc question/ interpretation)."""
    k1 = _bi_cache_key("q", True, None, None, mode="analyze")
    k2 = _bi_cache_key("q", True, None, None, mode="display")
    assert k1 != k2


def test_cache_key_inline_and_mode_default_none_equivalent():
    """Both None and absent should produce same key (backward-compat)."""
    k1 = _bi_cache_key("q", False, None, None)
    k2 = _bi_cache_key("q", False, None, None, inline_max_rows=None, mode=None)
    assert k1 == k2


def test_cache_key_normalize_collapses_dup():
    k1 = _bi_cache_key("  今年 GMV  同比  ", False, None, None)
    k2 = _bi_cache_key("今年 gmv 同比", False, None, None)
    assert k1 == k2


# ─────────────────────────────────────────────────────────────────
# F1 — cache get/put/cleanup
# ─────────────────────────────────────────────────────────────────


def test_cache_miss_then_hit_returns_same_value():
    _bi_cache_put("t1", "k1", {"value": 42})
    assert _bi_cache_get("t1", "k1") == {"value": 42}


def test_cache_isolates_turns():
    _bi_cache_put("t1", "k1", {"a": 1})
    assert _bi_cache_get("t2", "k1") is None


def test_cache_miss_returns_none():
    assert _bi_cache_get("nonexistent", "anything") is None


def test_cleanup_turn_query_cache_drops_bucket_and_lru():
    _bi_cache_put("t1", "k1", {"a": 1})
    _bi_cache_put("t1", "k2", {"b": 2})
    dropped = cleanup_turn_query_cache("t1")
    assert dropped == 2
    assert _bi_cache_get("t1", "k1") is None
    assert "t1" not in _TURN_QUERY_CACHE_LRU


def test_cleanup_idempotent():
    _bi_cache_put("t1", "k1", {"a": 1})
    cleanup_turn_query_cache("t1")
    # second call returns 0, no exception
    assert cleanup_turn_query_cache("t1") == 0
    assert cleanup_turn_query_cache("never_existed") == 0


def test_fifo_evicts_oldest_turn_after_max(monkeypatch):
    import bowhead_core.tool_registry as tr

    monkeypatch.setattr(tr, "_TURN_QUERY_CACHE_MAX_TURNS", 3)
    for i in range(5):
        _bi_cache_put(f"turn{i}", "k", {"v": i})
    # Only last 3 should remain
    assert _bi_cache_get("turn0", "k") is None
    assert _bi_cache_get("turn1", "k") is None
    assert _bi_cache_get("turn2", "k") == {"v": 2}
    assert _bi_cache_get("turn4", "k") == {"v": 4}


# ─────────────────────────────────────────────────────────────────
# F1 — error result skip (P0-1)
# ─────────────────────────────────────────────────────────────────


def test_bi_result_is_error_detects_error_kind():
    assert _bi_result_is_error({"error_kind": "datarc_500", "rows": []}) is True


def test_bi_result_is_error_detects_error_code():
    assert _bi_result_is_error({"error_code": "timeout"}) is True


def test_bi_result_is_error_detects_error_field():
    assert _bi_result_is_error({"error": "boom"}) is True


def test_bi_result_is_error_ok_result():
    assert _bi_result_is_error({"rows": [{"a": 1}], "artifact_id": "x"}) is False


def test_bi_result_is_error_non_dict():
    # Defensive: non-dict (legacy edge case) treated as non-error to allow caching
    assert _bi_result_is_error("string-result") is False
    assert _bi_result_is_error([1, 2, 3]) is False


# ─────────────────────────────────────────────────────────────────
# F2/F3 — bi_query schema
# ─────────────────────────────────────────────────────────────────


def test_bi_query_schema_has_no_text_or_fast():
    """λ-F2 + F3: schema must not expose `text` or `fast` properties to LLM."""
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    spec = reg._specs.get("bi_query")
    assert spec is not None
    props = spec.schema.input_schema["properties"]
    assert "text" not in props, "λ-F2: schema must drop `text` property"
    assert "fast" not in props, "λ-F3: schema must drop `fast` property"
    # queries still required
    assert "queries" in props
    assert spec.schema.input_schema.get("required") == ["queries"]


def test_bi_query_description_drops_fast_mention():
    """Description must not encourage `fast=True` usage.

    Sprint bi-hitl-cleanup B2 (2026-05-21) 瘦身: 删除 auto-detect 段, 旧的
    auto-detect 期望同时移除 — 核心 invariant 仍是 "无 fast=True 字样"。
    """
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    desc = reg._specs["bi_query"].schema.description
    # The old prompt using fast=True must be gone.
    assert "fast=True" not in desc
    assert "fast=true" not in desc


# ─────────────────────────────────────────────────────────────────
# F4 — Lead prompt PLAN_STAGE_DISCIPLINE
# ─────────────────────────────────────────────────────────────────


def test_lead_prompt_no_longer_carries_plan_stage_discipline():
    """λ-F4 的 `<plan_stage_discipline>` 已于 2026-08-21 **有意从 Lead prompt 删除**。

    原用例是「防 silent removal」的 lint（PLAN directive must exist）。删除不是 silent：
    该段整段讲「怎么编排 bi_query 的批次」，而 Lead 交出取数权转正后它没有 bi_query，
    留着就是教模型用一个不存在的工具。

    ⚠ 逐句对表（别当成整段等价搬家）：compute.md 有 batch 本身的纪律
    （`一次 batch ≤20 条, 别逐条串行`、`多 metric 必 batch`），但**没有**
    「禁 reactive 分批补充」的逐字对应；该句要不要补进 compute.md 归
    compute-agent-optimize 线定。本用例翻面保留，钉住「删除是决定，不是事故」——
    有人把它加回 Lead prompt 时会变红，届时应连同这段注释一起重新审。
    """
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt

    prompt = build_lead_system_prompt(workspace_id="test-ws", facts=[])
    assert "<plan_stage_discipline>" not in prompt
    assert "禁止 reactive 分批补充" not in prompt
    # Pin the removal **reason** too: Lead must no longer teach bi_query batching.
    assert "bi_query(queries=[" not in prompt


def test_section_ordering_around_the_removed_plan_block():
    """Section ordering invariant (P1-10)，减去已删除的那一段。

    原断言是 `parallel < plan_stage < refusal`。`<plan_stage_discipline>` 已于
    2026-08-21 随交权转正删除（见同文件 test_lead_prompt_no_longer_carries_...），
    删掉这条会连带丢掉**整个排序不变量**，所以改成钉住剩下那段真实的相邻关系：
    并行段 → 数据引用铁律 → 拒答与安全。
    """
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt

    prompt = build_lead_system_prompt(workspace_id="test-ws", facts=[])
    par_idx = prompt.find("<parallel_tool_calls>")
    data_ref_idx = prompt.find("<data_reference_rules>")
    refusal_idx = prompt.find("<refusal_and_safety>")
    assert par_idx != -1 and data_ref_idx != -1 and refusal_idx != -1
    assert par_idx < data_ref_idx < refusal_idx
    assert prompt.find("<plan_stage_discipline>") == -1


def test_repeat_guard_tells_the_model_to_stop_asking_the_same_thing():
    """★★ 同一轮里同参数反复查，从第 3 次起结果里明说「再查也不会变」。

    116 实测 `t_a091c1b59a78`：同一参数的 `bi_query` **连发 16 次**，51 秒的轮次几乎全耗在这。
    缓存已经挡住了重复取数（21 次调用只落了 5 个 artifact、18 次命中缓存），挡不住的是
    **循环本身**——每次重试都要一个完整的 LLM 往返。模型缺的不是数据，是这句话。
    """
    from bowhead_core.tool_registry import _TURN_QUERY_CACHE, _bi_bump_repeat, _repeat_guarded

    tid, key = "t_repeat", "k1"
    _TURN_QUERY_CACHE[tid] = {}
    base = {"rows": [{"a": 1}], "row_count": 358, "_preflight_hint": "⚠ 只给你看了 10 行…"}

    first = _repeat_guarded(base, _bi_bump_repeat(tid, key))
    assert "完全相同的参数" not in str(first.get("_preflight_hint")), "第 2 次调用还不算打转"

    second = _repeat_guarded(base, _bi_bump_repeat(tid, key))
    hint = str(second.get("_preflight_hint"))
    assert "完全相同的参数" in hint and "不会有任何变化" in hint, hint
    assert "只给你看了 10 行" in hint, "原有提示不能被顶掉"
    assert "python_sandbox" in hint, "要给出口"

    # ★ Must be a copy: writing this into the cached object makes the next hit "state history".
    assert "完全相同的参数" not in str(base.get("_preflight_hint")), "污染了缓存对象"

    third = _repeat_guarded(base, _bi_bump_repeat(tid, key))
    assert "查过 4 次" in str(third.get("_preflight_hint")), "次数要往上走"

    _TURN_QUERY_CACHE.pop(tid, None)


def test_repeat_counter_lives_in_the_same_turn_bucket():
    """★ 计数寄在 `_TURN_QUERY_CACHE` 同一个桶里 —— 另起字典就要自己管 LRU 与轮末清理。"""
    from bowhead_core.tool_registry import _TURN_QUERY_CACHE, _bi_bump_repeat, cleanup_turn_query_cache

    tid = "t_bucket"
    _TURN_QUERY_CACHE[tid] = {}
    _bi_bump_repeat(tid, "k")
    assert any(k.startswith("@rep:") for k in _TURN_QUERY_CACHE[tid]), "计数没写进同一个桶"
    cleanup_turn_query_cache(tid)
    assert tid not in _TURN_QUERY_CACHE, "轮末清理要把计数一起带走"
