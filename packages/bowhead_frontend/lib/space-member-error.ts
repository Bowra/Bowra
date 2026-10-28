import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "./api-client";

export type SpaceMemberErrorKey =
  | "network_error"
  | "err_generic"
  | "err_member_service_unavailable"
  | "err_not_workspace_member"
  | "err_workspace_team_mismatch"
  | "err_direct_create_required"
  | "err_recipient_unavailable"
  | "err_team_not_found"
  | "err_seat_full"
  | "err_invite_not_found"
  | "err_invite_revoked"
  | "err_invite_consumed"
  | "err_invite_satisfied"
  | "err_invite_expired"
  | "err_invite_not_reissuable"
  | "err_workspace_member_not_found"
  | "err_last_workspace_admin"
  | "err_team_membership_required"
  | "err_account_unavailable"
  | "err_sign_in_again"
  | "err_email_not_verified"
  | "err_public_entry_mismatch"
  | "err_insufficient_role"
  | "err_over_grace"
  | "err_suspended"
  | "err_needs_license"
  | "err_license_invalid"
  | "err_clock_invalid"
  | "err_entitlement_unavailable"
  | "err_invalid_invite_fields"
  | "err_invalid_role";

export const SPACE_MEMBER_ERROR_KEY: Readonly<Record<string, SpaceMemberErrorKey>> = {
  member_service_unavailable: "err_member_service_unavailable",
  not_workspace_member: "err_not_workspace_member",
  workspace_team_mismatch: "err_workspace_team_mismatch",
  feature_disabled_use_direct_create: "err_direct_create_required",
  user_not_found: "err_recipient_unavailable",
  team_not_found: "err_team_not_found",
  seat_full: "err_seat_full",
  invite_not_found: "err_invite_not_found",
  invite_status_revoked: "err_invite_revoked",
  invite_status_consumed: "err_invite_consumed",
  invite_status_satisfied: "err_invite_satisfied",
  invite_status_expired: "err_invite_expired",
  invite_not_reissuable: "err_invite_not_reissuable",
  workspace_member_not_found: "err_workspace_member_not_found",
  last_workspace_admin: "err_last_workspace_admin",
  team_membership_required: "err_team_membership_required",
  user_not_active: "err_account_unavailable",
  user_inactive: "err_account_unavailable",
  auth_required: "err_sign_in_again",
  invalid_token: "err_sign_in_again",
  password_changed_relogin_required: "err_sign_in_again",
  email_not_verified: "err_email_not_verified",
  public_entry_team_mismatch: "err_public_entry_mismatch",
  insufficient_role: "err_insufficient_role",
  over_grace: "err_over_grace",
  suspended: "err_suspended",
  needs_license: "err_needs_license",
  tamper: "err_license_invalid",
  clock_tamper: "err_clock_invalid",
  entitlement_unavailable: "err_entitlement_unavailable",
};

export function spaceMemberErrorKey(
  error: unknown,
  validationKey?: SpaceMemberErrorKey,
): SpaceMemberErrorKey {
  if (!(error instanceof ApiError)) return isNetworkFailure(error) ? "network_error" : "err_generic";
  if (error.code && SPACE_MEMBER_ERROR_KEY[error.code]) {
    return SPACE_MEMBER_ERROR_KEY[error.code];
  }
  if (error.status === 422 && validationKey) return validationKey;
  return "err_generic";
}
