"""Deterministic concept resolution helpers: token classifier, dim-name-suffix detection, pattern enum.

Shared by native BI concept resolution and verified recall. Nothing here calls an LLM:
when the dictionary has no answer, the calling agent interprets the term itself.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

from bowhead_core.embed_search import (
    EmbedHit,
    EmbedQueryResult,
    _get_thresholds,
)
from bowhead_core.embed_search import (
    query as embed_query,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Token-level types
# ─────────────────────────────────────────────────────────────


class TokenKind(StrEnum):
    DIM_NAME = "dim_name"  # "门店" / "省份" / "city" (schema match).
    DIM_VALUE = "dim_value"  # "上海市" / "阿迪达斯"
    METRIC = "metric"  # "销售额" / "客单价"
    TIME = "time"  # "Q3" / "2025" / "去年"
    OPERATOR = "operator"  # "包含" / "除外"
    UNKNOWN = "unknown"


class Pattern(StrEnum):
    EXACT = "exact"  # top-1 < exact_thresh, distant top-2.
    SUBSTRING = "substring"  # ≥2 hits containing user_token substring + ≥80% same dim_id.
    ABSTRACT = "abstract"  # top-K all miss.
    AMBIGUOUS = "ambiguous"  # Across ≥3 dimensions, all 0.10-0.25.
    UNCLEAR = "unclear"  # Fallback.


@dataclass
class TokenClass:
    """Per-token classification + resolution candidate."""

    token: str
    kind: TokenKind
    pattern: Pattern
    schema_dim_id: str | None = None  # On schema_coll match.
    keywords_hits: list[EmbedHit] = field(default_factory=list)
    dim_id: str | None = None  # Once locked.


# ─────────────────────────────────────────────────────────────
# Predicate output (consumed by query rewriter)
# ─────────────────────────────────────────────────────────────


@dataclass
class DimPredicate:
    dim_id: str
    op: Literal["eq", "in", "contains", "not_in", "not_contains"]
    values: list[str]  # eq/in: full values; contains: [substring]
    dim_word: str | None = None  # Datarc-recognized Chinese dimension (e.g. "品牌"); rewriter prepend.
    original_token: str | None = None  # Original query text to replace (e.g. "万达店" → replace entirely with "门店包含万达").


@dataclass
class DerivedMetric:
    name: str  # "客单价"
    formula: str  # "销售额 / 订单数"
    base_metrics: list[str]  # ["m1", "m3"]
    base_metric_words: list[str]


@dataclass
class MetricPlan:
    derived: list[DerivedMetric] = field(default_factory=list)


@dataclass
class ResolutionPlan:
    dim_predicates: list[DimPredicate] = field(default_factory=list)
    metric_plan: MetricPlan | None = None
    rewrite_log: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# C4: Pattern detection (deterministic)
# ─────────────────────────────────────────────────────────────


def _norm_term(text: str) -> str:
    """词表比较用的归一形式：NFKC + strip + casefold。

    datarc 的 embed collection 里存的是**词**（一个 doc = 一个维度值/维度名）。
    116 实测：字典 464 个 doc 里只有 1 个含大写（`AUM`），而用户会输入
    `BURBERRY` / `Burberry` / 全角 `ｂｕｒｂｅｒｒｙ`。两边都归一后比较，
    既吃掉大小写/全角/空格差异，也不会像单边 casefold 那样反伤大写原值。
    """
    return unicodedata.normalize("NFKC", text or "").strip().casefold()


def _merge_variant_results(results: list[EmbedQueryResult]) -> EmbedQueryResult | None:
    """把同一 token 多个查询变体的候选并成一组（去重 + 按 dist 升序）。

    只做**召回**合并：判定已改为字符串比较优先（见 classify_pattern），
    所以只需保证正确的 doc 出现在候选里。miss/exact/ratio 等标志位取
    top1 更近的那一组。
    """
    live = [r for r in results if r is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    def _top(r: EmbedQueryResult) -> float:
        return r.hits[0].dist if r.hits else float("inf")

    base = min(live, key=_top)
    seen: set[tuple[str, str]] = set()
    merged: list[EmbedHit] = []
    for h in sorted((h for r in live for h in r.hits), key=lambda x: x.dist):
        key = (h.doc or "", h.dim_id or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(h)
    denied = list(dict.fromkeys(h for r in live for h in r.denied))
    return replace(base, hits=merged, denied=denied)


def classify_pattern(
    token: str,
    embed_result: EmbedQueryResult,
    thresholds: tuple[float, float],
) -> Pattern:
    """4-pattern classification rules (per tech-design §2.4).

    Args:
        token: user input token
        embed_result: top-K from embed_search on keywords coll
        thresholds: (exact_thresh, abstract_thresh) per-ws calibrated or env default
    """
    exact_thresh, abstract_thresh = thresholds
    hits = embed_result.hits

    if not hits:
        return Pattern.ABSTRACT

    # ── Vocabulary lookup precedes distance (sprint concept-resolve-dim-value-2026-08 §16) ──
    # Datarc embed collections store **words**, not sentences. Opaque short brand names / branch IDs
    # have no meaningful semantic distance. Judging top-1/top-2 distance separation systematically
    # misclassifies: 116 observed 35 of 114 real dimension values (31%) with dist≈0 verbatim matches classified
    # unknown, **worse with regular names** (233000001营业部 / 238000001营业部 are neighbors,
    # top2 distance only 0.015). ⇒ Embeddings recall; strings decide.
    # None of the old distance rules below are removed; now fallback for genuinely fuzzy input.
    normalized_token = _norm_term(token)
    if normalized_token:
        literal = [h for h in hits if _norm_term(h.doc or "") == normalized_token]
        if literal:
            literal_dims = {h.dim_id for h in literal if h.dim_id}
            # Same literal can belong to multiple dimensions (116: 禽类 d55/d56,
            # 23区 d53/d54, in s d53/d54); a verbatim match **cannot** lock dimension then; clarify.
            return Pattern.EXACT if len(literal_dims) == 1 else Pattern.AMBIGUOUS

    top1 = hits[0]
    top2 = hits[1] if len(hits) > 1 else None

    # EXACT: top-1 clearly leads.
    if top1.dist < exact_thresh and (top2 is None or top2.dist > 2.0 * exact_thresh):
        return Pattern.EXACT

    # SUBSTRING: ≥2 actual user_token substring hits AND ≥80% same dim_id.
    substr_hits = [h for h in hits if token and token in (h.doc or "")]
    if len(substr_hits) >= 2:
        # Check same-dim_id ratio.
        dim_counts: dict[str, int] = {}
        for h in hits:
            dim_counts[h.dim_id] = dim_counts.get(h.dim_id, 0) + 1
        majority_count = max(dim_counts.values()) if dim_counts else 0
        if majority_count / len(hits) >= 0.8:
            return Pattern.SUBSTRING

    # ABSTRACT: top-1 > abstract_thresh
    if top1.dist > abstract_thresh:
        return Pattern.ABSTRACT

    # AMBIGUOUS: ≥3 distinct dim_id, all in [exact_thresh, abstract_thresh).
    unique_dims = set()
    for h in hits:
        if exact_thresh <= h.dist < abstract_thresh:
            unique_dims.add(h.dim_id)
    if len(unique_dims) >= 3:
        return Pattern.AMBIGUOUS

    # UNCLEAR fallback (default: hypothesis).
    return Pattern.UNCLEAR


async def lookup_dim_word(dim_id: str, ctx: Any) -> str | None:
    """Reverse-lookup: dim_id → 中文 dim 名 (datarc 能识别的) via schema coll.

    搜常见 dim seed ("品牌", "门店", "城市", "客户", "类别") top-N, 找命中 dim_id 的.
    cache miss 时回退 None (rewriter 退化为 prepend 模式).
    """
    if not dim_id or not dim_id.startswith("d"):
        return None
    SEEDS = ["品牌", "门店", "城市", "省份", "国家", "客户", "类别", "类目", "渠道", "区域", "产品"]
    try:
        results = await embed_query(ctx, query_texts=SEEDS, collection="schema", n_results=3)
    except Exception:
        return None
    # find any seed where top-1 = dim_id
    best: tuple[str, float] | None = None
    for r in results:
        if not r.hits:
            continue
        top = r.hits[0]
        if top.dim_id == dim_id and top.dist < 0.10 and (best is None or top.dist < best[1]):
            best = (top.doc, top.dist)
    return best[0] if best else None


def majority_dim_id(hits: list[EmbedHit]) -> str | None:
    """top hits 中占比最高的 dim_id."""
    if not hits:
        return None
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.dim_id] = counts.get(h.dim_id, 0) + 1
    return max(counts, key=counts.get) if counts else None


# ─────────────────────────────────────────────────────────────
# C5: Dim-name suffix detection (万达店 → strip "店" → "万达").
# ─────────────────────────────────────────────────────────────


@dataclass
class DimSuffix:
    dim_id: str
    dim_word: str
    value_part: str


async def detect_dim_suffix(
    token: str,
    ctx: Any,
    suffix_max_len: int = 3,
    suffix_thresh: float | None = None,
) -> DimSuffix | None:
    """从 token 尾扫 1~suffix_max_len 字, 若命中 schema coll 某 dim word → split.

    e.g. "万达店":
      n=1 → suffix="店" → schema query → 若 dist<suffix_thresh AND 命中 dim → split

    suffix_thresh 默认 = (exact_thresh + abstract_thresh) / 2, 比 exact 宽 (因为 suffix
    通常缩略/近义, e.g. "店" → "门店" dist=0.15 严的 exact_thresh=0.10 截不到).
    """
    if not token or len(token) <= 1:
        return None

    if suffix_thresh is None:
        exact, abstract = _get_thresholds(ctx)
        suffix_thresh = (exact + abstract) / 2.0  # Default ~0.175.

    # Longest suffix first (e.g. "医院" before "院").
    for n in range(min(suffix_max_len, len(token) - 1), 0, -1):
        suffix = token[-n:]
        if not suffix.strip():
            continue
        try:
            results = await embed_query(
                ctx,
                query_texts=[suffix],
                collection="schema",
                n_results=3,
            )
        except Exception as e:
            logger.debug("dim_suffix.embed_failed suffix=%r err=%s", suffix, e)
            continue
        if not results or not results[0].hits:
            continue
        top = results[0].hits[0]
        if top.dim_id.startswith("d") and top.dist < suffix_thresh:
            value_part = token[:-n]
            if value_part:
                return DimSuffix(
                    dim_id=top.dim_id,
                    dim_word=top.doc,
                    value_part=value_part,
                )

    return None


# ─────────────────────────────────────────────────────────────
# C6: Full-query embed cross-check (V5 single-entity detection)
# ─────────────────────────────────────────────────────────────


async def detect_single_entity(
    query: str,
    ctx: Any,
) -> EmbedHit | None:
    """Full-query embed, 若 top-1 dist < exact_thresh AND ratio top1/top2 < 0.7
    → 强 single-entity hit, 返该 EmbedHit (caller 改写为该 entity).

    用于 "上海市第三医院" → "上海交通大学医学院附属第三人民医院" 这种缩略 → 全名 case.
    """
    _exact_thresh, _ = _get_thresholds(ctx)
    try:
        results = await embed_query(ctx, query_texts=[query], collection="keywords", n_results=5)
    except Exception as e:
        logger.debug("full_query_embed.failed q=%r err=%s", query, e)
        return None
    if not results or not results[0].hits:
        return None
    r = results[0]
    if r.exact and r.top1_top2_ratio < 0.7:
        return r.hits[0]
    return None


# ─────────────────────────────────────────────────────────────
# Public entry: classify_and_resolve
# ─────────────────────────────────────────────────────────────


async def _query_term_variants(
    ctx: Any,
    token: str,
    collection: str,
    n_results: int,
) -> EmbedQueryResult | None:
    """查词表时把归一变体一起带上，保证正确的 doc **出现在候选里**。

    116 实测：`BURBERRY` / `Burberry` / 全角 `ｂｕｒｂｅｒｒｙ` 的 top-1 是 `za` / `a02`
    （0.31 / 0.52）——**正确值连候选都召不回来**，比较侧再强也救不回。归一后与原串
    相同时（绝大多数中文输入）只发一个串，零额外成本。
    """
    normalized = _norm_term(token)
    variants = [token] if not normalized or normalized == token else [token, normalized]
    try:
        results = await embed_query(ctx, query_texts=variants, collection=collection, n_results=n_results)  # type: ignore[arg-type]
    except Exception:
        return None
    return _merge_variant_results(list(results or []))


async def classify_token(
    token: str,
    ctx: Any,
    keywords_result: EmbedQueryResult | None = None,
    schema_result: EmbedQueryResult | None = None,
) -> TokenClass:
    """Combine schema + keywords embed + pattern detector to classify token.

    Lookup priority: schema (dim/metric name) → keywords (dim_value) → unknown.
    """
    thresholds = _get_thresholds(ctx)

    # 1. Schema check (dimension/metric/time name?).
    if schema_result is None:
        schema_result = await _query_term_variants(ctx, token, "schema", 3)

    if schema_result and schema_result.hits:
        top = schema_result.hits[0]
        if top.dist < thresholds[0]:  # exact_thresh
            if top.dim_id.startswith("m"):
                return TokenClass(
                    token=token,
                    kind=TokenKind.METRIC,
                    pattern=Pattern.EXACT,
                    schema_dim_id=top.dim_id,
                    dim_id=top.dim_id,
                )
            if top.dim_id.startswith("t"):
                return TokenClass(
                    token=token,
                    kind=TokenKind.TIME,
                    pattern=Pattern.EXACT,
                    schema_dim_id=top.dim_id,
                    dim_id=top.dim_id,
                )
            if top.dim_id.startswith("d"):
                # Dimension-name hit, not dim_value; mentioning a dimension usually means group by.
                return TokenClass(
                    token=token,
                    kind=TokenKind.DIM_NAME,
                    pattern=Pattern.EXACT,
                    schema_dim_id=top.dim_id,
                    dim_id=top.dim_id,
                )

    # 2. Keywords check (dim_value)
    if keywords_result is None:
        keywords_result = await _query_term_variants(ctx, token, "keywords", 10)

    if keywords_result and keywords_result.hits:
        pat = classify_pattern(token, keywords_result, thresholds)
        dim_id = majority_dim_id(keywords_result.hits) if pat in (Pattern.EXACT, Pattern.SUBSTRING) else None
        kind = TokenKind.DIM_VALUE if pat in (Pattern.EXACT, Pattern.SUBSTRING) else TokenKind.UNKNOWN
        return TokenClass(
            token=token,
            kind=kind,
            pattern=pat,
            keywords_hits=keywords_result.hits,
            dim_id=dim_id,
        )

    return TokenClass(token=token, kind=TokenKind.UNKNOWN, pattern=Pattern.ABSTRACT)
