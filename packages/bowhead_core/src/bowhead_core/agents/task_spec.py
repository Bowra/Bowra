"""TaskSpec / TaskResult / BusRef — TaskRunner 输入/输出 (PR-#5).

数据类型集中放此, 不依赖 turn_context (避免循环 import).

设计要点:
- `TaskSpec` 是 picklable (Celery 跨进程传递, T-CTO-P0.1)
- `TaskResult` 含 ok/payload/error 三态 + 统一 TaskFailureError wrap (T-BE-P1.4)
- `BusRef` 仅含 session_id + turn_id (worker 内 reconstruct 真 bus)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.agent_role_candidates import ExactAgentRoleChoice


@dataclass(frozen=True)
class BusRef:
    """跨进程 bus 引用 (worker 内 reconstruct TurnEventBus 用).

    SerializableTurnContext 携带此 ref, RuntimeTurnContext.reconstruct() 时用它
    建立真 bus 连接.
    """

    session_id: str
    turn_id: str


@dataclass(frozen=True)
class TaskSpec:
    """单 task 的执行规格.

    必须 picklable (Celery 跨进程). 不持有 runtime ref (client/bus 等).
    """

    agent_name: str
    user_question: str
    upstream_payloads: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    task_id: str = field(default_factory=lambda: f"t_{uuid.uuid4().hex[:12]}")

    # Optional hints (router reason / estimated tokens / estimated latency)
    routing_reason: str = ""
    tokens_estimate: int = 0
    latency_estimate_s: int = 0

    # v2 PR-#7: DAG dependencies. If present, dispatcher must finish depends_on before running this task.
    # Elements are other TaskSpec.task_id values. Empty list = parallel (default behavior).
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    # Planning Mode (2026-07): per-task timeout override (seconds). None/≤0 → runner uses
    # the compute_task_timeout(manifest) default. Plan execution steps are intentionally long tasks
    # (specialist branches actually take 8–15min); driver explicitly sets a value from remaining wall-clock budget,
    # rather than using chat-mode 500s/150s fallbacks (see plan_driver._step_timeout_s).
    timeout_s: int | None = None

    # ★2026-08-18 116 dogfood: **criteria never crossed this seam**. `success_criteria` had only
    # three repository uses: persistence, display to users on plan cards, and verifier judgment. Working agents never saw
    # their grading criteria. After enforcement shipped, 15/44 Top-N-name criteria failed; notes had
    # nearly identical shapes (payload names TOP1/TOP2/last place, never third place). Judge was correct; examinees never got the exam.
    # Driver includes criteria at dispatch (plan_reify.step_to_task); adapter inserts them into extra_instructions.
    # Empty tuple = ordinary chat dispatch (no criteria), byte-equivalent to previous behavior.
    success_criteria: tuple[str, ...] = field(default_factory=tuple)

    # ★2026-08-25 plan-report-assembly batch 3-a: **second instance of the same seam**.
    # `plan_steps.deliverable` is the planner's **output shape** (insight/table/chapter; since 2026-09-25, writer names chapters by content). Previously used as chapter titles ("industry macro benchmark table"
    # / "cross-validation benchmark conclusions", etc.), persisted and shown on plan cards, but working agents
    # never saw it. Batches 0/1/2 fixed assembly (116 t_c9853b490565: 8 reports became 1 report with 8 chapters),
    # yet assembly can downgrade **structure**, not **wording**: branches still write full-report scaffolds, self-referencing "this report",
    # each repeating global background and ending with global conclusions.
    # Empty string = ordinary multi-agent dispatch (planner omitted / non-plan step), byte-equivalent to previous behavior.
    # ⚠ Decision #4: **do not pass ordinal position**. Branches cannot see prior text; "chapter 3/8" would induce
    # "as stated earlier" references to content they never read.
    deliverable: str = ""

    # Planner-approved exact role holder.  None keeps every pre-M3 task byte-
    # compatible; a value requires Plan execution to pass immutable admission
    # before the peer loop starts.
    agent_choice: ExactAgentRoleChoice | None = None


class TaskFailureError(Exception):
    """统一错误 wrap (T-BE-P1.4) — LocalTaskRunner / CeleryTaskRunner 都返此.

    `reason`: 短分类 (TimeoutError / CancelledError / ValidationError / ...).
    `original`: 原始异常对象 (debug 用, 非必填 — 跨进程序列化时可能为 None).
    `detail`: 结构化附加信息 (JSON 字符串, 如 needs_user_input 的 clarification
    payload) — plan_driver 写 step 时透传, 非必填.
    """

    def __init__(
        self,
        reason: str,
        original: BaseException | None = None,
        *,
        detail: str | None = None,
    ) -> None:
        self.reason = reason
        self.original = original
        self.detail = detail
        super().__init__(f"{reason}: {original}" if original else reason)


class PlanStepError(Exception):
    """Plan 执行步的 typed 失败 (planning-mode-audit sprint P1-a/P1-b).

    子代理"技术上成功"(ok=True)但实质无效时, plan 执行 adapter
    (agent_runner_adapter._runner_fn) raise 本家族; task_runner._run_one 特判
    映射成干净 error_kind (否则泛型 wrap 会把异常类名当 kind 落库并泄进用户
    caveat), plan_driver._writeback_results 据 kind 决定 FAILED vs BLOCKED。
    普通 chat 路不用本家族 — subagent_spawn wrapper 有自己的
    subagent_empty_answer 守卫 (subagent.py), 语义等价。
    """

    error_kind: str = "plan_step_error"

    def __init__(self, message: str = "", *, detail_json: str | None = None) -> None:
        self.detail_json = detail_json
        super().__init__(message or self.error_kind)


class PlanStepEmptyOutputError(PlanStepError):
    """ok=True 但 payload 与 final_answer 双空 — 步没有产出任何东西.

    prod 实证 (discovery §1): 10 个 done-EMPTY 步全被计成"成功"并喂给
    canvas-writer 空上游 → 假成功报告。
    """

    error_kind = "empty_output"


class PlanStepBailError(PlanStepError):
    """payload 带 error/bail 信封 — 步"完成"了但内容是失败声明.

    subagent_return 的 error.kind / __terminal+bail_kind 信封 (subagent_return_tool)
    此前在 plan 路零消费 → datarc 故障日 7 步全"done"仍报"7 步成功" (review P0)。
    error_kind 取信封原 kind (per-instance 覆盖类属性)。
    """

    error_kind = "peer_bail"

    def __init__(self, kind: str = "", *, message: str = "", detail_json: str | None = None) -> None:
        # ★2026-08-14 116 dogfood: `message` was absent, so exception text equaled kind,
        # then `TaskFailureError.__str__` (`f"{reason}: {original}"`) produced **`kind: kind`**,
        # literally showing users "Failed: transient: transient" on plan cards.
        # Bail envelopes **already require readable reasons** (`analysis_payload` validation:
        # `error.message missing/empty (need 1-2 sentence reason)`); it was discarded here.
        # Passing it through yields, for example, "transient: BI query download timed out after 60s…".
        if kind:
            self.error_kind = kind
        super().__init__(message or kind or "peer_bail", detail_json=detail_json)


class PlanStepNeedsUserError(PlanStepError):
    """工具返回 needs_clarification sentinel — 步需要用户输入才能继续.

    典型: datarc 工作区未绑定时 bi_* 返回 tool_context_select picker。
    detail_json = clarification payload (JSON), writeback 落 plan_steps.error
    供 blocked message / FE 使用; 步落 BLOCKED (非 FAILED)。
    """

    error_kind = "needs_user_input"


class PlanStepAgentBindingError(PlanStepError):
    """The approved exact Agent is stale or no longer runnable.

    This is a recoverable Plan state: the user can revise/replan and approve a
    fresh candidate.  Keep the stable admission code in ``error_kind`` while
    the exception text remains safe, actionable copy for the plan card.
    """

    def __init__(self, code: str) -> None:
        self.binding_code = code
        self.error_kind = "agent_candidate_stale" if code == "agent_candidate_stale" else "agent_binding_unavailable"
        super().__init__(
            "所选 Agent 已发生变更或当前不可用，请重新规划并确认后再执行。",
        )


@dataclass
class TaskResult:
    """单 task 的执行结果.

    `ok=True` → payload 必填, error 必空.
    `ok=False` → error 必填 (TaskFailureError 实例), payload 可空.

    ⚠ "payload 必填"不在此处 enforce — plan 路的空产出闸在 adapter
    (PlanStepEmptyOutputError typed raise)。在 succeeded() 里 raise 会落在
    _run_one 的 try 之外, 经 asyncio.gather 炸穿整盘 plan (review 架构-#4),
    别加。
    """

    task_spec: TaskSpec
    ok: bool
    payload: dict[str, Any] | None = None
    error: TaskFailureError | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: int = 0

    @property
    def agent_name(self) -> str:
        """便利属性 (sprint-multi-agent.md §5.6 result aggregator 用)."""
        return self.task_spec.agent_name

    @classmethod
    def succeeded(
        cls,
        spec: TaskSpec,
        payload: dict[str, Any],
        *,
        tokens_input: int = 0,
        tokens_output: int = 0,
        duration_ms: int = 0,
    ) -> TaskResult:
        return cls(
            task_spec=spec,
            ok=True,
            payload=payload,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            duration_ms=duration_ms,
        )

    @classmethod
    def failed(
        cls,
        spec: TaskSpec,
        error: TaskFailureError,
        *,
        duration_ms: int = 0,
    ) -> TaskResult:
        return cls(
            task_spec=spec,
            ok=False,
            error=error,
            duration_ms=duration_ms,
        )
