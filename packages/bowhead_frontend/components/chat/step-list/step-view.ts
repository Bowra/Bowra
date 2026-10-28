/**
 * step-view — the **single** projection from turn-store `Step` to rendering `StepView`.
 *
 * chat-ux-2026-08 (invariant inventory §2.11). Previously two hand-written copies:
 * `StepListCardConnected.stepToStepView` (archive / F5 / no-beat path) and
 * `TurnContainer.collectStepViews` (main live path). Identical field sets,
 * even separate `formatDuration` implementations; only substantive difference was `uiKind`:
 * the former added `inferFailureUiKind(error)`, while the latter passed it through.
 *
 * **Describe the actual impact precisely** (correction required by final review; do not overstate it again):
 *   · `isRecoverableStepFailure` reads `uiKind ?? kind`, while `inferFailureUiKind` maps
 *     both `needs_user_input` / `unknown` to `"transient"`. The projections disagree **only
 *     for those two kind values** (red ✕ + always forced open vs amber ⚠).
 *   · **Production cannot currently reach this**: the only writer of `Step.error` is
 *     `turn-store-dispatch.ts`'s `mapError` → `sse-events.ts`'s `normalizeStepError`,
 *     hard-coding `kind: isPermanent ? "permanent" : "transient"` (`:370`).
 *     Live SSE and F5 replay share this path, so rendering divergence was **never** observed in production.
 *   · Merge rather than maintain synchronized copies: `StepError.kind` declares four values,
 *     as does `normalizeStepError`'s return type (only implementation narrows it to two);
 *     relaxing the normalizer immediately exposes divergence in the most visible place, with no mechanism updating both copies.
 *
 * Now there is one. **Never inline a second copy at any caller**:
 * add fields here for every surface to receive them.
 * Gate: `TurnContainer.step-projection.test.tsx` (including a real-ingest case).
 */
import { inferFailureUiKind, type PeerTerminal, type Step } from "@/lib/turn-store";

import type { StepView } from "./types";

/**
 * A failed spawn with a failed or absent peer terminal must remain a hard
 * failure even when the original Tool error was transient. Keep this severity
 * correction in the shared projection for live, archived and replayed views.
 * A known successful peer retains the original conflicting step result.
 * Issue counts preserve the failed step regardless of its display severity.
 */
function correctSpawnFailureKind(
  step: Step,
  peerTerminalById: Record<string, PeerTerminal>,
): Step["error"] {
  const peerId = step.peerProseKey ?? "";
  if (!step.error || !peerId) return step.error;
  const status = peerTerminalById[peerId]?.status;
  if (status !== undefined && status !== "failed") return step.error;
  return { ...step.error, uiKind: "permanent" };
}

/**
 * ⚠ `peerTerminalById` is **deliberately required** (caught in review 11): when optional,
 * a caller omitting the table and a peer lacking a terminal state both become
 * `undefined` in `correctSpawnFailureKind`; the latter takes the correct-anyway branch, so a new caller
 * omitting it makes **every** failed spawn row red/permanent (even actually successful peers),
 * while the suite still passes (the sole positive control explicitly provided the table). Required makes omission a compiler error.
 * Pass `{}` when there is no terminal state.
 */
export function stepToStepView(
  step: Step,
  peerTerminalById: Record<string, PeerTerminal>,
): StepView {
  // Older turns persisted model-only admission guidance in the user trace.
  // Project it at the shared live/replay boundary so those turns stay readable
  // after the backend starts emitting a public error envelope.
  const legacyAdmissionBlock =
    step.toolName === "subagent_spawn" &&
    step.state === "failed" &&
    typeof step.error?.message === "string" &&
    step.error.message.startsWith("Dispatch refused:");
  const correctedError = legacyAdmissionBlock && step.error
    ? { ...step.error, kind: "permanent" as const, uiKind: "permanent" as const, code: "agent_admission_blocked", message: "", retryable: false }
    : correctSpawnFailureKind(step, peerTerminalById);
  //「准备中」duration_ms is a hard-coded backend 0 when closing the placeholder, not elapsed time (see `foldWritingSteps`).
  const durationText =
    step.durationMs != null && !isWritingStep(step) ? formatDuration(step.durationMs) : undefined;

  return {
    stepId: step.stepId,
    parentStepId: step.parentStepId,
    concurrencyGroupId: step.concurrencyGroupId,
    stepType: step.stepType,
    toolName: step.toolName,
    headerZh: step.headerZh,
    label: step.label ?? undefined,
    state: step.state,
    summary: legacyAdmissionBlock ? undefined : step.summary ?? undefined,
    durationText,
    detail: legacyAdmissionBlock ? null : step.detail,
    subSteps: step.subSteps,
    outputArtifacts: step.outputArtifacts,
    partialContent: step.partialContent,
    // CTO review P1 #4 — preserve the source `kind` (carrying backend
    // taxonomy: needs_user_input / unknown / ...) and surface the narrowed
    // FailureKind via `uiKind`. FailureAckCard reads `uiKind` to pick the
    // failure variant (hint vs permanent); isRecoverableStepFailure reads
    // `uiKind ?? kind` to pick amber vs red.
    error: correctedError
      ? { ...correctedError, uiKind: correctedError.uiKind ?? inferFailureUiKind(correctedError) }
      : null,
    subagentName: step.subagentName ?? undefined,
    isStreaming: step.state === "active" && (step.partialContent?.length ?? 0) > 0,
    peerProseKey: step.peerProseKey ?? undefined, // dogfood s_3e854 ④
    // P4a: the peer's subagent id is the agent-block grouping key — without it
    // AgentTrace can't resolve peerNames[subagentId] and every step degrades to
    // the lead block (no peer blocks render).
    subagentId: step.subagentId ?? undefined,
    // chat-ux-2026-08: order basis for placing step and peer terminal events in one running-band history.
    evtSeq: step.evtSeq ?? undefined,
  };
}

/**
 * Merge preparation and execution steps belonging to the same tool call:
 * one kb_search call rendered「搜索知识库 0ms」+「搜索知识库 · 找到 5 条 367ms」as two rows).
 *
 * For Lead python_sandbox / bi_query / kb_search / web_search, while the model writes arguments, backend
 * first emits `writing_<call_id>` (`step_type=lead_thinking_writing`; argument tokens use step_progress
 * so users see SQL/Python being written), closes it on execution (`duration_ms: 0`), then starts `step_<call_id>`.
 * Both events persist unchanged; merging happens only here, the shared projection for live, F5 replay, and archive,
 * so all three show the same row.
 *
 * Rules:
 *   · Writing only: keep the row unchanged (live argument expansion unchanged; names follow `tool-catalog.ts::writingDisplayKey`,
 *     「正在写代码」「正在写查询」for code/query writing,「准备中」for other tools); 0ms is a hidden placeholder, not elapsed time.
 *   · Execution arrives: replace writing at its **position**, never keep a separate writing row;
 *     state, duration, result, and codeInput all come from execution.
 *   · Merged row retains writing's `stepId` and `evtSeq`: `stepId` is the band/ledger React key,
 *     so changing to execution ID unmounts/remounts the row (replays entrance animation, loses expansion state);
 *     `evtSeq` is its first appearance order, matching `applyStepStarted`'s no-reordering-on-reemit rule.
 *   · Execution with no matching writing remains unchanged in the original order.
 */
function isWritingStep(step: Step): boolean {
  return step.stepType === "lead_thinking_writing";
}

function writingCallId(step: Step): string | null {
  if (step.callId) return step.callId;
  return step.stepId.startsWith("writing_") ? step.stepId.slice("writing_".length) : null;
}

export function foldWritingSteps(steps: readonly Step[]): Step[] {
  const executionFor = new Map<number, number>();
  const consumed = new Set<number>();
  steps.forEach((step, i) => {
    if (!isWritingStep(step)) return;
    const callId = writingCallId(step);
    if (!callId) return;
    const j = steps.findIndex(
      (s, k) =>
        k !== i &&
        !consumed.has(k) &&
        !isWritingStep(s) &&
        (s.stepId === `step_${callId}` || (s.callId === callId && s.stepType === "tool_run")),
    );
    if (j < 0) return;
    executionFor.set(i, j);
    consumed.add(j);
  });
  if (consumed.size === 0) return [...steps];
  const out: Step[] = [];
  steps.forEach((step, i) => {
    if (consumed.has(i)) return;
    const j = executionFor.get(i);
    if (j === undefined) {
      out.push(step);
      return;
    }
    const execution = steps[j]!;
    out.push({
      ...execution,
      stepId: step.stepId,
      evtSeq: step.evtSeq ?? execution.evtSeq,
    });
  });
  return out;
}

/** Single list projection for a turn: merge writing and execution steps, then project each step. */
export function stepsToStepViews(
  steps: readonly Step[],
  peerTerminalById: Record<string, PeerTerminal>,
): StepView[] {
  return foldWritingSteps(steps).map((step) => stepToStepView(step, peerTerminalById));
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)}s`;
  return `${Math.round(s)}s`;
}
