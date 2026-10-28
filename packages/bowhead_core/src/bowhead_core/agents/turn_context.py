"""TurnContext 拆分: Serializable + Runtime (T-CTO-P0.1).

PR-#5: 仅类型定义 + 工厂方法。`reconstruct()` 真实接 datarc/router/bus 由
PR-#11 (turn_runner integration) 实现 — 现在仅占位接口.

为什么拆:
- LocalTaskRunner 共享 in-process refs (datarc/router/bus 都活), 不用 pickle
- CeleryTaskRunner v2 跨进程, 必须 picklable; runtime refs 在 worker 内重建
- v1 强制此拆分 = v2 切 Celery 时不重写 dispatcher

字段归属 (sprint-multi-agent.md §5.4.1 表):
- Serializable: ids / workspace_meta / upstream_payloads / handoff_chain /
  manifest_version_snapshot / settings_snapshot / plan_tier / handoff_depth
- Runtime: datarc_client / router / bus / canvas_store / pycell_counter_ref /
  _m137_subagent_payloads / _last_nested_invocations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .task_spec import BusRef


@dataclass(frozen=True)
class SerializableTurnContext:
    """Picklable, 跨进程 (Celery worker) 传递.

    所有字段必须 picklable (no client / no bus / no fn ref).
    """

    user_id: str
    workspace_id: str
    session_id: str
    turn_id: str
    role: str

    # Sprint θ Phase 8b-4 + Spike #1 finding fix: I4 invariant requires dispatcher
    # to propagate workspace_id + project_id + kb_role to peer parent_ctx.
    # Default None for graceful migration of existing callers; new turn entries set them.
    project_id: str | None = None
    kb_role: str | None = None  # "user-scoped" / "workspace-shared" / None
    # cca P3: team scope for team-subagent advertise/resolve across a Celery worker
    # boundary (in-process paths read parent_ctx.team_id; this carries it for workers).
    # Picklable (str|None) — round-trips via asdict + **kwargs. MUST be copied in
    # with_handoff below or it silently resets to None on a cross-domain handoff hop.
    team_id: str | None = None

    # Planning Mode (M1): non-None executes plan this turn (lead_orchestrator plan branch
    # replaces LLM loop). Picklable; with_handoff copies too, preserving nested plan-step handoff.
    plan_execution_id: str | None = None
    planning_mode: str | None = None  # "planning"/"off"/None(Auto): hook1 decision.

    # Fetched at turn start, immutable throughout turn.
    workspace_meta: dict[str, Any] = field(default_factory=dict)

    # Other-task typed payloads (canvas-writer / cross-domain handoff).
    upstream_payloads: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    # T-SEC-P0.2: cycle-detection key includes workspace_id.
    handoff_chain: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    handoff_depth: int = 0

    # T-CTO-P1.1: same manifest version throughout turn.
    manifest_version_snapshot: dict[str, int] = field(default_factory=dict)

    # Freeze critical settings (e.g. token budget caps), immune to midturn admin changes.
    settings_snapshot: dict[str, Any] = field(default_factory=dict)

    # Tier validation (free / pro / enterprise), user manifest gate.
    plan_tier: str = "free"

    # Cross-process bus ref (reconstructed in worker).
    bus_ref: BusRef | None = None

    def with_handoff(
        self,
        target_workspace_id: str,
        target_agent_name: str,
    ) -> SerializableTurnContext:
        """加一层 handoff_chain (用于 v2 cross-domain handoff)."""
        new_chain = (*self.handoff_chain, (target_workspace_id, target_agent_name))
        return SerializableTurnContext(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            role=self.role,
            project_id=self.project_id,  # Phase 8b-4 I4 invariant
            kb_role=self.kb_role,  # Phase 8b-4 I4 invariant
            team_id=self.team_id,  # cca P3: carry team scope across handoff (else resets to None)
            plan_execution_id=self.plan_execution_id,
            planning_mode=self.planning_mode,
            workspace_meta=self.workspace_meta,
            upstream_payloads=self.upstream_payloads,
            handoff_chain=new_chain,
            handoff_depth=self.handoff_depth + 1,
            manifest_version_snapshot=self.manifest_version_snapshot,
            settings_snapshot=self.settings_snapshot,
            plan_tier=self.plan_tier,
            bus_ref=self.bus_ref,
        )


@dataclass
class RuntimeTurnContext:
    """非 picklable, 仅 in-process / worker 内 reconstruct 后用.

    PR-#5: 占位接口 (字段类型用 Any). PR-#11 接真 datarc client / router / bus.
    """

    serializable: SerializableTurnContext

    # PR-#11 real wiring (DatarcClient / LLMRouter / TurnEventBus / CanvasStore).
    datarc_client: Any = None
    router: Any = None
    bus: Any = None
    canvas_store: Any = None

    # mutable refs (per-turn, in-process only)
    pycell_counter_ref: list[int] = field(default_factory=lambda: [0])
    m137_subagent_payloads: list[dict[str, Any]] = field(default_factory=list)
    last_nested_invocations: list[Any] = field(default_factory=list)

    @classmethod
    def reconstruct(cls, serializable: SerializableTurnContext) -> RuntimeTurnContext:
        """从 serializable 重建 runtime refs (worker entry point 用).

        PR-#5: 占位 (None refs). PR-#11 实现真 reconstruct.

        For LocalTaskRunner in-process 共享父进程 refs, 不调此方法; 由调用者
        直接传 RuntimeTurnContext 拼.
        """
        return cls(serializable=serializable)
