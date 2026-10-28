/**
 * V3 admin credits API wrapper.
 *
 * Backend: /api/v1/admin/credits/*  (credit capability gate)
 * Design document: docs/V3-admin-credits-tech-design.md §2.2
 */

import { apiFetch, readApiError } from "./api-client";
import { appPath } from "./base-path";
import { randomUuid } from "./random-id";

export interface CreditTeamPool {
  balance: number;
  quota: number;
  period_start: string | null;
  last_reset_at: string | null;
}

export interface Credit30dStats {
  spend: number;
  turns: number;
  tokens_in: number;
  tokens_out: number;
}

export interface AttentionUser {
  user_id: string;
  email: string;
  balance_pct: number;
  growth_7d: number;
  flags: ("low_balance" | "growth_spike" | "new_user")[];
}

export interface TopUser {
  user_id: string;
  email: string;
  display_name: string;
  spend: number;
}

export interface TopWorkspace {
  workspace_id: string | null;
  spend: number;
}

export interface CreditActorCapabilities {
  credits_read: boolean;
  credit_grant: boolean;
  credit_user_quota_write: boolean;
  credit_user_tier_write: boolean;
  credit_period_reset: boolean;
  credit_team_quota_write: boolean;
  credit_attention_dismiss: boolean;
}

export interface CreditAttentionAction {
  key: string;
  label: string;
  allowed: boolean;
  disabled_reason: string | null;
  capability_required: string | null;
}

export interface CreditActivationAttentionItem {
  attention_item_id: string;
  team_id: string;
  user_id: string | null;
  workspace_id: string | null;
  policy_key: string;
  state: "open" | "dismissed" | "acted" | "protected" | "resolved" | "suppressed";
  severity: "info" | "warning" | "critical";
  severity_rank: number;
  primary_segment: string;
  segment_flags: string[];
  attention_reason?: string;
  evidence?: Record<string, unknown>;
  recommended_action_key: string;
  action_intent?: { key: string; label: string; reason?: string };
  recommended_action?: CreditAttentionAction;
  available_actions?: CreditAttentionAction[];
  email?: string | null;
  display_name?: string | null;
  tier?: string | null;
  opened_at: string;
  expires_at: string;
  updated_at: string;
  resolved_at: string | null;
}

export interface CreditAttentionActionClickBody {
  action_key: string;
  source_surface: "summary" | "users" | "billing" | "drilldown" | string;
  allowed: boolean;
  disabled_reason?: string | null;
}

export interface CreditSummary {
  team: CreditTeamPool;
  last_30d: Credit30dStats;
  active_users_7d: number;
  total_users_active: number;
  view_attention: AttentionUser[];
  view_top_users: TopUser[];
  view_top_workspaces: TopWorkspace[];
  activation_attention: CreditActivationAttentionItem[];
  credit_actor?: {
    team_role: string;
    capabilities: CreditActorCapabilities;
  };
}

export interface CreditUserRow {
  user_id: string;
  email: string;
  display_name: string | null;
  tier: "pro" | "ultra";
  status: "active" | "removed";
  credit_balance: number;
  credit_quota: number;
  credit_period_start: string | null;
  spend_30d: number;
  last_activity_at: string | null;
  is_team_admin: boolean;
  attention_score: number;
  activation_attention?: CreditActivationAttentionItem | null;
  primary_segment?: string | null;
  segment_flags?: string[];
  recommended_action_key?: string | null;
  recommended_action?: CreditAttentionAction | null;
}

export interface CreditUserListDTO {
  items: CreditUserRow[];
  total: number;
  page: number;
  size: number;
}

export interface LedgerEntry {
  id: string;
  type: "turn" | "admin_grant" | "admin_set_balance" | "period_reset" | string;
  workspace_id: string | null;
  turn_id_short: string | null;
  tokens_input: number;
  tokens_output: number;
  model_usage?: Array<{
    provider: string | null;
    model: string | null;
    tokens_input: number;
    tokens_cached: number;
    tokens_output: number;
    rate_input: string;
    rate_cached: string;
    rate_output: string;
    weighted_tokens: string;
  }>;
  delta: number;
  created_at: string;
}

export interface LedgerListDTO {
  items: LedgerEntry[];
  total: number;
  page: number;
  size: number;
}

export interface PatchUserBody {
  tier?: "pro" | "ultra";
  credit_quota?: number;
  credit_balance?: number;
  reset_quota_with_tier?: boolean;
  pull_balance_to_quota?: boolean;
}

export interface PeriodResetPreview {
  users: Array<{
    user_id: string; email: string;
    before: { tier: string; quota: number; balance: number };
    after: { tier: string; quota: number; balance: number };
  }>;
  custom_quota_overrides: Array<{
    user_id: string; email: string;
    current_quota: number; new_quota: number;
  }>;
  team_balance_before: number;
  team_balance_after: number;
  mode: "balance_only" | "balance_and_quota";
}

// ─── error class ─────────────────────────────────

export class AdminCreditsError extends Error {
  status: number;
  retryAfter?: number;
  code?: string;
  constructor(status: number, message: string, retryAfter?: number, code?: string) {
    super(message);
    this.name = "AdminCreditsError";
    this.status = status;
    this.retryAfter = retryAfter;
    this.code = code;
  }
}

async function _check(r: Response): Promise<Response> {
  if (r.ok) return r;
  const error = await readApiError(r);
  let retry: number | undefined;
  if (r.status === 429) {
    const ra = r.headers.get("retry-after");
    retry = ra ? parseInt(ra, 10) : undefined;
  }
  throw new AdminCreditsError(error.status, error.message, retry, error.code);
}

// ─── API wrappers ────────────────────────────────

export async function fetchCreditSummary(): Promise<CreditSummary> {
  const r = await apiFetch("/api/v1/admin/credits/summary");
  await _check(r);
  return r.json();
}

export async function fetchCreditUserInfo(userId: string): Promise<CreditUserRow> {
  const r = await apiFetch(
    `/api/v1/admin/credits/users/${encodeURIComponent(userId)}/info`,
  );
  await _check(r);
  return r.json();
}

export async function fetchCreditUsers(params: {
  page?: number; size?: number; sort?: string; q?: string;
  tier?: string; workspace?: string; include_removed?: boolean;
} = {}): Promise<CreditUserListDTO> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null) qs.set(k, String(v));
  }
  const r = await apiFetch(`/api/v1/admin/credits/users?${qs}`);
  await _check(r);
  return r.json();
}

export async function patchCreditUser(
  userId: string, body: PatchUserBody,
): Promise<{ ok: boolean; user: CreditUserRow }> {
  const r = await apiFetch(`/api/v1/admin/credits/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await _check(r);
  return r.json();
}

export async function grantCredit(
  userId: string, amount: number, reason?: string,
): Promise<{ ok: boolean; new_balance: number }> {
  const idempotency_key = randomUuid();
  const r = await apiFetch(`/api/v1/admin/credits/users/${encodeURIComponent(userId)}/grant`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount, reason, idempotency_key }),
  });
  await _check(r);
  return r.json();
}

export async function periodResetPreview(
  mode: "balance_only" | "balance_and_quota",
): Promise<{ ok: boolean; preview: PeriodResetPreview; affected_users: number }> {
  const r = await apiFetch("/api/v1/admin/credits/period-reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, confirm: false }),
  });
  await _check(r);
  return r.json();
}

export async function periodResetConfirm(
  mode: "balance_only" | "balance_and_quota",
): Promise<{ ok: boolean; affected_users: number; mode: string }> {
  const r = await apiFetch("/api/v1/admin/credits/period-reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, confirm: true }),
  });
  await _check(r);
  return r.json();
}

export async function updateCreditAttentionState(
  attentionItemId: string,
  body: {
    state: "dismissed";
    reason?: string;
  },
): Promise<{ ok: boolean; item: CreditActivationAttentionItem }> {
  const r = await apiFetch(
    `/api/v1/admin/credits/attention/${encodeURIComponent(attentionItemId)}/state`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  await _check(r);
  return r.json();
}

export async function trackCreditAttentionActionClicked(
  attentionItemId: string,
  body: CreditAttentionActionClickBody,
): Promise<{ ok: boolean }> {
  const r = await apiFetch(
    `/api/v1/admin/credits/attention/${encodeURIComponent(attentionItemId)}/action-clicked`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  await _check(r);
  return r.json();
}

export async function fetchUserLedger(
  userId: string,
  params: { page?: number; size?: number; from?: string; to?: string; type?: string } = {},
): Promise<LedgerListDTO> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null) qs.set(k === "from" ? "from_" : k, String(v));
  }
  const r = await apiFetch(
    `/api/v1/admin/credits/users/${encodeURIComponent(userId)}/ledger?${qs}`,
  );
  await _check(r);
  return r.json();
}

/**
 * CSV download URL (anchor href). Same-origin cookie GET triggers native browser download.
 */
export function csvDownloadUrl(
  scope: "summary" | "users" | "user_ledger",
  opts: { user_id?: string; from?: string; to?: string } = {},
): string {
  const qs = new URLSearchParams({ scope });
  if (opts.user_id) qs.set("user_id", opts.user_id);
  if (opts.from) qs.set("from_", opts.from);
  if (opts.to) qs.set("to", opts.to);
  return appPath(`/api/v1/admin/credits/export.csv?${qs}`);
}

// ─── tier_credits — admin_billing twins (the teams.py routes were retired, licensing §9) ──────

/** Read this team's credit/tier meta (caller-team-bound). Replaces GET /api/v1/teams/{id}. */
export async function fetchTeamCredits(): Promise<{
  team_id: string;
  tier_credits: Record<string, number>;
  contract_expires_at: string | null;
  team_credit_balance: number;
  team_credit_quota: number;
}> {
  const r = await apiFetch("/api/v1/admin/credits");
  await _check(r);
  return r.json();
}

/** Update per-tier default credits via the caller-team-bound twin (no team_id path param; audited). */
export async function updateTierCredits(
  tierCredits: { pro?: number; ultra?: number },
  reason = "tier-credits updated via admin console",
): Promise<{ ok: boolean; old_tier_credits: Record<string, number>; new_tier_credits: Record<string, number> }> {
  const r = await apiFetch("/api/v1/admin/team/tier-credits", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tier_credits: tierCredits, reason }),
  });
  await _check(r);
  return r.json();
}

export interface TeamCreditQuotaUpdate {
  ok: boolean;
  old_quota: number;
  new_quota: number;
  old_balance: number;
  new_balance: number;
}

/** Update this team's total credit quota (caller-team-bound and audited). */
export async function updateTeamCreditQuota(
  payload: { new_quota: number; reason: string },
): Promise<TeamCreditQuotaUpdate> {
  const r = await apiFetch("/api/v1/admin/credits/quota", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await _check(r);
  return r.json();
}
