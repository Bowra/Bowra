"""Bowhead Core 的核心数据结构.

覆盖一个 turn 内 Orchestrator 需要的最小实体：
    - Turn / TurnStatus
    - ToolInvocation
    - OrchestratorEvent（对应 TD §2.4 SSE event 类型）

M1 Day 1 只实现单 turn + 单 Lead（不 spawn Subagent），所以暂不涉及 Plan / AgentCall
的持久化结构。下一阶段（Day 2+）补 TD §1 里的完整表结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from bowhead_core.agent_draft_send_target import AgentDraftSendTarget
from bowhead_core.context_action import ContextMaintenanceAction
from bowhead_core.document_chat_continuation import DocumentPlanningCheckpointRef
from bowhead_core.document_revision_target import DocumentRevisionTarget

if TYPE_CHECKING:
    from bowhead_core.agent_execution_binding import TurnAgentBindingInput
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceScope
    from bowhead_core.datarc_helper_switch_continuation import DatarcHelperSwitchResolution
    from bowhead_core.datarc_switch_continuation import DatarcSwitchResolution
    from bowhead_core.language_context import LanguageContext
    from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCheckpoint


# ─────────────────────────────────────────────────────────────
# ID helpers
# ─────────────────────────────────────────────────────────────
def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


_TS_COUNTER = 0  # Monotonically increasing sequence within a microsecond guarantees globally unique occurred_at.


def utc_now_iso() -> str:
    """进程级全局单调时间戳，微秒 + 纳秒 tie-breaker.

    用途：SSE replay / live dedupe 用 (event_type, occurred_at) 做 key. 仅微秒
    精度在同一微秒多 emit 的并发场景下仍可能撞键（Python call overhead 通常 > 1µs
    但不保证），加进程级单调计数器确保 100% 唯一.

    输出形如 `2026-04-22T00:01:02.123456+00:00#42`，
    `#N` 后缀被当作 dedup 字符串的一部分，不破坏 ISO 时间 prefix 排序.
    """
    global _TS_COUNTER
    _TS_COUNTER += 1
    return datetime.now(tz=UTC).isoformat(timespec="microseconds") + f"#{_TS_COUNTER}"


# ─────────────────────────────────────────────────────────────
# Turn
# ─────────────────────────────────────────────────────────────
class TurnStatus(StrEnum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Sprint θ Phase 7.5-E P0 #2 fix: explicit Lead orchestrator status values.
    CLARIFICATION_PENDING = "clarification_pending"  # heuristic OR sentinel abort
    ABORTED = "aborted"  # alias for user_cancel — frontend treats same as CANCELLED


# A worker has relinquished execution ownership for every non-running state.
# This is intentionally broader than an SSE/business "terminal" set:
# awaiting_user / awaiting_approval are durable pause facts whose worker and
# dispatch lease must settle, while a later continuation starts separately.
TURN_WORKER_SETTLED_STATUSES: frozenset[str] = frozenset(status.value for status in TurnStatus if status is not TurnStatus.RUNNING)

# Legacy Turn rows predate the Terminal Protocol wire vocabulary. Keep their
# persisted aliases and client-facing status normalization in one core truth so
# Store recovery, dispatch monitoring, and F5 replay cannot drift apart.
LEGACY_TURN_WORKER_SETTLED_STATUSES: frozenset[str] = TURN_WORKER_SETTLED_STATUSES | frozenset({"approval_pending", "error", "user_cancelled"})


def normalize_legacy_turn_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return {
        "error": "failed",
        "user_cancelled": "cancelled",
        "awaiting_user": "clarification_pending",
        "awaiting_approval": "approval_pending",
    }.get(value, value)


class IntentType(StrEnum):
    LOOKUP = "lookup"
    TREND = "trend"
    COMPARISON = "comparison"
    CAUSAL = "causal"
    COMPUTE = "compute"
    REPORT = "report"
    MODELING = "modeling"
    AMBIGUOUS = "ambiguous"


class ResponseKind(StrEnum):
    """Sprint ν2 — 决定 response 走 chat 气泡 OR canvas 报告.

    CHAT: Lead prose 直答 (feasibility/definition/lookup/modeling/ambiguous);
          MUST NOT subagent_spawn data peer.
    CANVAS: 现 default (trend/comparison/causal/compute/report); spawn peer +
            composer 出报告.
    """

    CHAT = "chat"
    CANVAS = "canvas"


@dataclass
class TurnRequest:
    """用户发起一个 turn 的输入."""

    session_id: str
    workspace_id: str
    user_id: str
    member_uid: str  # datarc member uid (from user memory)
    text: str
    context_maintenance: ContextMaintenanceAction | None = None
    turn_id: str = field(default_factory=lambda: _new_id("t"))
    # Durable same-Turn resumes increment this fence before their worker starts.
    # Fresh turns remain generation 1; cancellation usage finalization must
    # match the worker generation so a stale runner cannot enrich a later Stop.
    execution_generation: int = 1
    # Live server claim only; never accepted from a browser or restored from history.
    execution_owner_id: str | None = field(default=None, repr=False, compare=False)
    # (merge dedupe 2026-06-05: the feishu branch carried thin copies of
    # active_datarc_project / datarc_client / datarc_unavailable here; the canonical,
    # fully-documented definitions live further down — single source, same defaults.)
    attachments: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    # #109: frontend profile passthrough (language / answer_style / display_name preferences)
    user_profile: dict[str, str] | None = None
    # i18n v1: three language axes + week_start_day (resolved/injected by gateway chat.py route)
    # Legacy callers omit it; orchestrator falls back to LanguageContext.default(), preserving behavior.
    language_context: LanguageContext | None = None
    # The viewer's IANA zone when ``language_context`` cannot carry it (workspace i18n
    # disabled ⇒ no language snapshot). ``language_context.time_zone`` wins when set;
    # read both through ``model_clock.request_time_zone_name``. "" = deploy clock.
    viewer_time_zone: str = ""
    # M2.2: user-preferred (provider, model) from Composer model picker; affects only Lead/Composer.
    # routing_resolver (M2.3) priority: admin override > user_pref > workspace.default > yaml.
    # See docs/M157-routing-user-pref-design.md.
    preferred_provider: str | None = None  # e.g. "aliyun_qwen" / "ollama_local" / "custom_<id>"
    preferred_model: str | None = None  # e.g. "qwen-plus" / "llama3:70b"
    # M158 paradigm v2: workspace flag selects Lead five-step method + tool-allowlist enforcement.
    # Default False = reactive Lead (current production behavior); True = paradigm v2 (Lead manager + spawn).
    # Gateway chat.py injects workspace_settings.paradigm_v2_enabled; see docs/M158-architecture-clarity.md.
    paradigm_v2: bool = False
    # M158-12: full workspace_settings kv (i18n_v1 / paradigm_v2_enabled / default_time_window /
    # key_metrics / known_dimensions / internal_definitions / etc.), prefetched by chat.py,
    # passed by turn_runner to build_lead_system_prompt for workspace_context injection.
    workspace_settings: dict[str, str] | None = None
    # workspace-datarc-decouple P1 (R8): the datarc project THIS turn's BI tools query.
    # Resolved at gateway turn-start from ProjectMemoryStore (datarc_pref/default_project).
    # None pre-migration → ToolExecutionContext.resolve_datarc_project()/_ws fall back to
    # workspace_id (== today's datarc id). Post-decouple workspace_id is a Bowhead ws_*.
    active_datarc_project: str | None = None
    # Batch 2 (workspace-space-split PRD §3.3b/§3.11): gateway injects in per-user mode
    # selected workspace display name (provenance result-card labeling) + user's accessible-set snapshot
    # ({"id","name"} dict tuple for dispatch-gate sentinel options; core lacks the store,
    # so names must enter with the turn). Legacy mode keeps defaults, with no behavior.
    active_datarc_workspace_name: str = ""
    accessible_datarc_workspaces: tuple = ()
    # bi-absent-turn R3.1: BI-absence reason enum (gateway datarc_context-produced, passed to
    # ToolExecutionContext to select wording; "" = unknown/not applicable).
    datarc_absence_reason: str = ""
    # Batch 2/c6 (PRD §3.4): gateway-injected switch submission callback (live object, see
    # ToolExecutionContext.datarc_context_switcher). None = channel cannot switch.
    datarc_context_switcher: Any = field(default=None, repr=False, compare=False)
    datarc_binding: Any = field(default=None, repr=False, compare=False)
    datarc_source_evidence: dict | None = field(default=None, repr=False, compare=False)
    datarc_switch_resolution: DatarcSwitchResolution | None = field(default=None, repr=False, compare=False)
    datarc_helper_switch_resolution: DatarcHelperSwitchResolution | None = field(default=None, repr=False, compare=False)
    # Server-bound current-viewer projection for chart authoring; never model input.
    chart_authoring_projection: Any = field(default=None, repr=False, compare=False)
    # scheduled-recipe-runs S1: solidified workflow bound to this turn (``workflow_binding.BoundWorkflow``).
    # **Server-prefetched**: runner already checked parsing/fingerprint/admission; Lead executes it directly,
    # without rereading by name (bypasses all checks; Lead structurally lacks skill_read).
    # None = ordinary turn. turn_runner stores it in parent_ctx; lead_orchestrator consumes it.
    bound_workflow: Any = field(default=None, repr=False, compare=False)
    # M3-8: one exact Agent binding envelope for every Turn entry point.  It is
    # a live ``TurnAgentBindingInput`` object, never serialized into resume
    # context or model input.  None means this entry did not explicitly bind a
    # root Agent; ordinary Lead routing remains unchanged.
    agent_binding: TurnAgentBindingInput | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # Server-only initial-reference adapter state; never decoded from resume or
    # untrusted model arguments. The runtime rechecks the persisted lease.
    reference_scope: ReferenceScope | None = field(default=None, repr=False, compare=False)
    reference_runtime: ReferencePrimaryRuntime | None = field(default=None, repr=False, compare=False)
    reference_clarification_id: str | None = field(default=None, repr=False, compare=False)
    # Server-owned original card for the shared bounded-question resume fence.
    resume_clarification_id: str | None = field(default=None, repr=False, compare=False)
    # Server-only projection of original resolved cards. User data for Lead,
    # never canonical request evidence or a persisted continuation authority.
    resume_answer_context: str | None = field(default=None, repr=False, compare=False)
    # Original retained Lead state is loaded by the server after scope checks.
    ordinary_clarification_id: str | None = field(default=None, repr=False, compare=False)
    ordinary_lead_checkpoint: OrdinaryLeadCheckpoint | None = field(default=None, repr=False, compare=False)
    # Server-injected storage for fresh ordinary Web Chat collaboration.
    # Resumes reopen an existing admission instead of granting a new one.
    collaboration_store: Any = field(default=None, repr=False, compare=False)
    # Server-only checkpoint dispatch; never reconstructed as an ordinary Lead request.
    document_clarification_id: str | None = field(default=None, repr=False, compare=False)
    document_checkpoint: DocumentPlanningCheckpointRef | None = field(default=None, repr=False, compare=False)
    # V4 PRD-D-4-4: team_id for memory_pack_snapshots admin scope filtering.
    # NULL/omitted = orphan (dev/legacy/no-team user), hidden from admin paths. See PRD-D-4-4 §5.2.
    team_id: str | None = None
    # Planning Mode (M1, block 4): non-None makes this turn **execute** the plan (run_plan replaces Lead LLM loop
    # as turn body), reusing bus/persistence/canvas/credit. None = ordinary turn. See m1-block4 §2.
    plan_execution_id: str | None = None
    # Extra trusted seeds for URL provenance gate (user-authored text). Plan execution turn's ``text`` is a placeholder;
    # routes supply recent session user messages, or previously pasted links appear unseen during execution steps.
    provenance_seed_text: str = ""
    # Browser approval precondition; retained in the existing admission intent.
    expected_plan_revision: int | None = None
    # Planning Mode trigger (FE selector): "planning"=force plan / "off"=disabled / None=Auto (complexity-based).
    planning_mode: str | None = None
    # workspace-permissions M0/M1: server-derived access snapshot for this turn.
    # Serializable dict only; gateway owns construction, core/tool runtime consumes.
    access_context: dict[str, Any] | None = None
    # workspace-datarc-decouple P2a (③/R7.1): the per-connection DatarcClient resolved at the
    # gateway from the CALLER's team connection (DatarcConnectionStore, keyed by the same
    # fail-closed team_id above) via DatarcClientFactory. None → turn_runner falls back to the
    # process-global env client (orch.datarc) = pre-decouple behavior. In-memory only (live
    # object; never serialized — repr/compare excluded). Typed Any to avoid a core→bi_bridge dep.
    datarc_client: Any = field(default=None, repr=False, compare=False)
    # Power BI adapter: per-turn usable or finite-unavailable handle resolved gateway-side.
    # None is reserved for feature-off / no team scope; the pbi_* gate still refuses it.
    # In-memory live object (repr/compare excluded). Typed Any.
    # ws-datarc-decouple [review P1-b]: True when the team HAS a datarc connection but resolving it
    # FAILED — turn_runner must then FAIL CLOSED (ctx.datarc=None, NO global-env fallback) so the
    # team's queries don't silently hit the shared datarc (tenant isolation). Default False =
    # behavior-preserving (no connection / no failure → global fallback as before).
    datarc_unavailable: bool = False
    # embed-via-openapi (ws-datarc-decouple follow-up): per-turn VectorClient for the BI semantic
    # dictionary (concept-resolve / term-resolver / ar2 — schema+keywords collections), a
    # datarc-proxy client built from the SAME team connection as `datarc_client` above
    # (`/openapi/v1/embed/`). None → turn_runner falls back to the process-global direct embed
    # client (orch.vector_client) = pre-decouple behavior. Embed is nice-to-have: resolution
    # failure NEVER fails the turn and (unlike datarc above) has no fail-closed flag — the strict
    # no-global-fallback cut lands with P4 together with datarc's. In-memory only; typed Any.
    vector_client: Any = field(default=None, repr=False, compare=False)
    # BYO tier (P4.5.25 → byo-no-clone): the team's per-turn LLM Router / KB retriever,
    # gateway-resolved from the team's llm_config (TeamRouterRegistry) / kb runtime. For a request
    # with team_id, team_router=None is invalid and TurnRunner fails closed; only an explicit
    # no-team dev/legacy request may use process-global orch.router. team_kb_retriever remains
    # optional and may fall back to orch.kb_retriever. This is a
    # per-turn override that rides on the REQUEST (like datarc_client / vector_client
    # above), NOT via reconstructing the shared Orchestrator singleton: `dataclasses.replace(orch,
    # router=…)` silently dropped orch's dynamically-attached sidecars (subagent_runner /
    # multi_agent_registry / plan_store / scheduled_store / artifact_fetch_secret /
    # product_terms_catalog) → every BYO-team turn's subagent_spawn crashed with
    # "subagent_runner/bus not configured on context". Carrying the router here keeps the singleton
    # untouched (concurrency-safe). Steer-drain continuations inherit these via
    # dataclasses.replace(req); clarification-resume (web + Feishu) re-resolves them. (Background
    # scheduled/plan entry points don't carry BYO yet — separate pre-existing gap.) In-memory.
    team_router: Any = field(default=None, repr=False, compare=False)
    team_kb_retriever: Any = field(default=None, repr=False, compare=False)
    # Conversational Subagent drafting is assembled by the authenticated
    # Gateway request after team Router/entitlements resolve. Live-only:
    # never serialized into resume_context or turn persistence.
    subagent_draft_service: Any = field(
        default=None,
        repr=False,
        compare=False,
    )
    subagent_draft_mcp_entitled: bool = False
    # Authenticated, owner-scoped editor for the exact draft/version attached
    # to this web submission. Live-only; no draft body travels in chat events.
    subagent_draft_editor: Any = field(default=None, repr=False, compare=False)
    # Durable identity for start/recovery comparison; it grants no editor access.
    agent_draft_target: AgentDraftSendTarget | None = field(default=None, repr=False)
    document_revision_target: DocumentRevisionTarget | None = field(default=None, repr=False)
    agent_coach_resources: Any = field(default=None, repr=False, compare=False)
    # Terminal Protocol v1: generation-scoped runtime capability injected after Gateway admission/claim;
    # live request only, never resume_context, logs or repr.
    terminal_runtime: Any = field(default=None, repr=False, compare=False)
    # Terminal v1 authorization snapshot captured *before* the complete Gateway
    # authorization read. Admission re-reads the fixed vector in its own write
    # transaction, closing the revoke-between-preflight-and-admission window.
    # Live-only; never serialize this digest into resume or user-visible state.
    terminal_auth_fence_vector_digest: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # Config copilot batch 0.5 (tech-design-batch05-chat.md §5.2): channel supports lightweight chat inline confirmation
    # (config_propose channel gate). Only web constructors (routes/chat.py main path + routes/clarifications.py
    # clarification resume) set True; Feishu/scheduled/plans/workflow_solidify
    # stay False → structured propose rejection. steer inherits via dataclasses.replace(req); bool has no serialization leakage.
    config_lite_supported: bool = False
    # Live-only, authenticated settings selection; default entry points carry no binding.
    configuration_settings_target: Any = field(default=None, repr=False, compare=False)
    coach_canvas_target: Any = field(default=None, repr=False, compare=False)
    # V7 kb-agentic-fix: caller user RBAC role for KB ScopeContext. V1 mock uses
    # gateway/routes/kb.py _resolve_role(user) (BOWHEAD_AUTO_ADMIN_DOMAINS env);
    # Switch to JWT roles claim after real auth ships. "user" / "ws_admin" / "team_admin".
    kb_role: str = "user"
    # Sprint chat-v4-streaming Phase 4C — HITL same-turn-resume. When user
    # resolves a clarification, gateway /api/v1/clarifications/{id}/resolve
    # spawns a new orchestrator task with turn_id == resume_turn_id (same as
    # original turn), and ClarificationRequest.resume_context.user_text is
    # injected into Lead extra_instructions as the resolved-glossary hint.
    # None = fresh turn or pre-flight classification.
    resume_turn_id: str | None = None
    # Sprint chat-v4-streaming Phase 4C — resolved-glossary payload for resume.
    # Set by clarifications.resolve route when constructing the resume TurnRequest.
    # Lead orchestrator reads this and prepends a "user resolved ambiguity:
    # <key>=<value>" hint to extra_instructions so the second pass classifies
    # without asking again.
    resume_glossary: dict[str, str] | None = None
    # 2026-08-11: whether this turn belongs to a **user-interactive session** (shown in the sidebar).
    # Sole current consumer: `TurnRunner._spawn_session_title` (AI title on first turn).
    #
    # Must declare explicitly: `_run_inner` is shared by **all** turn producers
    # (chat / scheduled tasks / workflow dry runs / IM), with **no reliable source marker** today.
    # Independent review found scheduled ticks create new `s_sched_*` sessions; each dry run also creates
    # scratch sessions. Both count as first turns, wasting an LLM title-generation call every run.
    # `sessions.origin` cannot distinguish them (all 101 scheduled sessions on 116 had `normal` origin);
    # using incidental fields such as `ws_enforce` is a duck-typed bypass expressly forbidden by namespace guards.
    #
    # Default False = no title generation. New producers must **declare** this capability, not inherit by default;
    # omission loses a nice title, while erroneous inheritance costs money every tick.
    interactive_session: bool = False
    # Original human web ingress only; management writes retain this evidence on the Turn.
    knowledge_user_request: bool = False
    # Sprint chat-redesign-2026-05-21 Phase 1.1 — L3 HITL picker hint (P0-7).
    # Set by gateway chat.py when the user message is empty + attachments present.
    # Lead injects this into system prompt (via prompts/lead.py
    # _L3_PICKER_HINT_TEMPLATE) so the first turn issues a clarification_ask
    # with the suggested candidates rather than guessing intent. Candidates
    # are mime-derived (CSV/XLSX → "做个分析" / "收进知识库" / ...).
    l3_picker_hint: list[str] | None = None


@dataclass
class QueryOutline:
    """M157: 用户 prose 里抽出的报告骨架, 注入 subagent 作硬约束.

    tier_count = 0 表示无显式骨架, subagent 自由发挥.
    """

    tier_count: int = 0
    tier_titles: list[str] = field(default_factory=list)
    split_dims: list[str] = field(default_factory=list)
    required_kpis: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)


@dataclass
class ToolAnnotations:
    """Behavioural metadata for a tool, mirroring MCP ToolAnnotations
    (modelcontextprotocol). All fields are HINTS — advisory, never a hard
    authorization/routing gate (per the MCP spec: clients must treat annotations from
    untrusted servers as untrusted). team/MCP tools' self-declared values are read in
    verbatim; built-ins are seeded by tool_registry.apply_tool_annotations.
    """

    title: str | None = None  # MCP title
    read_only: bool | None = None  # MCP readOnlyHint — does not modify its environment
    destructive: bool | None = None  # MCP destructiveHint — destructive vs additive updates
    idempotent: bool | None = None  # MCP idempotentHint — repeated call = no extra effect
    open_world: bool | None = None  # MCP openWorldHint — external/web (True) vs closed (False)


@dataclass
class ToolInvocation:
    """一次 tool 调用的记录."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str = field(default_factory=lambda: _new_id("tl"))
    result: Any = None
    error: str | None = None
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str = ""
    duration_ms: int = 0
    # For `subagent_spawn`, nested holds the subagent's internal tool list.
    # After compose_answer flattens it, mapper sees the actual bi_ask_question /
    # bi_attribute data (fixes subagent_spawn swallowing 18 tool-data results).
    nested: list[ToolInvocation] = field(default_factory=list)
    # tool-domain (D1): the source/world this invocation acted on, stamped at
    # ToolRegistry.execute from the resolved ToolSpec (fast-path cache; the classifier
    # floor is tool_category.lookup_domain by name). open_world mirrors MCP openWorldHint.
    open_world: bool | None = None
    source: str | None = None


# ─────────────────────────────────────────────────────────────
# Turn digest: next-turn Lead sees prior session questions and datarc interpretations
# ─────────────────────────────────────────────────────────────
@dataclass
class TurnDigest:
    """一个 turn 的结构化摘要. 两种 mode：

    - **simple**：单次 `bi_ask_question` 为主的轻量 turn. 直接搬 datarc 的
      `explanations_struct[0]` 字段 —— 最准，0 LLM.
    - **aggregate**：subagent spawned 或 ≥3 次 ask_question 的复杂 turn.
      聚合统计本轮覆盖了哪些 metric / dim / time 范围，不保留 per-query 细节.
    - **empty / failed**：无 tool 调用 或 全失败. 轻量标记，Lead 见就知道别继承.

    存储：`turns.digest_json` 列（SQLite 单列 JSON）.
    注入：下一轮 Lead system prompt 的 `<conversation_context>` 段.
    """

    turn_id: str
    user_text: str
    mode: Literal["simple", "aggregate", "empty", "failed"]

    # --- simple mode only ---
    metrics: list[dict[str, Any]] = field(default_factory=list)  # explanations.measures
    dimensions: list[dict[str, Any]] = field(default_factory=list)  # explanations.dimensions
    d_filter: list[dict[str, Any]] = field(default_factory=list)  # explanations.filters
    m_filter: list[dict[str, Any]] = field(default_factory=list)  # explanations.having
    time_range: list[dict[str, Any]] = field(default_factory=list)  # explanations.date_filters
    time_granularity: str | None = None  # Year/quarter/month/week/day/hour
    sort: list[dict[str, Any]] = field(default_factory=list)  # explanations.order
    proportion: dict[str, Any] | None = None  # explanations.proportion
    each: list[dict[str, Any]] = field(default_factory=list)  # Nested each
    special_grammar: list[str] = field(default_factory=list)  # YoY/period-over-period/change rate...
    model: str = ""  # datarc data model

    # --- aggregate mode only ---
    scope: str = ""  # business_review / diagnosis / ...
    sop_used: str | None = None  # analyst-sop-*
    metrics_queried: list[str] = field(default_factory=list)  # ["营业额","利润",...]
    dimensions_explored: list[str] = field(default_factory=list)  # ["品牌","城市"]
    comparisons: list[str] = field(default_factory=list)  # ["同比","环比"]
    d_filters_applied: list[dict[str, Any]] = field(default_factory=list)
    aggregate_time_range: dict[str, Any] | None = None  # {start,end,base}

    # --- Shared by both modes ---
    key_facts: list[dict[str, Any]] = field(default_factory=list)  # [{label,value,...}]
    cards: list[dict[str, Any]] = field(default_factory=list)  # [{url,title}]
    conclusion: str = ""  # First final_answer sentence ≤80 characters

    # P0 (V2.7): complete final_answer (≤1500 characters), mainly for empty/content_only modes;
    # next-turn Lead sees its previous bullets/navigation, rather than only the 80-character conclusion.
    # Trigger: aspen sfe (s_47fbdf8a57a7), turn 1 had 5 bullets; turn 2 "都看看" failed reference resolution.
    final_answer_full: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = {
            "turn_id": self.turn_id,
            "user_text": self.user_text,
            "mode": self.mode,
            "conclusion": self.conclusion,
            "key_facts": list(self.key_facts),
            "cards": list(self.cards),
            "final_answer_full": self.final_answer_full,
        }
        if self.mode == "simple":
            d.update(
                {
                    "metrics": self.metrics,
                    "dimensions": self.dimensions,
                    "d_filter": self.d_filter,
                    "m_filter": self.m_filter,
                    "time_range": self.time_range,
                    "time_granularity": self.time_granularity,
                    "sort": self.sort,
                    "proportion": self.proportion,
                    "each": self.each,
                    "special_grammar": self.special_grammar,
                    "model": self.model,
                }
            )
        elif self.mode == "aggregate":
            d.update(
                {
                    "scope": self.scope,
                    "sop_used": self.sop_used,
                    "metrics_queried": self.metrics_queried,
                    "dimensions_explored": self.dimensions_explored,
                    "comparisons": self.comparisons,
                    "d_filters_applied": self.d_filters_applied,
                    "time_range": self.aggregate_time_range,
                }
            )
        return d


@dataclass
class TurnResult:
    """一个 turn 完成后的聚合结果."""

    turn_id: str
    status: TurnStatus
    intent: IntentType
    final_answer: str
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None
