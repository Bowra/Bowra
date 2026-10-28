/**
 * Readiness → individually actionable verification evidence.
 *
 * Design: `就绪面板-第2轮-逐项验证` (2a). Intentionally React-free:
 * what needs attention and how to order it are **criteria**, which must be independently testable.
 *
 * ★ Product premise here: **verification is optional, not a gate**. Customer model deployment location is unknown;
 * intranets/self-hosted gateways often cannot verify—that does not mean invalid configuration. Answer only how many actions are available,
 * never whether the user can use it.
 */

import type { LlmReadiness, LlmReadinessRow } from "@/lib/llm-config-api";

/** Four terminal evidence states + one ongoing state. */
export type EvidenceState = "verified" | "aged" | "never" | "failed" | "running";

export interface EvidenceItem {
  /** Stable identity: same provider/model/capability must retain the same key across fetches. */
  key: string;
  providerId: string;
  providerName: string;
  modelId: string;
  capability: string;
  roleKey: string;
  state: EvidenceState;
  observedAt: string | null;
}

/** Ongoing attempts override historical facts—never keep showing 14 days ago while verifying. */
const RUNNING_ATTEMPTS = new Set(["queued", "verifying"]);

const STATE_BY_FACT: Record<string, EvidenceState> = {
  verified: "verified",
  aged: "aged",
  stale: "aged",
  failed: "failed",
  not_testable: "never",
  unknown: "never",
};

/** Order: failed, never verified, aged evidence, then verified. Stable role-name ordering within each tier. */
const STATE_ORDER: Record<EvidenceState, number> = {
  running: 0,
  failed: 1,
  never: 2,
  aged: 3,
  verified: 4,
};

export function evidenceItemState(row: LlmReadinessRow): EvidenceState {
  if (row.attempt_state && RUNNING_ATTEMPTS.has(row.attempt_state)) return "running";
  return STATE_BY_FACT[row.fact_state] ?? "never";
}

export function evidenceItemKey(row: {
  provider_id: string;
  model_id: string;
  capability: string;
}): string {
  return `${row.provider_id} ${row.model_id} ${row.capability}`;
}

export function evidenceItems(readiness: LlmReadiness | null): EvidenceItem[] {
  if (!readiness) return [];
  return readiness.rows
    .map((row) => ({
      key: evidenceItemKey(row),
      providerId: row.provider_id,
      providerName: row.provider_display_name || row.provider_id,
      modelId: row.model_id,
      capability: row.capability,
      roleKey: row.role_key,
      state: evidenceItemState(row),
      observedAt: row.observed_at ?? null,
    }))
    .sort((left, right) =>
      STATE_ORDER[left.state] - STATE_ORDER[right.state]
      || left.roleKey.localeCompare(right.roleKey)
      || left.modelId.localeCompare(right.modelId));
}

/**
 * Items needing attention (exclude verified and running).
 *
 * Idle single-line hint appears only above 0—no permanent status bar when nothing needs action
 * (original models-page prohibition: no permanent current-effective overview table).
 */
export function attentionCount(items: readonly EvidenceItem[]): number {
  return items.filter(
    (item) => item.state === "aged" || item.state === "never" || item.state === "failed",
  ).length;
}

/** Grouped counts for single-line hints (e.g. 1 awaiting confirmation, 11 with older evidence). */
export function attentionBreakdown(items: readonly EvidenceItem[]): {
  needsConfirm: number;
  aged: number;
  failed: number;
} {
  return {
    needsConfirm: items.filter((item) => item.state === "never").length,
    aged: items.filter((item) => item.state === "aged").length,
    failed: items.filter((item) => item.state === "failed").length,
  };
}
