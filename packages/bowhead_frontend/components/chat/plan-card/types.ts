/**
 * Frontend view-model for the Planning Mode card. Mirrors the backend
 * plan_proposed / step_* / plan_* SSE payloads (see lib/sse-events.ts) and
 * the dev REST plan responses, projected to what the card renders.
 *
 * Backend enums (plan_store.py):
 *   PlanStatus  = proposed | awaiting_amend | running | blocked | completed | failed | cancelled
 *   StepStatus  = pending | ready | running | done | failed | skipped | blocked
 * `ready` collapses to `pending` for display (both = "待运行").
 */

export type PlanStatus =
  | "proposed"
  | "running"
  | "blocked"
  | "completed"
  | "failed"
  | "cancelled";

export type PlanStepStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "blocked"
  | "skipped"
  /** graph-mode batch 3: old steps superseded by revision/replanning (dead terminal rows, hidden by default). */
  | "superseded";

export interface PlanStepView {
  id: string;
  /** Raw backend agent_name (e.g. "compliance-analyst"); mapped to a role chip. */
  agent: string;
  objective: string;
  /** Topological depth assigned by the backend (0 = first parallel batch). */
  level: number;
  dependsOn: string[];
  needsCorpus: boolean;
  status: PlanStepStatus;
  /** A (editable): soft include/exclude — false = gray strikethrough, skipped during execution. Default true. */
  included: boolean;
  /** Display order within a level (backend seq). */
  seq?: number;
  /** "initial" | "amendment" — amendment steps render as the AI-ghost in amend preview. */
  origin?: string;
  /** Optional — backend may add a per-step deliverable name later. */
  deliverable?: string | null;
  /** Blocked/failed envelope (from step_blocked / step_failed). */
  reason?: string | null;
  errorKind?: string | null;
  /** planning-mode-audit WP-7: external public information step (web search, compute+web_search). */
  needsWeb?: boolean;
  /** WP-7 proposal-time feasibility: ok | needs_context (BI unbound) | no_corpus (KB empty). */
  feasibility?: "ok" | "needs_context" | "no_corpus" | null;
  /** graph-mode batch 1: acceptance criteria (second generation pass; approval covers objective + acceptance). */
  successCriteria?: string[];
  /** graph-mode batch 1: comparative verification result (step_done.verify / REST verify).
   *  skipped must look distinct from pass (coverage signal; broken verifier must not look all-green). */
  verify?: PlanStepVerify | null;
  /** graph-mode batch 3: revision pointer (revise = successor step ID; replan = generation sentinel string). */
  supersededBy?: string | null;
  /** M3-13E: exact Agent/revision frozen server-side; hidden, returned unchanged only on plan-edit confirmation. */
  agentChoice?: Record<string, unknown> | null;
}

export interface PlanStepVerifyItem {
  text: string;
  status: "met" | "unmet" | "cannot_tell" | "not_applicable";
  /** Backend one-sentence rationale when unmet (≤80 characters). */
  note?: string;
}

export interface PlanStepVerify {
  // out_of_domain: output honestly states this Datarc workspace has no data for the domain; an honest notice, not a failure.
  verdict: "pass" | "partial" | "fail" | "skipped" | "out_of_domain";
  unmet: string[];
  note?: string;
  /** Per-criterion status + rationale. Old events/REST responses lack this field; consumers must tolerate its absence. */
  items?: PlanStepVerifyItem[];
}

export interface PlanView {
  planId: string;
  /** Revision of the displayed steps; absent in older proposal events. */
  currentRev?: number;
  /** User query (backend title = first 80 chars of the query). */
  title: string;
  status: PlanStatus;
  steps: PlanStepView[];
  /** 2026-07: plan carries its Space (hook1 persisted sctx.workspace_id). Card actions
   *  (approve/cancel/skip/reconcile) prefer it; localStorage can differ from the database
   *  (dev-stub observed "" vs "1" → 404). undefined = absent in old events, fall back
   *  to localStorage. */
  workspaceId?: string;
  /** delivery-surface C3 (116 dogfood 1.0.93 P2): "answer" plan delivers a chat
   *  answer; completed state must not show「Report ready」/「View report」. undefined = old events lack
   *  this field; treat as "report" (unchanged behavior). */
  deliverableKind?: string;
}

/** Card variant derived from plan status. */
export type PlanVariant = "proposed" | "running" | "blocked" | "completed";

export function variantForStatus(status: PlanStatus): PlanVariant {
  switch (status) {
    case "running":
      return "running";
    case "blocked":
    case "failed":
      return "blocked";
    case "completed":
      return "completed";
    default:
      return "proposed";
  }
}
