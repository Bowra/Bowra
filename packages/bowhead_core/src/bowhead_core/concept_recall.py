"""Recall typed concepts deterministically from value and schema collections.

Batch both sources through embed_search.query without an LLM. Keep each hit's
dimension and value together, and group all matches by type and dimension so
ambiguity stays visible. Literal and supported containment matches are dictionary
evidence; only near matches use distance thresholds. Apply used=false checks to
schema metadata, since a value index need not include unused dimensions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from bowhead_core.concept_resolver import _merge_variant_results, _norm_term
from bowhead_core.embed_search import EmbedHit, EmbedQueryResult, first_unavailable_reason
from bowhead_core.embed_search import query as embed_query
from bowhead_core.env_utils import env_float

logger = logging.getLogger(__name__)

Evidence = Literal["literal", "token_in_doc", "doc_in_token", "near"]
MatchType = Literal["dim_value", "dim_name", "metric", "time"]
Verdict = Literal["unique", "ambiguous", "weak", "not_found", "unusable_dimension", "no_permission"]

# Evidence strength order: smaller is stronger. Compare this ordering, never strings.
_EVIDENCE_RANK: dict[str, int] = {"literal": 0, "token_in_doc": 1, "doc_in_token": 2, "near": 3}
# ≥ this strength counts as answerable by dictionary (literal / token_in_doc / doc_in_token).
_DICT_ANSWERABLE_RANK = _EVIDENCE_RANK["doc_in_token"]
# Tokens ≥ this length are entity names: containment alone is evidence, without coverage requirement (see grade_evidence).
_LONG_TOKEN = 3


def _recall_n() -> int:
    """Return the requested width for dimension-value recall.

    A crowded dimension must not exhaust a small candidate window before another
    dimension's literal match appears. embed_search enforces the upstream bounds.
    """
    return int(env_float("BOWHEAD_CONCEPT_RECALL_N", 30.0))


def _schema_recall_n() -> int:
    return int(env_float("BOWHEAD_CONCEPT_SCHEMA_RECALL_N", 5.0))


def _cover_min() -> float:
    """包含类证据的覆盖率下限 —— 见 `grade_evidence`。"""
    return env_float("BOWHEAD_CONCEPT_COVER_MIN", 0.5)


def _near_thresh(ctx: Any) -> float:
    from bowhead_core.embed_search import _get_thresholds

    return _get_thresholds(ctx)[1]


@dataclass(frozen=True)
class Match:
    """一条召回命中。``dim`` 与 ``value`` 来自同一条 hit（tech-design A2）。"""

    type: MatchType
    dim: str  # "d199" / "m56" / "t1"
    dim_name: str  # Chinese brand name; empty if not found
    value: str  # dim_value: concrete value; dim_name/metric/time: vocabulary terms
    evidence: Evidence
    dist: float

    @property
    def group_key(self) -> tuple[str, str]:
        """Keep match type in the grouping key.

        A dimension name requests grouping; a value in that dimension requests filtering.
        Sharing a dimension ID must not collapse those distinct interpretations.
        """
        return (self.type, self.dim)


@dataclass
class TokenRecall:
    token: str
    verdict: Verdict
    matches: list[Match] = field(default_factory=list)
    unusable: list[dict[str, str]] = field(default_factory=list)
    truncated: int = 0
    embed_unavailable: str = ""
    # True = no dimension inventory; used=false filtering ineffective this turn, all dim_name empty
    inventory_degraded: bool = False


def grade_evidence(token: str, doc: str, dist: float, near_thresh: float) -> Evidence | None:
    """Grade literal, containment, and nearby evidence for one hit.

    Disable containment for single-character tokens. Short tokens must meet coverage
    requirements, while longer entity names may match values with specification
    suffixes. Reverse containment retains its coverage requirement. Ambiguity is
    reported separately rather than hidden by this evidence gate.
    """
    t = _norm_term(token)
    d = _norm_term(doc)
    if not t or not d:
        return None
    if t == d:
        return "literal"
    if len(t) >= 2:
        cover_min = 0.0 if len(t) >= _LONG_TOKEN else _cover_min()
        if t in d and (len(t) / len(d)) >= cover_min:
            return "token_in_doc"
        if d in t and len(d) >= 2 and (len(d) / len(t)) >= max(cover_min, _cover_min()):
            return "doc_in_token"
    return "near" if dist <= near_thresh else None


def _match_type(dim_id: str, collection: str) -> MatchType | None:
    if collection == "keywords":
        return "dim_value" if dim_id.startswith("d") else None
    if dim_id.startswith("d"):
        return "dim_name"
    if dim_id.startswith("m"):
        return "metric"
    if dim_id.startswith("t"):
        return "time"
    return None


async def dim_inventory(ctx: Any) -> dict[str, tuple[list[str], bool]]:
    """``{dim_id: (names, used)}``，取自 datarc ``list_dimensions`` 的**原始全量**。

    **不是** ``bi_schema`` 的输出 —— 后者已把 ``used=false`` 滤掉
    （`tool_registry.py` D4 分支），而我们恰恰需要那些维度的**名字**来告诉用户
    「有这个字段，只是没数据」。

    缓存键是 **workspace**（review D9）：``resolve_datarc_project()`` 是 per-call 的，
    按 turn 缓存会在同 turn 跨 workspace 时串表。

    映射 ``f"d{DimensionMeta.id}"`` ↔ schema coll ``prop``：116 全量对拍
    90 个维度 → 一致 81 / 冲突 0 / 未逐字命中 9。
    """
    datarc = getattr(ctx, "datarc", None)
    resolver = getattr(ctx, "resolve_datarc_project", None)
    ws = resolver() if callable(resolver) else None
    if datarc is None or not ws:
        return {}
    cache = getattr(ctx, "_concept_dim_inventory", None)
    if not isinstance(cache, dict):
        cache = {}
        with contextlib.suppress(Exception):  # Stub ctx may forbid attribute writes (lesson from term_hook _FakeCtx).
            ctx._concept_dim_inventory = cache  # noqa: SLF001
    if ws in cache:
        return cache[ws]
    try:
        dims = await datarc.list_dimensions(str(ws))
    except Exception as e:
        logger.warning("concept_recall.dim_inventory_failed ws=%s err=%s", ws, e)
        return {}
    out = {f"d{d.id}": (list(d.names or []), bool(getattr(d, "used", False))) for d in dims}
    cache[ws] = out
    return out


def _variants(token: str) -> list[str]:
    norm = _norm_term(token)
    return [token] if not norm or norm == token else [token, norm]


async def _batched_query(
    ctx: Any,
    tokens: list[str],
    collection: str,
    n_results: int,
) -> tuple[dict[str, EmbedQueryResult | None], str]:
    """**按 collection 合批**（不是按 token 扇出）：N 个词一次查完。

    `embed_search.query` 本身就是批量 API（``query_texts ≤50``，返回顺序对应）。
    旧路径每 token 各查一次 ⇒ 2N 次往返；这里 **2 次，与词数无关**。
    """
    texts: list[str] = []
    owner: list[str] = []
    for t in tokens:
        for v in _variants(t):
            texts.append(v)
            owner.append(t)
    if not texts:
        return {}, ""

    per_token: dict[str, list[EmbedQueryResult]] = {t: [] for t in tokens}
    unavailable = ""
    # ≤50 is embed_search.query's contract cap; 8 terms × 2 variants = 16, usually one request.
    CHUNK = 50
    for i in range(0, len(texts), CHUNK):
        chunk_texts = texts[i : i + CHUNK]
        chunk_owner = owner[i : i + CHUNK]
        try:
            results = await embed_query(ctx, query_texts=chunk_texts, collection=collection, n_results=n_results)  # type: ignore[arg-type]
        except Exception as e:
            # Do not merely continue: silent failure yields empty hits ⇒ verdict=not_found,
            # making backend outage and genuine dictionary absence indistinguishable upstream;
            # this hid silent all-zero concept resolution at two private deployments.
            logger.warning("concept_recall.query_failed coll=%s err=%s", collection, e)
            if not unavailable:
                unavailable = f"query_raised:{collection}"
            continue
        results = list(results or [])
        if not unavailable:
            unavailable = first_unavailable_reason(results) or ""
        for j, r in enumerate(results):
            if j < len(chunk_owner):
                per_token[chunk_owner[j]].append(r)
    merged = {t: _merge_variant_results(v) for t, v in per_token.items()}
    return merged, unavailable


def _hits_to_matches(
    token: str,
    result: EmbedQueryResult | None,
    collection: str,
    inventory: dict[str, tuple[list[str], bool]],
    near_thresh: float,
) -> tuple[list[Match], list[dict[str, str]]]:
    """一条 hit → 一个 match；``used=false`` 的维度进 ``unusable`` 而非 ``matches``。"""
    matches: list[Match] = []
    unusable: dict[str, dict[str, str]] = {}
    for h in list(getattr(result, "hits", None) or []):
        if not isinstance(h, EmbedHit):
            continue
        mtype = _match_type(h.dim_id or "", collection)
        if mtype is None:
            continue
        doc = h.value if (collection == "keywords" and h.value) else h.doc
        ev = grade_evidence(token, doc or "", h.dist, near_thresh)
        if ev is None:
            continue
        names, used = inventory.get(h.dim_id, ([], True))
        dim_name = names[0] if names else ""
        # A3/review D1: used=false dimensions must not be recommended as queryable.
        if h.dim_id.startswith("d") and h.dim_id in inventory and not used:
            unusable[h.dim_id] = {
                "dim": h.dim_id,
                "dim_name": dim_name,
                "reason": "declared_but_no_data",
            }
            continue
        matches.append(
            Match(
                type=mtype,
                dim=h.dim_id,
                dim_name=dim_name,
                value=(doc or "").strip(),
                evidence=ev,
                dist=float(h.dist),
            )
        )
    # A field the member may not use is reported by name so the answer can say so and
    # point to an administrator. Denied dimension values stay silent: naming them
    # would disclose rows outside the member's permission.
    for h in list(getattr(result, "denied", None) or []):
        if collection != "schema" or h.dim_id in unusable or not h.dim_id.startswith(("d", "m")):
            continue
        if grade_evidence(token, h.doc or "", h.dist, near_thresh) not in ("literal", "token_in_doc", "doc_in_token"):
            continue
        names, _ = inventory.get(h.dim_id, ([], True))
        unusable[h.dim_id] = {
            "dim": h.dim_id,
            "dim_name": names[0] if names else (h.doc or "").strip(),
            "reason": "no_permission",
        }
    return matches, list(unusable.values())


def _rank_and_cap(matches: list[Match], cap: int) -> tuple[list[Match], int]:
    """Rank concept matches while preserving representation across groups.

    Select a representative for each type/dimension group before filling remaining
    capacity. The cap counts distinct values, not rows: the same value in multiple
    dimensions consumes no additional value slot. Report dropped distinct values.
    """
    if not matches:
        return [], 0
    groups: dict[tuple[str, str], list[Match]] = {}
    for m in matches:
        groups.setdefault(m.group_key, []).append(m)
    for g in groups.values():
        g.sort(key=lambda m: (_EVIDENCE_RANK[m.evidence], m.dist))
    ordered_groups = sorted(groups.values(), key=lambda g: (_EVIDENCE_RANK[g[0].evidence], g[0].dist))

    # Priority unchanged: one representative per group, then fill remainder by evidence/distance. Only **quota accounting** changes.
    ordered = [g[0] for g in ordered_groups] + sorted(
        (m for g in ordered_groups for m in g[1:]),
        key=lambda m: (_EVIDENCE_RANK[m.evidence], m.dist),
    )
    out: list[Match] = []
    admitted: set[str] = set()
    for m in ordered:
        if m.value in admitted:
            out.append(m)  # Same value, different dimension: shares admission
        elif len(admitted) < cap:
            admitted.add(m.value)
            out.append(m)
    dropped_values = {m.value for m in matches} - admitted
    return out, len(dropped_values)


def _is_answerable(m: Match) -> bool:
    """够不够格当「字典能答」——含 ``doc_in_token``（`北京市` → 词表里的 `北京`）。"""
    return _EVIDENCE_RANK[m.evidence] <= _DICT_ANSWERABLE_RANK


def _is_strong(m: Match) -> bool:
    """Decide whether a match can establish a separate ambiguity group.

    Literal and token_in_doc matches qualify. A shorter catalog string contained in
    a more specific user token can still answer that token, but does not by itself
    justify interrupting the user with another interpretation.
    """
    return _EVIDENCE_RANK[m.evidence] <= _EVIDENCE_RANK["token_in_doc"]


def _verdict(matches: list[Match], unusable: list[dict[str, str]]) -> Verdict:
    if not matches:
        if any(d.get("reason") == "no_permission" for d in unusable):
            return "no_permission"
        return "unusable_dimension" if unusable else "not_found"
    if not any(_is_answerable(m) for m in matches):
        return "weak"
    strong = {m.group_key for m in matches if _is_strong(m)}
    return "ambiguous" if len(strong) >= 2 else "unique"


async def recall_tokens(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
    """对一批 token 做确定性召回。**零 LLM**，两次 embed 调用。"""
    tokens = [t for t in tokens if t and t.strip()]
    if not tokens:
        return []
    inventory = await dim_inventory(ctx)
    near = _near_thresh(ctx)
    # A3 requires dimension inventory. Without it, fail-open (prefer extra answers to a silent whole-batch failure),
    # but **leave evidence**: otherwise recommending used=false dimensions is invisible in production.
    inventory_degraded = not inventory
    if inventory_degraded:
        logger.warning("concept_recall.inventory_unavailable — used=false 过滤本轮未生效 tokens=%d", len(tokens))

    # Concurrent: each source now ends with a Datarc permission check that takes seconds.
    (kw, kw_down), (sch, sch_down) = await asyncio.gather(
        _batched_query(ctx, tokens, "keywords", _recall_n()),
        _batched_query(ctx, tokens, "schema", _schema_recall_n()),
    )
    down = kw_down or sch_down

    out: list[TokenRecall] = []
    for t in tokens:
        m1, u1 = _hits_to_matches(t, kw.get(t), "keywords", inventory, near)
        m2, u2 = _hits_to_matches(t, sch.get(t), "schema", inventory, near)
        if u1:
            # Observed keywords collection does not index used=false values; seeing them means this datarc instance
            # differs from 116, so report it (review §7.2).
            logger.warning("concept_recall.unused_dim_in_keywords token=%r dims=%s", t, [d["dim"] for d in u1])
        merged, truncated = _rank_and_cap(m1 + m2, cap)
        seen: set[str] = set()
        unusable = [d for d in (u1 + u2) if not (d["dim"] in seen or seen.add(d["dim"]))]
        # The user named a field they may not use: neighbouring fields (退款额 for 退款率,
        # 门店 + 面积 for 门店面积) must not quietly stand in for it.
        if any(d["reason"] == "no_permission" and grade_evidence(t, d["dim_name"], 0.0, near) == "literal" for d in unusable):
            merged, truncated = [], 0
        out.append(
            TokenRecall(
                token=t,
                verdict=_verdict(merged, unusable),
                matches=merged,
                unusable=unusable,
                truncated=truncated,
                embed_unavailable=down,
                inventory_degraded=inventory_degraded,
            )
        )
    return out
