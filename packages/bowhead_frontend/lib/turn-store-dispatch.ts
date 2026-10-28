import { projectDocumentDraftOpen } from "./document-canvas-events";
import { parseCoachCanvasBinding } from "./coach-canvas-target";
import { projectCoachResourceOpen } from "./coach-resource-editors";
import { projectCoachOperationOpen } from "./coach-operations";
import { projectCoachAlignmentCard } from "./coach-alignment-cards";
/**
 * turn-store-dispatch — route raw SSE event payloads into lib/turn-store
 * mutators. Lives next to use-turn-runner.ts so the dual-emit window
 * (legacy tool_call/tool_result + new step_*) wires cleanly: every event
 * frame goes through one pre-handler call that fans out to turn-store
 * mutators, then the existing setState reducer in use-turn-runner runs
 * for the legacy state shape.
 *
 * Phase 1.2: handles step_started / step_progress / step_done /
 * step_list_card_created / step_list_card_finalized / turn.resumed /
 * emoji_ack / turn.follow_up. Other events are no-ops here (legacy dispatch
 * handles them). See docs/design/chat-pipeline.md for the authoritative event list.
 *
 * Most routing keys use payload.card_id (== turn_id). Lifecycle events that
 * intentionally omit it (currently turn.resumed) use payload.turn_id.
 */

import type {
  StepDetail,
  StepCodeInput,
  StepKind,
  StepOutputArtifact,
  StepState,
  SubStepResult,
  ToolName,
} from "@/components/chat/step-list/types";

import { normalizePeerFailureReasonCode } from "./error-messages";
import {
  normalizeErrorDiagnostics,
  parseErrorDiagnosticsWire,
  type ErrorDiagnostics,
} from "./error-presentation";
import { prettifyStepLabel } from "./prettify-step-label";
import { isLegacyFailureAnswerReason, parseStepOutcome } from "./turn-feedback";
import {
  applyApprovalExecuted,
  applyApprovalRejected,
  applyApprovalRequired,
  applyCardCreated,
  applyCardFinalized,
  applyCardResumed,
  applyCollaborationRevision,
  applyTurnDuration,
  applyTurnStarted,
  applyCoachCanvasTarget,
  applyEmojiAck,
  applyFinalAnswer,
  applyModelRetryScheduled,
  applyNarrativeAppend,
  applyNarrativeSuperseded,
  applyPeerAgentAttribution,
  applyPeerName,
  applyPeerProse,
  applyPeerProseKey,
  applyPeerRestart,
  applyPeerTerminal,
  applyStepDone,
  applyStepProgress,
  applyStepOutcome,
  applyStepStarted,
  dropLegacyFailureAnswer,
  getTurnSnapshot,
  pushDraftBeat,
  pushLiteBeat,
  pushPickerBeat,
  pushPlanBeat,
  applyTurnBlockedPlan,
  applyTurnDocumentRevision,
  clearModelRetry,
  parseDocumentRevisionReceipt,
  type ModelRetryStatus,
} from "./turn-store";
import { applyDraftEvent } from "./config-copilot-draft";
import { projectConfigurationCanvasOpen, projectConfigurationCanvasProposal } from "./configuration-canvas-events";
import { applyLiteProposal } from "./config-copilot-lite";
import type { ConfigDraftUpdatedPayload, ConfigLiteProposePayload } from "./types";
import {
  applyPlanProposed,
  applyPlanStatus,
  applyPlanStepBlocked,
  applyPlanStepDone,
  applyPlanStepFailed,
  applyPlanStepStarted,
} from "./plan-store";
import { parseThetaQuestions } from "./clarification-questions";
import type { ThetaClarificationState } from "./types";
import type {
  EmojiAckEvent,
  NarrativeDeltaEvent,
  StepDoneEvent,
  StepListCardCreatedEvent,
  StepListCardFinalizedEvent,
  StepProgressEvent,
  StepStartedEvent,
  StepType,
  TurnFollowUpEvent,
  WireStepError,
  WireSubStep,
} from "./sse-events";
import { normalizeStepError, normalizeStepState } from "./sse-events";

/**
 * Phase 1.7-A — per-turn narrative cursor. A contiguous run of
 * `narrative.delta` events shares one FE-derived `segId`; any other
 * beat-emitting event (step_started / final_answer) invalidates the cursor
 * so the next narrative.delta starts a new segment. Map is keyed by
 * card_id (== turn_id) so concurrent turns don't share cursor state.
 *
 * `seq` is monotonic across the turn lifetime so `_invalidateNarrativeSeg`
 * can mark the current segId as expired without losing the counter (a
 * subsequent narrative.delta will advance seq, yielding a unique segId).
 * `active=false` means "current segId is expired — next narrative.delta
 * starts a new segment". `active=true` means "current segId can be
 * appended to by next narrative.delta".
 */
interface NarrativeCursor {
  segId: string;
  seq: number;
  active: boolean;
}

function applyAgentAttribution(
  turnId: string,
  peerId: string,
  payload: Record<string, unknown>,
): void {
  if (!turnId || !peerId) return;
  const read = (key: string): string | undefined =>
    typeof payload[key] === "string" ? payload[key] : undefined;
  applyPeerAgentAttribution({
    turnId,
    peerId,
    agentId: read("agent_id"),
    agentRevisionId: read("agent_revision_id"),
    agentReleaseId: read("agent_release_id"),
    agentExecutionId: read("agent_execution_id"),
    displayNameSnapshot: read("display_name_snapshot"),
  });
}
const _narrativeCursor = new Map<string, NarrativeCursor>();
// Per-turn applied-seq sets for replay dedupe. Bounded so a long-lived session
// can't leak one Set per turn forever; the active turn is never evicted because
// same-turn resume reuses its turn_id (insertion-ordered eviction drops the
// oldest turns only). Exported const so the eviction is testable.
export const _APPLIED_SEQ_MAX_TURNS = 64;
const _appliedSeqByTurn = new Map<string, Set<number>>();

function peerFailureDiagnostics(
  payload: Record<string, unknown>,
  reasonCode: string,
  turnId: string,
): ErrorDiagnostics | undefined {
  const parsed = parseErrorDiagnosticsWire(payload.diagnostics);
  if (
    parsed?.disclosure !== "authenticated" ||
    parsed.diagnosticCode !== "chat_peer_failed" ||
    !turnId ||
    parsed.turnId !== turnId
  ) {
    return undefined;
  }
  return normalizeErrorDiagnostics({
    ...parsed,
    code: reasonCode,
  });
}

function _currentNarrativeSeg(turnId: string): string {
  const cur = _narrativeCursor.get(turnId);
  if (cur && cur.active) return cur.segId;
  // Advance to next segment.
  const seq = (cur?.seq ?? 0) + 1;
  const segId = `${turnId}::seg-${seq}`;
  _narrativeCursor.set(turnId, { segId, seq, active: true });
  return segId;
}

function _invalidateNarrativeSeg(turnId: string): void {
  const prev = _narrativeCursor.get(turnId);
  if (!prev) return;
  // Mark as expired; keep seq so the next narrative.delta gets a fresh,
  // monotonically-larger segId.
  _narrativeCursor.set(turnId, { ...prev, active: false });
}

/**
 * Test helper — clears dispatch caches across all turns.
 * Used by vitest afterEach to ensure deterministic segId/seq derivation.
 */
export function _resetNarrativeCursorsForTesting(): void {
  _narrativeCursor.clear();
  _appliedSeqByTurn.clear();
}

/**
 * prod-bug-bash-039 #13 — `isNarrativeSegActive(turnId, segId)` lets BeatStream
 * decide per-narrative-beat whether the streaming cursor should still blink.
 *
 * V8 spec (variant-a.jsx:343) renders each narrative chunk with its OWN
 * streaming flag — independent of beats position. A narrative beat is
 * "streaming" iff it owns the current active cursor (segId match + active).
 * Subsequent beats (step / answer / picker) invalidate the cursor (line 132,
 * 195, 235) so the prior narrative naturally stops blinking.
 */
export function isNarrativeSegActive(turnId: string, segId: string): boolean {
  const cur = _narrativeCursor.get(turnId);
  return cur != null && cur.active === true && cur.segId === segId;
}

/**
 * Try to dispatch a raw SSE frame into the turn-store. Returns true if
 * the event was claimed (so caller may skip its own legacy handling for
 * that event type), false otherwise.
 */
export function dispatchToTurnStore(
  eventName: string,
  rawPayload: unknown,
  source: "live" | "replay" = "live",
  streamTurnId?: string,
): boolean {
  if (!rawPayload || typeof rawPayload !== "object") return false;
  const payload = rawPayload as Record<string, unknown>;
  const cardId = pickCardId(payload);
  // A pending model retry is a transient running status: the next model output
  // or terminal event clears it. Live SSE and replay share this rule, so a
  // replayed finished turn always ends without it.
  if (eventName !== "llm.retry_scheduled" && !MODEL_RETRY_PRESERVING_EVENTS.has(eventName)) {
    const retryTurnId = cardId ?? pickTurnId(payload) ?? streamTurnId;
    const eventSeq = typeof payload.seq === "number" && Number.isFinite(payload.seq) ? payload.seq : undefined;
    if (retryTurnId) clearModelRetry(retryTurnId, eventSeq);
  }

  switch (eventName) {
    case "llm.retry_scheduled": {
      // Live frames carry no card_id; replay backfills it. Fall back to the stream's turn.
      const turnId = cardId ?? pickTurnId(payload) ?? streamTurnId ?? null;
      if (!turnId) return false;
      if (shouldSkipAppliedSeq(turnId, payload)) return true;
      const status = parseModelRetryStatus(payload);
      if (status) applyModelRetryScheduled(turnId, status);
      return true;
    }

    // report-streaming D2: record this turn's start for the canvas waiting timer.
    // Use turn_id, not card_id: `turn_started` precedes `step_list_card_created`,
    // before any card exists. `applyTurnStarted` accepts only the first write (full replay on reconnect sends
    // it again; overwriting would reset the timer to 0 after a user has already waited two minutes).
    case "turn_started": {
      const turnId = pickTurnId(payload);
      if (!turnId) return false;
      const originalTarget = parseCoachCanvasBinding(payload.coach_canvas_target);
      if (originalTarget && originalTarget.session_id === payload.session_id && originalTarget.workspace_id === payload.workspace_id)
        applyCoachCanvasTarget(turnId, originalTarget);
      const startedAt = payload.started_at;
      applyTurnStarted({
        turnId,
        startedAt: typeof startedAt === "string" ? startedAt : null,
      });
      const revision = parseDocumentRevisionReceipt(payload.document_revision);
      if (revision) applyTurnDocumentRevision(turnId, revision);
      return true;
    }

    case "step_list_card_created": {
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      const ev = payload as unknown as StepListCardCreatedEvent;
      applyCardCreated({ turnId: ev.card_id });
      return true;
    }

    case "step_list_card_finalized": {
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      const ev = payload as unknown as StepListCardFinalizedEvent;
      applyCardFinalized({
        turnId: ev.card_id,
        finalState: ev.state,
      });
      return true;
    }

    case "turn.resumed": {
      // HITL continues the SAME turn_id on a fresh bus generation. Reopen the
      // canonical card before new steps arrive; return false so the legacy
      // reducer still owns clarification tombstones and answered-summary state.
      // Live turn.resumed intentionally has no card_id, so route by turn_id.
      const turnId =
        typeof payload.turn_id === "string" && payload.turn_id
          ? payload.turn_id
          : null;
      if (!turnId) return false;
      if (!shouldSkipAppliedSeq(turnId, payload)) {
        applyCardResumed({ turnId });
      }
      return false;
    }

    case "agent.collaboration": {
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      if (typeof payload.collaboration_revision === "number") {
        applyCollaborationRevision(cardId, payload.collaboration_revision);
      }
      return true;
    }

    case "step_started": {
      // Planning Mode: the plan-scoped step_started carries plan_id + step_ids[]
      // (no card_id) — a different payload sharing the event name. Route it to
      // plan-store and do NOT fall into the step-list (card_id) handling.
      // 2026-07: `!payload.card_id` is symmetric with the step_done branch; a payload with card_id
      // always belongs to step-list, even if someone adds plan_id in the future.
      if (typeof payload.plan_id === "string" && Array.isArray(payload.step_ids)
          && !payload.card_id) {
        applyPlanStepStarted(payload.plan_id, (payload.step_ids as unknown[]).map((v) => String(v)));
        return true;
      }
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      const ev = payload as unknown as StepStartedEvent;
      // chat-solidify follow-up (#4): clarification_ask is an end-of-turn
      // sentinel (the Lead's "ask the user" hand-off), NOT a real tool step. Its
      // raw args (label + summary) fall through _prettify_input_preview to a raw
      // JSON blob and rendered as bare JSON in the step list (dogfood 0.72.8).
      // The clarification CARD (clarification_required event) is the real
      // surface — skip creating a trace step beat for it entirely.
      if (ev.tool_name === "clarification_ask") return true;
      // Phase 1.7-A — step beat invalidates narrative segId so the next
      // narrative.delta starts a fresh segment (interleaving rule).
      _invalidateNarrativeSeg(ev.card_id);
      const prettifiedLabel = prettifyStepLabel(
        ev.input_preview,
        ev.tool_name ?? null,
      );
      applyStepStarted({
        turnId: ev.card_id,
        stepId: ev.step_id,
        parentStepId: ev.parent_step_id ?? null,
        concurrencyGroupId: ev.concurrency_group_id ?? null,
        stepType: ev.step_type as StepKind,
        toolName: (ev.tool_name as ToolName | null | undefined) ?? null,
        headerZh: ev.header_zh,
        // Phase 1.7-A — coerced label per tech-design §2.3 (never raw JSON).
        label: prettifiedLabel || null,
        startedAt: ev.started_at,
        callId: ev.call_id ?? null,
        subagentId: ev.subagent_id ?? null,
        codeInput: mapCodeInput(ev.code_input),
        evtSeq: readEvtSeq(payload),
      });
      return true;
    }

    case "step_progress": {
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      const ev = payload as unknown as StepProgressEvent;
      applyStepProgress({
        turnId: ev.card_id,
        stepId: ev.step_id,
        contentDelta: ev.content_delta ?? undefined,
      });
      return true;
    }

    case "step_done": {
      // Planning Mode: plan-scoped step_done carries plan_id + step_id (no
      // card_id). Route to plan-store; the step-list path needs card_id.
      if (typeof payload.plan_id === "string" && !payload.card_id && typeof payload.step_id === "string") {
        // graph-mode batch 1: pass step_done.verify (verdict/unmet) through to plan-store.
        applyPlanStepDone(payload.plan_id, payload.step_id, payload.verify);
        return true;
      }
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      const ev = payload as unknown as StepDoneEvent;
      // 0.39.4 bug B/C fix: BE step_done payload carries `result_inline` (e.g.
      // bi_query returns {columns, rows} table) but FE used to ignore it →
      // `step.detail` stayed null → `hasDetail=false` → step NOT expandable in
      // any path (live OR hydrate). User reported "跑当中并行 step 展不开,
      // 查完手动刷新后可以" — actually neither was, F5 just refreshed UI cleanly.
      // Build a typed StepDetail from result_inline when it matches a known
      // shape; fall through to existing pass-through `detail` field for any
      // already-typed payloads.
      const detail = _deriveDetailFromInline(payload) ?? (payload.detail as StepDetail | null | undefined) ?? null;
      applyStepDone({
        turnId: ev.card_id,
        stepId: ev.step_id,
        state: normalizeStepState(ev.state) as Extract<StepState, "done" | "failed" | "partial">,
        summary: ev.output_preview ?? null,
        detail,
        subSteps: mapSubSteps(ev.sub_steps),
        outputArtifacts: mapOutputArtifacts(payload),
        durationMs: ev.duration_ms ?? null,
        error: mapError(ev.error ?? null),
      });
      return true;
    }

    // ── Config copilot batch 0.5: lightweight confirmation strip (beat type, same topology as plan cards, replay-safe) ──
    case "coach.resource_open": {
      projectCoachResourceOpen(payload);
      return true;
    }
    case "coach.operation_open": {
      projectCoachOperationOpen(payload);
      return true;
    }
    case "coach.alignment_card": {
      projectCoachAlignmentCard(payload);
      return true;
    }
    case "document.draft_open": {
      if (streamTurnId) projectDocumentDraftOpen(payload, streamTurnId);
      return true;
    }
    case "configuration.canvas_open": {
      projectConfigurationCanvasOpen(payload, source);
      return true;
    }
    case "config.lite_propose": {
      const proposalId = typeof payload.proposal_id === "string" ? payload.proposal_id : "";
      const rows = Array.isArray(payload.rows) ? payload.rows : null;
      // Beat anchor: payload.turn_id (explicit backend field, review P0-1) || cardId; both empty / bad payload → discard without breaking replay.
      const turnId = typeof payload.turn_id === "string" && payload.turn_id ? payload.turn_id : cardId;
      if (!proposalId || !rows || !turnId) return true;
      applyLiteProposal({
        proposal_id: proposalId,
        turn_id: turnId,
        form_id: typeof payload.form_id === "string" ? payload.form_id : "",
        ...(typeof payload.review_page === "string" ? { review_page: payload.review_page } : {}),
        workspace_id: typeof payload.workspace_id === "string" ? payload.workspace_id : null,
        workspace_name: typeof payload.workspace_name === "string" ? payload.workspace_name : null,
        ...(typeof payload.team_id === "string" ? { team_id: payload.team_id } : {}),
        ...(payload.settings_target && typeof payload.settings_target === "object"
          ? { settings_target: payload.settings_target as ConfigLiteProposePayload["settings_target"] }
          : {}),
        rows: rows as ConfigLiteProposePayload["rows"],
        notes: (payload.notes && typeof payload.notes === "object" ? payload.notes : {}) as Record<string, string>,
      });
      projectConfigurationCanvasProposal(turnId, proposalId);
      pushLiteBeat(turnId, proposalId);
      return true;
    }

    // ── Config copilot batch 1-b: config draft (tier-2 drawer; chip uses a beat, automatic drawer opening is a wire-layer
    //    live-only side path outside the dispatcher; replay does not open drawers, tech-design-batch1b §3-6) ──
    case "config.draft_updated": {
      const draftId = typeof payload.draft_id === "string" ? payload.draft_id : "";
      const fields = payload.fields && typeof payload.fields === "object" ? payload.fields : null;
      const turnId = typeof payload.turn_id === "string" && payload.turn_id ? payload.turn_id : cardId;
      if (!draftId || !fields || !turnId) return true;
      applyDraftEvent({
        draft_id: draftId,
        turn_id: turnId,
        form_id: typeof payload.form_id === "string" ? payload.form_id : "",
        created: payload.created === true,
        updated_fields: Array.isArray(payload.updated_fields) ? (payload.updated_fields as string[]) : [],
        fields: fields as Record<string, unknown>,
        field_meta: (payload.field_meta && typeof payload.field_meta === "object"
          ? payload.field_meta
          : {}) as ConfigDraftUpdatedPayload["field_meta"],
        notes: (payload.notes && typeof payload.notes === "object" ? payload.notes : {}) as Record<string, string>,
        workspace_id: typeof payload.workspace_id === "string" ? payload.workspace_id : null,
        ...(payload.target && typeof payload.target === "object"
          ? { target: payload.target as ConfigDraftUpdatedPayload["target"] }
          : {}),
        ...(Array.isArray(payload.changed_fields_cumulative)
          ? { changed_fields_cumulative: payload.changed_fields_cumulative as string[] }
          : {}),
      });
      pushDraftBeat(turnId, draftId);
      return true;
    }

    // ── Planning Mode (BOWHEAD_PLANNING_MODE) ──────────────────────
    case "plan_proposed": {
      const planId = typeof payload.plan_id === "string" ? payload.plan_id : "";
      if (!planId) return true;
      applyPlanProposed(payload as Parameters<typeof applyPlanProposed>[0]);
      // Anchor the card inline in the proposal turn's beat stream.
      const turnId = typeof payload.turn_id === "string" && payload.turn_id ? payload.turn_id : cardId;
      if (turnId) pushPlanBeat(turnId, planId);
      return true;
    }

    case "step_blocked": {
      if (typeof payload.plan_id !== "string" || typeof payload.step_id !== "string") return false;
      applyPlanStepBlocked(
        payload.plan_id,
        payload.step_id,
        String(payload.status ?? "blocked"),
        typeof payload.error_kind === "string" ? payload.error_kind : null,
        typeof payload.reason === "string" ? payload.reason : null,
      );
      return true;
    }

    case "step_failed": {
      if (typeof payload.plan_id !== "string" || typeof payload.step_id !== "string") return false;
      applyPlanStepFailed(
        payload.plan_id,
        payload.step_id,
        typeof payload.error_kind === "string" ? payload.error_kind : null,
        payload.verify,
        typeof payload.reason === "string" ? payload.reason : null,
      );
      return true;
    }

    case "plan_running": {
      if (typeof payload.plan_id === "string") applyPlanStatus(payload.plan_id, "running");
      return true;
    }
    case "plan_completed": {
      if (typeof payload.plan_id === "string") applyPlanStatus(payload.plan_id, "completed");
      return true;
    }
    case "plan_blocked": {
      if (typeof payload.plan_id === "string" && payload.plan_id) {
        applyPlanStatus(payload.plan_id, "blocked");
        // The failure card of this Turn offers resuming exactly this plan (live and replay alike).
        const blockedTurnId = streamTurnId ?? pickTurnId(payload) ?? cardId;
        if (blockedTurnId) applyTurnBlockedPlan(blockedTurnId, payload.plan_id);
      }
      return true;
    }
    case "plan_failed": {
      if (typeof payload.plan_id === "string") applyPlanStatus(payload.plan_id, "failed");
      return true;
    }

    case "agent_spawned": {
      // P4a (chat-redesign): agent_spawned is the UNIVERSAL, authoritative
      // peer-identity event — subagent.py `_run_subagent` emits it at EVERY peer's
      // runner-start, plan OR non-plan (planning mode doesn't change this), with
      // subagent_id + profile (== the backend agent_name, e.g. "business-analyst").
      // The plan-execution path spawns peers directly via _run_subagent → only
      // agent_spawned (no subagent_spawn tool step, no peer_dispatched). The
      // non-plan path ALSO emits peer_dispatched first (the Lead-tool dispatch),
      // but that's redundant — agent_spawned follows and is the single source we
      // key on. Verified live on 116. The event omits card_id → the runner injects
      // it. peerNames[subagent_id] → agent-meta identity for the agent block.
      if (!cardId) return false;
      const sid = typeof payload.subagent_id === "string" ? payload.subagent_id : "";
      const profile = typeof payload.profile === "string" ? payload.profile : "";
      const task = typeof payload.task === "string" ? payload.task : undefined;
      const displayName =
        typeof payload.display_name === "string"
          ? payload.display_name
          : typeof payload.display_name_snapshot === "string"
            ? payload.display_name_snapshot
            : undefined;
      // Skip the Lead's own spawn: its steps belong to the Lead flow (light block /
      // narrator), not a peer AgentBlock. Only real peers become peerNames entries.
      if (sid && profile && profile !== "lead") {
        applyPeerName(cardId, sid, profile, task, displayName);
        applyAgentAttribution(cardId, sid, payload);
        const status = payload.status;
        if (status === "completed") {
          applyPeerTerminal({
            turnId: cardId,
            peerId: sid,
            status: "succeeded",
            evtSeq: readEvtSeq(payload),
            ...(typeof payload.duration_ms === "number"
              ? { durationMs: payload.duration_ms }
              : {}),
          });
        } else if (status === "partial") {
          applyPeerTerminal({
            turnId: cardId,
            peerId: sid,
            status: "partial",
            evtSeq: readEvtSeq(payload),
            ...(typeof payload.stop_reason === "string"
              ? { stopReason: payload.stop_reason }
              : {}),
            ...(typeof payload.duration_ms === "number"
              ? { durationMs: payload.duration_ms }
              : {}),
          });
        } else if (status === "failed" || status === "max_iterations_exhausted") {
          // Do not project payload.error here: graph/legacy finish events predate
          // the credential scrub contract. A later canonical summary may safely
          // enrich this terminal with a scrubbed reason.
          applyPeerTerminal({
            turnId: cardId,
            peerId: sid,
            status: "failed",
            evtSeq: readEvtSeq(payload),
            ...(typeof payload.duration_ms === "number"
              ? { durationMs: payload.duration_ms }
              : {}),
          });
        } else if (status === undefined || status === "running") {
          // Runner start. For a settled Agent this is a same-turn continuation.
          applyPeerRestart({
            turnId: cardId,
            peerId: sid,
            evtSeq: readEvtSeq(payload),
          });
        }
      }
      return false; // informational — the legacy reducer also builds peersDispatched
    }

    case "peer_dispatched": {
      // dogfood s_3e854 ④: attach this peer's id to its subagent_spawn step so the
      // live peer prose (partial_answer.parent_agent_id = the same id) renders on
      // that row. Backend includes card_id in the payload (subagent.py:4549). Return
      // FALSE so the legacy handle() switch still updates the PeersDispatched tray.
      // NOTE: peer_dispatched (non-plan Lead-tool path) also carries the identity
      // (peer_name + subagent_id, subagent.py:4550-4553), but the agent_spawned
      // that always follows is the single source for peerNames — see above.
      if (cardId) {
        const spawnId =
          (typeof payload.spawn_id === "string" && payload.spawn_id) ||
          (typeof payload.subagent_id === "string" && payload.subagent_id) ||
          "";
        const parentStepId =
          typeof payload.parent_step_id === "string"
            ? payload.parent_step_id
            : undefined;
        if (spawnId) {
          applyPeerProseKey(cardId, spawnId, parentStepId);
          applyAgentAttribution(cardId, spawnId, payload);
        }
      }
      return false;
    }

    case "lead.subagent_summary": {
      const turnId = cardId ?? pickTurnId(payload);
      const peerId =
        (typeof payload.spawn_id === "string" && payload.spawn_id) ||
        (typeof payload.subagent_id === "string" && payload.subagent_id) ||
        "";
      const status = payload.status;
      if (
        !turnId ||
        !peerId ||
        (status !== "succeeded" &&
          status !== "succeeded_with_warnings" &&
          status !== "failed" &&
          status !== "partial")
      ) {
        return false;
      }
      applyAgentAttribution(turnId, peerId, payload);
      if (shouldSkipAppliedSeq(turnId, payload)) return false;
      const failureReason =
        status === "failed"
          ? normalizePeerFailureReasonCode(payload.failure_reason)
          : undefined;
      applyPeerTerminal({
        turnId,
        peerId,
        status,
        evtSeq: readEvtSeq(payload),
        durationMs:
          typeof payload.duration_ms === "number"
            ? payload.duration_ms
            : undefined,
        failureReason,
        failureDiagnostics: failureReason
          ? peerFailureDiagnostics(payload, failureReason, turnId)
          : undefined,
        ...(status === "partial" && typeof payload.stop_reason === "string"
          ? { stopReason: payload.stop_reason }
          : {}),
        ...(typeof payload.resume_checkpoint_id === "string" && payload.resume_checkpoint_id
          ? { resumeCheckpointId: payload.resume_checkpoint_id }
          : {}),
        ...(typeof payload.resume_refused === "string" && payload.resume_refused
          ? { resumeRefused: payload.resume_refused }
          : {}),
      });
      return false;
    }

    case "peer_dispatch_failed": {
      const turnId = cardId ?? pickTurnId(payload);
      const peerId =
        (typeof payload.spawn_id === "string" && payload.spawn_id) ||
        (typeof payload.subagent_id === "string" && payload.subagent_id) ||
        "";
      if (turnId && peerId && !shouldSkipAppliedSeq(turnId, payload)) {
        const failureReason = normalizePeerFailureReasonCode(
          payload.reason_code,
        );
        applyPeerTerminal({
          turnId,
          peerId,
          status: "failed",
          evtSeq: readEvtSeq(payload),
          failureReason,
          failureDiagnostics: peerFailureDiagnostics(
            payload,
            failureReason,
            turnId,
          ),
        });
      }
      return false;
    }

    case "approval.required": {
      const turnId = cardId ?? pickTurnId(payload);
      const approvalId =
        typeof payload.approval_id === "string" ? payload.approval_id : "";
      if (turnId && approvalId && !shouldSkipAppliedSeq(turnId, payload)) {
        applyApprovalRequired({ turnId, approvalId });
      }
      return false;
    }

    case "approval.executed": {
      const turnId = cardId ?? pickTurnId(payload);
      const approvalId =
        typeof payload.approval_id === "string" ? payload.approval_id : "";
      if (turnId && approvalId && !shouldSkipAppliedSeq(turnId, payload)) {
        applyApprovalExecuted({
          turnId,
          approvalId,
          ok: payload.ok === true,
        });
      }
      return false;
    }

    case "approval.rejected": {
      // R-2: terminal fact for the reject path. Keeps approvalStatusById honest
      // (pending → rejected) so the disclosure gate reflects an abnormal
      // completion instead of a permanently "pending" action.
      const turnId = cardId ?? pickTurnId(payload);
      const approvalId =
        typeof payload.approval_id === "string" ? payload.approval_id : "";
      if (turnId && approvalId && !shouldSkipAppliedSeq(turnId, payload)) {
        applyApprovalRejected({ turnId, approvalId });
      }
      return false;
    }

    case "partial_answer": {
      // dogfood s_3e854 ④: PEER prose (parent_agent_id set) → accumulate into the
      // canonical turn-store keyed by the peer id, so the live + same-session-archived
      // + F5/replay render paths read one store (replayTurnEvents re-runs this to
      // rebuild prose on reload). LEAD-scope partial_answer (parent_agent_id null) →
      // return false so the legacy handle() reducer appends it to the answer.
      const parentAgentId = payload.parent_agent_id;
      if (typeof parentAgentId === "string" && parentAgentId && cardId) {
        // chat-solidify O3 (2026-07-03): append mutators must pass the seq guard;
        // partial_answer was the sole unguarded append path, so duplicate delivery (replay/reconnect) doubled
        // peer prose (caught by golden replay tests).
        if (shouldSkipAppliedSeq(cardId, payload)) return true;
        const delta = typeof payload.delta === "string" ? payload.delta : "";
        if (delta) applyPeerProse(cardId, parentAgentId, delta);
        return true;
      }
      return false;
    }

    case "emoji_ack": {
      const ev = payload as unknown as EmojiAckEvent & { text?: string; ts?: string };
      const turnId = pickTurnIdForEmoji(payload);
      if (!turnId) return false;
      if (shouldSkipAppliedSeq(turnId, payload)) return true;
      applyEmojiAck({
        turnId,
        ack: {
          msgId: ev.target_msg_id,
          emoji: ev.emoji,
          text: ev.text,
          ts: ev.ts,
        },
      });
      return true;
    }

    // chat-solidify 2026-07-03: removed case "emoji_remove"; the backend never emitted it (dead name removed).

    case "turn.follow_up": {
      // turn.follow_up is consumed at the gateway / runner layer to
      // chain SSE subscription to the follow-up stream URL. turn-store
      // doesn't track this directly; the new turn will get its own
      // step_list_card_created.
      const ev = payload as unknown as TurnFollowUpEvent;
      void ev;
      return true;
    }

    case "narrative.delta": {
      // Phase 1.6 G3 — Lead's voice prose chunk. Route by card_id (== turn_id).
      if (!cardId) return false;
      if (shouldSkipAppliedSeq(cardId, payload)) return true;
      const ev = payload as unknown as NarrativeDeltaEvent;
      if (!ev.delta) return true;  // empty deltas claimed but not appended
      // Phase 1.7-A — derive segId from the per-turn cursor. A contiguous
      // run of narrative.delta events shares one segId; step_started /
      // final_answer / clarification_required clear the cursor so the
      // next narrative.delta opens a fresh segment (tech-design §2.2).
      const segId = _currentNarrativeSeg(ev.card_id);
      applyNarrativeAppend({ turnId: ev.card_id, text: ev.delta, segId });
      return true;
    }

    case "final_answer": {
      // Phase 1.7-A — push `answer` beat (tech-design §2.2). Returns
      // false because the legacy reducer in use-turn-runner.ts still
      // owns `runner.answer` for the chat bubble; this dispatch path
      // is the new beats[] surface only.
      const fnTurnId = typeof payload.turn_id === "string" && payload.turn_id
        ? payload.turn_id
        : cardId;
      if (!fnTurnId) return false;
      if (shouldSkipAppliedSeq(fnTurnId, payload)) return false;
      _invalidateNarrativeSeg(fnTurnId);
      const text = typeof payload.text === "string" ? payload.text : "";
      // Phase 1.7-A — answer beat carries a minimal AnalysisPayloadRef.
      // exec_summary set only when payload.exec_summary explicitly present
      // (rare on final_answer; lead.final.payload event carries the rich
      // structure separately — out of 1.7-A scope).
      applyFinalAnswer({
        turnId: fnTurnId,
        text,
        payload: { turnId: fnTurnId },
        // Chat turn feedback D3: a legacy failure fallback is dropped at the failure terminal.
        legacyFailureFallback: isLegacyFailureAnswerReason(payload.reason_code),
      });
      return false;  // do NOT short-circuit legacy reducer
    }

    case "clarification_required":
    case "clarification.required": {
      // Phase 1.7-A — picker beat (turn-terminating clarification only,
      // identified by clarification_id field). V2.7 HITL path (no
      // clarification_id) keeps using V2.7 ClarificationStore via the
      // legacy reducer; no beat pushed for that flow.
      if (payload.clarification_id === undefined) return false;
      const incomingTurnId = typeof payload.turn_id === "string" && payload.turn_id
        ? payload.turn_id
        : cardId;
      if (!incomingTurnId) return false;
      if (shouldSkipAppliedSeq(incomingTurnId, payload)) return false;
      _invalidateNarrativeSeg(incomingTurnId);
      if (payload.narrative_superseded === true) {
        applyNarrativeSuperseded(incomingTurnId);
      }
      const candidates = Array.isArray(payload.candidates)
        ? (payload.candidates as unknown[]).map((v) => String(v))
        : [];
      const fallbackText = payload.fallback_text != null
        ? String(payload.fallback_text)
        : null;
      const themaState: ThetaClarificationState = {
        candidates,
        fallbackText,
        clarificationId: String(payload.clarification_id),
        questions: parseThetaQuestions(payload.questions),
      };
      pushPickerBeat(incomingTurnId, themaState);
      return false;  // legacy reducer still handles runner.clarificationPending
    }

    // prod-bug-bash-039 #13 Risk A fix: terminal turn events MUST invalidate
    // narrative segId so the final narrative beat stops "streaming" on archived
    // view. Without this, turns where last event is `narrative.delta` + no
    // `final_answer` (short Q&A) would render with blinking cursor forever in
    // archived view (because isNarrativeSegActive returns true permanently).
    case "done":
    case "turn_failed":
    case "turn_aborted":
    case "canvas_op_failed": {
      const turnId = cardId ?? (payload.turn_id != null ? String(payload.turn_id) : null);
      if (turnId && shouldSkipAppliedSeq(turnId, payload)) return false;
      if (turnId) _invalidateNarrativeSeg(turnId);
      // The backend can mark a narrative as superseded when its numbers disagree with authoritative report values,
      // and evidenced final_answer has replaced it) → store in turn-store, letting the renderer suppress the narrative beat.
      // This path, rather than runner state, keeps live / archived / F5 consistent: this file is the
      // shared route for live SSE and turn-store-replay, so replay executes this same branch again.
      if (turnId && eventName === "done" && payload.narrative_superseded === true) {
        applyNarrativeSuperseded(turnId);
      }
      // chat-ux-2026-08: collapsed-strip duration must be **wall-clock**. `done.duration_ms` is the backend's
      // sole reported wall-clock duration (`step_list_card_finalized` payload has no duration),
      // and this is the shared route for live SSE and `turn-store-replay`; once written to turn-store,
      // live / archived / F5 all read one field, satisfying live == persisted == replayed.
      if (turnId && eventName === "done" && typeof payload.duration_ms === "number") {
        applyTurnDuration({ turnId, totalDurationMs: payload.duration_ms });
      }
      // Chat turn feedback D6: `done.step_outcome` through this shared branch, like duration_ms.
      if (turnId && eventName === "done") {
        // Cancellation and recovery can emit done without a card-finalized
        // event. Use the same terminal writer for live delivery and replay;
        // clarification pauses stay open for their original continuation.
        if (!payload.parent_agent_id && (payload.status === "completed" || payload.status === "failed" || payload.status === "cancelled")) {
          applyCardFinalized({ turnId, finalState: payload.status });
        }
        const stepOutcome = parseStepOutcome(payload.step_outcome, payload.status);
        if (stepOutcome) applyStepOutcome({ turnId, stepOutcome });
      }
      // Chat turn feedback D3: the failure card is the only failure statement.
      if (
        turnId
        && (eventName === "turn_failed"
          || (eventName === "done" && (payload.status === "error" || payload.status === "failed")))
      ) {
        dropLegacyFailureAnswer(turnId);
      }
      return false;  // legacy reducer continues; we just hook the cursor cleanup
    }

    case "error": {
      // A root wire-level error is a failure terminal too (use-turn-runner "error" case).
      const turnId = cardId ?? (payload.turn_id != null ? String(payload.turn_id) : null);
      const parentAgentId = (payload as { parent_agent_id?: unknown }).parent_agent_id;
      if (turnId && !parentAgentId) dropLegacyFailureAnswer(turnId);
      return false;
    }

    default:
      return false;
  }
}

/**
 * Events that are not model output and must not clear a pending retry status:
 * content-free bookkeeping (the retried attempt's own physical-send marker
 * included) and user/collaboration decorations.
 */
const MODEL_RETRY_PRESERVING_EVENTS: ReadonlySet<string> = new Set([
  "turn.llm_invocation",
  "turn.effect_disclosure_settled",
  "agent.collaboration",
  "emoji_ack",
]);

export function parseModelRetryStatus(payload: Record<string, unknown>): ModelRetryStatus | null {
  const retry = payload.retry;
  const maxRetries = payload.max_retries;
  if (
    typeof retry !== "number"
    || typeof maxRetries !== "number"
    || !Number.isSafeInteger(retry)
    || !Number.isSafeInteger(maxRetries)
    || retry < 1
    || maxRetries < retry
  ) {
    return null;
  }
  const status: ModelRetryStatus = {
    retry,
    maxRetries,
    reason: typeof payload.reason === "string" && payload.reason ? payload.reason : "unknown",
  };
  if (typeof payload.profile === "string" && payload.profile) status.profile = payload.profile;
  if (typeof payload.subagent_id === "string" && payload.subagent_id) status.subagentId = payload.subagent_id;
  if (typeof payload.seq === "number" && Number.isSafeInteger(payload.seq)) status.seq = payload.seq;
  return status;
}

function pickCardId(payload: Record<string, unknown>): string | null {
  const v = payload.card_id;
  return typeof v === "string" && v.length > 0 ? v : null;
}

function pickTurnId(payload: Record<string, unknown>): string | null {
  const v = payload.turn_id;
  return typeof v === "string" && v.length > 0 ? v : null;
}

/**
 * chat-ux-2026-08 — event order within this turn (stored in `Step.evtSeq` / `PeerTerminal.evtSeq`,
 * letting the running band place steps and peer terminal states in one history).
 *
 * Priority **shares the source** of reconnect deduplication at `use-turn-runner.ts:1713`, without inventing another convention:
 *   · `event_seq`: protocol-v1 transport sequence.
 *   · `evt_seq`: monotonic bus sequence stamped on **every** event by `events.py`'s `TurnEventBus.emit`,
 *     also written into payload, hence present in persistence and replay.
 *   · `seq`: fallback. Same bus sequence, but callers of some canvas events prefill it with
 *     section-order seq (`events.py` uses `setdefault`), so it comes last.
 *
 * ⚠ **Correction** (independent review caught my original comment's mistake): the first version said `event_seq`
 * was added only to terminal frames by `_terminal_v1_wire_payload`, absent from step_* / peer_*.
 * This holds for **live** frames, not **replayed** frames: `turn_log_store.py:288-293` writes
 * `event_seq` / `seq` / `evt_seq` to every protocol-v1 row, **all with the same value**.
 * Thus omitting a key currently causes no error, but the shared-source claim was false then; now it is true.
 *
 * Bypass-bus events have no sequence (the `seq<1` branch in `use-turn-runner.ts` comments) → return
 * undefined, letting consumers fall back to array order.
 */
function readEvtSeq(payload: Record<string, unknown>): number | undefined {
  for (const key of ["event_seq", "evt_seq", "seq"] as const) {
    const v = payload[key];
    if (typeof v === "number" && Number.isFinite(v) && v >= 0) return v;
  }
  return undefined;
}

function shouldSkipAppliedSeq(
  turnId: string,
  payload: Record<string, unknown>,
): boolean {
  const seq = typeof payload.seq === "number" && Number.isFinite(payload.seq)
    ? payload.seq
    : -1;
  if (seq < 1) return false;
  let seen = _appliedSeqByTurn.get(turnId);
  if (seen === undefined) {
    // New turn — evict to bound memory. chat-solidify O6 (2026-07-03): prefer evicting
    // the oldest finalized turn; plain insertion-order eviction would kick out older still-live turns
    // in sessions exceeding 64 turns, causing reconnect replay to apply append mutators twice
    // (a known weak point in chat-pipeline.md). Fall back to insertion order only when all turns remain live,
    // keeping the map bounded.
    while (_appliedSeqByTurn.size >= _APPLIED_SEQ_MAX_TURNS) {
      let victim: string | undefined;
      for (const key of _appliedSeqByTurn.keys()) {
        if (getTurnSnapshot(key)?.cardFinalized) { victim = key; break; }
      }
      victim ??= _appliedSeqByTurn.keys().next().value;
      if (victim === undefined) break;
      _appliedSeqByTurn.delete(victim);
    }
    seen = new Set<number>();
    _appliedSeqByTurn.set(turnId, seen);
  }
  if (seen.has(seq)) return true;
  seen.add(seq);
  return false;
}

/**
 * emoji_ack/emoji_remove don't carry card_id — they carry target_msg_id.
 * Caller must pre-populate `__turnId` on the payload (use-turn-runner
 * does this from setState.s.turnId) or we can't route.
 */
function pickTurnIdForEmoji(payload: Record<string, unknown>): string | null {
  const v = payload.__turnId;
  return typeof v === "string" && v.length > 0 ? v : null;
}

function mapSubSteps(wire: WireSubStep[] | null | undefined): SubStepResult[] | null {
  if (!wire || wire.length === 0) return null;
  return wire.map((w, i) => ({
    id: `sub-${i}`,
    label: w.label,
    summary: w.output_preview ?? undefined,
    status: w.state === "success" ? "done" : "failed",
  }));
}

function mapCodeInput(value: unknown): StepCodeInput | null {
  if (!value || typeof value !== "object") return null;
  const input = value as Record<string, unknown>;
  if (
    input.language !== "python"
    || typeof input.code !== "string"
    || input.code.length === 0
    || typeof input.truncated !== "boolean"
  ) return null;
  return {
    language: "python",
    code: input.code,
    truncated: input.truncated,
  };
}

const OUTPUT_ARTIFACT_REF_CAP = 32;
const OUTPUT_ARTIFACT_TEXT_CAP = 200;

/**
 * Read the size-independent refs emitted in result_meta. The result_inline
 * fallback restores discoverability for already-persisted turns from before
 * the backend projection existed, provided their result fitted the old 4 KiB
 * envelope. Both live SSE and F5 replay call this exact reducer.
 */
function mapOutputArtifacts(
  payload: Record<string, unknown>,
): StepOutputArtifact[] | null {
  const meta = payload.result_meta;
  const inline = payload.result_inline;
  const metaRecord = meta && typeof meta === "object"
    ? meta as Record<string, unknown>
    : null;
  const inlineRecord = inline && typeof inline === "object"
    ? inline as Record<string, unknown>
    : null;
  const raw = Array.isArray(metaRecord?.output_artifacts)
    ? metaRecord.output_artifacts
    : Array.isArray(inlineRecord?.output_artifacts)
      ? inlineRecord.output_artifacts
      : null;
  if (!raw) return null;

  const refs: StepOutputArtifact[] = [];
  const seen = new Set<string>();
  for (const candidate of raw) {
    if (!candidate || typeof candidate !== "object") continue;
    const item = candidate as Record<string, unknown>;
    const artifactId = typeof item.artifact_id === "string"
      ? item.artifact_id
      : "";
    if (
      !artifactId.trim()
      || artifactId.length > OUTPUT_ARTIFACT_TEXT_CAP
      || seen.has(artifactId)
    ) continue;
    seen.add(artifactId);
    const filenameValue = item.filename ?? item.name;
    const dataRole = item.data_role === "stdout" || item.data_role === "structured"
      ? item.data_role
      : undefined;
    refs.push({
      artifactId,
      ...(typeof filenameValue === "string" && filenameValue
        ? { filename: filenameValue.slice(0, OUTPUT_ARTIFACT_TEXT_CAP) }
        : {}),
      ...(typeof item.mime === "string" && item.mime
        ? { mime: item.mime.slice(0, OUTPUT_ARTIFACT_TEXT_CAP) }
        : {}),
      ...(typeof item.size === "number" && Number.isFinite(item.size) && item.size >= 0
        ? { size: item.size }
        : {}),
      ...(dataRole ? { dataRole } : {}),
    });
    if (refs.length >= OUTPUT_ARTIFACT_REF_CAP) break;
  }
  return refs.length > 0 ? refs : null;
}

function mapError(wire: WireStepError | null | undefined): import("./turn-store").Step["error"] | null {
  const norm = normalizeStepError(wire);
  if (!norm) return null;
  return norm;
}

/**
 * 0.39.4 bug B/C — derive a typed StepDetail from the loose `result_inline`
 * blob that BE attaches to step_done payloads. Without this, the StepRow
 * `hasDetail` check stays false → row stops being expandable in either
 * live OR hydrate paths, even though the BE shipped rich content.
 *
 * Currently maps:
 *  · table shape `{columns: string[], rows: object[]}`  → `{kind: "table", ...}`
 *  · table shape `{cols: string[], rows: array[]}`      → `{kind: "table", ...}`
 *  · python sandbox `{code: string}` / SQL `{sql: string}` → corresponding code
 *
 * Returns `null` when no shape matches — caller falls through to a pass-through
 * `payload.detail` field for already-typed payloads.
 */
function _deriveDetailFromInline(
  payload: Record<string, unknown>,
): StepDetail | null {
  const inline = payload.result_inline as Record<string, unknown> | undefined;
  if (!inline || typeof inline !== "object") return null;

  // Table shape A: {columns: [...], rows: [{col:val}, ...]}
  if (Array.isArray(inline.columns) && Array.isArray(inline.rows)) {
    const cols = (inline.columns as unknown[]).map((c) => String(c));
    const rows = (inline.rows as unknown[]).map((row) => {
      if (Array.isArray(row)) {
        return row.map((cell) => (typeof cell === "number" ? cell : String(cell ?? "")));
      }
      if (row && typeof row === "object") {
        return cols.map((c) => {
          const v = (row as Record<string, unknown>)[c];
          return typeof v === "number" ? v : String(v ?? "");
        });
      }
      return cols.map(() => "");
    });
    return { kind: "table", cols, rows };
  }

  // Table shape B: {cols: [...], rows: [[...], ...]}
  if (Array.isArray(inline.cols) && Array.isArray(inline.rows)) {
    const cols = (inline.cols as unknown[]).map((c) => String(c));
    const rows = (inline.rows as unknown[]).map((row) => {
      if (Array.isArray(row)) {
        return row.map((cell) => (typeof cell === "number" ? cell : String(cell ?? "")));
      }
      return cols.map(() => "");
    });
    return { kind: "table", cols, rows };
  }

  // SQL or Python code block
  if (typeof inline.sql === "string" && inline.sql.length > 0) {
    return { kind: "sql", code: inline.sql };
  }
  if (typeof inline.code === "string" && inline.code.length > 0) {
    const isPy = (payload.tool_name === "python_sandbox") || (payload.step_type === "lead_thinking_writing");
    return isPy ? { kind: "python", code: inline.code } : { kind: "sql", code: inline.code };
  }

  // 0.39.7 #4 — python_sandbox result shape `{ok, exit_code, stdout, stderr,
  // outputs?, duration_ms?}` (sometimes wrapped as `{result_json: {...}}`).
  // StepDetail "stdout" union already wired in StepContentToolRun renderer;
  // we just need to surface the bytes so the expanded body isn't blank on
  // sandbox failures (the prod walkthrough pain — user clicks "运行代码 临时失败"
  // expects to see stderr, gets empty body).
  if (payload.tool_name === "python_sandbox") {
    const sbx =
      (inline.result_json && typeof inline.result_json === "object")
        ? (inline.result_json as Record<string, unknown>)
        : inline;
    const stdout = typeof sbx.stdout === "string" ? sbx.stdout.trimEnd() : "";
    const stderr = typeof sbx.stderr === "string" ? sbx.stderr.trimEnd() : "";
    const exitCode = typeof sbx.exit_code === "number" ? sbx.exit_code : null;
    if (stdout || stderr || exitCode !== null) {
      const segs: string[] = [];
      if (stdout) segs.push(stdout);
      if (stderr) segs.push(`# stderr\n${stderr}`);
      if (exitCode !== null && exitCode !== 0) {
        segs.push(`# exit_code=${exitCode}`);
      }
      const text = segs.join("\n\n") || "(no output)";
      return { kind: "stdout", text };
    }
  }

  return null;
}
