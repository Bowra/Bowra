import { staticTranslator } from "./locale-messages";

/**
 * Stable HTTP error codes reachable from POST /api/v1/chat/send before a turn
 * starts. SSE reason codes have a separate lifecycle and remain in
 * error-messages.ts.
 */
export const CHAT_SEND_BACKEND_CODES = [
  "context_maintenance_unsupported",
  "context_maintenance_inputs_unsupported",
  "context_conversation_head_changed",
  "context_maintenance_session_busy",
  "context_maintenance_pending_operation",
  "document_not_found",
  "document_revision_conflict",
  "document_input_unavailable",
  "document_draft_unavailable",
  "document_revision_inputs_unsupported",
  "document_revision_unavailable",

  "reference_input_invalid",
  "reference_request_too_large",
  "reference_input_media_type_required",
  "unsupported_entry",
  "coach_mention_unsupported",
  "coach_mention_unavailable",
  "configuration_mention_unsupported",
  "configuration_mention_unavailable",
  "resource_unavailable",
  "plan_execution_claim_lost",
  "source_projection_unavailable",
  "attachment_bundle_mismatch",
  "attachment_metadata_stale",
  "attachment_unavailable",
  "attachment_validation_unavailable",
  "result_reference_unavailable",
  "chart_selection_unavailable",
  "auth_session_generation_inactive",
  "chat_action_not_found",
  "client_refresh_required",
  "subagent_draft_unsaved_changes",
  "coach_canvas_target_unavailable",
  "member_not_in_workspace",
  "datarc_identity_required",
  "datarc_unavailable",
  "datarc_member_not_found",
  "datarc_member_resolve_failed",
  "orchestrator_not_ready",
  "request_validation_failed",
  "message_too_long",
  "empty_message",
  "session_deleted",
  "authz_unavailable",
  "authz_lookup_failed",
  "ws_access_denied",
  "access_context_denied",
  "workspace_archived",
  "workspace_unavailable",
  "planning_unavailable",
  "plan_not_found",
  "plan_not_approvable",
  "user_credit_exhausted",
  "team_pool_exhausted",
  "plan_already_running",
  "replay_source_not_found",
  "steer_parent_not_active",
  "submission_abandoned",
  "submission_contract_inactive",
  "submission_fingerprint_unavailable",
  "submission_id_conflict",
  "submission_kind_unavailable",
  "submission_not_found",
  "submission_pending",
  "submission_rejected",
  "submission_store_unavailable",
  "team_model_unconfigured",
  "too_many_attachments",
  "turn_became_active",
  "turn_dispatch_unavailable",
  "turn_store_unavailable",
  "terminal_protocol_admission_rejected",
  "session_transitioning",
  "session_turn_active",
  "terminal_dependency_recovering",
  "terminal_protocol_future_version",
  "terminal_protocol_malformed",
  "terminal_protocol_writer_not_ready",
  "auth_required",
  "over_grace",
  "suspended",
  "needs_license",
  "tamper",
  "clock_tamper",
  "entitlement_unavailable",
] as const;

export type ChatSendBackendCode = (typeof CHAT_SEND_BACKEND_CODES)[number];

const CHAT_SEND_FALLBACK_CODES = [
  "chat_send_invalid_request",
  "chat_send_access_denied",
  "chat_send_resource_unavailable",
  "chat_send_conflict",
  "chat_send_rate_limited",
  "chat_send_entitlement_denied",
  "chat_send_service_unavailable",
  "send_failed",
] as const;

type ChatSendFallbackCode = (typeof CHAT_SEND_FALLBACK_CODES)[number];
export type ChatSendReasonCode = ChatSendBackendCode | ChatSendFallbackCode;

/** Every reason code has user-facing wording (messages chat_send_errors.reasons). */
export const CHAT_SEND_REASON_CODES: readonly ChatSendReasonCode[] = [
  ...CHAT_SEND_BACKEND_CODES,
  ...CHAT_SEND_FALLBACK_CODES,
];

// Wording lives in messages chat_send_errors namespace:
// · source_projection_unavailable: fail closed, never send without establishing which sources viewer can access (B1a).
// · team_model_unconfigured: baseline is role-neutral and states resource needs, not Ask an admin (misdirects admins themselves,
//   incorrectly denies audit_admin). Role branches: resolveModelUnconfiguredPresentation (credential-readiness D5 / Q9).
const REASON_CODE_SET = new Set<string>(CHAT_SEND_REASON_CODES);

const BACKEND_CODE_SET = new Set<string>(CHAT_SEND_BACKEND_CODES);

export function resolveChatSendErrorReason(
  code: string | null | undefined,
  status: number | null | undefined,
): ChatSendReasonCode {
  if (code && BACKEND_CODE_SET.has(code)) {
    return code as ChatSendBackendCode;
  }
  if (status === 402) return "chat_send_entitlement_denied";
  if (status === 400 || status === 422) return "chat_send_invalid_request";
  if (status === 401) return "auth_required";
  if (status === 403) return "chat_send_access_denied";
  if (status === 404 || status === 410) {
    return "chat_send_resource_unavailable";
  }
  if (status === 409) return "chat_send_conflict";
  if (status === 429) return "chat_send_rate_limited";
  if (status !== undefined && status !== null && status >= 500) {
    return "chat_send_service_unavailable";
  }
  return "send_failed";
}

export function getChatSendErrorMessage(
  reason: string | null | undefined,
): string {
  const t = staticTranslator("chat_send_errors.reasons");
  return t(reason && REASON_CODE_SET.has(reason) ? reason : "send_failed");
}

// ─── credential-readiness D5: role-aware team_model_unconfigured presentation ──────────
// Criterion is caller can_act on model configuration resource (PRD 6.0.3 ③), not merely being
// an administrator: audit_admin and member share unauthorized branch (6.0.4). Contact
// lists go only into UI presentation, never LLM prose (self-review F6).

export interface ModelUnconfiguredPresentation {
  message: string;
  /** can_act=true: presentation should offer appPath("/team/settings/models/") direct entry. */
  canAct: boolean;
}

let _contactsCache: { names: string[]; cachedAtMs: number } | null = null;
const _CONTACTS_CACHE_TTL_MS = 60_000;

async function _fetchModelConfigContacts(): Promise<string[]> {
  const now = Date.now();
  if (_contactsCache && now - _contactsCache.cachedAtMs < _CONTACTS_CACHE_TTL_MS) {
    return _contactsCache.names;
  }
  const { fetchWithRefresh } = await import("./auth-api");
  const resp = await fetchWithRefresh("/api/v1/team/capability-contacts");
  if (!resp.ok) throw new Error(`capability-contacts: ${resp.status}`);
  const data = (await resp.json()) as { contacts?: { model_config?: string[] } };
  const names = (data.contacts?.model_config ?? []).filter(Boolean);
  _contactsCache = { names, cachedAtMs: now };
  return names;
}

/**
 * Role-aware presentation. Any query failure falls back to role-neutral baseline (never throws);
 * unavailable-resource guidance must never acquire another error layer itself.
 */
export async function resolveModelUnconfiguredPresentation(): Promise<ModelUnconfiguredPresentation> {
  const t = staticTranslator("chat_send_errors.model_unconfigured");
  const fallback: ModelUnconfiguredPresentation = {
    message: getChatSendErrorMessage("team_model_unconfigured"),
    canAct: false,
  };
  try {
    const [{ fetchMe }, { MODEL_ADMIN_ROLES, teamRoleForUser }] = await Promise.all([
      import("./auth-api"),
      import("./settings-navigation"),
    ]);
    const me = await fetchMe();
    const role = teamRoleForUser(me) ?? "member";
    if ((MODEL_ADMIN_ROLES as readonly string[]).includes(role)) {
      return {
        message: t("admin"),
        canAct: true,
      };
    }
    let names: string[] = [];
    try {
      names = await _fetchModelConfigContacts();
    } catch {
      names = []; // Missing contacts does not affect main wording.
    }
    const contacts = names.length
      ? t("contacts", { names: names.join(t("contacts_separator")) })
      : "";
    return {
      message: t("member", { contacts }),
      canAct: false,
    };
  } catch {
    return fallback;
  }
}
