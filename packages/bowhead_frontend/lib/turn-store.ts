/**
 * turn-store — external store for chat-redesign Step List Card.
 *
 * Pattern mirrors lib/canvas-store.ts: a Map keyed by turnId holds the
 * full TurnState; subscribers register via subscribeTurn() and re-render
 * via useTurn() (React 18 useSyncExternalStore).
 *
 * Phase 1.2 only ships the store + mutators. Wiring from SSE events
 * (lib/use-turn-runner.ts) lands in a follow-up batch — the mutators
 * exported here are the API that lib/use-turn-runner.ts will call from
 * its step_* event handlers.
 *
 * Per tech-design §3.3.1 (chat-redesign-2026-05-21/tech-design.md). The
 * Step shape mirrors the backend's step_started / step_progress /
 * step_done payloads; the projection to the FE rendering type
 * (`components/chat/step-list/types.ts` StepView) lives in
 * `components/chat/step-list/step-view.ts` — one copy for every surface
 * (live trace, StepSummary, archived/F5).
 */

import type { CoachCanvasBinding } from "./coach-canvas-target";
import { useCallback, useMemo, useSyncExternalStore } from "react";

import type {
  FailureKind,
  StepDetail,
  StepError,
  StepKind,
  StepCodeInput,
  StepOutputArtifact,
  StepState,
  SubStepResult,
  ToolName,
} from "@/components/chat/step-list/types";
import type { ErrorDiagnostics } from "./error-presentation";
import type { ThetaClarificationState } from "./types";
import type { StepOutcome } from "./turn-feedback";

// ───────────────────────────────────────────────────────────────
// Types
// ───────────────────────────────────────────────────────────────

export interface Step {
  stepId: string;
  parentStepId: string | null;
  concurrencyGroupId: string | null;
  stepType: StepKind;
  toolName: ToolName | null;
  /** Backend-supplied Chinese headline ("解析文件" / "查询数据" / ...). */
  headerZh: string;
  /** Short label appended to the headline ("sales.xlsx" / SQL summary / ...). */
  label: string | null;
  state: StepState;
  /** Right-hand summary string ("234 行 × 12 列" / "5 / 5 成功" / ...). */
  summary: string | null;
  /** Backend ms duration; FE formats to "0.4s" at render. */
  durationMs: number | null;
  /** Append-only stream of code/sql tokens for lead_thinking_writing steps. */
  partialContent: string;
  /** Final body detail (table / hits / chunks / stdout / sql / python). */
  detail: StepDetail | null;
  /** Substeps — bi_query batch lanes or subagent peer trace rows. */
  subSteps: SubStepResult[] | null;
  /** Stable object refs produced by this step; content stays in Results. */
  outputArtifacts: StepOutputArtifact[] | null;
  /** Bounded executable source persisted on step_started for Results > Code. */
  codeInput: StepCodeInput | null;
  /** Failure envelope when state=failed. */
  error: StepError | null;
  /** Subagent peer display name (when stepType=subagent). */
  subagentName: string | null;
  /** Wall-clock when step_started landed (used for active-state timers). */
  startedAt: Date;
  // ── backward-compat / internal correlation ─────────────────
  /** Backend tool_call id — kept so legacy tool_call/tool_result dispatch can correlate. */
  callId: string | null;
  /** Subagent execution id (backend). */
  subagentId: string | null;
  /** dogfood s_3e854 ④: the PEER's id (from peer_dispatched.spawn_id), attached to
   *  the subagent_spawn step so live peer prose — keyed by partial_answer.parent_agent_id
   *  = the same peer id — renders ONCE on this spawn row. The spawn step's own
   *  `subagentId` is the LEAD's id, which is why keying prose on it never matched
   *  (the 3-strikes bug). Null on non-spawn steps → prose renders only on the spawn row. */
  peerProseKey: string | null;
  /**
   * chat-ux-2026-08: per-turn monotonic bus event order (`events.py`'s `TurnEventBus.emit`
   * stamps **every** event with `evt_seq` in payload, carried through persistence + replay).
   *
   * ★Sole purpose: the running band must place **steps** and **peer terminal states** in one history; previously
   * neither had a shared ordering basis (`Step` and `PeerTerminal` both lack timestamps).
   * ⚠ **Do not** reconstruct this client-side using steps.length at application time: during full server replay
   * on reconnect, seq deduplication skips previously applied steps, so steps.length stops growing, while
   * a missed peer terminal event only now applies → gets the current total, showing a peer that failed at step 3 as failing at step 12.
   * Order must reside on events, independent of when the client applies them.
   *
   * Bypass-bus events have no seq (see `use-turn-runner.ts` comments), so this can be null;
   * consumers fall back to array order.
   */
  evtSeq: number | null;
}

export interface ClarificationState {
  /** Discriminator from backend: token / options / task_plan. */
  kind: "token" | "options" | "task_plan";
  prompt: string;
  /** For kind=options / task_plan, the offered choices. */
  options?: Array<{ id: string; label: string; sub?: string }>;
}

export interface SteerEntry {
  text: string;
  /** ISO-8601 timestamp from the backend. */
  ts: string;
}

export interface EmojiAck {
  /** Message id the emoji decorates (e.g. user message that triggered steer). */
  msgId: string;
  emoji: string;
  /** Optional original text (e.g. user's steer message). Phase 1.4 — backend
   *  emit_emoji_ack threads this through so the FE can render the actual
   *  steer message as a SteerMessage bubble without re-fetching. */
  text?: string;
  /** ISO timestamp when the ack was emitted; used for display ordering + as
   *  a stable key when msgId is reused (rare). */
  ts?: string;
}

export type CardFinalState = "completed" | "failed" | "cancelled";

export type PeerTerminalStatus =
  | "succeeded"
  | "succeeded_with_warnings"
  | "failed"
  /** Stopped at an agent run limit and returned its finished work. */
  | "partial";

export interface PeerTerminal {
  status: PeerTerminalStatus;
  durationMs?: number;
  failureReason?: string;
  failureDiagnostics?: ErrorDiagnostics;
  /** Which agent run limit stopped a `partial` peer (step_limit, time_limit, …). */
  stopReason?: string;
  /** Saved run a later turn can continue in place (Continue button, `lead.subagent_summary`). */
  resumeCheckpointId?: string;
  /** Why this run could not continue its saved work and started fresh (expired, tools_changed, …). */
  resumeRefused?: string;
  /** Per-turn monotonic bus sequence; see `Step.evtSeq` (same ordering basis).
   *  ⚠ Preserve the **earliest** write: this terminal state is written twice (`agent_spawned` records state,
   *  `lead.subagent_summary` later adds reason/diagnostics). Using the later seq moves this
   *  terminal entry to the band's history tail. */
  evtSeq?: number;
  /** Latest terminal write's sequence. A runner start after it is a continuation. */
  settledSeq?: number;
  /** The same Agent was dispatched again in this turn (an @Agent continuation
   *  reuses its subagent id) and that run has not settled yet. */
  rerunning?: boolean;
  /** Sequence of the runner start that began the current continuation. */
  restartSeq?: number;
  /** An earlier run of this Agent in this turn succeeded. A later failed
   *  continuation does not turn the completed Agent into a failed one. */
  earlierSucceeded?: boolean;
  /** Reason of a failed continuation after an earlier success (secondary). */
  continuationFailureReason?: string;
}

/**
 * A model call failed before replying and the Router scheduled a retry
 * (`llm.retry_scheduled`). Transient running status only: cleared by the next
 * model output or terminal event, so a finished turn never carries it.
 */
export interface ModelRetryStatus {
  /** 1-based retry number. */
  retry: number;
  maxRetries: number;
  /** ProviderFailureKind value, e.g. "timeout", "network", "rate_limited", "upstream". */
  reason: string;
  /** Agent profile whose call is retrying ("lead" or a peer profile). */
  profile?: string;
  subagentId?: string;
  /** Bus seq of the retry event; only later events clear it (a reconnect replays earlier ones). */
  seq?: number;
}

export type ApprovalExecutionStatus =
  | "pending"
  | "executed"
  | "execution_failed"
  // R-2: the reject path is a terminal decision — the action never ran.
  // Semantically an abnormal completion (like execution_failed), so it keeps
  // the turn out of the normal-completion fold (see hasBlockingApprovalState).
  | "rejected";

/**
 * Minimal AnalysisPayload reference — backend emits `exec_summary` + a
 * loose grab-bag of kpis / insights / sources / etc. (the legacy
 * AnalysisPayload shape that used to live in v3-reducer). turn-store
 * holds it as an opaque blob and ChatBubbleNew projects it to inline
 * artifacts (chart / kpi / table) via ChatInline* wrappers.
 */
export interface AnalysisPayloadRef {
  turnId: string;
  exec_summary?: string;
  // Remaining fields stay loose — concrete shape lives at the consumer.
  [key: string]: unknown;
}

/**
 * Phase 1.7-A — `beats[]` discriminated union (chat-redesign-2026-05-21
 * tech-design §2.1). Single ordered emit log replacing the legacy disjoint
 * `narrative` / `answerStreaming` / `answerPayload` string slots.
 *
 * Picker beat carries `ThetaClarificationState` (turn-terminating
 * clarification from `lib/types.ts`), NOT the V2.7 `ClarificationState`
 * above — the two shapes are intentionally different. Picker UI in
 * `TurnContainer.tsx` reads from `runner.clarificationPending` today;
 * the picker beat is order-only for 1.7-A and feeds composition rebuild
 * in 1.7-B.
 *
 * `narrative` beats use FE-derived `segId` (per-turn cursor advanced by
 * any beat-emitting event); same-segId successor appends, else new beat.
 * `step` beats are pointers into `steps[]` keyed by `stepId`.
 * `answer` beats carry the latest `AnalysisPayloadRef` from final_answer.
 */
export type TurnBeat =
  | { kind: "narrative"; segId: string; text: string }
  | { kind: "step"; stepId: string }
  | { kind: "picker"; clarification: ThetaClarificationState }
  // chat-solidify O2 (2026-07-03): answer beat now carries text; previously only
  // payloadRef existed, live bubbles read legacy runner.answer, archived bubbles read snapshot answer,
  // and three sources could diverge anytime (W2). beat.text becomes one source for live/archived/F5;
  // old fields remain fallbacks (pre-O2 archived snapshots lack text).
  | {
      kind: "answer";
      payloadRef: AnalysisPayloadRef;
      text?: string;
      /** Chat turn feedback D3: a legacy system failure fallback (removed at the failure terminal). */
      legacyFailureFallback?: boolean;
    }
  // Planning Mode — pointer into lib/plan-store keyed by planId. The plan
  // card renders inline at this beat's position; its data + per-step status
  // live in plan-store (updated by plan_* / step_* SSE), so the card keeps
  // updating from the execution turn even though the beat lives on the
  // proposal turn.
  | { kind: "plan"; planId: string }
  // Config copilot batch 0.5: pointer into lib/config-copilot-lite keyed by proposalId.
  // Lightweight confirmation renders inline at this beat; payload lives in proposal store (dispatcher writes,
  // replay-safe). applied/reverted/ignored terminal states are render-layer overlays (localStorage session state).
  | { kind: "lite"; proposalId: string }
  // Config copilot batch 1-b: config-draft chip (open drawer/save receipt/discard/expired); draftId points to server truth.
  | { kind: "draft"; draftId: string };

export interface TurnState {
  turnId: string;
  /** Committed collaboration revision. Private message bodies never enter replay state. */
  collaborationRevision?: number;
  coachCanvasTarget?: CoachCanvasBinding | null;
  /** Card id == turnId (single source of truth). */
  cardId: string;
  /** `step_list_card_created` landed. */
  cardCreated: boolean;
  /** `step_list_card_finalized` landed. */
  cardFinalized: boolean;
  /** Terminal state of the card; null while running. */
  cardFinalState: CardFinalState | null;
  /** The backend marks narratives superseded when their numbers disagree with authoritative report values,
   *  replaced by evidenced final_answer → suppress all narrative beats for that turn during rendering.
   *
   *  **Lives in turn-store, not runner state**: turn-store has a canonical replay path
   *  (turn-store-replay → dispatchToTurnStore, same route as live SSE), naturally keeping live / archived /
   *  F5 consistent. The first version used runner + archived snapshots; on 116, "出结果时压住了、
   *  一刷新 92.37 又冒出来": F5 fills turn-store by event replay, never through the runner path. */
  narrativeSuperseded: boolean;
  steps: Step[];
  /**
   * Phase 1.7-A — ordered emit log (narrative / step / picker / answer).
   * Single source of truth for chat-surface composition. Replaces the
   * legacy disjoint `narrative` / `answerStreaming` / `answerPayload`
   * string slots. Derived accessors:
   *   - `selectNarrativeBuffer(state)` → concatenated narrative text
   *   - `selectAnswerPayload(state)`   → latest answer payload (or null)
   */
  beats: TurnBeat[];
  /** Active HITL clarification (V2.7 ClarificationStore slot — separate from picker beat). */
  clarification: ClarificationState | null;
  /** Display-only mirror of the backend session_store steer buffer. */
  steerBuffer: SteerEntry[];
  /** Emoji reactions decorating user messages (🤝 ack for steer, etc.). */
  emojiAcks: EmojiAck[];
  /** Total **wall-clock** duration (ms) for this turn; backend `done.duration_ms` is truth, written by `applyTurnDuration`.
   *
   *  **Not the full turn length; the difference was measured**: compare 8 actual turns on 116 individually against DB
   *  `ended_at - started_at`; `done.duration_ms` is consistently **1.5–2.2 seconds** lower
   *  (turn setup/cleanup lie outside Lead timing; the two canvas report-writing turns also differ only 1.6–1.7s,
   *  with no omission on the scale of the compose phase). Relative error ≤3%, versus the replaced
   *  sum-of-step-durations estimate being wrong by 5×–147× on those same turns.
   *
   *  ⚠ Must live in turn-store, not runner state: collapsed strips (`StepSummary`) in both live
   *  and archive/F5 read this one field, sharing the source. `runner.summary.total` has no corresponding
   *  field on `ArchivedTurn`, so it would show different numbers before/after F5, violating CLAUDE.md's
   *  live / persisted / replayed consistency requirement.
   *  HITL resume clears it to null via `applyCardResumed`; the new generation's `done` writes again. */
  totalDurationMs: number | null;
  /** Chat turn feedback D6: backend `done.step_outcome`, written by `applyStepOutcome` from the
   *  shared `done` branch (live and replay alike). Null = unknown (old turns): the step summary
   *  then keeps its neutral local count. */
  stepOutcome: StepOutcome | null;
  /** report-streaming D2: this turn's start (backend `turn_started.started_at`).
   *
   *  ⚠ **Must live in the store, not component-local state**: the 0.7.10 top status bar's
   *  seconds counter stayed at 0s because frequent component unmount/remount continually reset
   *  its interval. Store ownership preserves the baseline across remounts.
   *  The value comes from persisted event payload, so F5 replay preserves the original start
   *  (waiting p90 is 173s, exactly when users are most likely to refresh). */
  startedAt: Date | null;
  /** dogfood s_3e854 ④: live peer-LLM prose accumulated per peer id
   *  (partial_answer.parent_agent_id). Lives HERE in the canonical turn-store —
   *  NOT in the SSE runner — so the live, same-session-archived, and F5/replayed
   *  render paths all read one store (replayTurnEvents re-dispatches partial_answer
   *  to rebuild it). The spawn step's `peerProseKey` selects the entry. */
  peerProse: Record<string, string>;
  /** P4a (chat-redesign): peer agent identity (`peer_dispatched.peer_name`,
   *  e.g. "compute" / "financial-analyst") keyed by the peer's subagent id —
   *  the SAME id its inner steps carry as `subagentId` and that `peerProseKey`
   *  / `partial_answer.parent_agent_id` use. Lets the agent-block grouping
   *  resolve `peerNames[step.subagentId]` → an agent identity. Replay-rebuilt
   *  (dispatchToTurnStore re-runs peer_dispatched). */
  peerNames: Record<string, string>;
  /** User-facing configured Agent name captured by `agent_spawned.display_name`.
   *  Keyed by peer id and rebuilt from the event log like peerNames. */
  peerDisplayNames: Record<string, string>;
  /** Immutable Marketplace identity projected from the exact execution
   * binding. Each field is first-nonempty so later terminal event shapes can
   * enrich, but never rewrite, the start snapshot. */
  peerAgentAttribution: Record<string, PeerAgentAttribution>;
  /** P4a: peer task/objective keyed by subagent id (`agent_spawned.task`). In the
   *  plan-execution path there is NO subagent_spawn step, so the peer block header's
   *  task comes from here (distinguishes e.g. two `compute` peers). */
  peerTasks: Record<string, string>;
  /** Replayable peer terminal facts from `lead.subagent_summary`, terminal-shaped
   *  `agent_spawned`, or the earlier `peer_dispatch_failed` fallback. Terminal
   *  state wins over any stale active child step in the execution ledger. */
  peerTerminalById: Record<string, PeerTerminal>;
  /** Approval lifecycle facts used only to keep abnormal/pending action turns
   *  out of the normal-completion fold. The result/receipt UI remains a
   *  separate delivery surface. */
  approvalStatusById: Record<string, ApprovalExecutionStatus>;
  /** Revision Turn: the draft it revises (`turn_started.document_revision`, persisted with the event,
   *  so the live receipt and its replay read the same title and draft number). */
  documentRevision?: DocumentRevisionReceipt;
  /** Plan this Turn left blocked (`plan_blocked.plan_id`). The failure card uses it to offer
   *  resuming that plan; live and replayed Turns read the same persisted event. */
  blockedPlanId?: string;
  /** Pending model retry (`llm.retry_scheduled`) until the next model output or terminal event.
   *  Lives in turn-store so live SSE and replay run the same set/clear rules: replaying a
   *  finished turn always ends cleared because its later events clear it. */
  modelRetry?: ModelRetryStatus;
}

// ───────────────────────────────────────────────────────────────
// Phase 1.7-A — derived selectors (legacy slot replacements)
// ───────────────────────────────────────────────────────────────

/**
 * Concatenate all `narrative` beats in order. Same-segId append already
 * happened at dispatch time (see lib/turn-store-dispatch.ts narrative.delta
 * append rule). Output is the visible running buffer for LeadNarrative.
 */
export function selectNarrativeBuffer(state: TurnState | undefined): string {
  if (!state) return "";
  let out = "";
  for (const b of state.beats) if (b.kind === "narrative") out += b.text;
  return out;
}

/** Return the latest `answer` beat payload, or null if none recorded. */
export function selectAnswerPayload(
  state: TurnState | undefined,
): AnalysisPayloadRef | null {
  if (!state) return null;
  for (let i = state.beats.length - 1; i >= 0; i--) {
    const b = state.beats[i];
    if (b.kind === "answer") return b.payloadRef;
  }
  return null;
}

/**
 * report-streaming D3 — the phase label for the canvas streaming banner.
 *
 * Returns the currently-running step's backend-supplied Chinese headline
 * (+ its short label), or `""` when nothing is running. The banner used to
 * claim "正在更新报告 · 已生成 N 个部分" for the WHOLE turn, including the
 * ~62s (prod p50) while peers are still querying and no report exists at all
 * — see docs/sprints/report-streaming/discovery.md §2.6.
 *
 * Returns a **plain string**, not an object: the banner subscribes via
 * `useSyncExternalStore`, which compares with Object.is — a fresh object per
 * read would re-render forever (same reason `selectStreamProgress` returns a
 * primitive key, canvas-store.ts).
 *
 * `headerZh` is already localized by the backend ("查询数据" / "解析文件"), so
 * there is no per-tool i18n key here; `label` is the free-text detail
 * ("sales.xlsx" / a SQL summary).
 */
export function selectRunningStepLabel(state: TurnState | undefined): string {
  if (!state) return "";
  // Last active step wins: nested peer/tool steps land after their parent, so
  // the deepest in-flight one is the most specific thing to show.
  for (let i = state.steps.length - 1; i >= 0; i--) {
    const s = state.steps[i];
    if (s.state !== "active") continue;
    const head = (s.headerZh || "").trim();
    if (!head || !_isLocalizedHeadline(head)) continue;
    const label = _bannerLabel(s.label || "");
    return label ? `${head} · ${label}` : head;
  }
  return "";
}

/** Banner/waiting band is a **single-line status bar**, not a step card.
 *
 * Backend `_prettify_input_preview` produces `label`, usually a sentence (`sales.xlsx`,
 * `2025年 营业额 每月 +1 more`), but for `subagent_spawn` it is **the model's entire newly written task instruction**.
 * Observed verbatim on dev 1.0.77:
 *    `派子 agent · [compute] 用当前工作区（商超零售）的业务数据做一份销售表现分析报告，要求如下： 1. **至少两张图**：`
 * Internal slug, raw markdown, and a hard 120-character cut mid-sentence.
 *
 * ★This is **general consolidation**, not a subagent_spawn patch: any tool (including future ones)
 * must have a clean short single-line label on the status bar, or the next verbose tool repeats the problem
 * (same reason `_isLocalizedHeadline` checks for Chinese rather than maintaining a tool-name blacklist).
 * Full task text remains in chat's Agent card and "当前任务" block, so no information is lost. */
const _BANNER_LABEL_MAX = 36;
/** A clause terminator counts only if not too early: the colon in `运行: python_sandbox` is a **prefix separator**;
 *  cutting there would leave only "运行". */
const _BANNER_LABEL_MIN_CLAUSE = 8;

function _bannerLabel(raw: string): string {
  // Take only the first line: an instruction's gist is there, followed by items such as "1. …".
  const first = (raw.split("\n")[0] || "").trim();
  // Pass through unchanged if it fits; most tool labels are already sentences and should not be altered.
  if (first.length <= _BANNER_LABEL_MAX) return first;
  // Shorten only overlong labels, preferring semantic boundaries: the last clause terminator within the window.
  const window = first.slice(0, _BANNER_LABEL_MAX);
  let cut = -1;
  for (let i = _BANNER_LABEL_MIN_CLAUSE; i < window.length; i++) {
    if ("。；;！!？?：:".includes(window[i])) cut = i;
  }
  if (cut > 0) return first.slice(0, cut).trim();
  return first.slice(0, _BANNER_LABEL_MAX - 1).trim() + "…";
}

/** `headerZh` is misleading: **about 8% of steps contain raw tool names** (116 production measurements:
 *  `add_image` 146 times, `web_search` 79, `skill_list` 55, `bi_concept_resolve` 38,
 *  `tool_creator` 32, `canvas_patch_section` 26 …). Already unattractive on chat step cards,
 *  D3 moved them onto the **report panel's banner**, where business users read reports; displaying
 *  `add_image` is worse than showing nothing.
 *
 *  Deliberately check for Chinese rather than maintain a tool blacklist, which misses the next new tool
 *  (same as [[feedback_anthropic_pattern_unify_not_patch]]; do not add a fifth wire implementation).
 *  `查询数据 (bi_query)` **qualifies** despite parenthesized English: its main text is Chinese.
 *
 *  The root fix is missing Chinese names in backend step definitions; until then, prefer generic banner copy. */
function _isLocalizedHeadline(head: string): boolean {
  return /[一-鿿]/.test(head);
}

/** report-streaming D2: seconds elapsed for this turn; unknown start returns null (hide,
 *  rather than show an incorrect 0). Caller supplies `now` for testing and a shared component tick. */
export function selectElapsedSeconds(
  state: TurnState | undefined,
  now: number,
): number | null {
  if (!state?.startedAt) return null;
  const s = Math.floor((now - state.startedAt.getTime()) / 1000);
  return s >= 0 ? s : null;
}

/** A peer can do useful work without emitting a child tool step. Keep those
 * turns distinct from pure-prose fast paths so their agent ledger is visible. */
export function hasPeerDisclosureFacts(
  state: TurnState | undefined,
): boolean {
  if (!state) return false;
  return (
    (state.collaborationRevision ?? 0) > 0 ||
    Object.keys(state.peerNames).length > 0 ||
    Object.keys(state.peerDisplayNames).length > 0 ||
    Object.keys(state.peerAgentAttribution).length > 0 ||
    Object.keys(state.peerTasks).length > 0 ||
    Object.keys(state.peerProse).length > 0 ||
    Object.keys(state.peerTerminalById).length > 0
  );
}

/** Pending, failed, or rejected approval execution is not a normal completed
 *  action. Rejected (R-2) is treated like execution_failed: the disclosure
 *  stays expanded and does not auto-collapse. */
export function hasBlockingApprovalState(
  state: TurnState | undefined,
): boolean {
  if (!state) return false;
  return Object.values(state.approvalStatusById).some(
    (status) =>
      status === "pending" ||
      status === "execution_failed" ||
      status === "rejected",
  );
}

// ───────────────────────────────────────────────────────────────
// Internal state
// ───────────────────────────────────────────────────────────────

const _states = new Map<string, TurnState>();
// Listeners are keyed by turnId so a commit on turn A doesn't re-render
// every subscriber for turn B (CTO review P1 #5 — N×M render storm).
const _listeners = new Map<string, Set<() => void>>();
const _revisions = new Map<string, number>();
// Whole-store watchers (few, cheap boolean selectors only — e.g. "is any send_email step running").
const _anyTurnListeners = new Set<() => void>();

function _emptyTurnState(turnId: string): TurnState {
  return {
    turnId,
    cardId: turnId,
    cardCreated: false,
    cardFinalized: false,
    cardFinalState: null,
    narrativeSuperseded: false,
    steps: [],
    beats: [],
    clarification: null,
    steerBuffer: [],
    emojiAcks: [],
    totalDurationMs: null,
    stepOutcome: null,
    startedAt: null,
    peerProse: {},
    peerNames: {},
    peerDisplayNames: {},
    peerAgentAttribution: {},
    peerTasks: {},
    peerTerminalById: {},
    approvalStatusById: {},
  };
}

function _notify(turnId: string): void {
  const bucket = _listeners.get(turnId);
  if (bucket) {
    for (const cb of bucket) cb();
  }
  for (const cb of _anyTurnListeners) cb();
}

/**
 * Replace the state object reference for `turnId`, then notify only the
 * subscribers that bound to this turnId. We always create a new top-level
 * object so `useSyncExternalStore` sees a !== snapshot and re-renders.
 */
function _commit(turnId: string, next: TurnState): void {
  _states.set(turnId, next);
  _revisions.set(turnId, (_revisions.get(turnId) ?? 0) + 1);
  _notify(turnId);
}

// ───────────────────────────────────────────────────────────────
// Public read API
// ───────────────────────────────────────────────────────────────

export function getTurnSnapshot(turnId: string): TurnState | undefined {
  return _states.get(turnId);
}

export function applyCollaborationRevision(turnId: string, revision: number): void {
  if (!Number.isSafeInteger(revision) || revision <= 0) return;
  const current = _states.get(turnId) ?? _emptyTurnState(turnId);
  if (revision <= (current.collaborationRevision ?? 0)) return;
  _commit(turnId, { ...current, collaborationRevision: revision });
}

export function subscribeTurn(turnId: string, listener: () => void): () => void {
  let bucket = _listeners.get(turnId);
  if (!bucket) {
    bucket = new Set();
    _listeners.set(turnId, bucket);
  }
  bucket.add(listener);
  return () => {
    const b = _listeners.get(turnId);
    if (!b) return;
    b.delete(listener);
    if (b.size === 0) _listeners.delete(turnId);
  };
}

/**
 * React hook — subscribes to a single turn's state. Returns undefined
 * until the first event lands.
 *
 * Caller is responsible for stable turnId — re-renders on every turnId
 * change will rebind the subscriber.
 */
export function useTurn(turnId: string): TurnState | undefined {
  return useSyncExternalStore(
    (cb) => subscribeTurn(turnId, cb),
    () => getTurnSnapshot(turnId),
    () => undefined,
  );
}

/** Subscribe to a stable set of turns without calling a hook in a dynamic loop.
 * The primitive revision snapshot keeps useSyncExternalStore referentially stable. */
export function useTurns(turnIds: readonly string[]): readonly TurnState[] {
  const key = turnIds.join("\u001f");
  const stableIds = useMemo(
    () => Array.from(new Set(key ? key.split("\u001f") : [])).filter(Boolean),
    [key],
  );
  const subscribe = useCallback(
    (listener: () => void) => {
      const unsubscribe = stableIds.map((turnId) => subscribeTurn(turnId, listener));
      return () => unsubscribe.forEach((stop) => stop());
    },
    [stableIds],
  );
  const getRevision = useCallback(
    () => stableIds.map((turnId) => _revisions.get(turnId) ?? 0).join(","),
    [stableIds],
  );
  const revision = useSyncExternalStore(subscribe, getRevision, getRevision);
  return useMemo(
    () => stableIds.map((turnId) => _states.get(turnId)).filter((turn): turn is TurnState => Boolean(turn)),
    [revision, stableIds],
  );
}

/** Subscribe to every turn's commits. Keep the selector a primitive so React bails out cheaply. */
export function subscribeAllTurns(listener: () => void): () => void {
  _anyTurnListeners.add(listener);
  return () => {
    _anyTurnListeners.delete(listener);
  };
}

/** True while any known turn has a not-yet-finished step for `toolName`. */
export function hasActiveToolStep(toolName: string, match: "exact" | "suffix" = "exact"): boolean {
  for (const turn of _states.values()) {
    for (const step of turn.steps) {
      if ((match === "suffix" ? step.toolName?.endsWith(toolName) : step.toolName === toolName) && (step.state === "active" || step.state === "pending")) {
        return true;
      }
    }
  }
  return false;
}

/** React hook form of `hasActiveToolStep`, re-evaluated on every turn commit. */
export function useHasActiveToolStep(toolName: string, match: "exact" | "suffix" = "exact"): boolean {
  return useSyncExternalStore(
    subscribeAllTurns,
    () => hasActiveToolStep(toolName, match),
    () => false,
  );
}

/**
 * Test / dev helper — drops a turn's state. Use sparingly; production
 * normally lets state stick for the session lifetime.
 */
export function resetTurn(turnId: string): void {
  _states.delete(turnId);
  _revisions.delete(turnId);
  // ⚠ Clear pending claims too (caught by independent review): `resetTurn` runs in mock-runner replay and multiple
  //   component-test cleanups. Leftover entries would attach the **previous turn's** peer id to the next arriving same-named stepId,
  //   worse than losing a key. Both reset paths must match.
  _pendingProseKeys.delete(turnId);
  _notify(turnId);
}

/**
 * Test helper: number of remaining pending claims.
 *
 * ★Without it, do not enqueue without `parent_step_id` and remove entries after claiming
 * are **unobservable** through the public API (strict draining makes queued/unqueued behavior equivalent).
 * My first version had such a positive control; none of three mutations could fail it.
 * Rather than keep an ineffective self-proclaimed guard, expose internal state read-only to tests.
 */
export function _pendingProseKeyCountForTesting(turnId: string): number {
  return _pendingProseKeys.get(turnId)?.length ?? 0;
}

/** Test helper — clears all turns (used by unit tests' beforeEach). */
export function _resetAllTurnsForTesting(): void {
  _pendingProseKeys.clear();
  // Fire all known listener buckets so React subscribers re-read.
  const ids = Array.from(_states.keys());
  _states.clear();
  _revisions.clear();
  for (const id of ids) {
    _notify(id);
  }
}

// ───────────────────────────────────────────────────────────────
// Mutators — called from lib/use-turn-runner.ts step_* dispatch
// ───────────────────────────────────────────────────────────────

export interface ApplyCardCreatedInput {
  turnId: string;
}

export function applyCardCreated(input: ApplyCardCreatedInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  if (prev.cardCreated) return;
  _commit(input.turnId, { ...prev, cardCreated: true });
}

export function applyCoachCanvasTarget(turnId: string, target: CoachCanvasBinding): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  if (prev.coachCanvasTarget) return;
  _commit(turnId, { ...prev, coachCanvasTarget: structuredClone(target) });
}

export interface ApplyTurnStartedInput {
  turnId: string;
  /** Backend `turn_started.started_at` (ISO8601). Do not write when absent: hide the timer
   *  rather than use a wrong baseline (old backends omit this field). */
  startedAt?: string | null;
}

/** report-streaming D2: record the turn start for the waiting timer.
 *
 *  **Accept only the first write**: full replay on reconnect resends `turn_started`; overwriting would reset the baseline
 *  to the reconnect time, showing 0 after the user already waited two minutes. */
export function applyTurnStarted(input: ApplyTurnStartedInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  if (prev.startedAt) return;
  if (!input.startedAt) return;
  const d = new Date(input.startedAt);
  if (Number.isNaN(d.getTime())) return;
  _commit(input.turnId, { ...prev, startedAt: d });
}

export interface ApplyCardFinalizedInput {
  turnId: string;
  finalState: CardFinalState;
}

/** ⚠ Do **not** write `totalDurationMs` here: `step_list_card_finalized` wire payload
 *  has only `card_id` / `state` / `step_count` (`events.py`'s
 *  `StepListCardFinalizedPayload`), never duration. The previous
 *  `totalDurationMs?` argument looked connected but no production caller supplied it,
 *  leaving the collapsed strip always using approximate step-duration sums (on 116 a two-minute plan turn
 *  showed `· 15s`). `applyTurnDuration` separately writes wall-clock duration from `done.duration_ms`;
 *  retain one writer for one field. */
export function applyCardFinalized(input: ApplyCardFinalizedInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  // Terminal turn ⇒ steps still pending/active can no longer receive `step_done`;
  // finish them here, or the card spins forever.
  //
  // Why frontend rather than extra backend frames: `_execute_tool` (subagent.py:4089)
  // **deliberately** reraises `CancelledError` directly (comment says "caller aborts the turn"),
  // and `bus.emit` is async: adding await in an already-cancelled task has asyncio pitfalls.
  // More importantly, extra backend frames cannot save the other two states: disconnected (frame sent but missed) and **replay**
  // (that frame never existed in historical persisted events). This is the sole point covering all three states.
  const needsSweep = prev.steps.some((s) => s.state === "pending" || s.state === "active");
  const steps = needsSweep
    ? prev.steps.map((s) =>
        s.state === "pending" || s.state === "active"
          ? {
              ...s,
              // User-initiated Stop → cancelled (⚠ 已停止); turns failing/completing on their own with unfinished
              // steps → failed. Do not blindly mark cancelled, falsely attributing real failures to user Stop.
              state: input.finalState === "cancelled" ? ("cancelled" as const) : ("failed" as const),
            }
          : s,
      )
    : prev.steps;
  _commit(input.turnId, {
    ...prev,
    cardFinalized: true,
    cardFinalState: input.finalState,
    steps,
  });
}

export interface ApplyTurnDurationInput {
  turnId: string;
  /** Backend `done.duration_ms`. */
  totalDurationMs: number;
}

/**
 * Record total wall-clock duration for this turn (X in collapsed strip `✓ N 步完成 · M 次重试 · Xs`).
 *
 * Read from `done`, not `step_list_card_finalized`, for two reasons:
 *   ① `done.duration_ms` is the backend's sole wall-clock duration (all terminal branches of
 *      `turn_runner` / Lead carry it), whereas `step_list_card_finalized` payload lacks duration;
 *   ② `done` is already registered in the EventSource whitelist and persisted alongside other events;
 *      replay executes the same branch, naturally sharing the source across live / archived / F5,
 *      even obtaining correct values after refresh for **historical turns** completed before this change.
 *
 * **Later writes overwrite earlier ones**: HITL clarification runs a second generation on the same `turn_id`;
 * `applyCardResumed` first clears this to null, then generation two's `done` writes again. Replay order and outcome match.
 *
 * ⚠ **Known boundary**: `steps` accumulates across generations, while this keeps only the last duration, theoretically
 * mixing N steps from two generations with duration from only the last. The observed HITL shape on 116 **cannot hit this**:
 * clarification-generation `steps` and `done.duration_ms` are both 0 (the sentinel guard below ignores duration),
 * so both numbers actually belong to generation two. If a turn ever does work before clarifying midway,
 * change this to accumulate generations then, not speculatively now: accumulation doubles any undeduplicated duplicate dispatch.
 */
export function applyTurnDuration(input: ApplyTurnDurationInput): void {
  const ms = input.totalDurationMs;
  // 0 is the backend-did-not-measure sentinel (e.g. config-failure branch hardcodes duration_ms: 0),
  // not a zero-duration turn; rendering `· 0ms` is more misleading than hiding it.
  if (!Number.isFinite(ms) || ms <= 0) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  if (prev.totalDurationMs === ms) return;
  _commit(input.turnId, { ...prev, totalDurationMs: ms });
}

export interface ApplyStepOutcomeInput {
  turnId: string;
  stepOutcome: StepOutcome;
}

/** Record `done.step_outcome` (D6). Later generations overwrite, like `applyTurnDuration`. */
export function applyStepOutcome(input: ApplyStepOutcomeInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const next = input.stepOutcome;
  const cur = prev.stepOutcome;
  if (
    cur
    && cur.issueSteps === next.issueSteps
    && cur.recoveredSteps === next.recoveredSteps
    && cur.unresolvedSteps === next.unresolvedSteps
    && cur.answerAffected === next.answerAffected
    && cur.turnCompleted === next.turnCompleted
  ) {
    return;
  }
  _commit(input.turnId, { ...prev, stepOutcome: next });
}

export interface ApplyCardResumedInput {
  turnId: string;
}

/** Reopen the same card when HITL resumes the existing turn_id. */
export function applyCardResumed(input: ApplyCardResumedInput): void {
  const prev = _states.get(input.turnId);
  if (!prev) return;
  if (
    !prev.cardFinalized &&
    prev.cardFinalState === null &&
    prev.totalDurationMs === null
  ) {
    return;
  }
  _commit(input.turnId, {
    ...prev,
    cardFinalized: false,
    cardFinalState: null,
    totalDurationMs: null,
    stepOutcome: null,
  });
}

export interface ApplyStepStartedInput {
  turnId: string;
  stepId: string;
  parentStepId?: string | null;
  concurrencyGroupId?: string | null;
  stepType: StepKind;
  toolName?: ToolName | null;
  headerZh: string;
  label?: string | null;
  inputPreview?: string | null;
  startedAt?: string | Date;
  callId?: string | null;
  subagentId?: string | null;
  subagentName?: string | null;
  codeInput?: StepCodeInput | null;
  /** Per-turn monotonic bus sequence (`payload.evt_seq`); see `Step.evtSeq`. */
  evtSeq?: number;
}

export function applyStepStarted(input: ApplyStepStartedInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);

  // Replace by stepId if already present (defensive against duplicate
  // step_started, e.g. resume after disconnect).
  const idx = prev.steps.findIndex((s) => s.stepId === input.stepId);

  const step: Step = {
    stepId: input.stepId,
    parentStepId: input.parentStepId ?? null,
    concurrencyGroupId: input.concurrencyGroupId ?? null,
    stepType: input.stepType,
    toolName: input.toolName ?? null,
    headerZh: input.headerZh,
    label: input.label ?? input.inputPreview ?? null,
    state: "active",
    summary: null,
    durationMs: null,
    partialContent: "",
    detail: null,
    subSteps: null,
    outputArtifacts: null,
    codeInput: input.codeInput ?? null,
    error: null,
    subagentName: input.subagentName ?? null,
    startedAt: input.startedAt
      ? input.startedAt instanceof Date
        ? input.startedAt
        : new Date(input.startedAt)
      : new Date(),
    callId: input.callId ?? null,
    subagentId: input.subagentId ?? null,
    // ⚠ **Duplicate step_started must preserve the claimed peer key** (caught by independent review):
    //   this rebuilds the entire Step; hardcoded null means **reconnect redelivery erases the key**.
    //   Observed after `spawn → applyPeerProseKey → spawn`: `peerProseKey` changed from PEER_a to null,
    //   and pending claims could not rescue it (that queue was already consumed).
    //   Same rule as `evtSeq` below: redelivery must not erase facts established on first arrival.
    peerProseKey: prev.steps[idx]?.peerProseKey ?? null,
    // ⚠ Duplicate step_started preserves the **earliest** sequence: a step's timeline position is set on first
    //   appearance; reconnect redelivery must not move it later (same rule as PeerTerminal.evtSeq).
    evtSeq: prev.steps[idx]?.evtSeq ?? input.evtSeq ?? null,
  };

  const nextSteps =
    idx >= 0
      ? prev.steps.map((s, i) => (i === idx ? step : s))
      : [...prev.steps, step];

  // Phase 1.7-A — push a `step` beat. Idempotent: if the last beat is a
  // step with the same stepId (dup step_started, e.g. resume after
  // disconnect), no-op the beat push so beats[] stays in lockstep with
  // the steps[] de-dup above.
  const lastBeat = prev.beats[prev.beats.length - 1];
  const skipBeat = lastBeat && lastBeat.kind === "step" && lastBeat.stepId === input.stepId;
  // Also skip if the same stepId beat already exists anywhere in beats —
  // mirrors the steps[] replace-by-id rule (dup step_started across the
  // sequence shouldn't add a second beat).
  const existsAnywhere = prev.beats.some((b) => b.kind === "step" && b.stepId === input.stepId);
  const nextBeats: TurnBeat[] = skipBeat || existsAnywhere
    ? prev.beats
    : [...prev.beats, { kind: "step", stepId: input.stepId }];

  _commit(input.turnId, { ...prev, steps: nextSteps, beats: nextBeats });
  // On this step's arrival, claim earlier unclaimed peer keys (event order is not guaranteed; see `_pendingProseKeys`).
  _drainPendingProseKey(input.turnId, input.stepId);
}

/**
 * **Pending claims** table when attachment fails: `turnId -> [{ spawnId, parentStepId }]`.
 *
 * ★2026-08-16 discovery: `applyPeerProseKey` was **fire-and-forget**:
 *   `if (idx < 0) return;` **permanently lost the key** whenever the spawn step was not yet in store,
 *   with no later claim on step arrival. Tests proved peer_dispatched before step_started
 *   ⇒ `peerProseKey` always null.
 * ⚠ Losing this key **breaks four consumers together**: excluding spawn rows from the current row, `doneViaSpawnRow`,
 *   failed-peer spawn-row filtering, and `rowPeerIdOf` prose attribution.
 * ⚠ Queue only events **with `parent_step_id`**: without it, attachment only dares use a sole global candidate;
 *   queuing merely defers guessing, rather than fixing it.
 */
const _pendingProseKeys = new Map<string, { spawnId: string; parentStepId: string }[]>();
const PENDING_CAP = 32;

/**
 * Queue until the spawn step arrives. **Only with `parent_step_id`**: without it, attachment depends on a sole global
 * candidate; queuing merely defers guessing, contrary to the prefer-no-claim-over-wrong-guess design.
 */
function _queueProseKey(turnId: string, spawnId: string, parentStepId?: string): void {
  if (!parentStepId) return;
  const q = _pendingProseKeys.get(turnId) ?? [];
  if (q.some((e) => e.spawnId === spawnId)) return;
  q.push({ spawnId, parentStepId });
  while (q.length > PENDING_CAP) q.shift();
  _pendingProseKeys.set(turnId, q);
}

/**
 * Claim on spawn-step arrival (called at the end of `applyStepStarted`).
 *
 * ⚠ Use `_attachProseKey`, which **never requeues**: the old remove-then-call-
 * `applyPeerProseKey` flow requeued failed claims for other reasons (stepId exists but is not a spawn step /
 * another key already attached), even though that step's `step_started` had already passed ⇒
 * the entry **never drained**, growing the table monotonically through the session (caught by independent review).
 * Now remove it regardless of attachment: once the step arrived, waiting longer changes nothing.
 */
function _drainPendingProseKey(turnId: string, stepId: string): void {
  const q = _pendingProseKeys.get(turnId);
  if (!q?.length) return;
  const hit = q.findIndex((e) => e.parentStepId === stepId);
  if (hit < 0) return;
  const { spawnId } = q[hit];
  q.splice(hit, 1);
  if (!q.length) _pendingProseKeys.delete(turnId);
  _attachProseKey(turnId, spawnId, stepId);
}

/**
 * Pure attachment: true on success, false on failure. **No enqueue**: `applyPeerProseKey` handles queuing,
 * while `_drainPendingProseKey` must use this path (see its comments).
 */
function _attachProseKey(turnId: string, spawnId: string, parentStepId?: string): boolean {
  const prev = _states.get(turnId);
  if (!prev) return false;
  if (prev.steps.some((s) => s.peerProseKey === spawnId)) return true;  // replay idempotency
  let idx = parentStepId
    ? prev.steps.findIndex(
        (s) =>
          s.stepId === parentStepId &&
          s.toolName === "subagent_spawn" &&
          s.peerProseKey === null,
      )
    : -1;
  // Historical events do not carry parent_step_id. Associate only when there
  // is exactly one candidate; with parallel siblings, an unattached peer block
  // is more truthful than guessing the wrong spawn row.
  if (!parentStepId) {
    const candidates = prev.steps
      .map((step, candidateIdx) => ({ step, candidateIdx }))
      .filter(
        ({ step }) =>
          step.toolName === "subagent_spawn" && step.peerProseKey === null,
      );
    idx = candidates.length === 1 ? candidates[0].candidateIdx : -1;
  }
  if (idx < 0) return false;
  const nextSteps = prev.steps.map((s, i) =>
    i === idx ? { ...s, peerProseKey: spawnId } : s,
  );
  _commit(turnId, { ...prev, steps: nextSteps });
  return true;
}

/**
 * Attach a peer id to the exact Lead `subagent_spawn` step. New events carry
 * `parent_step_id`; historical events retain the sequential arrival-order fallback.
 * Idempotent across reconnect/replay.
 *
 * ⚠ Failed claims **no longer disappear immediately** (2026-08-16): queue when the spawn step is not yet in store
 * (even before turn state exists), then claim after `applyStepStarted` arrives. Both old returns permanently lost keys,
 * silently breaking all four consumers of this key together.
 */
export function applyPeerProseKey(
  turnId: string,
  spawnId: string,
  parentStepId?: string,
): void {
  if (!turnId || !spawnId) return;
  if (_attachProseKey(turnId, spawnId, parentStepId)) return;
  _queueProseKey(turnId, spawnId, parentStepId);
}

/** dogfood s_3e854 ④: append a peer-prose token (partial_answer.delta) to the
 *  canonical turn-store, keyed by the peer id (partial_answer.parent_agent_id).
 *  Driven by dispatchToTurnStore on BOTH the live and replay paths, so the render
 *  reads one store everywhere (no separate runner/archived prose store). */
export function applyPeerProse(turnId: string, peerId: string, delta: string): void {
  if (!turnId || !peerId || !delta) return;
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  const peerProse = {
    ...prev.peerProse,
    [peerId]: (prev.peerProse[peerId] ?? "") + delta,
  };
  _commit(turnId, { ...prev, peerProse });
}

/** Record a scheduled model retry. Ignored once the card is finalized: a finished
 *  turn must never look like it is still retrying. */
export function applyModelRetryScheduled(turnId: string, status: ModelRetryStatus): void {
  if (!turnId) return;
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  if (prev.cardFinalized || prev.cardFinalState !== null) return;
  _commit(turnId, { ...prev, modelRetry: status });
}

/** Clear a pending model retry once a later event arrives; no commit when none is pending. */
export function clearModelRetry(turnId: string, seq?: number): void {
  const prev = _states.get(turnId);
  if (!prev?.modelRetry) return;
  const retrySeq = prev.modelRetry.seq;
  if (retrySeq !== undefined && seq !== undefined && seq <= retrySeq) return;
  const { modelRetry: _cleared, ...rest } = prev;
  void _cleared;
  _commit(turnId, rest);
}

/** P4a (chat-redesign): persist a peer's agent identity (peer_dispatched.peer_name),
 *  keyed by the peer's subagent id (== spawn_id / the id its inner steps carry as
 *  `subagentId`). Lets the agent-block grouping resolve a peer's steps → an agent.
 *  Idempotent (replay-safe — re-dispatch of the same peer_dispatched is a no-op). */
export function applyPeerName(
  turnId: string,
  peerId: string,
  peerName: string,
  task?: string,
  displayName?: string,
): void {
  if (!turnId || !peerId || !peerName) return;
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  const sameName = prev.peerNames[peerId] === peerName;
  const normalizedDisplayName = displayName?.trim() || undefined;
  // The first non-empty start-shape value is the persisted identity snapshot.
  // Later node/finish shapes omit it and must never clear or replace it.
  const shouldSetDisplayName =
    !!normalizedDisplayName && !prev.peerDisplayNames[peerId];
  // agent_spawned re-fires (spawn/finish/max-iter); later emits may omit task →
  // only update peerTasks when a task is present, and keep the prior one otherwise.
  const sameTask = !task || prev.peerTasks[peerId] === task;
  if (sameName && sameTask && !shouldSetDisplayName) return; // no-op on replay / duplicate
  _commit(turnId, {
    ...prev,
    peerNames: { ...prev.peerNames, [peerId]: peerName },
    peerDisplayNames: shouldSetDisplayName
      ? { ...prev.peerDisplayNames, [peerId]: normalizedDisplayName }
      : prev.peerDisplayNames,
    peerTasks: task ? { ...prev.peerTasks, [peerId]: task } : prev.peerTasks,
  });
}

export interface PeerAgentAttribution {
  agentId?: string;
  agentRevisionId?: string;
  agentReleaseId?: string;
  agentExecutionId?: string;
  displayNameSnapshot?: string;
}

export interface ApplyPeerAgentAttributionInput extends PeerAgentAttribution {
  turnId: string;
  peerId: string;
}

/** Fold the additive M3 Agent identity fields without trusting event order.
 * Legacy events contain none of these fields and remain a no-op. */
export function applyPeerAgentAttribution(
  input: ApplyPeerAgentAttributionInput,
): void {
  if (!input.turnId || !input.peerId) return;
  const clean = (value: string | undefined): string | undefined =>
    value?.trim() || undefined;
  const incoming: PeerAgentAttribution = {
    agentId: clean(input.agentId),
    agentRevisionId: clean(input.agentRevisionId),
    agentReleaseId: clean(input.agentReleaseId),
    agentExecutionId: clean(input.agentExecutionId),
    displayNameSnapshot: clean(input.displayNameSnapshot),
  };
  if (!Object.values(incoming).some(Boolean)) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const prior = prev.peerAgentAttribution[input.peerId] ?? {};
  const next: PeerAgentAttribution = {
    agentId: prior.agentId ?? incoming.agentId,
    agentRevisionId: prior.agentRevisionId ?? incoming.agentRevisionId,
    agentReleaseId: prior.agentReleaseId ?? incoming.agentReleaseId,
    agentExecutionId: prior.agentExecutionId ?? incoming.agentExecutionId,
    displayNameSnapshot:
      prior.displayNameSnapshot ?? incoming.displayNameSnapshot,
  };
  if (
    prior.agentId === next.agentId &&
    prior.agentRevisionId === next.agentRevisionId &&
    prior.agentReleaseId === next.agentReleaseId &&
    prior.agentExecutionId === next.agentExecutionId &&
    prior.displayNameSnapshot === next.displayNameSnapshot
  ) {
    return;
  }
  _commit(input.turnId, {
    ...prev,
    peerAgentAttribution: {
      ...prev.peerAgentAttribution,
      [input.peerId]: next,
    },
  });
}

export interface ApplyPeerTerminalInput extends PeerTerminal {
  turnId: string;
  peerId: string;
}

/** Store the peer's terminal result. Later `lead.subagent_summary` facts may
 * enrich the earlier `peer_dispatch_failed` fallback with duration/reason. */
export function applyPeerTerminal(input: ApplyPeerTerminalInput): void {
  if (!input.turnId || !input.peerId) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const prior = prev.peerTerminalById[input.peerId];
  // A redelivered terminal from before the current continuation cannot settle it.
  if (
    prior?.rerunning &&
    prior.restartSeq !== undefined &&
    input.evtSeq !== undefined &&
    input.evtSeq < prior.restartSeq
  ) {
    return;
  }
  const keepSuccess =
    Boolean(prior?.earlierSucceeded) && input.status === "failed";
  const settledSeq =
    input.evtSeq !== undefined
      ? Math.max(input.evtSeq, prior?.settledSeq ?? input.evtSeq)
      : prior?.settledSeq;
  const continuationFailureReason = keepSuccess
    ? input.failureReason || prior?.continuationFailureReason
    : undefined;
  const next: PeerTerminal = {
    status: keepSuccess ? "succeeded" : input.status,
    ...(settledSeq !== undefined ? { settledSeq } : {}),
    ...(prior?.earlierSucceeded ? { earlierSucceeded: true } : {}),
    ...(continuationFailureReason ? { continuationFailureReason } : {}),
    ...(typeof input.durationMs === "number"
      ? { durationMs: input.durationMs }
      : prior?.durationMs !== undefined
        ? { durationMs: prior.durationMs }
        : {}),
    ...(keepSuccess
      ? {}
      : input.failureReason
        ? { failureReason: input.failureReason }
        : prior?.failureReason
          ? { failureReason: prior.failureReason }
          : {}),
    ...(input.stopReason
      ? { stopReason: input.stopReason }
      : prior?.stopReason && input.status === "partial"
        ? { stopReason: prior.stopReason }
        : {}),
    ...(keepSuccess
      ? {}
      : prior?.failureDiagnostics
        ? { failureDiagnostics: prior.failureDiagnostics }
        : input.failureDiagnostics
          ? { failureDiagnostics: input.failureDiagnostics }
          : {}),
    // ⚠ **Earliest write**: this terminal state is written twice (`agent_spawned` records state,
    //   `lead.subagent_summary` later adds reason/diagnostics). Using the later sequence
    //   moves this terminal row to the band's history tail, exactly the error ordering must prevent.
    ...(prior?.evtSeq !== undefined
      ? { evtSeq: prior.evtSeq }
      : input.evtSeq !== undefined
        ? { evtSeq: input.evtSeq }
        : {}),
    ...((input.resumeCheckpointId ?? prior?.resumeCheckpointId)
      ? { resumeCheckpointId: input.resumeCheckpointId ?? prior?.resumeCheckpointId }
      : {}),
    ...((input.resumeRefused ?? prior?.resumeRefused)
      ? { resumeRefused: input.resumeRefused ?? prior?.resumeRefused }
      : {}),
  };
  if (
    prior?.status === next.status &&
    !prior.rerunning &&
    prior.durationMs === next.durationMs &&
    prior.failureReason === next.failureReason &&
    prior.failureDiagnostics === next.failureDiagnostics &&
    prior.stopReason === next.stopReason &&
    prior.resumeCheckpointId === next.resumeCheckpointId &&
    prior.resumeRefused === next.resumeRefused &&
    prior.evtSeq === next.evtSeq &&
    prior.settledSeq === next.settledSeq &&
    prior.continuationFailureReason === next.continuationFailureReason
  ) {
    return;
  }
  _commit(input.turnId, {
    ...prev,
    peerTerminalById: { ...prev.peerTerminalById, [input.peerId]: next },
  });
}

export interface ApplyPeerRestartInput {
  turnId: string;
  peerId: string;
  evtSeq?: number;
}

/** A settled Agent's runner started again in this turn: an @Agent
 * continuation reuses the primary's subagent id. Its block runs again until
 * that run settles. Only an ordered start after the latest terminal counts,
 * so reconnect redelivery cannot reopen a settled Agent. */
export function applyPeerRestart(input: ApplyPeerRestartInput): void {
  if (!input.turnId || !input.peerId || input.evtSeq === undefined) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const prior = prev.peerTerminalById[input.peerId];
  if (!prior || prior.rerunning) return;
  if (prior.settledSeq === undefined || input.evtSeq <= prior.settledSeq) {
    return;
  }
  _commit(input.turnId, {
    ...prev,
    peerTerminalById: {
      ...prev.peerTerminalById,
      [input.peerId]: {
        ...prior,
        rerunning: true,
        restartSeq: input.evtSeq,
        earlierSucceeded: prior.earlierSucceeded || prior.status !== "failed",
      },
    },
  });
}

export interface ApplyApprovalRequiredInput {
  turnId: string;
  approvalId: string;
}

export function applyApprovalRequired(input: ApplyApprovalRequiredInput): void {
  if (!input.turnId || !input.approvalId) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const prior = prev.approvalStatusById[input.approvalId];
  // Approval lifecycle is monotonic. A late/replayed required event must not
  // resurrect a terminal action as pending and permanently hold the fold gate.
  // "rejected" (R-2) is terminal too — a stale required must not downgrade it.
  if (
    prior === "pending" ||
    prior === "executed" ||
    prior === "execution_failed" ||
    prior === "rejected"
  )
    return;
  _commit(input.turnId, {
    ...prev,
    approvalStatusById: {
      ...prev.approvalStatusById,
      [input.approvalId]: "pending",
    },
  });
}

export interface ApplyApprovalExecutedInput {
  turnId: string;
  approvalId: string;
  ok: boolean;
}

export function applyApprovalExecuted(input: ApplyApprovalExecutedInput): void {
  if (!input.turnId || !input.approvalId) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const prior = prev.approvalStatusById[input.approvalId];
  // "rejected" is a terminal human decision — a late/replayed execution event
  // must never un-reject it (an approval is reject XOR execute on the backend,
  // so this only bites replay/ordering anomalies, but the guard makes the
  // monotonic-terminal invariant structural; mirrors applyApprovalRequired).
  if (prior === "rejected") return;
  const status: ApprovalExecutionStatus = input.ok
    ? "executed"
    : "execution_failed";
  if (prior === status) return;
  _commit(input.turnId, {
    ...prev,
    approvalStatusById: {
      ...prev.approvalStatusById,
      [input.approvalId]: status,
    },
  });
}

export interface ApplyApprovalRejectedInput {
  turnId: string;
  approvalId: string;
}

/** R-2: mark a rejected approval terminal so approvalStatusById is honest
 * (pending → rejected) instead of stuck at "pending" forever. Idempotent and
 * replay-safe (live dispatch == F5 replay). */
export function applyApprovalRejected(input: ApplyApprovalRejectedInput): void {
  if (!input.turnId || !input.approvalId) return;
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  const prior = prev.approvalStatusById[input.approvalId];
  // Terminal is monotonic (mirrors applyApprovalRequired): a stale/replayed
  // rejection must not overwrite an already-recorded terminal. Idempotent on
  // an existing "rejected"; refuses to clobber "executed"/"execution_failed".
  if (
    prior === "rejected" ||
    prior === "executed" ||
    prior === "execution_failed"
  )
    return;
  _commit(input.turnId, {
    ...prev,
    approvalStatusById: {
      ...prev.approvalStatusById,
      [input.approvalId]: "rejected",
    },
  });
}

export interface ApplyStepProgressInput {
  turnId: string;
  stepId: string;
  /** Append-only token delta (code/sql tokens for lead_writing). */
  contentDelta?: string;
  /** Sub-step completion progress — for bi_query batches. */
  subStepsUpdate?: SubStepResult[];
}

export function applyStepProgress(input: ApplyStepProgressInput): void {
  const prev = _states.get(input.turnId);
  if (!prev) return;

  const idx = prev.steps.findIndex((s) => s.stepId === input.stepId);
  if (idx < 0) return;

  const step = prev.steps[idx];
  const next: Step = { ...step };
  if (typeof input.contentDelta === "string") {
    next.partialContent = (step.partialContent ?? "") + input.contentDelta;
  }
  if (input.subStepsUpdate) {
    next.subSteps = input.subStepsUpdate;
  }

  const nextSteps = prev.steps.map((s, i) => (i === idx ? next : s));
  _commit(input.turnId, { ...prev, steps: nextSteps });
}

export interface ApplyStepDoneInput {
  turnId: string;
  stepId: string;
  /** Terminal state. step_done with state=done|failed|partial. */
  state: Extract<StepState, "done" | "failed" | "partial">;
  /** Right-hand summary text. */
  summary?: string | null;
  /** Body detail blob. */
  detail?: StepDetail | null;
  /** Final substep results (overrides any progress-time substepsUpdate). */
  subSteps?: SubStepResult[] | null;
  /** Stable refs for stdout/Markdown/files produced by this step. */
  outputArtifacts?: StepOutputArtifact[] | null;
  /** Backend ms duration. */
  durationMs?: number | null;
  error?: StepError | null;
}

/** ⑩ (bug 0708): fail all still-"active" steps of a turn. Used on hydrate of a turn
 *  interrupted by a gateway restart — its step_started events replayed but no step_done
 *  ever will, so the steps would otherwise stay stuck on "运行中…". Left with error:null
 *  so each doesn't spawn a per-step FailureAckCard (the turn as a whole failed; the
 *  system message already tells the user it was interrupted). Idempotent. */
export function failActiveSteps(turnId: string): void {
  const prev = _states.get(turnId);
  if (!prev) return;
  let changed = false;
  const nextSteps = prev.steps.map((s) =>
    s.state === "active" ? ((changed = true), { ...s, state: "failed" as const }) : s,
  );
  if (changed) _commit(turnId, { ...prev, steps: nextSteps });
}

export function applyStepDone(input: ApplyStepDoneInput): void {
  const prev = _states.get(input.turnId);
  if (!prev) return;

  const idx = prev.steps.findIndex((s) => s.stepId === input.stepId);
  if (idx < 0) return;

  const step = prev.steps[idx];
  const next: Step = {
    ...step,
    state: input.state,
    summary: input.summary ?? step.summary,
    detail: input.detail ?? step.detail,
    subSteps: input.subSteps ?? step.subSteps,
    outputArtifacts: input.outputArtifacts ?? step.outputArtifacts,
    durationMs: typeof input.durationMs === "number" ? input.durationMs : step.durationMs,
    error: input.error ?? step.error,
  };

  const nextSteps = prev.steps.map((s, i) => (i === idx ? next : s));
  _commit(input.turnId, { ...prev, steps: nextSteps });
}

export interface ApplyNarrativeAppendInput {
  turnId: string;
  text: string;
  /**
   * Phase 1.7-A — segment identifier for the narrative beat (FE-derived
   * by the dispatcher's per-turn cursor; a contiguous run of
   * `narrative.delta` events shares one segId). When `segId` matches the
   * last narrative beat, the new text appends; else a new beat is pushed.
   * Caller must pass segId in 1.7-A wiring.
   */
  segId: string;
}

export function applyNarrativeAppend(input: ApplyNarrativeAppendInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  // Phase 1.7-A — push or append a `narrative` beat per the same-segId
  // append rule (tech-design §2.2). If the last beat is narrative with
  // the same segId, append the delta; else push a new beat.
  const lastBeat = prev.beats[prev.beats.length - 1];
  let nextBeats: TurnBeat[];
  if (lastBeat && lastBeat.kind === "narrative" && lastBeat.segId === input.segId) {
    nextBeats = prev.beats.slice(0, -1).concat({
      kind: "narrative",
      segId: lastBeat.segId,
      text: lastBeat.text + input.text,
    });
  } else {
    nextBeats = [...prev.beats, { kind: "narrative", segId: input.segId, text: input.text }];
  }
  _commit(input.turnId, { ...prev, beats: nextBeats });
}

// chat-solidify W7 (2026-07-03): removed applyAnswerToken no-op; its claimed
// legacy caller (use-turn-runner) had long since disappeared (0 runtime callers).
// Per-token answer streaming does not enter beats; final answer uses one applyFinalAnswer beat.

export interface ApplyFinalAnswerInput {
  turnId: string;
  /** Full final text — used by legacy callers only; beat carries payload only. */
  text: string;
  payload?: AnalysisPayloadRef | null;
  /** D3: the answer is a legacy system failure fallback (see `isLegacyFailureAnswerReason`). */
  legacyFailureFallback?: boolean;
}

export function applyNarrativeSuperseded(turnId: string): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  if (prev.narrativeSuperseded) return;
  _commit(turnId, { ...prev, narrativeSuperseded: true });
}

export function applyFinalAnswer(input: ApplyFinalAnswerInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  // Phase 1.7-A — ONE `answer` beat per turn. A turn can emit final_answer twice
  // (M2 report-reliability: the canvas-pointer reconciliation corrective fires a
  // 2nd final_answer after a D6-hidden phantom; runner.answer is overwritten in
  // place — see use-turn-runner "final_answer" case). Appending a 2nd beat would
  // render a duplicate chat bubble in scrollback, so REPLACE the existing answer
  // beat in place — keeps latest-payload-wins (selectAnswerPayload) without dup.
  const payloadRef: AnalysisPayloadRef = input.payload ?? { turnId: input.turnId };
  // O2: beat carries text, the single source for live/archived/F5 bubbles (replacement ensures
  // a second final_answer, M2 correction, updates text rather than creating duplicate bubbles).
  const answerBeat = {
    kind: "answer" as const,
    payloadRef,
    text: input.text,
    ...(input.legacyFailureFallback ? { legacyFailureFallback: true } : {}),
  };
  const idx = prev.beats.findIndex((b) => b.kind === "answer");
  const nextBeats =
    idx >= 0
      ? prev.beats.map((b, i) => (i === idx ? answerBeat : b))
      : [...prev.beats, answerBeat];
  _commit(input.turnId, { ...prev, beats: nextBeats });
}

/**
 * Chat turn feedback D3: the failure terminal arrived, so drop an answer beat that was only a
 * legacy system failure fallback. The failure card is the turn's one failure statement. Live
 * SSE and replay run the same dispatcher branch, so both drop it.
 */
export function dropLegacyFailureAnswer(turnId: string): void {
  const prev = _states.get(turnId);
  if (!prev) return;
  if (!prev.beats.some((b) => b.kind === "answer" && b.legacyFailureFallback)) return;
  _commit(turnId, {
    ...prev,
    beats: prev.beats.filter((b) => !(b.kind === "answer" && b.legacyFailureFallback)),
  });
}

export interface ApplyClarificationInput {
  turnId: string;
  clarification: ClarificationState | null;
}

export function applyClarification(input: ApplyClarificationInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  _commit(input.turnId, { ...prev, clarification: input.clarification });
}

/**
 * Phase 1.7-A — push a `picker` beat carrying the Sprint θ turn-terminating
 * `ThetaClarificationState`. Called from lib/use-turn-runner.ts in the
 * clarification handler (alongside the existing setState that sets
 * `runner.clarificationPending`). Idempotent by `clarificationId`.
 *
 * P0-r3.1: the beat carries `ThetaClarificationState` (lib/types.ts:515),
 * NOT the V2.7 `ClarificationState` above — two different shapes; the
 * picker UI in TurnContainer reads from runner.clarificationPending today
 * and the beat is order-only for 1.7-A.
 */
export function pushPickerBeat(
  turnId: string,
  clarification: ThetaClarificationState,
): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  // Idempotency: skip if a picker beat with the same clarificationId is
  // already in beats (dup clarification_required is rare but possible
  // across resume / replay paths).
  const exists = prev.beats.some(
    (b) => b.kind === "picker" && b.clarification.clarificationId === clarification.clarificationId,
  );
  if (exists) return;
  _commit(turnId, {
    ...prev,
    beats: [...prev.beats, { kind: "picker", clarification }],
  });
}

/**
 * Planning Mode — push a `plan` beat so the card renders inline in the
 * proposal turn's flow. Idempotent by planId (dup plan_proposed across
 * replay/reconnect won't add a second card). Plan data lives in plan-store.
 */
export function pushPlanBeat(turnId: string, planId: string): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  const exists = prev.beats.some((b) => b.kind === "plan" && b.planId === planId);
  if (exists) return;
  _commit(turnId, { ...prev, beats: [...prev.beats, { kind: "plan", planId }] });
}

/** The draft a revision Turn revises: its file name and "draft N" (no task identifiers). */
export interface DocumentRevisionReceipt {
  title: string;
  version: number;
}

export function parseDocumentRevisionReceipt(value: unknown): DocumentRevisionReceipt | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (typeof item.title !== "string" || !item.title.trim() || item.title.length > 200) return null;
  if (typeof item.version !== "number" || !Number.isSafeInteger(item.version) || item.version < 1) return null;
  return { title: item.title.trim(), version: item.version };
}

/** First write wins: a resumed Turn's later `turn_started` cannot rename the draft it revises. */
export function applyTurnDocumentRevision(turnId: string, receipt: DocumentRevisionReceipt): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  if (prev.documentRevision) return;
  _commit(turnId, { ...prev, documentRevision: receipt });
}

/** Record the plan a Turn left blocked (`plan_blocked`). Idempotent; the latest plan wins. */
export function applyTurnBlockedPlan(turnId: string, planId: string): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  if (prev.blockedPlanId === planId) return;
  _commit(turnId, { ...prev, blockedPlanId: planId });
}

/**
 * Config copilot batch 0.5: push a `lite` beat so the lightweight confirmation strip renders inline in the turn's
 * flow. Idempotent by proposalId (dup config.lite_propose across replay/reconnect
 * won't add a second card). Proposal payload lives in config-copilot-lite store.
 */
export function pushLiteBeat(turnId: string, proposalId: string): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  const exists = prev.beats.some((b) => b.kind === "lite" && b.proposalId === proposalId);
  if (exists) return;
  _commit(turnId, { ...prev, beats: [...prev.beats, { kind: "lite", proposalId }] });
}

/**
 * Config copilot batch 1-b: push a `draft` beat (config-draft chip). **Per-turn idempotent**: keep one per
 * draftId within a turn (no replay/reconnect duplicates), but one in each turn so each conversational update remains
 * visible in its own turn (tech-design-batch1b §3-4). Draft payload lives in config-copilot-draft store.
 */
export function pushDraftBeat(turnId: string, draftId: string): void {
  const prev = _states.get(turnId) ?? _emptyTurnState(turnId);
  const exists = prev.beats.some((b) => b.kind === "draft" && b.draftId === draftId);
  if (exists) return;
  _commit(turnId, { ...prev, beats: [...prev.beats, { kind: "draft", draftId }] });
}

export interface ApplySteerBufferedInput {
  turnId: string;
  entry: SteerEntry;
}

export function applySteerBuffered(input: ApplySteerBufferedInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  _commit(input.turnId, {
    ...prev,
    steerBuffer: [...prev.steerBuffer, input.entry],
  });
}

export interface ApplyEmojiAckInput {
  turnId: string;
  ack: EmojiAck;
}

export function applyEmojiAck(input: ApplyEmojiAckInput): void {
  const prev = _states.get(input.turnId) ?? _emptyTurnState(input.turnId);
  _commit(input.turnId, {
    ...prev,
    emojiAcks: [...prev.emojiAcks, input.ack],
  });
}

/**
 * Treat `errorKind` as a hint for the FailureAckCard variant when
 * the backend StepError.kind isn't strictly "transient" | "permanent".
 * Phase 1.2 maps "needs_user_input" + "unknown" → "transient" so the
 * user always sees a retry button as the safe default.
 */
export function inferFailureUiKind(error: StepError | null): FailureKind {
  if (!error) return "transient";
  if (error.kind === "permanent") return "permanent";
  return "transient";
}

// Browser E2E parity probe, matching canvas-store's existing snapshot hook.
// The build-time flag is absent in product deployments, so no runtime API is
// exposed outside the deterministic test server.
if (
  typeof window !== "undefined"
  && process.env.NEXT_PUBLIC_BOWHEAD_E2E === "1"
) {
  (window as unknown as {
    __TURN_STORE_SNAPSHOT__?: (turnId: string) => TurnState | undefined;
  }).__TURN_STORE_SNAPSHOT__ = getTurnSnapshot;
}
