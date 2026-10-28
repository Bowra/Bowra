
import { isNetworkFailure } from "@/lib/transport-error";
import { MatrixApiError } from "./api-error";

export type KbTeamTemplateErrorOperation =
  | "load"
  | "publish"
  | "revoke"
  | "copy";

export type KbTeamTemplateErrorKey =
  | "otpl_gone"
  | "otpl_viewer_tip"
  | "otpl_error"
  | "err_not_publishable"
  | "err_empty_template"
  | "err_schema_too_large"
  | "err_copy_into_source"
  | "err_invalid_template_schema"
  | "err_team_template_manage_required"
  | "err_team_template_sign_in_again"
  | "err_team_template_access_denied"
  | "err_team_template_readonly"
  | "err_team_template_feature_unavailable"
  | "err_team_template_service_unavailable"
  | "err_copy_refresh_required"
  | "err_team_template_rate_limited"
  | "err_team_template_network"
  | "err_publish_failed"
  | "err_unpublish_failed"
  | "err_copy_failed";

export const KB_TEAM_TEMPLATE_ERROR_KEY: Readonly<
  Record<string, KbTeamTemplateErrorKey>
> = {
  team_template_not_found: "otpl_gone",
  not_publishable: "err_not_publishable",
  empty_template: "err_empty_template",
  schema_too_large: "err_schema_too_large",
  invalid_template_schema: "err_invalid_template_schema",
  copy_into_source_space: "err_copy_into_source",
  workspace_admin_or_creator_required: "err_team_template_manage_required",
  owner_or_admin_required: "err_team_template_manage_required",
  revoke_not_allowed: "err_team_template_manage_required",
  workspace_member_role_required: "otpl_viewer_tip",
  workspace_access_denied: "err_team_template_access_denied",
  workspace_role_unknown: "err_team_template_access_denied",
  workspace_required: "err_team_template_access_denied",
  auth_required: "err_team_template_sign_in_again",
  invalid_token: "err_team_template_sign_in_again",
  password_changed_relogin_required: "err_team_template_sign_in_again",
  user_inactive: "err_team_template_sign_in_again",
  user_not_active: "err_team_template_sign_in_again",
  over_grace: "err_team_template_readonly",
  suspended: "err_team_template_readonly",
  needs_license: "err_team_template_readonly",
  tamper: "err_team_template_readonly",
  clock_tamper: "err_team_template_readonly",
  entitlement_unavailable: "err_team_template_feature_unavailable",
  feature_unlicensed: "err_team_template_feature_unavailable",
  feature_disabled: "err_team_template_feature_unavailable",
  team_template_service_unavailable:
    "err_team_template_service_unavailable",
  team_template_copy_refresh_required: "err_copy_refresh_required",
  access_control_unavailable: "err_team_template_service_unavailable",
  authz_unavailable: "err_team_template_service_unavailable",
  rate_limited: "err_team_template_rate_limited",
  rate_limit: "err_team_template_rate_limited",
};

const OPERATION_FALLBACK: Readonly<
  Record<KbTeamTemplateErrorOperation, KbTeamTemplateErrorKey>
> = {
  load: "otpl_error",
  publish: "err_publish_failed",
  revoke: "err_unpublish_failed",
  copy: "err_copy_failed",
};

/**
 * Resolve team-template failures from a finite code/status contract. Backend
 * prose is intentionally ignored so validation, database, and URL details
 * cannot be rendered in a toast.
 */
export function kbTeamTemplateError(
  error: unknown,
  operation: KbTeamTemplateErrorOperation,
): KbTeamTemplateErrorKey {
  if (!(error instanceof MatrixApiError)) {
    return isNetworkFailure(error) ? "err_team_template_network" : OPERATION_FALLBACK[operation];
  }

  const key = KB_TEAM_TEMPLATE_ERROR_KEY[error.code];
  if (key) return key;

  if (error.status === 401) return "err_team_template_sign_in_again";
  if (error.status === 403) return "err_team_template_access_denied";
  if (error.status === 404) return "otpl_gone";
  if (error.status === 429) return "err_team_template_rate_limited";
  if (error.status >= 500) {
    return "err_team_template_service_unavailable";
  }
  return OPERATION_FALLBACK[operation];
}
