
import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "@/lib/api-client";
import { MatrixApiError } from "@/lib/kb-matrix/api-error";

export type KbDocTypeErrorOperation =
  | "load"
  | "create"
  | "edit"
  | "delete"
  | "draft";

export type KbDocTypeErrorKey =
  | "errors.sign_in_again"
  | "errors.access_denied"
  | "errors.not_owner"
  | "errors.readonly"
  | "errors.not_found"
  | "errors.name_taken"
  | "errors.fields_frozen"
  | "errors.too_many_fields"
  | "errors.duplicate_fields"
  | "errors.options_required"
  | "errors.option_empty"
  | "errors.option_too_long"
  | "errors.option_duplicate"
  | "errors.options_too_many"
  | "errors.options_not_applicable"
  | "errors.invalid_schema"
  | "errors.draft_unavailable"
  | "errors.draft_invalid_response"
  | "errors.service_unavailable"
  | "errors.rate_limited"
  | "errors.network"
  | "errors.load_failed"
  | "errors.create_failed"
  | "errors.edit_failed"
  | "errors.delete_failed"
  | "errors.draft_failed";

export const KB_DOC_TYPE_ERROR_KEY: Readonly<Record<string, KbDocTypeErrorKey>> =
  {
    auth_required: "errors.sign_in_again",
    invalid_token: "errors.sign_in_again",
    password_changed_relogin_required: "errors.sign_in_again",
    user_inactive: "errors.sign_in_again",
    user_not_active: "errors.sign_in_again",
    workspace_access_denied: "errors.access_denied",
    workspace_role_unknown: "errors.access_denied",
    workspace_member_role_required: "errors.access_denied",
    workspace_admin_or_creator_required: "errors.not_owner",
    owner_or_admin_required: "errors.not_owner",
    over_grace: "errors.readonly",
    suspended: "errors.readonly",
    needs_license: "errors.readonly",
    tamper: "errors.readonly",
    clock_tamper: "errors.readonly",
    doc_type_not_found: "errors.not_found",
    doc_type_name_taken: "errors.name_taken",
    graduated_fields_frozen: "errors.fields_frozen",
    too_many_doc_type_fields: "errors.too_many_fields",
    duplicate_field_names: "errors.duplicate_fields",
    options_required: "errors.options_required",
    option_empty: "errors.option_empty",
    option_too_long: "errors.option_too_long",
    option_duplicate: "errors.option_duplicate",
    options_too_many: "errors.options_too_many",
    options_not_applicable: "errors.options_not_applicable",
    invalid_doc_type_schema: "errors.invalid_schema",
    doc_type_draft_service_unavailable: "errors.draft_unavailable",
    doc_type_draft_invalid_response: "errors.draft_invalid_response",
    doc_type_draft_failed: "errors.draft_failed",
    kb_doc_type_service_unavailable: "errors.service_unavailable",
    kb_doc_type_write_unavailable: "errors.service_unavailable",
    rate_limited: "errors.rate_limited",
    rate_limit: "errors.rate_limited",
  };

const OPERATION_FALLBACK: Readonly<
  Record<KbDocTypeErrorOperation, KbDocTypeErrorKey>
> = {
  load: "errors.load_failed",
  create: "errors.create_failed",
  edit: "errors.edit_failed",
  delete: "errors.delete_failed",
  draft: "errors.draft_failed",
};

function errorContract(
  error: unknown,
): { code?: string; status: number } | null {
  if (error instanceof ApiError || error instanceof MatrixApiError) {
    return { code: error.code, status: error.status };
  }
  return null;
}

/**
 * Resolve doc-type failures from a finite code/status set. Backend prose is
 * deliberately ignored so internal validation, SQL, and model diagnostics
 * cannot reach the page.
 */
export function kbDocTypeError(
  error: unknown,
  operation: KbDocTypeErrorOperation,
): KbDocTypeErrorKey {
  const contract = errorContract(error);
  if (!contract) return isNetworkFailure(error) ? "errors.network" : OPERATION_FALLBACK[operation];

  const key = contract.code
    ? KB_DOC_TYPE_ERROR_KEY[contract.code]
    : undefined;
  if (key) return key;

  if (contract.status === 401) return "errors.sign_in_again";
  if (contract.status === 403) return "errors.access_denied";
  if (contract.status === 404) return "errors.not_found";
  if (contract.status === 422 || contract.status === 400) {
    return "errors.invalid_schema";
  }
  if (contract.status === 429) return "errors.rate_limited";
  if (contract.status >= 500) return "errors.service_unavailable";
  return OPERATION_FALLBACK[operation];
}
