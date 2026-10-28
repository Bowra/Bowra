/** Configuration Copilot draft API — POST /api/v1/config-drafts (batch 0, stateless one-shot).
 *
 * Contract = backend DraftResult asdict (test_config_drafts_routes locks the shape):
 * status ok|clarification|fallback + fields + field_meta + issues + clarification + secret_redacted.
 * 409 team_model_unconfigured = Copilot unavailable (keep panel, disabled with explanation), 429 = rate limit, 422 = injection rejected—caller branches.
 */

import { apiJson } from "@/lib/api-client";

export interface ConfigDraftFieldMeta {
  origin: "generated" | "server";
  needs_confirm: boolean;
  note: string | null;
}

export interface ConfigDraftIssue {
  field: string;
  code: string;
  message: string;
  repairable?: boolean;
}

export interface ConfigDraftResult {
  status: "ok" | "clarification" | "fallback";
  fields: Record<string, unknown>;
  field_meta: Record<string, ConfigDraftFieldMeta>;
  issues: ConfigDraftIssue[];
  clarification: string | null;
  secret_redacted: boolean;
  form_id: string;
  /** Batch 1-d: extracted raw secret values (direct-fill bypass). Available only for opted-in forms + include_secrets. */
  secrets?: { value: string; label: string | null; kind: string }[];
}

export async function createConfigDraft(payload: {
  form_id: string;
  text: string;
  pasted_material?: string;
  current_values?: Record<string, unknown>;
  /** Batch 1-d: explicitly request extracted raw secret values (server still requires form opt-in before returning them). */
  include_secrets?: boolean;
}): Promise<ConfigDraftResult> {
  return apiJson<ConfigDraftResult>("/api/v1/config-drafts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
