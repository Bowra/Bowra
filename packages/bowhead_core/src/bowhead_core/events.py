"""Orchestrator 事件总线（in-memory，M1 起步版）.

对外契约：见 TD §2.4 的 SSE event 类型枚举（plan_ready / agent_spawned /
tool_call / partial_answer / artifact_ready / done / error 等）。

实现选择：
- M1 / Sprint 1：``asyncio.Queue`` per turn，进程内
- M2：切到 Redis Streams（TD §2.6）保持 API 不变
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.types import utc_now_iso

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

EventType = Literal[
    # chat-solidify 2026-07-03: remove 14 declared names with zero repository emit sites
    # (plan_ready/intent_classified/artifact_ready/approval_required[underscore version]/
    #  compression_*/prompt_snapshot/meta_intent_classified/canvas.export_enqueued/
    #  memory.proposal.suggested/chi_*/plan_announced/emoji_remove)。
    # Read docs/design/chat-pipeline.md before adding/removing event names.
    "turn_started",
    "agent_spawned",
    "agent.collaboration",  # Content-free committed exchange; details use the authorized reader.
    "tool_call",
    "tool_result",
    "partial_answer",
    # thinking removed: P4.D incremental emit path broken (zero repository sites); frontend vertical slice
    # (allowlist/reducer/state) temporarily retained; repair emit or delete all per chat-pipeline.md.
    "error",
    "done",
    # Canvas mode (TD §12.3)
    "canvas.opened",  # payload: {canvas_id, session_id, title?}
    "canvas.outline_ready",  # payload: {sections: [{id, kind}], meta_intent}
    "canvas.section_added",  # payload: {section, seq}  (section = full ReportSection)
    "canvas.section_patched",  # payload: {section_id, patch, rev, ok}
    # canvas.section_removed removed: zero emit sites (canvas-sse handler also removed;
    # harmless im/turn_render elif retained).
    "canvas.reordered",  # payload: {order: [section_id, ...], ok, direction?}
    "canvas.done",  # payload: {canvas_id, section_count}
    # D6 (canvas-empty bug 2026-05-25): emitted after turn finalize when sections
    # written by this turn are chat-mode (no insight_card/recommendations).
    # FE removes these section_ids from canvas tab view; chat bubble inline
    # KPI/chart still surfaces them via inline_artifacts wire.
    "canvas.sections_hidden",  # payload: {turn_id, hidden_section_ids: [str], rationale}
    # report-agent-authorship α v1 (2026-06-24): a later report-worthy turn superseded
    # prior report blocks (BOWHEAD_CANVAS_LATEST_WINS). FE renders them collapsed in
    # the history accordion (NOT removed — collapse-in-place).
    "canvas.sections_superseded",  # payload: {turn_id, superseded_section_ids: [str], superseded_by_turn}
    # P3.5 Multi-segment streaming (frontend canvas-store onTurnStreaming/Section/Chunk/Done):
    "canvas.turn_streaming",  # payload: {turn_id}: whole-turn lock begins.
    "canvas.section_streaming",  # payload: {section_id, kind, partial_payload?}
    "canvas.section_chunk",  # payload: {section_id, delta} — token-level apply
    "canvas.section_done",  # payload: {section_id, payload, is_partial?}
    # PyCell bridge (render tool_call/tool_result as Jupyter-style In[N]).
    "pycell.in",  # payload: {n, name, code, status: "running"}
    "pycell.out",  # payload: {n, status: "success"|"error", output, dur_ms}
    # Approval Loop (M2.3a): approval.decided removed; notification center only
    # (notification_service.emit_approval_decided), never turn bus.
    "approval.required",  # payload: {approval_id, tool_name, arguments, reason, risk_level}
    "approval.executed",  # payload: {approval_id, ok, result_preview|error}
    # R-2 (chat-interaction-redesign closing): approval **rejection** terminal chat fact, parallel to
    # approval.executed, both emit_out_of_band (decision after terminal turn / bus close).
    # Only approvalStatusById folding gate (rejected=abnormal completion→no fold); approval surface still REST/notifications.
    "approval.rejected",  # payload: {card_id, approval_id}
    # M156 HITL clarification (#150 / M3b)
    "clarification_required",  # payload: {request_id, token, candidates[], rationale, workspace_id}
    "clarification.resolved",  # payload: {request_id, chosen_value}
    # dingsiyan #11 (2026-07-07): resume start placeholder, spawn_resume_turn emits on fresh bus
    # before worker create_task. Two duties: ①immediate selection feedback (fills silence before first LLM token);
    # ②once persisted, later-than-paused-done event enables _replay_rows_for_stream to
    # remove stale done(clarification_pending) (t_f2f6a88ad3cc leftover window). Frontend tombstones
    # clarification immediately, preventing replay resurrection.
    "turn.resumed",  # payload: {request_id, chosen_values[], turn_id}
    # plan_preview removed: zero emit sites (workflow_distill reads historical rows only);
    # dormant frontend runner.plan slice documented in chat-pipeline.md.
    # cca P2.1 (C1): the tool_creator TRIGGER → FE opens the inline create card.
    # payload {spec_url_hint?: str|None} — NEVER a secret / structured state.
    "cca.tool_create_open",
    # Conversational subagent authoring. Legacy/manual payloads contain
    # purpose_hint plus the user-stated name_hint (slug) / display_name_hint;
    # additive draft payloads contain an owner-bound draft pointer/status/title.
    # Full definitions, prompts, and tool refs never travel in this event.
    "cca.subagent_creator_open",
    # workflow-solidify S2: workflow_creator TRIGGER → frontend opens inline workflow-solidification card.
    # payload {turn_id: str|None (distill target; None = FE resolves from conversation),
    # hint?: str|None} — only a one-line reuse-intent prefill, never structured state.
    "cca.workflow_solidify_open",
    # Admitted settings proposals retain the shared confirmation beat in live and replay.
    # Payload includes proposal/Turn/form/target identity, sparse typed rows and notes.
    # Rows may pin baseline_revision for governed metadata; values contain no credentials.
    # Authoritative review, apply/undo policy and receipts belong to the original domain.
    "config.lite_propose",
    "coach.operation_open",
    "coach.resource_open",
    # Alignment loop: content-free pointer to the Owner's question card or loop report; the card reads the loop.
    "coach.alignment_card",
    "configuration.canvas_open",
    "document.draft_open",  # Content-free original private-document review pointer.
    # Config copilot batch 1-b: tier-2 draft drawer (tech-design-batch1b §3). Self-contained payload:
    # {draft_id, turn_id(beat anchor), form_id, created, updated_fields, fields, field_meta,
    # notes, workspace_id}; before emit, assert fields ⊆ schema and zero secret keys (retained forever in turn_log,
    # R4-4 seam). Save/discard server draft state machine (frontend hydration), no return event; IM n/a (channel gate).
    "config.draft_updated",
    # Sprint θ Phase 8b/8d: Lead orchestrator events (PRD §3.7-§3.8)
    # tool_called retired chat-redesign Phase 1.5 — 0 backend emit sites,
    # chat-redesign step_started covers the surface. FE listener also dropped.
    "peer_dispatched",  # payload: {spawn_id, peer_name, call_id, parent_step_id, ...}
    "peer_dispatch_failed",  # payload: {card_id, spawn_id, peer_name, reason_code, diagnostics}
    "turn_failed",  # payload: {reason, value?}; exception text is log-only
    "turn_aborted",  # payload: {turn_id, partial_state}
    # S5 durable effect uncertainty.  The event is receipt-manifest evidence;
    # S6 adds its Web/IM projection from the same committed row.
    "interrupted_check_actions",  # payload: {reason_code, message_key, approval_id?|unsettled_count?}
    # D2b.3b durable effect vocabulary. turn.llm_invocation is a content-free
    # contract-v2 physical-send marker; turn.effect_disclosure_settled remains
    # a receipt-fenced Store control event rather than a normal bus emit.
    "turn.llm_invocation",
    "turn.effect_disclosure_settled",
    "canvas_op_failed",  # payload: {op, reason_code, section_id?, diagnostics}
    "final_answer",  # payload: {text}
    # omicron-12 audit: these were emitted via `# type: ignore[arg-type]` — added here
    "turn_cancelled",  # turn_runner.py user cancel; web uses done{cancelled}, IM turn_render consumes.
    "payload_missing_guard",  # canvas_composer.py (telemetry)
    # Sprint chat-v3 Phase A — LLM-emitted finding summary post tool_result (≤120 chars)
    "tool_finding",  # payload: {call_id, finding, tool_name}
    # Sprint chat-style-fix Phase A2 — Lead-emitted turn headline (≤30 chars, scanner-captured)
    "headline",  # payload: {text: str (≤30 chars)} — Lead turn summary, emit after </headline>
    # Sprint chat-v4 (0.25.0) — structured chat redesign events. Sprint
    # phase-plan-removal (0.27.0): "turn.plan" + "phase.update" +
    # "turn.plan_cancelled" removed (FE shape-gate now derives from
    # v4HasDispatched signal set by agent_spawned/tool_call/peer_dispatched).
    "lead.subagent_summary",  # W4: per-subagent_spawn emit after return (spawn_id, profile, child_canvas_sections[])
    "lead.final.payload",  # W4: Lead final structured AnalysisPayload (replaces V1 free markdown when valid)
    # Sprint chat-redesign-2026-05-21 (Phase 1.1) — Step List Card model
    # Dual-emit with tool_call/tool_result during Phase 1.1-1.5; tool_call/agent_spawned/peer_dispatched
    # emit sites kept until Phase 1.5 cleanup (FE switches to step_* fully then).
    "step_started",  # payload: StepStartedPayload — Step List Card adds new step row
    "step_progress",  # payload: StepProgressPayload — content delta or N/M progress
    "step_done",  # payload: StepDonePayload — step finish (success/failure/partial)
    "step_list_card_created",  # payload: StepListCardCreatedPayload — card created at turn start
    "step_list_card_finalized",  # payload: StepListCardFinalizedPayload — card lock (turn done)
    "emoji_ack",  # payload: EmojiAckPayload — add emoji on user msg (steer ack 🤝)
    # Planning Mode (M1, block 4) — plan-level + per-step events for the execution turn.
    "plan_proposed",  # payload: {plan_id, steps:[...]} — editable plan card (Turn A)
    "plan_running",  # payload: {plan_id}
    "plan_completed",  # payload: {plan_id}
    "plan_blocked",  # payload: {plan_id, blocked_step_ids} — needs user (corpus/retry)
    "plan_failed",  # payload: {plan_id, reason}
    "step_blocked",  # payload: {plan_id, step_id, error_kind, reason} — precondition gate
    "step_failed",  # payload: {plan_id, step_id, error_kind}
    "turn.follow_up",  # payload: {follow_up_turn_id, follow_up_stream_url, reason, buffered_count, attachments, execution_generation} — emitted on original bus before close when steer drain spawned a continuation turn; FE subscribes to follow_up_stream_url to continue
    # Phase 1.6 G3 — Lead narrative streaming (PRD §2.2 success metric: ≥80% slow turn narrative visible).
    # Emitted alongside scanner-gated safe_text deltas in lead_orchestrator
    # so FE LeadNarrative renders Lead's voice progressively (above StepListCard).
    "narrative.delta",  # payload: NarrativeDeltaPayload — Lead prose delta (post-scanner safe_text)
    # Step retry/skip ack: ⚠ actual state (chat-solidify 2026-07-03): both endpoints
    # are 202-ack stubs, never rerun; frontend never listened, O5 removed all retry/skip buttons
    # (unresponsive fake buttons). Names retained for API compatibility only. Restore buttons upon real redispatch
    # wiring through entitlement gate; see chat-pipeline.md.
    "turn.step_retry_requested",  # payload: {step_id, new_step_id, eta_seconds}
    "turn.step_skip_requested",  # payload: {step_id, skip_marked}
    # prod-bug-bash-039 Phase 5 (0.39.3) — turn-scoped citation registry events.
    # Emitted per Citation in seq order BEFORE final_answer, so FE has citations[]
    # populated when ArtifactChip resolves `[cite:N]` markers in narrative text.
    # Payload field renamed to `citation_seq` (NOT `seq`) — `seq` is reserved by
    # OrchestratorEvent.to_sse for the bus monotonic counter, which would otherwise
    # clobber the citation index and break every chip.
    "turn.citation_registered",  # payload: {citation_seq: int, artifact_id: str, label: str|None, kind: str}
    # chat-solidify 2026-07-03: add declarations (previous emit bypassed via `# type: ignore[arg-type]`):
    "peer.iter",  # Per-iteration telemetry, internal/no frontend. **Includes Lead**, distinguished by payload.profile.
    "llm.call_timing",  # Internal timing and bounded transport/parser diagnostics; no model content.
    "llm.retry_scheduled",  # payload: {subagent_id, profile, retry, max_retries, reason, delay_s} — Router retry after a failed model attempt; shown as running status.
    "notice",  # Subagent user notice (no frontend consumer yet).
    "trace.step",  # canvas_streaming timeline step, internal; old frontend allowlist removed.
    "template_clone.progress",  # Historical template progress retained for event replay.
    "turn.satisfaction_recorded",  # Gateway satisfaction receipt (routes/turns.py).
    "clarification.directive_cancelled",  # Gateway directive cancel (routes/clarifications.py; IM/receipt).
    "clarification.dismissed",  # Legacy clarification leaves actionable set; durable live + replay inverse.
    # Transport heartbeat/liveness: canvas_streaming helpers bus version; chat.py event_gen
    # direct generator version bypasses bus/storage. Frontend keepalive feeds watchdog,
    # streaming.state drives paused/error visuals.
    "streaming.state",  # payload: {state: active|paused|error, reason?}
    "keepalive",  # payload: {ts}
    "turn.progress",  # transient SSE frame, never on the bus: {turn_id, agents: [{subagent_id, profile, display_name, step}]}
    "stream.checkpoint",  # transport cursor for Terminal Protocol reconnect
    "terminal.protocol_error",  # durable terminal transport diagnostic
]

# report-streaming P1: criterion for this turn starting canvas content production.
# TurnEventBus.emit sees any opener before this turn's canvas.turn_streaming,
# injects that frame first (long emit() comment below).
#
# Only content-producing events, **not** canvas.section_patched / canvas.reordered edits:
# REST editing means user is not awaiting analysis; locking canvas invents
# in-progress state. Also exclude all pure-chat events: no canvas output means no canvas streaming
# (D2 critical constraint①: never imply output necessarily coming).
_CANVAS_TURN_OPENERS: frozenset[str] = frozenset(
    {
        "canvas.opened",
        "canvas.section_streaming",
        "canvas.section_added",
        "canvas.section_chunk",
    }
)


# chat-canvas-surface 2026-08: early-chat turns **entirely suppress** these canvas-revealing
# events (neither delivered nor persisted).
#
# ⚠ Not suppress_live_delivery: that persists without delivery; attachCanvasSSE
# **replays** canvas frames on reconnect (use-turn-runner has canvasGate dedup cursor),
# so persisted frame makes canvas reappear after reconnect.
# Clean live / appearing replay directly violates live == persisted == replay chat invariant.
# Therefore **never emit**: absent live and replay, consistent.
#
# Three beyond _CANVAS_TURN_OPENERS: turn_streaming (sole frontend canvas-store writer of
# currentTurnId; one stray frame lights in-progress canvas), outline_ready, section_done.
# Exclude canvas.section_patched / canvas.reordered edits: existing sections change,
# no phantom canvas output. Exclude canvas.done / sections_hidden too:
# closing semantics required for frontend state machine to terminate.
_CHAT_SURFACE_SUPPRESSED: frozenset[str] = frozenset(
    {
        "canvas.opened",
        "canvas.turn_streaming",
        "canvas.outline_ready",
        "canvas.section_streaming",
        "canvas.section_added",
        "canvas.section_chunk",
        "canvas.section_done",
    }
)

#: Events whose structural facts feed ``done.step_outcome`` (``turn_step_outcome``).
_STEP_OUTCOME_EVENT_TYPES: frozenset[str] = frozenset({"step_started", "step_done", "lead.subagent_summary", "peer_dispatch_failed"})
_TERMINAL_FAILURE_DONE_STATUSES: frozenset[str] = frozenset({"error", "failed"})


def _record_step_outcome_fact(bus: Any, event: OrchestratorEvent) -> None:
    """Append one bounded structural record for ``done.step_outcome``; never raises."""

    try:
        facts = getattr(bus, "_step_outcome_facts", None)
        if not isinstance(facts, list) or getattr(bus, "_step_outcome_overflow", False):
            return
        from bowhead_core.turn_step_outcome import STEP_OUTCOME_RECORD_CAP, step_fact_from_event

        fact = step_fact_from_event(
            str(event.event_type),
            event.payload,
            parent_agent_id=event.parent_agent_id,
            seq=int(event.seq),
        )
        if fact is None:
            return
        if len(facts) >= STEP_OUTCOME_RECORD_CAP:
            # A partial count would be a false claim; the field is omitted instead.
            bus._step_outcome_overflow = True
            facts.clear()
            return
        facts.append(fact)
    except Exception:  # noqa: S110 — telemetry-shaped bookkeeping must not block the event.
        pass


def _attach_recovery_action(event_type: str, payload: dict[str, Any]) -> None:
    """Add ``recovery_action`` to a root ``turn_failed`` / failed ``done`` carrying a public reason.

    Terminal v1 compatibility frames (already persisted with ``event_id``) are left untouched so
    the live frame cannot differ from its durable row.
    """

    if "recovery_action" in payload or "event_id" in payload:
        return
    if event_type == "done" and payload.get("status") not in _TERMINAL_FAILURE_DONE_STATUSES:
        return
    from bowhead_core.published_diagnostics import chat_recovery_action, failure_event_reason_code

    action = chat_recovery_action(failure_event_reason_code(payload))
    if action is not None:
        payload["recovery_action"] = action


@dataclass
class OrchestratorEvent:
    """SSE / 消息总线事件的公共信封."""

    event_type: EventType
    turn_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = field(default_factory=utc_now_iso)
    # Optional durable identity for cross-Gateway outbox delivery. Retries
    # with the same semantic payload reuse one TurnLog row.
    durable_event_id: str | None = None
    suppress_live_delivery: bool = False
    # ρ Phase E — bus.emit auto-populates seq (per-turn monotonic).
    seq: int = -1
    # ρ Phase E — caller-set in peer scope; None == Lead emit (default).
    parent_agent_id: str | None = None
    # A small number of UI-opening facts point at an already-durable aggregate.
    # Those events may require their turn-event row to commit before live
    # delivery so refresh cannot lose the only pointer to that aggregate.
    persist_before_delivery: bool = field(
        default=False,
        repr=False,
        compare=False,
    )

    def to_sse(self) -> dict[str, str]:
        import json

        # ρ Phase E (r2): warn-only during migration when seq was never
        # auto-populated (i.e. event constructed but not routed through
        # `TurnEventBus.emit`). Hard-raise scheduled for 0.11.3+ after
        # 7-day soak shows the counter is 0/day.
        if self.seq < 0:
            logger.warning(
                "OrchestratorEvent.to_sse seq=-1 (turn_id=%s type=%s) — bypassed bus.emit auto-population.",
                self.turn_id,
                self.event_type,
            )
            seq_safe = 0
        else:
            seq_safe = self.seq
        payload = dict(self.payload)
        payload["seq"] = seq_safe
        if self.parent_agent_id is not None:
            payload["parent_agent_id"] = self.parent_agent_id
        return {"event": self.event_type, "data": json.dumps(payload, ensure_ascii=False)}


def make_event(
    event_type: EventType,
    turn_id: str,
    payload: dict[str, Any],
    *,
    parent_agent_id: str | None = None,
    persist_before_delivery: bool = False,
) -> OrchestratorEvent:
    """ρ Phase E helper — preferred constructor for NEW emit sites.

    Existing 42 sites continue to call `OrchestratorEvent(...)` directly; the
    bus auto-populates `seq`. This helper just makes intent explicit and gives
    new code a single place to pass `parent_agent_id` without remembering the
    kwarg name.
    """
    return OrchestratorEvent(
        event_type=event_type,
        turn_id=turn_id,
        payload=payload,
        parent_agent_id=parent_agent_id,
        persist_before_delivery=persist_before_delivery,
    )


# ---------------------------------------------------------------------------
# Sprint chat-redesign-2026-05-21 (Phase 1.1) — Step List Card payload schemas
#
# These dataclasses document the wire shape of the new step_* / emoji_* /
# card_* events (tech-design.md §2.1.2). Existing emit sites construct payloads
# as plain dicts; callers may use these for typed construction or schema docs.
# Field order chosen to mirror the tech-design spec.
# ---------------------------------------------------------------------------


@dataclass
class StepError:
    """Error attached to a step_done payload when state in {failure, partial}."""

    # corrupted | unsupported_mime | too_big | http_4xx | http_5xx |
    # auth_required | timeout | vision_failed | sql_error | tool_exception
    type: str
    message_zh: str  # user-language message including self-help hint
    is_transient: bool  # decides button set (transient → [retry][skip], else [acknowledge])
    code: str | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass
class SubStepResult:
    """One sub-step inside a batched step_done (e.g. bi_query parallel SQL).

    Option A (FE parse): backend doesn't fill `sub_steps` until dogfood shows
    fallback rate >30%; FE derives from result_inline. Option B is just this
    list filled by backend (Phase 1.7 / Phase 2).
    """

    label: str  # e.g. "SELECT region, SUM(amt) FROM ..."
    state: Literal["success", "failure"]
    output_preview: str | None = None
    error_kind: str | None = None


@dataclass
class PlanItem:
    """Phase 3+ — Lead-announced TODO row. Phase 1 reserved (not emitted)."""

    item_id: str
    label: str
    tag: str
    priority: str
    status: Literal["pending", "in_progress", "done", "skipped"]
    related_step_id: str | None = None


@dataclass
class StepStartedPayload:
    """Wire shape for `step_started` event.

    Backward-compat fields (tool_name, call_id, subagent_id, arguments_preview,
    kind, display_category) duplicate the legacy `tool_call` payload during the
    Phase 1.1 → Phase 1.5 dual-emit window. They drop out once FE migrates fully.
    """

    card_id: str  # usually = turn_id (FE uses turn_id as React key)
    step_id: str  # UUID per step
    step_type: str  # parse | tool_run | lead_thinking_writing | subagent | plan_announce
    header_zh: str  # user-language header, e.g. "查询数据 (bi_query)"
    started_at: str  # ISO ts (use bowhead_core.types.utc_now_iso)
    parent_step_id: str | None = None  # subagent nesting / batch sub_step link
    concurrency_group_id: str | None = None  # P0-2: same batch shares group; sequential = None
    input_preview: str | None = None  # input summary (SQL / first N lines of code / search term)
    # Backward-compat fields (Phase 1.1 dual-emit window — drop in Phase 1.5)
    tool_name: str | None = None
    call_id: str | None = None
    subagent_id: str | None = None
    arguments_preview: str | None = None
    kind: str | None = None
    display_category: str | None = None
    open_world: bool | None = None  # tool-domain (D1): external/web (True) vs closed in-workspace


@dataclass
class StepProgressPayload:
    """Wire shape for `step_progress` event.

    content_delta: token append for lead_thinking_writing (LLM partial JSON).
    completed/total/percent: progress for batch or long-running step.
    """

    card_id: str
    step_id: str
    content_delta: str | None = None
    completed: int | None = None
    total: int | None = None
    percent: float | None = None


@dataclass
class StepDonePayload:
    """Wire shape for `step_done` event.

    State semantics:
    - success: tool/parse/subagent finished cleanly
    - failure: hard error, includes StepError
    - partial: some sub_steps succeeded, others failed (batch tool case)

    Backward-compat fields (tool_name, call_id, ok, result_inline,
    result_preview, result_meta, result_token_count) duplicate the legacy
    `tool_result` payload during the dual-emit window.
    """

    card_id: str
    step_id: str
    step_type: str
    state: Literal["success", "failure", "partial"]
    duration_ms: int
    output_preview: str | None = None  # result summary (mirrors legacy result_preview)
    sub_steps: list[SubStepResult] | None = None  # batch per-SQL — None until Option B
    error: StepError | None = None
    # Backward-compat fields (drop in Phase 1.5)
    tool_name: str | None = None
    call_id: str | None = None
    ok: bool | None = None
    result_inline: str | None = None  # ≤4096 bytes (φ Phase C cap)
    result_preview: str | None = None  # 500-char fallback
    result_meta: dict[str, Any] | None = None
    result_token_count: int | None = None


@dataclass
class StepListCardCreatedPayload:
    """Wire shape for `step_list_card_created` event (emitted once at turn start)."""

    card_id: str  # = turn_id
    plan: list[PlanItem] | None = None  # Phase 3+ only; Phase 1 None or []


@dataclass
class StepListCardFinalizedPayload:
    """Wire shape for `step_list_card_finalized` event (emitted once at turn end)."""

    card_id: str
    state: Literal["completed", "failed", "cancelled"]
    step_count: int


@dataclass
class EmojiAckPayload:
    """Wire shape for `emoji_ack` / `emoji_remove` events.

    Used by steer mode (🤝 FISTBUMP), HITL ack (⏳ THINKING), done ack (✅).
    """

    target_msg_id: str  # user message id to react on
    emoji: str  # 🤝 (steer) | ⏳ (thinking) | ✅ (done)


@dataclass
class NarrativeDeltaPayload:
    """Wire shape for `narrative.delta` event.

    Phase 1.6 G3: progressive emit of Lead's prose stream (post-scanner
    safe_text). One event per LLM chunk delivered to the scanner-gated
    stream callback. FE accumulates `delta` into `turn-store.narrative`
    and renders via LeadNarrative above the StepListCard.

    Skipped for empty / pure-whitespace deltas to keep the surface quiet
    when the LLM is mid-tool-call (no prose tokens).
    """

    card_id: str  # = turn_id (matches step_* routing key)
    delta: str  # appended to narrative buffer


def _fanout_enabled() -> bool:
    """fix/sse-fanout-bus — BOWHEAD_SSE_FANOUT，默认 ON.

    空串视同 unset（.env 残留 ``BOWHEAD_SSE_FANOUT=`` 不得静默关掉默认 ON）。
    解析 idiom 对齐 workflow_creator_trigger.workflow_creator_enabled。
    """
    raw = os.environ.get("BOWHEAD_SSE_FANOUT", "").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


class TerminalDurabilityAbort(BaseException):
    """Fail-stop a turn after its durable execution record becomes uncertain.

    This deliberately bypasses ordinary ``except Exception`` fallbacks.  A
    request/task supervisor may catch this exact type to hand the turn to
    lease-based recovery, while cancellation and process-control exceptions
    keep their native semantics.
    """


class TurnEventBus:
    """单 turn 的事件总线.

    生产者（Orchestrator）fire-and-forget 推事件；消费者（Gateway SSE /
    feishu mirror）按 turn_id 订阅。turn 结束后自动关闭。

    fix/sse-fanout-bus — 两种投递模式（BOWHEAD_SSE_FANOUT，默认 ON）：

    - **fanout（ring buffer，默认）**：事件 append 进 per-turn 只增列表，
      每个订阅者持私有 cursor 从头读全量 —— 任意数量并发订阅者（重连
      overlap / 双 tab / feishu mirror + web 同看）互不干扰，各收 seq 1 到
      终态（done/error）的完整序列。订阅者不在 bus 留任何状态：弃订阅者
      （task cancel / generator GC）即回收，无 unsubscribe 协议。
      ⚠ 订阅者共享**同一个** event 对象 —— 不得原地修改 event / payload
      及其内嵌容器（to_sse 已自带 payload 拷贝）。
    - **legacy（单消费者 asyncio.Queue，flag OFF rollback 路径）**：
      subscribe() 的 queue.get() 把事件移出队列，并发订阅互偷事件 ——
      仅作回滚保留，行为由 pin-OFF 测试锁住。

    若注入 `log_sink`（coroutine (OrchestratorEvent) -> None），每次 emit
    也会调用它 —— 用于 TurnLogStore 持久化（支持 replay / 审计）。
    """

    _SENTINEL = object()

    def __init__(
        self,
        log_sink: Any | None = None,
        *,
        initial_seq: int = 0,
        producer_registered: bool = True,
        persist_before_live: bool = False,
    ) -> None:
        self._fanout = _fanout_enabled()
        if self._fanout:
            # Per-turn append-only ring, fully released on registry.close.
            # Unbounded: eviction + unpersisted fire-and-forget log_sink loses events, exactly
            # underdelivery this mode eliminates (largest measured turn ~2.5k events/247KB).
            self._events: list[OrchestratorEvent] = []
            self._cond = asyncio.Condition()
        else:
            self._queue: asyncio.Queue[OrchestratorEvent | object] = asyncio.Queue()
        self._closed = False
        # Terminal publication has an asynchronous continuation hook and an
        # asynchronous durable write. These fences reject concurrent terminal
        # attempts and late frames without exposing EOF before the terminal
        # event itself reaches the live ring.
        self._terminal_in_progress = False
        self._terminal_frozen = False
        # source-accessibility F3 residual closure: drain hooks before terminal freeze (e.g. citation flush).
        # Branchwise cancel/cap/error flushes never cover all (3 patches still missed); done/error
        # emit is **sole boundary across all paths**. Drain before freeze; branch patches become
        # early optimization. Clear-before-await gives exactly-once; drain error never blocks terminal state.
        self._pre_terminal_drains: list[Any] = []
        # Subscriber-only mirrors may pre-create a local bus on a Gateway that
        # does not own the Turn worker.  Durable projection must never treat
        # such an empty bus as proof that a producer will eventually close it.
        self._producer_registered = producer_registered
        # Composer dispatch recovery can fence stale owner writes. Tracked
        # workers therefore upgrade their bus before scheduling so no event is
        # visible in the live ring until that same row is durable for replay.
        self._persist_before_live = persist_before_live
        self._fatal_error: BaseException | None = None
        self._log_sink = log_sink
        # ρ Phase E — per-turn monotonic counter for SSE event ordering.
        # First emitted event gets seq=initial_seq+1. Normal turns use
        # initial_seq=0; same-turn resume seeds this from persisted replay max.
        self._seq_counter = max(0, int(initial_seq))
        # Sprint chat-redesign-2026-05-21 Phase 1.1 — step_done count for the
        # terminal step_list_card_finalized payload (no caller threading required).
        self._step_count = 0
        # Turn-summary tools_count: every Lead/peer call emits tool_result on this bus
        # (subagent.py _execute_tool; peers parent_agent_id). Counting here
        # covers all agents without threading count through each runner layer.
        self._tool_result_count = 0
        # report-streaming P1: `canvas.turn_streaming` at most once per turn (see emit()).
        self._canvas_turn_streaming_sent = False
        # Chat turn feedback D6: bounded structural step records for the terminal
        # ``done.step_outcome`` (no previews or arguments; capped, then omitted).
        self._step_outcome_facts: list[Any] = []
        self._step_outcome_overflow = False
        # A continuation generation (resume after clarification, same-turn reattach) starts with
        # earlier events already persisted by another bus generation; its facts would under-report,
        # so the outcome is unknown and omitted.
        self._step_outcome_partial = int(initial_seq) > 0

    def latch_fatal_error(self, error: BaseException) -> None:
        """Fence later provider/tool work after a durable event rejection."""

        if self._fatal_error is None:
            self._fatal_error = error

    def raise_if_failed(self) -> None:
        if self._fatal_error is not None:
            raise TerminalDurabilityAbort(
                "legacy durable event write has failed",
            ) from self._fatal_error

    def seed_seq_counter(self, seq: int) -> None:
        """Ensure future emits continue after at least `seq`.

        Same-turn resume replays persisted events, then streams a fresh bus
        generation. Seeding prevents new live events from reusing replayed seq
        values and being dropped by FE per-turn seq dedupe.
        """
        if int(seq) > self._seq_counter:
            # Events this bus never saw exist for the turn (continuation generation).
            self._step_outcome_partial = True
        self._seq_counter = max(self._seq_counter, 0, int(seq))

    def add_pre_terminal_drain(self, drain: Any) -> None:
        """注册一个终态（done/顶层 error）冻结前执行的异步 drain。

        语义：drain 在终态帧真正投递**之前**运行，此刻 bus 未冻结，drain 内可以
        照常 emit 普通事件（典型：`_flush_citations` 把登记表排空成
        `turn.citation_registered` 事件流）。exactly-once（列表先清后跑）；
        drain 抛异常只记 warning，不影响终态投递。
        """
        self._pre_terminal_drains.append(drain)

    async def emit(self, event: OrchestratorEvent) -> bool:
        """Emit an event. Returns True if delivered, False if the bus was already
        closed (terminal done/error emitted) — the caller can then decide to
        re-route (dingsiyan #11: emit_out_of_band downgrades to a transient bus so
        a post-pause event that raced a concurrent close still gets persisted)."""
        if self._fatal_error is not None:
            self.raise_if_failed()
        terminal = event.event_type == "done" or (
            event.event_type == "error" and event.parent_agent_id is None and not event.payload.get("parent_agent_id")
        )
        if self._closed or self._terminal_frozen:
            return False
        # chat-canvas-surface 2026-08: early-chat turns entirely suppress canvas-revealing events.
        # emit is **sole convergence point**: canvas.section_added alone has 10 emit sites
        # across 6 files (canvas_tools / canvas_composer / report_writer_loop /
        # template_clone_runner …); patching each duplicates one concept N ways.
        # Uncertain early classification (none of three states speaks) leaves _early_chat_surface unset ⇒ byte-identical.
        if getattr(self, "_early_chat_surface", False) and event.event_type in _CHAT_SURFACE_SUPPRESSED:
            return True
        if terminal:
            # Claim the only terminal attempt before awaiting its continuation
            # hook. A second done/error must not bypass a hook that is still
            # materializing (or has rejected) accepted steer work.
            if self._terminal_in_progress:
                return False
            self._terminal_in_progress = True
        # report-streaming P1 fix: inject canvas.turn_streaming here, exactly-once.
        #
        # Sole frontend canvas-store writer of currentTurnId (onTurnStreaming);
        # D2 waiting-strip first visibility gate is Boolean(turnId), D3 banner
        # `selectComposeStarted / selectTurnSurfaceCounts / selectRunningStepLabel`
        # all turn-scoped; empty turnId disables both batches entirely.
        #
        # Previously only canvas_composer Lead closing (emit_lifecycle=True) emitted; current main path
        # single report_writer_loop + canvas_tools writes sections directly, never invokes it.
        # Actual 116 production: only 3 of 141 canvas-output turns emitted (~2%).
        #
        # Inject at bus, not each site: 7 sites (composer×4 / report_writer_loop×2 /
        # canvas_tools×1); patching eighth misses ninth. Bus converges
        # seq assignment, log_sink persistence, replay; injected frame **also persists
        # and replays on F5**, preserving live == persist == replay.
        #
        # Also suppress duplicates: onTurnStreaming resets streamingSectionIds /
        # cancelledSectionIds; midturn duplicate clears streaming markers (batch A draft slot
        # likely streaming now). Exactly-once safer than before.
        if event.event_type == "canvas.turn_streaming":
            if self._canvas_turn_streaming_sent:
                return True  # Already sent; suppress midturn frontend streaming-set reset.
            self._canvas_turn_streaming_sent = True
        elif not self._canvas_turn_streaming_sent and event.event_type in _CANVAS_TURN_OPENERS:
            # Recursion depth always 1: inner sets flag then normal delivery.
            await self.emit(
                OrchestratorEvent(
                    event_type="canvas.turn_streaming",
                    turn_id=event.turn_id,
                    payload={"turn_id": event.turn_id},
                )
            )
            # The recursive opener yields. A terminal may claim and freeze the
            # bus while it is awaiting the synthetic frame; never let the outer
            # canvas event land after that terminal.
            if self._closed or self._terminal_frozen:
                return False
        # Composer steer continuations must be discoverable before the parent
        # terminal frame closes the live SSE stream.  Gateway may install one
        # per-bus hook which atomically materializes the accepted buffer and
        # emits ``turn.follow_up`` on this same bus.  Clear-before-await makes
        # it exactly-once even if the hook itself emits another event.
        if terminal:
            drains = self._pre_terminal_drains
            if drains:
                self._pre_terminal_drains = []
                try:
                    for drain in drains:
                        try:
                            await drain()
                        except Exception as exc:  # Drain never blocks terminal.
                            logger.warning(
                                "turn_bus.pre_terminal_drain_failed turn=%s type=%s err=%s",
                                event.turn_id,
                                event.event_type,
                                exc,
                            )
                except BaseException:
                    # Review F5: CancelledError escaping after _terminal_in_progress=True
                    # permanently strands bus without terminal (later done/error rejected by occupied
                    # terminal gate). Like neighboring _before_terminal_emit: freeze then rethrow,
                    # worker finalizer reclaims via synthetic terminal.
                    self._terminal_frozen = True
                    raise
            before_terminal = getattr(self, "_before_terminal_emit", None)
            if before_terminal is not None:
                self._before_terminal_emit = None
                try:
                    terminal_ready = await before_terminal()
                    if terminal_ready is False:
                        # The hook has already created a durable continuation
                        # relation but could not publish its ordering event.
                        # Leave this bus open/non-terminal; the worker finalizer
                        # closes it so EventSource reconnect projects the
                        # durable parent→child relation before synthetic done.
                        self._terminal_frozen = True
                        return False
                except BaseException as exc:
                    self._terminal_frozen = True
                    if not isinstance(exc, Exception):
                        raise
                    # Fail closed.  The worker finalizer closes the bus and
                    # retries the durable drain marker; reconnect can then
                    # project the relation (or synthetic terminal) without a
                    # parent terminal frame overtaking accepted steer work.
                    logger.warning(
                        "turn_bus.before_terminal_failed turn=%s type=%s err=%s",
                        event.turn_id,
                        event.event_type,
                        exc,
                    )
                    return False
            # Events that entered before this synchronous fence are already in
            # front of the terminal in the ordered sink. Reject everything
            # later while the terminal persistence await is in flight.
            self._terminal_frozen = True
        # ρ Phase E — auto-populate seq on every emit. Bus is the single
        # source of truth for ordering; caller doesn't need to think about it.
        # parent_agent_id stays caller-set (bus doesn't know agent scope).
        if event.seq < 0:
            self._seq_counter += 1
            event.seq = self._seq_counter
        # fix/sse-reconnect-resilience — persist the bus seq INTO the payload.
        # Reconnect chat.py replay dumps row.payload directly, no to_sse. Previously replay
        # payload lacked seq → frontend handle() seenSeq dedup failed; full reconnect replay
        # doubled append reducers (chips/traceSteps/narrative). Write seq here into payload
        # for persistence/replay. setdefault preserves existing seq
        # (e.g. canvas.section_added section-order), outside handle() dedup path.
        event.payload.setdefault("seq", event.seq)
        # canvas-comprehensive Batch 1 (TD-1): monotonic per-turn event-order key on **every event**.
        # seq above retains canvas.section_added/patched/chunk section-ORDER via setdefault
        # (preset payload), unusable for canvas dedup; event.seq object property always bus-monotonic
        # (canvas caller only sets payload["seq"], never event.seq). Direct assignment,
        # not setdefault: evt_seq always equals bus seq, no caller sets it. Frontend canvas-sse
        # attachCanvasSSE sole dedup gate drops repeated full reconnect replay frames, fixing section_chunk
        # doubled accumulation (F2). Direct row.payload replay includes persisted evt_seq.
        event.payload["evt_seq"] = event.seq
        # Sprint chat-redesign-2026-05-21 Phase 1.1 — Step List Card step count.
        # Tracks step_done emit count so the terminal step_list_card_finalized
        # event can carry the correct step_count without callers threading it.
        if event.event_type in _STEP_OUTCOME_EVENT_TYPES:
            _record_step_outcome_fact(self, event)
        if event.event_type in ("turn_failed", "done") and event.parent_agent_id is None:
            # Chat turn feedback D5: the failure card's primary action rides next to the public
            # reason. Injected at this persistence seam (like seq) so every producer of a root
            # failure terminal carries it, and live == persisted == replayed.
            _attach_recovery_action(event.event_type, event.payload)
        if event.event_type == "step_done":
            try:
                self._step_count += 1  # type: ignore[has-type]
            except AttributeError:
                self._step_count = 1
        elif event.event_type == "tool_result":
            self._tool_result_count = int(getattr(self, "_tool_result_count", 0) or 0) + 1
        # fix/sse-fanout-bus: replay lacks parent_agent_id; to_sse injects only live
        # (below :178-179), not persisted payload. Fresh reconnect replay frontend cannot
        # separate peer prose / main answer (use-turn-runner.ts ~1077). Backfill at persistence
        # seam like seq; live to_sse unchanged.
        if event.parent_agent_id is not None:
            event.payload.setdefault("parent_agent_id", event.parent_agent_id)
        if self._persist_before_live:
            event.persist_before_delivery = True
        if event.persist_before_delivery and self._log_sink is None:
            sink_error = RuntimeError("durable event log sink is unavailable")
            if self._persist_before_live:
                self.latch_fatal_error(sink_error)
            logger.warning(
                "turn_bus.durable_log_sink_missing turn=%s type=%s",
                event.turn_id,
                event.event_type,
            )
            if self._persist_before_live:
                self.raise_if_failed()
            return False

        async def _publish_live(*, close_queue_terminal: bool = True) -> None:
            if self._fanout:
                self._events.append(event)
                if terminal:
                    self._closed = True
                async with self._cond:
                    self._cond.notify_all()
            else:
                await self._queue.put(event)
                if terminal and close_queue_terminal:
                    await self._queue.put(self._SENTINEL)
                    self._closed = True

        async def _persist_then_publish() -> bool:
            try:
                persisted = await self._log_sink(event)
            except Exception as e:
                if self._persist_before_live:
                    self.latch_fatal_error(e)
                logger.warning(
                    "turn_bus.durable_log_sink_failed turn=%s type=%s err=%s",
                    event.turn_id,
                    event.event_type,
                    e,
                )
                if self._persist_before_live:
                    self.raise_if_failed()
                return False
            if persisted is False:
                sink_error = RuntimeError(
                    "durable event log sink rejected the event",
                )
                if self._persist_before_live:
                    self.latch_fatal_error(sink_error)
                logger.warning(
                    "turn_bus.durable_log_sink_rejected turn=%s type=%s",
                    event.turn_id,
                    event.event_type,
                )
                if self._persist_before_live:
                    self.raise_if_failed()
                return False
            if event.suppress_live_delivery:
                self._seq_counter = max(self._seq_counter, event.seq)
                return True
            await _publish_live()
            return True

        if event.persist_before_delivery:
            # Once a durable write starts, persistence and compatibility live
            # publication are one cancellation-safe critical section. The
            # caller still observes cancellation, but only after the committed
            # frame is visible to existing subscribers as well as replay.
            critical_task = asyncio.create_task(
                _persist_then_publish(),
                name=f"turn-durable-publish:{event.turn_id}:{event.seq}",
            )
            cancellation: asyncio.CancelledError | None = None
            while not critical_task.done():
                try:
                    await asyncio.shield(critical_task)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
            try:
                delivered = critical_task.result()
            except asyncio.CancelledError as exc:
                if self._persist_before_live:
                    self.latch_fatal_error(exc)
                    self.raise_if_failed()
                raise
            if cancellation is not None:
                raise cancellation
            return delivered

        await _publish_live(close_queue_terminal=False)
        if self._log_sink is not None and not event.persist_before_delivery:
            try:
                await self._log_sink(event)
            except Exception as e:
                import logging as _lg

                _lg.getLogger(__name__).warning(
                    "turn_bus.log_sink_failed turn=%s type=%s err=%s",
                    event.turn_id,
                    event.event_type,
                    e,
                )
        if terminal and not self._fanout:
            await self._queue.put(self._SENTINEL)
            self._closed = True
        return True

    async def subscribe(self) -> AsyncIterator[OrchestratorEvent]:
        if not self._fanout:
            # Legacy single-consumer path (flag OFF rollback): old behavior verbatim.
            while True:
                item = await self._queue.get()
                if item is self._SENTINEL:
                    return
                assert isinstance(item, OrchestratorEvent)
                yield item
        # Fanout: subscriber private cursor reads ring from start; bus holds no subscriber state.
        cursor = 0
        while True:
            while cursor < len(self._events):
                ev = self._events[cursor]
                cursor += 1
                yield ev
            if self._closed:
                return
            async with self._cond:
                # Recheck under lock → no lost wakeup (emit append precedes any locked len read).
                if cursor >= len(self._events) and not self._closed:
                    await self._cond.wait()

    async def close(self) -> None:
        """Close transport delivery without fabricating a business event.

        Terminal v1 uses this only when the durable receipt committed but live
        fanout failed. Subscribers end the stream and reconnect to authoritative
        replay instead of waiting forever or observing an unpersisted error.
        """

        if self._closed:
            return
        self._closed = True
        if self._fanout:
            async with self._cond:
                self._cond.notify_all()
        else:
            await self._queue.put(self._SENTINEL)

    def snapshot(self) -> list[OrchestratorEvent]:
        """ring 当前内容的浅拷贝（测试 / debug 用）。仅 fanout 模式可用.

        legacy 模式 raise 而非静默返空 —— absence 型断言拿空集会假绿。
        """
        if not self._fanout:
            raise RuntimeError("snapshot() requires fanout mode (BOWHEAD_SSE_FANOUT)")
        return list(self._events)


def raise_if_terminal_bus_failed(bus: Any) -> None:
    """Raise a latched durable-writer failure at an explicit execution fence."""

    checker = getattr(bus, "raise_if_failed", None)
    if callable(checker) and (getattr(bus, "terminal_protocol_version", None) == 1 or bool(getattr(bus, "_persist_before_live", False))):
        checker()


class EventBusRegistry:
    """按 turn_id 路由的全局注册表（进程内单例）.

    Gateway mirror 订阅端：`bus = registry.get_or_create(turn_id, producer=False)`
    Orchestrator 发布端：同一 turn_id 拿到同一 bus
    可选 `default_log_sink` 由全局持久化 store 提供（所有 bus 共用）.
    """

    _SEQ_FLOOR_MAX = 512

    def __init__(self, default_log_sink: Any | None = None) -> None:
        self._buses: dict[str, TurnEventBus] = {}
        self._lock = asyncio.Lock()
        self._default_log_sink = default_log_sink
        # dingsiyan #11 (review P1-B): per-turn seq high-water mark, synchronous memory across bus generations.
        # Root: log_sink fire-and-forget (main.py create_task), DB persistence delayed.
        # Post-pause emit_out_of_band / resume seed reading only DB max_seq sees stale value on
        # **sequential** resolve→resume → clarification.resolved and turn.resumed
        # payload seq collision → frontend per-turn seenSeq dedup drops one.
        # Update high-water mark **synchronously** in emit_out_of_band/close, decoupling seed from async persistence.
        # Ordinary worker emit bypasses registry, never updates this; snapshot bus counter at close
        # plus DB max_seq fallback, max gives correct lower bound. FIFO cap prevents unbounded growth.
        self._seq_floor: dict[str, int] = {}

    def set_log_sink(self, sink: Any) -> None:
        """允许 gateway 启动后注入 sink（默认 registry 在 module import 时已建）."""
        self._default_log_sink = sink

    def _bump_seq_floor(self, turn_id: str, seq: int) -> None:
        """调用点必须持 self._lock（floor 与 bus dict 一致性）。"""
        if seq <= 0:
            return
        cur = self._seq_floor.get(turn_id)
        if cur is not None:
            if seq > cur:
                self._seq_floor[turn_id] = seq
            return
        if len(self._seq_floor) >= self._SEQ_FLOOR_MAX:
            # FIFO evicts oldest (dict insertion order); later out-of-band emit for evicted turn
            # falls back to DB max_seq (long-terminal turn persistence caught up, no collision).
            oldest = next(iter(self._seq_floor), None)
            if oldest is not None:
                self._seq_floor.pop(oldest, None)
        self._seq_floor[turn_id] = seq

    def seq_floor(self, turn_id: str) -> int:
        """已知的 per-turn seq 下界（0 = 未知）。gateway 的 seq 播种取
        max(DB max_seq, 此值)，避免 fire-and-forget 落库延迟导致的撞号。"""
        return self._seq_floor.get(turn_id, 0)

    async def projection_checkpoint(
        self,
        turn_id: str,
    ) -> tuple[TurnEventBus | None, int]:
        """Return the current producer generation or a stable retired floor.

        A durable parent→child relation can become visible before its
        ``persist_before_delivery`` event has entered the ordered log barrier.
        While a bus generation is still registered, replay and ``seq_floor``
        are therefore not a safe basis for synthesising an event: prior writes
        may still be draining and the live counter may be ahead of both.

        Subscriber-only buses are ignored.  The registry lock makes the safe
        alternatives atomic: callers either keep a producer-proven bus and
        drain that exact generation, or (only when it is ``None``) use the
        returned retired floor for projection.
        """
        async with self._lock:
            bus = self._buses.get(turn_id)
            if bus is not None and bus._producer_registered:
                return bus, 0
            return None, self._seq_floor.get(turn_id, 0)

    async def emit_if_local_producer(
        self,
        turn_id: str,
        event: OrchestratorEvent,
    ) -> bool:
        """Emit an auxiliary event only through a proven local producer.

        Steer acknowledgements are best-effort UI feedback, not durable work.
        A Gateway that does not own the worker must therefore never create or
        upgrade a registry bus for them: such a fake producer has nobody to
        close it and can make SSE wait forever.  A concurrent close is safe;
        ``TurnEventBus.emit`` returns ``False`` and the acknowledgement is
        simply omitted.
        """
        producer_bus, _stable_floor = await self.projection_checkpoint(turn_id)
        if producer_bus is None:
            return False
        try:
            return await producer_bus.emit(event)
        except TerminalDurabilityAbort as exc:
            # This registry boundary is used only for best-effort auxiliary
            # acknowledgements. Keep worker-side direct ``bus.emit`` fail-stop,
            # but let the existing ``except Exception`` caller omit the ack
            # after its steer receipt has already committed.
            raise RuntimeError(
                "auxiliary_event_durability_unavailable",
            ) from exc

    async def drain_turn_persistence(self, turn_id: str) -> None:
        """Wait for the configured ordered sink's per-Turn tail, if supported."""
        drain_turn = getattr(self._default_log_sink, "drain_turn", None)
        if callable(drain_turn):
            await drain_turn(turn_id)

    async def get_or_create(
        self,
        turn_id: str,
        *,
        initial_seq: int = 0,
        producer: bool = True,
        exclusive_producer: bool = False,
        persist_before_live: bool = False,
    ) -> TurnEventBus:
        async with self._lock:
            # dingsiyan #11 (review P1-B): **never** read seq_floor here; consumed only at two explicit
            # seed sites (gateway turn_seq_seed resume initial_seq; below emit_out_of_band
            # transient seed); caller already incorporated floor into initial_seq.
            # Keep get_or_create pure, no floor access, avoiding altered seq expectations for same-turn reuse
            # in ordinary subscribe/worker paths.
            bus = self._buses.get(turn_id)
            if bus is not None and producer and exclusive_producer and bus._producer_registered:
                raise RuntimeError("turn producer already admitted")
            if bus is None:
                bus = TurnEventBus(
                    log_sink=self._default_log_sink,
                    initial_seq=initial_seq,
                    producer_registered=producer,
                    persist_before_live=persist_before_live,
                )
                self._buses[turn_id] = bus
            else:
                if producer:
                    bus._producer_registered = True
                if persist_before_live:
                    bus._persist_before_live = True
                if initial_seq > 0:
                    bus.seed_seq_counter(initial_seq)
            return bus

    async def emit_out_of_band(
        self,
        turn_id: str,
        event: OrchestratorEvent,
        *,
        fallback_initial_seq: int = 0,
    ) -> str:
        """HTTP 端点等「bus 生命周期之外」的发射唯一入口（dingsiyan #11 根治）.

        pause 后（done(clarification_pending) 已 emit）再往 turn 上补事件有两个坑，
        裸 ``get_or_create`` + ``emit`` 两个都踩（prod 实证 t_1ccf9e0aec11）：

        - **<5s 窗口**：旧 bus ``_closed=True`` 但 worker 的延迟 close 还没把它 pop，
          ``get_or_create`` 返回死 bus → ``emit`` 早退 → 事件既不投递也不落库；
        - **>5s**：bus 已 pop，``get_or_create`` 建了个 seq 从 0 计数的新 bus →
          payload seq 与 turn 早期事件撞号 → FE 两层 seq 去重把重放中的该事件当
          重复丢弃（clarification.resolved 收卡逆操作因此在生产必死）。

        语义：
        - registry 里有 **open** bus（如 token-kind 澄清在 turn 进行中被 resolve）
          → 正常 ``emit``（live 投递 + 落库），返回 ``"live"``。
        - closed / absent → 用一个**不注册**的 transient bus（seed 到
          ``fallback_initial_seq``，共享 log_sink）emit —— 只为把事件带着正确
          seq 落库（closed bus 定义上没有订阅者，投递本就只能靠 replay）。
          不往 registry 塞新 bus = 无泄漏、不与 resume 的代际切换打架。
          返回 ``"persisted"``。

        seq 播种（review P1-B 修正）：seed 取 ``max(fallback_initial_seq, 内存高水位)``。
        ``fallback_initial_seq`` 由调用方传 ``turn_log_store.max_seq(turn_id)``——但
        log_sink 是 **fire-and-forget**，前一条补发事件的落库有延迟，DB max_seq 会读到
        陈旧值。**这不是「并发」问题而是「顺序 + 异步落库」问题**（旧 docstring 归因于
        「单用户互斥即安全」是错的：resolve→resume 是顺序两步，resume 读 max_seq 时
        resolve 的行未必已落库 → 两者撞号）。内存高水位（emit 后 ``_bump_seq_floor``、
        同步更新）补上这个缺失的因果屏障：下一次 out-of-band emit / resume seed 一定
        看到本次的 seq，不撞号。

        closed-race（review BE-P2-2）：open 分支的 ``emit`` 在锁外 await；若此刻并发
        ``close`` 把 bus 置 ``_closed``，``emit`` 返回 False → **降级到 transient 重发**，
        事件不丢。
        """
        async with self._lock:
            floor = self._seq_floor.get(turn_id, 0)
            seed = max(int(fallback_initial_seq), floor)
            bus = self._buses.get(turn_id)
            if bus is not None:
                # dogfood #11: registered bus, open **or** closed, _seq_counter is **synchronous memory authority**,
                # stronger than potentially stale DB max_seq / floor.
                # In <5s window, closed old worker bus not yet popped has real tail counter
                # (e.g. done 27), DB only 25; ignoring it seeds resolved before paused tail
                # (observed evt_seq=26 < done27, collides canvas.done; other timing
                # collides handle channel and drops). Include counter: resolved follows paused tail.
                seed = max(seed, bus._seq_counter)
            if bus is not None and not bus._closed:
                # Defensive seed: open bus created by subscriber get_or_create without seed
                # (e.g. /chat/stream for absent turn) may lag already-persisted rows.
                bus.seed_seq_counter(seed)
                target, mode = bus, "live"
            else:
                target = TurnEventBus(
                    log_sink=self._default_log_sink,
                    initial_seq=seed,
                )
                mode = "persisted"
        try:
            delivered = await target.emit(event)
            if not delivered and mode == "live":
                # Open bus concurrently closes outside lock (BE-P2-2 TOCTOU) → transient persistence fallback.
                target = TurnEventBus(
                    log_sink=self._default_log_sink,
                    initial_seq=seed,
                )
                mode = "persisted"
                delivered = await target.emit(event)
        except TerminalDurabilityAbort as exc:
            # Out-of-band HTTP/background boundaries distinguish required from
            # best-effort work with ordinary ``except Exception`` handlers.
            # Preserve failure for required decisions without letting a
            # BaseException escape after their business fact already committed.
            raise RuntimeError(
                "out_of_band_event_durability_unavailable",
            ) from exc
        if delivered:
            async with self._lock:
                self._bump_seq_floor(turn_id, event.seq)
        elif event.persist_before_delivery:
            raise RuntimeError(
                f"durable out-of-band event rejected: {event.event_type}",
            )
        logger.debug(
            "turn_bus.emit_out_of_band turn=%s type=%s mode=%s seed=%d seq=%d",
            turn_id,
            event.event_type,
            mode,
            seed,
            event.seq,
        )
        return mode

    async def close(
        self,
        turn_id: str,
        *,
        expected: TurnEventBus | None = None,
    ) -> None:
        """turn 结束后清理.

        legacy 模式：必须在订阅端消费完毕后调用（订阅端读到 SENTINEL 才退出）。
        fanout 模式：订阅者持 bus 直接引用，pop 不影响 mid-drain ——
        5s 延迟（chat.py _run_turn_and_cleanup）退化为纯重连宽限窗；
        且 close 会唤醒/终止仍在等待的订阅者（done 永不来的 crash 路径
        不再挂死到 FE 10min give-up）。

        ``expected``（impl review P1 代际守卫）：clarification resume 复用同
        turn_id（close 旧 bus → get_or_create 新代际）。旧 worker 的延迟 5s
        close 不带守卫会 pop+关掉 resume 的新代际 bus —— 后续 emit 在 log_sink
        **之前**早退 = 事件既不进流也不落库。worker 传自己持有的 bus；registry
        里已换代则整体跳过（pop 和状态都不碰）。不传 = 无条件 close（resume
        回收路径的既有语义）。
        """

        async def _close_exact_generation() -> None:
            async with self._lock:
                bus = self._buses.get(turn_id)
                if expected is not None and bus is not None and bus is not expected:
                    return  # New generation: not caller-created bus, leave alone.
                # dingsiyan #11 (review P1-B): snapshot final bus seq into high-water mark for resume
                # get_or_create seed across generations (worker emit bypasses registry, only captured here).
                if bus is not None:
                    self._bump_seq_floor(turn_id, bus._seq_counter)
                self._buses.pop(turn_id, None)
            if bus is None:
                return
            if not getattr(bus, "_fanout", False):
                await bus.close()
                return
            was_closed = bus._closed
            bus._closed = True
            # Removal and wakeup are one cancellation-safe primitive. A caller
            # may observe cancellation only after every subscriber can observe
            # the closed state; otherwise no later exact close can find this
            # generation to repair the missed notification.
            async with bus._cond:
                bus._cond.notify_all()
            if not was_closed:
                # Closed without done/error emit = abnormal signal (crash-path detector).
                logger.warning(
                    "turn_bus.closed_without_terminal turn=%s ring=%d",
                    turn_id,
                    len(bus._events),
                )
            else:
                logger.debug(
                    "turn_bus.closed turn=%s ring=%d buses=%d",
                    turn_id,
                    len(bus._events),
                    len(self._buses),
                )

        close_task = asyncio.create_task(
            _close_exact_generation(),
            name=f"turn_bus_close:{turn_id}",
        )
        cancellation: asyncio.CancelledError | None = None
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        close_task.result()
        if cancellation is not None:
            raise cancellation


# Process-global singleton; replace implementation when M2 moves to Redis.
_default_registry: EventBusRegistry | None = None


def get_event_registry() -> EventBusRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = EventBusRegistry()
    return _default_registry
