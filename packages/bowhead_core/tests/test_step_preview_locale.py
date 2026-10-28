"""Localize display names in step previews without rewriting query identifiers."""

from __future__ import annotations

from dataclasses import dataclass, field

from bowhead_core.step_preview_locale import (
    build_locale_relabel_map,
    localize_step_preview,
    relabel_query_preview,
)


@dataclass
class _Meta:
    names: list[str]
    display_names: dict[str, str] = field(default_factory=dict)


@dataclass
class _Schema:
    measures: list[_Meta] = field(default_factory=list)
    dimensions: list[_Meta] = field(default_factory=list)


class _Cache:
    def __init__(self, schema: _Schema | None) -> None:
        self._schema = schema
        self.calls = 0

    def get(self, ws: str, *, count_stats: bool = True) -> _Schema | None:
        self.calls += 1
        assert count_stats is False, "展示层读取不该污染取数路径的命中率指标"
        return self._schema


def _localized_schema() -> _Schema:
    return _Schema(
        measures=[
            _Meta(["内销销售金额"], {"en-US": "In-mkt Sales Value", "zh-CN": "内销销售金额"}),
            _Meta(["内销指标金额"], {}),  # This synthetic metric intentionally lacks an English label.
            _Meta(["法定工作日天数"], {"en-US": "Workday Day Count"}),
        ],
        dimensions=[
            _Meta(["一级品牌名称英文"], {"en-US": "Product"}),
            _Meta(["渠道"], {"en-US": "Channel"}),
        ],
    )


# ─────────── Descending length: correctness, not optimization ───────────


def test_longer_name_wins_over_its_own_substring() -> None:
    """`内销指标金额` 是 `内销指标金额达成率` 的子串——短的先替换会把长的切坏。"""
    schema = _Schema(
        measures=[
            _Meta(["内销指标金额"], {"en-US": "Target Value"}),
            _Meta(["内销指标金额达成率"], {"en-US": "Target Achi"}),
        ],
    )
    pairs = build_locale_relabel_map(schema, "en-US")
    assert [n for n, _ in pairs][0] == "内销指标金额达成率"  # Descending order
    assert relabel_query_preview("2026年8月 内销指标金额达成率", pairs) == "2026年8月 Target Achi"
    assert relabel_query_preview("2026年8月 内销指标金额", pairs) == "2026年8月 Target Value"


def test_replacement_output_is_not_rematched() -> None:
    """换出来的英文名不该被后续规则二次匹配。"""
    schema = _Schema(measures=[_Meta(["销售金额"], {"en-US": "Sales Value"}), _Meta(["Sales"], {"en-US": "X"})])
    out = relabel_query_preview("2026年 销售金额", build_locale_relabel_map(schema, "en-US"))
    assert out == "2026年 Sales Value"


# ─────────── Actual shape ───────────


def test_localized_query_line() -> None:
    pairs = build_locale_relabel_map(_localized_schema(), "en-US")
    assert relabel_query_preview("2026年1-8月 内销销售金额 每月", pairs) == "2026年1-8月 In-mkt Sales Value 每月"
    assert relabel_query_preview("2026年1-8月 一级品牌名称英文 内销销售金额 同比", pairs) == "2026年1-8月 Product In-mkt Sales Value 同比"


def test_measure_without_en_label_is_left_alone() -> None:
    """`内销指标金额` 没配 en-US —— 保持原文，不许瞎编一个英文名。"""
    pairs = build_locale_relabel_map(_localized_schema(), "en-US")
    assert relabel_query_preview("2026年8月 内销指标金额", pairs) == "2026年8月 内销指标金额"


def test_display_same_as_canonical_is_not_replaced() -> None:
    pairs = build_locale_relabel_map(_localized_schema(), "zh-CN")
    assert all(n != d for n, d in pairs)


# ─────────── Entry-point guards ───────────


def test_zh_locale_keeps_the_query_verbatim() -> None:
    """中文轮不换——规范名本来就是中文，换了反而丢掉可核对性。"""
    cache = _Cache(_localized_schema())
    out = localize_step_preview("2026年 内销销售金额", schema_cache=cache, datarc_workspace_id="1", locale="zh-CN")
    assert out == "2026年 内销销售金额"
    assert cache.calls == 0  # Must not even read cache.


def test_cache_miss_returns_text_and_never_fetches() -> None:
    """未命中 → 原样返回。绝不为了展示在流式路径上发请求。"""
    cache = _Cache(None)
    out = localize_step_preview("2026年 内销销售金额", schema_cache=cache, datarc_workspace_id="1", locale="en-US")
    assert out == "2026年 内销销售金额"
    assert cache.calls == 1  # One cache read, no fetch branch


def test_missing_workspace_or_cache_is_a_noop() -> None:
    txt = "2026年 内销销售金额"
    assert localize_step_preview(txt, schema_cache=None, datarc_workspace_id="1", locale="en-US") == txt
    assert localize_step_preview(txt, schema_cache=_Cache(_localized_schema()), datarc_workspace_id="", locale="en-US") == txt
    assert localize_step_preview(txt, schema_cache=_Cache(_localized_schema()), datarc_workspace_id="1", locale="") == txt


def test_broken_cache_never_breaks_the_step() -> None:
    class _Boom:
        def get(self, ws: str, *, count_stats: bool = True) -> None:
            raise RuntimeError("cache exploded")

    txt = "2026年 内销销售金额"
    assert localize_step_preview(txt, schema_cache=_Boom(), datarc_workspace_id="1", locale="en-US") == txt


def test_short_names_are_skipped() -> None:
    """双字名在自然语言串里会命中无关片段，误伤大于收益。"""
    schema = _Schema(dimensions=[_Meta(["每月"], {"en-US": "Monthly"})])
    assert build_locale_relabel_map(schema, "en-US") == []


# Clarification option display names.


def _partially_localized_dims() -> _Schema:
    """Synthetic dimensions include one missing and one configured English label."""
    return _Schema(
        dimensions=[
            _Meta(["一级品牌名称英文"], {"en-US": "Brand"}),
            _Meta(["品规"], {}),
            _Meta(["产品组"], {"en-US": "Product Group"}),
            _Meta(["市场分类一级"], {"en-US": "Market L1"}),
        ],
    )


async def test_option_labels_are_positional_and_keep_unmapped_names() -> None:
    """长度必等、逐位对应；没配 en-US 的那项保持中文（中英混排是对的，不许编）。"""
    from bowhead_core.step_preview_locale import localize_option_labels

    opts = ["一级品牌名称英文", "品规", "产品组", "市场分类一级"]
    out = await localize_option_labels(opts, schema_cache=_Cache(_partially_localized_dims()), datarc_workspace_id="1", locale="en-US")
    assert out == ["Brand", "品规", "Product Group", "Market L1"]


async def test_option_labels_exact_match_not_substring() -> None:
    """模型可能写出 schema 里没有的串——精确匹配，不做子串替换，否则切成半中半英。"""
    from bowhead_core.step_preview_locale import localize_option_labels

    # ⚠ Probe must prefix a name **with en-US configured**, or substring replacement equals exact matching,
    # making the case nondiscriminating (first version used unlabeled `品规`, so mutations still passed).
    out = await localize_option_labels(
        ["产品组 (PG)", "产品组"], schema_cache=_Cache(_partially_localized_dims()), datarc_workspace_id="1", locale="en-US"
    )
    assert out == ["产品组 (PG)", "Product Group"], "子串替换会把第一项切成 'Product Group (PG)'"


async def test_option_labels_none_when_nothing_changes() -> None:
    """一项都换不动 → None，调用方据此不发这个字段（前端落回 options）。"""
    from bowhead_core.step_preview_locale import localize_option_labels

    cache = _Cache(_partially_localized_dims())
    assert await localize_option_labels(["品规"], schema_cache=cache, datarc_workspace_id="1", locale="en-US") is None
    # Chinese turn short-circuits without even reading cache.
    zh_cache = _Cache(_partially_localized_dims())
    assert await localize_option_labels(["产品组"], schema_cache=zh_cache, datarc_workspace_id="1", locale="zh-CN") is None
    assert zh_cache.calls == 0


async def test_option_labels_never_raise() -> None:
    from bowhead_core.step_preview_locale import localize_option_labels

    class _Boom:
        def get(self, ws: str, *, count_stats: bool = True) -> None:
            raise RuntimeError("boom")

    assert await localize_option_labels(["产品组"], schema_cache=_Boom(), datarc_workspace_id="1", locale="en-US") is None
    assert await localize_option_labels([], schema_cache=_Cache(_partially_localized_dims()), datarc_workspace_id="1", locale="en-US") is None
