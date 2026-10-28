// skill-import batch 4: personal skill coach revision (B-track agent-feedback-loop batch 3d endpoint).
//
// Contract: docs/sprints/skill-import-adaptation-2026-09-08/interface-requests.md §1b/§1c,
// **Server `routes/feedback_coach.py` authoritative** (review caught me recognizing four documented degradations; actually seven).
// Synchronously returns revised **entire SKILL.md**, no draft object; page compares, save uses existing `PUT /skills/{name}/body`.

import { apiJson } from "@/lib/api-client";
import { sha256Hex } from "@/lib/sha256-fallback";

/** Seven reasons coach gives no usable revision, **copied individually from server exits**; each needs its own sentence. */
export type SkillCoachDegradeReason =
  | "surface_too_large" // Body exceeds whole-rewrite size limit.
  | "url_guard" // Output contains outbound address absent from baseline, blocked.
  | "coach_unavailable" // Timeout/parse failure/all upstreams failed fallback: **most frequent reason**.
  | "coach_rerouted" // Coach considers another body the appropriate revision target.
  | "name_changed" // frontmatter name changed (save rejects entire request).
  | "invented_tool" // allowed-tools includes names absent from platform.
  | "no_change"; // Substantially identical to current body.

export interface SkillCoachBase {
  body_sha256: string;
  import_status?: string | null;
}

interface SkillCoachCommon {
  base: SkillCoachBase;
  /** Server-supplied verbatim banner authoritative, preventing wording drift between sides. */
  banner?: { text?: string } | null;
}

export interface SkillCoachDraftOk extends SkillCoachCommon {
  degraded?: false;
  /** `rationale` is coach’s three-part explanation; server `report` has **no** notes. */
  prefill: { kind: "user_skill_body"; edited_content: string; rationale?: string | null };
  report: {
    red_count?: number;
    yellow_count?: number;
    /** Inventory report truncated ⇒ list of addressed findings itself incomplete too. */
    truncated?: boolean;
    findings_addressed: string[];
  };
}

export interface SkillCoachDraftDegraded extends SkillCoachCommon {
  degraded: true;
  // `& {}` preserves literal suggestions: `| string` collapses entire union to string, losing call-site spelling checks.
  degrade_reason: SkillCoachDegradeReason | (string & {});
  prefill?: null;
  rationale?: string | null;
  /** `invented_tool` includes invented names. */
  invented?: string[];
}

export type SkillCoachDraft = SkillCoachDraftOk | SkillCoachDraftDegraded;

export function isDegraded(d: SkillCoachDraft): d is SkillCoachDraftDegraded {
  return d.degraded === true;
}

// `Record`, not `Set`: missing entry becomes **type error**, not runtime generic fallback
// (review observed removing coach_unavailable from Set left all 23 tests and tsc green, while most common degradation
// silently became generic wording, losing key information “这次不算用掉今天的次数”).
const KNOWN_REASONS: Record<SkillCoachDegradeReason, true> = {
  surface_too_large: true,
  url_guard: true,
  coach_unavailable: true,
  coach_rerouted: true,
  name_changed: true,
  invented_tool: true,
  no_change: true,
};

/** Unknown reason (new B-track exit later) gets generic sentence, never i18n key shown to user. */
export function degradeMessageKey(reason: string): string {
  return reason in KNOWN_REASONS ? `degraded_${reason}` : "degraded_unknown";
}

/** Body fingerprint: matches server sha256_text, detecting baseline changes since drafting.
 *
 * ★ Use existing sha256Hex (pure-JS fallback included), never direct crypto.subtle: only available in **secure contexts**,
 * whereas operator-configured local deployments can use plain HTTP.
 * A direct call would prevent fingerprint validation in those supported contexts.
 *
 * Normalize CRLF first: server disk read uses Python universal newlines (already LF), defensive second layer here,
 * so even if upstream someday switches to byte reads, newline differences never falsely report baseline drift.
 */
export async function bodySha256(text: string): Promise<string> {
  return sha256Hex(new TextEncoder().encode(text.replace(/\r\n/g, "\n")));
}

export async function requestSkillCoachDraft(name: string): Promise<SkillCoachDraft> {
  return apiJson<SkillCoachDraft>(`/api/v1/feedback-coach/user-skills/${encodeURIComponent(name)}/draft`, {
    method: "POST",
  });
}
