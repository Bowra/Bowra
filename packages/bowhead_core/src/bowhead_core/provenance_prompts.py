"""δ-#5 system-prompt injection blocks — Provenance + Active Facts.

Two builders that the compute / lead system prompts consume:

1. ``build_provenance_block()``   — fixed ~200 tok rule block teaching the LLM
   the BNF tag format and source priority. Static, no per-turn variation.

2. ``build_active_facts_block(...)`` — per-turn fact retrieval. Query-keyed
   match + recency fill, capped at 10 facts (~500 tok). Bounded by 60s LRU
   cache (round 2 SRE-P0-2) so back-to-back turns in the same workspace
   don't hit SQLite for every spawn.

Both builders are **flag-gated**: if ``is_feature_enabled("PROVENANCE_TAGS")``
or ``"FACT_LOOKUP"`` is false, they return ``""`` so callers can splice
unconditionally without conditionals.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from bowhead_core.provenance_flags import is_feature_enabled

if TYPE_CHECKING:
    from bowhead_core.registered_fact_store import RegisteredFact, RegisteredFactStore

logger = logging.getLogger(__name__)


_PROVENANCE_BLOCK = """## Provenance Rules

每条 claim 必须以 source tag 结束: [<kind>:<ref>]

- 数字 / 实体名 / 时间 → [bi:art_id] / [user:turn_id] / [fact:fact_key]
- 公式 / 概念定义 → 优先级 [user:..] > [fact:..] > [kb:..] > [llm-std:]
- LLM 推导 → [llm-rsn:<chain>] (e.g. [llm-rsn:bi:art_001|fact:cogs])

通用财务 / 会计 / 行业概念 (EBITDA / ROE / DOT / OEE 等) **可以**用 [llm-std:],
客户后续可 override → 后续 turn 自动改用 [fact:..]. 不要因为 KB 没命中就拒答.

ref 含 `:` `|` `]` 必须 percent-encode (`%3A` / `%7C` / `%5D`).

"""

_FACTS_HEADER = "## Active Facts (project / user 修正过的)\n\n"

# Minimal keyword tokenizer: mixed Chinese/English; numbers and `_` are words; ≥2 characters count as substantive.
# CJK uses a Unicode property for the entire Chinese range; Western text uses \w. Callers normalize case.
_KEYWORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_MIN_KW_LEN = 2
_MAX_KEYWORDS = 10
# Too common in user_text; fact_key must not match via these stop words.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "what",
        "how",
        "why",
        "are",
        "did",
        "does",
        "can",
        "could",
        "should",
        "would",
        "我们",
        "我",
        "你",
        "你们",
        "他们",
        "什么",
        "怎么",
        "为什么",
        "如何",
        "请",
        "帮",
        "想",
        "要",
        "是不是",
        "可以",
        "能不能",
        "可不可以",
    }
)

_FACT_VALUE_TRUNC_CHARS = 200  # Cap one fact's value_json in the prompt.
_DEFAULT_FACT_LIMIT = 10  # Cap on total fact count.
_DEFAULT_KEYWORD_LIMIT = 10  # Cap on keywords used with LIKE.
# δ-#8.5 SRE P0-1: pre-LLM block sits on TTFT critical path. Hard cap the
# combined keyword + recency lookups so SQLite contention can't add multi-second
# latency to first-token. Empty result + metric on timeout.
_LOOKUP_TIMEOUT_SEC = 0.2

# round 2 SRE-P0-2 + δ-#8.5 SRE P0-2 — pre-LLM fact retrieval cache (bounded).
# Key = (workspace_id, project_id, user_id) tuple; value = (facts, expires_ts).
# 60s TTL shares consecutive turns in a workspace; OrderedDict + maxsize=256 prevents unbounded growth from
# 50 sessions × N projects × M users in long-running processes. Evict old entries by LRU.
_CACHE_MAXSIZE = 256
_CACHE_TTL_SEC = 60.0
_FACT_CACHE: OrderedDict[tuple[str, str, str], tuple[list[RegisteredFact], float]] = OrderedDict()
_CACHE_LOCK = asyncio.Lock()


def build_provenance_block() -> str:
    """Static provenance rules block. Returns ``""`` when sub-flag off."""
    if not is_feature_enabled("PROVENANCE_TAGS"):
        return ""
    return _PROVENANCE_BLOCK


def _extract_keywords(text: str, *, limit: int = _DEFAULT_KEYWORD_LIMIT) -> list[str]:
    """Pull lowercase keywords from user_text, drop stop-words, cap to ``limit``."""
    tokens = _KEYWORD_RE.findall(text.lower())
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if len(tok) < _MIN_KW_LEN:
            continue
        if tok in _STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _format_facts(facts: list[RegisteredFact]) -> str:
    """Render fact list as bulleted prompt block with truncation."""
    if not facts:
        return ""
    lines = [_FACTS_HEADER]
    for f in facts:
        value = (f.value_json or "")[:_FACT_VALUE_TRUNC_CHARS]
        if len(f.value_json or "") > _FACT_VALUE_TRUNC_CHARS:
            value += "…"
        lines.append(f"- [{f.fact_key}] {value} ({f.fact_type}, {f.source})\n")
    lines.append("\n")
    return "".join(lines)


async def _list_active_cached(
    workspace_id: str,
    project_id: str | None,
    user_id: str | None,
    fact_store: RegisteredFactStore,
    *,
    limit: int,
) -> list[RegisteredFact]:
    """list_active with 60s TTL cache + maxsize bound (LRU) — invalidate via
    ``invalidate_cache(workspace_id=..)`` after fact write."""
    key = (workspace_id, project_id or "", user_id or "")
    now = time.monotonic()
    async with _CACHE_LOCK:
        cached = _FACT_CACHE.get(key)
        if cached and cached[1] > now:
            # Move to end (LRU touch)
            _FACT_CACHE.move_to_end(key)
            return cached[0]
    facts = await fact_store.list_active(
        workspace_id=workspace_id,
        project_id=project_id,
        user_id=user_id,
        limit=limit,
    )
    async with _CACHE_LOCK:
        _FACT_CACHE[key] = (facts, now + _CACHE_TTL_SEC)
        _FACT_CACHE.move_to_end(key)
        # LRU evict — pop oldest until we're at cap.
        while len(_FACT_CACHE) > _CACHE_MAXSIZE:
            _FACT_CACHE.popitem(last=False)
    return facts


def invalidate_cache(
    *,
    workspace_id: str,
    project_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Drop cache. After auto_learner write next turn's prompt sees new fact
    immediately, not 60s later.

    δ-#8.5 SRE P1-5: drop *all* keys whose workspace matches when scope-args
    are None — a project-scoped fact write invalidates `(ws, proj, "")` AND
    any user-scoped read `(ws, proj, "u_xxx")` that may have included it via
    overlay semantics. Cheaper than racing scope mismatch.
    """
    if project_id is None and user_id is None:
        # Wipe the whole workspace
        keys_to_drop = [k for k in _FACT_CACHE if k[0] == workspace_id]
        for k in keys_to_drop:
            _FACT_CACHE.pop(k, None)
        return
    # Scoped invalidation: drop the exact key AND any cache entry that would
    # have inherited the new fact (workspace-shared / project overlay).
    drop = (workspace_id, project_id or "", user_id or "")
    _FACT_CACHE.pop(drop, None)
    # Also drop the "all-shared" cache for this workspace since it may have
    # included the now-stale view.
    _FACT_CACHE.pop((workspace_id, "", ""), None)


def reset_cache_for_test() -> None:
    """Test hook: clear all entries."""
    _FACT_CACHE.clear()


async def build_active_facts_block(
    *,
    workspace_id: str,
    user_text: str,
    fact_store: RegisteredFactStore,
    project_id: str | None = None,
    user_id: str | None = None,
    fact_limit: int = _DEFAULT_FACT_LIMIT,
) -> str:
    """Per-turn pre-LLM fact injection.

    Strategy: query-keyed match (CTO-P0-4 — recency-only loses domain hits)
    + recency fill from cache to cap. Returns empty string when no facts,
    sub-flag disabled, or lookup exceeds ``_LOOKUP_TIMEOUT_SEC`` (TTFT guard).
    Caller can append unconditionally.
    """
    if not is_feature_enabled("FACT_LOOKUP"):
        return ""
    try:
        return await asyncio.wait_for(
            _build_active_facts_block_inner(
                workspace_id=workspace_id,
                user_text=user_text,
                fact_store=fact_store,
                project_id=project_id,
                user_id=user_id,
                fact_limit=fact_limit,
            ),
            timeout=_LOOKUP_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.warning(
            "delta.facts_lookup_timeout sec=%s ws=%s",
            _LOOKUP_TIMEOUT_SEC,
            workspace_id,
        )
        return ""


async def _build_active_facts_block_inner(
    *,
    workspace_id: str,
    user_text: str,
    fact_store: RegisteredFactStore,
    project_id: str | None,
    user_id: str | None,
    fact_limit: int,
) -> str:
    keywords = _extract_keywords(user_text)
    matched: list[RegisteredFact] = []
    if keywords:
        try:
            matched = await fact_store.list_by_key_keywords(
                workspace_id=workspace_id,
                keywords=keywords,
                project_id=project_id,
                user_id=user_id,
                limit=fact_limit,
            )
        except Exception as e:
            logger.warning("delta.facts_keyword_lookup_failed err=%s", e)
            matched = []

    if len(matched) >= fact_limit:
        return _format_facts(matched[:fact_limit])

    fill_needed = fact_limit - len(matched)
    try:
        recent = await _list_active_cached(
            workspace_id,
            project_id,
            user_id,
            fact_store,
            limit=fact_limit + fill_needed,
        )
    except Exception as e:
        logger.warning("delta.facts_recency_lookup_failed err=%s", e)
        recent = []

    matched_keys = {(f.fact_key, f.project_id, f.user_id) for f in matched}
    fills: list[RegisteredFact] = []
    for f in recent:
        if (f.fact_key, f.project_id, f.user_id) in matched_keys:
            continue
        fills.append(f)
        if len(fills) >= fill_needed:
            break

    return _format_facts(matched + fills)
