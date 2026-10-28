/**
 * CI run timeline (design S4, tech-design §6 "Timeline"): merged → candidate prepared → evaluating →
 * release check, derived only from what the workbench already returns plus the merge this page recorded.
 * No endpoint of its own.
 */
import type { CiMerge } from "./agent-ci-merges";
import type { AgentEvalProgress, AgentVersionWorkbench } from "./agents-api";

export type CiTimelineStepKey = "merged" | "prepared" | "evaluating" | "check";
export type CiTimelineState = "done" | "current" | "waiting" | "failed";
export type CiCheckOutcome = "ready" | "ready_with_risks" | "blocked" | "unknown";

export interface CiTimelineStep {
  key: CiTimelineStepKey;
  state: CiTimelineState;
  /** ISO time the step finished (or started, while current); null when the workbench doesn't say. */
  at: string | null;
  /** Who acted, when known: the Owner merges; nothing else is attributed without evidence. */
  actor: "owner" | null;
  progress?: AgentEvalProgress["cases"];
  /** Execution status for the evaluating step (queued, running, finalizing, failed, cancelled). */
  status?: string;
  outcome?: CiCheckOutcome;
}

const ACTIVE = new Set(["queued", "running", "finalizing"]);
const OUTCOMES = new Set<CiCheckOutcome>(["ready", "ready_with_risks", "blocked", "unknown"]);

/**
 * The CI flow this workbench shows: the newest merge recorded for the Agent, or, after a reload, a
 * candidate whose draft still carries CI changes. Returns null when no CI flow is in progress.
 */
export function ciRunTimeline(workbench: AgentVersionWorkbench, merge: CiMerge | null): CiTimelineStep[] | null {
  const candidate = workbench.candidate_identity;
  const preparedByMerge = merge ? (merge.preparedCandidateId ?? null) : null;
  const waitingForCandidate = Boolean(merge) && !preparedByMerge
    && (!candidate || candidate.candidate_id === merge!.candidateIdAtMerge);
  const ciCandidate = candidate !== null && (
    preparedByMerge !== null
      ? candidate.candidate_id === preparedByMerge
      : merge !== null
        ? candidate.candidate_id !== merge.candidateIdAtMerge
        : (workbench.draft_changes ?? []).some((change) => change.source === "ci")
  );
  if (!waitingForCandidate && !ciCandidate) return null;

  const merged: CiTimelineStep = {
    key: "merged",
    state: "done",
    at: merge ? new Date(merge.mergedAt).toISOString() : null,
    actor: merge ? "owner" : null,
  };
  if (waitingForCandidate) {
    return [
      merged,
      { key: "prepared", state: "current", at: null, actor: null },
      { key: "evaluating", state: "waiting", at: null, actor: null },
      { key: "check", state: "waiting", at: null, actor: null },
    ];
  }

  const prepared: CiTimelineStep = { key: "prepared", state: "done", at: null, actor: null };
  const execution = workbench.active_execution_summary;
  const snapshot = workbench.active_snapshot_identity;
  let evaluating: CiTimelineStep;
  if (execution && ACTIVE.has(execution.status)) {
    evaluating = { key: "evaluating", state: "current", at: execution.started_at, actor: null, status: execution.status, progress: execution.progress?.cases };
  } else if (snapshot || execution?.status === "completed") {
    evaluating = { key: "evaluating", state: "done", at: execution?.finished_at ?? snapshot?.created_at ?? null, actor: null, progress: execution?.progress?.cases };
  } else if (execution && (execution.status === "failed" || execution.status === "cancelled")) {
    evaluating = { key: "evaluating", state: "failed", at: execution.finished_at, actor: null, status: execution.status };
  } else {
    evaluating = { key: "evaluating", state: "waiting", at: null, actor: null };
  }

  const readiness = workbench.readiness;
  const outcome = snapshot && readiness && OUTCOMES.has(readiness as CiCheckOutcome) ? readiness as CiCheckOutcome : null;
  const check: CiTimelineStep = outcome
    ? { key: "check", state: outcome === "blocked" || outcome === "unknown" ? "failed" : "done", at: snapshot!.created_at, actor: null, outcome }
    : { key: "check", state: "waiting", at: null, actor: null };
  return [merged, prepared, evaluating, check];
}
