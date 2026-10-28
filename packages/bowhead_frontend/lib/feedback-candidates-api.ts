// agent-feedback-loop FE batch A — suggestion-inbox API (backend routes/feedback_candidates.py, dark:
//
// Responses match backend one-to-one; never guess server decisions here: API decides adoption/rejection permissions,
// UI consumes only server-computed viewer_is_admin / mine / for_my_agent signals.

import { apiJson } from "./api-client";
import { notifySettingsNavCountsChanged } from "./settings-nav-signal";

export type FeedbackTargetKind = "agent_def" | "semantic" | "team_skill" | "tool_source" | "new_asset" | "retire";
export type FeedbackCandidateState = "open" | "accepted" | "rejected" | "expired" | "superseded";

/** Coach reroute target (batch 3a: server-validated (kind, ref)); label always server-resolved, never trust model. */
export interface FeedbackRerouteTarget {
  target_kind: FeedbackTargetKind | null;
  target_ref: string | null;
  label?: string | null;
}

/** Background casework projection (batch 3a `casework.public()`): no prefilled body; proposer sees no reroute reason either. */
export interface FeedbackCasework {
  status: "queued" | "running" | "done" | "failed" | "blocked";
  verdict: "edit" | "reroute" | null;
  reason_code: string | null;
  reroute_target: (FeedbackRerouteTarget & { reason?: string | null }) | null;
  evidence_used: number | null;
  drafted_at: string | null;
}

/** FR7 recurrence: previously approved issue (same semantic signature = same target); v1 reports only, no auto rollback. */
export interface FeedbackRecurrence {
  /** Proposer sees only prior approval + elapsed time, other fields null (previous approver/coach target belong to inaccessible candidate). */
  candidate_id: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decided_by_display?: string | null;
  days_ago: number | null;
  /** Previous coach verdict target: now check whether target was wrong initially. */
  prior_verdict: "edit" | "reroute" | null;
}

export interface FeedbackCandidateRow {
  id: string;
  target_kind: FeedbackTargetKind;
  target_ref: string;
  proposed: Record<string, unknown> & { text?: string; asset_kind?: string };
  evidence_count: number;
  state: FeedbackCandidateState;
  created_at: string;
  expires_at: string;
  mine: boolean;
  for_my_agent?: boolean;
  proposer_id?: string;
  /** Target asset display name (resolved from agent/skill/tool); null if unresolved, UI falls back raw target_ref. */
  target_label?: string | null;
  /** Proposer display name (approver view only); unresolved falls back id. */
  proposer_display?: string | null;
  /** Background coach casework state (null without row). */
  casework?: FeedbackCasework | null;
  /** Same issue previously approved (null if none). */
  recurrence?: FeedbackRecurrence | null;
  /** When a closed suggestion was decided, withdrawn or expired (decided view only). */
  decided_at?: string | null;
  /** Accepted Business-definition suggestion (administrators, decided view): its wording and source Spaces. */
  space_targets?: { text: string; spaces: FeedbackSpaceInstructionTarget[] } | null;
}

/** Inbox lists: suggestions waiting for a decision, recently closed ones, or both. */
export type FeedbackInboxView = "open" | "decided" | "all";

export interface FeedbackEvidenceEntry {
  contributor_id: string | null;
  rubric_text: string;
  disclosure_ts: string | null;
  source_count: number;
}

export interface FeedbackCandidateDetail extends FeedbackCandidateRow {
  viewer_role: "admin" | "proposer" | "resource_owner";
  contributor_displays?: Record<string, string>;
  rubric_text: string;
  evidence: { entries: FeedbackEvidenceEntry[]; others_count: number };
  sources: { total: number; viewable: number; viewable_indexes: number[] };
  decided_by_me: boolean;
  decide_note: string | null;
}

/** A source Space of a Business-definition suggestion, with where its wording already landed. */
export interface FeedbackSpaceInstructionTarget {
  workspace_id: string;
  name: string;
  /** Distinct conversations in this Space behind the suggestion. */
  conversation_count: number;
  /** Current Space instructions version; 0 when the Space has none. */
  instructions_version: number;
  /** The version that added this suggestion's wording, if it was saved already. */
  added: { version: number; by_name: string; at: string } | null;
}

export type FeedbackPrefill =
  | { kind: "agent_system_prompt"; edited_content: string; rationale: string }
  | { kind: "team_skill_body"; edited_content: string; rationale: string }
  | { kind: "space_instructions"; text: string; spaces: FeedbackSpaceInstructionTarget[] }
  | { kind: "tool_source_patch"; proposal_text: string; current: Record<string, unknown> }
  | { kind: "cca_intent"; purpose: string; locale: string }
  | { kind: "retire"; asset_kind: string; asset_ref: string; native_action: string; reason: string };

export type FeedbackDegradeReason = "coach_unavailable" | "url_in_proposed" | "surface_too_large" | "coach_rerouted";

export interface FeedbackAdoptResult {
  id: string;
  state: FeedbackCandidateState;
  prefill?: FeedbackPrefill | null;
  base?: Record<string, string | number | null>;
  degraded?: boolean;
  degrade_reason?: FeedbackDegradeReason;
  reroute?: { reason: string; rationale: string; target?: FeedbackRerouteTarget };
  banner: { candidate_id: string; proposer_id: string; evidence_count: number; proposer_display?: string | null };
}

export interface FeedbackCoachStatus {
  /** This UTC month’s background coach drafts: count casework rows, not bills. */
  month_drafts: number;
  /** Team background-coach flag (default on); affects background casework only, not adoption-time drafting. */
  casework_enabled: boolean;
}

export function listFeedbackCandidates(
  view: FeedbackInboxView = "open",
): Promise<{ items: FeedbackCandidateRow[]; viewer_is_admin: boolean; coach?: FeedbackCoachStatus }> {
  return apiJson(view === "open" ? "/api/v1/feedback-candidates" : `/api/v1/feedback-candidates?view=${view}`);
}

/** Open suggestions waiting for the caller's decision (own proposals and co-signatures excluded). */
export function fetchFeedbackPendingCount(): Promise<{ count: number }> {
  return apiJson("/api/v1/feedback-candidates/pending-count");
}

/** Unread terminal receipt (batch 5 delivery surface). `notification_id` is acknowledgment target; acknowledge = mark read. */
export interface FeedbackReceipt {
  notification_id: string;
  /** Candidate id for receipt. Hidden on card; retained to match database row during diagnosis. */
  candidate_id: string;
  /**
   * Server-resolved target-asset display name.
   *
   * ★ Always nonempty: semantic/new_asset/retire kinds lacking display name fall back server-side
   * to target_ref (their ref already readable). Frontend needs no null check.
   */
  target_label: string;
  final_state: FeedbackCandidateState;
  decided_at: string;
  /** true = own proposal; false = received as cosigner. */
  mine: boolean;
}

/**
 * Current user's unread terminal receipts.
 *
 * Delivered through inline chat cards, not notification center: 116 measured 139 notifications, zero clicks, while eight
 * human decisions in same period all occurred on inline chat cards. This endpoint serves that card only.
 */
export function listFeedbackReceipts(): Promise<{ items: FeedbackReceipt[] }> {
  return apiJson("/api/v1/feedback-candidates/receipts");
}

export interface FeedbackRerouteResult {
  superseded_id: string;
  outcome: "created" | "merged";
  candidate_id: string;
  /** New candidate projected for caller; null if caller has no authorized lane on new target. */
  candidate: FeedbackCandidateRow | null;
}

// Reroute (batch 3b): move open candidate to another target; admin or agent-resource owner only, not cosigner alone.
export function rerouteFeedbackCandidate(id: string, target: { target_kind: FeedbackTargetKind; target_ref: string }): Promise<FeedbackRerouteResult> {
  return Promise.resolve(apiJson<FeedbackRerouteResult>(`/api/v1/feedback-candidates/${encodeURIComponent(id)}/reroute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(target),
  })).then((result) => {
    notifySettingsNavCountsChanged();
    return result;
  });
}

export function setFeedbackCaseworkEnabled(enabled: boolean): Promise<{ enabled: boolean }> {
  return apiJson("/api/v1/feedback-candidates/settings/casework", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function getFeedbackCandidate(id: string): Promise<FeedbackCandidateDetail> {
  return apiJson(`/api/v1/feedback-candidates/${encodeURIComponent(id)}`);
}

export function decideFeedbackCandidate(
  id: string,
  decision: "accepted" | "rejected",
  note = "",
): Promise<{ id: string; state: FeedbackCandidateState }> {
  return Promise.resolve(apiJson<{ id: string; state: FeedbackCandidateState }>(`/api/v1/feedback-candidates/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note }),
  })).then((result) => {
    notifySettingsNavCountsChanged();
    return result;
  });
}

export function adoptFeedbackCandidate(id: string): Promise<FeedbackAdoptResult> {
  return Promise.resolve(apiJson<FeedbackAdoptResult>(`/api/v1/feedback-candidates/${encodeURIComponent(id)}/adopt`, { method: "POST" })).then((result) => {
    notifySettingsNavCountsChanged();
    return result;
  });
}

// Rematerialize accepted candidate prefill against current baseline (tech-design §2.3 ⑤ “基于新基线重新对齐”); adopter only.
export function redraftFeedbackCandidate(id: string): Promise<FeedbackAdoptResult> {
  return apiJson(`/api/v1/feedback-candidates/${encodeURIComponent(id)}/prefill`, { method: "POST" });
}

export function withdrawFeedbackCandidate(id: string): Promise<{ id: string; state: FeedbackCandidateState }> {
  return apiJson(`/api/v1/feedback-candidates/${encodeURIComponent(id)}/withdraw`, { method: "POST" });
}

// Postadoption prefill handoff: one-time sessionStorage channel (like lib/composer-handoff.ts:
// delete on read, consumer validates agent_id to prevent stale key reaching another agent editor). Consumed by FE batch B.
export const FEEDBACK_PREFILL_KEY = "bowhead:feedback-prefill";

export interface FeedbackPrefillHandoff {
  candidate_id: string;
  target_kind: FeedbackTargetKind;
  target_ref: string;
  prefill: FeedbackPrefill;
  base: Record<string, string | number | null>;
  banner: FeedbackAdoptResult["banner"];
}

export function stashFeedbackPrefill(handoff: FeedbackPrefillHandoff): boolean {
  try {
    sessionStorage.setItem(FEEDBACK_PREFILL_KEY, JSON.stringify(handoff));
    return true;
  } catch {
    return false;
  }
}

// Deliberately unlike composer-handoff’s delete-on-read: accepted coach candidate irreversible,
// redraft endpoint not yet implemented; deleting at mount permanently loses draft after one refresh/load failure. Thus:
// matching-agent handoff **read only** (peek); editor clears explicitly on apply/discard;
// mismatching stale keys still deleted (prevent reaching other agent).
export function peekFeedbackPrefill(expectedTargetRef: string): FeedbackPrefillHandoff | null {
  try {
    const raw = sessionStorage.getItem(FEEDBACK_PREFILL_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as FeedbackPrefillHandoff | null;
    if (!parsed || typeof parsed !== "object" || parsed.target_ref !== expectedTargetRef || !parsed.prefill?.kind) {
      sessionStorage.removeItem(FEEDBACK_PREFILL_KEY);
      return null;
    }
    return parsed;
  } catch {
    try {
      sessionStorage.removeItem(FEEDBACK_PREFILL_KEY);
    } catch {
      // ignore
    }
    return null;
  }
}

export function clearFeedbackPrefill(): void {
  try {
    sessionStorage.removeItem(FEEDBACK_PREFILL_KEY);
  } catch {
    // ignore
  }
}
