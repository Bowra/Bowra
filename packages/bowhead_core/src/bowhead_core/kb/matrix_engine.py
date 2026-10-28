"""KbMatrixEngine — 文档矩阵增量抽取执行引擎（kb-matrix M2 tech design §5-6）。

职责：run 生命周期（创建/派发/收敛）、(文件×待跑列) 调用管线、provider 健康短路、
per-call post-flight 计费（扣失败停跑）、配额接线、取消/boot resume/zombie。

关键不变量（review 钉死）：
- **cell-done 落态事务 commit 严格早于 deduct；resume 永不重跑 done 格**——崩在两者
  之间 = 该 call 白嫖（favors user，敞口 ≤ 全局并发帽 × 单 call，§6.3），绝不可反序。
- **resume 第一步原子认领**（claim_resumable_runs 顶心跳，防 scanner 竞写误杀）。
- 健康短路的 primary 基准必须在与 extractor 同一 router 实例上 resolve（team v2
  config 下全局 router 的 binding 完全不同，§5.4 review P1）。

单进程假设：seq/inflight/健康态依赖 WEB_CONCURRENCY=1——引擎构造时自 assert
（独立于 chi_flags 的门，chi 退役不静默失效）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import unicodedata
from typing import TYPE_CHECKING, Any

import aiosqlite
import anyio

from bowhead_core import metrics
from bowhead_core.kb.doc_type_store import ExtractField
from bowhead_core.kb.eligibility import KbUseEligibility
from bowhead_core.kb.extracted_record_store import wrap_extractor_result_to_variants
from bowhead_core.kb.generations import use_guard_enabled
from bowhead_core.kb.matrix_store import cell_is_stale, is_raw_string_sentinel, normalize_matrix_value
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.structured_extractor import backfill_chunk_ids
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.rate_card import credits_for_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.kb.matrix_store import (
        KbMatrixStore,
        MatrixCellDTO,
        MatrixDTO,
        MatrixRunDTO,
    )
    from bowhead_core.kb.store import KbStore
    from bowhead_core.kb.structured_extractor import StructuredExtractor
    from bowhead_core.kb.workspace_quotas_store import KbWorkspaceQuotasStore
    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)


# Execution-unit soft cap: columns per extract call (aligned with MAX_FIELDS_PER_SCHEMA; split excess into batches, do not reject).
MAX_COLS_PER_CALL = 20
# Extractor serial segmentation (same as structured_extractor.MAX_CHUNK_CHARS; shared by estimates/timeouts).
CHUNK_CHARS = 24_000
# Estimated reasoning term: rounded spike median 1.4k (§6.1).
EST_REASONING_TOKENS_PER_COL = 1500
# ETA basis: spike p50 28s/cell (FR-21).
ETA_SECONDS_PER_CELL = 28

BATCH_SCOPE_KINDS = ("all_pending", "column", "force_all", "force_column", "stale", "stale_column")
SCOPE_KINDS = (*BATCH_SCOPE_KINDS, "single_cell")
# Stale scopes (FR-12b rerun-expired row + FR-4 post-edit column confirmation): selection = derived stale predicate,
# includes verified, excludes edited; empty is not stale (rerun existing results semantics).
STALE_SCOPE_KINDS = ("stale", "stale_column")

# cross-verify (FR-39): kernel samples whole prompts (SELF_CONSISTENCY_N=2 reruns all fields);
# cost/duration ×2 (tech-design-m3 §0.1/decision 10).
CV_COST_MULTIPLIER = 2


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def cell_concurrency() -> int:
    return max(1, _env_int("BOWHEAD_KB_MATRIX_CELL_CONCURRENCY", 8))


def global_concurrency() -> int:
    return max(1, _env_int("BOWHEAD_KB_MATRIX_GLOBAL_CONCURRENCY", 16))


def ws_active_runs_cap() -> int:
    # M3 default 2→4 (tech-design-m3 decision 6): keeping 2 after multiple runs per matrix
    # would make concurrency nominal; global concurrency gate controls LLM pressure. Document soft-cap semantics honestly (cross-matrix TOCTOU).
    return max(1, _env_int("BOWHEAD_KB_MATRIX_WS_ACTIVE_RUNS", 4))


def matrix_active_runs_cap() -> int:
    """per-matrix 活跃**批量** run 帽（M3 替代单 run 409 闸；single_cell 豁免，决策 7）。"""
    return max(1, _env_int("BOWHEAD_KB_MATRIX_MATRIX_ACTIVE_RUNS", 3))


def call_timeout_s(n_chunks: int, *, cross_verify: bool = False) -> int:
    """per-call 超时按 chunk 数动态（review P2：固定值会误杀大文件死-primary 场景）。
    cv call ×2（采样在 chunk 循环内串行，决策 10）。"""
    base = _env_int("BOWHEAD_KB_MATRIX_CALL_TIMEOUT_BASE_S", 120)
    per_chunk = _env_int("BOWHEAD_KB_MATRIX_CALL_TIMEOUT_PER_CHUNK_S", 180)
    timeout = base + per_chunk * max(1, n_chunks)
    return timeout * CV_COST_MULTIPLIER if cross_verify else timeout


def zombie_threshold_s() -> int:
    """动态阈值（review P1：硬编码 15min 会在运维调大 timeout 后误杀健康长 call）。
    锚 10-chunk cv call timeout 的 2 倍（覆盖绝大多数场景）。**注意（impl-diff review
    P3-1）：>20 chunk（>~500KB 文本）的 cv call timeout 会超过本阈值，「恒大于」在极大
    文件上不成立**——但这**非承重**：`scan_zombies_once` 只对「无活 asyncio task」的
    run 判死（进程内 live 长 call 永不误杀），阈值仅管跨重启的孤儿回收（那时 call 确已
    随进程死亡）。M2 起就是此性质（非 M3 回归）。"""
    return max(900, 2 * call_timeout_s(10, cross_verify=True))


def normalize_prompt_text(s: str | None) -> str:
    """缓存指纹规范化（§5.6）：NFKC（全半角折叠）+ 连续空白折叠。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def column_fingerprint(col: dict[str, Any]) -> str:
    """prompt_version 不入 fingerprint——语义等价性由 question/hint/type 文本承载（FR-18 r4）。
    M3（FR-18 r5，条件式扩容规则明文——review G-P2-6）：options 段仅在列带 options 时
    入串、cv 段仅在 cross_verify=true 时入串——**存量列（无 options、cv=false）指纹
    逐字节不变**（回归 UT 锚此规则）。"""
    segments = [
        normalize_prompt_text(str(col.get("question") or "")),
        str(col.get("type") or ""),
        normalize_prompt_text(str(col.get("extraction_hint") or "")),
    ]
    options = col.get("options")
    if options:
        # Order-independent (FR-4/§4.6: options changes and semantic equivalence use sets, not order—
        # impl-diff review P2-1): sort before serialization; reorder does not invalidate cache.
        segments.append("opts:" + "\x1e".join(sorted(normalize_prompt_text(str(o)) for o in options)))
    if col.get("cross_verify"):
        segments.append("cv:1")
    return hashlib.sha256("\x1f".join(segments).encode("utf-8")).hexdigest()


def option_constraint_clause(col: dict[str, Any]) -> str:
    """新类型降解为 string 时追加进 extraction_hint 的选项约束句（tech-design-m3
    §4.6；预估与执行共用同一函数，预估的 prompt 文本项才对得上真实拼装）。"""
    type_ = str(col.get("type") or "")
    options = col.get("options") or []
    joined = "、".join(str(o) for o in options)
    if type_ == "select":
        return f"答案必须严格从以下选项中选择一个（原样输出选项文本）：{joined}。均不符合时输出 null。"
    if type_ == "multi_select":
        return f"答案可选多个，输出 JSON 字符串数组，元素必须严格来自以下选项（原样输出选项文本）：{joined}。均不符合时输出 null。"
    if type_ == "boolean":
        return "只输出 true 或 false；无法判断时输出 null。"
    return ""


def build_cache_key(team_id: str, content_hash: str, col: dict[str, Any], model: str) -> str:
    """team_id 入键防跨租户 oracle；content_hash 是 proof-of-possession（§5.6 越权论证）。"""
    raw = "\x1f".join([team_id, content_hash, column_fingerprint(col), model])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─── Exception families (route maps HTTP semantics) ──────


class MatrixRunError(Exception):
    """引擎侧 run 创建拒绝。code 供 route 映射响应体。"""

    def __init__(self, code: str, message: str = "", **payload: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.payload = payload


class MatrixRunConflict(MatrixRunError):
    """409：单矩阵单 run（FR-13）/ 空间级活跃 run 帽。"""


class MatrixInsufficientCredits(MatrixRunError):
    """402：预估 > min(双层) 拒绝发起（FR-14）。"""


class MatrixQuotaExceeded(MatrixRunError):
    """429：kb_workspace_quotas 月度 token 配额触顶（FR-37b）。"""


class MatrixRateLimited(MatrixRunError):
    """429：触发频控（§6.6，防误触不防成本）。"""


# ─── Provider health short-circuit (§5.4, run-level memory state) ─


class ProviderHealth:
    """按 run 记 primary 连续失败；≥阈值且有健康 fallback → 经 user_pref seam 把
    健康 provider 顶到 primary 位（resolve() 保留 fallback 链 = 短路不是切断）。"""

    def __init__(
        self,
        primary: tuple[str, str] | None,
        *,
        threshold: int | None = None,
        probe_every: int | None = None,
    ) -> None:
        self.primary = primary
        self.threshold = threshold if threshold is not None else max(1, _env_int("BOWHEAD_KB_MATRIX_PRIMARY_FAIL_THRESHOLD", 2))
        self.probe_every = probe_every if probe_every is not None else max(2, _env_int("BOWHEAD_KB_MATRIX_PRIMARY_PROBE_EVERY", 10))
        self.consecutive_primary_failures = 0
        self.healthy_fallback: tuple[str, str] | None = None
        self.shorted = False
        self._calls_since_short = 0

    def next_pref(self) -> Any:
        """本次调用的 user_pref；短路期间每 probe_every 次放行一次原 binding 探测恢复。"""
        if not self.shorted or self.healthy_fallback is None or self.primary is None:
            return None
        self._calls_since_short += 1
        if self._calls_since_short % self.probe_every == 0:
            return None  # Recovery probe
        from bowhead_router.router import UserModelPref

        return UserModelPref(provider=self.healthy_fallback[0], model=self.healthy_fallback[1])

    def record_success(self, effective_provider: str | None, effective_model: str | None) -> None:
        if self.primary is None or not effective_provider:
            return
        if effective_provider == self.primary[0]:
            # Primary itself succeeds (including admitted probe) → clear short-circuit
            self.consecutive_primary_failures = 0
            if self.shorted:
                self.shorted = False
                self._calls_since_short = 0
                emit_audit("kb.matrix_provider_shortcircuit", {"action": "recovered", "provider": self.primary[0]})
        else:
            # Fallback succeeds = primary failed this call + fallback healthy (Router chain semantics)
            self.consecutive_primary_failures += 1
            self.healthy_fallback = (effective_provider, effective_model or "")
            if not self.shorted and self.consecutive_primary_failures >= self.threshold:
                self.shorted = True
                self._calls_since_short = 0
                metrics.inc("kb_matrix.provider_shortcircuit")
                emit_audit(
                    "kb.matrix_provider_shortcircuit",
                    {"action": "engaged", "provider": self.primary[0], "fallback": effective_provider},
                )

    def record_chain_failure(self) -> None:
        """strict_invoke 异常 = fallback 链穷尽：primary 必失败，但无健康 fallback 可短路。"""
        self.consecutive_primary_failures += 1


# ─── engine ─────────────────────────────────────────────────


class KbMatrixEngine:
    """独立执行池：不走 parse_and_index / row_extractor，不与 _ROW_EXTRACT_LLM_SEMAPHORE
    抢闸；LLM 总压力由引擎全局帽约束（Open Q6）。"""

    def __init__(
        self,
        *,
        matrix_store: KbMatrixStore,
        files_store: KbFilesStore,
        kb_store: KbStore,
        extractor: StructuredExtractor,
        router: Any,
        user_store: UserStore,
        quotas_store: KbWorkspaceQuotasStore,
        runtime_resolver: Callable[[str], Awaitable[Any]] | None = None,
        artifact_store: Any = None,
        file_authorizer: Any = None,
    ) -> None:
        # Single-process self-assert (decision 2): independent of chi_flags gate; chi retirement cannot silently disable it.
        web_concurrency = os.environ.get("WEB_CONCURRENCY", "1") or "1"
        if web_concurrency != "1":
            raise RuntimeError(f"kb-matrix engine requires WEB_CONCURRENCY=1 (got {web_concurrency}): seq/inflight/health state is per-process")
        self._store = matrix_store
        self._files_store = files_store
        self._kb_store = kb_store
        self._extractor = extractor
        self._router = router
        self._user_store = user_store
        self._quotas = quotas_store
        self._runtime_resolver = runtime_resolver
        self._artifact_store = artifact_store
        self._file_authorizer = file_authorizer
        self.run_tasks: dict[str, asyncio.Task[None]] = {}
        self._inflight: set[str] = set()  # Second idempotency guard (store UPSERT is first)
        self._global_sem: asyncio.Semaphore | None = None
        # start_run checks span awaits (TOCTOU): concurrent double-click can bypass single-run-per-matrix gate
        # (impl review B-P2-4)—per-matrix lock makes active-run check→create atomic
        self._start_locks: dict[str, asyncio.Lock] = {}

    # ─── Target expansion (FR-12b authority table + FR-9 server enforcement) ─

    @staticmethod
    def expand_targets(
        matrix: MatrixDTO,
        cells: list[MatrixCellDTO],
        scope: dict[str, Any],
        *,
        file_versions: dict[str, int] | None = None,
    ) -> tuple[list[tuple[str, str]], int]:
        """返回 (targets, skipped_edited)。批量 scope 一律排除 edited（FR-9 优先级
        高于一切）并计数；queued/running 在飞格不入目标（幂等 + cells_total 口径）。
        stale 族（M3）：圈选 = ``cell_is_stale`` 单一权威谓词（与快照 `_cell_payload`
        双消费，决策 2）——需要 ``file_versions``（stale 族缺省 → ValueError，
        caller 必须显式喂，不允许静默半个谓词）。"""
        kind = str(scope.get("kind") or "")
        if kind not in SCOPE_KINDS:
            raise ValueError(f"scope.kind must be one of {SCOPE_KINDS}")
        if kind == "single_cell":
            file_id = str(scope.get("file_id") or "")
            column_id = str(scope.get("column_id") or "")
            if file_id not in {str(f.get("file_id") or "") for f in matrix.files}:
                raise ValueError(f"unknown file_id: {file_id}")
            if column_id not in {str(c["column_id"]) for c in matrix.columns}:
                raise ValueError(f"unknown column_id: {column_id}")
            return [(file_id, column_id)], 0

        stale_kind = kind in STALE_SCOPE_KINDS
        if stale_kind and file_versions is None:
            raise ValueError("stale scope requires file_versions")

        cell_by_key = {(c.file_id, c.column_id): c for c in cells}
        col_pv_by_id = {str(c["column_id"]): int(c.get("prompt_version") or 1) for c in matrix.columns}
        col_ids = [str(c["column_id"]) for c in matrix.columns]
        if kind in ("column", "force_column", "stale_column"):
            wanted = str(scope.get("column_id") or "")
            if wanted not in col_ids:
                raise ValueError(f"unknown column_id: {wanted}")
            col_ids = [wanted]
        force = kind in ("force_all", "force_column")

        targets: list[tuple[str, str]] = []
        skipped_edited = 0
        for f in matrix.files:
            file_id = str(f.get("file_id") or "")
            for col_id in col_ids:
                cell = cell_by_key.get((file_id, col_id))
                if cell is None:
                    if not stale_kind:
                        targets.append((file_id, col_id))  # empty: include outside stale scopes
                    continue
                if cell.status in ("queued", "running"):
                    continue  # In flight
                if stale_kind:
                    # Include verified, not edited (FR-12b authority row); empty/failed not stale
                    would_target = cell_is_stale(
                        cell.status,
                        cell.prompt_version,
                        col_pv_by_id.get(col_id, 1),
                        cell.file_version,
                        (file_versions or {}).get(file_id, 1),
                    )
                else:
                    would_target = force or cell.status == "failed"
                if not would_target:
                    continue
                if cell.edited_by_user:
                    # FR-9: batches never touch edited; count only edited cells that would otherwise be targets
                    # (done+edited not candidate under all_pending; do not inflate dialog count)
                    skipped_edited += 1
                    continue
                targets.append((file_id, col_id))
        return targets, skipped_edited

    # ─── Estimates (§6.1, calibration = Spike A) ─────────────

    def build_calls(
        self,
        matrix: MatrixDTO,
        targets: list[tuple[str, str]],
    ) -> list[tuple[str, list[str]]]:
        """(文件 × 待跑列集) 分批：≤MAX_COLS_PER_CALL 且批内列名唯一
        （LLM 输出按字段名 key，同名列同批会互相覆盖）。
        M3（决策 10）：分组键 = (file, cross_verify)——内核采样是 prompt 级，cv 列
        与非 cv 列同 call 会让全批陪跑双倍成本；拆开后 ×2 严格圈在勾选列。
        批内 cv 同质，消费方取 batch 首列的 cross_verify 即代表全批。"""
        name_by_col = {str(c["column_id"]): str(c.get("name") or "") for c in matrix.columns}
        cv_by_col = {str(c["column_id"]): bool(c.get("cross_verify")) for c in matrix.columns}
        by_group: dict[tuple[str, bool], list[str]] = {}
        for file_id, col_id in targets:
            by_group.setdefault((file_id, cv_by_col.get(col_id, False)), []).append(col_id)
        calls: list[tuple[str, list[str]]] = []
        for (file_id, _cv), col_ids in by_group.items():
            batch: list[str] = []
            batch_names: set[str] = set()
            for col_id in col_ids:
                name = name_by_col.get(col_id, col_id)
                if len(batch) >= MAX_COLS_PER_CALL or name in batch_names:
                    calls.append((file_id, batch))
                    batch, batch_names = [], set()
                batch.append(col_id)
                batch_names.add(name)
            if batch:
                calls.append((file_id, batch))
        return calls

    def estimate_targets(self, matrix: MatrixDTO, targets: list[tuple[str, str]], *, router: Any | None = None) -> dict[str, Any]:
        from bowhead_core.rate_card import ModelCreditRates, ModelTokenUsage, credits_for_model_usage

        active_router = router or self._router
        primary = self._resolve_primary(active_router)
        configured_rates = getattr(active_router, "model_credit_rates", {}).get(primary) if primary else None
        rates = ModelCreditRates.from_config(configured_rates)
        conservative_rate = max(rates.input, rates.cached_input, rates.output)
        char_by_file = {str(f.get("file_id") or ""): int(f.get("char_count") or 0) for f in matrix.files}
        col_by_id = {str(c["column_id"]): c for c in matrix.columns}
        calls = self.build_calls(matrix, targets)
        credits_est = 0
        for file_id, col_ids in calls:
            chars = char_by_file.get(file_id, 0)
            n_chunks = max(1, math.ceil(chars / CHUNK_CHARS)) if chars else 1
            # Prompt text term (M3 review S-P2-2 added): resend questions/hints/options constraints per chunk
            # —negligible at question≤2000/hint≤500, no longer negligible with options up to ~6.6KB/column;
            # omission systematically understates FR-14 hard gate for options-heavy columns. Same clause function as execution.
            prompt_chars = sum(
                len(str(col_by_id.get(c, {}).get("question") or ""))
                + len(str(col_by_id.get(c, {}).get("extraction_hint") or ""))
                + len(option_constraint_clause(col_by_id.get(c, {})))
                for c in col_ids
            )
            # Reasoning ×n_chunks: each chunk independently runs all fields (review P1: omission systematically underestimates large files)
            est_tokens = chars + prompt_chars * n_chunks + EST_REASONING_TOKENS_PER_COL * len(col_ids) * n_chunks
            if col_ids and bool(col_by_id.get(col_ids[0], {}).get("cross_verify")):
                est_tokens *= CV_COST_MULTIPLIER  # Double entire cv call (honest FR-39 estimate)
            # Estimate and actual deduction must use same formula, or promised pre-run cost N drifts from actual charge
            credits_est += credits_for_model_usage(
                [ModelTokenUsage(est_tokens, 0, 0, ModelCreditRates(conservative_rate, conservative_rate, conservative_rate))]
            )
        concurrency = min(cell_concurrency(), global_concurrency())
        weighted_cells = sum(CV_COST_MULTIPLIER if bool(col_by_id.get(col_id, {}).get("cross_verify")) else 1 for _, col_id in targets)
        eta_s = math.ceil(ETA_SECONDS_PER_CELL * weighted_cells / max(1, concurrency))
        return {
            "cells_pending": len(targets),
            "calls": len(calls),
            "credits_estimated": credits_est,
            "eta_s": eta_s,
        }

    async def estimate_run(
        self,
        matrix: MatrixDTO,
        scope: dict[str, Any],
        *,
        user_id: str,
    ) -> dict[str, Any]:
        """预估端点响应体（§6.1）：只读不落库。team 池更紧时不回 team 原值
        （review P2：池余额今天只有 admin 可见）。"""
        cells = await self._store.list_cells(matrix.matrix_id)
        file_versions = await self._file_versions(matrix) if str(scope.get("kind") or "") in STALE_SCOPE_KINDS else None
        targets, skipped_edited = self.expand_targets(matrix, cells, scope, file_versions=file_versions)
        _, _, router = await self._resolve_runtime(matrix.team_id)
        est = self.estimate_targets(matrix, targets, router=router)
        effective, user_layer, team_tighter = await self._effective_balance(user_id, matrix.team_id)
        credits_est = int(est["credits_estimated"])
        return {
            **est,
            "skipped_edited": skipped_edited,
            "available_credits": user_layer,
            "team_pool_tighter": team_tighter,
            "warn": credits_est > 0.2 * effective,
            "blocked": credits_est > effective,
        }

    # ─── Run creation (§5.1) ────────────────────────────────

    async def start_run(
        self,
        matrix: MatrixDTO,
        scope: dict[str, Any],
        *,
        initiated_by: str,
    ) -> MatrixRunDTO:
        kind = str(scope.get("kind") or "")
        await self._check_rate_limits(kind, initiated_by, matrix.workspace_id)

        lock = self._start_locks.setdefault(matrix.matrix_id, asyncio.Lock())
        async with lock:  # Atomic checks (B-P2-4); locked expansion+enqueue ⇒ disjoint run targets (decision 3)
            if kind != "single_cell":
                # single_cell exempt from both caps (decision 7): retry-this-cell is FR-11 main flow; batch runs must not
                # crowd it into 409; abuse bounded by separate RL_CELL bucket + global sem.
                if await self._store.count_active_batch_runs_for_matrix(matrix.matrix_id) >= matrix_active_runs_cap():
                    raise MatrixRunConflict("matrix_active_runs_cap", "此矩阵同时运行数已达上限")
                if await self._store.count_active_runs_for_workspace(matrix.workspace_id) >= ws_active_runs_cap():
                    raise MatrixRunConflict("ws_active_runs_cap", "空间同时运行数已达上限")

            cells = await self._store.list_cells(matrix.matrix_id)
            file_versions = await self._file_versions(matrix)
            targets, skipped_edited = self.expand_targets(matrix, cells, scope, file_versions=file_versions)
            if not targets:
                raise MatrixRunError("no_pending_cells", "没有待运行的格", skipped_edited=skipped_edited)

            if use_guard_enabled():
                index, _, _ = await self._resolve_runtime(matrix.team_id)
                pending_files = iter(dict.fromkeys(file_id for file_id, _ in targets))
                denied = False

                async def check_sources():
                    nonlocal denied
                    for file_id in pending_files:
                        check = await self._source_check(file_id, initiated_by, matrix.team_id, matrix.workspace_id, index)
                        if not check.allowed:
                            denied = True
                            return

                with anyio.move_on_after(30) as check_deadline:
                    async with anyio.create_task_group() as group:
                        for _ in range(4):
                            group.start_soon(check_sources)
                if denied or check_deadline.cancel_called:
                    raise MatrixRunError("source_unavailable", "来源暂不可用，请核对文件后重试")

            _, _, router = await self._resolve_runtime(matrix.team_id)
            est = self.estimate_targets(matrix, targets, router=router)
            credits_estimated = int(est["credits_estimated"])
            effective, user_layer, team_tighter = await self._effective_balance(initiated_by, matrix.team_id)
            if credits_estimated > effective:
                # available always returns user balance (security review S-P1-1: when team is tighter, returning effective =
                # leaking raw team pool to ordinary members; align with estimate endpoint redaction)
                raise MatrixInsufficientCredits(
                    "insufficient_credits",
                    "余额不足",
                    estimated=credits_estimated,
                    available=user_layer,
                    team_pool_tighter=team_tighter,
                )
            if not await self._quotas.check_quota(matrix.workspace_id):
                raise MatrixQuotaExceeded("kb_quota_exceeded", "空间本月 LLM 配额已用完")

            column_versions = {str(c["column_id"]): int(c.get("prompt_version") or 1) for c in matrix.columns}
            run = await self._store.create_run_and_enqueue(
                matrix_id=matrix.matrix_id,
                workspace_id=matrix.workspace_id,
                team_id=matrix.team_id,
                scope=scope,
                initiated_by=initiated_by,
                targets=targets,
                skipped_edited=skipped_edited,
                credits_estimated=credits_estimated,
                column_versions=column_versions,
                file_versions=file_versions,
            )
            if run is None:
                # Actual enqueue 0 (theoretically unreachable with locked expansion; defensive closure—decision 4):
                # store rolled back entire operation, leaving no empty run. Engine throws exception (reverse store
                # import creates cycle, review G-P3-15).
                raise MatrixRunError("no_pending_cells", "没有待运行的格", skipped_edited=skipped_edited)
        await self._store.touch_last_run_at(matrix.matrix_id)
        self._spawn_run_task(run.run_id)
        metrics.inc("kb_matrix.run_started")
        emit_audit(
            "kb.matrix_run_started",
            {
                "run_id": run.run_id,
                "matrix_id": matrix.matrix_id,
                "scope_kind": kind,
                "cells_total": run.cells_total,
                "skipped_edited": skipped_edited,
                "credits_estimated": credits_estimated,
            },
        )
        return run

    # ─── Run execution ─────────────────────────────────────

    async def execute_run(self, run_id: str) -> None:
        try:
            await self._execute_run_inner(run_id)
        except Exception:
            # Prevent swallowed-exception zombies: best-effort converge to failed(system_interrupted)
            logger.exception("kb_matrix.execute_run crashed run=%s", run_id)
            try:
                await self._store.fail_pending_cells_for_run(run_id, fail_reason="system_interrupted")
                await self._store.set_run_status(run_id, "failed", error_code="system_interrupted", expected_from=("running", "cancelling"))
            except Exception:
                logger.exception("kb_matrix.execute_run cleanup failed run=%s", run_id)

    async def cancel_run(self, run_id: str) -> bool:
        """置 cancelling（CAS）；无活 task（paused/崩后未 resume）→ 就地收敛。"""
        ok = await self._store.set_run_status(run_id, "cancelling", expected_from=("running", "paused"))
        if not ok:
            return False
        task = self.run_tasks.get(run_id)
        if task is None or task.done():
            await self._converge_cancelled(run_id)
        return True

    async def resume_interrupted_runs(self) -> int:
        """boot resume（M2 验收 3）。第一步原子认领（claim 顶心跳）——否则 resume
        慢时 scanner 会把正在被恢复的 run 判 zombie。恢复后无永久 running 格。"""
        runs = await self._store.claim_resumable_runs()
        resumed = 0
        for run in runs:
            existing = self.run_tasks.get(run.run_id)
            if existing is not None and not existing.done():
                continue
            if run.status == "cancelling":
                await self._converge_cancelled(run.run_id)
                continue
            # Process died mid-call: running cells return to queued (charge happens after state commit;
            # not yet charged = free-service exposure ≤global concurrency cap ×one call, documented §6.3)
            await self._store.reset_running_cells_to_queued(run.run_id)
            if run.status == "paused":
                if not await self._store.set_run_status(run.run_id, "running", expected_from=("paused",)):
                    continue
            self._spawn_run_task(run.run_id)
            emit_audit("kb.matrix_run_resumed", {"run_id": run.run_id, "matrix_id": run.matrix_id, "from_status": run.status})
            resumed += 1
        return resumed

    async def scan_zombies_once(self) -> int:
        """进程内兜底（防吞异常）：心跳超阈值且无活 task 的 running run → failed。"""
        candidates = await self._store.list_zombie_candidate_runs(zombie_threshold_s())
        n = 0
        for run in candidates:
            task = self.run_tasks.get(run.run_id)
            if task is not None and not task.done():
                continue  # Skip run with live task (review P1 race)
            await self._store.fail_pending_cells_for_run(run.run_id, fail_reason="system_interrupted", fail_hint="zombie_no_heartbeat")
            if await self._store.set_run_status(run.run_id, "failed", error_code="system_interrupted", expected_from=("running",)):
                n += 1
                # Converge through billing backfill (including run_finished/failed summary audit)—zombies
                # are exactly where in-loop totals most likely drift (impl review B-P2-7)
                await self._finalize_run_billing(run.run_id)
        return n

    async def zombie_scan_loop(self, interval_s: int = 600) -> None:
        """lifespan 任务（_kb_per_row_heartbeat_scanner 循环范式）。"""
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.scan_zombies_once()
            except Exception:
                logger.exception("kb_matrix.zombie_scan failed")

    # ─── internals ──────────────────────────────────────────

    def _spawn_run_task(self, run_id: str) -> None:
        """强引用存 dict（worker.py detached task 范式）；boot resume 的 task 同入口。"""
        task = asyncio.create_task(self.execute_run(run_id))
        self.run_tasks[run_id] = task
        task.add_done_callback(lambda _t: self.run_tasks.pop(run_id, None))

    def _get_global_sem(self) -> asyncio.Semaphore:
        if self._global_sem is None:
            self._global_sem = asyncio.Semaphore(global_concurrency())
        return self._global_sem

    async def _check_rate_limits(self, kind: str, user_id: str, workspace_id: str) -> None:
        """双桶（kb_extract_service 范式）+ 单格重跑独立桶（review P2：与批量共桶
        会让手动修 30+ 失败格自伤）。只防误触不防成本——成本护栏是余额+配额。"""
        if kind == "single_cell":
            limit = _env_int("BOWHEAD_KB_MATRIX_RL_CELL", 120)
            if not await self._user_store.rate_limit_check(f"kb_matrix_cell_rerun:{user_id}", limit=limit, window_s=3600):
                raise MatrixRateLimited("rate_limited", "单格重跑触发频控")
            return
        user_limit = _env_int("BOWHEAD_KB_MATRIX_RL_USER", 30)
        ws_limit = _env_int("BOWHEAD_KB_MATRIX_RL_WS", 120)
        if not await self._user_store.rate_limit_check(f"kb_matrix_run:{user_id}", limit=user_limit, window_s=3600):
            raise MatrixRateLimited("rate_limited", "运行触发频控（每小时）")
        if not await self._user_store.rate_limit_check(f"kb_matrix_run_ws:{workspace_id}", limit=ws_limit, window_s=86400):
            raise MatrixRateLimited("rate_limited", "空间运行触发频控（每天）")

    async def _effective_balance(self, user_id: str, team_id: str) -> tuple[int, int, bool]:
        """(effective=min(双层), user 层数字, team 池更紧)。"""
        user = await self._user_store.get_user(user_id)
        team = await self._user_store.get_team(team_id)
        user_bal = int(getattr(user, "credit_balance", 0) or 0) if user else 0
        team_bal = int(getattr(team, "team_credit_balance", 0) or 0) if team else 0
        return min(user_bal, team_bal), user_bal, team_bal < user_bal

    async def _file_versions(self, matrix: MatrixDTO) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in matrix.files:
            file_id = str(f.get("file_id") or "")
            dto = await self._files_store.get(file_id)
            # Soft-deleted / missing → version 1, symmetric with snapshot `get_snapshot` file_meta
            # `("", True, 1)`—`cell_is_stale` is decision 2's sole predicate; both consumers
            # (_cell_payload and expand_targets) must receive same file_version, otherwise
            # soft-deleted files with bumped versions display nonstale yet are selected by stale scope
            # (impl-diff review P2-2; _execute_call pre-dispatch recheck self-heals, still pin symmetry).
            if dto is None or getattr(dto, "soft_deleted_at", None) is not None:
                out[file_id] = 1
            else:
                out[file_id] = int(getattr(dto, "version", 1) or 1)
        return out

    async def _resolve_runtime(self, team_id: str) -> tuple[KbStore, StructuredExtractor, Any]:
        """per-team runtime（§5.4 review P1：extractor 与 router 必须同实例）；
        解析失败回默认（kb.py _get_effective_kb_store 范式）。"""
        if self._runtime_resolver is not None:
            try:
                rt = await self._runtime_resolver(team_id)
            except Exception:
                logger.warning("kb_matrix.runtime_resolve_failed team=%s", team_id, exc_info=True)
                rt = None
            if rt is not None and getattr(rt, "structured_extractor", None) is not None and getattr(rt, "router", None) is not None:
                return rt.kb_store, rt.structured_extractor, rt.router
        return self._kb_store, self._extractor, self._router

    def _resolve_primary(self, router: Any) -> tuple[str, str] | None:
        try:
            binding = router.resolve("lead")
            primary = getattr(binding, "primary", None)
            if isinstance(primary, tuple) and len(primary) == 2:
                return (str(primary[0]), str(primary[1]))
        except Exception:
            logger.warning("kb_matrix.resolve_primary_failed", exc_info=True)
        return None

    async def _execute_run_inner(self, run_id: str) -> None:
        run = await self._store.get_run(run_id)
        if run is None or run.status != "running":
            return
        matrix = await self._store.get_matrix(run.matrix_id)
        if matrix is None:
            await self._store.fail_pending_cells_for_run(run_id, fail_reason="system_interrupted", fail_hint="matrix_deleted")
            await self._store.set_run_status(run_id, "failed", error_code="system_interrupted")
            return
        kb_store, extractor, router = await self._resolve_runtime(run.team_id)
        health = ProviderHealth(self._resolve_primary(router))
        queued = await self._store.list_cells_for_run(run_id, statuses=("queued",))
        targets = [(c.file_id, c.column_id) for c in queued]
        calls = self.build_calls(matrix, targets)
        col_def_by_id = {str(c["column_id"]): c for c in matrix.columns}
        state: dict[str, Any] = {"stop": None, "calls_done": 0}
        run_sem = asyncio.Semaphore(cell_concurrency())
        global_sem = self._get_global_sem()
        progress_every = max(1, len(calls) // 5)  # Fixed %20 gives small matrices no progress events (review P2)

        async def run_one_call(file_id: str, col_ids: list[str]) -> None:
            # Acquire run_sem before global_sem: reversed order holds global permits while waiting for
            # run slots, starving two concurrent runs per space into near-serial execution (impl review B-P2-6)
            async with run_sem, global_sem:
                if state["stop"]:
                    return
                current = await self._store.get_run(run_id)
                if current is None or current.status != "running":
                    return  # Pre-dispatch cancelling/external-state check
                if not await self._quotas.check_quota(run.workspace_id):
                    state["stop"] = "quota_exceeded"
                    metrics.inc("kb_matrix.quota_hit")
                    return
                inflight_keys = [f"{run.matrix_id}:{file_id}:{c}" for c in col_ids]
                if any(k in self._inflight for k in inflight_keys):
                    return
                self._inflight.update(inflight_keys)
                try:
                    await self._execute_call(run, matrix, file_id, col_ids, col_def_by_id, kb_store, extractor, router, health, state)
                except Exception:
                    # Unexpected exceptions reaching gather(return_exceptions=True) are swallowed: cells stay permanently
                    # queued/running while run still succeeds (impl review B-P1-2)—catch locally
                    # as failed, retryable
                    logger.exception("kb_matrix.call_crashed run=%s file=%s", run_id, file_id)
                    try:
                        n = await self._store.fail_cells(
                            run.matrix_id,
                            [(file_id, c) for c in col_ids],
                            fail_reason="system_interrupted",
                            fail_hint="call_crashed",
                        )
                        if n:
                            await self._store.increment_run_counters(run_id, failed=n)
                    except Exception:
                        logger.exception("kb_matrix.call_crash_cleanup_failed run=%s", run_id)
                finally:
                    self._inflight.difference_update(inflight_keys)
                    state["calls_done"] += 1
                    if state["calls_done"] % progress_every == 0:
                        emit_audit(
                            "kb.matrix_run_progress",
                            {"run_id": run_id, "matrix_id": run.matrix_id, "calls_done": state["calls_done"], "calls_total": len(calls)},
                        )
                    # Call ≥15s, no throttle needed (§5.1)
                    await self._store.touch_run_heartbeat(run_id)

        await asyncio.gather(*(run_one_call(f, cs) for f, cs in calls), return_exceptions=True)

        # ─── Convergence (§5.5) ───
        current = await self._store.get_run(run_id)
        if current is None:
            return
        if current.status == "cancelling":
            await self._converge_cancelled(run_id)  # Includes billing backfill
        elif state["stop"]:
            await self._store.fail_pending_cells_for_run(run_id, fail_reason=str(state["stop"]))
            await self._store.set_run_status(run_id, "failed", error_code=str(state["stop"]), expected_from=("running",))
            metrics.inc("kb_matrix.run_failed")
            await self._finalize_run_billing(run_id)
        else:
            # Residual-cell sweep (B-P1-2 second guard): fail all leaked queued/running cells before succeeded—
            # nonterminal cells under terminal runs cannot be rescued (zombie sweep only sees running runs)
            leaked = await self._store.fail_pending_cells_for_run(run_id, fail_reason="system_interrupted", fail_hint="call_leaked")
            if leaked:
                await self._store.increment_run_counters(run_id, failed=leaked)
            if not await self._store.set_run_status(run_id, "succeeded", expected_from=("running",)):
                # Narrow window: set cancelling after gather
                await self._converge_cancelled(run_id)
            else:
                metrics.inc("kb_matrix.run_succeeded")
                await self._finalize_run_billing(run_id)

    async def _converge_cancelled(self, run_id: str) -> None:
        run = await self._store.get_run(run_id)
        # Run crashing while cancelling may retain running cells (process died mid-call)—reset to
        # queued, then roll back uniformly; otherwise terminal run's running cells are permanent orphans: batch expansion skips in-flight,
        # idempotent enqueue skips, zombie sweep only sees running runs; no rescue path (impl review B-P1-1).
        await self._store.reset_running_cells_to_queued(run_id)
        await self._store.revert_queued_cells_for_run(run_id)
        if not await self._store.set_run_status(run_id, "cancelled", expected_from=("cancelling", "running", "paused")):
            return  # Already terminal (cancel_run/boot resume): skip duplicate side effects (B-P2-10)
        metrics.inc("kb_matrix.run_cancelled")
        emit_audit("kb.matrix_run_cancelled", {"run_id": run_id, "matrix_id": run.matrix_id if run else ""})
        await self._finalize_run_billing(run_id)

    async def _finalize_run_billing(self, run_id: str) -> None:
        """对账唯一真相 = ledger（单库原子）；runs 是派生台账，收敛时反查回填自愈
        （review P1：跨库两 commit 之间崩溃会让两账分叉）。附 run_finished/失败汇总审计。"""
        credits_charged, tokens_in, tokens_out = await self._ledger_totals(run_id)
        await self._store.update_run_billing(run_id, credits_charged=credits_charged, tokens_input=tokens_in, tokens_output=tokens_out)
        run = await self._store.get_run(run_id)
        if run is None or run.status not in ("succeeded", "failed", "cancelled"):
            return
        emit_audit(
            "kb.matrix_run_finished",
            {
                "run_id": run_id,
                "matrix_id": run.matrix_id,
                "status": run.status,
                "error_code": run.error_code,
                "cells_total": run.cells_total,
                "cells_done": run.cells_done,
                "cells_failed": run.cells_failed,
                "cells_cached": run.cells_cached,
                "skipped_edited": run.skipped_edited,
                "credits_estimated": run.credits_estimated,
                "credits_charged": credits_charged,
            },
        )
        failed_cells = await self._store.list_cells_for_run(run_id, statuses=("failed",))
        if failed_cells:
            by_reason: dict[str, int] = {}
            for c in failed_cells:
                by_reason[c.fail_reason or "unknown"] = by_reason.get(c.fail_reason or "unknown", 0) + 1
            emit_audit("kb.matrix_cell_failed_summary", {"run_id": run_id, "matrix_id": run.matrix_id, "by_reason": by_reason})

    async def _ledger_totals(self, run_id: str) -> tuple[int, int, int]:
        async with aiosqlite.connect(self._user_store.db_path) as db:  # noqa: SIM117
            async with db.execute(
                "SELECT COALESCE(SUM(-delta), 0), COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0)"
                " FROM credit_ledger WHERE turn_id = ? AND reason = 'kb_matrix_run' AND delta < 0",
                (run_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return 0, 0, 0
        return int(row[0]), int(row[1]), int(row[2])

    async def _execute_call(
        self,
        run: MatrixRunDTO,
        matrix: MatrixDTO,
        file_id: str,
        col_ids: list[str],
        col_def_by_id: dict[str, dict[str, Any]],
        kb_store: KbStore,
        extractor: StructuredExtractor,
        router: Any,
        health: ProviderHealth,
        state: dict[str, Any],
    ) -> None:
        """单调用管线（§5.3）。顺序不变量：cell-done 落态 commit 严格早于 deduct。"""
        matrix_id = run.matrix_id
        keys = [(file_id, c) for c in col_ids]

        # 1. Recheck file availability before dispatch (§4.6): unavailable → skip row, not failed
        file = await self._files_store.get(file_id)
        if file is None or getattr(file, "soft_deleted_at", None) is not None:
            reverted = await self._store.revert_queued_cells(matrix_id, keys)
            await self._store.adjust_run_cells_total(run.run_id, -reverted)
            return

        source_check = None
        if use_guard_enabled():
            source_check = await self._source_check(file_id, run.initiated_by, run.team_id, run.workspace_id, kb_store)
            if not source_check.allowed:
                n = await self._store.fail_cells(matrix_id, keys, fail_reason="source_unavailable", fail_hint=source_check.reason)
                await self._store.increment_run_counters(run.run_id, failed=n)
                return
            file = await self._files_store.get(file_id)
            if file is None or file.active_generation_id != source_check.generation_id:
                raise MatrixRunError("source_unavailable")

        chunks3 = await kb_store.get_ordered_chunks_for_file(file)
        if not chunks3:
            n = await self._store.fail_cells(matrix_id, keys, fail_reason="llm_invoke_failed", fail_hint="file_has_no_chunks")
            await self._store.increment_run_counters(run.run_id, failed=n)
            return
        # Reduce projection: backfill_chunk_ids accepts only pairs (review P2)
        chunks2 = [(cid, content) for cid, content, _ in chunks3]
        text = "\n\n".join(content for _, content, _ in chunks3)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # 2. Cache lookup (§5.6): key model = primary resolved by same router instance
        primary = self._resolve_primary(router)
        done_n = failed_n = cached_n = 0
        pending: list[tuple[dict[str, Any], str | None]] = []
        for col_id in col_ids:
            col = col_def_by_id[col_id]
            lookup_key = build_cache_key(run.team_id, content_hash, col, primary[1]) if primary else None
            hit = await self._store.find_cached_cell(run.team_id, lookup_key) if lookup_key else None
            if hit is not None:
                variants = self._reground_cached_variants(hit.variants, chunks2)
                landed = await self._finalize_ai_cell(
                    run,
                    matrix_id,
                    file_id,
                    col,
                    variants=variants,
                    file_version=int(getattr(file, "version", 1) or 1),
                    content_hash=content_hash,
                    cache_key=lookup_key,
                    cached=True,
                    kb_store=kb_store,
                    source_check=source_check,
                )
                if landed:
                    done_n += 1
                    cached_n += 1
                    metrics.inc("kb_matrix.cache_hit")
                continue
            pending.append((col, lookup_key))
        if not pending:
            await self._store.increment_run_counters(run.run_id, done=done_n, cached=cached_n)
            return

        # 3. LLM call. Column type degradation (decisions 8/9, FR-2 "must not change execution engine interface"):
        # question and three new types (select/multi_select/boolean) degrade to string; new types'
        # option constraints appended to extraction_hint (option_constraint_clause shared with estimate).
        # cv: build_calls groups by (file, cv); homogeneous batch—first column represents whole batch (decision 10).
        pending_keys = [(file_id, str(col["column_id"])) for col, _ in pending]
        await self._store.mark_cells_running(matrix_id, pending_keys)
        cv = bool(pending[0][0].get("cross_verify")) if pending else False

        def _degraded_field(col: dict[str, Any]) -> ExtractField:
            type_ = str(col.get("type") or "string")
            hint = str(col.get("extraction_hint") or "")
            clause = option_constraint_clause(col)
            if clause:
                hint = f"{hint}\n{clause}".strip()
            return ExtractField(
                name=str(col.get("name") or ""),
                type=("string" if type_ in ("question", "select", "multi_select", "boolean") else type_),
                description=str(col.get("question") or ""),
                extraction_hint=hint,
                cross_verify=cv,
            )

        fields = [_degraded_field(col) for col, _ in pending]
        n_chunks = max(1, math.ceil(len(text) / CHUNK_CHARS))
        pref = health.next_pref()
        try:
            result = await asyncio.wait_for(
                extractor.extract(text=text, fields=fields, cross_verify_enabled=cv, user_pref=pref, strict_invoke=True),
                timeout=call_timeout_s(n_chunks, cross_verify=cv),
            )
        except TimeoutError:
            n = await self._store.fail_cells(matrix_id, pending_keys, fail_reason="timeout")
            await self._store.increment_run_counters(run.run_id, done=done_n, failed=n, cached=cached_n)
            return
        except Exception:
            # strict_invoke throws = exhausted fallback chain; provider failure is temporary, does not stop system-wide execution (§5.4)
            logger.warning("kb_matrix.call_invoke_failed run=%s file=%s", run.run_id, file_id, exc_info=True)
            health.record_chain_failure()
            n = await self._store.fail_cells(matrix_id, pending_keys, fail_reason="llm_invoke_failed")
            await self._store.increment_run_counters(run.run_id, done=done_n, failed=n, cached=cached_n)
            return

        health.record_success(result.effective_provider, result.effective_model)
        if primary is not None and result.effective_provider and result.effective_provider != primary[0]:
            metrics.inc("kb_matrix.call_fallback")

        if cv:
            metrics.inc("kb_matrix.cross_verify_call")  # Observe double-cost share (§6)
        backfill_chunk_ids(result.fields, chunks2)
        variants_map = wrap_extractor_result_to_variants(result.fields, fields)
        file_version = int(getattr(file, "version", 1) or 1)
        for col, lookup_key in pending:
            fname = str(col.get("name") or "")
            # Three new types: wrap uses degraded string hint → normalized {"value": str};
            # renormalize original kernel value (multi_select may be list) at matrix layer, overwriting primary
            # normalized (sentinel → needs_review uses same source in _finalize_ai_cell, §4.6).
            if fname in variants_map and str(col.get("type")) in ("select", "multi_select", "boolean"):
                raw_value = (result.fields.get(fname) or {}).get("value")
                for v in variants_map[fname]:
                    if v.get("is_primary"):
                        v["normalized"] = normalize_matrix_value(raw_value, col)
                        break
            if fname in variants_map:
                store_key = build_cache_key(run.team_id, content_hash, col, result.effective_model) if result.effective_model else lookup_key
                landed = await self._finalize_ai_cell(
                    run,
                    matrix_id,
                    file_id,
                    col,
                    variants=variants_map[fname],
                    file_version=file_version,
                    content_hash=content_hash,
                    cache_key=store_key,
                    cached=False,
                    kb_store=kb_store,
                    source_check=source_check,
                )
                if landed:
                    done_n += 1
            else:
                # LLM omitted field (value=null is valid done; only missing key is parse failure)
                await self._store.fail_cells(matrix_id, [(file_id, str(col["column_id"]))], fail_reason="llm_parse_failed")
                failed_n += 1

        # 4. Billing (§6.2): cell state committed → deduct (never reverse: crash would double-charge)
        tokens_in = int(result.usage_input_tokens or 0)
        tokens_out = int(result.usage_output_tokens or 0)
        cost = 0
        if (tokens_in + tokens_out > 0 or (run.billing_rate_card_version == "v2" and getattr(result, "model_usage", ()))) and (
            not use_guard_enabled() or done_n > cached_n
        ):
            from bowhead_core.rate_card import ModelCreditRates, ModelTokenUsage, ModelUsageLine, credits_for_model_lines

            model_usage = list(getattr(result, "model_usage", ()))
            missing_in = max(
                0,
                tokens_in
                - sum(
                    line.usage.input
                    + (line.usage.cached_input if getattr(router, "model_provider_types", {}).get(line.provider) != "anthropic_compatible" else 0)
                    for line in model_usage
                ),
            )
            missing_out = max(0, tokens_out - sum(line.usage.output for line in model_usage))
            if run.billing_rate_card_version == "v2" and (missing_in or missing_out):
                provider, model = result.effective_provider, result.effective_model
                rates = getattr(router, "model_credit_rates", {}).get((provider, model))
                model_usage.append(ModelUsageLine(provider, model, ModelTokenUsage(missing_in, 0, missing_out, ModelCreditRates.from_config(rates))))
            cost = credits_for_model_lines(model_usage) if run.billing_rate_card_version == "v2" else credits_for_tokens(tokens_in, tokens_out)
            ok, reason = await self._user_store.try_deduct_credit(
                user_id=run.initiated_by,
                team_id=run.team_id,
                cost_credits=cost,
                turn_id=run.run_id,
                workspace_id=run.workspace_id,
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                reason="kb_matrix_run",
                model_usage=tuple(model_usage) if run.billing_rate_card_version == "v2" else (),
                rate_card_version=run.billing_rate_card_version,
            )
            exhausted_reason: str | None = None
            if not ok:
                cost = 0
                exhausted_reason = reason
            else:
                # Settlement may overdraw (call already delivered, charge actual) ⇒ balance ≤0 after deduction stops subsequent dispatch (decision 7),
                # same `<= 0` predicate as chat preflight
                effective, _user_bal, team_tighter = await self._effective_balance(run.initiated_by, run.team_id)
                if effective <= 0:
                    exhausted_reason = "team_pool_zero" if team_tighter else "user_balance_zero"
            if exhausted_reason is not None:
                # This call already delivered; retain done
                state["stop"] = "insufficient_credits"
                metrics.inc("kb_matrix.credit_stop")
                try:
                    await self._user_store.record_credit_exhaustion_event(
                        team_id=run.team_id,
                        user_id=run.initiated_by,
                        workspace_id=run.workspace_id,
                        code=("team_pool_exhausted" if exhausted_reason == "team_pool_zero" else "user_credit_exhausted"),
                        turn_id=run.run_id,
                    )
                except Exception:
                    logger.warning("kb_matrix.exhaustion_record_failed run=%s", run.run_id, exc_info=True)
            await self._quotas.increment_usage(run.workspace_id, tokens_in + tokens_out)
        await self._store.increment_run_counters(
            run.run_id,
            done=done_n,
            failed=failed_n,
            cached=cached_n,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            credits_delta=cost,
        )

    async def _finalize_ai_cell(
        self,
        run: MatrixRunDTO,
        matrix_id: str,
        file_id: str,
        col: dict[str, Any],
        *,
        variants: list[dict[str, Any]],
        file_version: int,
        content_hash: str,
        cache_key: str | None,
        cached: bool,
        kb_store: Any = None,
        source_check: Any = None,
    ) -> bool:
        """AI 值落格：哨兵 → needs_review；edited 格处理按 scope 分流（B-P1-3）：
        - 批量 run：FR-9 在 run 全时长成立——enqueue 后被编辑的格不覆盖（store 层
          ``require_unedited`` 原子守卫兜竞态窗口），返回 False 表示跳过；
        - single_cell：显式意图，旧人工值降级保留为非 primary variant（§5.5 轻量
          undo）+ 审计。
        返回是否真正落态。"""
        column_id = str(col["column_id"])
        provenance = {}
        if use_guard_enabled():
            check = await self._source_check(
                file_id,
                run.initiated_by,
                run.team_id,
                run.workspace_id,
                kb_store,
                expected_generation=source_check.generation_id if source_check else "",
            )
            if not check.allowed:
                n = await self._store.fail_cells(matrix_id, [(file_id, column_id)], fail_reason="source_unavailable", fail_hint=check.reason)
                await self._store.increment_run_counters(run.run_id, failed=n)
                return False
            provenance = {
                "kb_generation_id": check.generation_id,
                "kb_source_check_state": check.warning or (source_check.warning if source_check else None) or "verified_binding",
                "generation_db_path": self._files_store._db_path,
            }
        single_cell = str(run.scope.get("kind") or "") == "single_cell"
        primary_norm = next((v.get("normalized") for v in variants if v.get("is_primary")), None)
        status = "needs_review" if is_raw_string_sentinel(primary_norm if isinstance(primary_norm, dict) else None) else "done"
        existing = await self._store.get_cell(matrix_id, file_id, column_id)
        if existing is not None and existing.edited_by_user:
            if not single_cell:
                # Manual edit landed during batch run → discard AI value for this cell (LLM already ran; only discard persistence)
                logger.info("kb_matrix.finalize_skip_edited run=%s matrix=%s file=%s col=%s", run.run_id, matrix_id, file_id, column_id)
                return False
            old_primary = next((v for v in existing.variants if v.get("is_primary")), None)
            if old_primary is not None:
                superseded = dict(old_primary)
                superseded["is_primary"] = False
                superseded["extracted_by"] = "user_edit_superseded"
                variants = [*variants, superseded]
            emit_audit("kb.matrix_cell_edit_superseded", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id, "run_id": run.run_id})
        landed = await self._store.finalize_cell(
            matrix_id,
            file_id,
            column_id,
            status=status,
            variants=variants,
            cached=cached,
            prompt_version=int(col.get("prompt_version") or 1),
            file_version=file_version,
            content_hash=content_hash,
            cache_key=cache_key,
            run_id=run.run_id,
            require_unedited=not single_cell,
            **provenance,
        )
        if not landed and use_guard_enabled():
            n = await self._store.fail_cells(matrix_id, [(file_id, column_id)], fail_reason="source_unavailable", fail_hint="generation_changed")
            await self._store.increment_run_counters(run.run_id, failed=n)
        if not landed:
            logger.info("kb_matrix.finalize_guard_hit run=%s matrix=%s file=%s col=%s", run.run_id, matrix_id, file_id, column_id)
        return landed

    async def _source_check(self, file_id: str, user_id: str, team_id: str, workspace_id: str, index: Any, *, expected_generation: str | None = None):
        async def authorize(file):
            return bool(
                self._file_authorizer and file.scope != "personal" and await self._file_authorizer(file, user_id, team_id, workspace_id, execute=True)
            )

        scope = ScopeContext(user_id=user_id, team_id=team_id, workspace_id=workspace_id, role="user")
        return await KbUseEligibility(self._files_store, self._artifact_store, index).check(
            file_id, scope, authorize, expected_generation=expected_generation
        )

    def _reground_cached_variants(
        self,
        source_variants: list[dict[str, Any]],
        chunks2: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """缓存复制句柄卫生（§5.6 review P2）：不搬源 ws 的 chunk_id/page 句柄，
        对 caller 自己文件的 chunks 重跑 backfill（同 content_hash ⇒ 内容相同必命中）。
        毒缓存收口路④（M3 review S-P1-1）：剔除人工来源 variant。**真双保险两条件**
        （impl-diff review S-P2-a）：`extracted_by` 以 user_edit 起头（user_edit /
        user_edit_superseded）**或** variant 级 `edited_by_user`——superseded 变体由
        `dict(old_primary)` 拷贝携带 `edited_by_user=True`，加此条即使未来 stamp 字面量
        重构漂移仍 fail-safe（AI 变体无此键 → 不误伤）。single_cell 重跑 edited 格会把
        superseded 人工值拼进 variants 落库，该 cell done+edited=0 满足全部命中条件——
        不过滤 = 人工内容跨空间复制。不变量：**缓存复制结果永不含人工来源 variant**（UT 钉）。"""
        out: list[dict[str, Any]] = []
        for v in source_variants:
            if str(v.get("extracted_by") or "").startswith("user_edit") or v.get("edited_by_user"):
                continue
            nv = dict(v)
            nv["chunk_id"] = None
            nv["page"] = None
            excerpt = nv.get("source_excerpt")
            if excerpt and isinstance(excerpt, str):
                probe: dict[str, dict[str, Any]] = {"_": {"source_snippet": excerpt}}
                backfill_chunk_ids(probe, chunks2)
                nv["chunk_id"] = probe["_"].get("chunk_id")
            out.append(nv)
        return out
