
import { isNetworkFailure } from "@/lib/transport-error";
import { MatrixApiError } from "./api-error";

export type KbMatrixRunErrorKey =
  | "err_insufficient_credits"
  | "err_quota"
  | "err_rate_limited"
  | "err_matrix_cap"
  | "err_ws_cap"
  | "err_no_pending"
  | "err_not_cancellable"
  | "err_run_invalid_scope"
  | "err_run_service_unavailable"
  | "err_run_target_changed"
  | "err_run_access_unavailable"
  | "err_run_space_required"
  | "err_run_access_denied"
  | "err_run_member_required"
  | "err_run_cancel_forbidden"
  | "err_run_sign_in_again"
  | "err_run_readonly"
  | "err_run_feature_unavailable"
  | "err_run_invalid_request"
  | "err_run_network"
  | "fail_source_unavailable"
  | "err_run_generic";

export const KB_MATRIX_RUN_ERROR_KEY: Readonly<Record<string, KbMatrixRunErrorKey>> = {
  insufficient_credits: "err_insufficient_credits",
  kb_quota_exceeded: "err_quota",
  rate_limited: "err_rate_limited",
  matrix_active_runs_cap: "err_matrix_cap",
  ws_active_runs_cap: "err_ws_cap",
  no_pending_cells: "err_no_pending",
  source_unavailable: "fail_source_unavailable",
  run_not_cancellable: "err_not_cancellable",
  invalid_matrix_run_scope: "err_run_invalid_scope",
  matrix_service_unavailable: "err_run_service_unavailable",
  matrix_not_found: "err_run_target_changed",
  matrix_column_not_found: "err_run_target_changed",
  matrix_file_not_found: "err_run_target_changed",
  matrix_run_not_found: "err_run_target_changed",
  access_control_unavailable: "err_run_access_unavailable",
  authz_unavailable: "err_run_access_unavailable",
  workspace_required: "err_run_space_required",
  workspace_access_denied: "err_run_access_denied",
  workspace_role_unknown: "err_run_access_denied",
  workspace_member_role_required: "err_run_member_required",
  owner_or_admin_required: "err_run_cancel_forbidden",
  auth_required: "err_run_sign_in_again",
  invalid_token: "err_run_sign_in_again",
  password_changed_relogin_required: "err_run_sign_in_again",
  user_inactive: "err_run_sign_in_again",
  user_not_active: "err_run_sign_in_again",
  over_grace: "err_run_readonly",
  suspended: "err_run_readonly",
  needs_license: "err_run_readonly",
  tamper: "err_run_readonly",
  clock_tamper: "err_run_readonly",
  entitlement_unavailable: "err_run_feature_unavailable",
  feature_unlicensed: "err_run_feature_unavailable",
};

/** Map run/cancel failures without ever exposing backend prose, codes, or HTTP status. */
export function kbMatrixRunErrorKey(error: unknown): KbMatrixRunErrorKey {
  if (!(error instanceof MatrixApiError)) return isNetworkFailure(error) ? "err_run_network" : "err_run_generic";

  const code = error.code;
  if (KB_MATRIX_RUN_ERROR_KEY[code]) return KB_MATRIX_RUN_ERROR_KEY[code];
  if (code.startsWith("ws_access_denied:")) return "err_run_access_denied";

  if (error.status === 401) return "err_run_sign_in_again";
  if (error.status === 403) return "err_run_access_denied";
  if (error.status === 404) return "err_run_target_changed";
  if (error.status === 422) return "err_run_invalid_request";
  if (error.status === 429) return "err_rate_limited";
  if (error.status >= 500) return "err_run_service_unavailable";
  return "err_run_generic";
}
