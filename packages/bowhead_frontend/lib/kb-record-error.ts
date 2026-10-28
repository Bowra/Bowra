
import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "@/lib/api-client";
import { MatrixApiError } from "@/lib/kb-matrix/api-error";

export type KbRecordErrorOperation = "load" | "edit" | "status";

export type KbRecordErrorKey =
  | "errors.sign_in_again"
  | "errors.access_denied"
  | "errors.readonly"
  | "errors.not_found"
  | "errors.invalid_value"
  | "errors.service_unavailable"
  | "errors.network"
  | "errors.load_failed"
  | "errors.edit_failed"
  | "errors.status_failed";

const KB_RECORD_ERROR_KEY: Readonly<Record<string, KbRecordErrorKey>> = {
  auth_required: "errors.sign_in_again",
  invalid_token: "errors.sign_in_again",
  password_changed_relogin_required: "errors.sign_in_again",
  user_inactive: "errors.sign_in_again",
  user_not_active: "errors.sign_in_again",
  workspace_access_denied: "errors.access_denied",
  workspace_role_unknown: "errors.access_denied",
  workspace_member_role_required: "errors.access_denied",
  resource_grant_denied: "errors.access_denied",
  over_grace: "errors.readonly",
  suspended: "errors.readonly",
  needs_license: "errors.readonly",
  tamper: "errors.readonly",
  clock_tamper: "errors.readonly",
  kb_record_not_found: "errors.not_found",
  invalid_kb_record_value: "errors.invalid_value",
  kb_record_service_unavailable: "errors.service_unavailable",
};

const OPERATION_FALLBACK: Readonly<
  Record<KbRecordErrorOperation, KbRecordErrorKey>
> = {
  load: "errors.load_failed",
  edit: "errors.edit_failed",
  status: "errors.status_failed",
};

export function kbRecordError(
  error: unknown,
  operation: KbRecordErrorOperation,
): KbRecordErrorKey {
  if (!(error instanceof ApiError || error instanceof MatrixApiError)) {
    return isNetworkFailure(error) ? "errors.network" : OPERATION_FALLBACK[operation];
  }
  const mapped = error.code ? KB_RECORD_ERROR_KEY[error.code] : undefined;
  if (mapped) return mapped;

  if (error.status === 401) return "errors.sign_in_again";
  if (error.status === 403) return "errors.access_denied";
  if (error.status === 404) return "errors.not_found";
  if (error.status === 400 || error.status === 422) {
    return "errors.invalid_value";
  }
  if (error.status >= 500) return "errors.service_unavailable";
  return OPERATION_FALLBACK[operation];
}
