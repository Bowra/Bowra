"""ar2 internal embed_search helper.

concept resolve 词表召回的唯一入口。**LLM 不能直接调** — datarc 工作区 / tenant 通过
``ctx`` 注入, 防跨 ws 投毒.

两种来源:

* **维度值**（``collection="keywords"``）：datarc ``POST /workspaces/{ws}/keywords/recall/``。
  datarc 在服务端按工作区选向量库，**不需要** embed 代理、tenant 或 collection 名；
  返回 ``field="d{id}"`` + ``word=维度值原文``，一条候选就是一个 (dim, dim_value) pair.
  工作区所属客户没配向量服务时 datarc 回 errcode=400「未配置向量服务」.

* **schema**（``collection="schema"``，``v1_{tenant}_p{ws}``）：仍经 embed 代理直读 collection.
  metadata ``prop="d{id}"`` 或 ``"t{id}"`` 或 ``"m{id}"`` (纯 id, 不带 value).
  一行 = 一个 dim 别名定义. ``doc`` 字段 = dim word (中文/英文同义词).

阈值来源 (per-ws calibrated > env default):

* ``ctx.ar2_thresholds`` per-ws calibrated (gateway lifespan 从 ProjectMemoryStore
  kind=``ws_threshold`` 拉, lazy load)
* fallback env ``BOWHEAD_AR2_EXACT_THRESH=0.10``, ``BOWHEAD_AR2_ABSTRACT_THRESH=0.25``
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

_DEFAULT_EXACT_THRESH = float(os.environ.get("BOWHEAD_AR2_EXACT_THRESH", "0.10"))
_DEFAULT_ABSTRACT_THRESH = float(os.environ.get("BOWHEAD_AR2_ABSTRACT_THRESH", "0.25"))

# Datarc /keywords/recall/ n_results contract range; clamp here so one validation error does not make the whole batch unavailable.
_RECALL_N_MIN = 1
_RECALL_N_MAX = 100


@dataclass(frozen=True)
class EmbedHit:
    """Single hit from embed query.

    schema coll: value=None (因为 prop 不含 value, doc=dim word).
    维度值召回: value=doc=datarc 返回的维度值原文 (e.g. "阿迪达斯").
    """

    doc: str
    dim_id: str  # "d1" / "m1" / "t1" (without the "=" suffix)
    value: str | None  # For dimension-value recall: the original dimension value.
    dist: float
    # Datarc token identity, used for the member permission check.
    prop: str = ""
    tuids: tuple[str, ...] = ()


@dataclass
class EmbedQueryResult:
    query: str
    hits: list[EmbedHit] = field(default_factory=list)
    miss: bool = True  # top-1 dist > abstract_thresh
    exact: bool = False  # top-1 dist < exact_thresh AND top-2 dist > 2*exact_thresh
    top1_top2_ratio: float = 1.0  # for V5 single-entity detection
    # Nonempty = backend could not answer (no vector store / no Datarc workspace / undetermined tenant / query error),
    # **not** a completed query without hits. Both used to collapse to hits=[] + miss=True, making a broken embed
    # indistinguishable from a word absent from the dictionary; this hid concept resolve silently returning
    # all zeros for two private deployments. Judge only this field, never infer from empty hits.
    unavailable_reason: str = ""
    # Hits withheld because the asking member lacks permission (see datarc_permissions).
    denied: list[EmbedHit] = field(default_factory=list)

    @property
    def unavailable(self) -> bool:
        return bool(self.unavailable_reason)


def _degraded(query_texts: list[str], reason: str) -> list[EmbedQueryResult]:
    """后端不可用时的占位结果——形状与正常返回一致，但带上不可用原因."""
    return [EmbedQueryResult(query=q, hits=[], miss=True, unavailable_reason=reason) for q in query_texts]


def first_unavailable_reason(results: list[EmbedQueryResult]) -> str:
    """一批结果里的不可用原因（取第一个）；全部可用 → ""."""
    for r in results:
        if r.unavailable_reason:
            return r.unavailable_reason
    return ""


def _get_thresholds(ctx: Any) -> tuple[float, float]:
    """ctx.ar2_thresholds per-ws calibrated > env default."""
    cal = getattr(ctx, "ar2_thresholds", None)
    if isinstance(cal, dict):
        exact = float(cal.get("exact", _DEFAULT_EXACT_THRESH))
        abstract = float(cal.get("abstract", _DEFAULT_ABSTRACT_THRESH))
        return exact, abstract
    return _DEFAULT_EXACT_THRESH, _DEFAULT_ABSTRACT_THRESH


def effective_tenant(vector_client: Any, configured: Any = "") -> str:
    """schema collection 名的 tenant 段：连接实测判定 > 部署级 pin；都没有 → ``""``.

    **所有拼 collection 名的地方必须走这里**，别各自取一个默认值——2026-08-17 的事故
    正是同一判据两份实现：``embed_search`` 用 ``ctx.tenant``、``term_context`` 用自己的
    ``vector_tenant="demo"`` 默认参数，两处都写死成 demo。返回空串时调用方必须降级，
    **不许**拿默认值拼名字：同一向量服务上不同 client 的 project id 会撞号，拼错等于
    读别家客户的数据。
    """
    discovered = str(getattr(vector_client, "datarc_tenant", "") or "")
    return discovered or str(configured or "")


def _parse_prop_dim_id(prop: str) -> str:
    """Parse ``prop``, return dim_id (e.g. 'd54' / 'm1' / 't1').

    维度值 prop format: ``"d54=<value>"`` — 取 "=" 前段
    schema coll prop format: ``"d1"`` / ``"t1"`` / ``"m1"`` — 直接是 dim_id
    operator (rare): "$like" 等 — pass-through
    """
    if not prop:
        return ""
    if "=" in prop:
        return prop.partition("=")[0]
    return prop


def _resolve_project(ctx: Any) -> str:
    # embed-via-openapi (R1.2 catch-up): Datarc workspaces resolve only through ``resolve_datarc_project``;
    # post-1c-migration ctx.workspace_id is a Bowhead `ws_*`.
    # Batch 1/c3: never fall back to ctx.workspace_id (defensive getattr for stubs, lesson from term_hook _FakeCtx).
    resolver = getattr(ctx, "resolve_datarc_project", None)
    return str((resolver() if callable(resolver) else None) or "")


async def query(
    ctx: Any,
    query_texts: list[str],
    collection: Literal["keywords", "schema"],
    n_results: int = 10,
) -> list[EmbedQueryResult]:
    """ar2 middleware 内部 embed query 入口.

    Args:
        ctx: ToolExecutionContext (含 datarc / vector_client / tenant / ar2_thresholds)
        query_texts: ≤50 项 batch.
        collection: "keywords" (dim_value 查，走 datarc 召回接口) | "schema" (dim 名 / metric 名 查)
        n_results: 每个 query 返 top-N

    Returns:
        EmbedQueryResult per query (顺序对应 query_texts).
        后端不可用 → 全 miss=True 且带 ``unavailable_reason`` 的 placeholder.
    """
    if len(query_texts) > 50:
        logger.warning(
            "embed_search.batch_too_large size=%d (>50), truncating",
            len(query_texts),
        )
        query_texts = query_texts[:50]

    if collection == "keywords":
        return await _withhold_denied(ctx, await _recall_dim_values(ctx, query_texts, n_results))

    vc = getattr(ctx, "vector_client", None)
    if vc is None:
        logger.debug("embed_search.vector_client_unavailable, returning empty results")
        return _degraded(query_texts, "no_vector_client")

    try:
        from bowhead_memory import schema_collection
    except ImportError:
        logger.warning("embed_search.bowhead_memory_unavailable")
        return _degraded(query_texts, "memory_package_missing")

    ws_id = _resolve_project(ctx)
    if not ws_id:
        logger.warning("embed_search.datarc_workspace_missing on ctx")
        return _degraded(query_texts, "no_datarc_workspace")

    # Tenant priority: detected on this connection > explicit deployment pin. **No default**: observed 2026-08-17,
    # hardcoded "demo" produced `v1_demo_p1` on a private deployment (actual `v1_dev_p1`); Datarc returned
    # "Collection does not exist", so concept resolve always returned 0 candidates without warning.
    # If undetermined, degrade; do not build names from defaults. Different clients on the same vector service can share project IDs,
    # so a wrong name reads another customer's semantic dictionary.
    tenant = effective_tenant(vc, getattr(ctx, "tenant", ""))
    if not tenant:
        logger.warning("embed_search.tenant_unresolved project=%s", ws_id)
        return _degraded(query_texts, "tenant_unresolved")

    coll_name = schema_collection(tenant, ws_id)

    try:
        raw = await vc.query(
            collection=coll_name,
            query_texts=query_texts,
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.warning(
            "embed_search.query_failed coll=%s n=%d err=%s",
            coll_name,
            len(query_texts),
            e,
        )
        return _degraded(query_texts, "query_failed")

    return await _withhold_denied(ctx, _parse_schema_results(raw, query_texts, ctx))


async def _withhold_denied(ctx: Any, results: list[EmbedQueryResult]) -> list[EmbedQueryResult]:
    """Remove hits the asking member may not use; one Datarc check covers the whole batch.

    Recall has no member context, so without this a clarification card or prompt hint
    can name fields and values the member cannot see. Datarc checks a dimension value
    against row permission only, so a value hit also checks its dimension's column.
    """
    from bowhead_core.datarc_permissions import PermissionToken, check_tokens, member_of

    def checks(h: EmbedHit) -> list[PermissionToken]:
        own = PermissionToken(h.doc, h.prop, h.tuids)
        return [own, PermissionToken(h.doc, h.dim_id)] if "=" in h.prop and h.dim_id else [own]

    hits = [h for r in results for h in r.hits if h.prop]
    if not hits:
        return results
    tokens = list(dict.fromkeys(t for h in hits for t in checks(h)))
    decisions = dict(zip(tokens, await check_tokens(getattr(ctx, "datarc", None), _resolve_project(ctx), member_of(ctx), tokens), strict=True))
    if "denied" not in decisions.values():
        return results
    thresholds = _get_thresholds(ctx)
    out: list[EmbedQueryResult] = []
    for r in results:
        denied = [h for h in r.hits if h.prop and any(decisions.get(t) == "denied" for t in checks(h))]
        if not denied:
            out.append(r)
            continue
        kept = [h for h in r.hits if h not in denied]
        rebuilt = _build_result(r.query, kept, thresholds)
        rebuilt.unavailable_reason = r.unavailable_reason
        rebuilt.denied = denied
        out.append(rebuilt)
    return out


def _recall_failure_reason(exc: Exception) -> str:
    """把 datarc 召回接口的失败归成可区分的不可用原因——别都叫 ``query_failed``.

    ``keyword_recall_unsupported`` 单列：HTTP 404 = 该 datarc 版本还没有这个接口，
    升级 datarc 才能好，与瞬时故障的处置完全不同。业务层的「不存在」（errcode 非空）不算。
    """
    from bowhead_bi_bridge import BINotFoundError, BIPermissionError

    msg = str(getattr(exc, "errmsg", "") or exc)
    if "向量服务" in msg:
        return "vector_service_unconfigured"
    if isinstance(exc, BINotFoundError) and not getattr(exc, "errcode", None):
        return "keyword_recall_unsupported"
    if isinstance(exc, BIPermissionError):
        return "keyword_recall_forbidden"
    return "query_failed"


async def _recall_dim_values(ctx: Any, query_texts: list[str], n_results: int) -> list[EmbedQueryResult]:
    """维度值召回：datarc ``/keywords/recall/``，一批词一次请求."""
    from bowhead_core.datarc_permissions import split_tuids

    datarc = getattr(ctx, "datarc", None)
    if datarc is None:
        return _degraded(query_texts, "no_datarc_client")
    project = _resolve_project(ctx)
    if not project:
        logger.warning("embed_search.datarc_workspace_missing on ctx")
        return _degraded(query_texts, "no_datarc_workspace")

    # Datarc disallows empty words; omit blank queries but **preserve placeholders/order** because callers align results by index.
    # Send duplicates once: server computes words serially without deduplication (observed 2026-09-15); repeats only waste queue time.
    words = list(dict.fromkeys(q for q in query_texts if q and q.strip()))
    if not words:
        return [EmbedQueryResult(query=q) for q in query_texts]
    n = max(_RECALL_N_MIN, min(_RECALL_N_MAX, int(n_results)))
    try:
        recalls = await datarc.recall_keywords(project, words, n_results=n)
        by_word = dict(zip(words, recalls, strict=True))
    except Exception as e:
        reason = _recall_failure_reason(e)
        logger.warning(
            "embed_search.keyword_recall_failed project=%s n=%d reason=%s err=%s",
            project,
            len(words),
            reason,
            e,
        )
        return _degraded(query_texts, reason)

    thresholds = _get_thresholds(ctx)
    results: list[EmbedQueryResult] = []
    for q in query_texts:
        if not (q and q.strip()):
            results.append(EmbedQueryResult(query=q))
            continue
        rec = by_word[q]
        # Downstream classify_pattern / hypothesis verification treats hits[0] as top-1; API docs promise no order, so sort here.
        hits = sorted(
            (
                EmbedHit(
                    doc=c.word,
                    dim_id=c.field or _parse_prop_dim_id(c.prop),
                    value=c.word,
                    dist=float(c.distance),
                    prop=c.prop,
                    tuids=split_tuids(c.tuids),
                )
                for c in rec.candidates
                if c.word
            ),
            key=lambda h: h.dist,
        )
        results.append(_build_result(q, hits, thresholds))
    return results


def _build_result(query_text: str, hits: list[EmbedHit], thresholds: tuple[float, float]) -> EmbedQueryResult:
    """hits（已按 dist 升序）→ EmbedQueryResult，统一算 miss / exact / ratio."""
    if not hits:
        return EmbedQueryResult(query=query_text, hits=[], miss=True)

    exact_thresh, abstract_thresh = thresholds
    top1 = hits[0]
    top2 = hits[1] if len(hits) > 1 else None
    ratio = top1.dist / top2.dist if (top2 and top2.dist > 0) else 1.0

    return EmbedQueryResult(
        query=query_text,
        hits=hits,
        miss=top1.dist > abstract_thresh,
        # exact: top1 clearly leads (2× scales with the threshold better than fixed 0.20).
        exact=top1.dist < exact_thresh and (top2 is None or top2.dist > 2.0 * exact_thresh),
        top1_top2_ratio=ratio,
    )


def _parse_schema_results(
    raw: dict[str, Any],
    queries: list[str],
    ctx: Any,
) -> list[EmbedQueryResult]:
    """VectorClient 返 {documents: list[list[str]], metadatas: list[list[dict]], distances: list[list[float]]}.

    外层 list 对应 batch query, 内层 list 对应 top-N.
    """
    thresholds = _get_thresholds(ctx)

    docs_all = raw.get("documents") or []
    metas_all = raw.get("metadatas") or []
    dists_all = raw.get("distances") or []

    results: list[EmbedQueryResult] = []
    for qi, q in enumerate(queries):
        docs = docs_all[qi] if qi < len(docs_all) else []
        metas = metas_all[qi] if qi < len(metas_all) else []
        dists = dists_all[qi] if qi < len(dists_all) else []

        hits: list[EmbedHit] = []
        for i, doc in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            dist = float(dists[i]) if i < len(dists) else 1.0
            prop = (meta or {}).get("prop", "") if isinstance(meta, dict) else ""
            hits.append(
                EmbedHit(
                    doc=str(doc) if doc else "",
                    dim_id=_parse_prop_dim_id(prop),
                    value=None,
                    dist=dist,
                    prop=prop,
                )
            )
        results.append(_build_result(q, hits, thresholds))

    return results
