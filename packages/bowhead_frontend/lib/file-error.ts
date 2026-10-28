
import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "@/lib/api-client";
import {
  ConfirmExpiredError,
  PresignExpiredError,
  UploadHttpError,
  UploadNetworkError,
  UploadTerminalError,
} from "@/lib/upload-xhr";

export type FileErrorOperation =
  | "load"
  | "upload"
  | "delete"
  | "restore"
  | "purge"
  | "preview"
  | "category"
  | "usages";

export type FileErrorKey =
  | "errors.sign_in_again"
  | "errors.access_denied"
  | "errors.file_not_found"
  | "errors.file_in_knowledge_base"
  | "errors.file_too_large"
  | "errors.file_type_unsupported"
  | "errors.office_type_mismatch"
  | "errors.office_invalid_package"
  | "errors.office_active_content_unsupported"
  | "errors.office_package_limit"
  | "errors.office_multipart_required"
  | "errors.invalid_request"
  | "errors.upload_rate_limited"
  | "errors.upload_pending_limit"
  | "errors.upload_expired"
  | "errors.upload_not_received"
  | "errors.upload_in_progress"
  | "errors.file_content_missing"
  | "errors.service_unavailable"
  | "errors.network"
  | "errors.load_failed"
  | "errors.upload_failed"
  | "errors.delete_failed"
  | "errors.restore_failed"
  | "errors.purge_failed"
  | "errors.preview_failed"
  | "errors.category_failed"
  | "errors.usages_failed";

const FILE_ERROR_KEY: Readonly<Record<string, FileErrorKey>> = {
  auth_required: "errors.sign_in_again",
  invalid_token: "errors.sign_in_again",
  password_changed_relogin_required: "errors.sign_in_again",
  user_inactive: "errors.sign_in_again",
  user_not_active: "errors.sign_in_again",
  file_upload_authorization_failed: "errors.upload_expired",
  workspace_access_denied: "errors.access_denied",
  resource_grant_denied: "errors.access_denied",
  file_upload_session_denied: "errors.access_denied",
  file_not_found: "errors.file_not_found",
  file_in_knowledge_base: "errors.file_in_knowledge_base",
  file_too_large: "errors.file_too_large",
  file_type_unsupported: "errors.file_type_unsupported",
  file_office_type_mismatch: "errors.office_type_mismatch",
  file_office_invalid_package: "errors.office_invalid_package",
  file_office_unsafe_xml: "errors.office_invalid_package",
  file_office_active_content_unsupported: "errors.office_active_content_unsupported",
  file_office_package_limit: "errors.office_package_limit",
  file_office_multipart_required: "errors.office_multipart_required",
  invalid_file_upload_request: "errors.invalid_request",
  invalid_file_category: "errors.invalid_request",
  file_presign_rate_limited: "errors.upload_rate_limited",
  file_presign_pending_limit: "errors.upload_pending_limit",
  file_presign_expired: "errors.upload_expired",
  file_upload_not_received: "errors.upload_not_received",
  file_confirm_in_progress: "errors.upload_in_progress",
  file_upload_in_progress: "errors.upload_in_progress",
  file_upload_empty: "errors.invalid_request",
  file_preview_failed: "errors.preview_failed",
  file_content_missing: "errors.file_content_missing",
  file_service_unavailable: "errors.service_unavailable",
  file_access_service_unavailable: "errors.service_unavailable",
  file_presign_unavailable: "errors.service_unavailable",
  file_upload_write_failed: "errors.upload_failed",
};

const OPERATION_FALLBACK: Readonly<Record<FileErrorOperation, FileErrorKey>> = {
  load: "errors.load_failed",
  upload: "errors.upload_failed",
  delete: "errors.delete_failed",
  restore: "errors.restore_failed",
  purge: "errors.purge_failed",
  preview: "errors.preview_failed",
  category: "errors.category_failed",
  usages: "errors.usages_failed",
};

function errorContract(
  error: unknown,
): { code?: string; status: number } | null {
  if (error instanceof ApiError || error instanceof UploadHttpError) {
    return { code: error.code, status: error.status };
  }
  if (error instanceof UploadTerminalError) {
    return { code: error.cause.code, status: error.cause.status };
  }
  return null;
}

/** Resolve file failures without rendering raw backend/XHR prose or HTTP status. */
export function fileError(
  error: unknown,
  operation: FileErrorOperation,
): FileErrorKey {
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

  const contract = errorContract(error);
  if (!contract) return OPERATION_FALLBACK[operation];
  const mapped = contract.code ? FILE_ERROR_KEY[contract.code] : undefined;
  if (mapped) return mapped;

  if (contract.status === 401) return "errors.sign_in_again";
  if (contract.status === 403) return "errors.access_denied";
  if (contract.status === 404) return "errors.file_not_found";
  if (contract.status === 413) return "errors.file_too_large";
  if (contract.status === 415) return "errors.file_type_unsupported";
  if (contract.status === 429) return "errors.upload_rate_limited";
  if (contract.status === 400 || contract.status === 422) {
    return "errors.invalid_request";
  }
  if (contract.status >= 500) return "errors.service_unavailable";
  return OPERATION_FALLBACK[operation];
}

export function fileErrorFromCode(
  code: string | null | undefined,
  operation: FileErrorOperation,
): FileErrorKey {
  return (code ? FILE_ERROR_KEY[code] : undefined)
    ?? OPERATION_FALLBACK[operation];
}
