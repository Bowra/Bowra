"""KbRetriever — 4-stage retrieval pipeline.

Tech design v3.6 §3.5 + PRD §3.3.5.

Pipeline:
1. embed query (dashscope text-embedding-v3)
2. chromadb cross-collection vector search (跨 ws + team collection, metadata scope filter)
3. dashscope rerank (gte-rerank-v2 cross-encoder)
4. client-side post-process:
   - 4a) soft boost role-match (glossary/sop > intro/data)
   - 4b) promotion: 保留 1 slot 给 glossary/sop (即使 score 低)
   - 4c) [P1-N2] prompt budget cap: 单 chunk content > 1500 token 截
   - 4d) [P1-P5] dedupe by chunk_id 容错跨 coll 中间态
   - 4e) [α-2] junk filter: empty-header markdown tables + super-short chunks
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any

import anyio

from bowhead_core.kb.document import is_parse_failure_placeholder
from bowhead_core.kb.eligibility import FileAuthorizer, KbUseEligibility
from bowhead_core.kb.generations import use_guard_enabled
from bowhead_core.kb.reranker import FallbackRerankPairs
from bowhead_core.kb.retrieval_policy import DEFAULT_TOP_K, MAX_TOP_K, retrieval_top_k
from bowhead_core.kb.schemas import KbFileDTO, KbSearchResult, KbSearchResultChunk
from bowhead_core.kb.splitter.paragraph_aware import count_tokens, truncate_to_tokens
from bowhead_core.kb.synonyms import expand_synonyms_for_query
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.embedder import Embedder
    from bowhead_core.kb.permission import ScopeContext
    from bowhead_core.kb.reranker import Reranker
    from bowhead_core.kb.store import KbStore


logger = logging.getLogger(__name__)


# 4a soft boost (aligned with PRD §3.3.5)
ROLE_BOOST: dict[str, float] = {
    "glossary": 0.05,
    "sop": 0.04,
    "analysis": 0.02,
    "data": 0.01,
    "intro": 0.0,
    "appendix": -0.02,
}

# 4c prompt budget cap (P1-N2 round 2): truncate single-chunk content >1500 tokens
PROMPT_CHUNK_TOKEN_CAP = 1500
PROMPT_CHUNK_TRUNCATED_SUFFIX = "\n\n...(此 chunk 超 prompt budget, 截前 1500 token. 全文见原文档)"

# Stage 2 candidate pool (before rerank)
CANDIDATE_POOL_SIZE = 20

# flooding fix (kb-optimization finding C): vector oversampling → deduplicate copies + per-file
# cap before rerank—prevent one large file/many copies taking entire pool. Eval evidence: 序时账-谊慧 (331 chunks ×
# 5 copies ≈1650 instances) flooded entity-specific queries (上海熊熊/发生额余额表 crowded out); correct
# files never reached rerank; stage 4d copy deduplication after rerank cannot rescue the pool.
# Oversample only vector stage (HNSW cheap); rerank still receives CANDIDATE_POOL_SIZE documents (same cost).
# Soft params, env-configurable (invalid values default tolerantly, review F4; read at import, changes need restart).


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


VECTOR_FETCH_SIZE = _int_env("BOWHEAD_KB_VECTOR_FETCH", 100)
PER_FILE_CANDIDATE_CAP = _int_env("BOWHEAD_KB_PER_FILE_CAP", 5)
# Per-file exemption upper bound for promotion roles (review F5)
_PROMO_EXEMPT_PER_FILE = 2

# Stage 3 rerank: fetch 2x top_k to leave promotion slots
RERANK_OVERSAMPLE_FACTOR = 2

# Promotion slots—1 each for glossary/sop
PROMOTION_ROLES = ("glossary", "sop")


def _flag_on(env_name: str, default: bool = True) -> bool:
    """α flags: env BOWHEAD_ALPHA_* in {0,false,no,off} → off; default true."""
    raw = os.environ.get(env_name, "").lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


# α-3: BM25-lite tokenizer + boost — lexical signal on top of dense rerank.
# CJK bigrams catch terms like "毛利率" (3 chars → 2 bigrams), English words by
# space split. Capped boost so reranker still leads.
_BM25_LITE_BOOST_CAP = 0.05
_BM25_LITE_PER_OVERLAP = 0.005
_EN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
# 0.40.2 #2: ID-like structured tokens (dash/underscore allowed, digit-led OK).
# Pre-0.40.2 the EN-only regex required `[A-Za-z]` start, so dash-/digit-led ids
# like "GR202211000250" never produced lexical tokens. Phase 4 dogfood Q5
# ("2022\u5e74\u4ed8-0004") missed despite the pattern existing verbatim in the chunk.
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_]{2,}")
# CJK+dash+alnum: "\u4ed8-0004", "\u51ed\u8bc1-001" \u2014 mixed-script structured ids.
_CJK_ID_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+[\-_][A-Za-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize_cjk_bigram_en(text: str) -> set[str]:
    """Tokenize: English words (≥ 2 chars, lowercased) + CJK character bigrams + ID tokens.

    "毛利率 ROE 走势 付-0004" → {"mr", "利率", "roe", "走势", "付-0004", ...}.
    Uppercase folded to lower so "ROE" matches "roe".
    """
    tokens: set[str] = set()
    # English / digits words (length ≥ 2, prevents single-char noise)
    for m in _EN_TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if len(tok) >= 2:
            tokens.add(tok)
    # 0.40.2 #2: ID-like structured tokens (dash/underscore-bearing, digit-led OK).
    # Skip pure digits ("2022" alone) — too common, would over-boost.
    for m in _ID_TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if len(tok) >= 3 and not tok.isdigit():
            tokens.add(tok)
    # CJK+dash+alnum: 付-0004 / 凭证-001 (mixed-script structured ids).
    for m in _CJK_ID_TOKEN_RE.finditer(text):
        tokens.add(m.group(0).lower())
    # CJK char run → bigrams (and the run itself if ≤ 4 chars)
    cjk_run = ""
    for ch in text + " ":
        if _CJK_RE.match(ch):
            cjk_run += ch
        else:
            if len(cjk_run) >= 2:
                # Emit bigrams
                for i in range(len(cjk_run) - 1):
                    tokens.add(cjk_run[i : i + 2])
            cjk_run = ""
    return tokens


def _bm25_lite_boost(query_tokens: set[str], content: str) -> float:
    """Token-overlap boost capped at +0.05; +0.005 per overlap term."""
    if not query_tokens:
        return 0.0
    c_tokens = _tokenize_cjk_bigram_en(content)
    overlap = len(query_tokens & c_tokens)
    return min(_BM25_LITE_BOOST_CAP, overlap * _BM25_LITE_PER_OVERLAP)


# α-2: junk-chunk filter heuristics
# Threshold kept low — short glossary entries ("GMV definition") are valid signal,
# only literally-empty content should be cut.
_MIN_USEFUL_CHARS = 5
_EMPTY_HEADER_RE = re.compile(r"^\s*\|\s*(?:\|\s*){2,}$", re.MULTILINE)
"""Empty markdown header row: `| | | |` (≥ 3 empty cells)."""


# Parse-failure/empty-table placeholder predicate shared with ingestion (worker also drops after splitting), defined in document.py.
_is_parse_failure_placeholder = is_parse_failure_placeholder


def _looks_like_junk_chunk(content: str) -> bool:
    """α-2: detect Excel/markdown junk chunks (empty headers + degenerate content).

    Heuristics:
    - Empty / whitespace-only / < 5 chars
    - First line is empty markdown header `| | | |` (>=3 empty cells, openpyxl
      flattening of unlabeled columns — what pandas would render `Unnamed: N`)

    Glossary entries are intentionally short ("GMV definition" = 14 chars) —
    they are NOT junk. Threshold tuned to allow them.
    """
    if not content:
        return True
    stripped = content.strip()
    if len(stripped) < _MIN_USEFUL_CHARS:
        return True
    # Search the first ~3 lines for empty header pattern
    head = "\n".join(stripped.splitlines()[:3])
    return bool(_EMPTY_HEADER_RE.search(head))


def _chunk_index_or_none(raw: Any) -> int | None:
    """元数据里的段序号；缺失或非整数（旧 chunk）返回 None。0 是合法值。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def _reranker_model_id(reranker: Any) -> str | None:
    model_id = getattr(reranker, "model_id", None)
    return model_id if isinstance(model_id, str) and model_id else None


def _rerank_doc(c: dict[str, Any]) -> str:
    """rerank/lexical 用的文档表示:《file_name》前缀 + content(flooding fix②)。

    file_name 空(legacy)→ 裸 content。前缀 ~30 char,对 rerank token 预算可忽略。
    """
    md = c.get("metadata", {}) or {}
    fn = str(md.get("file_name") or "")
    content = str(c.get("content", ""))
    return f"《{fn}》\n{content}" if fn else content


def _twin_key(md: dict[str, Any], content: str) -> tuple[str, str, str] | None:
    """同一份资料重复上传产生的「孪生」分块判据:(file_name, chunk_index, 内容哈希)。

    file_id 不同但文件名、段序号、内容都相同 → 同 bundle 传了多次,只留首个。只看
    (file_name, chunk_index) 会把同名的不同文件(多张粘贴的 image.png)误并成一个。
    file_name 或 chunk_index 缺失(legacy / 测试夹具)→ None,不参与去重。
    NB chunk_index=0 是合法值(falsy!)——只对 None 归空。
    """
    fn_raw = md.get("file_name")
    fn = "" if fn_raw is None else str(fn_raw)
    ci_raw = md.get("chunk_index")
    ci = "" if ci_raw is None else str(ci_raw)
    if not (fn and ci):
        return None
    return (fn, ci, hashlib.sha1(content.encode("utf-8")).hexdigest())


def _file_cap_key(md: dict[str, Any]) -> str:
    """每文件 cap 的计数键:file_id(同名不同文件分开计);legacy 缺 file_id 时回落 file_name。"""
    return str(md.get("file_id") or md.get("file_name") or "")


def _diversify_candidates(
    candidates: list[dict[str, Any]],
    *,
    per_file_cap: int,
    pool_size: int,
    minimum_size: int = 0,
) -> list[dict[str, Any]]:
    """向量候选 → rerank 输入池:副本去重 + 每文件 cap,保向量分序(flooding fix)。

    - 副本去重:``_twin_key``(与 stage 4d 同判据)——同 bundle 传多次的孪生 chunk
      只留首个,不再倍增占池。
    - 每文件 cap:按 ``_file_cap_key`` 计数;超 cap 丢弃(留下的已是该文件向量分最高
      的 cap 条,rerank 若认可该文件自会排上去)。**promotion 角色(glossary/sop)
      有限豁免**——它们在 stage 4b 有专属槽位,被大文件自己的正文 chunk 挤出池
      会饿死 promotion;但豁免限每文件 ≤2(review F5:无上界的话几百 chunk 的
      glossary 文档会换个 role 门把 flooding 原样复刻回来)。
    - 截到 pool_size。metadata 缺失的 legacy chunk 不去重不计 cap(同 4d 宽容)。
    - **caller 须先滤 placeholder/junk 再进来**(review F1:死 chunk 不占 cap 槽)。
    """
    seen_twins: set[tuple[str, str, str]] = set()
    per_file: dict[str, int] = {}
    per_file_promo: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for c in candidates:
        md = c.get("metadata", {}) or {}
        twin = _twin_key(md, str(c.get("content", "")))
        if twin is not None:
            if twin in seen_twins:
                continue
            seen_twins.add(twin)
        cap_key = _file_cap_key(md)
        if cap_key:
            if md.get("role") in PROMOTION_ROLES:
                n = per_file_promo.get(cap_key, 0)
                if n >= _PROMO_EXEMPT_PER_FILE:
                    continue
                per_file_promo[cap_key] = n + 1
            else:
                n = per_file.get(cap_key, 0)
                if n >= per_file_cap:
                    overflow.append(c)
                    continue
                per_file[cap_key] = n + 1
        out.append(c)
        if len(out) >= pool_size:
            break
    # Preserve the diverse first pass. When too few files are available, fill
    # only the requested count from unique overflow instead of silently capping
    # a single-file search at the diversity target (normally five chunks).
    fill = min(pool_size, max(0, minimum_size)) - len(out)
    if fill > 0:
        out.extend(overflow[:fill])
    return out


class KbRetriever:
    """4-stage retrieval pipeline.

    依赖注入: caller 传 embedder / store / reranker (DI 利于 mock + tests).
    """

    def __init__(
        self,
        embedder: Embedder,
        store: KbStore,
        reranker: Reranker,
        *,
        files_store: Any = None,
        artifact_store: Any = None,
        default_top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._reranker = reranker
        self._files_store = files_store
        self._artifact_store = artifact_store
        self.default_top_k = retrieval_top_k({"retrieval_top_k": default_top_k})

    async def preview_source(self, file: KbFileDTO, scope_ctx: ScopeContext, authorize_file: FileAuthorizer) -> list[tuple[str, str, dict[str, Any]]]:
        """Reuse indexed text without an embedding, reranking or generation call."""
        from bowhead_core.kb.permission import KbPermission

        if file.team_id != scope_ctx.team_id or not KbPermission.can_read(file, scope_ctx) or not await authorize_file(file):
            return []
        if use_guard_enabled():
            if self._files_store is None or self._artifact_store is None:
                return []
            check = await KbUseEligibility(self._files_store, self._artifact_store, self._store).check(
                file.file_id, scope_ctx, authorize_file, expected_generation=file.active_generation_id
            )
            if check.state != "eligible":
                return []
        return await self._store.preview_current_chunks(file)

    async def invoke(
        self,
        query: str,
        scope_ctx: ScopeContext,
        top_k: int | None = None,
        scope_filter: list[str] | None = None,
        authorize_file: FileAuthorizer | None = None,
        allowed_file_ids: tuple[str, ...] | None = None,
        enforce_source_access: bool = False,
    ) -> KbSearchResult:
        """4-stage pipeline. Returns KbSearchResult."""
        t0 = time.time()
        if allowed_file_ids == ():
            return KbSearchResult(chunks=[], total_candidates=0, rerank_used=False, latency_ms=0)
        source_filter = {"allowed_file_ids": allowed_file_ids} if allowed_file_ids is not None else {}

        # 0.40.3 Bug 5: short-circuit garbage input. Empty/whitespace/<2-char
        # queries produced degenerate embeddings → rerank API 400 rejected
        # them → retriever fell back to cosine → returned semi-random top-k
        # chunks (dev stress test T4 surface). Top-k from cosine on
        # zero-information query is noise, not signal. Also guard non-positive
        # top_k (ill-behaved LLM tool_call may emit 0 / negative).
        if not query or not query.strip() or len(query.strip()) < 2:
            emit_audit("kb.search_query_too_short", {"query_len": len(query or "")})
            return KbSearchResult(
                chunks=[],
                total_candidates=0,
                rerank_used=False,
                latency_ms=int((time.time() - t0) * 1000),
            )
        top_k = self.default_top_k if top_k is None else top_k
        if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
            emit_audit("kb.search_invalid_top_k", {"top_k": top_k})
            return KbSearchResult(
                chunks=[],
                total_candidates=0,
                rerank_used=False,
                latency_ms=int((time.time() - t0) * 1000),
            )

        pool_size = max(CANDIDATE_POOL_SIZE, top_k * RERANK_OVERSAMPLE_FACTOR)

        # ─── Stage 1: embed query ───────────────────────────
        query_embs = await self._embedder.embed_batch([query])
        if not query_embs:
            return KbSearchResult(
                chunks=[],
                total_candidates=0,
                rerank_used=False,
                latency_ms=int((time.time() - t0) * 1000),
            )
        query_emb = query_embs[0]

        # ─── Stage 2: chromadb cross-collection search ──────
        # flooding fix: oversample VECTOR_FETCH_SIZE to leave stage 2.5 diversification headroom.
        candidates = await self._store.search(
            scope_ctx,
            query_emb,
            top_k=VECTOR_FETCH_SIZE,
            scope_filter=scope_filter,
            **source_filter,
        )

        # Collections skipped on timeout/error → incomplete results, honestly pass to caller (see KbSearchCandidates)
        partial = bool(getattr(candidates, "partial", False))
        if self._files_store is not None or use_guard_enabled() or enforce_source_access:
            # Filter before reranking/top-k; bounded expansion accounts for old
            # generations occupying vector slots. The cap is reported as partial.
            seen_checks: dict[str, tuple[Any, Any]] = {}
            for window in (VECTOR_FETCH_SIZE, VECTOR_FETCH_SIZE * 2, VECTOR_FETCH_SIZE * 4):
                if window != VECTOR_FETCH_SIZE:
                    candidates = await self._store.search(scope_ctx, query_emb, top_k=window, scope_filter=scope_filter, **source_filter)
                    partial |= bool(getattr(candidates, "partial", False))
                fetched = len(candidates)
                surviving = []
                for candidate in candidates:
                    metadata = candidate.get("metadata", {})
                    file_id = str(metadata.get("file_id") or "")
                    if allowed_file_ids is not None and file_id not in allowed_file_ids:
                        continue
                    if file_id not in seen_checks:
                        file = await self._files_store.get(file_id) if self._files_store else None
                        check = None
                        if enforce_source_access and (not file or authorize_file is None or not await authorize_file(file)):
                            seen_checks[file_id] = (None, None)
                            continue
                        if file and use_guard_enabled() and authorize_file is not None:
                            remaining = 12 - (time.time() - t0)
                            if remaining > 0 and len(seen_checks) < 64:
                                with anyio.move_on_after(remaining) as budget:
                                    check = await KbUseEligibility(self._files_store, self._artifact_store, self._store).check(
                                        file_id, scope_ctx, authorize_file
                                    )
                                partial |= budget.cancel_called
                            else:
                                partial = True
                        seen_checks[file_id] = (file, check)
                    file, check = seen_checks[file_id]
                    if not file or file.soft_deleted_at or file.team_id != scope_ctx.team_id:
                        continue
                    from bowhead_core.kb.permission import KbPermission

                    if not KbPermission.can_read(file, scope_ctx):
                        continue
                    if (metadata.get("generation_id") or None) != file.active_generation_id:
                        continue
                    if use_guard_enabled() and (check is None or not check.allowed):
                        partial = True
                        continue
                    if check is not None:
                        metadata = {**metadata, "source_warning": check.warning}
                        candidate = {**candidate, "metadata": metadata}
                    surviving.append(candidate)
                if len(surviving) >= pool_size or fetched < window or window == VECTOR_FETCH_SIZE * 4:
                    partial |= fetched >= window and len(surviving) < pool_size
                    candidates = surviving
                    break
        if not candidates:
            return KbSearchResult(
                chunks=[],
                total_candidates=0,
                rerank_used=False,
                latency_ms=int((time.time() - t0) * 1000),
                partial=partial,
            )

        # ─── Stage 2.5: filtering + diversification before rerank (flooding fix) ─
        # review F1: move placeholder/junk filtering before cap—dead chunks do not consume
        # per-file cap slots (otherwise empty Excel header junk consumes cap then is dropped in 4e,
        # structurally thinning that file's output). Stage 4d/4e retain same predicates as fallback (residual should be 0).
        total_fetched = len(candidates)  # Candidates remaining after current-generation/source filtering, before diversification.
        _junk_on = _flag_on("BOWHEAD_ALPHA_JUNK_FILTER", default=True)
        _pre_placeholder = _pre_junk = 0
        _survivable: list[dict[str, Any]] = []
        for c in candidates:
            _content = c.get("content", "")
            if _is_parse_failure_placeholder(_content):
                _pre_placeholder += 1
                continue
            if _junk_on and _looks_like_junk_chunk(_content):
                _pre_junk += 1
                continue
            _survivable.append(c)
        if _pre_junk:
            emit_audit("kb.junk_chunks_filtered", {"count": _pre_junk})
        if _pre_placeholder:
            emit_audit("kb.parse_failure_chunks_filtered", {"count": _pre_placeholder})
        candidates = _diversify_candidates(
            _survivable,
            per_file_cap=PER_FILE_CANDIDATE_CAP,
            pool_size=pool_size,
            minimum_size=top_k,
        )
        # review F6: oversampling/filtering/diversification observability (fetched → survivable → pool)
        emit_audit(
            "kb.candidate_diversify",
            {"fetched": total_fetched, "survivable": len(_survivable), "pool": len(candidates)},
        )
        if not candidates:
            return KbSearchResult(
                chunks=[],
                total_candidates=total_fetched,
                rerank_used=False,
                latency_ms=int((time.time() - t0) * 1000),
                partial=partial,
            )

        # ─── Stage 3: dashscope rerank ──────────────────────
        rerank_oversample = top_k * RERANK_OVERSAMPLE_FACTOR
        # flooding fix②: prefix rerank documents with filename—table chunks have no entity names in body
        # (companies' chronological ledgers have identical structures); cross-encoder using content alone cannot distinguish 谊慧 vs 上海
        # 熊熊; entity/file-level signals live in file_name, feed reranker as first-class retrieval signals
        # (title prefix is standard doc retrieval practice).
        rerank_pairs = await self._reranker.rerank(
            query=query,
            documents=[_rerank_doc(c) for c in candidates],
            top_k=rerank_oversample,
        )

        # ─── Stage 4: client-side post-process ──────────────
        # α-3 / α-4: pre-compute query tokens (env-flag gated for eval baseline)
        bm25_on = _flag_on("BOWHEAD_ALPHA_BM25_BOOST", default=True)
        synonyms_on = _flag_on("BOWHEAD_ALPHA_SYNONYMS", default=True)
        junk_on = _flag_on("BOWHEAD_ALPHA_JUNK_FILTER", default=True)

        query_tokens: set[str] = set()
        if bm25_on or synonyms_on:
            query_tokens = _tokenize_cjk_bigram_en(query)
        if synonyms_on:
            query_tokens |= expand_synonyms_for_query(query)

        # 4a) role boost + α-3 lexical (BM25-lite) boost
        # raw cross-encoder score per candidate (by object id) — None on fallback
        # (synthetic rank scores carry no relevance signal; PRD FR6.3 floor skips).
        rerank_fell_back = isinstance(rerank_pairs, FallbackRerankPairs)
        raw_rerank: dict[int, float | None] = {}
        scored: list[tuple[dict[str, Any], float]] = []
        for orig_idx, score in rerank_pairs:
            if 0 <= orig_idx < len(candidates):
                chunk = candidates[orig_idx]
                raw_rerank[id(chunk)] = None if rerank_fell_back else score
                role = chunk.get("metadata", {}).get("role", "intro")
                role_boost = ROLE_BOOST.get(role, 0.0)
                # flooding fix②: lexical hay also includes file_name (entity names often appear only there)
                lex_boost = _bm25_lite_boost(query_tokens, _rerank_doc(chunk)) if bm25_on else 0.0
                scored.append((chunk, score + role_boost + lex_boost))
        scored.sort(key=lambda x: x[1], reverse=True)

        # 4d) [P1-P5] dedupe by chunk_id (tolerate cross-collection intermediate state during scope changes)
        # 0.40.2 #1: also dedup twins (_twin_key: file_name, chunk_index, content
        # hash) — same bundle uploaded twice produces DIFFERENT chunk_ids (different
        # file_id) but IDENTICAL content, tying up top-k slots with twin content.
        # Content hash must participate: same name, different files (multiple pasted image.png) are not twins.
        # Phase 4 dogfood Q1: top 2 chunks had score 0.5619 exactly → both copies
        # of 序时账-谊慧22年全年.XLS chunk 187. Lose 1 user-visible slot per dup.
        # 4e) [α-2] junk filter (empty-header markdown / super-short noise)
        seen_ids: set[str] = set()
        seen_content_keys: set[tuple[str, str, str]] = set()
        deduped: list[tuple[dict[str, Any], float]] = []
        junk_filtered = 0
        placeholder_filtered = 0
        for chunk, score in scored:
            cid = chunk.get("chunk_id", "")
            if cid in seen_ids:
                continue
            # ALWAYS-ON safety filter (not α-gated): drop parse-failure placeholder
            # stubs so a "(PDF parse failed…" chunk is never returned/cited as content.
            if _is_parse_failure_placeholder(chunk.get("content", "")):
                placeholder_filtered += 1
                continue
            # 0.40.2: cross-file twin detection (_twin_key). Partial metadata
            # (legacy test fixtures, fast-path chunks) → skip dedup so we don't
            # collapse legitimate distinct chunks that share an empty stub.
            content_key = _twin_key(chunk.get("metadata", {}) or {}, str(chunk.get("content", "")))
            if content_key is not None:
                if content_key in seen_content_keys:
                    continue
                seen_content_keys.add(content_key)
            if junk_on and _looks_like_junk_chunk(chunk.get("content", "")):
                junk_filtered += 1
                continue
            seen_ids.add(cid)
            deduped.append((chunk, score))
        scored = deduped
        if junk_filtered:
            emit_audit("kb.junk_chunks_filtered", {"count": junk_filtered})
        if placeholder_filtered:
            emit_audit("kb.parse_failure_chunks_filtered", {"count": placeholder_filtered})

        # 4b) promotion: reserve 1 slot for glossary / sop (even with low score)
        top = scored[:top_k]
        promoted_roles = {c.get("metadata", {}).get("role") for c, _ in top}
        for promo_role in PROMOTION_ROLES:
            if promo_role not in promoted_roles:
                # Find first role-matching chunk in scored[top_k:]
                for chunk, score in scored[top_k:]:
                    if chunk.get("metadata", {}).get("role") == promo_role:
                        top.append((chunk, score))
                        break

        # 4c) [P1-N2] prompt budget cap + emit audit
        result_chunks: list[KbSearchResultChunk] = []
        for chunk, score in top:
            content = chunk.get("content", "")
            if count_tokens(content) > PROMPT_CHUNK_TOKEN_CAP:
                content = truncate_to_tokens(content, PROMPT_CHUNK_TOKEN_CAP) + PROMPT_CHUNK_TRUNCATED_SUFFIX
            metadata = chunk.get("metadata", {}) or {}
            result_chunks.append(
                KbSearchResultChunk(
                    chunk_id=chunk.get("chunk_id", ""),
                    content=content,
                    file_id=metadata.get("file_id", ""),
                    file_name=metadata.get("file_name", ""),
                    page=metadata.get("page") if metadata.get("page", -1) != -1 else None,
                    section_title=metadata.get("section_title") or None,
                    role=metadata.get("role", "intro"),
                    score=score,
                    scope=metadata.get("scope", ""),
                    chunk_index=_chunk_index_or_none(metadata.get("chunk_index")),
                    rerank_score=raw_rerank.get(id(chunk)),
                    generation_id=metadata.get("generation_id") or None,
                    source_warning=metadata.get("source_warning"),
                )
            )

        latency = int((time.time() - t0) * 1000)
        emit_audit(
            "kb.searched_chunks",
            {
                "query_len": len(query),
                "chunks_returned": len(result_chunks),
                "latency_ms": latency,
            },
        )
        return KbSearchResult(
            chunks=result_chunks,
            # review F3: report stage 2 raw candidates (before diversification)—LLM/route consumers
            # use this for KB sparsity; reporting pool size ≤20 misleads.
            total_candidates=total_fetched,
            # Honestly report False when rerank request fails and falls back to vector order (previously always True).
            rerank_used=not rerank_fell_back,
            latency_ms=latency,
            rerank_model=None if rerank_fell_back else _reranker_model_id(self._reranker),
            partial=partial,
        )
