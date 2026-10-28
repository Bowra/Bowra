import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "./api-client";

export type ModelSettingsErrorKey =
  | "network_error"
  | "err_gateway_blocked"
  | "err_generic"
  | "err_invalid_fields"
  | "err_sign_in_again"
  | "err_insufficient_role"
  | "err_readonly"
  | "err_config_unavailable"
  | "err_config_version_conflict"
  | "err_invalid_config"
  | "err_reindex_required"
  | "err_encryption_required"
  | "err_provider_not_configured"
  | "err_reindex_not_found"
  | "err_reindex_failed"
  | "err_provider_not_testable"
  | "err_provider_type_unsupported"
  | "err_provider_identity_conflict"
  | "err_discovery_failed"
  | "err_verification_stale"
  | "err_review_stale"
  | "err_readiness_blocked"
  | "err_security_policy"
  | "err_api_key_required";

export const MODEL_SETTINGS_ERROR_KEY: Readonly<
  Record<string, ModelSettingsErrorKey>
> = {
  auth_required: "err_sign_in_again",
  invalid_token: "err_sign_in_again",
  password_changed_relogin_required: "err_sign_in_again",
  user_inactive: "err_sign_in_again",
  user_not_active: "err_sign_in_again",
  team_membership_required: "err_sign_in_again",
  insufficient_role: "err_insufficient_role",
  over_grace: "err_readonly",
  suspended: "err_readonly",
  needs_license: "err_readonly",
  tamper: "err_readonly",
  clock_tamper: "err_readonly",
  entitlement_unavailable: "err_readonly",
  config_decrypt_failed: "err_config_unavailable",
  config_version_conflict: "err_config_version_conflict",
  invalid_llm_config: "err_invalid_config",
  embed_reindex_required: "err_reindex_required",
  llm_config_encryption_required: "err_encryption_required",
  provider_not_in_v2_config: "err_provider_not_configured",
  provider_not_configured: "err_provider_not_configured",
  reindex_task_not_found: "err_reindex_not_found",
  reindex_failed: "err_reindex_failed",
  provider_not_testable: "err_provider_not_testable",
  provider_type_unsupported: "err_provider_type_unsupported",
  provider_identity_conflict: "err_provider_identity_conflict",
  api_key_required: "err_api_key_required",
  blocked_by_security_policy: "err_security_policy",
  discovery_blocked_by_security_policy: "err_security_policy",
  discovery_response_too_large: "err_discovery_failed",
  discovery_timeout: "err_discovery_failed",
  discovery_auth_failed: "err_discovery_failed",
  discovery_endpoint_not_found: "err_discovery_failed",
  discovery_rate_limited: "err_discovery_failed",
  discovery_tls_error: "err_discovery_failed",
  discovery_dns_error: "err_discovery_failed",
  discovery_invalid_response: "err_discovery_failed",
  invalid_confirmation_token: "err_verification_stale",
  verification_plan_expired: "err_verification_stale",
  verification_plan_not_found: "err_verification_stale",
  verification_plan_changed: "err_verification_stale",
  readiness_review_required: "err_verification_stale",
  // A stale apply review is not a failed or expired connection test. Keeping
  // these separate tells an admin to recheck the saved draft rather than send
  // another potentially billable verification request.
  readiness_review_stale: "err_review_stale",
  readiness_blocked: "err_readiness_blocked",
  migration_acceptance_required: "err_readiness_blocked",
  risk_acceptance_required: "err_readiness_blocked",
};

export function modelSettingsErrorKey(error: unknown): ModelSettingsErrorKey {
  // Nonapplication responder (HTML error page) means intermediary blocked request, **never reached app**; prioritize before all
  // business classifications, whose fallbacks misdirect admin toward app instead of gateway rules.
  //
  // WAF rules can reject URLs in nested `config.providers.<name>.base_url` fields before the
  // request reaches the application. Report the gateway classification instead of a generic save failure.
  if (error instanceof ApiError && error.nonApiResponse) {
    return "err_gateway_blocked";
  }
  if (!(error instanceof ApiError)) {
    // 116 evidence 2026-08-14: cannot blanket-classify network_error; `settings/models.tsx`
    // called secure-context-only `crypto.randomUUID` over plain HTTP on 116, throwing
    // TypeError("crypto.randomUUID is not a function"), with **zero requests sent**,
    // yet screen showed “网络连接失败，请检查网络后重试”. Thus verification never succeeding
    // was disguised as user network trouble; three evidence tables stayed at 0 rows and model configuration never saved.
    // Report unrecognized exceptions honestly as err_generic (refresh/retry/contact admin wording),
    // at least avoiding wrong-direction guidance.
    return isNetworkFailure(error) ? "network_error" : "err_generic";
  }
  if (error.code && MODEL_SETTINGS_ERROR_KEY[error.code]) {
    return MODEL_SETTINGS_ERROR_KEY[error.code];
  }
  if (error.status === 422) return "err_invalid_fields";
  return "err_generic";
}

export type ModelSettingsValidationErrorKey =
  | "validation_invalid_schema"
  | "validation_blocked_url"
  | "validation_unknown_reference"
  | "validation_required"
  | "validation_unsupported"
  | "validation_invalid_value"
  | "validation_provider_identity_required"
  | "validation_invalid_config"
  | "validation_warning";

export const MODEL_SETTINGS_VALIDATION_ERROR_KEY: Readonly<
  Record<string, ModelSettingsValidationErrorKey>
> = {
  invalid_schema: "validation_invalid_schema",
  blocked_url: "validation_blocked_url",
  unknown_reference: "validation_unknown_reference",
  required: "validation_required",
  unsupported: "validation_unsupported",
  invalid_value: "validation_invalid_value",
  provider_identity_required: "validation_provider_identity_required",
  invalid_llm_config: "validation_invalid_config",
};

export function modelSettingsValidationErrorKey(
  code: string | undefined,
  severity: "error" | "warning" | undefined,
): ModelSettingsValidationErrorKey {
  if (code && MODEL_SETTINGS_VALIDATION_ERROR_KEY[code]) {
    return MODEL_SETTINGS_VALIDATION_ERROR_KEY[code];
  }
  return severity === "warning"
    ? "validation_warning"
    : "validation_invalid_config";
}
