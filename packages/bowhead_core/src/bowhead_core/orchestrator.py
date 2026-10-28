"""Turn facade, runtime configuration, event construction and cancellation signals.

Gateway admission dispatches run_turn to TurnRunner and the current Lead loop.
TurnRunner and Lead own execution and durable terminal settlement.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any


class TurnCancelledError(Exception):
    """M2.1: 用户点 Stop, TurnRunner 检测到 cancel_event 抛此异常.

    设计文档: docs/M157-cancel-turn-design.md
    TurnRunner.run() 顶层 catch 这个异常 → emit turn_cancelled + done(status=cancelled).
    """


from bowhead_core.events import (
    EventBusRegistry,
    OrchestratorEvent,
    get_event_registry,
)
from bowhead_core.tool_registry import (
    ToolRegistry,
    build_default_registry,
)
from bowhead_core.types import (
    TurnRequest,
    TurnResult,
)

if TYPE_CHECKING:
    import asyncio

    from bowhead_bi_bridge import DatarcClient
    from bowhead_core.approval_store import ApprovalStore
    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.session_store import SessionStore
    from bowhead_core.user_memory import UserMemoryStore
    from bowhead_router import Router


# #123 Step 3 → final state on 2026-07-14: tool-loop free helpers moved to turn_runner.py with
# public names (data_attachment_ids / user_explicitly_wants_bi / abbrev_thinking, etc.).
# The re-export compatibility period ended (no old private-name callers remain); import public names from turn_runner.


@dataclass
class OrchestratorConfig:
    """Runtime guardrails shared by the current TurnRunner and Lead execution."""

    max_tool_iterations: int = 6
    max_output_tokens: int = 2000
    llm_agent_type: str = "lead"  # Router agent binding key


@dataclass
class Orchestrator:
    """Lead Agent 的运行时 runner.

    线程模型：每个 turn 独立走一次 ``run_turn``；Orchestrator 实例本身可共享。
    可选 ``user_memory_store`` / ``session_store`` —— 配置后会：
        - 注入 User Memory 到 Lead system prompt
        - 持久化 session / turn 到 SQLite（驱动前端 /sessions API）
    """

    router: Router
    # Gateway binds the caller team's connection on each TurnRequest. The
    # orchestrator intentionally has no deployment-wide Datarc client.
    datarc: DatarcClient | None
    tool_registry: ToolRegistry = field(default_factory=build_default_registry)
    event_registry: EventBusRegistry = field(default_factory=get_event_registry)
    config: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    user_memory_store: UserMemoryStore | None = None
    session_store: SessionStore | None = None
    personal_mcp_license_check: Callable[[str], Awaitable[bool]] | None = None
    canvas_store: CanvasStore | None = None
    approval_store: ApprovalStore | None = None
    notification_service: Any | None = None
    approval_gate: Any | None = None
    dreaming_light: Any | None = None  # bowhead_memory.DreamingLight
    # V3.5 PRD-C
    nl_memory_intent_classifier: Any | None = None  # bowhead_core.NLMemoryIntentClassifier
    memory_candidate_store: Any | None = None  # bowhead_memory.MemoryCandidateStore
    memory_notes_store: Any | None = None  # bowhead_core.MemoryNotesStore
    nl_intent_enabled: bool = False
    # V4 PRD-D-4-4
    memory_pack_snapshots_store: Any | None = None  # bowhead_core.MemoryPackSnapshotsStore
    # Memory redesign batch 0④: whether turn-start memory snapshots are stored in memory_pack_snapshots. Together with
    # BOWHEAD_REPLAY_PCT, this illustrates a dead flag: `_PCT` defaults to 0 (neither key exists in production
    # ⇒ always 0 rows). This repository already paid for this pattern once; do not duplicate it.
    memory_pack_snapshots_enabled: bool = False
    space_instructions_store: Any | None = None  # bowhead_core.SpaceInstructionsStore
    # V5 PRD-E2: inline tool result preview gate.
    inline_preview_enabled: bool = False
    metrics_log: Any | None = None  # bowhead_core.MetricsLog
    vector_client: Any | None = None  # bowhead_memory.VectorClient（M2.6a）
    artifact_store: Any | None = None  # bowhead_core.ArtifactStore (attachment reads)
    skill_instance_store: Any | None = None  # bowhead_core.SkillInstanceStore — per-user skill filtering
    skill_catalog: Any | None = None  # bowhead_skill.SkillCatalog — skill_save_generated refresh
    custom_subagent_store: Any | None = None  # #127 CustomSubagentStore — user-defined subagents
    # M156: term resolver dependencies
    schema_cache: Any | None = None  # bowhead_core.datarc_schema_cache.DatarcSchemaCache — TTL cache
    clarification_store: Any | None = None  # bowhead_core.ClarificationStore — M3b HITL requests
    kb_retriever: Any | None = None
    kb_files_store: Any | None = None
    kb_doc_type_store: Any | None = None
    kb_extracted_record_store: Any | None = None
    kb_extracted_rows_store: Any | None = None
    # δ Sprint: RegisteredFactStore for auto_learner / pre-LLM fact injection.
    # If None, turn_runner builds Noop lifecycle (δ disabled for this Orchestrator).
    fact_store: Any | None = None  # bowhead_core.RegisteredFactStore
    # cca LOAD POINT: per-team tool sources + decrypted-auth cache, for injecting an team's
    # `teammcp.*` tools into the turn's tool view. Both None ⇒ team-tool loading is off
    # (native-only), so existing single-tenant / dev Orchestrators are behavior-identical.
    user_store: Any | None = None  # bowhead_core.UserStore (team_tool_source CRUD)
    tool_auth_registry: Any | None = None  # bowhead_core.ToolAuthRegistry (④d)
    # Sprint ar2: datarc tenant for embed collection naming + per-ws calibrated thresholds
    # gateway lifespan sets these (from settings.vector_db_tenant + ProjectMemoryStore kind="ws_threshold")
    # Empty by default = unpinned (determine from actual connection checks). The previous default was "demo" and was never assigned anywhere,
    # so every deployment built collection names with demo; see tool_registry.ToolExecutionContext.tenant.
    datarc_tenant: str = ""
    ar2_thresholds_by_ws: dict[str, dict[str, float]] = field(default_factory=dict)
    # M2.1: cancel signal registry for running turns. TurnRunner.run registers an asyncio.Event at startup
    # and unregisters it in finally. POST /turns/:tid/cancel → signal_cancel(tid) → the next boundary check raises
    # TurnCancelledError. See docs/M157-cancel-turn-design.md.
    _cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
    # Terminal v1 reuses one turn_id across generations.  Its cancellation
    # registry therefore needs the generation identity; otherwise an old Web
    # request authorized for N can race with N+1 registration and kill N+1.
    _terminal_cancel_events: dict[tuple[str, int], asyncio.Event] = field(default_factory=dict)
    # Feishu 07.06 #7: running planning-mode execution turn → its active plan_id. TurnRunner registers/unregisters
    # this during the turn lifecycle. cancel_turn uses it to cascade durable plan cancellation (Stop also marks the plan
    # CANCELLED); otherwise only the current turn stops while run_plan continues in the background / revives after F5.
    _executing_plans: dict[str, str] = field(default_factory=dict)

    def signal_cancel(self, turn_id: str) -> bool:
        """请求 cancel 跑动中的 turn. 返 True = 信号已发送; False = 该 turn 没在跑.

        幂等: 重复调对已 cancelled 的 turn 仍返 True (event.set 幂等).
        """
        ev = self._cancel_events.get(turn_id)
        if ev is None:
            return False
        ev.set()
        return True

    def is_cancelled(self, turn_id: str) -> bool:
        """供 TurnRunner 在 boundary 查 cancel 状态."""
        ev = self._cancel_events.get(turn_id)
        return ev is not None and ev.is_set()

    def signal_cancel_generation(self, turn_id: str, generation: int) -> bool:
        """Signal only the exact running Terminal Protocol generation."""

        if not isinstance(turn_id, str) or not turn_id or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return False
        ev = self._terminal_cancel_events.get((turn_id, generation))
        if ev is None:
            return False
        ev.set()
        return True

    def get_executing_plan(self, turn_id: str) -> str | None:
        """飞书07.06 #7: 返回该 turn 正在执行的 plan_id, 无则 None。

        供 gateway cancel_turn 级联 durable 取消: 点"停止"时若该 turn 在跑计划,
        同时把 plan 盖 CANCELLED 章 → run_plan 下个 level 边界自停 + F5 不复活
        (取消执行按钮已删, 停止是唯一入口)。"""
        return self._executing_plans.get(turn_id)

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        """Entry point. Thin facade → `TurnRunner.run` (#124 + #128 Step 3c).

        Full body (~940 行) 已 inline 到 `bowhead_core.turn_runner.TurnRunner.run`.
        这里只做 circular-import 规避的 lazy import + 委托。这是低层 worker
        body；生产 chat 只能由 Gateway `spawn_turn` 在 admission/dispatch 成功后调用。

        M158 FU-2 paradigm v2 ramp gate metric: 收到 TurnResult 后 observe
        duration / token_input / token_output histogram (按 paradigm_v2 flag 分组).
        """
        from bowhead_core.turn_runner import TurnRunner  # avoid circular

        result = await TurnRunner(self).run(request)
        # FU-2: key ramp gate metric histogram
        try:
            from bowhead_core import metrics as _m

            labels = {
                "flag": "on" if request.paradigm_v2 else "off",
                "intent": (result.intent.value if result.intent else "unknown"),
                "status": result.status.value if hasattr(result.status, "value") else str(result.status),
            }
            _m.observe(
                "bowhead_turn_duration_seconds",
                value=(result.duration_ms or 0) / 1000.0,
                labels=labels,
                help="paradigm v2 turn 耗时 (s, paradigm_v2 vs reactive 对比)",
            )
            _m.observe(
                "bowhead_turn_tokens_input",
                value=float(result.tokens_input or 0),
                labels=labels,
                help="paradigm v2 turn input token (省 token 验证)",
                buckets=(1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000),
            )
            _m.observe(
                "bowhead_turn_tokens_output",
                value=float(result.tokens_output or 0),
                labels=labels,
                help="paradigm v2 turn output token",
                buckets=(100, 500, 1000, 2500, 5000, 10000, 25000, 50000),
            )
        except Exception:  # noqa: S110
            pass
        return result

    # ─────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _event(turn_id: str, event_type: str, payload: dict[str, Any]) -> OrchestratorEvent:
        return OrchestratorEvent(
            event_type=event_type,  # type: ignore[arg-type]
            turn_id=turn_id,
            payload=payload,
        )


# data_attachment_ids / user_explicitly_wants_bi / safe_preview / result_meta
# Moved to turn_runner.py (public names); re-exports were removed when the compatibility period ended.
