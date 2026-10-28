
import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "@/lib/api-client";
import { AuthError } from "@/lib/auth-api";
import {
  ConfirmExpiredError,
  PresignExpiredError,
  UploadCutOffError,
  UploadHttpError,
  UploadNetworkError,
  UploadTerminalError,
} from "@/lib/upload-xhr";

export type KbFileErrorOperation =
  | "load"
  | "search"
  | "preview"
  | "download"
  | "delete"
  | "scope"
  | "bind"
  | "reparse"
  | "extract"
  | "upload";

export type KbFileErrorKey =
  | "errors.sign_in_again"
  | "errors.missing_space"
  | "errors.access_denied"
  | "errors.admin_required"
  | "errors.feature_unlicensed"
  | "errors.readonly"
  | "errors.file_not_found"
  | "errors.source_missing"
  | "errors.doc_type_not_found"
  | "errors.doc_type_not_bindable"
  | "errors.file_has_no_doc_type"
  | "errors.doc_type_extract_disabled"
  | "errors.file_retry_rate_limited"
  | "errors.space_daily_rate_limited"
  | "errors.file_too_large"
  | "errors.file_type_unsupported"
  | "errors.bundle_invalid"
  | "errors.bundle_too_large"
  | "errors.bundle_too_many_entries"
  | "errors.upload_pending_limit"
  | "errors.upload_expired"
  | "errors.upload_not_received"
  | "errors.upload_in_progress"
  | "errors.bundle_required"
  | "errors.scope_reason_required"
  | "errors.promotion_not_supported"
  | "errors.invalid_selection"
  | "errors.service_unavailable"
  | "errors.rate_limited"
  | "errors.network"
  | "errors.upload_cut_off"
  | "errors.invalid_request"
  | "errors.load_failed"
  | "errors.search_failed"
  | "errors.preview_operation_failed"
  | "errors.download_failed"
  | "errors.delete_operation_failed"
  | "errors.scope_change_failed"
  | "errors.bind_operation_failed"
  | "errors.reparse_failed"
  | "errors.extract_failed"
  | "errors.upload_operation_failed";

export const KB_FILE_ERROR_KEY: Readonly<Record<string, KbFileErrorKey>> = {
  auth_required: "errors.sign_in_again",
  invalid_token: "errors.sign_in_again",
  password_changed_relogin_required: "errors.sign_in_again",
  user_inactive: "errors.sign_in_again",
  user_not_active: "errors.sign_in_again",
  workspace_required: "errors.missing_space",
  missing_workspace_context: "errors.missing_space",
  workspace_access_denied: "errors.access_denied",
  workspace_role_unknown: "errors.access_denied",
  workspace_member_role_required: "errors.access_denied",
  resource_grant_denied: "errors.access_denied",
  kb_scope_access_denied: "errors.access_denied",
  kb_file_access_denied: "errors.access_denied",
  workspace_admin_required: "errors.admin_required",
  workspace_admin_or_creator_required: "errors.admin_required",
  owner_or_admin_required: "errors.admin_required",
  feature_unlicensed: "errors.feature_unlicensed",
  over_grace: "errors.readonly",
  suspended: "errors.readonly",
  needs_license: "errors.readonly",
  tamper: "errors.readonly",
  clock_tamper: "errors.readonly",
  kb_file_not_found: "errors.file_not_found",
  kb_artifact_not_found: "errors.file_not_found",
  kb_source_missing: "errors.source_missing",
  kb_file_not_in_knowledge_base: "errors.file_not_found",
  doc_type_not_found: "errors.doc_type_not_found",
  doc_type_not_bindable: "errors.doc_type_not_bindable",
  file_has_no_doc_type: "errors.file_has_no_doc_type",
  doc_type_extract_disabled: "errors.doc_type_extract_disabled",
  rate_limit_per_file: "errors.file_retry_rate_limited",
  rate_limit_per_ws_daily: "errors.space_daily_rate_limited",
  kb_file_too_large: "errors.file_too_large",
  file_too_large: "errors.file_too_large",
  kb_file_type_unsupported: "errors.file_type_unsupported",
  file_type_unsupported: "errors.file_type_unsupported",
  kb_bundle_invalid: "errors.bundle_invalid",
  kb_bundle_too_large: "errors.bundle_too_large",
  kb_bundle_uncompressed_too_large: "errors.bundle_too_large",
  kb_bundle_too_many_entries: "errors.bundle_too_many_entries",
  file_presign_pending_limit: "errors.upload_pending_limit",
  file_presign_expired: "errors.upload_expired",
  file_upload_not_received: "errors.upload_not_received",
  file_confirm_in_progress: "errors.upload_in_progress",
  file_upload_in_progress: "errors.upload_in_progress",
  file_upload_authorization_failed: "errors.upload_expired",
  file_upload_empty: "errors.invalid_request",
  invalid_file_upload_request: "errors.invalid_request",
  file_presign_rate_limited: "errors.rate_limited",
  file_presign_unavailable: "errors.service_unavailable",
  file_upload_write_failed: "errors.upload_operation_failed",
  kb_bundle_required: "errors.bundle_required",
  kb_scope_change_reason_required: "errors.scope_reason_required",
  kb_promotion_not_supported: "errors.promotion_not_supported",
  invalid_kb_bulk_request: "errors.invalid_selection",
  kb_scope_change_failed: "errors.scope_change_failed",
  kb_service_unavailable: "errors.service_unavailable",
  kb_doc_type_service_unavailable: "errors.service_unavailable",
  kb_not_ready: "errors.service_unavailable",
  per_row_extract_unavailable: "errors.service_unavailable",
  kb_backfill_failed: "errors.service_unavailable",
  rate_limited: "errors.rate_limited",
  rate_limit: "errors.rate_limited",
};

const OPERATION_FALLBACK: Readonly<
  Record<KbFileErrorOperation, KbFileErrorKey>
> = {
  load: "errors.load_failed",
  search: "errors.search_failed",
  preview: "errors.preview_operation_failed",
  download: "errors.download_failed",
  delete: "errors.delete_operation_failed",
  scope: "errors.scope_change_failed",
  bind: "errors.bind_operation_failed",
  reparse: "errors.reparse_failed",
  extract: "errors.extract_failed",
  upload: "errors.upload_operation_failed",
};

export function kbFileErrorFromCode(
  code: string | null | undefined,
  operation: KbFileErrorOperation,
): KbFileErrorKey {
  return (code ? KB_FILE_ERROR_KEY[code] : undefined)
    ?? OPERATION_FALLBACK[operation];
}

/**
 * Resolve KB failures from a finite code/status contract. Backend prose is
 * deliberately ignored so store, database, path, and model diagnostics cannot
 * reach the knowledge-base UI.
 */
export function kbFileError(
  error: unknown,
  operation: KbFileErrorOperation,
): KbFileErrorKey {
  // FR5.2: connection dropped before the request body finished sending (usually a size-based access-Gateway rejection), distinct from
  // ordinary network failure before sending. Decide from transfer progress (see upload-xhr.watchUploadTransfer), not guesses from file size.
  if (error instanceof UploadCutOffError) return "errors.upload_cut_off";
  if (
    error instanceof UploadNetworkError
    || isNetworkFailure(error)
  ) {
    return "errors.network";
  }
  if (
    error instanceof PresignExpiredError
    || error instanceof ConfirmExpiredError
  ) {
    return "errors.upload_expired";
  }

  let status: number;
  let code: string | undefined;
  if (error instanceof ApiError) {
    status = error.status;
    code = error.code;
  } else if (error instanceof AuthError) {
    status = error.status;
    code = error.reasonCode;
  } else if (error instanceof UploadHttpError) {
    status = error.status;
    code = error.code;
  } else if (error instanceof UploadTerminalError) {
    status = error.cause.status;
    code = error.cause.code;
  } else {
    return OPERATION_FALLBACK[operation];
  }

  const mapped = code ? KB_FILE_ERROR_KEY[code] : undefined;
  if (mapped) return mapped;
  // These producers also cover a failed read after a committed write.
  if (code === "kb_file_write_failed" || code === "kb_file_operation_failed") {
    return OPERATION_FALLBACK[operation];
  }

  if (status === 0) return "errors.network";
  if (status === 401) return "errors.sign_in_again";
  if (status === 403) return "errors.access_denied";
  if (status === 404) return "errors.file_not_found";
  if (status === 413) return "errors.file_too_large";
  if (status === 415) return "errors.file_type_unsupported";
  if (status === 429) return "errors.rate_limited";
  if (status === 400 || status === 422) {
    return "errors.invalid_request";
  }
  if (status >= 500) return "errors.service_unavailable";
  return OPERATION_FALLBACK[operation];
}
