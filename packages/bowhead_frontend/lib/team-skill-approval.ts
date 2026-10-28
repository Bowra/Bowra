/**
 * skill-team-governance P3 — drawer team-status derivation (pure function).
 *
 * Input = same-named team row for personal skill (author view includes approval/candidate details) + personal bundle fingerprint
 * (SkillDetailDTO.content_sha256, same hash as team CAS). Mutually exclusive states/CTAs follow PRD §3.6 B:
 * unsubmitted→Submit to team; under review→Withdraw; rejected→Resubmit (reason on hover);
 * synced→no button; team version behind→Submit update; update under review→Withdraw; update rejected→Resubmit.
 */

import type { TeamSkillPublic } from "./workflow-solidify-api";

export type TeamSyncState =
  | "none" // No team row / withdrawn private state → Submit to team.
  | "name_taken" // Same-named team row belongs to someone else → forbid submission, suggest collaboration/renaming.
  | "pending" // L1 approval pending → Withdraw.
  | "rejected" // L1 rejected → Resubmit.
  | "update_pending" // L2 candidate under review → Withdraw.
  | "update_rejected" // L2 candidate rejected → Resubmit.
  | "synced" // Approved and content matches.
  | "behind"; // Approved but personal version ahead → Submit update.

export interface TeamSyncStatus {
  state: TeamSyncState;
  row?: TeamSkillPublic;
  /** Complete rejected / update_rejected reason (hover/display). */
  reason?: string;
}

export function deriveTeamSyncStatus(
  row: TeamSkillPublic | undefined | null,
  personalSha: string | null | undefined,
  meId: string | null | undefined,
): TeamSyncStatus {
  if (!row) return { state: "none" };
  if (row.created_by && meId && row.created_by !== meId) return { state: "name_taken", row };
  if ((row.visibility || "team_wide") === "creator_only") {
    // Private state reclaimed after withdrawal/rejection equals unsubmitted for submission flow (promote = L1 submission leg).
    return { state: "none", row };
  }
  const approval = row.approval || "approved";
  if (approval === "pending") return { state: "pending", row };
  if (approval === "rejected") return { state: "rejected", row, reason: row.rejected_reason || "" };
  if (row.candidate_state === "pending") return { state: "update_pending", row };
  if (row.candidate_state === "rejected") {
    return { state: "update_rejected", row, reason: row.candidate_rejected_reason || "" };
  }
  if (personalSha && row.approved_sha256 && personalSha !== row.approved_sha256) {
    return { state: "behind", row };
  }
  return { state: "synced", row };
}
