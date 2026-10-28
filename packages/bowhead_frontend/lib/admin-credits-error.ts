
import { isNetworkFailure } from "@/lib/transport-error";
import { AdminCreditsError } from "./admin-credits-api";

export type AdminCreditsErrorKey =
  | "err_network"
  | "err_generic"
  | "err_invalid_fields"
  | "err_member_not_found"
  | "err_user_inactive"
  | "err_changes_required"
  | "err_team_pool_insufficient"
  | "err_period_reset_cooldown"
  | "err_attention_not_found"
  | "err_attention_reason_required"
  | "err_user_id_required"
  | "err_insufficient_capability"
  | "err_team_not_found"
  | "err_sign_in_again"
  | "err_over_grace"
  | "err_suspended"
  | "err_needs_license"
  | "err_license_invalid"
  | "err_clock_invalid"
  | "err_entitlement_unavailable"
  | "err_feature_unlicensed"
  | "err_managed_by_license";

export const ADMIN_CREDITS_ERROR_KEY: Readonly<
  Record<string, AdminCreditsErrorKey>
> = {
  member_not_found: "err_member_not_found",
  credit_user_inactive: "err_user_inactive",
  credit_changes_required: "err_changes_required",
  credit_team_pool_insufficient: "err_team_pool_insufficient",
  credit_period_reset_cooldown: "err_period_reset_cooldown",
  credit_attention_not_found: "err_attention_not_found",
  credit_attention_reason_required: "err_attention_reason_required",
  credit_user_id_required: "err_user_id_required",
  invalid_credit_pagination: "err_invalid_fields",
  insufficient_credit_capability: "err_insufficient_capability",
  team_not_found: "err_team_not_found",
  insufficient_role: "err_insufficient_capability",
  auth_required: "err_sign_in_again",
  invalid_token: "err_sign_in_again",
  password_changed_relogin_required: "err_sign_in_again",
  user_not_active: "err_sign_in_again",
  user_inactive: "err_sign_in_again",
  over_grace: "err_over_grace",
  suspended: "err_suspended",
  needs_license: "err_needs_license",
  tamper: "err_license_invalid",
  clock_tamper: "err_clock_invalid",
  entitlement_unavailable: "err_entitlement_unavailable",
  feature_unlicensed: "err_feature_unlicensed",
  managed_by_license: "err_managed_by_license",
};

export function adminCreditsErrorKey(error: unknown): AdminCreditsErrorKey {
  if (!(error instanceof AdminCreditsError)) return isNetworkFailure(error) ? "err_network" : "err_generic";
  if (error.code && ADMIN_CREDITS_ERROR_KEY[error.code]) {
    return ADMIN_CREDITS_ERROR_KEY[error.code];
  }
  if (error.status === 401) return "err_sign_in_again";
  if (error.status === 422) return "err_invalid_fields";
  return "err_generic";
}
