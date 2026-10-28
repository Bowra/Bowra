"""词表查找优先于距离 —— sprint concept-resolve-dim-value-2026-08 §16.

datarc 的 embed collection 里存的是**词**（一个 doc = 一个维度值/维度名），不是句子。
把「top-1 与 top-2 的距离分离度」当判据，对品牌名 / 网点编号这类不透明短串会系统性误判。

夹具里的距离全部取自 116 真机实测（`v1_demo_p1` / `v1_demo_p21`），不是编的：
- 虎牌:        top1 0.0     top2 0.2989 → 改动前后都该 EXACT
- 品牌蔬菜:     top1 0.0     top2 0.1751 → 改动前被判 unclear（假阴性）
- 239029999分行: top1 0.0    top2 0.0159 → 命名规整导致邻居极近，改动前必死
- 禽类:        top1/top2 都 1.19e-07，且跨 d55/d56 → 必须判歧义而非锁维度
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bowhead_core.concept_resolver import Pattern, classify_pattern
from bowhead_core.embed_search import EmbedHit, EmbedQueryResult

THRESHOLDS = (0.10, 0.25)  # (exact, abstract), matching production defaults


def _res(*hits: tuple[str, str, float]) -> EmbedQueryResult:
    return EmbedQueryResult(
        query="q",
        hits=[EmbedHit(doc=doc, dim_id=dim, value=doc, dist=dist) for doc, dim, dist in hits],
    )


# ── Positive cases: literal matches are unaffected by top2 ────────────────────────────


def test_literal_hit_wins_even_when_top2_is_adjacent() -> None:
    """★ 本轮核心：top2 近到 0.0159 也不该否掉一个 dist=0 的逐字命中。

    改动前 EXACT 要求 `top2.dist > 2*exact_thresh`（=0.20），银行网点名互为近邻
    （0.0159）⇒ 整批全军覆没。116 实测 114 个真实维度值里 35 个是这么丢的。
    """
    r = _res(("239029999分行", "d7", 0.0), ("239009999分行", "d7", 0.0159))
    assert classify_pattern("239029999分行", r, THRESHOLDS) is Pattern.EXACT


def test_literal_hit_beats_distance_rule_that_would_say_abstract() -> None:
    """判定顺序：字符串比较优先于距离。距离规则会判 ABSTRACT，逐字命中必须翻盘。"""
    # top1 distance far exceeds abstract_thresh(0.25); distance alone would classify as ABSTRACT.
    r = _res(("护肤品", "d56", 0.90), ("彩妆", "d56", 0.95))
    assert classify_pattern("护肤品", r, THRESHOLDS) is Pattern.EXACT


def test_literal_compare_is_normalized_both_sides() -> None:
    """归一在**比较侧双向**做：库里存 `AUM`、用户输 `aum` 必须命中。

    单边 casefold 会反伤字典里的大写原值（116 实测 464 个 doc 有 1 个是 `AUM`）。
    """
    r = _res(("AUM", "m3", 0.02), ("aum余额", "m3", 0.19))
    assert classify_pattern("aum", r, THRESHOLDS) is Pattern.EXACT
    assert classify_pattern("ＡＵＭ", r, THRESHOLDS) is Pattern.EXACT  # Full-width
    assert classify_pattern("  AUM  ", r, THRESHOLDS) is Pattern.EXACT


# ── Negative cases: guard against confidently returning the wrong dimension ──────────


def test_literal_hit_across_two_dims_is_ambiguous_not_exact() -> None:
    """★ 跨维度同名是真实存在的（116 实测：禽类 d55/d56、23区 d53/d54）。

    逐字命中此时**不能**锁维度——锁错维度比返回空糟得多。
    """
    r = _res(("禽类", "d55", 1.19e-07), ("禽类", "d56", 1.19e-07))
    assert classify_pattern("禽类", r, THRESHOLDS) is Pattern.AMBIGUOUS


def test_typo_does_not_trigger_literal_path() -> None:
    """脏输入安全性由构造保证：错字不相等 ⇒ 逐字判据天然不触发，落回原有规则。"""
    # 116 observation: 虎脾 → top1 虎都 at 0.3674 (> abstract_thresh)
    r = _res(("虎都", "d54", 0.3674), ("虎牌", "d54", 0.3697))
    assert classify_pattern("虎脾", r, THRESHOLDS) is Pattern.ABSTRACT


# ── Regression: without a literal match, classification is identical to before ────────


def test_no_hits_still_abstract() -> None:
    assert classify_pattern("任意", _res(), THRESHOLDS) is Pattern.ABSTRACT


def test_distance_exact_path_unchanged() -> None:
    """非逐字（带后缀）时仍走原距离规则：116 实测 虎牌牌子 → 0.0897 / 0.3767。"""
    r = _res(("虎牌", "d54", 0.0897), ("虎都", "d54", 0.3767))
    assert classify_pattern("虎牌牌子", r, THRESHOLDS) is Pattern.EXACT


def test_distance_abstract_path_unchanged() -> None:
    """116 实测 耐克阿迪王 → 耐克 0.2704（> abstract_thresh）。"""
    r = _res(("耐克", "d54", 0.2704), ("阿迪达斯", "d54", 0.3589))
    assert classify_pattern("耐克阿迪王", r, THRESHOLDS) is Pattern.ABSTRACT


# ── Recall: send an additional normalized variant only when needed ───────────────────


@pytest.mark.asyncio
async def test_ascii_token_queries_both_raw_and_normalized() -> None:
    """`BURBERRY` 的 top1 是 `za`（116 实测）——正确值连候选都召不回来，
    所以归一必须发生在**送去 embedding 之前**。"""
    from bowhead_core.concept_resolver import _query_term_variants

    fake = AsyncMock(return_value=[_res(("za", "d53", 0.31)), _res(("burberry", "d53", 0.0))])
    with patch("bowhead_core.concept_resolver.embed_query", new=fake):
        out = await _query_term_variants(object(), "BURBERRY", "keywords", 10)
    assert fake.await_args.kwargs["query_texts"] == ["BURBERRY", "burberry"]
    # After merging, the correct doc must be among candidates and lead them (ascending dist).
    assert out is not None
    assert out.hits[0].doc == "burberry"


@pytest.mark.asyncio
async def test_cjk_token_queries_only_once() -> None:
    """归一后与原串相同（绝大多数中文输入）⇒ 只发一个串，零额外成本。"""
    from bowhead_core.concept_resolver import _query_term_variants

    fake = AsyncMock(return_value=[_res(("虎牌", "d54", 0.0))])
    with patch("bowhead_core.concept_resolver.embed_query", new=fake):
        await _query_term_variants(object(), "虎牌", "keywords", 10)
    assert fake.await_args.kwargs["query_texts"] == ["虎牌"]
