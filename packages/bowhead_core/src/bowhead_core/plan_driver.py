"""Plan driver — level-stepped 执行循环 (M1 block 3).

见 m1-execution-tech-design.md v2 §3. **不调 dispatch_dag**(需 per-step gate / 写回 /
resume,dispatch_dag 一次跑完不提供这些;复用其纯逻辑 `topological_sort`(reify 用)+
`filter_upstream_for`(本模块用)—— D-D1)。

执行内核(批2 S1, batch2-hitl-design §1-D4): **frontier-min round 循环**——每轮重取
steps、取最低含 eligible(非终态且非本 run held)步的 level 处理;本 run 内判
BLOCKED/FAILED 的步 hold 不重派(重试=下次 run_plan);DONE 写回解直接下游
upstream_failed(un-block 级联)。hooks 缺席时与原 for 循环逐字节等价(16 个既有
driver 测试零改动通过=等价性硬标准)。

M1 特性:
- 无中途暂停(mid-run 拾取=批2 S4 revive_check)。单轮跑完 → completed;有
  blocked/failed → blocked_needs_user;cap 超 / cancel → failed。
- **per-step 重试**:同一 `run_plan` 再调即可 —— 入口 held 为空, 上次 BLOCKED/FAILED
  步重新 eligible;done 步跳过、其全 payload 从 `payload_json` 取(全保真,非摘要)。
- per-step precondition gate:上游(全失败才 skip,部分 ok 仍跑)→ agent allowlist
  (manifest_lookup/KeyError)→ 语料(needs_corpus + corpus_check)。
- caps 可执行:max_steps(行数)/ max_total_tokens(Σ 实际 tokens)/ max_wallclock_s
  (now - execution_started_at)。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from json import dumps as _json_dumps
from json import loads as _json_loads
from typing import TYPE_CHECKING, Any

from .agents.dag_dispatcher import filter_upstream_for
from .agents.dispatcher import carve_token_budget
from .agents.task_spec import TaskFailureError, TaskResult, TaskSpec
from .plan_reify import step_to_task
from .plan_store import PlanStatus, PlanStep, PlanStore, StepStatus, is_retired_plan_agent
from .plan_verify import VERDICT_FAIL, VERDICT_SKIPPED, VerifyOutcome, outcome_from_json
from .planning_v2 import planning_replan_enabled

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanDeps:
    """执行五元组 —— 由 orchestrator runtime 提供 (test 用 fakes)."""

    sctx: Any  # SerializableTurnContext (needs .user_id/.workspace_id/.turn_id).
    runtime_factory: Any
    agent_runner_fn: Any
    manifest_lookup: Callable[[str], Any]  # registry.get (space-bound), raises KeyError.
    runner: Any  # TaskRunner.dispatch(...)
    prepare_batch: Callable[[Sequence[TaskSpec]], Awaitable[None]] | None = None


@dataclass(frozen=True)
class PlanHooks:
    corpus_check: Callable[[PlanStep], Awaitable[bool]] | None = None
    emit: Callable[[str, dict], Awaitable[None]] | None = None
    # graph-mode batch 1: comparative verifier (tech-design §1.3/§1.4). Contract: closure itself must
    # be wholly exception-proof (every exception returns skipped VerifyOutcome); driver adds no wrapper
    # (single fallback; glue error loses batch writeback + sticky FAILED plan, review C8). None=no verification.
    verify: Callable[[PlanStep, dict], Awaitable[VerifyOutcome]] | None = None
    # Batch 2 S2: midrun needs_user_input cards (orchestrator closure; per-(tool,scope)
    # aggregation and flag inside closure). Same exception-proof contract as verify; driver adds defense.
    on_needs_user: Callable[[PlanStep, str], Awaitable[None]] | None = None
    # Batch 2 S4: pickup; input=this round's BLOCKED(needs_user_input) candidates, output=revivable IDs.
    # All decisions (direct binding-store read + in-set + ctx backfill) in orchestrator closure; driver executes mechanically.
    revive_check: Callable[[list[PlanStep]], Awaitable[list[str]]] | None = None
    # plan-report-assembly batch 1-c: **binding** hook before step DONE writeback.
    # Input=(just-completed step, all steps), output=its canvas section IDs (driver backfills
    # output_section_ids). Closure reorders slots once by owner→plan_steps.seq.
    # Same contract as verify/revive_check: **entire closure exception-proof** (every error returns
    # empty tuple); no driver wrapper (glue error loses batch writeback, review C8). None=no binding.
    on_step_composed: Callable[[PlanStep, list[PlanStep]], Awaitable[tuple[str, ...]]] | None = None


@dataclass(frozen=True)
class PlanCaps:
    """plan 级硬安全栏 (default-on, 与 per-turn 软 cap 区分; review P2-M)."""

    max_steps: int = 40
    max_total_tokens: int | None = None
    max_wallclock_s: int | None = None


@dataclass(frozen=True)
class PlanRunOutcome:
    status: str  # completed | blocked_needs_user | failed
    done: int = 0
    blocked: int = 0
    failed: int = 0
    skipped: int = 0
    spent_tokens: int = 0
    reason: str = ""


@dataclass(frozen=True)
class _Gate:
    ok: bool
    status: str = StepStatus.READY
    kind: str = ""
    reason: str = ""


_TERMINAL_STEP = (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.SUPERSEDED)

# ── Plan-step timeouts (P0 2026-07 production review) ───────────
# Expert legs actually take 8-15min, report writing 2-10min, but chat-mode
# compute_task_timeout fallback only 500s/150s → parallel legs of two actual plans on 116
# all Timeout at exec+500s (8min completed work discarded); canvas-writer
# 150s timeout → no report. Plan steps are long tasks by design; use remaining wallclock budget.
_PLAN_STEP_TIMEOUT_FLOOR_S = 120  # Minimum when remaining exhausted (caps close afterward).
_PLAN_WRITER_RESERVE_S = 360  # Nonfinal-level reserve for downstream report-writing layer.
_PLAN_STEP_TIMEOUT_FALLBACK_S = 1500  # When caps.max_wallclock_s absent.


def _step_timeout_s(remaining_s: float | None, *, is_last_level: bool) -> int:
    """plan 执行步的 per-task 超时(秒)。

    env `BOWHEAD_PLANNING_STEP_TIMEOUT_S` 显式覆盖一切(soft guideline,可调);
    否则:非末层 = 剩余 wallclock - writer 预留;末层吃满剩余(只留 30s 收尾)。
    """
    env = os.environ.get("BOWHEAD_PLANNING_STEP_TIMEOUT_S", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    if remaining_s is None:
        return _PLAN_STEP_TIMEOUT_FALLBACK_S
    reserve = 30 if is_last_level else _PLAN_WRITER_RESERVE_S
    return max(int(remaining_s) - reserve, _PLAN_STEP_TIMEOUT_FLOOR_S)


async def _emit(hooks: PlanHooks, name: str, payload: dict) -> None:
    if hooks.emit is not None:
        await hooks.emit(name, payload)


async def _composed_section_ids(hooks: PlanHooks, step: PlanStep, all_steps: list[PlanStep]) -> tuple[str, ...] | None:
    """批 1-c: 调装订钩子。返回 None = 没钩子/没产出 ⇒ 调用方不写 output_section_ids。

    ψ-E 在**步执行期**就把段落了库(owner=sub_<step_id>), 所以这里查得到——不需要
    任何出参从 tool handler 深处穿回来(self-review R2 否决了出参方案)。"""
    if hooks.on_step_composed is None:
        return None
    ids = await hooks.on_step_composed(step, all_steps)
    return tuple(ids) if ids else None


def _group_by_level(steps: list[PlanStep]) -> list[list[PlanStep]]:
    if not steps:
        return []
    n = max(s.level for s in steps) + 1
    levels: list[list[PlanStep]] = [[] for _ in range(n)]
    for s in steps:
        levels[s.level].append(s)
    for lvl in levels:
        lvl.sort(key=lambda s: s.seq)
    return levels


async def _load_done_results(steps: list[PlanStep]) -> dict[str, TaskResult]:
    """已 done 步 → TaskResult(全 payload from payload_json) —— resume 时喂下游全保真."""
    out: dict[str, TaskResult] = {}
    for s in steps:
        if s.status == StepStatus.DONE and s.payload_json:
            try:
                payload = _json_loads(s.payload_json)
            except (ValueError, TypeError):
                payload = {}
            out[s.id] = TaskResult.succeeded(step_to_task(s), payload)
    return out


def _build_input_manifest(
    s: PlanStep,
    received: Sequence[dict],
    all_steps_by_id: dict[str, PlanStep],
) -> dict[str, Any] | None:
    """期望 vs 实收上游(批3 D-E)。missing 分桶(review R2-F9): 上游 skipped/
    excluded 是用户意图 → excluded_inputs(不进 caveats/verifier); 其余(failed/
    blocked/superseded/缺行) → degraded_inputs。无依赖步返 None(零噪声)。"""
    expected = list(s.depends_on)
    if not expected:
        return None
    got = {str(u.get("task_id") or "") for u in received}
    degraded: list[str] = []
    excluded: list[str] = []
    for dep in expected:
        if dep in got:
            continue
        row = all_steps_by_id.get(dep)
        if row is not None and (row.status == StepStatus.SKIPPED or not row.included):
            excluded.append(dep)
        else:
            degraded.append(dep)
    return {
        "expected": expected,
        "received": sorted(got & set(expected)),
        "degraded_inputs": degraded,
        "excluded_inputs": excluded,
    }


_SUPERSESSION_MAX_HOPS = 32


def _resolve_supersession_tail(old: PlanStep, rows_by_id: dict[str, PlanStep]) -> str | None:
    """沿 superseded_by 链解析到活尾步 id; 环/超限/世代哨兵(非 ps_ 前缀) → None.

    fail-open(review R1-F9): 数据被弄脏时按无镜像处理, 不许 driver 死循环。
    """
    seen: set[str] = set()
    cur = old
    for _ in range(_SUPERSESSION_MAX_HOPS):
        ptr = cur.superseded_by or ""
        if not ptr.startswith("ps_"):
            return None  # Generation sentinel(replan:revN)/empty: no sole successor.
        if ptr in seen:
            logger.error("plan_driver.supersession_cycle plan=%s step=%s", old.plan_id, old.id)
            return None
        seen.add(ptr)
        nxt = rows_by_id.get(ptr)
        if nxt is None:
            return None
        if nxt.status != StepStatus.SUPERSEDED:
            return nxt.id
        cur = nxt
    logger.error("plan_driver.supersession_chain_too_long plan=%s step=%s", old.plan_id, old.id)
    return None


def _digest(payload: Any) -> str:
    """进度/(M2)决策用的短摘要 —— **非报告用**(报告读 payload_json 全保真)."""
    if isinstance(payload, dict):
        for k in ("exec_summary", "headline", "conclusion", "summary"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v[:300]
    return ""


def _precondition(
    s: PlanStep,
    results: dict[str, TaskResult],
    manifest_lookup: Callable[[str], Any],
    corpus_ok: bool,
) -> _Gate:
    # 1) Upstream: all failed/missing → BLOCKED (nonterminal, recoverable: reruns after upstream repair/retry;
    #    SKIPPED only for user-unchecked steps). Some ok still runs (filter_upstream_for alignment, CTO P2-11).
    if s.depends_on:
        ok_up = [d for d in s.depends_on if d in results and results[d].ok]
        if not ok_up:
            return _Gate(False, StepStatus.BLOCKED, "upstream_failed", "所有上游步未成功")
    # 2) Agent allowlist: manifest_lookup/KeyError, never __contains__ (CTO P1-5).
    try:
        manifest_lookup(s.agent_name)
    except KeyError:
        return _Gate(False, StepStatus.BLOCKED, "unknown_agent", f"agent {s.agent_name!r} 不在 registry")
    # 3) Corpus (needs_corpus + corpus_check already awaited by caller).
    if s.needs_corpus and not corpus_ok:
        return _Gate(False, StepStatus.BLOCKED, "needs_corpus", "该步需数据室语料, workspace KB 未找到")
    return _Gate(True)


def _elapsed_s(started_at: str | None) -> float:
    if not started_at:
        return 0.0
    try:
        return (datetime.now(tz=UTC) - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return 0.0


async def _run_verifies(
    ok_results: list[TaskResult],
    steps_by_id: dict[str, PlanStep],
    hooks: PlanHooks,
) -> dict[str, VerifyOutcome | None]:
    """本批 ok 步集中 gather 核验(review A6/C5: 串行逐步会把 level 并行清零).

    hook 契约=exception-proof(§C8), 此处不再包 —— 但 steps_by_id 缺席(防御)按未验。
    """
    if hooks.verify is None or not ok_results:
        return {r.task_spec.task_id: None for r in ok_results}

    async def _safe(step: PlanStep, payload: dict) -> VerifyOutcome:
        # Defense in depth: closure contract is exception-proof, but any gather exception loses entire
        # batch writeback + sticky FAILED plan (review C8 incident); degrade to skipped here
        # with visible logging, never silently.
        try:
            return await hooks.verify(step, payload)  # type: ignore[misc]
        except Exception as e:
            logger.warning("plan_verify.hook_error step=%s err=%s", step.id, e)
            return VerifyOutcome(VERDICT_SKIPPED, note=f"hook_error:{type(e).__name__}")

    targets = [r for r in ok_results if r.task_spec.task_id in steps_by_id]
    outcomes = await asyncio.gather(*(_safe(steps_by_id[r.task_spec.task_id], r.payload or {}) for r in targets))
    got = dict(zip((r.task_spec.task_id for r in targets), outcomes, strict=True))
    return {r.task_spec.task_id: got.get(r.task_spec.task_id) for r in ok_results}


def _verify_event_field(outcome: VerifyOutcome) -> dict:
    return {**_json_loads(outcome.to_json()), "unmet": list(outcome.unmet_texts())}


def _acc_tokens(step_row: PlanStep | None, r: TaskResult, turn_id: str) -> tuple[int, int]:
    """token 记账口径(批2 V6): 同 turn 双派(revive 合法两派)累加, 跨 turn 覆盖.

    覆盖语义会把 BLOCKED 那次真实烧掉的 token 丢账(「钱扣了账没记」形状);
    但跨 turn 不能累加——turn 级计费按 turn_id 分桶(_this_turn 过滤), 累加会把
    上个 turn 已计费的 token 再记一遍。step_row 来自本轮重取, tokens/turn_id 新鲜。
    """
    if step_row is not None and step_row.turn_id == turn_id:
        return (step_row.tokens_input + r.tokens_input, step_row.tokens_output + r.tokens_output)
    return (r.tokens_input, r.tokens_output)


def _tally_verify(counts: dict[str, int], outcome: VerifyOutcome) -> None:
    counts[f"verify_{outcome.verdict}"] = counts.get(f"verify_{outcome.verdict}", 0) + 1
    # Calibration metrics (PRD R1.3a: enforce prerequisite = 0 fail false negatives @ N≥20). Shadow
    # verdicts previously only in plan_steps.verify_json, no aggregate outlet; tracking progress required
    # manual DB/card inspection (S6 counted by hand); actual runs cannot replay, lost counts unrecoverable.
    # Counters supply N/distribution only; 0 false negatives still requires human review of failures (sole S6 fail later
    # relabeled censored=right-for-wrong-reason, proving no automation here). Metrics narrow review scope
    # from everything to failures only. Follow tool_registry term_resolver
    # precedent; metrics failure never blocks business.
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_plan_verify_verdict_total",
            labels={"verdict": outcome.verdict},
            help="plan 步对照式核验的 verdict 分布(enforce 翻档标定用)",
        )
    except Exception:  # noqa: S110
        pass
    # Coverage signal (review A8): repeated skips may mean broken verifier configuration; apparently all-green must speak up.
    if outcome.verdict == VERDICT_SKIPPED:
        counts["_verify_skip_streak"] = counts.get("_verify_skip_streak", 0) + 1
        if counts["_verify_skip_streak"] == 3:
            logger.warning("plan_verify.consecutive_skips=3 — verifier 可能配置坏死(死 flag 形态), 检查 VERIFY_AGENT/routing")
    else:
        counts["_verify_skip_streak"] = 0


async def _writeback_results(
    res: list[TaskResult],
    results: dict[str, TaskResult],
    counts: dict[str, int],
    *,
    store: PlanStore,
    deps: PlanDeps,
    hooks: PlanHooks,
    plan_id: str,
    steps_by_id: dict[str, PlanStep] | None = None,
    held: set[str] | None = None,
    all_steps_by_id: dict[str, PlanStep] | None = None,
    children: dict[str, list[str]] | None = None,
) -> None:
    steps_by_id = steps_by_id or {}
    held = held if held is not None else set()
    all_steps_by_id = all_steps_by_id or {}
    children = children or {}
    verify_outcomes = await _run_verifies([r for r in res if r.ok], steps_by_id, hooks)
    for r in res:
        sid = r.task_spec.task_id
        results[sid] = r
        if r.ok:
            outcome = verify_outcomes.get(sid)
            if outcome is not None:
                _tally_verify(counts, outcome)
            step = steps_by_id.get(sid)
            # ★2026-08-16 shadow/enforce phases closed: calibration gate (0 fail false negatives @ N=152, see
            # docs/sprints/graph-mode/enforce-calibration-2026-08-14.md) reached; phased
            # `verify_enforce` flag and `BOWHEAD_PLANNING_VERIFY_MODE` removed together;
            # **temporary calibration scaffolding, not rollout gate**; retaining adds unexercised shadow path
            # (disabled path does run in production; see feedback_capability_off_path_untested).
            # fail now unconditionally FAILED; rollback via previous image (no migrations in batch).
            # No-progress degradation (review A5): step previously criteria_unmet
            # fails again on rerun → stop killing it, DONE+caveat (same inputs, repeated verdict loop too costly).
            # Two prior-verdict sources (implementation review B4-B): retry Timeout can overwrite error_kind;
            # historical verify_json fail is stable. OR both to prevent unmet→timeout→unmet oscillation.
            _prior_vo = outcome_from_json(step.verify_json) if step is not None else None
            prior_unmet = step is not None and (step.error_kind == "criteria_unmet" or (_prior_vo is not None and _prior_vo.verdict == VERDICT_FAIL))
            if outcome is not None and outcome.verdict == VERDICT_FAIL and not prior_unmet:
                unmet = "; ".join(outcome.unmet_texts())[:300]
                # Complete writeback (review B3/C7): record tokens + retain real output (batch 3 revise_step
                # material + user inspection).
                # Same-run downstream isolation (implementation review B1, P1): replace results[sid] with failed, or
                # rejected output still feeds later levels' filter_upstream_for as ok;
                # failed exclusion would apply only on resume.
                results[sid] = TaskResult.failed(r.task_spec, TaskFailureError("criteria_unmet"))
                held.add(sid)  # Batch 2 S1: no same-run FAILED redispatch (retry=re-approve).
                _ti, _to = _acc_tokens(step, r, deps.sctx.turn_id)
                await store.update_step_status(
                    sid,
                    status=StepStatus.FAILED,
                    error=f"验收未达: {unmet}",
                    error_kind="criteria_unmet",
                    verify_json=outcome.to_json(),
                    turn_id=deps.sctx.turn_id,
                    tokens_input=_ti,
                    tokens_output=_to,
                    payload_json=_json_dumps(r.payload or {}),
                )
                counts["failed"] += 1
                await _emit(
                    hooks,
                    "step_failed",
                    {
                        "plan_id": plan_id,
                        "step_id": sid,
                        "error_kind": "criteria_unmet",
                        "verify": _verify_event_field(outcome),
                    },
                )
                continue
            if outcome is not None and outcome.verdict == VERDICT_FAIL and prior_unmet:
                outcome = replace(outcome, note="验收两次未过, 产出已保留")
            _ti, _to = _acc_tokens(step, r, deps.sctx.turn_id)
            # Batch 1-c: bind (slot reorder) before writeback; ψ-E already persisted paragraphs during execution,
            # so owner lookup finds actual section IDs produced by this step.
            # Batch 1-d: previously `_section_ids(r.payload)` read payload["section_ids"],
            # a key subagent payload never had ⇒ output_section_ids always []; dead read removed.
            _slot_ids = await _composed_section_ids(hooks, step, list(all_steps_by_id.values()))
            await store.update_step_status(
                sid,
                status=StepStatus.DONE,
                turn_id=deps.sctx.turn_id,
                payload_json=_json_dumps(r.payload or {}),
                tokens_input=_ti,
                tokens_output=_to,
                output_section_ids=_slot_ids,
                result_digest=_digest(r.payload),
                verify_json=outcome.to_json() if outcome is not None else None,
                # Review B4: successful retry must clear previous error/error_kind;
                # B4-A: no this-turn verify (flag off/hook absent) also clears stale verify_json,
                # or old fail accuses new output through _step_view/caveats.
                clear_error=True,
                clear_verify=outcome is None,
            )
            counts["done"] += 1
            done_payload = {"plan_id": plan_id, "step_id": sid, "digest": _digest(r.payload)}
            if outcome is not None:
                done_payload["verify"] = _verify_event_field(outcome)
            await _emit(hooks, "step_done", done_payload)
            # Batch 2 S1 unblock cascade: hold breaks automatic downstream release on upstream recovery; DONE
            # explicitly resets **direct downstream** BLOCKED(upstream_failed) → PENDING + clear errors (only this
            # kind, never needs_corpus/unknown_agent/needs_user_input); cascade unfolds lazily
            # (unblocked step enters frontier, then DONE unlocks its own downstream).
            for cid in children.get(sid, ()):
                child = all_steps_by_id.get(cid)
                if child is not None and child.status == StepStatus.BLOCKED and child.error_kind == "upstream_failed":
                    await store.update_step_status(cid, status=StepStatus.PENDING, clear_error=True)
                    held.discard(cid)
        elif r.error is not None and r.error.reason == "needs_user_input":
            # planning-mode-audit P1-b: runtime clarification sentinel (e.g. datarc
            # space picker) → BLOCKED (recoverable, resume on re-approve), never FAILED;
            # persist clarification JSON in error column for blocked message / frontend.
            held.add(sid)  # Batch 2 S1 hold: sole same-run revival=revive_check(S4).
            _ti, _to = _acc_tokens(steps_by_id.get(sid), r, deps.sctx.turn_id)
            await store.update_step_status(
                sid,
                status=StepStatus.BLOCKED,
                error=r.error.detail or str(r.error),
                error_kind="needs_user_input",
                turn_id=deps.sctx.turn_id,
                tokens_input=_ti,
                tokens_output=_to,
            )
            counts["blocked"] += 1
            await _emit(
                hooks,
                "step_blocked",
                {
                    "plan_id": plan_id,
                    "step_id": sid,
                    "status": StepStatus.BLOCKED,
                    "error_kind": "needs_user_input",
                    "reason": "等待用户选择工具上下文",
                },
            )
            # Batch 2 S2: card immediately (before plan convergence); aggregation/flag in closure, defense here.
            if hooks.on_needs_user is not None:
                _spec = steps_by_id.get(sid)
                if _spec is not None:
                    try:
                        await hooks.on_needs_user(_spec, (r.error.detail if r.error else "") or "")
                    except Exception as _oe:
                        logger.warning("plan_midrun.on_needs_user_error step=%s err=%s", sid, _oe)
        elif r.error is not None and r.error.reason in {
            "agent_candidate_stale",
            "agent_binding_unavailable",
        }:
            # Agent Marketplace M3-13E: an approved exact candidate may be
            # unpublished, unassigned or replaced before the approval Turn
            # executes.  This is recoverable by revise/replan, so keep the
            # Plan BLOCKED; never retry by routing-name to whichever revision
            # happens to be latest.
            kind = r.error.reason
            held.add(sid)
            _ti, _to = _acc_tokens(steps_by_id.get(sid), r, deps.sctx.turn_id)
            _orig = getattr(r.error, "original", None)
            reason = (str(_orig).strip() if _orig is not None else "") or "所选 Agent 已发生变更或当前不可用，请重新规划后重试。"
            await store.update_step_status(
                sid,
                status=StepStatus.BLOCKED,
                error=reason,
                error_kind=kind,
                turn_id=deps.sctx.turn_id,
                tokens_input=_ti,
                tokens_output=_to,
            )
            counts["blocked"] += 1
            await _emit(
                hooks,
                "step_blocked",
                {
                    "plan_id": plan_id,
                    "step_id": sid,
                    "status": StepStatus.BLOCKED,
                    "error_kind": kind,
                    "reason": reason,
                },
            )
        else:
            kind = r.error.reason if r.error else "error"
            held.add(sid)  # Batch 2 S1: no same-run FAILED redispatch.
            _ti, _to = _acc_tokens(steps_by_id.get(sid), r, deps.sctx.turn_id)
            # ★116 dogfood 2026-08-14/15 caught two consecutive issues, handled together here:
            # ①`str(r.error)` is `f"{kind}: {original}"`, while error_kind already separately stores kind,
            #   causing **repeated enum** cards like "Failed: domain_mismatch: 知识库中无…".
            #   Keep readable half only; fallback to old shape only if original empty (e.g. no-arg exception), preserving info.
            # ②`step_failed` previously **only carried error_kind, no reason** (step_blocked always did),
            #   so live card blank after "Failed:", **full text only after F5**; DB always correct.
            #   Same event, same defect as missing verify field documented in applyPlanStepFailed docstring.
            #   ⚠ Storage/event must use **same string**, or live/persisted/replay diverge (CLAUDE.md invariant).
            _orig = getattr(r.error, "original", None)
            _reason = (str(_orig).strip() if _orig is not None else "") or str(r.error or "?")
            await store.update_step_status(
                sid,
                status=StepStatus.FAILED,
                error=_reason,
                error_kind=kind,
                turn_id=deps.sctx.turn_id,
                tokens_input=_ti,
                tokens_output=_to,
            )
            counts["failed"] += 1
            await _emit(hooks, "step_failed", {"plan_id": plan_id, "step_id": sid, "error_kind": kind, "reason": _reason})


# ── Automatic transient retry ──────────────────────────────────
# ★2026-08-24: `transient` contract **already required retry**; `analysis_payload._VALID_BAIL_KINDS`
# original definition says transient: tool fails but space has data → **retry** or show error; nobody
# implemented retry across pipeline, driver directly FAILED.
#
# Actual cost (plan_ca30aa484f68, 2026-08-19): BI row query 60s timeout → 2 transient steps,
# 2 more BLOCKED by `upstream_failed`; **one slow database ruined 4 of 5 steps**,
# user sees nearly all-red graph.
#
# Signal reliability (checked specifically before editing, contrary to initial concern):
#   · Kind here **is not** chat error card's hardcoded `is_transient` (TaskFailureError
#     lacks that field; driver cannot see it). **Peer declares kind in bail envelope**,
#     only after own tool retries exhausted / circuit breaker, never casually.
#   · Closed vocabulary (`_VALID_BAIL_KINDS`); inability kinds `domain_mismatch`/`needs_user_input`
#     excluded from retry: a hundred retries change nothing.
#
# Retry only once, no unbounded escalation: cannot fix truly unavailable BI, only turns one failure
# into two expensive failures (same tradeoff as `_AUTHOR_RETRY_MAX_TOKENS`).
_RETRYABLE_KINDS = frozenset({"transient"})


def _retryable(r: TaskResult) -> bool:
    """判据单点: 与 `_writeback_results` 取 kind 用同一个字段(`r.error.reason`)。

    ⚠ 别在别处复制这段判断 —— 同一判据两份实现是本仓反复踩的形状。
    """
    return (not r.ok) and bool(r.error) and (r.error.reason in _RETRYABLE_KINDS)


async def _process_level(
    eligible: list[PlanStep],
    lvl: int,
    results: dict[str, TaskResult],
    counts: dict[str, int],
    *,
    store: PlanStore,
    deps: PlanDeps,
    hooks: PlanHooks,
    plan_id: str,
    parent_token_budget: int,
    step_timeout_s: int | None = None,
    remaining_s: float | None = None,
    held: set[str] | None = None,
    all_steps_by_id: dict[str, PlanStep] | None = None,
    children: dict[str, list[str]] | None = None,
) -> tuple[int, int]:
    """处理 frontier 层的 eligible 步(caller 已过滤 hold/终态)。

    返回 (dispatched, gated) —— round 循环的后置进展判据(批2 S1)。
    held: 本 run 内判 BLOCKED/FAILED 的步 id(hold, 同 run 不重派);gate 判掉的步在此登记。
    """
    held = held if held is not None else set()
    gated = 0
    runnable: list[PlanStep] = []
    for s in eligible:
        corpus_ok = True
        if s.needs_corpus and hooks.corpus_check is not None:
            corpus_ok = await hooks.corpus_check(s)
        g = _precondition(s, results, deps.manifest_lookup, corpus_ok)
        if not g.ok:
            await store.update_step_status(s.id, status=g.status, error=g.reason, error_kind=g.kind)
            counts["skipped" if g.status == StepStatus.SKIPPED else "blocked"] += 1
            held.add(s.id)
            gated += 1
            await _emit(
                hooks,
                "step_blocked",
                {
                    "plan_id": plan_id,
                    "step_id": s.id,
                    "status": g.status,
                    "error_kind": g.kind,
                    "reason": g.reason,
                },
            )
        else:
            runnable.append(s)
    if not runnable:
        return (0, gated)
    per_task = carve_token_budget(parent_token_budget, len(runnable))
    tasks = []
    for s in runnable:
        t = replace(step_to_task(s), tokens_estimate=per_task, timeout_s=step_timeout_s)
        up = filter_upstream_for(t, list(results.values()))
        tasks.append(replace(t, upstream_payloads=(*t.upstream_payloads, *up)))
        # Batch 3 D-E: persist upstream receipts once with RUNNING (same round/iteration, correct order),
        # and backfill memory: verify hook receives round-top snapshot; DB-only write makes
        # first-dispatch verifier always see None (review R1-F3/R2-F10; same precedent as revive memory sync).
        _mani = _build_input_manifest(s, up, all_steps_by_id or {}) if planning_replan_enabled() else None
        if _mani is not None:
            _mani_json = _json_dumps(_mani, ensure_ascii=False)
            s.input_manifest_json = _mani_json
            await store.update_step_status(s.id, status=StepStatus.RUNNING, input_manifest_json=_mani_json)
        else:
            # Flag off/no dependencies: clear old manifest (no stale retry receipts for verifier, P2-2).
            s.input_manifest_json = None
            await store.update_step_status(s.id, status=StepStatus.RUNNING, clear_input_manifest=True)
    await _emit(hooks, "step_started", {"plan_id": plan_id, "level": lvl, "step_ids": [s.id for s in runnable]})
    _t0 = time.monotonic()
    if deps.prepare_batch is not None:
        await deps.prepare_batch(tasks)
    res = await deps.runner.dispatch(tasks, deps.sctx, deps.runtime_factory, deps.agent_runner_fn, deps.manifest_lookup)
    _spent = time.monotonic() - _t0

    # Retry transient once, **before writeback**, so first failure neither persists FAILED
    # nor emits step_failed nor blocks downstream via upstream_failed (all occur during writeback).
    #
    # ⚠ Budget guard: `_caps_breach` runs **after each level** (run_plan loop), retry occurs before
    # it, so check wallclock here or one retry overshoots max_wallclock_s by a whole step,
    # especially deep graphs. Criterion: after first dispatch, enough time for next minimum step (FLOOR)?
    _left = None if remaining_s is None else remaining_s - _spent
    _retry = [r.task_spec for r in res if _retryable(r)]
    if _retry and _left is not None and _left < _PLAN_STEP_TIMEOUT_FLOOR_S:
        logger.info(
            "plan_driver.transient_retry_skipped plan=%s level=%s steps=%s left=%.0fs — 墙钟不够, 不重试",
            plan_id,
            lvl,
            [t.task_id for t in _retry],
            _left,
        )
        _retry = []
    if _retry:
        logger.info(
            "plan_driver.transient_retry plan=%s level=%s steps=%s — 契约说该 retry, 重派一次",
            plan_id,
            lvl,
            [t.task_id for t in _retry],
        )
        await _emit(
            hooks,
            "step_started",
            {"plan_id": plan_id, "level": lvl, "step_ids": [t.task_id for t in _retry], "retry": True},
        )
        # Redispatch timeout uses **actual remainder**, never initial generous budget already partly spent.
        if _left is not None:
            _retry = [replace(t, timeout_s=_step_timeout_s(_left, is_last_level=False)) for t in _retry]
        if deps.prepare_batch is not None:
            await deps.prepare_batch(_retry)
        _again = await deps.runner.dispatch(_retry, deps.sctx, deps.runtime_factory, deps.agent_runner_fn, deps.manifest_lookup)
        # Retry replaces first results (even still transient: fresher error, tokens
        # reflect actual second cost); unretried steps unchanged. No third call.
        _fresh = {r.task_spec.task_id: r for r in _again}
        res = [_fresh.get(r.task_spec.task_id, r) for r in res]

    await _writeback_results(
        res,
        results,
        counts,
        store=store,
        deps=deps,
        hooks=hooks,
        plan_id=plan_id,
        steps_by_id={s.id: s for s in runnable},
        held=held,
        all_steps_by_id=all_steps_by_id,
        children=children,
    )
    return (len(runnable), gated)


async def _caps_breach(
    store: PlanStore,
    plan_id: str,
    caps: PlanCaps,
    *,
    uid: str,
    wid: str,
) -> str | None:
    """返回越界原因 (max_total_tokens / max_wallclock) 或 None. max_steps 在入口查."""
    if caps.max_total_tokens is not None and await _spent_tokens(store, plan_id) > caps.max_total_tokens:
        return "max_total_tokens"
    if caps.max_wallclock_s is not None:
        p = await store.get_plan(plan_id, user_id=uid, workspace_id=wid)
        if p and _elapsed_s(p.execution_started_at) > caps.max_wallclock_s:
            return "max_wallclock"
    return None


async def run_plan(
    plan_id: str,
    *,
    store: PlanStore,
    deps: PlanDeps,
    hooks: PlanHooks,
    caps: PlanCaps,
    parent_token_budget: int,
) -> PlanRunOutcome:
    """执行(或 per-step 重试续跑)一个已批准的 plan。

    parent_token_budget 由调用方(orchestrator)显式传入 —— 不读无人写的
    settings_snapshot(CTO P0-3)。
    """
    uid = deps.sctx.user_id
    wid = deps.sctx.workspace_id
    counts = {"done": 0, "blocked": 0, "failed": 0, "skipped": 0}

    steps = await store.list_steps(plan_id)
    # A (editable): user-unchecked (included=False) → terminal SKIPPED, outside execution DAG.
    # Downstream with other successful upstream still runs (_precondition partial-ok); only wholly excluded
    # upstream blocks step (genuinely needed). Keep excluded rows in DB (get_plan returns; frontend dimmed/recheckable).
    for s in steps:
        if not s.included and s.status not in _TERMINAL_STEP:
            await store.update_step_status(s.id, status=StepStatus.SKIPPED)
        elif is_retired_plan_agent(s.agent_name) and s.status not in _TERMINAL_STEP:
            # Retired legacy-plan agents never dispatch: close skipped, not unknown_agent blockage.
            await store.update_step_status(s.id, status=StepStatus.SKIPPED, error="agent_retired", error_kind="agent_retired")
    # Batch 3 dual-list discipline (review R1-F6): scheduling=live rows (superseded excluded from max_steps,
    # or revision-history plans become sticky FAILED; also never inflate round limit);
    # alias-resolution=all rows (mirror needs superseded), injected into round kernel below.
    steps = [s for s in steps if s.included and s.status != StepStatus.SUPERSEDED]
    if not steps:
        await store.update_plan_status(plan_id, PlanStatus.FAILED)
        return PlanRunOutcome("failed", reason="empty_plan")
    if len(steps) > caps.max_steps:
        await store.update_plan_status(plan_id, PlanStatus.FAILED)
        return PlanRunOutcome("failed", reason="max_steps_exceeded")

    # nothing_to_run fast path (all terminal); round kernel anchors by frontier, no longer start.
    if await store.resume_level(plan_id) is None:
        return PlanRunOutcome("completed", reason="nothing_to_run")

    results = await _load_done_results(steps)
    # Reset wallclock start on every run_plan (first or re-approve resume): cap means
    # this execution turn's budget (inside loop hard timeout). Without reset, delayed retry
    # exceeds wallclock via old started_at → terminal FAILED; retry can never succeed.
    await store.set_execution_started_at(plan_id)
    await store.update_plan_status(plan_id, PlanStatus.RUNNING)
    # Frontend plan-store always had running (turn-store-dispatch case plan_running), but backend
    # never emitted; card skipped proposed→completed, other clients/replay missed RUNNING (dead event).
    # Emit start here, including resume. chat-solidify and origin planning fixed independently, converged.
    await _emit(hooks, "plan_running", {"plan_id": plan_id})

    # ── Batch 2 S1 round kernel (batch2-hitl-design §1-D4, finalized after four P0 scheduling reviews) ──
    # frontier-min round anchor + hold + reread each round. Equivalence: without hooks, initially all PENDING,
    # frontier visits 0..n exactly once per level, byte-identical to old for loop; this-run
    # BLOCKED/FAILED enter held, no redispatch (retry still next run_plan/re-approve);
    # sole same-run revival=revive_check(S4) and DONE-writeback unblock cascade.
    fail_reason: str | None = None
    held: set[str] = set()
    revived_guard: set[str] = set()  # Batch 2 S4: once per step/run.
    rounds = 0
    max_rounds = 2 * len(steps) + 8

    def _eligible(s: PlanStep) -> bool:
        return s.status not in _TERMINAL_STEP and s.id not in held

    while True:
        # Durable pull-based cancel (D-D6): read persisted state each round boundary.
        plan = await store.get_plan(plan_id, user_id=uid, workspace_id=wid)
        if plan is None or plan.status == PlanStatus.CANCELLED:
            fail_reason = "cancelled"
            break
        # ★Reread each round: fresh state (entry snapshot redispatches DONE/misses revivals; scheduling review P0-3).
        all_rows = await store.list_steps(plan_id)
        steps = [s for s in all_rows if s.included and s.status != StepStatus.SUPERSEDED]
        levels = _group_by_level(steps)
        # All-row map (excluded/superseded included): alias tail lookup + manifest bucketing need
        # skipped/excluded/superseded; cascade consumers look up live IDs only, dead entries harmless.
        all_by_id = {s.id: s for s in all_rows}
        children: dict[str, list[str]] = {}
        for s in steps:
            for dep in s.depends_on:
                children.setdefault(dep, []).append(s.id)
        # Merge historical done increments across runs (this-run writebacks already in results).
        for _sid, _r in (await _load_done_results(steps)).items():
            results.setdefault(_sid, _r)
        # ── Batch 3 revision aliases (batch3-replan-design D-A) ──
        # Downstream depends_on keeps old ID (no downstream rewrite); follow superseded_by chain to
        # live tail. Tail DONE injects mirror result under old ID (task_id=old ID feeds
        # filter_upstream_for/_precondition; agent ownership=tail, review R1-F8),
        # and aggregates old-ID children into tail (DONE-writeback unblock cascade uses it).
        _superseded_rows = [s for s in all_rows if s.status == StepStatus.SUPERSEDED]
        if _superseded_rows:
            _rows_by_id = {s.id: s for s in all_rows}
            for _old in _superseded_rows:
                _tail_id = _resolve_supersession_tail(_old, _rows_by_id)
                if _tail_id is None:
                    continue
                if _old.id in children:
                    _kids = children.setdefault(_tail_id, [])
                    _kids.extend(k for k in children[_old.id] if k not in _kids)
                _tail_res = results.get(_tail_id)
                _tail_row = all_by_id.get(_tail_id)
                if _tail_res is not None and _tail_res.ok and _tail_row is not None:
                    results[_old.id] = TaskResult.succeeded(
                        replace(step_to_task(_old), agent_name=_tail_row.agent_name),
                        _tail_res.payload,
                    )
        # Batch 2 S4 pickup: every round, including final frontier=None before convergence, answered
        # BLOCKED(needs_user_input) can revive. revived_guard permits once per step/run;
        # blocking again after e.g. failed ctx backfill cannot loop (final guard against P0-3
        # token-burning infinite loop).
        if hooks.revive_check is not None:
            _cands = [s for s in steps if s.status == StepStatus.BLOCKED and s.error_kind == "needs_user_input" and s.id not in revived_guard]
            if _cands:
                try:
                    _revived = [x for x in await hooks.revive_check(_cands) if x in {c.id for c in _cands}]
                except Exception as _re:
                    logger.warning("plan_midrun.revive_check_error plan=%s err=%s", plan_id, _re)
                    _revived = []
                for _rid in _revived:
                    await store.update_step_status(_rid, status=StepStatus.READY, clear_error=True)
                    held.discard(_rid)
                    revived_guard.add(_rid)
                    # Sync memory (DB already updated; this frontier sees READY).
                    _obj = next((s for s in steps if s.id == _rid), None)
                    if _obj is not None:
                        _obj.status = StepStatus.READY
                        _obj.error = None
                        _obj.error_kind = None
                # No new event: revival dispatch naturally emits step_started, flipping frontend state.
        frontier = next(
            (lvl for lvl, grp in enumerate(levels) if any(_eligible(s) for s in grp)),
            None,
        )
        if frontier is None:
            break
        remaining: float | None = None
        if caps.max_wallclock_s is not None:
            remaining = caps.max_wallclock_s - _elapsed_s(plan.execution_started_at)
        dispatched, gated = await _process_level(
            [s for s in levels[frontier] if _eligible(s)],
            frontier,
            results,
            counts,
            store=store,
            deps=deps,
            hooks=hooks,
            plan_id=plan_id,
            parent_token_budget=parent_token_budget,
            step_timeout_s=_step_timeout_s(remaining, is_last_level=(frontier == len(levels) - 1)),
            remaining_s=remaining,
            held=held,
            all_steps_by_id=all_by_id,
            children=children,
        )
        fail_reason = await _caps_breach(store, plan_id, caps, uid=uid, wid=wid)
        if fail_reason:
            break
        rounds += 1
        if dispatched == 0 and gated == 0:
            # Defensive postcondition: eligible steps must gate or dispatch; normally unreachable;
            # reachable means scheduling bug: log and converge (terminal check handles blocked), never spin.
            logger.warning("plan_driver.no_progress_round plan=%s round=%d", plan_id, rounds)
            break
        if rounds > max_rounds:
            # Guard against future status additions / faulty revival creating infinite loop.
            logger.error("plan_driver.rounds_guard_tripped plan=%s rounds=%d steps=%d", plan_id, rounds, len(steps))
            break

    if fail_reason:
        if fail_reason == "cancelled":
            # review-fix: durable cancel already set plans.status=CANCELLED (sticky).
            # Return a `cancelled` outcome (not "failed") so hook2 surfaces an aborted
            # (not error) turn — prior code returned "failed" → user saw a failed turn
            # for their own cancel. (update_plan_status(FAILED) was a no-op anyway.)
            return PlanRunOutcome("cancelled", reason="cancelled", spent_tokens=await _spent_tokens(store, plan_id), **_public_counts(counts))
        await store.update_plan_status(plan_id, PlanStatus.FAILED)
        await _emit(hooks, "plan_failed", {"plan_id": plan_id, "reason": fail_reason})
        return PlanRunOutcome("failed", reason=fail_reason, spent_tokens=await _spent_tokens(store, plan_id), **_public_counts(counts))

    # Terminal-state decision.
    final_steps = await store.list_steps(plan_id)
    unfinished = [s for s in final_steps if s.status not in _TERMINAL_STEP]
    spent = await _spent_tokens(store, plan_id)
    if unfinished:
        await store.update_plan_status(plan_id, PlanStatus.BLOCKED)
        await _emit(
            hooks,
            "plan_blocked",
            {
                "plan_id": plan_id,
                "blocked_step_ids": [s.id for s in unfinished],
            },
        )
        return PlanRunOutcome("blocked_needs_user", spent_tokens=spent, **_public_counts(counts))
    await store.update_plan_status(plan_id, PlanStatus.COMPLETED)
    # Coverage signal (review A8): card gets this-turn verdict counts; user distinguishes skipped/pass;
    # broken verifier configuration must not look all-green.
    completed_payload: dict[str, Any] = {"plan_id": plan_id}
    verify_counts = {k.removeprefix("verify_"): v for k, v in counts.items() if k.startswith("verify_")}
    if verify_counts:
        # Implementation review B7: verified=actually assessed (pass/partial/fail), excluding skipped;
        # "verified: 4" with four skips makes coverage signal falsely green.
        verify_counts["verified"] = sum(v for k, v in verify_counts.items() if k != VERDICT_SKIPPED)
        completed_payload["verify"] = verify_counts
    await _emit(hooks, "plan_completed", completed_payload)
    return PlanRunOutcome("completed", spent_tokens=spent, **_public_counts(counts))


def _public_counts(counts: dict[str, int]) -> dict[str, int]:
    """PlanRunOutcome 只认 done/blocked/failed/skipped —— verify_* 与内部 _ 键剔除."""
    return {k: v for k, v in counts.items() if k in ("done", "blocked", "failed", "skipped")}


async def _spent_tokens(store: PlanStore, plan_id: str) -> int:
    steps = await store.list_steps(plan_id)
    return sum(s.tokens_input + s.tokens_output for s in steps)
