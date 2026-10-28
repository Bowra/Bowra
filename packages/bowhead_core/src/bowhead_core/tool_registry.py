"""Tool Registry & Runner.

职责：
- 把 `docs/tool-registry-v2.md` 定义的 13 个 `bi.*` 工具注册成 LLM 可见的 schema
- 执行 tool call 时路由到对应的 handler
- 统一异常捕获 + trace 产出

Handler 是 async callable，签名：
    async (args: dict, ctx: ToolExecutionContext) -> Any
"""

from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import logging
import os
import random as _random
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bowhead_router.types import ToolSchema
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope, reference_skill_active
from bowhead_skill.sandbox_call_shape import sandbox_inputs_hint

from bowhead_bi_bridge import BIAsyncTaskTimeout, BIConnectionError, DatarcClient, DatarcError
from bowhead_core.document_revision_target import DocumentRevisionTarget
from bowhead_core.model_clock import model_today
from bowhead_core.resource_access import (
    resource_grant_targets_for_tool as _shared_resource_grant_targets_for_tool,
)
from bowhead_core.scrub import scrub  # ④f: scrub cred shapes from raised-exception error text
from bowhead_core.terminal_effect_contract import (
    ClassifiedToolEffectResult,
    ToolEffectSpec,
)
from bowhead_core.tool_call_policy import TOOL_CALL_POLICIES, PolicyDenial, PolicyStage, ToolCallRequest
from bowhead_core.types import ToolAnnotations, ToolInvocation, utc_now_iso

logger = logging.getLogger(__name__)


class AmbiguousToolEffectOutcomeError(RuntimeError):
    """A dispatched write returned no proof of applied vs not-applied."""

    def __init__(self, error_kind: str) -> None:
        super().__init__("tool effect outcome is unknown")
        self.error_kind = error_kind


_DETERMINISTIC_NOT_APPLIED_ERROR_KINDS = frozenset(
    {
        "blocked",
        "rate_limited",
        "team_denied",
        "upstream_4xx",
        "upstream_auth",
        "validation",
    }
)


# M158 paradigm v2: role-based tool whitelist
# All roles allowed by default (backward compatibility); paradigm_v2=False sets ctx.role=None, skipping enforcement.
_DEFAULT_ALLOWED_ROLES: frozenset[str] = frozenset({"lead", "analyst", "composer"})


class ToolPermissionError(Exception):
    """role 不在 tool 的 allowed_for_roles 集合, 拒绝调用.
    handler 抛后 LLM 收到错 message hint, 自纠正改用允许的 tool (e.g. spawn).
    """


# ── Datarc rate-limit retries ────────────────────────────────────────
# Observed at a privately deployed customer on 2026-08-18: 45/104 daily bi_query subqueries failed (43%),
# all saying 「查询过于频繁，请稍后再试」. Every batch of 2/3/5/8/10 queries
# always had only two successes, rejecting the rest immediately: datarc concurrency limit two, while we
# default to six concurrent queries (BOWHEAD_CHI_MAX_CONCURRENT). Without retries/backoff, throttled
# queries were lost; agents continued without their data, presenting repeated BI errors to users.
# The markers live with the client (bowhead_bi_bridge.errors), which also flags its typed errors with them.


def _is_datarc_throttled(message: str) -> bool:
    """这条 datarc 错误是不是「限流」——限流可重试，其它 datarc 错误重试无意义。"""
    from bowhead_bi_bridge.errors import is_throttle_message

    return is_throttle_message(message)


def _bi_throttle_retries() -> int:
    """限流重试次数（soft cap，env 可调）。0 = 关掉重试。"""
    try:
        return max(0, int(os.environ.get("BOWHEAD_BI_THROTTLE_RETRIES", "3")))
    except ValueError:
        return 3


def _bi_throttle_backoff_base() -> float:
    """首次退避秒数；之后指数增长并加抖动（抖动很关键：同批被拒的几条若同时重试会再次撞墙）。"""
    try:
        return max(0.05, float(os.environ.get("BOWHEAD_BI_THROTTLE_BACKOFF_S", "0.8")))
    except ValueError:
        return 0.8


# ─────────────────────────────────────────────────────────────────────────
# Sprint λ — bi_query helpers (F1 turn cache + F3 fast resolver)
# ─────────────────────────────────────────────────────────────────────────
# Invariant I-λ-SW1: module-level cache state assumes single-worker process
# (enforced by chi_flags.assert_consistent_and_single_worker, WEB_CONCURRENCY=1).
# Future multi-worker deploy MUST migrate to distributed cache (Redis / SQLite).

# {turn_id: {cache_key: result_dict}} — bounded by turn count, not entry count.
# Per-turn entry count naturally bounded by Lead tool_call budget (~50/turn).
_TURN_QUERY_CACHE: dict[str, dict[str, Any]] = {}
_TURN_QUERY_CACHE_LRU: list[str] = []  # turn_id FIFO (eviction safety net)
_TURN_QUERY_CACHE_MAX_TURNS = 200  # cap memory; ~200 × 50 × 5KB = 50MB worst

# Deduplicate concurrent same-query fetches within a turn. The completed-result
# cache handles serial calls but cannot stop simultaneous misses.
# Map turn IDs and cache keys to futures. A failed leader resolves to None so
# followers retain the right to retry after a transient failure.
_TURN_QUERY_INFLIGHT: dict[str, dict[str, Any]] = {}


async def _single_flight_bi_fetch(
    turn_id: str,
    cache_key: str,
    fetch: Any,
) -> Any:
    """批2 single-flight：并发同 (turn, cache_key) 只跑一次 ``fetch()``。

    - follower await leader 的 future；leader 成功且非 error 结果 → 直接复用
      （与 λ-F1 缓存命中同语义：同一 result 对象、同 artifact_id）。
    - leader 抛异常或返回 error 结果 → follower 回落自己跑（自任新 leader）。
      异常路 future 收 None 而非 set_exception（无人 retrieve 会触发 asyncio
      告警，且 None 就是「回落」信号）。
    - 模块级纯助手而非内联在 ``_query_single``：闭包内的 handler 无法从测试
      patch，这里可直接 UT 并发语义 + 让 wiring 可 spy。
    """
    import asyncio as _aio_sf

    # Review P2-1: recheck in a loop, not once; N followers wake simultaneously after leader failure.
    # A single check makes them fire at datarc together, more synchronized than natural staggering before the change,
    # losing protection precisely under throttling. In a loop, the first follower sees an empty slot and becomes leader;
    # others find that new leader's future and wait again. At most one actual fetch per iteration.
    # Reacquire setdefault at the top of every iteration (review2 P2-5: after the leader's finally removes an
    # empty bucket, followers holding its old reference write new futures into an orphan dict; later callers
    # create a new bucket and miss it, allowing a duplicate).
    while True:
        bucket = _TURN_QUERY_INFLIGHT.setdefault(turn_id, {})
        leader = bucket.get(cache_key)
        if leader is None:
            break
        try:
            dup = await leader
        except Exception:  # noqa: S110 — Leader failure falls back.
            dup = None
        if dup is not None and not _bi_result_is_error(dup):
            try:
                from bowhead_core import metrics as _m_sf

                _m_sf.inc(
                    "bowhead_bi_query_single_flight_dedup_total",
                    labels={"path": "single"},
                    help="批2: 并发同 query follower 复用 leader 结果, 未打 datarc。",
                )
            except Exception:  # noqa: S110
                pass
            return dup
        # Leader failure/error result → loop top: become leader if slot empty, otherwise wait for the new leader.
        # ⚠ Prevent spinning: cleanup_turn_query_cache pops the whole bucket without clearing entries; a resolved
        # future still occupying the local-reference slot must be removed, or the loop spins on a dead future.
        # Normal leader finally already pops it; this only covers cleanup races.
        if bucket.get(cache_key) is leader:
            bucket.pop(cache_key, None)
    fut = _aio_sf.get_running_loop().create_future()
    bucket[cache_key] = fut
    try:
        result = await fetch()
    except BaseException:
        if not fut.done():
            fut.set_result(None)
        raise
    else:
        if not fut.done():
            fut.set_result(result)
        return result
    finally:
        if bucket.get(cache_key) is fut:
            bucket.pop(cache_key, None)
        if not bucket and _TURN_QUERY_INFLIGHT.get(turn_id) is bucket:
            _TURN_QUERY_INFLIGHT.pop(turn_id, None)


def _normalize_query_for_cache(text: str) -> str:
    """λ-F1: canonicalize query string for cache match.
    Rules:
      - strip leading/trailing whitespace (incl. full-width 　)
      - lower-case (datarc treats case-insensitively)
      - collapse internal whitespace to single space
      - strip trailing 标点 (?？.。!!，,;；)
    """
    import re as _re

    s = text.strip().lower()
    s = _re.sub(r"[\s　]+", " ", s)
    s = _re.sub(r"\s*[?？.。!！,，;；]+$", "", s)  # strip trailing whitespace + punct
    return s.strip()


#: Warn when one BI fetch exceeds this byte threshold. Not a cap; reject no calls.
#: A size-cap design was disproven (docs/sprints/artifact-volume-2026-09-04/tech-design.md §4.3:
#: of 27 artifacts >10MB, 25 entered sandbox and 23 turns delivered successfully; capping makes sandbox silently compute wrong results).
#: Detect recurrence that day, rather than block now.
#:
#: Default 10MB rationale: clean-window bi_fetch_rows p90 was 31KB; pathological fetches were 10–88MB.
#: Three orders of magnitude apart, so any intermediate threshold works; choose a round value.
_BI_FETCH_OVERSIZED_BYTES = 10 * 1024 * 1024


def _bi_fetch_oversized_threshold() -> int:
    """读 env 不缓存——运维要能不重启就调；解析失败退默认值，绝不因此打挂取数。"""
    raw = os.environ.get("BOWHEAD_BI_FETCH_OVERSIZED_BYTES", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _BI_FETCH_OVERSIZED_BYTES
    return value if value > 0 else _BI_FETCH_OVERSIZED_BYTES


def _record_bi_fetch_scale(*, byte_size: int, row_count: int, query_text: str) -> None:
    """记一次 BI 取数的规模；超阈值再多打一条 WARN。

    为什么要有这条：09-02 之前 BI 取数出现过 10–88MB 的行级明细（拉回来在沙箱里
    `groupby` 成几十行），耗时 91–472 秒。它在 09-02 之后自己停了，但**归因不成立**
    （劝阻文案 09-02T16:29Z 才写出来、09-04 才上线，而最后一次明细取数是 09-02T09:26Z）
    ⇒ 说不清是被修好了还是恰好没人这么问。所以留一条探针，复发时当天看得见。

    全段 fail-soft：埋点失败绝不能影响一次已经成功的取数。
    """
    try:
        from bowhead_core import metrics as _m

        _m.observe(
            "bowhead_bi_fetch_rows_bytes",
            float(byte_size),
            help="单次 bi_fetch_rows 落盘字节数（复发探针，见 artifact-volume sprint §4.3）。",
            buckets=(1e3, 1e4, 1e5, 1e6, 1e7, 1e8),
        )
        _m.observe(
            "bowhead_bi_fetch_rows_row_count",
            float(row_count),
            help="单次 bi_fetch_rows 行数。行级明细的判据是行数不是字节（宽表少行也可能很大）。",
            buckets=(10, 100, 1e3, 1e4, 1e5, 1e6),
        )
        if byte_size > _bi_fetch_oversized_threshold():
            _m.inc(
                "bowhead_bi_fetch_rows_oversized_total",
                help="超阈值的 bi_fetch_rows 次数。**不拒绝调用**，只是让复发看得见。",
            )
            logger.warning(
                "bi_fetch_rows.oversized bytes=%d rows=%d query=%r",
                byte_size,
                row_count,
                query_text[:80],
            )
    except Exception:  # noqa: BLE001 — Instrumentation must never break a successful fetch.
        logger.debug("bi_fetch_rows.scale_metric_failed", exc_info=True)


def _drop_repeated_csv_header_rows(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> list[dict[str, Any]]:
    """Drop datarc-emitted rows whose values are exactly the CSV header."""
    if not rows or not columns:
        return rows

    def _cell(value: Any) -> str:
        return str(value or "").strip().lstrip("\ufeff")

    header = [_cell(c) for c in columns]
    out: list[dict[str, Any]] = []
    for row in rows:
        if all(_cell(row.get(col)) == expected for col, expected in zip(columns, header, strict=False)):
            continue
        out.append(row)
    return out


# Sprint agent-redesign-1 Phase E2 (2026-05-15) — semantic prefix for cache hint.
# Strip "Top N" / "按X分组" / single-dim refinement suffixes so the same
# underlying dataset (time × geo × metric) shares one canonical key. Used
# ONLY for the advisory `_cache_hint` field — exact cache key (with all
# path-affecting params) still controls actual fetch / hit.
#
# Prod evidence: t_d6a63c92 had 5 queries sharing "2025年7-12月 + 8省 + 销售达成金额"
# prefix but differing on `Top 20` / `按代表分组` / `Destination` suffix → 0 cache
# hit. With semantic key, all 5 fall into 1-2 buckets, peer gets hint to reuse.

_SEMANTIC_SUFFIX_PATTERNS = (
    # Sort/rank
    r"\s*top\s*\d+\b.*$",
    r"\s*前\s*\d+\b.*$",
    r"\s*排名\s*(前|后)?\s*\d+.*$",
    # Group by
    r"\s*按[^，,。.;；]*分组.*$",
    r"\s*group\s*by\s+.*$",
    r"\s*按[^，,。.;；]+(拆分|拆解|拆为).*$",
    # Limit
    r"\s*限制\s*\d+.*$",
    r"\s*limit\s*\d+.*$",
)


def _semantic_prefix(text: str) -> str:
    """Strip ranking/grouping/limit suffixes for cache hint semantic dedup.

    Examples:
      "华东 2025 H2 销售达成金额 Top 20"            → "华东 2025 h2 销售达成金额"
      "上海 2025 Q3 销售按代表分组"                  → "上海 2025 q3 销售"
      "华东 2025 H2 销售 限制 100"                   → "华东 2025 h2 销售"
      "华东 2025 H2 销售"                            → "华东 2025 h2 销售" (no-op)

    Requires ≥8 chars of remaining prefix (avoids over-collapsing short queries).
    """
    import re as _re

    norm = _normalize_query_for_cache(text)
    for pat in _SEMANTIC_SUFFIX_PATTERNS:
        norm2 = _re.sub(pat, "", norm, flags=_re.IGNORECASE).strip()
        if 8 <= len(norm2) < len(norm):
            norm = norm2
    return norm


# Phase E2: turn-scope semantic prefix → first call's full info (artifact_id, etc.)
# Stores in same _TURN_QUERY_CACHE namespace under prefix "@sem:" to avoid collision.


def _semantic_cache_put(turn_id: str, prefix: str, hint: dict[str, Any]) -> None:
    """Store semantic hint (call_id, artifact_id, original_query) by prefix key."""
    if not prefix or len(prefix) < 8:
        return
    sem_key = f"@sem:{prefix}"
    if turn_id not in _TURN_QUERY_CACHE:
        _TURN_QUERY_CACHE[turn_id] = {}
        _TURN_QUERY_CACHE_LRU.append(turn_id)
    _TURN_QUERY_CACHE[turn_id].setdefault(sem_key, hint)


def _semantic_cache_lookup(turn_id: str, prefix: str) -> dict[str, Any] | None:
    """Look up prior call's hint by semantic prefix. None if first-time."""
    if not prefix or len(prefix) < 8:
        return None
    bucket = _TURN_QUERY_CACHE.get(turn_id)
    if not bucket:
        return None
    return bucket.get(f"@sem:{prefix}")


def _bi_cache_key(
    text: str,
    fast: bool,
    limit: Any,
    columns: Any,
    inline_max_rows: Any = None,
    mode: Any = None,
    datarc_workspace: Any = None,
) -> str:
    """λ-F1: cache key — normalized text + ALL path-affecting args.

    P1-Eng-1 (post-review): key must include inline_max_rows + mode since
    they change result shape (inline_max_rows truncates LLM-visible rows;
    mode hints datarc question/ interpretation).

    批2/c6 (PRD §3.4, review P0): + datarc_workspace 维度——tool_switch_context
    同 turn 切换工作区后，同 query 绝不能命中旧工作区的缓存结果（「确认了但
    没换」比不支持更糟）。
    """
    norm = _normalize_query_for_cache(text)
    cols = ",".join(sorted(columns)) if isinstance(columns, list) else ""
    lim = "" if limit is None else str(limit)
    imr = "" if inline_max_rows is None else str(inline_max_rows)
    md = "" if mode is None else str(mode)
    dws = "" if datarc_workspace is None else str(datarc_workspace)
    return f"{norm}|f{'1' if fast else '0'}|l{lim}|c{cols}|i{imr}|m{md}|w{dws}"


#: Number of repeated same-query calls in one turn before explicitly saying another fetch is unchanged.
#: Choose two: first fetch obtains data, second may be an ordinary rephrasing loop; from the third, the model is spinning.
_BI_REPEAT_WARN_AT = 2

#: Store counts in the same _TURN_QUERY_CACHE bucket (@rep: prefix, like existing @sem:).
#: Another dict would need separate LRU eviction and turn-end cleanup, duplicating lifecycle management and eventually leaking.
_BI_REPEAT_KEY_PREFIX = "@rep:"


def _bi_bump_repeat(turn_id: str, key: str) -> int:
    """记一次「同参数又查了一遍」，返回累计命中次数（不含首次取数）。"""
    if turn_id not in _TURN_QUERY_CACHE:
        return 1
    bucket = _TURN_QUERY_CACHE[turn_id]
    rk = _BI_REPEAT_KEY_PREFIX + key
    n = int(bucket.get(rk) or 0) + 1
    bucket[rk] = n
    return n


def _repeat_guarded(cached: Any, hits: int) -> Any:
    """命中缓存到第 `_BI_REPEAT_WARN_AT` 次起，在结果里明说「别再查了」。

    ⚠ 缓存本身**已经**挡住了重复取数的开销（116 实测 21 次调用只落了 5 个 artifact、
    18 次命中缓存），挡不住的是**循环本身**：`t_a091c1b59a78` 里同一参数连发 16 次，
    每次都要一个完整的 LLM 往返，51 秒的轮次几乎全耗在这里。
    模型缺的不是数据，是一句「这条已经查过了，再查不会变」。

    ⚠ 必须返回**副本**：直接改缓存里的对象会把这句话固化进缓存，下一次命中就成了陈述历史。
    """
    if hits < _BI_REPEAT_WARN_AT or not isinstance(cached, dict):
        return cached
    out = dict(cached)
    note = (
        f"⚠ 本轮已经用**完全相同的参数**查过 {hits + 1} 次，这次返回的是同一份缓存结果——"
        "再查一次不会有任何变化。要全量数据请用 `artifact_id` 走 python_sandbox；"
        "要别的数据请改查询条件，别重复同一条。"
    )
    prev = str(out.get("_preflight_hint") or "").strip()
    out["_preflight_hint"] = f"{note}\n{prev}" if prev else note
    return out


def _bi_cache_get(turn_id: str, key: str) -> Any | None:
    bucket = _TURN_QUERY_CACHE.get(turn_id)
    return bucket.get(key) if bucket else None


def _bi_cache_put(turn_id: str, key: str, value: Any) -> None:
    if turn_id not in _TURN_QUERY_CACHE:
        _TURN_QUERY_CACHE[turn_id] = {}
        _TURN_QUERY_CACHE_LRU.append(turn_id)
        # FIFO eviction (safety net; primary cleanup is cleanup_turn_query_cache hook)
        while len(_TURN_QUERY_CACHE_LRU) > _TURN_QUERY_CACHE_MAX_TURNS:
            evict = _TURN_QUERY_CACHE_LRU.pop(0)
            _TURN_QUERY_CACHE.pop(evict, None)
    _TURN_QUERY_CACHE[turn_id][key] = value
    # SRE observability — size monitoring for memory cap planning
    try:
        from bowhead_core import metrics as _m

        total = sum(len(b) for b in _TURN_QUERY_CACHE.values())
        _m.observe("bowhead_bi_query_cache_size_total", total, labels={}, help="λ-F1: total cached entries across all live turns.")
    except Exception:  # noqa: S110
        pass


# ── Datarc time-period drift detection (started 2026-08-25; notation expanded after adversarial review on 2026-08-26) ──
#
# Datarc can replace requested periods without errors and with normal result shapes, in two forms:
#
#   ② Year fallback: 2026年11月 营业额 actually queries 2025-11-01~30, byte-identical to 2025年11月 results.
#      Detectable: replaced periods name CSVs as explicit ranges (2025-11-01_至_30营业额.csv),
#      while unchanged periods echo user notation (2025年11月营业额.csv); the filename shape itself is a red flag.
#   ① Truncated interval: 2026年1-12月 … 每月 returns only January–August (months with data).
#      Filenames cannot detect this (echoed unchanged); count returned month coverage instead.
#
# ★★ Why neither criterion falsely flags: tested 518 actual 116 production (origin_query, filename) pairs
#    plus 51 live probes on 2026-08-26, zero false positives. Without changed periods, persisted names roughly strip query
#    whitespace and 各, then add a hash, preserving date phrases. _extract_period therefore makes the same mistake
#    on request and filename sides ⇒ want == got ⇒ silent. Essentially a date-string-shape differencer,
#    speaking only when datarc actually rewrites dates, precisely the drift signal.
#    ⇒ Expand notation symmetrically using the same _extract_period on both sides. Request-only special cases immediately
#      break symmetry, turning zero false positives into daily false alarms. Before editing, run test_bi_period_drift.py's
#      live golden-pair cases.
#
# ⚠ Two explicitly uncovered cases (not proof of passing):
#   - Year only (2026年 / 2026 revenue): scalar returns have no signal. Monthly grouping could theoretically count,
#     but 2026年 usually means YTD; warning of missing September–December asserts an interval the user never requested,
#     so deliberately omit it. Coverage requires upstream actual execution intervals.
#   - Relative periods / fiscal years (今年 / 近30天 / 2026财年): cannot parse absolute year/month ⇒ fail open.
#
# Anchor on text (original query sent to datarc), not _parse from parallel _cut_safe,
# observed intermittently empty. More importantly, in eight drift cases on 2026-08-26, cut dateRange_* all reported
# requested rather than executed periods (2026年11月 → dateRange_2026110120261130, actually executing
# 2025-11), structurally the wrong signal source rather than occasionally empty.
# Fail open throughout: if either side cannot parse year/month, omit warnings; prefer missed detection over false alarms.

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4}
_EN_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# Persisted format when datarc rewrites: 2025-11-01_至_30 / 2025-09-01_至_12-31 / 2026-08-11_至_2026-08-17.
# ⚠ Recognize MM in ending MM-DD. Previously "12" in _至_12-31 was interpreted as day, silently
# losing the ending month (2025-09-01_至_12-31 parsed only September), making same-year rewrite warnings report incorrect months.
_ISO_SEP = r"(?:[_\s]*(?:至|到|~|～)[_\s]*|[_\s]+to[_\s]+|_to_)"
_PERIOD_ISO_RE = re.compile(
    r"(20\d{2})-(\d{1,2})-\d{1,2}" + _ISO_SEP + r"(?:(?:(20\d{2})-)?(\d{1,2})-)?\d{1,2}",
)

# Chinese: year / year-month / month range / quarter / half-year. Ranges cover 1-8月, 1月-8月, 1月至2026年8月,
# and 6月到12月. Observed in the 116 production corpus on 2026-08-26: 52% of monthly-range queries used the last three;
# the old version recognized only the first, silencing truncation detection precisely where needed.
_PERIOD_CN_RE = re.compile(
    r"(?P<y>20\d{2})\s*年\s*"
    r"(?:"
    r"(?P<lo>\d{1,2})\s*月?\s*[-–—~～至到]\s*(?:(?P<y2>20\d{2})\s*年\s*)?(?P<hi>\d{1,2})\s*月"
    r"|(?P<mo>\d{1,2})\s*月"
    r"|前\s*(?P<pq>[1-4一二三四])\s*季度"
    r"|(?:第\s*)?(?P<q>[1-4一二三四])\s*季度"
    r"|[Qq](?P<q2>[1-4])"
    r"|(?P<half>上|下)半年"
    r")?",
)

# Recognize English month names as well as localized and ISO periods so drift
# checks cover every supported query-language form.
_EN_MON = (
    r"(?:january|february|march|april|june|july|august|september|october|november|december"
    r"|sept|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(?![a-z])"
)
_PERIOD_EN_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"(?P<y1>20\d{2})[\s_,./-]{0,3}(?P<a_lo>" + _EN_MON + r")(?:[\s_]*[-–—~][\s_]*(?P<a_hi>" + _EN_MON + r"))?"
    r"|(?P<b_lo>" + _EN_MON + r")(?:[\s_]*[-–—~][\s_]*(?P<b_hi>" + _EN_MON + r"))?[\s_,./-]{0,3}(?P<y2e>20\d{2})"
    r")(?!\d)",
    re.IGNORECASE,
)
# ⚠ No immediately following CJK: 2026财年 often crosses calendar years (Apr–Mar); interpreting it as calendar 2026 falsely
# warns of year changes when datarc truthfully returns 2025-04-01_至_2026-03-31. Fail open is safer.
_PERIOD_EN_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?![\d\u4e00-\u9fff])")

# Only names containing these markers can be time-grouping columns (每月 / 每季度 / month).
# Filter columns first, then extract per row; previously every key in every row was substring-checked,
# synchronously blocking the event loop for a measured 0.86s over 400k×20 columns in the 116 container.
_PERIOD_COL_HINT_RE = re.compile(r"月|季|month|quarter", re.IGNORECASE)


def _months_of_quarter(q: int) -> tuple[int, ...]:
    return tuple(range(q * 3 - 2, q * 3 + 1))


def _period_from_iso(s: str) -> tuple[int, tuple[int, ...]] | None:
    """ISO 区间 —— datarc **改写掉时间段时自己写的**落盘格式。"""
    m = _PERIOD_ISO_RE.search(s)
    if not m:
        return None
    y0, m0, y1, m1 = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    if not 1 <= m0 <= 12:
        return (y0, ())
    if y1 and int(y1) != y0:
        return (y0, ())  # Cross-year range: this function represents only one year; degrade to year-level.
    hi = int(m1) if m1 else m0
    if not 1 <= hi <= 12 or hi < m0:
        return (y0, ())
    return (y0, tuple(range(m0, hi + 1)))


def _period_from_cn(s: str) -> tuple[int, tuple[int, ...]] | None:
    m = _PERIOD_CN_RE.search(s)
    if not m:
        return None
    year = int(m.group("y"))
    # ⚠ Recognized year but invalid month (2026年13月 / 1-13月) ⇒ degrade to year-level, not None,
    # which loses the year too. On 2026-08-26, 2026年13月 营业额 actually fell back to 2025-12.
    if m.group("lo") and m.group("hi"):
        if m.group("y2") and int(m.group("y2")) != year:
            return (year, ())  # Cross-year range: degrade to year-level.
        lo, hi = int(m.group("lo")), int(m.group("hi"))
        if not (1 <= lo <= 12 and 1 <= hi <= 12) or hi < lo:
            return (year, ())
        return (year, tuple(range(lo, hi + 1)))
    if m.group("mo"):
        mo = int(m.group("mo"))
        return (year, (mo,) if 1 <= mo <= 12 else ())
    if m.group("pq"):  # `前三季度`
        n = _CN_DIGITS.get(m.group("pq")) or int(m.group("pq"))
        return (year, tuple(range(1, n * 3 + 1)))
    if m.group("q") or m.group("q2"):  # `第一季度` / `1季度` / `Q1`
        raw = m.group("q") or m.group("q2")
        q = _CN_DIGITS.get(raw) or int(raw)
        return (year, _months_of_quarter(q))
    if m.group("half"):
        return (year, tuple(range(1, 7)) if m.group("half") == "上" else tuple(range(7, 13)))
    return (year, ())  # Year only


def _period_from_en(s: str) -> tuple[int, tuple[int, ...]] | None:
    ym = _PERIOD_EN_RE.search(s)
    bare = _PERIOD_EN_YEAR_RE.search(s)
    if ym is not None and (bare is None or ym.start() <= bare.start()):
        year = int(ym.group("y1") or ym.group("y2e"))
        lo_tok = ym.group("a_lo") or ym.group("b_lo")
        hi_tok = ym.group("a_hi") or ym.group("b_hi")
        lo = _EN_MONTHS[lo_tok[:3].lower()]
        if hi_tok is None:
            return (year, (lo,))
        hi = _EN_MONTHS[hi_tok[:3].lower()]
        return (year, tuple(range(lo, hi + 1)) if hi >= lo else ())
    if bare is not None:
        return (int(bare.group(1)), ())
    return None


def _extract_period_tagged(s: str) -> tuple[int, tuple[int, ...], str] | None:
    """Extract a period with its ISO, Chinese, or English source tag.

    Preserve ISO > Chinese > English precedence. Monthly drift checks use ISO dates
    from rewritten queries without changing existing localized parsing.
    """
    if not s:
        return None
    for src, finder in (("iso", _period_from_iso), ("cn", _period_from_cn), ("en", _period_from_en)):
        hit = finder(s)
        if hit is not None:
            return (hit[0], hit[1], src)
    return None


def _extract_period(s: str) -> tuple[int, tuple[int, ...]] | None:
    """从一段文本里抠出 (年, 月份元组)。月份为空 = 只认出年份，没点到具体月。

    认三族写法，**请求原文和 datarc 落盘文件名走的是同一个函数**（对称性见模块顶部）：
      - 中文：`2026年` / `2026年11月` / `2026年1-8月` / `2026年1月至2026年8月`
              / `2026年Q1` / `2026年第一季度` / `2026年上半年` / `2026年前三季度`
      - ISO（datarc 改写后自己写的）：`2025-11-01_至_30` / `2025-09-01_至_12-31`
      - 英文：`November 2026` / `2026 Jan-Jul` / `Jan-Dec 2026` / `revenue for 2026`
    认不出返回 None（调用方 fail-open）。
    """
    hit = _extract_period_tagged(s)
    return (hit[0], hit[1]) if hit else None


def _unfinished_period_note(query_text: str) -> str | None:
    """③ 未完结当期检测：请求区间含**今天所在的那个月** → 警告。

    与 `_month_coverage_note` 不是一回事：那条管「请求 12 个月只回了 3 个季度」——**月份缺失**；
    这条管「9 个月一个不少，但最后那个月只过了 10 天」——**月内不完整**。前者按月份集合比对，
    天然看不见后者。

    取证（09-10 116 dogfood，`s_ff7b38fe79ca`）：问「今年各品类表现」，取 1-9 月而当天是 9 月 10 号。
    产品把半个月的 9 月与完整月份直接比，写出「9月环比骤降 60%-76%、季节性收缩显著」，做成 KPI 卡，
    并据此给了**采购建议**。09-19 复跑同场景量出量级：含半个月 9 月时全年同比 -29.7%「系统性下滑」，
    排除后 +0.08% —— 半个月把一个基本持平的年份显示成崩塌，差约 30pp。

    ⚠ 判据只用**我们自己发出去的问句** + 模型时钟的今天（model_clock），不解析 datarc 回的
    `message_json.date_filters`：那些值是随成员账户语言变的展示串，datarc 明写
    「不应依赖数组元素内部的文本格式进行查询」。等它给出机器可读的起止日，换掉这里的输入源即可，
    判定逻辑不动。

    fail-open：问句里没点出年月（`_extract_period` 返 None）就不报——宁可少报，不猜。
    """
    want = _extract_period(query_text or "")
    if want is None:
        return None
    year, months = want
    today = model_today()
    if year != today.year:
        return None  # Past years are completed; future years are outside this criterion.
    # Recognized year without month (YTD notations like 2026年 / 今年) ⇒ interval extends through today, necessarily including this month.
    if months and today.month not in months:
        return None
    return (
        f"⚠ 请求区间含**尚未结束**的 {year}年{today.month}月（今天 {today.isoformat()}）："
        f"该月只有部分天数的数据。与完整月份做环比/同比会系统性偏低，"
        f"把它当成「下滑」是取数口径的产物、不是业务现象。"
        f"做月度趋势/环比时请排除 {today.month} 月，或在结论里明确标注它是不完整月份。"
    )


def _period_drift_note(query_text: str, csv_filename: str | None) -> str | None:
    """② 跨年/换段检测：请求原文 vs datarc 实际落盘文件名。不一致 → 警告文案。

    ⚠ 文案里**不要回显文件名**：`model_view._BACKEND_ONLY_KEYS` 专门把
    `_debug_csv_filename` 从模型面摘掉（artifact-addressing 2×2 实测：同一个槽位多一个
    "看起来也像路径的串"，正确率 100% → 85.9%）。经这条 hint 把同一个串送回去等于绕过
    那道摘除，而年份/月份本身已经把话说清楚了，文件名对模型没有用。
    """
    want = _extract_period(query_text or "")
    got = _extract_period_tagged(csv_filename or "")
    if want is None or got is None:
        return None  # fail-open
    wy, wm = want
    gy, gm, gsrc = got
    if (wy, wm) == (gy, gm):
        return None
    if wy != gy:
        return (
            f"🚨 datarc 实际查询的是 **{gy} 年**，而你要的是 {wy} 年。这是**换了一年的数据**，不是你要的口径——不要直接使用；如实说明该时间段无数据。"
        )
    # Compare months only when filename is an explicit ISO range, the sole form where datarc actually changed dates.
    # Truthful echoes parse the same text twice; differences can only arise from parser asymmetry
    # (observed: datarc joins words, leaving no word boundary before jan in …jan-dec_2026).
    # That is purely false detection; _month_coverage_note counts actual returned rows for more accurate monthly truncation.
    if gsrc == "iso" and wm and gm and set(wm) != set(gm):
        return f"⚠ datarc 实际查询的月份是 {sorted(set(gm))}，而你要的是 {sorted(set(wm))}。请按实际区间叙述，或说明缺失月份。"
    return None


def _month_coverage_note(query_text: str, rows: list[Any]) -> str | None:
    """① 区间截断检测：按月（或按季）分组时，返回的月份少于请求区间 → 警告。

    只在结果里真有时间分组列时生效（`每月` / `每季度`）；否则 fail-open。

    ⚠ 按季分组时一个季度算作它的三个月：好处是「整季缺失」抓得到（实测
    `2026年1-12月 … 每季度` 只返 Q1/Q2/Q3），代价是**季内缺月看不见**
    （Q3 实际只有 7、8 两个月的数，这里仍会把 9 月算成已覆盖）。
    比旧版「`每季度` 这列压根不看」严格更好，但别当成月级完整性证明。

    性能：`rows` 是**全量**（116 上单条查询实测最大 92 MB CSV）。所以先从首行 keys 里筛出
    候选列、再逐行只取那几列，并且 `seen` 一凑齐就早停 —— 旧版逐行扫每个 key，
    400k×20 列在 116 容器上实测 0.86 s，而这段是**同步跑在事件循环上**的。
    """
    want = _extract_period(query_text or "")
    if want is None or len(want[1]) <= 1:
        return None  # No interval (or one month only) ⇒ cannot compare.
    year, months = want
    first = next((r for r in rows or [] if isinstance(r, dict)), None)
    if first is None:
        return None
    period_cols = [k for k in first if _PERIOD_COL_HINT_RE.search(str(k))]
    if not period_cols:
        return None
    want_set = set(months)
    seen: set[int] = set()
    # One column may contain millions of rows but only a dozen month labels; memoize values so regexes run only those few times.
    # Cap at 512 against pathological columns (unique values per row do not form time-grouping columns).
    memo: dict[str, tuple[int, tuple[int, ...]] | None] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        grew = False
        for k in period_cols:
            v = r.get(k)
            if not isinstance(v, str):
                continue
            if v in memo:
                got = memo[v]
            else:
                got = _extract_period(v)
                if len(memo) < 512:
                    memo[v] = got
            if got and got[0] == year and not seen.issuperset(got[1]):
                seen.update(got[1])
                grew = True
        if grew and seen >= want_set:
            return None  # Stop early once coverage is complete; further scanning cannot change the conclusion.
    if not seen:
        return None
    missing = sorted(want_set - seen)
    return (
        f"⚠ 你要的是 {year}年{months[0]}-{months[-1]}月，但只返回了 {len(seen)} 个月"
        f"（缺 {missing}）。datarc 会按**有数据的月份**静默截断——"
        "不要把它当成完整区间做汇总或达成率计算。"
    )


#: Target/quota measure terms: a match denotes a planned value for the entire period, not actual-to-date.
#: Deliberately narrow: 指标 also generically means metric in Chinese, but requires a queried period, no time columns in results,
#: and an unfinished window before warning. Combined false-positive cost is one extra advisory.
_TARGET_MEASURE_RE = re.compile(r"指标|目标|target|budget|quota", re.IGNORECASE)


def _uncomparable_target_note(
    query_text: str,
    cols: list[Any],
    today: dt.date | None = None,
) -> str | None:
    """Flag target windows that cannot be compared with actual-to-date data.

    Without a time column, a future or year-only target window may be incomparable.
    This complements requested/executed drift and returned-month coverage checks.
    Use the query, result names, and clock; leave unrecognized periods, missing
    targets, and results with time columns to their existing handling.
    """
    if not _TARGET_MEASURE_RE.search(query_text or ""):
        return None
    # A time-grouping result column lets the model trim to completed months; outside this criterion.
    if any(_PERIOD_COL_HINT_RE.search(str(c)) for c in cols or ()):
        return None
    want = _extract_period(query_text or "")
    if want is None:
        return None  # fail-open
    year, months = want
    end_month = max(months) if months else 12  # Year only ⇒ entire year.
    now = today or model_today()
    if (year, end_month) < (now.year, now.month):
        return None  # Window closes before the last completed month ⇒ comparable.
    ly, lm = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    # Keep advice to the same question: one month for a monthly request, a range only for a range request.
    suggestion = f"{ly}年{lm}月" if len(months) == 1 else f"{ly}年1-{lm}月"
    return (
        f"⚠ 这条取的是**目标/指标类**度量，窗口覆盖到 {year}年{end_month}月，"
        f"而今天是 {now.year}年{now.month}月{now.day}日 —— 这一段还没走完，"
        "而实际发生数只到今天。**这一列不能直接和实际值相减**（算出来的缺口会被整段"
        "目标撑大，各分项加起来会超过总数）。\n"
        f"要算达成率/缺口，二选一：① 把窗口改成上一个完整月（{suggestion}）；"
        "② 改成带 `每月` 的查询，自己裁到完整月再汇总。"
    )


def _column_units(ctx: Any, cols: list[Any]) -> dict[str, str]:
    """Expose cached measure-unit metadata on query results.

    Prefer currency_code, then unit_name. Do not guess units or add network requests;
    skip enrichment when the client, workspace, or cached metadata is unavailable.
    """
    client = getattr(ctx, "datarc", None)
    if client is None or not hasattr(client, "measure_metadata"):
        return {}
    # ⚠ Do not use _ws(): it is an internal build_default_registry closure, inaccessible at module scope.
    # _ws(ctx) raises NameError, swallowed by fail-open below, making this function always return {},
    # silent dead code (the initial 2026-09-17 version did this; only tests caught it).
    # resolve_datarc_project() is already the sole workspace→datarc project boundary.
    resolve = getattr(ctx, "resolve_datarc_project", None)
    if not callable(resolve):
        return {}
    try:
        ws = resolve()
    except Exception:
        return {}
    if not ws:
        return {}
    units: dict[str, str] = {}
    for col in cols or ():
        name = str(col)
        try:
            hit = client.measure_metadata(ws, name)
        except Exception:  # noqa: S112 — Missing units must not affect retrieval.
            continue
        if not hit:
            continue
        label = str(hit.get("currency_code") or "").strip() or str(hit.get("unit_name") or "").strip()
        if label:
            units[name] = label
    return units


def _bi_result_is_error(result: Any) -> bool:
    """λ-F1 P0-1: skip caching errors to prevent poisoning."""
    if not isinstance(result, dict):
        return False  # non-dict (e.g. legacy) — let it cache, no error markers
    return bool(result.get("error_kind") or result.get("error_code") or result.get("error"))


def cleanup_turn_query_cache(turn_id: str) -> int:
    """λ-F1 P1-7: explicit cleanup hook called from turn_runner / lead orchestrator
    on turn done/cancel/fail. Returns entries dropped. Idempotent.

    Wired at:
      - lead_orchestrator.py:1810 (done chokepoint, alongside drain_pending_findings)
      - turn_runner.py:944 (cancel path)
    """
    bucket = _TURN_QUERY_CACHE.pop(turn_id, None)
    if turn_id in _TURN_QUERY_CACHE_LRU:
        _TURN_QUERY_CACHE_LRU.remove(turn_id)
    # Batch 2: clear the single-flight pool; resolve pending futures (None=fallback signal),
    # or awaiters remaining through cancellation races never wake up.
    inflight = _TURN_QUERY_INFLIGHT.pop(turn_id, None)
    if inflight:
        for _fut in inflight.values():
            if not _fut.done():
                _fut.set_result(None)
    dropped = len(bucket) if bucket else 0
    # SRE P1-SRE-1 (post-review): observe cleanup count so we can monitor
    # explicit-hook vs FIFO-eviction ratio (high FIFO = wire missing somewhere).
    try:
        from bowhead_core import metrics as _m

        _m.observe("bowhead_bi_query_cache_cleanup_dropped", dropped, labels={}, help="λ-F1: entries dropped by explicit cleanup hook per turn.")
    except Exception:  # noqa: S110
        pass
    return dropped


# Handler signature
ToolHandler = Callable[[dict[str, Any], "ToolExecutionContext"], Awaitable[Any]]


def _slice_balanced_json(s: str) -> str:
    """从 s 第一个 `{` 开始扫到 brace 配平的位置，截尾部冗余.

    实测 Qwen 偶尔在 valid JSON 后追加 garbage（"Extra data: line 1 col N"）；
    截到第一个 balanced 位置 json.loads 就能成功.
    引号内的 `{ }` 不计入；处理 `\\"` 转义.
    """
    if not s:
        return s
    start = s.find("{")
    if start < 0:
        return s
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s  # Unbalanced → return unchanged


def _unwrap_tool_args(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """把 LLM 脏 tool args 串恢复成 dict.

    支持三类污染（按实测 Doubao-Seed-2.0-pro / Volces Ark /api/coding 行为分类）：

    1. `<text_never_used_<hex>>` 标记（prompt-cache 压缩残留）
    2. JSON 合法，但 `input` 字段是 JSON-string 而非 object（LLM 双层编码）
    3. tool-call XML 污染（`"input" type="object">\\n<property name="code">...</property>`）

    返 `(parsed_dict, None)` 或 `(None, err_msg)`.
    """
    import json as _json
    import re as _re

    # 1. Canary markers
    cleaned = _re.sub(r"<text_never_used_[0-9a-fA-F]+>", "", raw)

    # 1.5 Mixed-in XML tool-call fragments: stripping these often lets json.loads succeed. qwen/doubao
    # have been observed leaking `<parameter name="x" type="T">V</parameter>` into args JSON under pressure.
    # Typical contamination: `{"a": 1, "b" type="integer">20</parameter><parameter name="c">"x"}`
    #   step 1: Remove </parameter>
    #   step 2: `<parameter name="X">` → `, "X": `
    #   step 3: `<parameter name="X"` (without >) → `, "X"`
    #   step 4: `"field" type="T">` → `"field": ` (XML attributes directly after the field name)
    def _strip_xml_parameter_contamination(s: str) -> str:
        # Remove wrapper tags such as `</parameter>` / `<tool_input>` / `<arguments>`
        s = s.replace("</parameter>", "")
        s = _re.sub(r"</?\s*tool_input[^>]*>", "", s)
        s = _re.sub(r"</?\s*arguments[^>]*>", "", s)
        s = _re.sub(r"<parameter\s+name=\"([^\"]+)\"\s*(?:type=\"[^\"]*\"\s*)?>", r', "\1": ', s)
        s = _re.sub(r"<parameter\s+name=\"([^\"]+)\"(?=[^>]*:)", r', "\1"', s)
        # field-level type attr：`"limit" type="integer">20` → `"limit": 20`
        s = _re.sub(r"(\"[^\"]+\")\s*type=\"[^\"]*\"\s*>", r"\1: ", s)
        # Remove redundant commas introduced above (`, , "x"` / `{, "x"`)
        s = _re.sub(r"\{\s*,", "{", s)
        s = _re.sub(r",\s*,", ",", s)
        s = _re.sub(r",\s*\}", "}", s)
        # Fallback: if it does not start with `{` (LLM omitted outer braces), try adding them
        t = s.strip()
        if t and not t.startswith("{"):
            # Strip leading/trailing commas + wrap in {}
            t = t.strip(",").strip()
            if t.startswith(('"', "'")):
                s = "{" + t + "}"
        return s

    # 2a. Try json.loads directly; on failure, strip XML contamination and retry
    last_err: str | None = None
    try:
        parsed = _json.loads(cleaned)
    except (ValueError, TypeError) as e:
        last_err = f"json.loads: {e}"
        parsed = None

    if parsed is None:
        stripped = _strip_xml_parameter_contamination(cleaned)
        if stripped != cleaned:
            try:
                parsed = _json.loads(stripped)
                last_err = None
                cleaned = stripped
            except (ValueError, TypeError) as e:
                last_err = f"json.loads after xml strip: {e}"

    # 2a.5 Python literal contamination (Qwen sometimes writes None / True / False in JSON instead of
    # null / true / false) + trailing extra characters ("Extra data"): two additional repair attempts.
    if parsed is None:
        # (i) Replace Python literals with JSON literals
        repaired = _re.sub(r"\bNone\b", "null", cleaned)
        repaired = _re.sub(r"\bTrue\b", "true", repaired)
        repaired = _re.sub(r"\bFalse\b", "false", repaired)
        # (ii) Slice balanced braces to obtain the first complete JSON object
        repaired = _slice_balanced_json(repaired)
        if repaired and repaired != cleaned:
            try:
                parsed = _json.loads(repaired)
                last_err = None
                cleaned = repaired
            except (ValueError, TypeError) as e:
                last_err = f"json.loads after py-literal+balance fix: {e}"

    # 2a.7 Final fallback: json-repair, the established LLM JSON repair library
    # Handles single quotes, missing commas, unclosed structures, trailing commas, damaged Unicode, etc.
    if parsed is None:
        try:
            from json_repair import repair_json

            repaired = repair_json(cleaned, return_objects=True)
            if isinstance(repaired, dict):
                parsed = repaired
                last_err = None
            elif isinstance(repaired, str) and repaired:
                # If repair returns a string, parse it again
                try:
                    parsed = _json.loads(repaired)
                    last_err = None
                except (ValueError, TypeError):
                    pass
        except ImportError:
            logger.debug("json_repair not installed; skip last-resort fix")
        except Exception as e:
            last_err = f"json_repair failed: {e}"

    if isinstance(parsed, dict):
        # 2b. Repair common nested fields whose value is a JSON string rather than an object (LLM double encoding).
        # Observed fields: `input` (python_sandbox) / `payload` (subagent_return, M137) /
        # `args` (skill_read). Decode one level for every field whose value is a valid JSON object string.
        for k, v in list(parsed.items()):
            if isinstance(v, str):
                inner = v.strip()
                if (inner.startswith("{") and inner.endswith("}")) or (inner.startswith("[") and inner.endswith("]")):
                    try:  # noqa: SIM105
                        parsed[k] = _json.loads(inner)
                    except (ValueError, TypeError):
                        pass  # Leave unchanged so the handler reports the error
        return parsed, None

    # 3. XML contamination recovery: extract key fields from `"name": "xxx"` / `"name">xxx</...>` / `<property name="xxx">VAL</property>`
    # patterns. Try only a few common fields (name / script / input.code / input.inputs).
    result: dict[str, Any] = {}

    # name
    m = _re.search(r'"name"\s*:\s*"([^"]+)"', cleaned) or _re.search(r'"name"\s*>\s*([^<]+?)\s*</', cleaned)
    if m:
        result["name"] = m.group(1).strip()

    # script
    m = _re.search(r'"script"\s*:\s*"([^"]+)"', cleaned) or _re.search(r'"script"\s*>\s*([^<]+?)\s*</', cleaned)
    if m:
        result["script"] = m.group(1).strip()

    # code: either `"code": "..."` (with escaped \n) or `<property name="code">...</property>` (with newlines)
    code: str | None = None
    m = _re.search(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, _re.DOTALL)
    if m:
        try:
            code = _json.loads(f'"{m.group(1)}"')  # Unescape
        except (ValueError, TypeError):
            code = m.group(1).encode("utf-8").decode("unicode_escape")
    if code is None:
        m = _re.search(
            r'<property\s+name="code"[^>]*>\s*(.+?)\s*</property>',
            cleaned,
            _re.DOTALL,
        )
        if m:
            code = m.group(1)

    # inputs: `<property name="inputs">` or `"inputs": {...}`; try only the JSON form
    inputs: dict[str, Any] | None = None
    m = _re.search(
        r'"inputs"\s*:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})',
        cleaned,
        _re.DOTALL,
    )
    if m:
        try:
            inputs = _json.loads(m.group(1))
        except (ValueError, TypeError):
            inputs = None

    if code is not None or inputs is not None:
        result["input"] = {}
        if code is not None:
            result["input"]["code"] = code
        if inputs is not None:
            result["input"]["inputs"] = inputs

    if result:
        return result, None

    return None, last_err or "could not recover structured args from raw string"


def _caller_sandbox_entry(ctx: Any) -> str | None:
    """Read the runner's advertised, Skill-authorized sandbox entry for guidance.

    The snapshot survives peer exit; require the active subagent marker so a
    later caller cannot receive the preceding peer's invocation shape.
    A generic Skill invoker alone does not establish sandbox permission.
    """
    if not getattr(ctx, "_current_subagent_id", None):
        return None
    return getattr(ctx, "_running_sandbox_entry", None)


@dataclass(frozen=True)
class TurnIdentity:
    """#104 v1 封装：turn 身份五元组（workspace/member/user/turn/session）.

    纯值对象. handler 读 identity 时做 structured log / metric label 一步到位，
    不再散读 5 次 `ctx.X`. 未来若再抽 infra / hooks 类可循此模式.
    """

    workspace_id: str
    member_uid: str
    user_id: str
    turn_id: str
    session_id: str = ""

    def as_log_tags(self) -> dict[str, str]:
        """给结构化 log / metrics label 用."""
        return {
            "workspace_id": self.workspace_id,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
        }


@dataclass
class BIOutageLatch:
    """Share a mutable BI outage marker across shallow-copied execution contexts.

    A scalar written on a copied context would not update the caller's marker.
    """

    timeouts: int = 0
    latched_reason: str = ""


# What `format: "percentage"` means to the agent: **one definition shared by the schema/measures outputs**.
#
# Why this explanation is required (2026-08-28 live run on 116: an en-US turn displayed `1784.0%` in a table):
# BI returns 0.1784 → agent saves 17.84 after `df['利润率'] *= 100` in the sandbox → backend uses source metadata
# to mark `percent_scale=fraction` → frontend multiplies by 100 again → double scaling. Fix 4fed445de made rendering
# robust to processed values (no shape declared when the range contradicts it), but this is a **fallback**: once triggered,
# it guesses from values and discards an otherwise available shape. The root cause is that the agent does not know it need not convert
# the value itself, likely because the schema has told it `format: percentage` since b9d4a7e47.
#
# Provide usage instructions alongside facts, or we are handing the agent a trap.
_PERCENTAGE_FORMAT_NOTE = (
    '⚠ `format: "percentage"` 的指标，取回的**原始值就是 0-1 的比率**'
    "（利润率 0.1784 = 17.84%）。渲染层会负责乘 100 显示，"
    "**不要在沙箱里自己 ×100**——乘过之后后端仍按源元数据当比率再乘一次，"
    "用户看到的是 1784%（116 真机实录）。需要在散文里写百分数时直接写换算后的数，"
    "但**落进 CSV / 图表数据的那一列保持原值**。"
)


def _measure_public(m: Any, locale: str = "en-US") -> dict[str, Any]:
    """measure 的模型面投影。

    `unit` / `format` 是 2026-08-24 补上的——datarc `/measures/` 一直带 unitName，
    `format`（"float"/"int"/"percentage"）来自模型层，此前两个都被丢掉，于是
    「达成率是 0-1 比率」这件事在模型面完全不可见，只能靠每个客户在自己的 skill
    里手写一遍，或者靠下游按值猜（猜必错：达成率 1.02=102%、市场份额 2.45=2.45%）。
    取不到就不出这两个键，模型面与老行为一致。

    No Datarc `id`: no tool accepts it, and the model refers to fields by name.
    """
    out: dict[str, Any] = {"names": m.names, "description": m.description, "sort": m.sort}
    unit = getattr(m, "unit_name", "") or getattr(m, "currency_code", "")
    if unit:
        out["unit"] = unit
    fmt = getattr(m, "value_format", "")
    if fmt:
        out["format"] = fmt
    # Display label (datarc displayName, selected by this turn's output language). Omit if equal to names[0],
    # saving tokens and preventing the model from treating it as a second queryable name.
    label = _display_label(m, locale)
    if label and label != (m.names[0] if m.names else ""):
        out["display"] = label
    return out


def _resolve_keep_cols(
    keep_cols: list[str],
    actual: list[str],
) -> tuple[list[str], list[str], list[tuple[str, list[str]]]]:
    """Resolve requested columns by normalized exact match, then unique containment.

    Return resolved, missing, and ambiguous names separately. Do not guess an
    ambiguous match or hardcode source-specific prefix transformations.
    """

    def _n(x: str) -> str:
        return unicodedata.normalize("NFKC", x or "").strip().casefold()

    by_norm: dict[str, list[str]] = {}
    for a in actual:
        by_norm.setdefault(_n(a), []).append(a)

    resolved: list[str] = []
    missing: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for req in keep_cols:
        rn = _n(req)
        if rn in by_norm:  # 1) Exact match
            resolved.append(by_norm[rn][0])
            continue
        hits = [a for a in actual if rn and rn in _n(a)]  # 2) Unique containment match
        if len(hits) == 1:
            resolved.append(hits[0])
        elif len(hits) > 1:
            ambiguous.append((req, hits))
        else:
            missing.append(req)
    return resolved, missing, ambiguous


def _dimension_public(d: Any, locale: str = "en-US") -> dict[str, Any]:
    """dimension 的模型面投影（与 measure 同规矩：names 查询、display 展示，不带 Datarc id）。"""
    out: dict[str, Any] = {
        "names": d.names,
        "type": d.type,
        "isKeyword": d.is_keyword,
        "used": d.used,
    }
    label = _display_label(d, locale)
    if label and label != (d.names[0] if d.names else ""):
        out["display"] = label
    return out


def _display_label(meta: Any, locale: str) -> str:
    """按**输出语言**挑展示标签；挑不到返回 ""（调用方就不出这个键）。

    三轴里挑 `output_language` 而不是 `data_language`：前者的定义就是
    「LLM 用户可见输出（final_answer / 图表 title）」，正是标签要跟的那条；
    `data_language` 说的是**库里存的是什么语言**，那是 `names` 该跟的轴——
    `names` 是拼给 datarc NL 引擎的查询串，翻译了就可能解析不出来。

    回落顺序：本轮语言 → 另一种语言 → 空。**不回落到 names[0]**：
    标签与 names[0] 相同时不必重复输出，调用方按空值省略即可。
    """
    dn = getattr(meta, "display_names", None)
    if not isinstance(dn, dict) or not dn:
        return ""
    label = str(dn.get(locale) or "").strip()
    if label:
        return label
    for other in dn.values():
        if str(other or "").strip():
            return str(other).strip()
    return ""


def _out_lang(ctx: Any) -> str:
    """Use the turn output language, falling back to the English product default."""
    lc = getattr(ctx, "language_context", None)
    return str(getattr(lc, "output_language", "") or "en-US")


# ── Lexical criteria for row-level retrieval (routing for bi-outage-latch) ──────────────
#
# datarc parses these words into `keyword_detail` tokens, requesting **row-level details** rather than aggregation.
# Measured on 116: with this word, 133 seconds / 1 million rows; without it, 4 seconds / 38 rows (same data, dimensions and measures).
#
# ⚠ Use **lexical** checks only: on timeout we cannot obtain datarc's `_parse` (it arrives with the result,
# and this is precisely the query that did not return). Lexical checks can err, but the costs here are symmetric and small:
# false row-level match ⇒ skip one latch activation (one extra 60-second window); missed match ⇒ old behavior (activate latch).
_ROW_LEVEL_HINT_WORDS = ("明细", "逐笔", "原始记录", "流水", "每一笔", "raw", "detail")


def _looks_row_level(text: str) -> bool:
    """这条查询是不是在要行级原始记录（而不是按维度聚合）。"""
    low = (text or "").lower()
    return any(w in low for w in _ROW_LEVEL_HINT_WORDS)


@dataclass
class ToolExecutionContext:
    """传给 handler 的运行时上下文."""

    workspace_id: str
    member_uid: str
    user_id: str
    turn_id: str
    datarc: DatarcClient
    # Server-owned worker fence for shared pause admission.
    execution_generation: int = 1
    execution_owner_id: str | None = field(default=None, repr=False)
    # Shared with per-call copies; private retained-loop material grants no Tool access.
    ordinary_lead_state: Any = field(default=None, repr=False)
    ordinary_call_identity: tuple[str, str] | None = field(default=None, repr=False)
    # Server-owned collaboration adapter and exact retained Tool identity.
    # A copied context does not admit a participant or grant source access.
    collaboration_runtime: Any = field(default=None, repr=False)
    collaboration_store: Any = field(default=None, repr=False)
    collaboration_call: Any = field(default=None, repr=False)
    collaboration_execution: Any = field(default=None, repr=False)
    # Latch timed-out row-level queries to avoid repeated sequential outage waits.
    # The marker belongs to an execution context, not the whole turn: Lead and peers
    # have distinct contexts. Concurrent sibling calls can already be in flight before
    # either sets the latch; this is not an in-flight synchronization mechanism.
    bi_outage: BIOutageLatch = field(default_factory=BIOutageLatch)
    # P0-SEC-1: project_id for cross-project scope filtering. Sprint θ propagates
    # this from sctx (set by gateway from request) so KB / fact-store calls in
    # tool handlers can apply project-scoped filters. None = workspace-global
    # (current default for tenants without project_id partitioning).
    project_id: str | None = None
    # The selected Power BI team tool binds its own connection for one call.
    pbi: Any | None = None
    # Subagent spawning (used only by the subagent_spawn handler; other handlers do not touch it)
    subagent_runner: Any | None = None
    parent_bus: Any | None = None
    # Server-derived exact personal MCP dispatch identity; never model input.
    personal_mcp_license_check: Callable[[str], Awaitable[bool]] | None = None
    personal_mcp_execution_id: str | None = None
    personal_mcp_call_id: str | None = None
    personal_mcp_labels: dict[str, str] = field(default_factory=dict)
    # Vector store (integrated in M2.6a; used by preflight / memory / skill queries)
    # None means gateway has no vector_db configured or vector_db_enabled=False; handlers should skip gracefully
    vector_client: Any | None = None
    # Skill filtering: skill_list / skill_read handlers use user_id to look up the current user's enabled set.
    # None = no filtering (legacy paths / unit-test compatibility); injected when gateway starts.
    skill_instance_store: Any | None = None
    # workflow-solidify S4c —— team_skill resolve adapter（TeamSkillStoreAdapter，get_by_name
    # duck-type). None = no team skill resolution (flag off / unit tests); turn_runner injects it according to the flag.
    team_skill_store: Any | None = None
    # content-permissions S4 (tech-design §2.1 prefetch mode): IDs prefetched with one SQL query at turn start for
    # "分享给我" resources (deny-wins already subtracted). None = sharing off / not injected → checkpoints fall back to
    # visibility-only filtering (fail-safe: sharing has no effect, without excess access). Snapshot semantics: mid-turn revocation does not affect this turn.
    granted_team_subagent_ids: frozenset[str] | None = None
    granted_team_skill_ids: frozenset[str] | None = None
    # Built-in subagent management (tech-design §4.1 prefetch mode): one SQL prefetch at turn start of
    # {built-in name: BuiltinSubagentOverride} (the team's delta over system built-ins). Zero SQL on the spawn hot path.
    # None = flag off / not injected → both seams use all code defaults (fail-safe: governance has no effect; turn does not fail).
    builtin_subagent_overrides: dict[str, Any] | None = None
    # Capability Center disabling (tech design §8.3): team/user disable settings read once at turn start.
    # None = no disable state injected; no run-time removal.
    resource_disable_state: Any | None = None
    # Team agent run limits for this turn (FEATURE_AGENT_RUN_LIMITS); None when the feature is off.
    agent_run_limits: Any | None = None
    # Server-resolved Team sandbox/HTML policy shared by the active turn.
    execution_limits: Any | None = None
    # Skill catalog index (bowhead_skill.SkillCatalog): refresh() after skill_save_generated saves
    # lets Lead immediately skill_read the new skill within the same turn
    skill_catalog: Any | None = None
    # Server-derived permissions for the actual Agent, never model arguments.
    skill_runtime_scope: ExactSkillRuntimeScope | None = None
    skill_runtime_grants: tuple[Any, ...] | None = field(default=None, repr=False)
    assigned_skill_material: Any | None = field(default=None, repr=False)
    # Internal initial read-only adapter. Reconstructed contexts must restore
    # the durable association before this can be enabled by a runtime producer.
    reference_source_db_path: str | None = field(default=None, repr=False)
    reference_skill_required: bool = False
    reference_skill_access: Any | None = field(default=None, repr=False)
    reference_helper_skill_scope: Any | None = field(default=None, repr=False)
    reference_mcp_read_receipts: list[dict[str, str]] | None = field(default=None, repr=False)

    # Bound by the active Lead runner; never supplied by tool arguments.
    document_admission_binding: Any | None = field(default=None, repr=False)
    document_turn_state: Any | None = field(default=None, repr=False)
    # Server-admitted new-presentation inputs; shared by per-call copies only.
    deck_render_binding: Any | None = field(default=None, repr=False)
    document_revision_target: DocumentRevisionTarget | None = field(default=None, repr=False)
    # ArtifactStore: bi_fetch_rows writes CSV to artifact_store and returns artifact_id,
    # letting python_sandbox load it through inputs (instead of the LLM typing it verbatim into code)
    artifact_store: Any | None = None
    # Agent evaluation only (FEATURE_AGENT_EVAL_DATARC_READ_ONLY): the attempt's read-only Datarc binding
    # (agent_eval_datarc.EvalDatarcBinding). Set only by the evaluation Datarc route on its per-call copy;
    # production contexts never carry it.
    eval_datarc: Any | None = field(default=None, repr=False)
    # CanvasStore + session_id: add_chart writes sections; when None, the handler
    # returns an error indicating gateway has not integrated it.
    canvas_store: Any | None = None
    # ScheduledJobStore: used by the scheduled_job_create tool (flag-gated). None = gateway has not integrated it.
    scheduled_store: Any | None = None
    # SessionStore: subagent previous-work memo (peer_prior_run) reads the previous spawn with the same session/profile; None = memo absent.
    session_store: Any | None = None
    session_id: str = ""
    # Terminal approval provider fence. Write-capable handlers must forward
    # this key to providers that support idempotency; None is legacy/read-only.
    effect_idempotency_key: str | None = None
    # template-refresh: the turn's VERBATIM user message (request.text), forwarded so
    # the deck-render guidance can inject the user's actual words for the restyle-vs-
    # refresh intent decision — the Lead-paraphrased `task` alone is a lossy paraphrase channel
    # (deck-refresh design §5 / review P0-3). "" when not threaded (old callers / tests).
    user_text: str = ""
    # Shared pycell counter (list[int] mutable reference shared by orchestrator + subagent). Lets
    # tool calls within subagents also emit pycell.in/out with globally unique n. None = no pycell
    # events (legacy compatibility). Orchestrator initializes and injects [0] at turn start.
    pycell_counter_ref: Any | None = None
    # router: model router for tools that act on the model's behalf (e.g. subagent_return repair).
    router: Any | None = None
    # Remember rebuild (item 2): user-scope long-term memory stores for the `remember`
    # / `recall` Lead tools. memory_notes_store = MemoryNotesStore (NL "记住" notes,
    # single-segment-per-user, backs the /memory page); user_memory_store =
    # UserMemoryStore (typed preference/constraint entries). Both forwarded from
    # orch by turn_runner. None = memory unavailable →
    # the handler returns a soft error instead of hallucinating "已记住".
    memory_notes_store: Any | None = None
    user_memory_store: Any | None = None
    # Memory redesign batch 0④ (tech-design §3.2): memory snapshot built **once** at turn start
    # (`memory_pack.MemoryPackSnapshot`). Single decision point: Lead no longer calls
    # `await build_memory_pack(...)` mid-turn; it reads this instead, as do batch 3 peers / report writer.
    # This ensures live data, persisted `memory_pack_snapshots`, and `/memory/replay` replay are **necessarily consistent**
    # (chat invariant), with one DB read per turn rather than per executor.
    # None = flag off / not injected → callers fall back to building it themselves (= today's behavior).
    memory_pack: Any | None = None
    # Space instructions read once at turn start (`space_instructions.SpaceInstructionsSnapshot`).
    # Lead and every executor inject the same `block`; None = the Space has no instructions.
    space_instructions: Any | None = None
    # ws-datarc-decouple P3 (R8): ProjectMemoryStore — bi tools write the per-turn datarc
    # project as `datarc_pref/last_used` after a successful query, so the next turn's
    # gateway resolution prefers it (project continuity). None → write skipped.
    project_memory_store: Any | None = None
    # schema_cache: workspace dim/measure TTL cache for UI label localization. None = fetch every time (slow)
    schema_cache: Any | None = None
    # clarification_store: M3b HITL request audit + SSE triggering
    clarification_store: Any | None = None
    # i18n v1: three language axes + week_start_day. None = LanguageContext.default() fallback
    # Used when tool handlers call datarc / assemble chart labels / format numbers
    language_context: Any | None = None
    # Forward #109's `TurnRequest.user_profile` (answer_style / display_name / language).
    # ⚠ Before 2026-09-16 this dict stopped at TurnRequest (no consumers in the repository; the language axis had moved to
    # the `user_profile_language` parameter): "答案风格" saved and displayed correctly in the UI, but did not change a single word of the answer.
    # Attach it to ctx so `answer_style.resolve_answer_style` can read it at prompt assembly.
    user_profile: dict[str, str] | None = None
    # Chat model picker preference. Router applies this only to lead/composer.
    preferred_provider: str | None = None
    preferred_model: str | None = None
    # M158 paradigm v2: role-based tool whitelist enforcement
    # None = no enforcement (backward compatible). "lead" / "analyst" / "composer" enforce.
    role: str | None = None
    # V5 PRD-E2: gate inline tool result preview emission. False = no preview_payload
    # emission (legacy backward compatibility). Injected by gateway main.py.
    inline_preview_enabled: bool = False
    # V5 PRD-E2: canvas section anchor for inline preview "查看完整版 →" link.
    # Set by canvas_composer when emitting a section; read by emit_tool_result after tool dispatch.
    current_section_id: str | None = None
    canvas_id: str | None = None  # Same session_id-derived canvas, set by canvas_composer
    # V7 KB sprint Day 16: kb_retriever for kb_search tool. None = KB unconfigured (handler returns soft empty)
    kb_retriever: Any | None = None
    # V7 KB: team_id for ScopeContext (was implicit via workspace_id, now explicit for KB scope filter)
    team_id: str = ""
    # workspace-permissions M0/M1: server-derived access snapshot. Kept as a
    # plain dict so copied/subagent contexts and serialized resume_context stay simple.
    access_context: dict[str, Any] | None = None
    # Store handle for resource grant runtime checks. None keeps legacy tests and
    # non-gateway callers behavior-identical.
    access_control_store: Any | None = None
    # V7 kb-agentic-fix: user RBAC role for KB ScopeContext (was hardcoded "user").
    # gateway/routes/kb.py _resolve_role(user) computes via the BOWHEAD_AUTO_ADMIN_DOMAINS env;
    # switch to the JWT roles claim when real auth ships. "user" / "ws_admin" / "team_admin".
    kb_role: str = "user"
    # URL provenance gate (url_provenance.py): hosts seen this turn. Must be a formal field, not a dynamic
    # setattr attribute: the plan-step path creates peer ctx with dataclasses.replace, which drops dynamic attributes and makes the gate fail open.
    # replace / copy.copy carry the same set object, shared by Lead and all peers. None = not seeded this turn (gate off).
    _seen_url_hosts: set[str] | None = None
    # cca sprint block ③: per-turn team tool VIEW. None = use the runner's shared
    # base registry (existing behavior — every current turn). When set (by
    # turn_runner once team-scoped tools land in P4/P5), it is a `clone_with_overlay`
    # snapshot = native ∪ this-team's overlay tools; subagent dispatch resolves
    # `ctx.tool_view or self.tool_registry` so team `teammcp.*` tools are reachable
    # WITHOUT mutating the process-shared singleton (cross-tenant isolation).
    tool_view: ToolRegistry | None = None
    # team-tools grouped by source (2026-07-22): {advertised tool name → source display name ("GitHub
    # API")}, set with tool_view by capability_resolver at the load point (the only place that
    # holds both by_id and entries). Attach to ctx rather than view: _widen_spawn_enum and other
    # clones replace view, while ctx is stable. Lead/planner use it for group headings in <team_tools>. None = no labels.
    team_tool_source_labels: dict[str, str] | None = None
    # kb-knowledge-tables sprint v1.0: doc_type + extracted_record stores for
    # kb_list_tables / kb_query_records / kb_get_record tools. None = KB tables unavailable.
    kb_doc_type_store: Any | None = None
    kb_extracted_record_store: Any | None = None
    # kb-per-row-extract Sprint 25: row-level store for cardinality=many doc_types.
    # When set, kb_query_records dispatches by doc_type.cardinality to either
    # extracted_record_store (one) or extracted_rows_store (many). None → fallback
    # to extracted_record_store only (Sprint 1+2 behavior).
    kb_extracted_rows_store: Any | None = None
    # 0.40.4 Bug 8 step a: kb_files_store wire so _kb_search can resolve the
    # user's personal-scope ws collections (datarc ws ≠ Bowhead KB scope).
    # Without this, kb_search misses personal chunks uploaded in a different
    # active workspace. Defaults None → fallback to ctx.workspace_id only.
    kb_files_store: Any | None = None
    kb_file_authorizer: Any | None = None
    kb_source_uses: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Sprint ω Phase 5 P0-1 fix (2026-05-12): metrics_log handle for emitting
    # response_kind_scanner_decision + future ω observability events. Read by
    # lead_orchestrator stream callback. None = telemetry skipped (degrade).
    metrics_log: Any | None = None
    # Sprint ar2 (2026-05-16): tenant for embed schema collection naming
    # (`v1_{tenant}_p{workspace_id}` per bowhead_memory.schema_collection helper；
    # Dimension-value recall uses datarc /keywords/recall/, without constructing collection names or depending on tenant).
    # Deployment-level **explicit pin** (BOWHEAD_VECTOR_DB_TENANT); empty default = no pin, letting
    # ``EmbedProxyClientFactory`` determine it through live connection checks. The default was once "demo", which
    # produced nonexistent collections and silently failed throughout the pipeline on private deployments (measured 2026-08-17). Thus
    # no nonempty default is allowed: failure to determine it must degrade with an unavailable error, not construct names from a plausible-looking value.
    tenant: str = ""
    # Sprint ar2: per-workspace dist threshold for embed pattern detector.
    # {"exact": 0.10, "abstract": 0.25} or None (env default). populated by
    # gateway lifespan from ProjectMemoryStore kind="ws_threshold" (lazy load,
    # ar2 Phase F.0 calibration script writes; gateway reads on startup).
    ar2_thresholds: dict[str, float] | None = None
    # workspace-datarc-decouple P1 (R1.2/R1.5): the datarc project THIS turn's BI
    # tools query. None → resolve_datarc_project() falls back to workspace_id (==
    # pre-decouple behavior, before migration seeds ws→default_project). Post-decouple
    # workspace_id is a Bowhead ws_*; this carries the seeded/runtime-selected datarc
    # project. MUST be populated by a real loader (turn_runner ctx-construction reading
    # ProjectMemoryStore datarc_pref) — do NOT leave it field-only like ar2_thresholds,
    # whose "gateway lifespan loads from ProjectMemoryStore" comment was never wired.
    active_datarc_project: str | None = None
    # Batch 2 (workspace-space-split PRD §3.3b/§3.11): in per-user mode, inject the selected workspace
    # display name + accessible-set snapshot ({"id","name"} dict tuple) with the turn, for sentinel-guided
    # option lists / provenance labels. Legacy mode always keeps defaults.
    active_datarc_workspace_name: str = ""
    accessible_datarc_workspaces: tuple = ()
    # bi-absent-turn R3.1: reason enum for BI absence this turn ("" | "no_connection" |
    # "no_identity" | "zero_workspaces" | "transient"), produced by gateway's
    # connector_context_service and forwarded through TurnRequest. "" = unknown / not applicable
    # (e.g. headless channel lacks user_id): wording falls back to neutral text; never direct users to ask an admin to enable access.
    datarc_absence_reason: str = ""
    # credential-readiness R6: optional capability tool names unavailable this turn because the team has no configuration
    # (no active model/key), e.g. {"web_search", "image_generate"}. When building ctx, turn_runner resolves these
    # through orch's team_search/media_resolver (team configuration changes take effect next turn);
    # consumers: advertised_tool_schemas removes advertisements (LLM cannot see them), _gather_lead_peers removes
    # marketing-analyst (roster gate). Empty default = byte-identical behavior at every existing construction site.
    unconfigured_capability_tools: frozenset = frozenset()
    # Best-effort continuity store: BI can record the Datarc project used by this workspace.
    # Approval gate for write tools. None keeps legacy tests and non-gateway callers unchanged.
    approval_gate: Any | None = None
    # Conversational Subagent authoring handles. The Gateway binds the
    # owner/team-scoped service only after the team model and entitlements
    # resolve; non-Web and legacy contexts keep the safe None/False defaults.
    subagent_draft_service: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    subagent_authoring_context_store: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    subagent_draft_mcp_entitled: bool = False
    subagent_draft_editor: Any | None = field(default=None, repr=False, compare=False)
    agent_coach_resources: Any | None = field(default=None, repr=False, compare=False)
    # Configuration copilot batch 0.5 (tech-design-batch05-chat.md §5.2): config_propose channel gate, forwarded from the
    # same-named TurnRequest field (True for flag-enabled web construction; False for Feishu/scheduled, etc. → structured propose rejection).
    config_lite_supported: bool = False
    configuration_settings_target: Any | None = field(default=None, repr=False, compare=False)
    coach_canvas_target: Any | None = field(default=None, repr=False, compare=False)
    # Batch 1/c12: deduplicate resolve_datarc_project invalid-state warnings once per ctx (internal state; do not inject).
    _warned_non_canonical: bool = field(default=False, repr=False, compare=False)
    # Batch 2/c6 (PRD §3.4): gateway-injected switch-commit callback,
    # async (workspace_id, workspace_name) -> member_uid. Closure encapsulates member_resolver
    # + identity + session-level preference writes + audit (no new gateway dependency in core). None = this channel
    # does not support switching (scheduled/IM, etc.); handler rejects it structurally.
    datarc_context_switcher: Any | None = field(default=None, repr=False, compare=False)
    # Batch 2/c6: same-turn idempotency for ambiguous-switch clarification (prevent injection-driven card spam, review P2).
    _switch_clar_emitted: bool = field(default=False, repr=False, compare=False)
    # ξ B2: **mutable container** for skill_read per-turn cache (read/written by the skill_bridge wrapper).
    #
    # ★★ Must be a dataclass field **present at construction**, not lazy setattr in the wrapper:
    # peer dispatch uses `dispatch_ctx = copy.copy(ctx)` (the should_swap branch in `subagent.py`),
    # and **a shallow copy aliases only attributes present at copy time**. Attributes newly created on the copy afterward
    # live only in that copy's `__dict__`; parent ctx never sees them. The original wrapper lazily created its dict, so
    # every subagent tool dispatch rebuilt an empty dict on a **fresh copy** ⇒ hit rate always 0. Lead happened to work because
    # `running_profile == "lead"` avoids copying, while skill_read is primarily designed for
    # analyst peers (skill_access.py's _LEAD_METHOD_SUMMARY: Lead does not call it directly).
    # Same lesson as `BIOutageLatch` (above) and the f6-followup transplant whitelist, but do not add it to that
    # whitelist: construction-time fields are naturally aliased by shallow copy, requiring no transplant.
    #
    # Lifetime = per turn: the real `turn_runner._peer_ctx` path creates it each turn, as both Lead's
    # tool ctx and the parent_ctx for all subagent dispatch ⇒ this dict is scoped to one turn.
    # Sharing one copy among Lead and all subagents was decided at ξ B2 initiation (prd-r1 open question 3: per-turn sharing).
    #
    # ⚠ **Constraint**: cache key is `name|ref|args` (assembled by skill_bridge), **without role**. Safe today:
    # skill_read resolution depends only on user_id / team_id / stores / granted IDs (no role matches anywhere in bowhead_skill,
    # measured in 2026-08-28 review R0), all inherited from this ctx within the turn. **If role-dependent skill
    # visibility is introduced, role must join the cache key**, or the first reader's role will feed content
    # to a role that should not see it.
    # ⚠ Overwrite saves within the turn must invalidate the entire cache (skill_save_tool success path clear(), review R1).
    _ξ_skill_cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # Future additions: checkpoint_store, etc.

    async def ensure_reference_skill_context(self) -> None:
        from bowhead_core.skill_reference_context import refresh_reference_skill_context

        await refresh_reference_skill_context(self)

    @property
    def identity(self) -> TurnIdentity:
        """#104 v1: turn 身份五元组便捷 getter（每次 new 一个 frozen 实例）."""
        return TurnIdentity(
            workspace_id=self.workspace_id,
            member_uid=self.member_uid,
            user_id=self.user_id,
            turn_id=self.turn_id,
            session_id=self.session_id,
        )

    def resolve_datarc_project(self) -> str:
        """workspace-datarc-decouple (R1.2 + S1): the datarc project id for THIS turn's BI
        calls. Prefers the runtime-selected/seeded ``active_datarc_project``. Single chokepoint
        so the bi tool handlers (``_ws``) and the non-tool datarc touchpoints
        (UI label localization / suggestions) never diverge. NEVER LLM-supplied —
        project selection is a constrained server-side step (PRD R5.2 / R7).

        Rule (批1/c3, workspace-space-split): with no seeded/selected datarc workspace this
        resolves to ``""`` (→ the bi_* gate reports "no datarc workspace" instead of querying
        a bogus id). ``ctx.workspace_id`` (the Bowhead space id) NEVER stands in — the
        pre-canonical escape hatch (a legacy NUMERIC workspace_id == its own datarc workspace)
        was retired after the 2026-07-07 prod scan (116) found zero non-``ws_*`` workspace ids
        in any table; a non-canonical id reaching here is a caller bug and is logged."""
        if self.active_datarc_project:
            return self.active_datarc_project
        if self.workspace_id and not self.workspace_id.startswith("ws_"):
            # Batch 1/c12 (implementation review): once-per-ctx deduplication; this method is called N times per turn
            # (bi_* gate/prefetch/term-context/preflight). Without a gate, the same actionable
            # signal becomes log noise (gateway datarc_turn_context separately emits one per turn).
            if not self._warned_non_canonical:
                object.__setattr__(self, "_warned_non_canonical", True)
                logger.warning(
                    "resolve_datarc_project.non_canonical_workspace_dropped ws=%r",
                    self.workspace_id,
                )
        return ""

    def has_datarc(self) -> bool:
        """Return whether an authorized Datarc client is bound to this turn.

        Gateway and background entry points resolve the Team connection through
        the connection registry before constructing this context.
        """
        return self.datarc is not None

    def bi_availability(self) -> str:
        """Return the shared three-state BI availability decision for this turn.

        A bound Team connection alone does not establish that the member can
        access a Datarc project. Prompt, planner and dispatch consumers must use
        this decision together so they advertise only usable BI capabilities.

        Treat at most one accessible project without an active project as
        absent. Normal selection persists a sole project automatically, but
        separately propagated access and selection fields can be incomplete.
        """
        if not self.has_datarc():
            return BI_ABSENT
        if self.resolve_datarc_project():
            return BI_AVAILABLE
        if len(self.accessible_datarc_workspaces or ()) > 1:
            return BI_NEEDS_SELECTION
        return BI_ABSENT


# bi-absent-turn R1.1: bi_availability() value constants (module-level, imported by four consumers).
BI_AVAILABLE = "available"
BI_NEEDS_SELECTION = "needs_selection"
BI_ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class TeamToolExecutionBinding:
    """Server-owned identity of one handler in a team Tool overlay.

    Display schemas and MCP annotations are untrusted metadata.  Reference
    execution uses this binding to prove that the selected operation reaches
    the handler built for the same team source instead of a same-named spec.
    """

    team_id: str
    source_id: str
    operation_id: str
    advertised_name: str


@dataclass
class ToolSpec:
    schema: ToolSchema
    handler: ToolHandler
    needs_approval: bool = False
    risk_level: str = "low"
    # M158 paradigm v2: roles allowed to call this tool. Shared by all roles by default (backward compatible).
    # Set explicitly at paradigm v2 registration (e.g. BI only analyst, spawn only lead).
    allowed_for_roles: frozenset[str] = field(default_factory=lambda: _DEFAULT_ALLOWED_ROLES)
    # Sprint omicron Phase C2 (2026-05-11): when False, tool not exposed to LLM
    # via to_schemas() but handler still callable for backward compat (alias
    # path). Used to deprecate bi_ask_question / bi_breakdown / bi_list_measures
    # / bi_list_dimensions / bi_attribute (F5) + 7 unused BI tools.
    visible_to_llm: bool = True
    # tool-domain (D1): MCP-aligned behavioural hints + the Bowhead source/world axis.
    # Built-ins are seeded by apply_tool_annotations (post-registration); team/MCP tools
    # carry their own self-declared values (build_team_specs). source ∈
    # dataset|kb|web|compute|external; annotations.open_world mirrors MCP openWorldHint.
    annotations: ToolAnnotations | None = None
    source: str | None = None
    # Durable execution trusts only this Bowhead-owned static contract. Display
    # annotations and team/MCP self-declared hints never grant dispatch.
    effect_spec: ToolEffectSpec | None = None
    # Present only on specs created by ``build_team_specs``.  This is internal
    # dispatch identity, not MCP self-declared metadata or a permission grant.
    team_tool_binding: TeamToolExecutionBinding | None = None
    personal_mcp_label: str | None = None


def _opened_to_role(name: str, role: str) -> bool:
    """Lead-only Tools opened to team Agents with the analyst role."""
    from bowhead_core.resource_disable import team_agent_opened_tools

    return role == "analyst" and name in team_agent_opened_tools()


def _effective_tool_whitelist(
    profile_whitelist: tuple[str, ...] | frozenset[str],
    view: ToolRegistry,
    team_tool_bundle: tuple[str, ...] | frozenset[str] | None = None,
) -> set[str]:
    """cca block ③ + P3: per-turn effective whitelist.

    ``team_tool_bundle is None`` (built-in/system profiles — compute/consultant, and
    every non-team turn where the overlay is empty) → static whitelist ∪ the FULL
    team overlay (``view.overlay_names``), byte-identical to the pre-P3 behavior.

    ``team_tool_bundle`` set (a P3 team subagent — ALWAYS a concrete tuple, possibly
    ``()``) → static whitelist ∪ (overlay ∩ bundle): the team tools the subagent may
    see/call are EXACTLY its declared SUBSET. ``()`` → zero team tools (a native-only
    bundle), NOT the full union — closes the "empty ⇒ all" inversion. Native tool
    names ride ``profile_whitelist`` (they are never in ``overlay_names``), so they
    are unaffected by the bundle. A stale bundle ref (team tool no longer in the
    overlay) drops out via the intersection.

    cca block ③ (unchanged): per-turn effective whitelist = static profile whitelist ∪
    the team-overlay tool names the per-turn VIEW added (``view.overlay_names``).

    Reads the view's self-recorded overlay names rather than diffing against a
    base registry, so EVERY subagent backend computes the same augmented
    whitelist from the view alone — LangGraph only ever receives the view
    (the effective registry), never the base. The overlay set is exactly this
    team's explicitly added tools, so a GLOBAL stdio ``mcp.*`` tool sitting in the
    shared base is never auto-whitelisted (cca review F2 — cross-tenant leak); a
    name-prefix scan would have leaked them. When ``ctx.tool_view`` is None the
    caller passes the base registry, whose overlay set is empty → whitelist
    unchanged, so existing turns are behavior-identical.

    Lives here (not in subagent.py) so both the legacy and langgraph backends can
    import it from this lower layer without the subagent↔subagent_graph import
    cycle.
    """
    overlay = set(view.overlay_names)
    if team_tool_bundle is not None:
        overlay &= set(team_tool_bundle)
    return set(profile_whitelist) | overlay


# Tool namespace → connector type ID (v2 concept model: SSOT docs/tool-context-framework.md).
# Keys are **connector types**: datarc is a single-active-connection special case (instance↔type 1:1);
# multi-instance connectors (teammcp.<src>.*) must use instance IDs as discriminators, outside this table (advertised names
# already carry ots_*-derived slugs; see openapi_tool_source.py).
CONNECTOR_NAMESPACES: dict[str, str] = {
    "bi_": "datarc",
}
# Default English connector names; localized interfaces own translated copy.
CONNECTOR_DISPLAY_NAMES: dict[str, str] = {
    "datarc": "Datarc",
}


def connector_for_tool(tool_name: str) -> str | None:
    """Map a tool name to its connector type id by namespace prefix, or None."""
    for prefix, connector in CONNECTOR_NAMESPACES.items():
        if tool_name.startswith(prefix):
            return connector
    return None


@dataclass(frozen=True)
class ConnectorContextBinding:
    """T2#9: 「N 选 1 上下文选择」语义的 connector 绑定描述符（sentinel 闸 +
    switch handler 表驱动参数化的数据源，tech-design §2.3）。

    只有「N 选 1 上下文选择语义」的 connector 进本表（今天 datarc 单住户）；
    pbi 无此语义（team→1 固定绑定）不进表——dispatch 闸保持裸错（连接轴，归 T2#8）。
    ``*_attr`` 值 = ``ToolExecutionContext`` 字段/方法名（信封 D2 冻结拼写，T2#8 前
    不动）；有效性由 test_connector_namespaces 的表有效性 pin 锁（防 typo 静默
    getattr None）。
    """

    connector: str  # "datarc" (in the value range of CONNECTOR_NAMESPACES)
    display_name: str  # Default English name, consistent with CONNECTOR_DISPLAY_NAMES
    context_noun: str  # Full English context name used in default note/hint text
    # Full Chinese context name for the zh-CN note/hint sentences; docs/design/概念术语.md fixes
    # the user-facing zh term, so these sentences must not embed the English noun.
    zh_context_noun: str
    zh_display_name: str  # Chinese product name for zh-CN sentences (概念术语: 北极九章)
    unavailable_label: str  # "DatarcUnavailableError" (LLM-visible error prefix; frozen bytes)
    resolve_attr: str  # "resolve_datarc_project" (ctx method name)
    accessible_attr: str  # "accessible_datarc_workspaces"
    switcher_attr: str  # "datarc_context_switcher"
    active_id_attr: str  # "active_datarc_project"
    active_name_attr: str  # "active_datarc_workspace_name"
    # bi-workspace-tour review r3#6: the tool for inspecting current context depends on the connector
    # (referenced by hint text; hardcoding bi_schema would teach the wrong tool when a second connector arrives).
    inspect_tool: str = "bi_schema"


CONNECTOR_CONTEXT_BINDINGS: dict[str, ConnectorContextBinding] = {
    "datarc": ConnectorContextBinding(
        connector="datarc",
        display_name="Datarc",
        context_noun="Datarc workspace",
        zh_context_noun="北极九章工作区",
        zh_display_name="北极九章",
        unavailable_label="DatarcUnavailableError",
        resolve_attr="resolve_datarc_project",
        accessible_attr="accessible_datarc_workspaces",
        switcher_attr="datarc_context_switcher",
        active_id_attr="active_datarc_project",
        active_name_attr="active_datarc_workspace_name",
    ),
}


# bi-workspace-tour circuit-break threshold: which consecutive committed switch is paused (allow
# PAUSE_AT-1 consecutive commits). Production/exemption paths share this constant (review r3#3: separate literal 3s drift silently;
# changing only one side renders an entire batch of intentional pauses as red failed steps).
SWITCH_STREAK_PAUSE_AT = 3


def _connector_accessible_roster(
    ctx: ToolExecutionContext,
    binding: ConnectorContextBinding,
) -> tuple[list[dict[str, Any]], list[str]]:
    """ctx 可及集合快照 → (合法条目, 显示名) 的唯一归一点。

    bi-workspace-list review c8: 这段过滤/投影此前在 switch 与 list 两个 handler
    里各抄一份——roster 形状一变就得改两处，漏一处两个工具对「什么算可及」
    静默分歧。判合法=dict 且 id 真值；显示名=name 缺省回落 id（str 归一）。
    """
    accessible = [w for w in (getattr(ctx, binding.accessible_attr, ()) or ()) if isinstance(w, dict) and w.get("id")]
    names = [str(w.get("name") or w["id"]) for w in accessible]
    return accessible, names


def _tool_requires_datarc(tool_name: str) -> bool:
    """ws-datarc-decouple P3: does this tool need a datarc BI client to run?

    The BI bridge owns the ``bi_*`` namespace (bi-api-contract) and is the only datarc
    consumer — all 14 ``bi_*`` tools call datarc, and no non-BI tool uses the prefix — so a
    prefix match is exact today. Body delegates to ``CONNECTOR_NAMESPACES`` (T1#7); the
    function name stays because tests and docs reference it directly."""
    return connector_for_tool(tool_name) == "datarc"


def _resource_grant_targets_for_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    include_argument_targets: bool = True,
) -> list[tuple[str, str, str]]:
    """Map runtime tool calls to resource grant targets when the mapping is stable."""
    return _shared_resource_grant_targets_for_tool(
        tool_name,
        arguments,
        include_argument_targets=include_argument_targets,
    )


KB_SEARCH_PARTIAL_NOTE = "注意：本次有部分知识库范围检索超时或出错，结果可能不完整；不要据此断言知识库里没有相关内容，可稍后重试检索。"


async def _filter_kb_search_chunks_by_file_grants(
    ctx: Any,
    chunks: list[Any],
    *,
    target_kind: str = "tool",
    target_id: str = "kb_search",
) -> list[Any]:
    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is None or not access_ctx:
        return chunks
    enforce = False
    try:
        from bowhead_core.access_control import resource_grants_enforce_enabled

        enforce = resource_grants_enforce_enabled()
        out: list[Any] = []
        for chunk in chunks:
            file_id = str(getattr(chunk, "file_id", "") or "")
            if not file_id:
                out.append(chunk)
                continue
            decision = await access_store.explain_decision(
                access_ctx,
                "kb_file",
                file_id,
                "read",
            )
            if decision.get("allowed"):
                out.append(chunk)
                continue
            logger.info(
                "resource_grant.kb_search_file_would_deny file=%s permission=read reason=%s enforce=%s",
                file_id,
                decision.get("reason"),
                enforce,
            )
            await access_store.record_runtime_denied(
                access_ctx,
                resource_kind="kb_file",
                resource_id=file_id,
                permission="read",
                reason=str(decision.get("reason") or "denied"),
                target_kind=target_kind,
                target_id=target_id,
                payload={
                    "chunk_id": getattr(chunk, "chunk_id", ""),
                    "scope": getattr(chunk, "scope", ""),
                    "source": "kb_search",
                    "enforce": enforce,
                },
            )
            if not enforce:
                out.append(chunk)
        return out
    except Exception as exc:
        logger.warning("resource_grant.kb_search_file_check_failed err=%s", exc)
        if enforce:
            return []
        return chunks


async def _kb_search_scope_filter_for_grants(ctx: Any) -> list[str] | None:
    """Return allowed KB search scopes for enforce mode.

    None means legacy/full search is allowed. An empty list means enforcement is
    active but no KB collection scope is readable.
    """
    access_store = getattr(ctx, "access_control_store", None)
    access_ctx = getattr(ctx, "access_context", None)
    if access_store is None or not access_ctx:
        return None
    try:
        from bowhead_core.access_control import resource_grants_enforce_enabled

        enforce = resource_grants_enforce_enabled()
        if not enforce:
            return None
        checks = [
            ("workspace", f"workspace:{getattr(ctx, 'workspace_id', '') or ''}"),
            ("team", f"team:{getattr(ctx, 'team_id', '') or ''}"),
        ]
        allowed: list[str] = []
        for scope_kind, resource_id in checks:
            if resource_id.endswith(":"):
                continue
            decision = await access_store.explain_decision(
                access_ctx,
                "kb_scope",
                resource_id,
                "read",
            )
            if decision.get("allowed"):
                if scope_kind == "workspace":
                    allowed.extend(["personal", "space"])  # Batch 3/c1 canonical (resource ID prefix remains frozen as workspace:)
                else:
                    allowed.append("team")
                continue
            await access_store.record_runtime_denied(
                access_ctx,
                resource_kind="kb_scope",
                resource_id=resource_id,
                permission="read",
                reason=str(decision.get("reason") or "denied"),
                target_kind="tool",
                target_id="kb_search",
                payload={"source": "kb_search_scope_filter", "enforce": enforce},
            )
            logger.info(
                "resource_grant.kb_search_scope_would_deny resource=%s permission=read reason=%s enforce=%s",
                resource_id,
                decision.get("reason"),
                enforce,
            )
        return allowed
    except Exception as exc:
        logger.warning("resource_grant.kb_search_scope_check_failed err=%s", exc)
        try:
            from bowhead_core.access_control import resource_grants_enforce_enabled

            if resource_grants_enforce_enabled():
                return []
        except Exception:  # noqa: S110
            pass
        return None


class ToolRegistry:
    """Tool 注册 + 执行."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        # cca block ③: names this registry added as a per-turn team overlay (via
        # clone_with_overlay). Empty on the shared base singleton. The view is
        # self-describing about what it overlaid so ANY backend can derive the
        # augmented whitelist from the view alone — no base diff needed (legacy,
        # LangGraph only receives the view, not the base).
        self._overlay_names: frozenset[str] = frozenset()

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.schema.name] = spec

    def clone_with_overlay(self, extra_specs: list[ToolSpec]) -> ToolRegistry:
        """cca sprint block ③: return a per-turn VIEW = this registry's specs
        overlaid with ``extra_specs`` (team-scoped `teammcp.*` tools).

        Shallow-copies the name→spec dict so the returned view can be mutated /
        dispatched against WITHOUT touching the process-shared base singleton —
        an team's overlay tools must never leak into the global registry, and an
        overlay spec that shadows a native name overrides ONLY inside the view.
        ToolSpec objects are shared by reference (dispatch never mutates a spec),
        which is safe. The overlaid names are recorded on ``view.overlay_names``
        (union'd with any inherited overlay) so the whitelist augmentation can be
        derived from the view alone. Callers stash the result on ``ctx.tool_view``.
        """
        view = ToolRegistry()
        view._specs = dict(self._specs)
        for spec in extra_specs:
            view._specs[spec.schema.name] = spec
        view._overlay_names = self._overlay_names | frozenset(s.schema.name for s in extra_specs)
        return view

    def clone_without(self, names: frozenset[str]) -> ToolRegistry:
        """Return a per-turn VIEW without ``names`` (deployment-switched Tools that are off).

        Like ``clone_filtered_by_resource_grants``, a name absent from the view is never advertised
        (``to_schemas`` skips it) and can't be dispatched through the view; the base is untouched.
        """
        if not names & set(self._specs):
            return self
        view = ToolRegistry()
        view._specs = {name: spec for name, spec in self._specs.items() if name not in names}
        view._overlay_names = self._overlay_names - names
        return view

    def clone_with_spec_override(self, spec: ToolSpec) -> ToolRegistry:
        """cca P3 (S3.4): return a per-turn VIEW identical to this one but with ``spec``
        replacing the same-named entry — WITHOUT recording the name in ``overlay_names``.

        The sibling of ``clone_with_overlay`` for an in-place rewrite of a NATIVE tool's
        schema (e.g. widening ``subagent_spawn``'s ``profile`` enum per turn). Unlike
        ``clone_with_overlay``, the overridden name MUST NOT enter ``overlay_names``:
        that set feeds ``_effective_tool_whitelist`` (overlay-union for bundle=None
        profiles), so a native control tool like ``subagent_spawn`` leaking there would
        be ADVERTISED to built-in peers whose role can't call it (lead-only) → the LLM
        emits it → ``execute()`` role-denies → a wasted iteration + a visible error tile
        (the exact UX the lead-only advertise filter exists to prevent). Team-overlay
        ``teammcp.*`` tools use ``clone_with_overlay``; this is its sibling for native
        per-turn spec rewrites. Shallow-copies ``_specs`` (base singleton untouched).
        """
        view = ToolRegistry()
        view._specs = dict(self._specs)
        view._specs[spec.schema.name] = spec
        view._overlay_names = self._overlay_names  # UNCHANGED — not an overlay add
        return view

    async def clone_filtered_by_resource_grants(
        self,
        ctx: ToolExecutionContext,
    ) -> ToolRegistry:
        """Return a per-turn view with denied tool schemas removed.

        This is the visibility companion to ``execute``'s fail-closed check. It
        only hides in hard-enforce mode; shadow mode keeps schemas visible and
        relies on execution logs to preview future denials.
        """
        access_store = getattr(ctx, "access_control_store", None)
        access_ctx = getattr(ctx, "access_context", None)
        if access_store is None:
            return self

        def _empty_view() -> ToolRegistry:
            view = ToolRegistry()
            view._specs = {}
            view._overlay_names = frozenset()
            return view

        try:
            from bowhead_core.access_control import resource_grants_enforce_enabled

            if not resource_grants_enforce_enabled():
                return self
            if not access_ctx:
                logger.warning("resource_grant.tool_schema_filter_missing_access_context")
                return _empty_view()
            from contextlib import aclosing

            from bowhead_core.grant_evaluation import iter_grant_verdicts

            denied_names: set[str] = set()
            for name in list(self._specs):
                grant_targets = _resource_grant_targets_for_tool(
                    name,
                    {},
                    include_argument_targets=False,
                )
                async with aclosing(iter_grant_verdicts(ctx, access_store, access_ctx, grant_targets)) as verdicts:
                    async for verdict in verdicts:
                        if not verdict.allowed:
                            denied_names.add(name)
                            logger.info(
                                "resource_grant.tool_schema_hidden tool=%s resource=%s:%s permission=%s reason=%s",
                                name,
                                verdict.resource_kind,
                                verdict.resource_id,
                                verdict.permission,
                                verdict.reason,
                            )
                            break
            if not denied_names:
                return self
            view = ToolRegistry()
            view._specs = {name: spec for name, spec in self._specs.items() if name not in denied_names}
            view._overlay_names = self._overlay_names - frozenset(denied_names)
            return view
        except Exception as exc:
            logger.warning("resource_grant.tool_schema_filter_failed err=%s", exc)
            return _empty_view()

    @property
    def overlay_names(self) -> frozenset[str]:
        """cca block ③: team-overlay tool names this per-turn view added on top of
        the shared base registry (set by clone_with_overlay). Empty on the base
        singleton, so a base registry yields no whitelist augmentation."""
        return self._overlay_names

    def get(self, name: str) -> ToolSpec:
        if name not in self._specs:
            raise KeyError(f"Tool '{name}' not registered")
        return self._specs[name]

    def has(self, name: str) -> bool:
        return name in self._specs

    def set_allowed_roles(self, name: str, roles: frozenset[str]) -> None:
        """M158 paradigm v2: 给已注册 tool 批量打 role binding.
        没注册的 tool 静默 skip (允许 paradigm v2 在 tool subset 上启用).
        """
        if name in self._specs:
            self._specs[name].allowed_for_roles = roles

    def list_names(self) -> list[str]:
        return list(self._specs.keys())

    def to_schemas(self, whitelist: list[str] | None = None) -> list[ToolSchema]:
        """导出 LLM 可见的 tool schema 列表（供 PromptBuilder / Router 使用）.

        Sprint omicron Phase C2: filter on visible_to_llm field — hidden tools
        (deprecated aliases) excluded even if in whitelist.
        """
        names = whitelist or list(self._specs)
        return [self._specs[n].schema for n in names if n in self._specs and self._specs[n].visible_to_llm]

    async def admit(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ToolExecutionContext,
        *,
        call_id: str,
        bus: Any | None = None,
    ) -> ToolInvocation | None:
        """Run the ``BEFORE_PRESENTATION`` policies; a returned invocation stops the call before its step card.

        Policies see the call exactly as the model sent it, as the wrapper checks always did:
        ``skill_operation_denial`` reads poisoned ``raw_arguments`` itself, and the approval
        request key is derived from the raw arguments.
        """
        call = ToolCallRequest(name, arguments or {}, self._specs.get(name), registry=self, call_id=call_id, bus=bus)
        for policy in TOOL_CALL_POLICIES:
            if policy.stage is not PolicyStage.BEFORE_PRESENTATION:
                continue
            if (outcome := await policy.check(call, ctx)) is None:
                continue
            if isinstance(outcome, ToolInvocation):
                # A gate's own invocation (approval) carries the model call id, except for
                # subagent_spawn, whose invocation identity belongs to the spawned run.
                if name != "subagent_spawn" and call_id:
                    outcome.call_id = call_id
                return outcome
            if (on_denial := getattr(policy, "on_denial", None)) is not None:
                on_denial(name, arguments or {}, outcome, ctx)
            return ToolInvocation(
                tool_name=name,
                arguments=arguments or {},
                result=outcome.result,
                error=outcome.error,
                call_id=call_id,
                started_at=utc_now_iso(),
                ended_at=utc_now_iso(),
            )
        return None

    async def _run_policies(
        self,
        stage: PolicyStage,
        call: ToolCallRequest,
        ctx: ToolExecutionContext,
        *,
        before_spec_lookup: bool,
    ) -> PolicyDenial | None:
        """Run ``stage`` policies in order; the first denial stops the chain."""
        for policy in TOOL_CALL_POLICIES:
            if policy.stage is stage and policy.before_spec_lookup is before_spec_lookup:
                if (denial := await policy.check(call, ctx)) is not None:
                    return denial
        return None

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
        *,
        propagate_ambiguous_transport: bool = False,
    ) -> ToolInvocation:
        """执行 tool call，统一异常处理.

        Approval effects opt into propagating transport failures because a
        timeout/connection loss after dispatch cannot prove NOT_APPLIED.
        Read-only and legacy callers retain the existing error projection.
        """
        import asyncio
        import copy
        import time

        # Sprint omicron r2 (2026-05-11): DeepSeek + Doubao anthropic-compat
        # uses sanitize_tool_dots=True to send `bi_query` as `bi__query` on the wire
        # (a safe namespace). After seeing underscores in cached prompts / training data, the LLM
        # repeats `bi__query` when emitting tool_call. Backend finds no matching
        # tool → errors such as "skill '' not found" → LLM rationalizes this as "工具白名单问题"
        # hallucination. Measured dogfood 0.10.13 Turn 4 had this shape.
        #
        # Fix: detect `<prefix>__<rest>` and automatically rewrite to `<prefix>.<rest>`
        # for the tool registered by the real spec. Fallback only (prefer exact matches).
        # ─── C5 closure: normalize renamed tools on the read path (fallback) ─────────────
        # Normal paths already normalize in C1 (first line receiving tool_calls in the agent loop); this protects
        # **callers bypassing C1**: backend self-calls (auto-chart directly `execute("add_chart")`),
        # future loops, and tests. Normalization accepts only `renamed_from` (pure renames),
        # **not** `legacy_aliases`: old names have their own handlers, so normalization would change the dispatch target.
        # Specification: docs/design/工具改名迁移.md.
        _canon = canonical_tool_name(name)
        if _canon != name:
            logger.info("tool_registry.renamed_tool_name %s → %s", name, _canon)
            name = _canon

        if name not in self._specs and "__" in name:
            _restored = name.replace("__", ".", 1)
            if _restored in self._specs:
                logger.info(
                    "tool_registry.underscore_alias %s → %s (sanitize_tool_dots wire round-trip)",
                    name,
                    _restored,
                )
                name = _restored

        if denial := await self._run_policies(PolicyStage.BEFORE_DISPATCH, ToolCallRequest(name, arguments, None), ctx, before_spec_lookup=True):
            return ToolInvocation(
                tool_name=name,
                arguments=arguments,
                result=denial.result,
                error=denial.error,
                started_at=utc_now_iso(),
                ended_at=utc_now_iso(),
                duration_ms=0,
            )

        # Some OpenAI-compatible / Anthropic-compatible models (Qwen / Doubao, etc.) put
        # structured arguments in `{"raw_arguments": "<JSON string>"}` rather than an expanded dict;
        # Doubao-Seed-2.0-pro also contaminates JSON with tool-call XML when nested JSON and multiline strings coexist
        # (e.g. `"input" type="object">\n<property name="code">...</property>`),
        # while Volces Ark /api/coding leaks `<text_never_used_<hex>>` prompt-cache markers.
        # _unwrap_tool_args tries multiple repairs to restore this dirty data to a usable dict; on failure it puts the specific
        # error message in arguments so the handler returns an explicit error, rather than an empty dict causing
        # misdiagnosis such as "skill '' not found".
        # Identify contamination with one key + string value that looks like JSON.
        # Measured key names: `raw_arguments` (Qwen) / `_raw` (Doubao) / `arguments` (some proxies).
        # **Whitelist and shape must both match**: a known poison key + JSON-like value. Shape alone is insufficient,
        # or valid `{"payload": "<JSON string>"}` (double encoding) would incorrectly lose its outer dict.
        _POISON_KEYS = {"raw_arguments", "_raw", "arguments"}
        _poison_key: str | None = None
        if isinstance(arguments, dict) and len(arguments) == 1:
            only_k = next(iter(arguments))
            only_v = arguments[only_k]
            if only_k in _POISON_KEYS and isinstance(only_v, str) and len(only_v) > 20 and only_v.lstrip().startswith(("{", "<")):
                _poison_key = only_k
        if _poison_key is not None:
            raw = arguments[_poison_key]
            unwrapped, unwrap_err = _unwrap_tool_args(raw)
            if unwrapped is not None:
                arguments = unwrapped
            else:
                if reference_skill_active(ctx):
                    logger.warning("tool_registry.reference_arguments_unavailable")
                    unwrap_err = "reference_skill_arguments_unavailable"
                else:
                    logger.warning(
                        "tool_registry.raw_arguments_unwrap_failed tool=%s err=%s preview=%s",
                        name,
                        unwrap_err,
                        raw[:300],
                    )
                # Give the handler an explicit error dict so its input validation returns "specific error: args
                # malformed", instead of silently running with empty ''. The LLM then knows to resend args.
                arguments = {
                    "_args_parse_error": unwrap_err or "raw_arguments unparseable",
                }

        # bi-workspace-tour circuit-break breadcrumbs (review r3 refactor): this chokepoint only handles
        # other-tool execution → reset and container initialization; counting happens at the switch handler's **commit point**
        # (count commits, not attempts: typo misses / ambiguity clarification / channels without switchers use no quota).
        # Container must be mutable: peer handlers receive copy.copy(ctx) shallow copies,
        # so scalar writes disappear entirely (same lesson as BIOutageLatch docstring, for the fourth time).
        # Use generated tool-name constants (review r3#6: bare strings silently kill the breaker after canonical renames).
        from bowhead_core.agents._generated.tool_names import TOOL_SWITCH_CONTEXT as _switch_tool_name

        _streak_box = getattr(ctx, "_switch_streak_box", None)
        if name == _switch_tool_name:
            if _streak_box is None:
                object.__setattr__(ctx, "_switch_streak_box", {"n": 0})
        elif _streak_box is not None and _streak_box.get("n"):
            _streak_box["n"] = 0

        spec = self.get(name)
        invocation = ToolInvocation(
            tool_name=name,
            arguments=arguments,
            started_at=utc_now_iso(),
            # tool-domain (D1): stamp the resolved spec's source/world (fast-path cache;
            # classify_turn_source falls back to lookup_domain by name when None). `self`
            # is the per-turn view here (dispatcher calls effective.execute), so teammcp.*
            # overlay specs self-classify.
            source=spec.source,
            open_world=(spec.annotations.open_world if spec.annotations else None),
        )
        if denial := await self._run_policies(PolicyStage.BEFORE_DISPATCH, ToolCallRequest(name, arguments, spec), ctx, before_spec_lookup=False):
            invocation.result = denial.result
            invocation.error = denial.error
            invocation.ended_at = utc_now_iso()
            invocation.duration_ms = 0
            return invocation
        from bowhead_router.errors import InvocationOutcomeUnknownError
        from bowhead_router.invocation import (
            abandon_physical_invocation,
            fail_physical_invocation,
        )

        from bowhead_core.events import TerminalDurabilityAbort
        from bowhead_core.terminal_llm_invocation import (
            begin_durable_tool_effect,
            complete_durable_tool_effect,
            durable_tool_provider_token,
        )

        tool_attempt = await begin_durable_tool_effect(
            tool_name=name,
            arguments=arguments,
            effect_spec=spec.effect_spec,
            approval_bound=spec.needs_approval,
        )
        handler_ctx = ctx
        if tool_attempt is not None:
            handler_ctx = copy.copy(ctx)
            handler_ctx.effect_idempotency_key = durable_tool_provider_token(
                tool_attempt,
                spec.effect_spec,
            )

        t0 = time.perf_counter()
        handler_completed = False
        try:
            result = await spec.handler(arguments, handler_ctx)
            handler_completed = True
            durable_classified = tool_attempt is not None and isinstance(
                result,
                ClassifiedToolEffectResult,
            )
            result = await complete_durable_tool_effect(
                tool_attempt,
                result=result,
            )
            if (
                name == "skill_read"
                and isinstance(result, dict)
                and result.get("ok") is False
                and result.get("error_kind") == "reference_skill_unavailable"
            ):
                # Revocation can occur after the registry gate and before the
                # raw read. This is a denied read, never an unknown write effect.
                invocation.result = result
                invocation.error = "ToolPermissionError: reference_skill_unavailable"
            elif propagate_ambiguous_transport and not durable_classified and isinstance(result, dict) and result.get("ok") is False:
                error_kind = str(result.get("error_kind") or "unclassified")
                if error_kind not in (_DETERMINISTIC_NOT_APPLIED_ERROR_KINDS):
                    raise AmbiguousToolEffectOutcomeError(error_kind)
                invocation.error = scrub(str(result.get("error") or error_kind))
            else:
                invocation.result = result
        except InvocationOutcomeUnknownError:
            if not handler_completed:
                await fail_physical_invocation(
                    tool_attempt,
                    reason_code="tool_handler_reported_unknown",
                )
            raise
        except asyncio.CancelledError:
            await abandon_physical_invocation(
                tool_attempt,
                reason_code="tool_handler_cancelled",
            )
            raise
        except TerminalDurabilityAbort:
            await fail_physical_invocation(
                tool_attempt,
                reason_code="tool_handler_durability_abort",
            )
            raise
        except AmbiguousToolEffectOutcomeError:
            await fail_physical_invocation(
                tool_attempt,
                reason_code="tool_handler_outcome_ambiguous",
            )
            raise
        except (BIConnectionError, TimeoutError, ConnectionError) as e:
            await fail_physical_invocation(
                tool_attempt,
                reason_code="tool_handler_transport_error",
            )
            if propagate_ambiguous_transport:
                raise
            invocation.error = scrub(f"{type(e).__name__}: {e}")
            logger.warning(
                "tool_registry.handler_transport_failed tool=%s err=%s",
                name,
                invocation.error,
            )
        except DatarcError as e:
            await fail_physical_invocation(
                tool_attempt,
                reason_code="tool_handler_provider_error",
            )
            if propagate_ambiguous_transport:
                raise AmbiguousToolEffectOutcomeError(f"{type(e).__name__}: {e}") from e
            # ④f chokepoint: an team-created/MCP/native tool's exception text funnels here
            # → invocation.error → the LLM; scrub credential shapes (belt-and-suspenders on
            # top of the engine's own scrub, which only covers its expected-failure dicts).
            invocation.error = scrub(f"{type(e).__name__}: {e}")
            logger.warning(
                "tool_registry.handler_failed tool=%s err=%s",
                name,
                invocation.error,
            )
        except Exception as e:
            await fail_physical_invocation(
                tool_attempt,
                reason_code="tool_handler_unclassified_error",
            )
            if propagate_ambiguous_transport:
                raise AmbiguousToolEffectOutcomeError(f"{type(e).__name__}: {e}") from e
            invocation.error = scrub(f"unexpected_error: {type(e).__name__}: {e}")
            logger.exception(
                "tool_registry.handler_unexpected_error tool=%s args_preview=%s",
                name,
                str(arguments)[:300],
            )
        finally:
            invocation.ended_at = utc_now_iso()
            invocation.duration_ms = int((time.perf_counter() - t0) * 1000)
            # subagent_spawn handler temporarily writes its internal ToolInvocation list to
            # ctx._last_nested_invocations; move it to invocation.nested here and clear the temporary ctx field.
            # Benefit: compose_answer can see actual bi.* data called inside subagents after flattening.
            from bowhead_core.agents._lead_context import take_nested_invocations

            nested = take_nested_invocations(ctx) if ctx is not None else None
            if nested:
                invocation.nested = list(nested)
                with contextlib.suppress(AttributeError):
                    if getattr(ctx, "_last_nested_invocations", None) is nested:
                        delattr(ctx, "_last_nested_invocations")
        return invocation


# ─────────────────────────────────────────────────────────────
# Default registration: 13 `bi.*` tools (M1 Day 1 first implements the four core tools)
# ─────────────────────────────────────────────────────────────
def build_default_registry() -> ToolRegistry:
    """返回带默认 datarc 工具的 Registry.

    M1 Day 1 只注册垂直切片必需的 4 个：
        - bi_list_measures
        - bi_list_dimensions
        - bi_ask_question
        - bi_attribute (F5: hidden from LLM but handler kept)

    其余 9 个（card / cards / explain / LLM async / fetch_account / 写操作）
    在 Sprint 1 Day 2-3 陆续加入。
    """
    reg = ToolRegistry()
    from bowhead_core.attachment_read import register_attachment_read

    register_attachment_read(reg)

    # Identity fields (workspace_id / member_uid) always come from ctx, never the LLM;
    # even if LLM args contain them, ignore them to avoid hallucinated arguments causing 400s
    def _ws(ctx: ToolExecutionContext) -> str:
        # workspace-datarc-decouple (R1.2): resolve via the single chokepoint — the
        # turn's active/seeded datarc project, else fall back to workspace_id (==
        # pre-decouple behavior). Still never from LLM args (ignored on args route).
        return ctx.resolve_datarc_project()

    def _member(ctx: ToolExecutionContext) -> str:
        return ctx.member_uid

    # ─── bi_list_measures ────────────────────────────────────
    async def _list_measures(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        from bowhead_core.datarc_permissions import filter_schema

        items = await ctx.datarc.list_measures(_ws(ctx))
        items, _, _, _ = await filter_schema(ctx.datarc, _ws(ctx), _member(ctx), list(items), [])
        _loc = _out_lang(ctx)
        return [_measure_public(m, _loc) for m in items]

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="bi_list_measures",
                description=(
                    "列出当前 workspace 的所有指标定义（含主关键词与别名）。"
                    "Agent 应在提问前调用一次，取指标的 names[0] 作为 NL 提问的首选关键词。"
                    "无需参数；workspace 由系统自动注入。"
                    # This tool returns a **bare list**, with nowhere to attach conditional explanations (`bi_schema` returns a dict
                    # and emits them conditionally in its payload). Put them in this description, using the same constant.
                    f" {_PERCENTAGE_FORMAT_NOTE}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                },
            ),
            handler=_list_measures,
        )
    )

    # ─── bi_list_dimensions ──────────────────────────────────
    async def _list_dimensions(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        from bowhead_core.datarc_permissions import filter_schema

        items = await ctx.datarc.list_dimensions(_ws(ctx))
        _, items, _, _ = await filter_schema(ctx.datarc, _ws(ctx), _member(ctx), [], list(items))
        return [
            {
                "names": d.names,
                "type": d.type,
                "isKeyword": d.is_keyword,
                "used": d.used,
            }
            for d in items
        ]

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="bi_list_dimensions",
                description=("列出当前 workspace 的维度定义。用于构造 NL 提问时的维度关键词。无需参数；workspace 由系统自动注入。"),
                input_schema={
                    "type": "object",
                    "properties": {},
                },
            ),
            handler=_list_dimensions,
        )
    )

    # ─── bi_schema ────────────────────────────────────────────
    # Bowhead-side aggregation: concurrently call datarc list_measures + list_dimensions
    # instead of two separate LLM calls, saving one round trip.
    # Unified entry point for G4.5 "Step 1 schema probe".
    async def _schema(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        import asyncio as _aio_schema

        # Fetch both measures and dimensions; omitting one creates a misleading empty
        # schema rather than a smaller view of the same data. Legacy kind arguments are
        # accepted and ignored because tool arguments do not reject unknown fields here.
        # Log their use through the existing compatibility path.
        if args.get("kind"):
            logger.info("bi_schema.kind_deprecated value=%s", args.get("kind"))
        ws = _ws(ctx)
        measures, dims = await _aio_schema.gather(
            ctx.datarc.list_measures(ws),
            ctx.datarc.list_dimensions(ws),
        )
        # The UI localizes step-card field labels from this cache; fill it from the fetch the agent
        # already asked for instead of a separate Datarc call.
        from bowhead_core.datarc_schema_cache import remember_schema

        remember_schema(
            getattr(ctx, "schema_cache", None),
            ws,
            workspace_name=getattr(ctx, "active_datarc_workspace_name", "") or "",
            measures=list(measures or []),
            dimensions=list(dims or []),
        )

        # D4: datarc /dimensions/ can return HTTP-200 with empty `data` on a transient
        # hiccup (datarc_client coalesces None/[] → []). measures-OK + dims-empty is a
        # partial-failure fingerprint (measures-on-data but zero declared dimensions is
        # implausible). Retry the dims fetch a couple times before accepting empty; if
        # still empty on the asymmetric case, surface a soft warning so the LLM does NOT
        # assert "本 workspace 不支持按 X 拆分" off a flake. Key on the RAW `dims` (the
        # used-filter below legitimately empties dims_out as a domain_mismatch signal and
        # must not trip this).
        _dims_warn: list[str] = []
        # The condition is the **asymmetric fingerprint** that the other side is normal, not retrying every empty result: both empty usually means
        # an empty/new workspace. Retrying wastes time, and warning would lie (its text says "但指标正常返回").
        # This is D4's existing intentional decision (pinned by test_both_empty_is_ambiguous_no_retry_no_warning);
        # removing `kind` in batch 2 makes `both` the only path, retaining that contract unchanged.
        if not dims and measures:
            for _attempt in range(2):  # up to 2 retries after the initial empty
                await _aio_schema.sleep(0.3 * (_attempt + 1))
                try:
                    dims = await ctx.datarc.list_dimensions(ws)
                except Exception:
                    break
                if dims:
                    break
            if not dims:
                _dims_warn.append(
                    "维度列表本次返回为空，但指标正常返回——疑似 datarc /dimensions/ 临时抖动"
                    "（已重试仍空）。结果可能缺少可拆分维度，不代表该 workspace 无维度；"
                    "勿据此断言「不支持按 X 拆分」，必要时提示用户稍后重试。"
                )
                try:
                    from bowhead_core import metrics as _m_dims

                    _m_dims.inc("bowhead_bi_dims_empty_asymmetric_total")
                except Exception:  # noqa: S110
                    pass

        # Batch 2 adds symmetry: previously only dims had this retry + warning logic; measures had **none**.
        # Before always-both this was latent (models often requested only dimensions, so measures were not queried);
        # afterward both sides are actually queried, making asymmetry a live risk: accepting transiently empty measures
        # makes the model assert "本 workspace 没有指标". Same criterion as dims:
        # **The other side must be normal** for an asymmetric fingerprint (both empty usually means an empty workspace, not instability).
        _measures_warn: list[str] = []
        if not measures and dims:  # Same criterion as dims: only asymmetry counts as a fingerprint
            for _attempt in range(2):
                await _aio_schema.sleep(0.3 * (_attempt + 1))
                try:
                    measures = await ctx.datarc.list_measures(ws)
                except Exception:
                    break
                if measures:
                    break
            if not measures:
                _measures_warn.append(
                    "指标列表本次返回为空，但维度正常返回——疑似 datarc /measures/ 临时抖动"
                    "（已重试仍空）。**不要**据此断言「本 workspace 没有指标」，"
                    "必要时提示用户稍后重试。"
                )
                try:
                    from bowhead_core import metrics as _m_meas

                    _m_meas.inc("bowhead_bi_measures_empty_asymmetric_total")
                except Exception:  # noqa: S110
                    pass

        # followup-context-deck #1 (2026-06-11): datarc returns all workspace **declared** dimensions,
        # but only used=true dimensions have queryable data. NL queries on used=false dimensions silently fall back to
        # default dimensions (autoGroup, e.g. 中文品牌) → wrong-dimension data + _parse.unknown_words,
        # misleading the LLM into endless rewording/retries (s_ee09a8953acc: only 10 of 77 dimensions had used=true;
        # user requested "按省份 top5", but 中文省份 had used=false → all 12 attempts fell back to brand rankings).
        # Root fix: expose only queryable used=true dimensions to the LLM. Missing schema dimension → existing prompt directs the LLM
        # to immediate domain_mismatch (subagent.py:222 "bail immediately when schema lacks the requested dimension"),
        # ending blind retries. Degradation protection: if all used=false (abnormal/empty workspace), still return all dimensions to avoid empty schema
        # leaving the LLM unable to proceed.
        used_dims = [d for d in dims if getattr(d, "used", False)]
        dims_out = used_dims if used_dims else dims
        # 2026-09-17: **symmetrically complete the measures side**. The above fixes dimensions; `measures` was never filtered
        # so `used=false` measures from the same workspace still reached the LLM, with exactly the same harm.
        # Measured demo.datarc.cn ws=1 (3 of 28 measures had used=false):
        #   "面积" → unknown token, empty result;
        #   "IPT" → split into I/PT, all unknown, **yet returned "营业额 229.75万"**;
        #   "id_2" → likewise unknown, **returned "营业额 302.25万"**.
        # datarc raises no error and substitutes **another measure's numbers**. The only defense is to bail when
        # `_parse.unknown_words` is nonempty, a discipline previously held only by compute.
        # ⚠ Criterion must be `used`, **not whether `models` is empty**: measured "利润率", "退款率", and "特殊收入"
        # all query successfully without model bindings (derived measures); filtering by models would wrongly remove them.
        # Same degradation protection as dimensions: return everything when all used=false (abnormal/empty workspace).
        used_measures = [m for m in measures if getattr(m, "used", True)]
        measures_out = used_measures if used_measures else measures
        # Fields the member has no column permission for are withheld after the used
        # fallback, so an all-denied workspace stays empty instead of showing everything.
        from bowhead_core.datarc_permissions import filter_schema

        measures_out, dims_out, _denied_measures, _denied_dims = await filter_schema(ctx.datarc, ws, _member(ctx), measures_out, dims_out)
        _schema_loc = _out_lang(ctx)
        result: dict[str, Any] = {
            "measures": [_measure_public(m, _schema_loc) for m in measures_out],
            "dimensions": [_dimension_public(d, _schema_loc) for d in dims_out],
            # Count of filtered-out declared-but-data-less measures. Same semantics as dimensions_unused_count.
            "measures_unused_count": len(measures) - len(measures_out) - _denied_measures,
            # Count of filtered-out declared-but-data-less (datarc used=false) dimensions. >0 tells the LLM this workspace
            # declares other dimensions without data → if the user asks, return domain_mismatch("本 workspace 无 X 数据"),
            # rather than force a query (which silently falls back to a wrong dimension).
            "dimensions_unused_count": len(dims) - len(dims_out) - _denied_dims,
            # Which Datarc workspace this schema belongs to; it follows the user, not the Space
            # (bi-answer-correctness B2). Same shape as bi_query's `data_source`.
            "data_source": {
                "tool": "datarc",
                "context_id": ws,
                "context_name": getattr(ctx, "active_datarc_workspace_name", "") or "",
            },
        }
        if _denied_measures or _denied_dims:
            # The user may ask for one of these; say it exists but needs permission
            # rather than reporting that the workspace lacks it.
            result["measures_no_permission_count"] = _denied_measures
            result["dimensions_no_permission_count"] = _denied_dims
            result["_permission_note"] = (
                "Some fields are hidden because the current user has no permission for them. "
                "If the user asks for a field missing here, say it may need permission and suggest contacting the data administrator; "
                "do not report it as absent from the workspace."
            )
        # Emit this note only when percentage measures **actually exist**: none means no tokens spent; otherwise it shares
        # the payload containing `format: "percentage"` read by the agent (explanation alongside facts is least likely to be skipped).
        if any(m_pub.get("format") == "percentage" for m_pub in result["measures"]):
            result["_value_format_note"] = _PERCENTAGE_FORMAT_NOTE
        if _dims_warn or _measures_warn:
            result["_schema_warnings"] = _dims_warn + _measures_warn
        return result

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="bi_schema",
                description=(
                    "一次拿 workspace schema (measures + dimensions). 一次并发拿全"
                    " ~500ms, 不用分两次列举. **复杂"
                    " query (公式/清单/派生指标) 第 1 步必跑**, 用于看 BI 真有什么"
                    " raw 字段, 再决定如何组装 query (见 framework §0.45 G4.5 Step 1). "
                    "⚠ dimensions **只含 used=true 的可查维度** (datarc 声明但无数据的维度"
                    "已过滤, 数量见 dimensions_unused_count); schema 里没有的维度 = 本"
                    " workspace 不支持, 直接 domain_mismatch, 不要换措辞盲试 (会被静默兜底成错维度). "
                    "⚠ `names` 与 `display` 是两种东西, 按**用途**分, 不按语言分: "
                    "**拼 query 只用 `names`**(那是 datarc NL 引擎认的标识符, 换成别的"
                    "可能解析不出来); 而**用户会看到的一切文字**——散文里怎么称呼这个指标、"
                    "图表列名、KPI 标签、表头——**有 `display` 就一律用 `display`**. "
                    "它是管理员按本轮语言配的正式名称, 用它不叫翻译, 叫用配置的名字; "
                    "可追溯性由 `names` 那一侧保证, 不受影响. "
                    "❌ 别在同一个答复里图表写 `In-mkt Sales Value`、散文写 `内销销售金额`"
                    ". "
                    "没有 `display` 键才用 names[0] 称呼."
                ),
                # No arguments: fetch all measures + dimensions concurrently. A former `kind` switch
                # was removed for falsely reporting "0 测度" to users (see handler comments).
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_schema,
        )
    )

    # ─── bi_fetch_rows ───────────────────────────────────────
    # Sole entry point for row-level details (group by / lists / top N / negative filters).
    # Calls datarc taskType=22 → ZIP → CSV → list[dict]. Latency 5–15s;
    # use only when an actual table is needed. See docs/datarc-capability-boundaries.md.
    async def _fetch_rows(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "text is required"}
        limit_raw = args.get("limit")
        try:
            limit = int(limit_raw) if limit_raw not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            limit = None
        keep_cols_raw = args.get("columns") or []
        keep_cols = [str(c).strip() for c in keep_cols_raw if str(c).strip()] if isinstance(keep_cols_raw, list) else []

        # bi-outage-latch: timeout already occurred within this ctx ⇒ fail immediately, without another polling window.
        # ⚠ Still raises BIAsyncTaskTimeout (existing except BIConnectionError still catches it,
        # step still records error): only the **wait duration** changes, not failure semantics.
        #
        # ★★ **Wording deliberately avoids attribution**: state only the **observed fact** that the previous query did not return within 60 seconds,
        # not the **inference** that BI is unavailable. One timeout is ambiguous: upstream may be down,
        # or this may simply be a heavy query (successful samples elsewhere have download_ms > 60_000).
        # Attributing a cause would **lie to the user** in the latter case; the observation is true in both.
        # Same principle as this sprint's runtime status: **no attribution avoids false reports**.
        if ctx.bi_outage.latched_reason:
            raise BIAsyncTaskTimeout(
                f"{ctx.bi_outage.latched_reason}；为避免再等一个 60 秒窗口，本次未发起查询。"
                "本轮别再发行级取数——每条都要再付一个 60 秒窗口（Lead 与 peer 各有自己的闸，"
                "跨 agent 那一半只能靠这句话兜）。如实告知用户「数据查询超时、暂未取得可核验结果」，"
                "可建议稍后再试。",
            )
        try:
            result = await ctx.datarc.download_data(
                datarc_workspace_id=_ws(ctx),
                member_uid=_member(ctx),
                text=text,
            )
        except BIAsyncTaskTimeout as _outage:
            # Latch only when a task was accepted but never reaches a terminal state. Other BIConnectionErrors (OBS download failure /
            # network instability) may affect a single query sporadically; **do not latch**.
            ctx.bi_outage.timeouts += 1
            # ★★ Before latching, distinguish whether this query is **inherently heavy**.
            #
            # Measured on 116 (2026-09-02): same data, dimension and measure,
            #   "2026年各种类大类营业额"        → 4.0 seconds / 38 rows
            #   "2026年营业额按种类大类**明细**" → 133.1 seconds / **1 million rows**
            # Production comparison: five tasks submitted together at 21:44, three completed in 0–1 seconds,
            # two took 224/304 seconds; their params differed only by one `keyword_detail` token.
            # **All eventually succeeded** (status=2, result ZIPs present, errors empty), completing only after
            # we gave up. This means a query needs minutes, not that BI is unavailable.
            #
            # Purpose of routing: timeouts with row-level words **do not latch**; latching would also block the reworded
            # aggregation query that could return in four seconds, closing off the only solution.
            # (Old wording "请不要换措辞重试" was consistent with old behavior: the latch blocked everything, so rewording really was useless.
            #  Both were wrong.)
            if _looks_row_level(text):
                raise BIAsyncTaskTimeout(
                    f"{_outage}。这条查询里带「明细/逐笔/原始记录」这类词 ⇒ datarc 走**行级取数**，"
                    "百万行量级（实测 133 秒）。用户说「明细表」「一个 X 一行」「不要只给 Top N」时，"
                    "要的通常是**按 X 分组的聚合**——去掉那个词重发一次（同一份数据实测 4 秒）。"
                    "确实需要逐笔原始记录、或改写后仍超时，再如实告知用户查询超时。",
                ) from _outage
            ctx.bi_outage.latched_reason = "上一次行级数据查询 60 秒内未返回"
            raise BIAsyncTaskTimeout(
                f"{_outage}。本轮后续行级查询将不再发起。如实告知用户查询超时、暂未取得可核验结果，并说明可缩小范围或稍后再试。",
            ) from _outage
        # Success ⇒ reset. A sporadic failure should not affect later queries that have recovered.
        ctx.bi_outage.timeouts = 0
        ctx.bi_outage.latched_reason = ""

        all_rows = result.rows
        cols = list(result.columns)
        preflight_notes: list[str] = []

        # datarc period drift: it silently replaces the requested period without error (see evidence in
        # `_period_drift_note` at module top). Both criteria fail open: no report if year/month cannot be recognized.
        # ⚠ Must check here: `_debug_csv_filename` (the only field carrying the **actual executed period**)
        # reaches only the backend; `model_view` strips it from the model view and the batch wrapper whitelist does not forward it.
        for _drift in (
            _period_drift_note(text, result.csv_filename),
            _month_coverage_note(text, all_rows),
            _unfinished_period_note(text),
            _uncomparable_target_note(text, cols),
        ):
            if _drift:
                preflight_notes.append(_drift)

        # datarc does not truncate "前 N"; tell the LLM it was handled locally
        low = text.lower()
        has_topn = any(s in text for s in ("前10", "前20", "前5", "前3", "前50", "前100")) or any(
            s in low for s in ("top10", "top 10", "top5", "top 5", "top20")
        )
        if has_topn and limit is None and len(all_rows) > 10:
            preflight_notes.append(f"datarc 对 'top N / 前 N' 不截断，已返全量 {len(all_rows)} 行；如需 top N 自行排序切片或传 limit 参数")

        # Full data = rows after dropping repeated datarc headers; artifact persistence, row_count and model-visible rows share this source.
        full_rows = _drop_repeated_csv_header_rows(list(all_rows), list(result.columns))
        rows = list(full_rows)
        cols_before_projection = list(cols)
        if keep_cols:
            resolved, missing, ambiguous = _resolve_keep_cols(keep_cols, cols)
            # Abandon the entire projection when any requested column is unresolved.
            # Keeping only matching columns could hide a real grouping dimension. Projection
            # is a token optimization and must not make a user-visible table incomplete.
            if missing or ambiguous:
                for m in missing:
                    preflight_notes.append(f"请求列 {m!r} 在结果里不存在（实际列：{cols}）；本次不做列投影，返回全部列")
                for a, cands in ambiguous:
                    preflight_notes.append(f"请求列 {a!r} 同时匹配 {cands}，无法判定；本次不做列投影，返回全部列")
            elif resolved:
                cols = resolved
                rows = [{c: r.get(c) for c in resolved} for r in rows]
        if limit is not None and limit > 0 and len(rows) > limit:
            rows = rows[:limit]

        # ⭐ Prevent LLM prompt explosion: large datasets expose only the first N sample rows; full data uses artifact_id
        # Default inline cap is 30 rows; above it, truncate and force the sandbox path
        inline_cap = int(args.get("inline_max_rows") or 30)
        rows_for_llm = rows[:inline_cap] if len(rows) > inline_cap else rows
        full_count = len(full_rows)
        # ★ bi-answer-correctness C (2026-09-15): `row_count` is **full data**; `rows_shown` is the model-visible count.
        # Previously `row_count=len(rows_for_llm)`, opposite to full row_count in chart specs / artifacts / sandbox INPUTS;
        # measured on 116, model used `row_count: 30` to write "共 30 个种类大类", when there were actually 38.
        # Rows removed by limit also mean only part of the data is shown.
        truncated_for_llm = len(rows_for_llm) < full_count

        out: dict[str, Any] = {
            "columns": cols,
            "rows": rows_for_llm,
            "row_count": full_count,
            "rows_shown": len(rows_for_llm),
            "total_rows": result.total_rows,
            # Step card's "N 行 · M 列" previously used counts **after projection + truncation**,
            # displaying 4,180 rows / 3 columns as "30 行 · 2 列", reasonably making users think there were only two columns.
            # Expose the pre-projection column count so summary is truthful (see subagent._prettify_output_preview).
            "_columns_before_projection": len(cols_before_projection),
            # Batch 2/c10 (PRD §3.11): structured provenance, enabling LLM source descriptions;
            # tool_result events / previews carry it unchanged.
            "data_source": {
                "tool": "datarc",
                "context_id": _ws(ctx),
                "context_name": getattr(ctx, "active_datarc_workspace_name", "") or "",
            },
            # ⚠ debug_* fields are not for python_sandbox CSV reads: the LLM often hallucinates csv_filename
            # as a sandbox container path for pd.read_csv(), which will fail. To use table
            # data in the sandbox, use artifact_id through inputs (see artifact fields below).
            "_debug_csv_filename": result.csv_filename,
            "_debug_task_uid": result.task_uid,
            "_debug_download_ms": result.download_ms,
        }
        # datarc self-reports the actual effective query specification (added for taskType=22). We send natural language;
        # datarc parses models/measures/dimensions/**time range**/filters before executing. Previously this entire self-report was discarded,
        # leaving the model guessing what it queried from row data. Expose `message_json` (fixed English keys,
        # values all string[]) to the model; leave the human-readable `message` for backend troubleshooting, spending no model budget.
        # ⚠ Values are **display strings**, varying with member account language (Chinese/English). datarc explicitly says not to rely on
        # the text format within array elements for queries ⇒ pass unchanged for model reading; never parse them for any decision.
        # Type narrowing is not formality: these values come from the external response body, whose shape is controlled externally;
        # truthiness checks also let MagicMock test stubs leak straight into the result dict.
        _ej = getattr(result, "explain_json", None)
        if isinstance(_ej, dict) and _ej:
            out["query_explain"] = _ej
            # G §2.6: datarc reports a row limit ⇒ **these rows themselves** are partial, not the full set. C defines row_count
            # as full data, but here that means post-truncation; model reads "10" as ten in total (measured in development:
            # same question without 前10 returned 38 rows, with 前10 returned 10). Check only whether `limit` exists; without structured query specification,
            # **say nothing**. Do not infer top N from our own question: old instances do not truncate, so guessing would produce false claims.
            if "limit" in _ej:
                from bowhead_core.tool_result_budget import query_limit_note

                preflight_notes.append(query_limit_note())
                out["_rows_limited_by_query"] = True
        _em = getattr(result, "explain_message", None)
        if isinstance(_em, str) and _em:
            out["_debug_explain_message"] = _em
            # ★ Without this fallback the feature is dead on 116 (E handoff §2): that instance returns only `message`,
            # not `message_json` ⇒ query_explain is never set; the only informative sentence has a `_debug_`
            # prefix and is stripped by `model_view._BACKEND_ONLY_KEYS` ⇒ model receives none of it.
            # Fallback: expose that sentence to the model under a key **without the `_debug_` prefix** (`_debug_` means
            # backend-only in this repository; removing the strip line would also expose CSV filenames).
            # **Do not set** when a structured version exists: synonymous copies spend the budget twice on the same information.
            if "query_explain" not in out:
                out["query_explain_text"] = _em
        # datarc may split one question into multiple data items (multiple CSVs); we read only the first, and `errors` lists
        # failed downloads (ZIP contains only successes). Both were previously **silent**, so model and user assumed they had everything.
        _dl_errors = [i for i in (getattr(result, "download_errors", None) or []) if isinstance(i, int)]
        _csv_count = int(getattr(result, "csv_count", 0) or 0)
        if _csv_count > 1 or _dl_errors:
            _partial_parts: list[str] = []
            if _csv_count > 1:
                _partial_parts.append(f"datarc 把结果拆成了 {_csv_count} 个数据文件，这里只读了第一个")
            if _dl_errors:
                _partial_parts.append(f"有 {len(_dl_errors)} 个数据项下载失败（下标 {_dl_errors}）")
            preflight_notes.append("⚠ 本次数据不完整：" + "；".join(_partial_parts) + "。结论里要说明这一点，或把问题拆成多条 query 分别取。")
            out["_partial_data"] = True
        if truncated_for_llm:
            from bowhead_core.tool_result_budget import rows_truncation_note

            preflight_notes.append(rows_truncation_note(len(rows_for_llm), full_count))
            out["_rows_truncated"] = True
            out["_rows_inline_count"] = len(rows_for_llm)
            out["_rows_full_count"] = full_count

        # ⭐ M157 run-2 lesson: multidimensional + multi-filter NL queries sometimes fail to parse in datarc NL2SQL,
        # returning empty without error while consuming 60–130s network + DB time. Pattern:
        #   - columns lists ≥5 dimensions
        #   - text contains multiple filter words (... 且 ... / 中 / 为 X)
        #   - total_rows == 0, download_ms > 60_000
        # On a match, hint the LLM to fetch separately with single filters + join locally, rather than stack complex NL.
        _MULTI_FILTER_MARKERS = ("且", " 中 ", " 为 ", " in ", " IN ")
        n_dim_cols = len(keep_cols) if keep_cols else len(cols)
        download_ms = int(getattr(result, "download_ms", 0) or 0)
        n_filter_markers = sum(text.count(m) for m in _MULTI_FILTER_MARKERS)
        if result.total_rows == 0 and len(rows) == 0 and n_dim_cols >= 5 and n_filter_markers >= 2 and download_ms > 60_000:
            try:
                from bowhead_core import metrics as _m

                _m.inc(
                    "bowhead_bi_fetch_multidim_empty_total",
                    help="fetch_rows 多 dim+多 filter 但返 0 行 + 慢 (NL2SQL 疑似解析失败).",
                )
            except Exception:  # noqa: S110
                pass
            preflight_notes.append(
                f"⚠ multi-dim+multi-filter 返 0 行 (cols={n_dim_cols}, "
                f"filter_markers={n_filter_markers}, ms={download_ms}). "
                "datarc NL2SQL 对复合 filter 解析不稳. **建议**: 拆成单 filter "
                "多次 fetch + python_sandbox 本地 join, 不要继续堆复杂 NL."
            )

        # Empty fast path: datarc succeeds but returns 0 rows. Say so plainly; the agent decides what to do next.
        # No in-tool term judgment: a word that may not match the data's naming is the agent's call to check
        # with bi_concept_resolve (2026-10-17 user decision: tools do not call the model).
        if result.total_rows == 0 and len(rows) == 0:
            out["_no_data"] = True
            out["_no_data_hint"] = (
                "datarc 查询成功但返回 0 行 —— 该维度/指标/时间范围确实没有匹配数据。"
                "不要重发同样的 query;换方法(换时间段、去掉某个 filter、或直接报告无数据)。"
                "如果怀疑是某个词和数据里的叫法对不上(看 `_parse`),可以用 bi_concept_resolve 核对。"
            )

        _pv = args.get("_stripped_product_vocab")
        if _pv:
            preflight_notes.append(
                f"已从查询里去掉 {_pv}——那是 Bowhead 界面的词，不是你数据里的字段，"
                "留着会被 BI 当成筛选值（实测能把结果打成 0 行）。"
                "查询文本里只放**维度 / 指标 / 时间 / 筛选值**，别写给系统看的指令。"
                "若你的数据里确有同名字段，换个说法再查。"
            )
        # Column currencies/units: without these the model labels currency from memory; in 3 of 7 measured turns it wrote $ for RMB.
        # Omit the key for empty maps; an empty shell only invites the model to interpret why units are absent.
        _units = _column_units(ctx, cols)
        if _units:
            out["_units"] = _units

        if preflight_notes:
            out["_preflight_hint"] = "; ".join(preflight_notes)

        # ─── Persist **full rows** (before limit) to artifact_store for python_sandbox
        # to load through inputs. Avoid the LLM typing 1,000+ CSV rows verbatim into a code string
        # (token explosion + errors + truncation). Only when ctx.artifact_store is nonempty.
        if ctx.artifact_store is not None and all_rows:
            import csv as _csv
            import io as _io

            csv_columns = list(result.columns)
            csv_rows = full_rows
            buf = _io.StringIO()
            w = _csv.DictWriter(buf, fieldnames=csv_columns)
            w.writeheader()
            for r in csv_rows:
                w.writerow(r)
            csv_bytes = buf.getvalue().encode("utf-8")
            fn = result.csv_filename or "bi_fetch_rows.csv"
            try:
                art = await ctx.artifact_store.put_bytes(
                    content=csv_bytes,
                    filename=fn,
                    mime="text/csv",
                    source="bi_fetch_rows",
                    created_by=ctx.user_id or None,
                    # Sprint µ N4: link to session so Data tab surfaces it
                    session_id=getattr(ctx, "session_id", "") or None,
                    created_by_turn=getattr(ctx, "turn_id", "") or None,
                    # Batch 2a: record the original query producing this artifact. This is the **only** BI data artifact
                    # persistence entry (bi_query converges here through _query_single), so one location suffices.
                    # Do not expose to the model yet; that is batch 2b. This batch's negative control is unchanged model-view bytes.
                    origin_query=text,
                    # Row count can only be calculated now (downstream must reread the blob, which will be reclaimed).
                    # Uses: near-duplicate detection (22% of within-turn near duplicates differ only by row count) + scale warnings below.
                    row_count=len(csv_rows),
                )
                out["artifact_id"] = art.id
                out["artifact_filename"] = art.filename
                from bowhead_core.canvas_tools import _parse_csv_with_types
                from bowhead_core.chart.binding_catalog import build_binding_catalog

                catalog_rows, catalog_columns = _parse_csv_with_types(csv_bytes)
                out["chart_catalog"] = build_binding_catalog(catalog_columns, catalog_rows, raw=csv_bytes)
                _record_bi_fetch_scale(
                    byte_size=len(csv_bytes),
                    row_count=len(csv_rows),
                    query_text=text,
                )
                # Render the input contract for the caller's actual sandbox entry through
                # sandbox_call_shape. If the entry is unknown, describe inputs at the same level
                # as code, which is valid for both supported shapes.
                out["_how_to_use_in_sandbox"] = sandbox_inputs_hint(
                    _caller_sandbox_entry(ctx),
                    artifact_id=art.id,
                    row_count=len(csv_rows),
                )
            except Exception as _e:
                # Sprint omicron Phase 0: surface artifact persistence failures
                # instead of silent swallow. T3 prod audit (2026-05-11):
                # /api/v1/sessions/{sid}/artifacts returned empty across 15 turns
                # despite bi_fetch_rows succeeding — silent fail masked the issue.
                logger.error(
                    "bi_fetch_rows: artifact put_bytes failed (omicron-0)",
                    exc_info=_e,
                )
                # Artifact persistence stays soft so a transient delivery failure
                # does not erase an otherwise successful data response.
        return out

    # ─── bi_query (merged ask_question + fetch_rows + breakdown entry) ─
    # Recommend this to the LLM instead of specific tools. Defaults to fetch_rows (taskType=22, persists artifact);
    # `fast=True` switches to ask_question (single-value lookup, <2s synchronous path).
    # Preflight runs automatically in the lower wrapper; LLM reads `_preflight_warnings` to decide next steps.
    #
    # Sprint χ-3: schema uses `queries: List[str]` for batches (parallel fan-out).
    # _query_single implements one query (extracted from the original _query body, no behavior change).
    # Top-level _query handler: normalize + sanity check + dispatch (single/batch/sequential fallback).
    async def _query_single(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        """Single bi_query — text → _fetch_rows (only path post bi-hitl-cleanup P13.5).

        λ-F1: turn-scope cache — 同 turn 内同 normalized_query 复用结果, 不再 hit datarc.
              error 不 cache (poisoning guard).
        lead-analyst-logic 批2: cache miss 后经 `_single_flight_bi_fetch` 收口——
              并发同 query（多 peer 同轮扇出 / batch 内重复）只打一次 datarc、
              共享同一 result/artifact_id；λ-F1 只挡串行重复, 挡不住并行竞态。

        bi-hitl-cleanup B2 (2026-05-21): ar2 preflight 内嵌路径已砍.
        bi-hitl-cleanup P13.5 (2026-05-21): fast/ask_question 路径全砍, 统一 _fetch_rows.
        bi-hitl-cleanup P13 (2026-05-21): cut transparency — parallel cut + fetch.
        """
        text = args["text"]
        # Remove product vocabulary before building the cache key so it describes the
        # actual outgoing query. See strip_product_vocabulary for the narrow scope.
        from bowhead_core.bi_preflight import strip_product_vocabulary as _strip_pv

        text, _stripped_pv = _strip_pv(text)
        if _stripped_pv:
            logger.info("bi_query.product_vocab_stripped turn=%s words=%s", getattr(ctx, "turn_id", None), _stripped_pv)
            args = {**args, "text": text, "_stripped_product_vocab": _stripped_pv}

        # λ-F1 cache lookup (fast placeholder False, kept for cache_key signature stability)
        cache_key = _bi_cache_key(
            text,
            False,
            args.get("limit"),
            args.get("columns"),
            inline_max_rows=args.get("inline_max_rows"),
            mode=args.get("mode"),
            datarc_workspace=_ws(ctx),  # Batch 2/c6: after switching, do not use the old context's cache
        )
        cached = _bi_cache_get(ctx.turn_id, cache_key)
        if cached is not None:
            try:
                from bowhead_core import metrics as _m

                _m.inc("bowhead_bi_query_cache_hit_total", labels={"path": "single"}, help="λ-F1: cache hit on same-turn duplicate bi_query.")
            except Exception:  # noqa: S110
                pass
            return _repeat_guarded(cached, _bi_bump_repeat(ctx.turn_id, cache_key))

        return await _single_flight_bi_fetch(
            ctx.turn_id,
            cache_key,
            lambda: _query_single_fetch(args, ctx, text=text),
        )

    async def _query_single_fetch(args: dict[str, Any], ctx: ToolExecutionContext, *, text: str) -> Any:
        """批2 拆分：`_query_single` 去掉 cache/single-flight 后的真取数体
        （cut 透明度 + fetch + _parse 注入 + cache put + 语义 hint），逻辑零变化。
        cache_key 在本函数内重算（与 wrapper 同参同结果——`_bi_cache_key` 纯函数）。
        """
        # Key for λ-F1 cache put (same as wrapper lookup key)
        cache_key = _bi_cache_key(
            text,
            False,
            args.get("limit"),
            args.get("columns"),
            inline_max_rows=args.get("inline_max_rows"),
            mode=args.get("mode"),
            datarc_workspace=_ws(ctx),
        )

        # Sprint agent-redesign-1 Phase E2 — semantic prefix advisory hint.
        # Read BEFORE fetch; the actual fetch still runs (no block). Hint
        # attached to result so peer's next decision is informed.
        _sem_prefix = _semantic_prefix(text)
        _sem_hint = _semantic_cache_lookup(ctx.turn_id, _sem_prefix)

        # bi-hitl-cleanup P13 (2026-05-21): cut transparency — run datarc /cut/
        # in parallel with fetch so peer sees how datarc parsed the NL text
        # (tokens + unknown_words + half_match_values). Solves S3-class fuzzy
        # silent-fake: peer can detect `_parse.unknown_words=["国美店"]` and bail
        # instead of trusting fuzzy aggregate row from datarc.
        # Best-effort: cut failure does NOT fail bi_query (omit _parse).
        #
        # P13.6 (2026-05-21): cut endpoint previously silently swallowed every timeout/error with bare `except Exception`,
        # leaving 51/52 production calls silently without `_parse`. Fix: distinguish
        # TimeoutError from other exceptions, emit `bowhead_bi_query_cut_failed_total
        # {reason}` + log.warning for visibility. timeout=60s matches the datarc client default
        # (too short would time out normal calls too, reducing _parse injection).
        import asyncio as _asyncio_cut

        async def _cut_safe() -> CutResult | None:  # noqa: F821
            try:
                return await _asyncio_cut.wait_for(
                    ctx.datarc.cut_question(
                        datarc_workspace_id=_ws(ctx),
                        member_uid=_member(ctx),
                        question=text,
                    ),
                    timeout=60.0,
                )
            except TimeoutError:
                _reason = "timeout"
            except Exception as _cut_exc:
                _reason = type(_cut_exc).__name__
            # Failure path: emit metric + log.warning so silent fails become visible.
            try:
                from bowhead_core import metrics as _m_cut_fail

                _m_cut_fail.inc(
                    "bowhead_bi_query_cut_failed_total",
                    labels={"reason": _reason},
                    help="P13.6: cut transparency call failed (timeout / exception).",
                )
            except Exception:  # noqa: S110
                pass
            import logging as _logging_cut

            _logging_cut.getLogger("bowhead.bi_query").warning(
                "bi_query cut transparency failed reason=%s query=%r",
                _reason,
                text[:80],
            )
            return None

        # bi-hitl-cleanup P13.5 (2026-05-21): remove fast path and unify through _fetch_rows.
        # Single-value lookup returns a one-row table; LLM reads rows[0][col]. Benefits:
        # - One result shape (rows+cols+artifact+_parse), no peer if-else
        # - artifact_id always present → sandbox/full data always accessible
        # - _ask_question dead code will be removed next ship; schema "fast" already removed, this only protects internal callers
        fetch_coro = _fetch_rows(
            {
                "text": text,
                **({"limit": args["limit"]} if "limit" in args else {}),
                **({"columns": args["columns"]} if "columns" in args else {}),
                **({"inline_max_rows": args["inline_max_rows"]} if "inline_max_rows" in args else {}),
                # ⚠ **Literal whitelist**: _fetch_rows receives no keys not explicitly listed here.
                # Product vocabulary cleanup notification must reach `_preflight_hint` through it, so forward explicitly.
                **({"_stripped_product_vocab": _pv} if (_pv := args.get("_stripped_product_vocab")) else {}),
            },
            ctx,
        )

        import asyncio as _asyncio_p13

        async def _field_names_safe() -> tuple[dict[int, str], dict[int, str]]:
            # Names for translating cut props; read from the shared schema cache in parallel with the query.
            from bowhead_core.bi_cut_tokens import field_names
            from bowhead_core.datarc_schema_cache import get_schema_with_cache

            try:
                schema = await get_schema_with_cache(
                    datarc_client=ctx.datarc, datarc_workspace_id=_ws(ctx), schema_cache=getattr(ctx, "schema_cache", None)
                )
                return field_names(schema.measures), field_names(schema.dimensions)
            except Exception as e:
                logger.debug("bi_query.field_names_unavailable ws=%s err=%s", _ws(ctx), e)
                return {}, {}

        cut_result, result, (measure_names, dimension_names) = await _asyncio_p13.gather(_cut_safe(), fetch_coro, _field_names_safe())

        # Inject _parse onto result if cut succeeded and result is dict
        if cut_result is not None and isinstance(result, dict):
            from bowhead_core.bi_cut_tokens import describe_token
            from bowhead_core.bi_unknown_words import (
                denoise_unknown_words,
                engine_applied_no_filter,
                fallback_measure_columns,
                unfiltered_fact_note,
                unknown_words_note,
                withhold_fallback_measures,
            )

            # bi-answer-correctness A step 1: the denoised list contains truly unrecognized words (raw lists are nonempty 95% of the time,
            # so the model stopped reading them). Tokens carry names, not Datarc ids; raw props stay in `_debug_cut_props`
            # (hidden from the model by model_view) for audit.
            unknown_words = denoise_unknown_words(t.word for t in cut_result.unknown_tokens())
            half_match = [t.word for t in cut_result.value_without_dim()]
            result["_parse"] = {
                "tokens": [describe_token(t.word, t.prop, measures=measure_names, dimensions=dimension_names) for t in cut_result.tokens],
                "unknown_words": unknown_words,
                "half_match_values": half_match,
            }
            result["_debug_cut_props"] = [{"word": t.word, "prop": t.prop} for t in cut_result.tokens]
            # Step 2: deterministic naming, without relying on the model to inspect `_parse`. Skip failed retrievals: without data there is no filtering claim.
            if unknown_words and not _bi_result_is_error(result):
                _notes = [unknown_words_note(unknown_words)]
                # G §2.5: cross-check structured query specification for stronger evidence: datarc reports **no business filters at all**,
                # allowing the fact that it did not filter by these words, rather than an inference. This decision belongs to tools, not prompts (user said
                # it is a tool function); inspect only **key presence** (keys do not vary with account language), never parse values;
                # state facts without conclusions. Text-only instances (current 116) retain the sentence above without asserting this fact.
                if engine_applied_no_filter(result.get("query_explain")):
                    _notes.append(unfiltered_fact_note(unknown_words))
                _unknown_note = "\n".join(_notes)
                _prev_hint = result.get("_preflight_hint")
                result["_preflight_hint"] = f"{_prev_hint}\n{_unknown_note}" if _prev_hint else _unknown_note
                # No measures recognized and engine supplies its default ⇒ that column is a fallback; withhold it (criteria in bi_unknown_words)
                _fallback_cols = fallback_measure_columns(cut_result.tokens, unknown_words, result.get("query_explain"), result.get("columns"))
                if _fallback_cols:
                    withhold_fallback_measures(result, _fallback_cols, unknown_words, (result.get("query_explain") or {}).get("measures") or [])
            try:
                from bowhead_core import metrics as _m_p13

                _m_p13.inc(
                    "bowhead_bi_query_cut_injected_total",
                    labels={
                        "has_unknown": "1" if unknown_words else "0",
                        "has_half_match": "1" if half_match else "0",
                    },
                    help="P13: cut transparency — _parse field injected on bi_query result.",
                )
            except Exception:  # noqa: S110
                pass

        # λ-F1 P0-1: cache only ok results — never cache transient errors
        if not _bi_result_is_error(result):
            _bi_cache_put(ctx.turn_id, cache_key, result)

        # Sprint agent-redesign-1 Phase E2 — semantic cache hint advisory.
        # If a prior call shared this semantic prefix, attach `_cache_hint` to
        # current result so peer's next-iter LLM sees it and considers reuse
        # via python_sandbox instead of refetching BI.
        # If no prior call, register THIS one as the seed.
        if isinstance(result, dict) and not _bi_result_is_error(result):
            try:
                _aid = result.get("artifact_id") or result.get("_ref_artifact_id")
                if _sem_hint:
                    # Show advisory hint pointing to prior call.
                    result["_cache_hint"] = {
                        "prior_artifact_id": _sem_hint.get("artifact_id"),
                        "prior_query": _sem_hint.get("query")[:120] if _sem_hint.get("query") else "",
                        "suggestion": (
                            "同 turn 已查过相同时间+地区+指标 prefix 数据. "
                            "如果只是 Top-N / 维度分组 / 排序差异, "
                            "考虑用 python_sandbox 处理 prior_artifact_id 而非 refetch BI."
                        ),
                    }
                    try:
                        from bowhead_core import metrics as _m_sem

                        _m_sem.inc(
                            "bowhead_bi_query_semantic_cache_hint_total",
                            labels={"path": "single"},
                            help="Phase E2: semantic prefix hit → advisory hint emitted.",
                        )
                    except Exception:  # noqa: S110
                        pass
                elif _aid:
                    # First call with this semantic prefix → seed.
                    _semantic_cache_put(
                        ctx.turn_id,
                        _sem_prefix,
                        {
                            "artifact_id": _aid,
                            "query": text,
                        },
                    )
            except Exception:  # noqa: S110
                pass

        return result

    # ─── χ-3 batch helpers ────────────────────────────────────
    _META_KEYWORDS = ("怎么用", "how to", "是什么", "解释", "举例", "示例", "tutorial", "如何用")
    # ar2-v2 fix: 4 chars covers "上海销售" / "万达营业" / "Q3销售";
    # was 8 → blocked legit short Chinese queries before ar2 preflight could run.
    _MIN_QUERY_LEN = 4

    def _sanity_check_query(q: str) -> str | None:
        """Return error_kind str if query looks non-data (meta/help), else None.
        RISK-3 (r3 review) — defend against LLM stuffing meta questions into queries."""
        if not isinstance(q, str) or len(q.strip()) < _MIN_QUERY_LEN:
            return "non_query_text"
        if any(kw in q for kw in _META_KEYWORDS):
            return "non_query_text"
        return None

    def _normalize_bi_query_args(args: dict[str, Any]) -> dict[str, Any]:
        """χ-3 transition: legacy `text` → `queries=[text]` (silent fallback).
        Increment metric so we can monitor LLM emit rate of old shape."""
        if "text" in args and "queries" not in args:
            try:
                from bowhead_core import metrics as _m_chi3

                _m_chi3.inc(
                    "bowhead_bi_query_legacy_shape_total",
                    labels={},
                    help="χ-3: LLM emitted bi_query with legacy text= shape; normalize wrapped to queries=[].",
                )
            except Exception:  # noqa: S110
                pass
            new_args = dict(args)
            new_args["queries"] = [new_args.pop("text")]
            return new_args
        return args

    def _wrap_sub_result(sub: dict[str, Any] | Any, query_text: str) -> dict[str, Any]:
        """Wrap _query_single response into batch sub-entry shape.

        χ-3.1 P0-C1/E1/E2: handles BOTH _fetch_rows shape (rows/artifact_id) and
        _ask_question fast-path shape (metrics/url/explanations). Normalizes
        error_code → error_kind (fast-path emits error_code; batch consumers expect error_kind).

        Sub-entry contract: {ok, artifact_id, _ref_artifact_id, query_text,
        error_kind, error_message, sample_rows, _preflight_warnings,
        metrics (fast-path), url (fast-path)}.
        """
        if not isinstance(sub, dict):
            return {
                "ok": False,
                "artifact_id": None,
                "_ref_artifact_id": None,
                "query_text": query_text,
                "error_kind": "non_dict_result",
                "error_message": f"got {type(sub).__name__}",
                "sample_rows": [],
                "_preflight_warnings": [],
            }
        # Error normalization: _ask_question emits error_code, _fetch_rows emits error_kind.
        error_kind = sub.get("error_kind") or sub.get("error_code")
        error_message = sub.get("error_message") or sub.get("error")

        # ok detection: explicit ok=False wins; else absence of error_kind/code means OK.
        ok = bool(sub.get("ok")) and error_kind is None if "ok" in sub else error_kind is None

        artifact_id = sub.get("artifact_id")  # only slow-path _fetch_rows has this

        # Sample data: prefer rows (slow-path), fall back to metrics (fast-path).
        # prod-bug-bash-038 #1: bumped 5 → 10 so LLM sees enough rows to trust the
        # preview (5 was triggering "incomplete data" hallucination → 3× retry loop
        # per trace t_c5403e7c). Also surface `total_rows` so LLM knows full count.
        sample_rows: list[Any] = (sub.get("rows") or [])[:10]
        metrics_summary: list[dict[str, Any]] = []
        if not sample_rows and isinstance(sub.get("metrics"), list):
            # Fast-path: metrics is list of {name, content, value} dicts.
            metrics_summary = list(sub["metrics"])[:5]

        wrapped: dict[str, Any] = {
            "ok": ok,
            "artifact_id": artifact_id,
            "_ref_artifact_id": artifact_id,  # ENG-4: mirror to prevent LLM array-index misuse
            "query_text": query_text,
            "error_kind": error_kind,
            "error_message": error_message,
            "sample_rows": sample_rows,
            "total_rows": sub.get("total_rows"),  # prod-bug-bash-038 #1
            "_preflight_warnings": sub.get("_preflight_warnings") or [],
        }
        # bi-answer-correctness C (2026-09-15): batch subresults also include full row counts and counts shown to the model.
        # Previously only sample_rows (≤10) + total_rows, without truncation marker; measured on 116, model called five sample rows
        # "省份数据仅覆盖5个省市" (actually 38 rows). Fast path (metrics) has no row-level data; do not add.
        if "rows" in sub:
            _full = sub.get("row_count") if isinstance(sub.get("row_count"), int) else sub.get("total_rows")
            if isinstance(_full, int):
                wrapped["row_count"] = _full
                wrapped["rows_shown"] = len(sample_rows)
                if len(sample_rows) < _full:
                    wrapped["_rows_truncated"] = True
        # Actual query specification reported by datarc: each batch subquery has its own; without forwarding it, batching returns the model
        # to guessing what it queried from row data. Not whitelisted = discarded (existing wrapper semantics).
        if sub.get("query_explain"):
            wrapped["query_explain"] = sub["query_explain"]
        # Fast-path specific: surface metrics + url so LLM/frontend can render.
        if metrics_summary:
            wrapped["metrics"] = metrics_summary
        if sub.get("url"):
            wrapped["url"] = sub["url"]
        # hint_use_tool relayed (auto-fallback / preflight hint).
        if sub.get("hint_use_tool"):
            wrapped["hint_use_tool"] = sub["hint_use_tool"]
        # bi-hitl-cleanup P13 (2026-05-21): forward _parse (cut transparency)
        # injected by _query_single. Batch path was stripping it because
        # wrapped only whitelisted specific fields.
        if sub.get("_parse"):
            wrapped["_parse"] = sub["_parse"]
        # empty fast-path: forward the success-but-empty signal and hints. Without this the
        # whitelist strips them (prod t_711b31fecee1 masked the empty-result signal).
        # `data_source` names the Datarc workspace the rows came from (prompts point the model at it).
        for _ek in ("_no_data", "_no_data_hint", "_preflight_hint", "_units", "chart_catalog", "data_source"):
            if sub.get(_ek) is not None:
                wrapped[_ek] = sub[_ek]
        return wrapped

    async def _query(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        """Top-level bi_query handler — dispatches single / batch (gather) / sequential-fallback.

        - N=1 (legacy text or queries=[one]) + BI_BATCH OFF → flat shape (0.14.1 backward-compat)
        - N≥2 + BI_BATCH ON → asyncio.gather fan-out, return {results: [...]}
        - N≥2 + BI_BATCH OFF → sequential N _query_single calls, wrap into {results: [...]}
          (LLM sees uniform schema regardless of flag)
        """
        import asyncio as _asyncio

        from bowhead_core import chi_flags as _chi_flags

        args = _normalize_bi_query_args(args)
        queries: list[str] = list(args.get("queries") or [])

        # Empty (defensive: shouldn't happen with schema validation)
        if not queries:
            return {"results": [], "error_kind": "no_queries"}

        # bi-hitl-cleanup P8 (2026-05-21): top-level bi_query call counter.
        # Replaces removed ar2 preflight counter; tracks invocation volume by
        # caller (peer profile derived from ctx where available).
        #
        # P9 SRE fix (2026-05-21): use `_running_profile` (set by SubagentRunner
        # at subagent.py:1608) — same source as bi_concept_resolve.py:403.
        # Previous attr names (`subagent_profile` / `profile_name`) don't exist
        # in real ctx, so caller would have been "unknown" 100% in prod.
        try:
            from bowhead_core import metrics as _m

            _caller = str(getattr(ctx, "_running_profile", None) or "lead")
            _m.inc(
                "bowhead_bi_query_call_total",
                labels={
                    "caller": _caller,
                    "n_queries": "1" if len(queries) == 1 else ("2-5" if len(queries) <= 5 else "6-20"),
                },
                help="bi_query top-level handler invocations (B8 L1 BI metric).",
            )
        except Exception:  # noqa: S110
            pass

        # Sanity-check each query (RISK-3 — meta/help text rejection without datarc call)
        sanity_errors: list[tuple[int, str, str | None]] = [(i, q, _sanity_check_query(q)) for i, q in enumerate(queries)]

        # Single-query path (N=1) — return flat shape backward-compat.
        # Even with BI_BATCH ON, N=1 stays flat so single-shot callers see unchanged response.
        if len(queries) == 1:
            err = sanity_errors[0][2]
            if err:
                return {
                    "ok": False,
                    "artifact_id": None,
                    "query_text": queries[0],
                    "error_kind": err,
                    "error_message": f"sanity check: {err}",
                    "rows": [],
                }
            single_args = {**args, "text": queries[0]}
            single_args.pop("queries", None)
            _single = await _query_single(single_args, ctx)
            # Review batch 1-R4: flat shape previously only had artifact_id, while batch shape used _ref_artifact_id;
            # prompt instructions to read _ref_artifact_id therefore failed on the common N=1 case. Add a mirror key
            # for identical access in both shapes; purely additive, no existing field changes.
            if isinstance(_single, dict) and _single.get("artifact_id") and "_ref_artifact_id" not in _single:
                _single["_ref_artifact_id"] = _single["artifact_id"]
            return _single

        # Batch path (N≥2). BI_BATCH flag selects parallel vs sequential internal execution.
        per_batch_sem: _asyncio.Semaphore | None = None
        use_parallel = _chi_flags.bi_batch_enabled()
        if use_parallel:
            per_batch_sem = _asyncio.Semaphore(_chi_flags.max_concurrent())
            try:
                from bowhead_core import metrics as _m_chi3b

                _m_chi3b.inc(
                    "bowhead_bi_query_batch_parallel_total",
                    labels={"size": str(len(queries))},
                    help="χ-3: bi_query batch dispatched via gather (BI_BATCH=1).",
                )
            except Exception:  # noqa: S110
                pass

        # χ-3.2 P0 fix (review-r1): inner gather uses only per_batch_sem, **no longer** acquiring
        # `_chi_runtime.get_global_sem()`: outer subagent.py:_bounded_exec already wraps
        # all of _execute_tool in `async with _global_sem`; reacquiring that same semaphore inside
        # deadlocks non-reentrant asyncio.Semaphore (permanent hang when ≥12 outer calls all enter bi_query).
        # Total concurrent cap = outer global=12 × inner per_batch=6 = 72 worst case;
        # Actual DAU is low (~3 Leads × 6 inner = 18 typical concurrent calls).

        async def _one(idx: int, q: str, err: str | None) -> dict[str, Any]:
            if err:
                return {
                    "ok": False,
                    "artifact_id": None,
                    "_ref_artifact_id": None,
                    "query_text": q,
                    "error_kind": err,
                    "error_message": f"sanity check: {err}",
                    "sample_rows": [],
                    "_preflight_warnings": [],
                }
            single_args = {
                "text": q,
                **{k: args[k] for k in ("fast", "limit", "columns", "inline_max_rows", "mode") if k in args},
            }

            async def _attempt() -> dict:
                # Only intra-batch sem (outer subagent gather already holds global_sem).
                # Retry **inside the semaphore**: retaining the slot naturally serializes this query, so retries
                # do not increase concurrency and hit the rate limit again.
                if per_batch_sem is not None:
                    async with per_batch_sem:
                        return await _query_single(single_args, ctx)
                return await _query_single(single_args, ctx)

            last_err: Exception | None = None
            attempts = _bi_throttle_retries() + 1
            for attempt_i in range(attempts):
                try:
                    sub = await _attempt()
                    return _wrap_sub_result(sub, q)
                except Exception as e:  # noqa: PERF203
                    last_err = e
                    msg = scrub(str(e))
                    # Retry only rate limiting. Other datarc errors (unsupported semantics / permissions / missing dimensions)
                    # would repeat the same failure and waste time.
                    if attempt_i == attempts - 1 or not _is_datarc_throttled(msg):
                        break
                    delay = _bi_throttle_backoff_base() * (2**attempt_i)
                    delay += _random.uniform(0, delay * 0.5)  # Jitter: queries rejected in the same batch should not all return simultaneously
                    logger.warning(
                        "bi_query.datarc_throttled attempt=%d/%d delay=%.2fs q=%r",
                        attempt_i + 1,
                        attempts,
                        delay,
                        q[:60],
                    )
                    await _asyncio.sleep(delay)

            _msg = scrub(str(last_err))
            _throttled = _is_datarc_throttled(_msg)
            if _throttled:
                logger.warning("bi_query.datarc_throttled_giving_up attempts=%d q=%r", attempts, q[:60])
            return {
                "ok": False,
                "artifact_id": None,
                "_ref_artifact_id": None,
                "query_text": q,
                # Separate error kind for rate limiting: backend is busy, not this query is wrong. Agent should adjust timing
                # and retry rather than rewrite; datarc_error conflates the two.
                "error_kind": "datarc_rate_limited" if _throttled else "datarc_error",
                "error_message": _msg[:200],  # ④f: batch result dict → LLM (not via execute except)
                "sample_rows": [],
                "_preflight_warnings": [],
            }

        if use_parallel:
            results = list(
                await _asyncio.gather(
                    *[_one(i, q, e) for i, q, e in sanity_errors],
                )
            )
        else:
            # BI_BATCH OFF path: sequential N (LLM still gets batch shape response).
            results = []
            for i, q, e in sanity_errors:
                results.append(await _one(i, q, e))

        return {"results": results}

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="bi_query",
                description=(
                    # bi-hitl-cleanup B2 (2026-05-21): shrink 191→≤80 Chinese characters
                    # (PRD §7.3 character cap). ar2 embedded preflight removed; LLM disambiguation
                    # uses atomic bi_concept_resolve, not this path.
                    "NL → datarc rows. 批量传 `queries: List[str]`, 一次发 N 个并行, "
                    "返 `{results: [{ok, _ref_artifact_id, query_text, sample_rows, _parse, ...}]}`. "
                    "N=1 返 flat backward-compat. 同 turn 内重复 query 自动 cache.\n\n"
                    "**`_parse`** (P13 cut transparency): 每个 result 带 datarc 怎么解析这个 query — "
                    "`{tokens, unknown_words, half_match_values}`. "
                    # bi-answer-correctness A: description has an 80-Chinese-character cap (test_bi_query_lean_p2); handling rules live in
                    # compute.md and result `_preflight_hint` naming sentences; leave only a pointer here.
                    "`unknown_words 不空` = 结果没按它筛选, 看 `_preflight_hint`. "
                    "`half_match_values 不空` = value 半精确, 先 `bi_concept_resolve` 看候选.\n\n"
                    "若 query 含模糊 token (店名/区域别名/derived metric), 先调 "
                    "`bi_concept_resolve` 看候选再决定本 tool 怎么发. 本 tool **不**做"
                    '概念解析, 不再返 `error_kind="needs_clarification"`.'
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 20,
                            "description": (
                                "NL query 列表. 每个独立 metric/dim 组合一个 query string. "
                                '**支持单查询 = `queries=["..."]`**。批查询: 3-5+ 独立 metric × 同 timeframe, '
                                "datarc 并发执行 (BI_BATCH=1 启用 gather). "
                                '例: queries=["今年 库存周转率 同比", "今年 主营业务成本 同比", "今年 平均存货余额 同比"]。'
                                "时间语法原生支持, 1 条 query 直接问: 同比/环比 (自带 delta%)、"
                                '每月/每季度/每周/趋势 (返完整时间序列)。❌ 禁逐期拆 (如 queries=["1月 X","2月 X",...]) '
                                "或查两期自己算同比。\n"
                                "❌ **别写「明细/逐笔/原始记录」** —— datarc 认作行级取数, 拉百万行量级 "
                                "(116 实测 133 秒 / 100 万行)。用户说「明细表」「一个 X 一行」「全部, 不要只给 Top N」时, "
                                "要的是**按 X 分组的聚合**: 写 `2026年各品牌营业额` 即可 "
                                "(同一份数据实测 4 秒 / 38 行, 且本来就是一个品牌一行)。"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "截最大返回行数. 不传返全量.",
                        },
                        "columns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "只保留这些列省 token.",
                        },
                        "inline_max_rows": {
                            "type": "integer",
                            "default": 30,
                            "description": (
                                "inline 给你看的最大行数. 结果里 `rows_shown`=实际给你看的行数, `row_count`=全量行数; "
                                "`rows_shown < row_count` 时别把看到的行数当成总数."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["display", "analyze", "compute"],
                            "description": "(single-value path 时) ask_question 的 mode hint.",
                        },
                    },
                    "required": ["queries"],
                    # λ-F2/F3: remove `text` and `fast`. Internal callers use _normalize_bi_query_args (text→queries=[text]).
                    # `fast` fully removed: backend auto-detect + group_by hard override.
                },
            ),
            handler=_query,
        )
    )

    # bi-workspace-list direction ① (2026-09-03): soft cap for inspection within one turn, not a hard cap;
    # only switch hint variants; server rejects no calls. Value comes from actual iteration budget: Lead
    # max_iterations=12; each workspace inspection takes two iterations (switch+bi_schema), minus one each for list
    # and closing ⇒ ~5 workspaces per turn (live 116 t_619ab802b174: forcing 32 workspaces exhausted the cap).
    _WORKSPACE_TOUR_SOFT_LIMIT = 5

    # ─── tool_list_workspaces（bi-workspace-list，2026-09-02）────────
    # Production s_a233883d468a: Lead is structurally blind to "我有哪些工作区"; accessible lists exist on ctx every turn
    # but appear only in three patches: switch failure / did_you_mean / catalog after skipping picker.
    # Model truthfully answers "我没有工具能列出工作区". This tool provides a direct read-only
    # list entry. Compared live (deepseek-v4-flash × 4 variants × 3 trials):
    # tool-only, unchanged Lead prompt yielded discovery 6/6; prompt injection added no value and cost ~200 tok per turn,
    # so ship only the tool, without a prompt block. Routing discipline goes in result hints (models obeyed in tests,
    # including switching back after inspection); "不要连环重复切换" guards the inspection loops seen in two of four deep runs
    # (hop cap truncates them, but burns the whole turn).
    async def _list_workspaces(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        tool = str(args.get("tool") or "datarc")
        binding = CONNECTOR_CONTEXT_BINDINGS.get(tool)
        if binding is None:
            return {"ok": False, "error": "unsupported_tool", "tool": tool}
        accessible, names = _connector_accessible_roster(ctx, binding)
        if not accessible:
            # Review c1: an empty accessible list is **the answer itself** ("你没有可访问的工作区"),
            # not a fault; error_kind renders it as an expected outcome (EXPECTED_OUTCOME_
            # ERROR_KINDS), without a red error step.
            return {
                "ok": False,
                "error": "no_accessible_workspaces",
                "error_kind": "no_accessible_workspaces",
                # Review r2#1: total/workspaces are structural evidence for this kind (subagent.
                # _is_non_failure_result checks the empty-list shape), not decorative fields.
                "total": 0,
                "workspaces": [],
                "hint": f"当前通道无可访问的{binding.zh_context_noun}（无身份/无账号/通道不支持）。",
            }
        current = str(getattr(ctx, binding.active_name_attr, "") or "")
        if not current:
            # Review c7: name and project ID are separate fields through six hops (bi_availability
            # docstring desync warning). If name is missing, look it up by ID in the list, avoiding the model turning
            # a workspace with an unwired name into "当前未连接任何工作区".
            # Review r2#5/#8: ID attributes use the binding table (no axis mix-up when a second connector arrives);
            # ID absent from list (access revoked/desync) → honestly leave empty; do not report a bare ID as its name.
            active_id = str(getattr(ctx, binding.active_id_attr, "") or "")
            if active_id:
                current = next((n for w, n in zip(accessible, names) if str(w["id"]) == active_id), "")
        # Delegation wording (_LEAD_TOOL_DENY permanently excludes bi_query): inspect capabilities with bi_schema
        # (Lead has it), but fetch data through spawn; hints must not direct Lead to tools it lacks.
        # bi-answer-correctness B2 (2026-09-15, user decision): original "明显属于另一个工作区才切换，不确定就先问"
        # permits silent switches after model judgment, contradicting tool_switch_context description (only explicit user requests, review c4). Unify on asking first.
        base_hint = (
            "这就是完整列表。普通数据问题不要逐个工作区查询——默认在当前北极九章工作区查。"
            "问题看起来属于另一个北极九章工作区时，先用 clarification_ask 问用户要不要切换，"
            "用户同意后再调 tool_switch_context；不要自己切换。"
        )
        if len(names) > _WORKSPACE_TOUR_SOFT_LIMIT:
            # Live 116 evidence (1.2.17, t_619ab802b174): full inspection of 32 workspaces cannot structurally fit
            # one turn; first blindly switch 31 in parallel, then inspect correctly, iter=12 reaches Lead cap and user receives
            # "本次分析未能完成". Scale guidance changes only plan shape, not a hard limit: server rejects nothing;
            # model chooses based on user intent (focus / batch / plan_propose).
            tour_hint = (
                f"注意：工作区较多（{len(names)} 个），不要在本轮逐个盘点——单轮迭代预算装不下，"
                "中途会失败。用户要求盘点全部时：先把名单给用户，建议挑重点或分批，"
                "或调 plan_propose 出分步计划让用户确认；选定范围后再逐个切换"
                f"（一次只发起一个切换调用，并行切换互相覆盖没有意义；切完立即用 {binding.inspect_tool} 盘点，"
                "要取数则 spawn 取数 peer），完成后切回原来的工作区。"
            )
        else:
            tour_hint = (
                "仅当用户明确要求盘点全部时才逐个切换（一次只发起一个切换调用，并行切换互相"
                f"覆盖没有意义；每切换一个立即用 {binding.inspect_tool} 盘点该工作区，要取数则 spawn 取数 peer），"
                "全部完成后切回原来的工作区。"
            )
        if ctx.role != "lead" and ctx.role is not None:
            # Team Agents have no clarification_ask / tool_switch_context / spawn: the Lead hints above would
            # point them at Tools they lack, so they get the list and a neutral routing note.
            return {
                "ok": True,
                "total": len(names),
                "current": current,
                "workspaces": names,
                "hint": ("这就是完整列表。默认在当前北极九章工作区查询；问题看起来属于另一个工作区时，在回答里告诉用户，由用户决定是否切换。"),
            }
        return {
            "ok": True,
            "total": len(names),
            "current": current,
            "workspaces": names,
            "hint": base_hint + tour_hint,
        }

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="tool_list_workspaces",
                description=(
                    "列出当前用户可访问的全部北极九章工作区（数据源），含当前连接的是哪个。"
                    "回答「我有哪些工作区」、或需要切换/盘点工作区而不知道有哪些时使用。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        # Same as tool_switch_context: derive enum from binding table (currently == ["datarc"]).
                        "tool": {"type": "string", "enum": sorted(CONNECTOR_CONTEXT_BINDINGS)},
                    },
                },
            ),
            handler=_list_workspaces,
        )
    )

    # ─── tool_switch_context (batch 2/c6, PRD §3.4 restricted switch primitive) ─────
    # "换华东大区查" → LLM passes only the user's name; ID resolution/authorization stay entirely server-side:
    # matches are confined to this user's accessible set (preserving NEVER LLM-supplied semantics: LLM cannot make
    # server use a workspace outside it). Exact/unique prefix match = auto-switch; substring/ambiguity =
    # always clarification confirmation (prevent "华东" silently switching to "华东大区"); outside the set = reject.
    # Takes effect by gateway-injected callback re-resolving (workspace, member_uid) and updating this ctx;
    # subsequent bi_* calls in this turn immediately use the new context (workspace axis added to cache key);
    # already-dispatched peers (copy.copy(ctx)) are unaffected. Preferences persist at session level (do not overwrite
    # explicit default, bounding accidental/injected persistence damage, review P2).
    def _switch_metric(outcome: str) -> None:
        try:
            from bowhead_core import metrics as _m

            _m.inc(
                "bowhead_tool_switch_context_total",
                labels={"outcome": outcome},
                help="批2 §4.4: switch 原语结果分类 (switched/clarification/refused/failed)",
            )
        except Exception:  # noqa: S110
            pass

    async def _switch_context(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        tool = str(args.get("tool") or "datarc")
        # T2#9: early-return gate uses table lookup; binding miss (non-datarc input) is byte-equivalent to old `tool != "datarc"`
        # hard rejection (golden pin). In-block ctx access/wording uses binding descriptors.
        binding = CONNECTOR_CONTEXT_BINDINGS.get(tool)
        if binding is None:
            return {"ok": False, "error": "unsupported_tool", "tool": tool}
        query = str(args.get("name") or "").strip()
        accessible, names = _connector_accessible_roster(ctx, binding)
        if not query:
            return {"ok": False, "error": "empty_name", "accessible": names}
        if not accessible:
            return {
                "ok": False,
                "error": "no_accessible_workspaces",
                "hint": "当前通道无可切换的工作区（无身份/无账号/通道不支持）。",
            }
        switcher = getattr(ctx, binding.switcher_attr, None)
        if switcher is None:
            return {
                "ok": False,
                "error": "channel_not_supported",
                # Architecture review: old hint points to "设置→工具→北极九章" (removed); now accurately points to web chat.
                "hint": f"本通道暂不支持切换{binding.zh_context_noun}，请到网页端会话中说「换 XX」切换。",
            }

        def _match(tier: str) -> list[dict]:
            if tier == "exact":
                return [w for w in accessible if str(w.get("name") or "") == query or str(w["id"]) == query]
            if tier == "prefix":
                return [w for w in accessible if str(w.get("name") or "").startswith(query)]
            return [w for w in accessible if query in str(w.get("name") or "")]

        chosen: dict | None = None
        tier_hit = ""
        for tier in ("exact", "prefix"):
            hits = _match(tier)
            if len(hits) == 1:
                chosen, tier_hit = hits[0], tier
                break
            if len(hits) > 1:
                break  # Ambiguity → clarification
        if chosen is None:
            sub_hits = _match("substring")
            if not sub_hits:
                _switch_metric("refused")
                # feedback-0713 #2: hard misses often come from typos (production: "商朝零售" vs
                # "商超零售场景", one-character difference, all three lexical match levels miss). Use edit similarity for
                # high-confidence did_you_mean suggestions, letting the model confirm with the user (conservative: never switch
                # for users unprompted). error_kind=not_accessible renders a missing name as an expected outcome
                # (subagent.EXPECTED_OUTCOME_ERROR_KINDS), without a red error step.
                did_you_mean = difflib.get_close_matches(query, names, n=3, cutoff=0.5)
                hint = "该工作区不在你的可访问列表内。"
                if did_you_mean:
                    hint += f"用户可能想说：{'、'.join(did_you_mean)}——请向用户确认后再切换，不要擅自替用户选择。"
                return {
                    "ok": False,
                    "error": "not_accessible",
                    "error_kind": "not_accessible",
                    "hint": hint,
                    "did_you_mean": did_you_mean,
                    "accessible": names,
                }
            # Substring/ambiguity: always clarification confirmation; never silently auto-commit.
            # T2#9 deliberately does not parameterize ③: _switch_clar_emitted same-turn idempotency flag stays global
            # per ctx (single-tenant equivalent; change to per-connector when multiple connectors arrive).
            if getattr(ctx, "_switch_clar_emitted", False):
                # Same-turn idempotency: do not show repeat cards (prevent prompt-injection-driven spam).
                # feedback-0713 F4: error_kind renders this as an expected outcome
                # (subagent.EXPECTED_OUTCOME_ERROR_KINDS), without a red error step.
                return {
                    "ok": False,
                    "error": "pending_user_confirmation",
                    "error_kind": "pending_user_confirmation",
                    "accessible": [str(w.get("name") or w["id"]) for w in sub_hits],
                }
            object.__setattr__(ctx, "_switch_clar_emitted", True)
            _switch_metric("clarification")
            return {
                "ok": False,
                "error_kind": "needs_clarification",
                "clarification": {
                    "kind": "tool_context_select",
                    "tool": binding.connector,
                    "prompt": f"「{query}」匹配到 {len(sub_hits)} 个工作区，请确认用哪个：",
                    "options": [{"value": str(w["id"]), "label": str(w.get("name") or w["id"])} for w in sub_hits],
                    "remember_option": True,
                },
                "rows": [],
            }

        new_id = str(chosen["id"])
        new_name = str(chosen.get("name") or new_id)
        # tool-context-framework v1（docs/tool-context-framework.md §5）：remember=true
        # → closure writes explicit permanent default (solves locked selection: settings panel removed, chat is the only entry).
        # Omitted false = current session scope. ID matching still stays in the accessible set above (NEVER LLM-supplied
        # invariant preserved); worst-case injection switches to another already-accessible workspace, reversible with one sentence.
        # Review P1-a: registry has no schema validation; qwen models often send booleans as strings;
        # accept only True / "true" (case-insensitive); all others (including "false") are False, preventing
        # explicit user refusal to remember from being inverted into a permanent default by bool("false")=True.
        _rem_raw = args.get("remember")
        remember = _rem_raw is True or (isinstance(_rem_raw, str) and _rem_raw.strip().lower() == "true")
        # ── Consecutive-switch circuit breaker (commit-point check, review r3#1/#2 refactor) ─
        # Criterion: SWITCH_STREAK_PAUSE_AT-1 consecutive **committed** switches with no intervening
        # tools (execute chokepoint resets the container for other tools). Commit-point placement prevents
        # miss/did_you_mean/ambiguity clarification/idempotent confirmation cards from being preempted by this gate, and ensures hint
        # claims of N consecutive switches are always true (N=actual commits). Check + reserve are synchronously atomic
        # (no await between) ⇒ parallel gather blind-switch bursts (the motivating incident)
        # deterministically commit the first PAUSE_AT-1 and pause the rest. In mixed batches, other-tool resets
        # and switch reservations have uncertain gather ordering; best effort for that shape.
        _streak_box = getattr(ctx, "_switch_streak_box", None)
        if _streak_box is None:
            _streak_box = {"n": 0}
            object.__setattr__(ctx, "_switch_streak_box", _streak_box)
        if _streak_box["n"] >= SWITCH_STREAK_PAUSE_AT - 1:
            _switch_metric("streak_paused")
            return {
                "ok": False,
                "error": "switch_streak_paused",
                "error_kind": "switch_streak_paused",
                # Structural evidence (subagent._is_non_failure_result checks it): the paused attempt is consecutive switch
                # number committed-count+1; carry the actual value.
                "switch_streak": _streak_box["n"] + 1,
                "hint": (
                    f"你已连续切换 {_streak_box['n']} 次而没有读取任何工作区——连环切换互相覆盖，没有意义。"
                    f"巡检要切一个读一个：先用 {binding.inspect_tool} 盘点当前工作区，再切下一个；"
                    "全部完成后切回最初的工作区。"
                ),
            }
        _streak_box["n"] += 1  # Reserve a slot; roll back on switcher failure, so failed switches do not consume inspection quota
        try:
            new_member, _persisted = await switcher(new_id, new_name, remember=remember)
        except Exception as e:
            _streak_box["n"] -= 1
            logger.warning(
                "tool_switch_context.commit_failed user=%s to=%s err=%s",
                ctx.user_id,
                new_id,
                scrub(str(e)),
            )
            _switch_metric("failed")
            return {"ok": False, "error": "member_resolve_failed", "hint": "身份解析失败，工作区未切换，请稍后再试。"}
        object.__setattr__(ctx, binding.active_id_attr, new_id)
        object.__setattr__(ctx, binding.active_name_attr, new_name)
        # T2#9 deliberately does not parameterize ①: member_uid is the identity axis, returned by the datarc switcher closure
        # contract `(id, name, *, remember) -> (member_uid, persisted)`. Abstracting identity
        # belongs to T2#10 (calibrate against real instances); retain residual datarc semantics here.
        object.__setattr__(ctx, "member_uid", new_member)
        logger.info(
            "tool_switch_context.switched user=%s turn=%s to=%s(%s) tier=%s",
            ctx.user_id,
            ctx.turn_id,
            new_id,
            new_name,
            tier_hit,
        )
        _switch_metric("switched")
        return {
            "ok": True,
            "switched": {"id": new_id, "name": new_name},
            "effective": "immediate",
            # Old wording points to "设置→工具→北极九章" (panel removed, batch 2 Changelog(5)); in-chat
            # remember is the only entry for setting defaults. Wording follows _persisted truthfully (review P1-c: never claim
            # "已设为默认" when persistence is skipped/fails); terminology uses full zh_context_noun (P0 gate).
            "note": (
                f"已设为你的默认{binding.zh_context_noun}（跨会话生效）。"
                if remember and _persisted
                else (
                    "已切换（本会话生效），但默认保存失败——稍后可以再说一次「记住」。"
                    if remember
                    else f"本会话内生效；要长期使用这个{binding.zh_context_noun}，请说「记住」或「以后都用它」。"
                )
            ),
        }

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="tool_switch_context",
                # T2#9 deliberately does not parameterize ②: description stays literal (single-tenant equivalent);
                # change to a generic template when the second binding arrives (e.g. switch connector context).
                # bi-workspace-list: original "仅当用户明确要求更换工作区时使用" blocks inspection requests
                # such as "每个工作区都看看" (not naming each workspace ≠ no authorization to switch).
                # Review c4: inspection branch must retain the explicit-request constraint; removing it lets the model
                # decide a question needs another workspace and silently switch session context.
                description=(
                    "切换当前北极九章工作区（数据源）。当用户明确要求更换工作区、或明确要求逐个查看/盘点多个工作区时使用；name 传目标工作区名字（不知道有哪些时先用 tool_list_workspaces 查看）。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        # Derive enum from binding table (currently == ["datarc"], identical bytes).
                        "tool": {"type": "string", "enum": sorted(CONNECTOR_CONTEXT_BINDINGS)},
                        "name": {"type": "string"},
                        "remember": {
                            "type": "boolean",
                            "description": "true=设为跨会话默认。仅当用户明确说「记住/以后都用它/设为默认」时才传；默认 false=仅本会话。",
                        },
                    },
                    "required": ["name"],
                },
            ),
            handler=_switch_context,
        )
    )

    # ─── BI write operations (bi_create/update/delete_measures + bi_update_dimensions) removed ───
    # 2026-07-21: remove this whole four-tool family for draft writes → admin approval → system execution.
    # They never had an executable endpoint: datarc client (bowhead_bi_bridge/datarc_client.py)
    # only has list_measures / list_dimensions, **no write methods**, so even after admin approval,
    # approval_executor only calls an always-raising stub (M3 real datarc write API never implemented).
    # Production 116 approvals table has had 0 rows since launch; this flow was never actually used.
    # Retain approval infrastructure (ApprovalGate / ApprovalStore / approval_executor / notification center);
    # add to approval_gate.SUPPORTED_NATIVE_WRITE_TOOLS when genuinely executable write tools exist.

    # ─── kb_search (V7 KB sprint Day 16) ─────────────────────
    async def _kb_search(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        """KB 4-stage retrieval pipeline. ctx.kb_retriever None → soft empty (KB unconfigured)."""
        if ctx.kb_retriever is None:
            # KB runtime is built only from team model settings. Most common cause now is missing
            # _peer_ctx wire (Bug 3 fix at turn_runner.py kb_retriever= wire).
            return {
                "chunks": [],
                "total_candidates": 0,
                "rerank_used": False,
                "latency_ms": 0,
                "note": "KB retriever not wired into tool context (check turn_runner _peer_ctx + app.state.kb_retriever)",
            }
        from bowhead_core.kb.permission import ScopeContext, drop_chunks_of_deleted_files, resolve_extra_personal_workspaces

        # V7 kb-agentic-fix: use ctx.kb_role (turn_runner forwards from TurnRequest.kb_role).
        # Defend against invalid roles: cast to a legal enum or fall back to "user".
        _role = ctx.kb_role if ctx.kb_role in ("user", "ws_admin", "team_admin") else "user"
        # 0.40.4 Bug 8 step a: resolve user's personal-scope workspaces so
        # KbStore.search can query the right collections. Shared with the page
        # deep search route (PRD FR6.1) — best-effort, [] on failure.
        extra_personal_workspaces = await resolve_extra_personal_workspaces(ctx.kb_files_store, ctx.user_id)
        scope_ctx = ScopeContext(
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            # Batch 1/c5: fail-closed — a space id never stands in for an team id (the V1-mock
            # 1-1 era fallback re-smeared what c4 cleaned; team-less ctx ⇒ no team-scope hits).
            team_id=ctx.team_id,
            role=_role,  # type: ignore[arg-type]
            extra_personal_workspaces=extra_personal_workspaces,
        )
        scope_filter = await _kb_search_scope_filter_for_grants(ctx)
        if scope_filter == []:
            return {
                "chunks": [],
                "total_candidates": 0,
                "rerank_used": False,
                "latency_ms": 0,
                "hint": "No readable KB scopes are granted for this workspace/team.",
            }
        from bowhead_core.kb.generations import use_guard_enabled
        from bowhead_core.kb.retrieval_policy import DEFAULT_TOP_K, MAX_TOP_K

        top_k = args.get("top_k", getattr(ctx.kb_retriever, "default_top_k", DEFAULT_TOP_K))
        if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
            return {"ok": False, "error": f"top_k must be an integer from 1 to {MAX_TOP_K}"}

        async def authorize(file):
            return bool(ctx.kb_file_authorizer and await ctx.kb_file_authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id))

        guard_args = {"authorize_file": authorize} if use_guard_enabled() else {}
        result = await ctx.kb_retriever.invoke(
            query=str(args.get("query", "")),
            scope_ctx=scope_ctx,
            top_k=top_k,
            scope_filter=scope_filter,
            **guard_args,
        )
        result_chunks = await drop_chunks_of_deleted_files(ctx.kb_files_store, result.chunks)
        result_chunks = await _filter_kb_search_chunks_by_file_grants(ctx, result_chunks)
        if use_guard_enabled():
            for chunk in result_chunks:
                key = f"{chunk.file_id}:{chunk.generation_id}"
                prior = ctx.kb_source_uses.get(key, {})
                ctx.kb_source_uses[key] = {
                    "file_id": chunk.file_id,
                    "generation_id": chunk.generation_id,
                    "check_state": chunk.source_warning or prior.get("check_state", "verified_binding"),
                }
        # 0.40.3 Bug 6: actionable hint for empty / weak results (per Anthropic
        # agent tools guidance — structured hints in output > prompt rules).
        out_chunks = [
            {
                "chunk_id": c.chunk_id,
                "content": c.content,
                "file_id": c.file_id,
                "file_name": c.file_name,
                "page": c.page,
                "section_title": c.section_title,
                "role": c.role,
                "score": c.score,
                "scope": c.scope,
                "generation_id": getattr(c, "generation_id", None),
                "source_warning": getattr(c, "source_warning", None),
            }
            for c in result_chunks
        ]
        if not out_chunks:
            hint = (
                "No authorized chunks matched this query; this does not prove the knowledge base is empty. "
                "For a bounded fact lookup, use an initial search and at most two meaningful reformulation rounds. "
                "Stop earlier if no useful reformulation remains. Do not repeat queries or use unrelated terms. "
                "Use kb_list_tables only if the requested evidence may be structured. "
                "If no supporting evidence is found, state that limitation without inventing citations."
            )
        elif all(c["score"] < 0.3 for c in out_chunks):
            hint = (
                "All results scored low (<0.3) — semantic match weak. Consider "
                "reformulating the query with more specific entities/terms before "
                "citing these chunks."
            )
        else:
            hint = None  # results look good, no extra steering needed
        out = {
            "chunks": out_chunks,
            "total_candidates": result.total_candidates,
            "rerank_used": result.rerank_used,
            "latency_ms": result.latency_ms,
        }
        if hint:
            out["hint"] = hint
        if result.partial:
            # Some retrieval scope timed out/failed and was skipped: cannot infer that the KB has no matching content.
            out["partial"] = True
            out["note"] = KB_SEARCH_PARTIAL_NOTE
        return out

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="kb_search",
                description=(
                    "Semantic search across ALL knowledge base content (chunks from PDFs, "
                    "Excel/XLS, contracts, receipts, ledgers, reports, glossaries, SOPs — "
                    "every uploaded document, fully indexed).\n\n"
                    "Use this for ANY question about KB content: 'find/search/look for / "
                    "is there / what does X document say about Y'.\n\n"
                    "Use kb_list_tables INSTEAD only when user explicitly asks "
                    "'what schemas/tables/structured data do I have' (metadata-only question).\n\n"
                    "Returns top-K chunks with chunk_id, content, file_name, page, section_title, "
                    "and score. Cite a chunk in your narrative as [ref:N], where N is its number "
                    "in the doc list of <available_refs>."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索查询, 自然语言"},
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "description": "Optional result count. Omit to use the Team knowledge-search default (15 unless configured).",
                        },
                    },
                    "required": ["query"],
                },
            ),
            handler=_kb_search,
        )
    )

    # ─── kb_list_tables (kb-knowledge-tables sprint v1.0) ─────────
    async def _kb_list_tables(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        """列当前 workspace 的全部文档表 (KbDocType).

        用户问 "我有哪些文档表 / 数据表" 时调. 返 [{doc_type_id, name, fields, record_count}].
        0.40.3 Bug 6: include `hint` field in response to steer agent toward kb_search
        when tables are empty / not matching — per Anthropic agent tools guidance,
        actionable hints in tool output > prompt rules.
        """
        del args  # workspace_id comes from ctx; reject LLM-supplied ws (prevent cross-ws leaks).
        store = ctx.kb_doc_type_store
        if store is None or not ctx.workspace_id:
            return {
                "items": [],
                "hint": "kb_doc_type_store unavailable — for any KB content question, call kb_search.",
            }
        items = await store.list_for_ws(ctx.workspace_id)
        # kb-matrix M4 (decision 13): ①exclude seed/saved templates (column-group templates, zero-data schemas)
        # from table lists; chat queries would always be empty and waste LLM calls. Retain graduated snapshots with rows.
        # ②count reports true size by cardinality; many types previously always sent record_count=0, making LLM
        # skip apparently empty tables (also fixes latent LIVE Sprint-25 defect + main graduated-table path). Keep key
        # record_count (BC), and also expose kind to distinguish types.
        from bowhead_core.kb.doc_type_store import derive_doc_type_kind as _dt_kind

        out_items = [
            {
                "doc_type_id": d.doc_type_id,
                "name": d.name,
                "description": d.description,
                "record_count": (d.row_count if d.cardinality == "many" else d.record_count),
                "kind": _dt_kind(d),
                "fields": [{"name": f.name, "type": f.type, "description": f.description} for f in d.fields],
            }
            for d in items
            if _dt_kind(d) not in ("seed", "template")
        ]
        # 0.40.3 Bug 6: actionable hint based on output shape.
        if not out_items:
            hint = (
                "No structured tables in this workspace. This does NOT mean the KB is empty — "
                "chunks (unextracted documents) may still hold the answer. "
                "If user asked about content, call kb_search next."
            )
        else:
            hint = (
                "These are the structured tables only. The chunks layer (full KB content) "
                "may have additional relevant data. If user asked a content question, "
                "ALSO call kb_search alongside kb_query_records to avoid missing chunks."
            )
        return {"items": out_items, "hint": hint}

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="kb_list_tables",
                description=(
                    "List the structured-extracted document tables in the workspace "
                    "(metadata-only: doc_type_id, name, fields, record_count). "
                    "These tables are a SUBSET of the KB — only documents that have been "
                    "run through structured extraction become tables; everything else lives "
                    "in chunks (searchable via kb_search).\n\n"
                    "Use this ONLY when the user asks 'what tables/schemas do I have', "
                    "OR as a precursor to kb_query_records when you already know the table "
                    "exists. Do NOT use this to decide whether the KB has data for a content "
                    "question — chunks may have it even if no table does. For content "
                    "questions, call kb_search.\n\n"
                    "Output: {items: [...], hint: <next-action guidance>}. The hint field "
                    "tells you whether to fall through to kb_search."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_kb_list_tables,
        ),
    )

    # ─── kb_query_records (kb-knowledge-tables sprint v1.0 + kb-per-row-extract Sprint 25) ──
    async def _kb_query_records(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        """SQL 查文档表的 records (按字段 filter / sort / limit).

        Sprint 25 NEW: backend dispatches by doc_type.cardinality:
        - cardinality=one (合同/评估/税务/补助) → kb_extracted_records (file-level)
        - cardinality=many (明细账/总账/发生额表) → kb_extracted_rows (row-level)
        Tool接口 unchanged — LLM 不需要知道 cardinality 概念.

        NEW args (Sprint 25):
        - top_level_filters (optional): {row_seq: N | row_seq_range: [lo,hi] | page: N}
          only for cardinality=many tables
        - aggregate (optional): {field, op: SUM/COUNT/AVG/MIN/MAX, group_by: [...]}
          only for cardinality=many tables
        - offset (optional): server-side pagination (default 0)

        Return shape (Sprint 25 BC compat per P0-TD-ENG-7):
        - `records` key kept (Sprint 1+2 consumers)
        - `rows` key NEW (Sprint 25 row mode)
        - `mode` field: 'rows' | 'aggregate'
        - `aggregate_result` (only when aggregate kwarg passed)
        """
        rec_store = ctx.kb_extracted_record_store
        rows_store = ctx.kb_extracted_rows_store
        dt_store = ctx.kb_doc_type_store
        # Need at least one of rec_store / rows_store wired + workspace_id present
        if (rec_store is None and rows_store is None) or not ctx.workspace_id:
            return {"records": [], "note": "kb stores 未配置 或 workspace_id 缺"}
        doc_type_id = str(args.get("doc_type_id", "") or "")
        if not doc_type_id:
            return {"records": [], "error": "doc_type_id required"}

        # Resolve cardinality (Sprint 25 dispatch)
        cardinality = "one"  # default
        if dt_store is not None:
            try:
                doc_type = await dt_store.get(doc_type_id)
                if doc_type is not None:
                    cardinality = doc_type.cardinality
            except Exception:  # noqa: S110
                pass

        # Sprint 25: cardinality=many → use kb_extracted_rows
        if cardinality == "many" and rows_store is not None:
            # Aggregate path
            if args.get("aggregate"):
                agg = args["aggregate"]
                agg_field = str(agg.get("field", ""))
                agg_op = str(agg.get("op", "COUNT")).upper()
                # kb-matrix M4 FR-44 (honest aggregation) type gate:
                # SUM/AVG only currency/number (string/.value CAST→0.0 is non-NULL and gets counted as zero,
                # yielding confidently wrong numbers); MIN/MAX also allow date (graduated data is ISO-normalized, compare lexically;
                # wave-1 review P1: CAST turns '2026-04-12' into meaningless 2026.0, while
                # "最早签约日期" is a core finance-persona question); categorical columns use COUNT/GROUP BY.
                field_def = None
                if doc_type is not None:
                    field_def = next((f for f in doc_type.fields if f.name == agg_field), None)
                # Unknown-field gate (review P3-6: ghost fields absent from schema, with keys lingering in old
                # rows after renaming; block CAST-0.0 from being endorsed as included by coverage).
                if agg_op != "COUNT" and field_def is None and doc_type is not None and doc_type.fields:
                    return {
                        "records": [],
                        "error": f"aggregate_field_unknown: 「{agg_field}」不在此表的字段列表中——先用 kb_list_tables 核对字段名。",
                    }
                _numeric_types = ("currency", "number")
                if field_def is not None:
                    ftype = field_def.type
                    if agg_op in ("SUM", "AVG") and ftype not in _numeric_types:
                        if ftype == "date":
                            msg = f"「{agg_field}」是日期列——求和/平均无意义；要「最早/最晚」用 MIN/MAX，要分布用筛选/排序。"
                        elif ftype == "multi_select":
                            msg = f"「{agg_field}」是多选列——暂不支持聚合；用 where 过滤「含某选项」或直接列举行。"
                        elif ftype in ("select", "boolean"):
                            msg = f"「{agg_field}」是分类列——用 op=COUNT + group_by 数分布，不能求和/平均。"
                        else:
                            msg = f"「{agg_field}」是 {ftype} 列——数值聚合仅适用于金额/数字列；文本列请改用筛选/列举。"
                        return {"records": [], "error": f"aggregate_field_not_numeric: {msg}"}
                    if agg_op in ("MIN", "MAX") and ftype not in (*_numeric_types, "date"):
                        return {
                            "records": [],
                            "error": f"aggregate_field_not_numeric: 「{agg_field}」是 {ftype} 列——MIN/MAX 仅适用于金额/数字/日期列；分类列用 COUNT+group_by，文本列用筛选/列举。",
                        }
                raw_compare = bool(agg_op in ("MIN", "MAX") and field_def is not None and field_def.type == "date")
                try:
                    results = await rows_store.aggregate(
                        workspace_id=ctx.workspace_id,
                        doc_type_id=doc_type_id,
                        field=agg_field,
                        op=agg_op,
                        group_by=list(agg.get("group_by") or []),
                        where=list(args.get("where") or []),
                        top_level_filters=args.get("top_level_filters") or None,
                        raw_compare=raw_compare,
                    )
                except ValueError as e:
                    return {"records": [], "error": f"invalid aggregate: {e}"}
                out: dict[str, Any] = {
                    "mode": "aggregate",
                    "aggregate_result": [{"group_keys": r.group_keys, "value": r.value} for r in results],
                    "count": len(results),
                    "doc_type_id": doc_type_id,
                    # BC compat: empty records list so old consumers don't crash
                    "records": [],
                }
                # kb-matrix M4 FR-44: numeric aggregation always carries **coverage** (included/total + excluded examples,
                # making residual-guard rejections such as "$300/month/面议" explicit rather than silent) +
                # **unverified-unit caveat** (bare numbers without declared units may mix implicit currencies/units;
                # unit declaration is M5; never provide confidently wrong numbers). caveat/unit_verified are DB-independent,
                # outside coverage try (review P3-1: coverage failure must not silently discard the honesty layer).
                if agg_op in ("SUM", "AVG", "MIN", "MAX"):
                    notes: list[str] = []
                    if field_def is not None and field_def.type in _numeric_types:
                        unit_verified = bool(field_def.dimension)
                        out["unit_verified"] = unit_verified
                        if not unit_verified:
                            notes.append(
                                f"「{agg_field}」列未声明单位：若各文档的币种/单位不一致，此聚合结果不可靠——"
                                "回答时必须提示用户（例如「金额单位未声明，若各文档币种不一，此和仅供参考」）。"
                            )
                    if raw_compare and getattr(doc_type, "graduated_mode", None) != "snapshot":
                        # Legacy (nongraduated) many-type dates lack ISO normalization; lexical order is only best effort
                        notes.append("此表日期格式未统一归一，最早/最晚按文本顺序比较，结果仅供参考——回答时须提示。")
                    try:
                        coverage = await rows_store.aggregate_coverage(
                            workspace_id=ctx.workspace_id,
                            doc_type_id=doc_type_id,
                            field=agg_field,
                            where=list(args.get("where") or []),
                            top_level_filters=args.get("top_level_filters") or None,
                            numeric=not raw_compare,
                        )
                        out["coverage"] = coverage
                        if coverage["included"] < coverage["total"]:
                            excluded = coverage["total"] - coverage["included"]
                            samples = "、".join(coverage["excluded_samples"][:5])
                            notes.append(
                                f"覆盖 {coverage['included']}/{coverage['total']} 行（{excluded} 行值无法计入"
                                + (f"，例如：{samples}" if samples else "")
                                + "）——回答时必须如实说明覆盖范围。"
                            )
                    except Exception:
                        logger.warning("kb aggregate coverage failed dt=%s", doc_type_id, exc_info=True)
                        notes.append("覆盖信息不可用——回答时说明聚合覆盖范围未知。")
                    if notes:
                        out["note"] = " ".join(notes)
                return out
            # Rows path
            try:
                rows = await rows_store.query_rows(
                    workspace_id=ctx.workspace_id,
                    doc_type_id=doc_type_id,
                    where=list(args.get("where") or []),
                    top_level_filters=args.get("top_level_filters") or None,
                    sort_by=args.get("sort_by") or None,
                    sort_dir=str(args.get("sort_dir", "ASC")),
                    limit=int(args.get("limit", 50)),
                    offset=int(args.get("offset", 0)),
                    cast_numeric=list(args.get("cast_numeric") or []),
                )
            except ValueError as e:
                return {"records": [], "error": f"invalid query: {e}"}
            rows_dict = [r.to_dict() for r in rows]
            # Sprint 26 citation registry: register each row so LLM can [cite:N]
            # alongside chunks. artifact_id=row_id (dedup via registry).
            # label = "{doc_type_name}第{row_seq}行 (key=value)" for FE chip tooltip.
            try:
                from bowhead_core.citations import ensure_registry

                # ctx is ToolExecutionContext (turn-scoped) — registry attaches here
                registry = ensure_registry(ctx)
                doc_type_name = doc_type.name if doc_type else "row"
                # kb-matrix M4 (review P2-5): graduated tables always have row_seq=1 per file; repeated "第1行" ×N
                # chips are indistinguishable. Graduated table labels use **filenames** (natural keys).
                fname_by_file: dict[str, str] = {}
                if getattr(doc_type, "graduated_mode", None) == "snapshot" and ctx.kb_files_store is not None:
                    try:
                        for fid in {r.file_id for r in rows}:
                            fdto = await ctx.kb_files_store.get(fid)
                            if fdto is not None and getattr(fdto, "filename", None):
                                fname_by_file[fid] = str(fdto.filename)[:40]
                    except Exception:
                        fname_by_file = {}
                for row in rows:
                    # Build short label from required-ish fields for chip tooltip
                    first_field_summary = ""
                    if isinstance(row.fields, dict):
                        for fname, fval in row.fields.items():
                            if isinstance(fval, list) and fval:
                                v = fval[0].get("normalized", {})
                                if isinstance(v, dict):
                                    # kb-matrix M4 (decision 15): extend labels to categorical values;
                                    # missing choice/flag degrades all-categorical tables to bare "第N行" labels.
                                    val = v.get("amount") or v.get("value") or v.get("choice")
                                    if val is None and isinstance(v.get("flag"), bool):
                                        val = "是" if v["flag"] else "否"
                                    if val:
                                        first_field_summary = f" ({fname}={val})"
                                        break
                    row_key = fname_by_file.get(row.file_id) or f"第{row.row_seq}行"
                    label = (
                        f"{doc_type_name}·{row_key}{first_field_summary}"
                        if row.file_id in fname_by_file
                        else f"{doc_type_name}第{row.row_seq}行{first_field_summary}"
                    )
                    # artifact_id = row_id (full uuid). dedup via registry.
                    registry.register(row.row_id, label=label, kind="kb_row")
            except Exception as _exc:
                logger.warning("kb_row citation registry failed: %s", _exc)
            return {
                "mode": "rows",
                "rows": rows_dict,
                # BC: also populate `records` for Sprint 1+2 consumers
                "records": rows_dict,
                "count": len(rows),
                "doc_type_id": doc_type_id,
                "cardinality": "many",
            }

        # cardinality=one (or no rows_store) → existing Sprint 1+2 path
        if rec_store is None:
            return {"records": [], "note": "kb_extracted_record_store 未配置 (cardinality=one path)"}
        try:
            records = await rec_store.query_by_field(
                workspace_id=ctx.workspace_id,
                doc_type_id=doc_type_id,
                where=list(args.get("where") or []),
                sort_by=args.get("sort_by") or None,
                sort_dir=str(args.get("sort_dir", "DESC")),
                limit=int(args.get("limit", 50)),
                cast_numeric=list(args.get("cast_numeric") or []),
            )
        except ValueError as e:
            return {"records": [], "error": f"invalid query: {e}"}
        return {
            "mode": "rows",
            "records": [r.to_dict() for r in records],
            "count": len(records),
            "doc_type_id": doc_type_id,
            "cardinality": "one",
        }

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="kb_query_records",
                description=(
                    "对文档表里的 records 走 SQL 查询 (filter / sort / aggregate / limit). "
                    "用户问 '金额最大的 5 个合同 / 凭证号 0001 借方 / 2022Q3 总借方' 时调. "
                    "**先调 kb_list_tables** 拿 doc_type_id, 然后传 doc_type_id + where/sort 调本 tool. "
                    "Backend 按 doc_type cardinality 自动 dispatch (一行/多行透明). "
                    "**Citation**: 返回的每条 row/record 都注册到 turn citation registry, "
                    "narrative 中引用某行用 `[cite:N]`, N 是 row 在 registry 中的 seq. "
                    "注意: 聚合 (aggregate) 结果不带 citation——需要引用出处时改用行查询.\n\n"
                    "**聚合诚实 (硬规则)**: 数值聚合 (SUM/AVG/MIN/MAX) 的返回带 `coverage` "
                    "({included, total, excluded_samples}) 与可选 `note`——回答用户时**必须**如实"
                    "转述: ①included < total 时明说覆盖范围与被排除的值 (它们无法作为数值计入, "
                    "如 '$300/month'/'面议'), 绝不把部分和当全量和报; ②`unit_verified: false` 时"
                    "明说金额单位未声明、若各文档币种/单位不一致此和不可靠. 永远不给确信的错数. "
                    "数值聚合仅限金额/数字列 (string/select/boolean 列会被拒——分类列用 "
                    "op=COUNT + group_by 数分布, 文本列用筛选/列举)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "doc_type_id": {
                            "type": "string",
                            "description": "文档表 ID (走 kb_list_tables 拿)",
                        },
                        "where": {
                            "type": "array",
                            "description": ("过滤条件 list, AND 拼. 每条 {field: 字段名, op: =/!=/>/>=/</<=/LIKE, value: 值}"),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "op": {"type": "string"},
                                    "value": {},
                                },
                            },
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "排序字段名",
                        },
                        "sort_dir": {
                            "type": "string",
                            "enum": ["ASC", "DESC"],
                            "default": "DESC",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "cast_numeric": {
                            "type": "array",
                            "description": "需 CAST AS REAL 的字段名 (currency / number 类型 sort/比 必传)",
                            "items": {"type": "string"},
                        },
                        # Sprint 25 kb-per-row-extract NEW
                        "top_level_filters": {
                            "type": "object",
                            "description": (
                                "Sprint 25 row-level filter on SQL 顶层列 (不走 json_extract). "
                                "仅 cardinality=many doc_type (明细账/总账/发生额表) 支持. "
                                "row_seq: 1-indexed in source file. page: PDF page number."
                            ),
                            "properties": {
                                "row_seq": {"type": "integer", "minimum": 1},
                                "row_seq_range": {
                                    "type": "array",
                                    "items": {"type": "integer", "minimum": 1},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "page": {"type": "integer", "minimum": 1},
                            },
                        },
                        "aggregate": {
                            "type": "object",
                            "description": (
                                "Sprint 25 SQL aggregate. 仅 cardinality=many 支持. "
                                "返 aggregate_result list with group_keys + value. "
                                "示例: '2022Q3 银行存款总借方' → {field: 借方金额, op: SUM, "
                                "group_by: [会计期间, 科目编码]}."
                            ),
                            "properties": {
                                "field": {"type": "string"},
                                "op": {
                                    "type": "string",
                                    "enum": ["SUM", "COUNT", "AVG", "MIN", "MAX"],
                                },
                                "group_by": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["field", "op"],
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Server-side pagination offset (Sprint 25, 0-indexed)",
                            "default": 0,
                            "minimum": 0,
                        },
                    },
                    "required": ["doc_type_id"],
                },
            ),
            handler=_kb_query_records,
        ),
    )

    # ─── kb_get_record (kb-knowledge-tables sprint v1.0) ─────────
    async def _kb_get_record(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        """单条 record 详情 (含 fields_json + citation snippets + verify_status)."""
        store = ctx.kb_extracted_record_store
        if store is None:
            return {"record": None, "note": "kb_extracted_record_store 未配置"}
        record_id = str(args.get("record_id", "") or "")
        if not record_id:
            return {"record": None, "error": "record_id required"}
        # P0-R1: prevent cross-ws leaks; fail closed by rejecting ctx without workspace_id.
        if not ctx.workspace_id:
            return {"record": None, "error": "workspace_id missing in context"}
        record = await store.get(record_id)
        if record is None or record.workspace_id != ctx.workspace_id:
            # Do not disclose whether record exists (defense-in-depth); consistently return not-found.
            return {"record": None, "note": "记录不存在或已删"}
        return {"record": record.to_dict()}

    reg.register(
        ToolSpec(
            schema=ToolSchema(
                name="kb_get_record",
                description=(
                    "查单条 record 的详情 (含 fields_json + 每字段 source_snippet 引用 + 页码). 用户问 '看一下 X 合同的详情 / 第 3 条 record' 时调."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "record_id": {"type": "string"},
                    },
                    "required": ["record_id"],
                },
            ),
            handler=_kb_get_record,
        ),
    )

    # ─── Sprint omicron Phase C2 legacy: still-registered legacy_alias names hidden from LLM ─────
    # `visible_to_llm=False` keeps handlers callable (historical alias backward compatibility), but excludes them
    # from `to_schemas()` output, so LLM cannot see them.
    #
    # 2026-09-05 (tool-inventory sprint): original table's 10 BI tools
    # （bi_attribute / bi_breakdown / bi_explain / bi_get_card / bi_get_cards /
    # bi_plan_query / bi_call_llm_async / bi_get_llm_result / bi_fetch_account /
    # bi_fetch_rows) **removed along with registration sites**, so no need to hide them here; retaining them would create
    # entries like `bi_ask_question`, always inert yet misleadingly suggesting hidden-but-callable tools.
    # Criteria (116 real calls over nearly 120 days, not artifacts.source labels): nine had zero calls and were absent from
    # all profile allow / capability required_tools / customer-author NATIVE_ALLOWLIST;
    # all 15 bi_fetch_rows calls occurred 2026-06-25→07-03 with 73% failure, while its five allowed
    # profiles spawned 487 times in the same period (positive control established).
    # ⚠ **Retain `_fetch_rows` function**: it is the only `bi_query` implementation path (all fast paths removed
    # after P13.5); it is simply no longer registered as a standalone tool.
    # See docs/sprints/tool-inventory-2026-09-04/dead-tool-inventory.md
    _OMICRON_DEPRECATED_BI = (
        # Two legacy_alias names under bi_schema remain **dual-registered** (each has its own handler),
        # so continue hiding them here; they are not old names from a rename.
        "bi_list_measures",  # → bi_schema()
        "bi_list_dimensions",  # → bi_schema()
    )
    for _dep_name in _OMICRON_DEPRECATED_BI:
        if _dep_name in reg._specs:
            reg._specs[_dep_name].visible_to_llm = False

    return reg


# Sprint ω Phase 4 · paradigm v2 role bindings now come from generated map.
# Single source: packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml
# Codegen: uv run python -m bowhead_cli.codegen_tool_aliases
# Generated module includes canonical + legacy aliases (legacy still registered).
from bowhead_core.agents._generated.role_bindings import (  # noqa: E402
    _PARADIGM_V2_ROLE_MAP,
)
from bowhead_core.agents._generated.tool_renames import (  # noqa: E402
    canonical_tool_name,
)


def apply_paradigm_v2_role_bindings(reg: ToolRegistry) -> None:
    """gateway main.py 启动末尾调一次, 给所有已注册 tool 打 role binding.

    paradigm_v2_enabled=False workspace 下 ctx.role=None, ToolRegistry.execute 跳过 enforce.
    paradigm_v2_enabled=True workspace 下 ctx.role="lead" / "analyst", BI 工具 Lead 调被拒.
    """
    for tool_name, roles in _PARADIGM_V2_ROLE_MAP.items():
        reg.set_allowed_roles(tool_name, roles)
    logger.info(
        "tool_registry.paradigm_v2_role_bindings_applied tools=%d",
        len(_PARADIGM_V2_ROLE_MAP),
    )


def apply_tool_annotations(reg: ToolRegistry) -> None:
    """tool-domain (D1): seed MCP-aligned annotations + source on every registered built-in.

    One post-registration pass (like apply_paradigm_v2_role_bindings) — call once at startup
    AFTER all register_* (incl. creator-triggers + bootstrap_multi_agent's report tools).
    source comes from tool_category.lookup_domain; read_only/destructive from the rendering
    kind (write → destructive, else read_only); open_world from the source (web/external =
    open). Specs that already carry a source (team/MCP tools, which self-declare in
    build_team_specs) are left untouched.
    """
    from bowhead_core.tool_category import lookup_domain, lookup_kind

    n = 0
    for name, spec in reg._specs.items():
        if spec.source is not None:
            continue  # team/MCP self-declared — don't clobber
        src = lookup_domain(name)
        is_write = lookup_kind(name) == "write"
        spec.source = src
        spec.annotations = ToolAnnotations(
            open_world=src in ("web", "external"),
            destructive=is_write,
            read_only=not is_write,
        )
        n += 1
    logger.info("tool_registry.tool_annotations_applied tools=%d", n)
