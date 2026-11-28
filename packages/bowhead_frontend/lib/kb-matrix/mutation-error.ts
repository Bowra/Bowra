
import { isNetworkFailure } from "@/lib/transport-error";
import { MatrixApiError } from "./api-error";

export type KbMatrixMutationErrorKey =
  | "err_create_personal_files"
  | "err_create_source_file"
  | "err_create_template_missing"
  | "err_create_template_flow"
  | "err_invalid_column_type"
  | "err_options_required"
  | "err_option_empty"
  | "err_option_too_long"
  | "err_option_duplicate"
  | "err_options_too_many"
  | "err_options_not_applicable"
  | "err_cross_verify_not_applicable"
  | "err_promotion_sole"
  | "err_invalid_promotion"
  | "err_column_name_invalid"
  | "err_column_name_duplicate"
  | "err_column_busy"
  | "err_matrix_column_limit_add"
  | "err_matrix_column_limit_create"
  | "err_matrix_file_limit_add"
  | "err_matrix_file_limit_create"
  | "err_matrix_files_batch_limit"
  | "err_matrix_name_invalid"
  | "err_remove_requires_owner"
  | "err_cell_value_too_large"
  | "err_cell_value_not_serializable"
  | "err_cell_not_edited"
  | "err_cell_verified_required"
  | "err_cell_verify_requires_done"
  | "err_verify_files_empty"
  | "err_verify_files_not_in_matrix"
  | "err_visibility_personal_files"
  | "err_visibility_personal_files_hidden"
  | "err_visibility_runs_active"
  | "err_navigation_role_private"
  | "err_navigation_columns_block_private"
  | "err_visibility_unavailable"
  | "err_creator_required"
  | "err_grad_personal_files"
  | "err_visibility_changed"
  | "err_grad_rows_failed"
  | "err_grad_too_many_files"
  | "grad_over"
  | "err_grad_dup_cols"
  | "err_grad_bad_colname"
  | "err_grad_colname_long"
  | "err_grad_empty"
  | "err_grad_invalid_request"
  | "err_name_taken"
  | "err_rate_limited"
  | "err_mutation_service_unavailable"
  | "err_mutation_matrix_changed"
  | "err_mutation_column_changed"
  | "err_mutation_cell_changed"
  | "err_mutation_access_unavailable"
  | "err_mutation_space_required"
  | "err_mutation_access_denied"
  | "err_mutation_member_required"
  | "err_mutation_owner_required"
  | "err_mutation_sign_in_again"
  | "err_mutation_readonly"
  | "err_mutation_feature_unavailable"
  | "err_mutation_invalid_request"
  | "err_mutation_network"
  | "err_mutation_generic";

export interface KbMatrixMutationErrorMessage {
  key: KbMatrixMutationErrorKey;
  values?: Record<string, string | number>;
}

export const KB_MATRIX_MUTATION_ERROR_KEY: Readonly<
  Record<string, KbMatrixMutationErrorKey>
> = {
  personal_files_not_allowed: "err_create_personal_files",
  matrix_source_file_unavailable: "err_create_source_file",
  matrix_file_not_found: "err_create_source_file",
  matrix_template_not_found: "err_create_template_missing",
  template_use_new_matrix_flow: "err_create_template_flow",
  invalid_column_type: "err_invalid_column_type",
  invalid_matrix_request: "err_mutation_invalid_request",
  options_required: "err_options_required",
  option_empty: "err_option_empty",
  option_too_long: "err_option_too_long",
  option_duplicate: "err_option_duplicate",
  options_too_many: "err_options_too_many",
  options_not_applicable: "err_options_not_applicable",
  cross_verify_not_applicable: "err_cross_verify_not_applicable",
  promote_must_be_sole_change: "err_promotion_sole",
  invalid_promotion: "err_invalid_promotion",
  column_name_invalid: "err_column_name_invalid",
  matrix_name_invalid: "err_matrix_name_invalid",
  cell_value_too_large: "err_cell_value_too_large",
  cell_value_not_serializable: "err_cell_value_not_serializable",
  not_edited: "err_cell_not_edited",
  verified_required: "err_cell_verified_required",
  verify_requires_done: "err_cell_verify_requires_done",
  matrix_verify_files_empty: "err_verify_files_empty",
  // Step 1 S8: only-me matrices.
  matrix_navigation_role_private: "err_navigation_role_private",
  matrix_visibility_unavailable: "err_visibility_unavailable",
  matrix_creator_required: "err_creator_required",
  matrix_visibility_changed: "err_visibility_changed",
  graduation_rows_failed: "err_grad_rows_failed",
  column_name_too_long: "err_grad_colname_long",
  empty_matrix: "err_grad_empty",
  invalid_graduation_request: "err_grad_invalid_request",
  doc_type_name_taken: "err_name_taken",
  rate_limited: "err_rate_limited",
  matrix_data_service_unavailable: "err_mutation_service_unavailable",
  matrix_service_unavailable: "err_mutation_service_unavailable",
  matrix_not_found: "err_mutation_matrix_changed",
  matrix_column_not_found: "err_mutation_column_changed",
  matrix_cell_not_found: "err_mutation_cell_changed",
  access_control_unavailable: "err_mutation_access_unavailable",
  authz_unavailable: "err_mutation_access_unavailable",
  workspace_required: "err_mutation_space_required",
  workspace_access_denied: "err_mutation_access_denied",
  workspace_role_unknown: "err_mutation_access_denied",
  workspace_member_role_required: "err_mutation_member_required",
  owner_or_admin_required: "err_mutation_owner_required",
  auth_required: "err_mutation_sign_in_again",
  invalid_token: "err_mutation_sign_in_again",
  password_changed_relogin_required: "err_mutation_sign_in_again",
  user_inactive: "err_mutation_sign_in_again",
  user_not_active: "err_mutation_sign_in_again",
  over_grace: "err_mutation_readonly",
  suspended: "err_mutation_readonly",
  needs_license: "err_mutation_readonly",
  tamper: "err_mutation_readonly",
  clock_tamper: "err_mutation_readonly",
  entitlement_unavailable: "err_mutation_feature_unavailable",
  feature_unlicensed: "err_mutation_feature_unavailable",
};

function columnsValue(payload: Record<string, unknown>): string {
  return Array.isArray(payload.columns)
    ? payload.columns.map(String).join(", ")
    : "";
}

/** The file names a refusal lists (the caller's own files; at most the first 20 from the server). */
function filesValue(payload: Record<string, unknown>): string {
  return Array.isArray(payload.files)
    ? payload.files
        .map((f) => (f && typeof f === "object" && "name" in f ? String((f as { name: unknown }).name) : ""))
        .filter(Boolean)
        .join(", ")
    : "";
}

/**
 * Map create/edit/graduate failures without exposing backend prose, codes, or
 * HTTP function/status fallbacks.
 */
export function kbMatrixMutationError(
  error: unknown,
): KbMatrixMutationErrorMessage {
  if (!(error instanceof MatrixApiError)) {
    return { key: isNetworkFailure(error) ? "err_mutation_network" : "err_mutation_generic" };
  }

  if (
    error.code === "matrix_column_limit" ||
    error.code === "matrix_file_limit"
  ) {
    // count is what the matrix holds now (0 for a new matrix); requested is the size after the change.
    const count = Number(error.payload.count ?? 0);
    const columns = error.code === "matrix_column_limit";
    const key: KbMatrixMutationErrorKey =
      count > 0
        ? columns
          ? "err_matrix_column_limit_add"
          : "err_matrix_file_limit_add"
        : columns
          ? "err_matrix_column_limit_create"
          : "err_matrix_file_limit_create";
    return {
      key,
      values: {
        limit: Number(error.payload.limit ?? 0),
        count,
        requested: Number(error.payload.requested ?? 0),
      },
    };
  }
  if (error.code === "matrix_files_batch_limit") {
    return {
      key: "err_matrix_files_batch_limit",
      values: {
        limit: Number(error.payload.limit ?? 0),
        requested: Number(error.payload.requested ?? 0),
      },
    };
  }
  if (error.code === "matrix_remove_requires_owner") {
    return {
      key: "err_remove_requires_owner",
      values: {
        edited: Number(error.payload.edited ?? 0),
        verified: Number(error.payload.verified ?? 0),
      },
    };
  }
  if (error.code === "matrix_verify_files_not_in_matrix") {
    return {
      key: "err_verify_files_not_in_matrix",
      values: { count: Number(error.payload.count ?? 0) },
    };
  }
  if (error.code === "matrix_personal_files_block_space") {
    // Name the caller's own personal files; other people's are only counted.
    const count = Number(error.payload.count ?? 0);
    return count > 0
      ? { key: "err_visibility_personal_files", values: { count, files: filesValue(error.payload) } }
      : { key: "err_visibility_personal_files_hidden", values: { count: Number(error.payload.hidden ?? 0) } };
  }
  if (error.code === "matrix_graduation_personal_files") {
    return {
      key: "err_grad_personal_files",
      values: { count: Number(error.payload.count ?? 0), files: filesValue(error.payload) },
    };
  }
  if (error.code === "matrix_visibility_runs_active") {
    return { key: "err_visibility_runs_active", values: { count: Number(error.payload.count ?? 0) } };
  }
  if (error.code === "matrix_navigation_columns_block_private") {
    return { key: "err_navigation_columns_block_private", values: { cols: columnsValue(error.payload) } };
  }
  if (error.code === "column_name_duplicate") {
    return {
      key: "err_column_name_duplicate",
      values: { name: String(error.payload.name ?? "") },
    };
  }
  if (error.code === "column_busy") {
    return {
      key: "err_column_busy",
      values: {
        name: String(error.payload.name ?? ""),
        count: Number(error.payload.pending ?? 0),
      },
    };
  }
  if (error.code === "too_many_files_to_graduate") {
    return {
      key: "err_grad_too_many_files",
      values: { max: Number(error.payload.max ?? 0) },
    };
  }
  if (error.code === "too_many_columns_to_graduate") {
    return {
      key: "grad_over",
      values: {
        max: Number(error.payload.max ?? 20),
        got: Number(error.payload.got ?? 0),
      },
    };
  }
  if (error.code === "duplicate_column_names") {
    return {
      key: "err_grad_dup_cols",
      values: { cols: columnsValue(error.payload) },
    };
  }
  if (error.code === "invalid_column_name") {
    return {
      key: "err_grad_bad_colname",
      values: { cols: columnsValue(error.payload) },
    };
  }

  const key = KB_MATRIX_MUTATION_ERROR_KEY[error.code];
  if (key) return { key };
  if (error.code.startsWith("ws_access_denied:")) {
    return { key: "err_mutation_access_denied" };
  }

  if (error.status === 401) return { key: "err_mutation_sign_in_again" };
  if (error.status === 403) return { key: "err_mutation_access_denied" };
  if (error.status === 404) return { key: "err_mutation_matrix_changed" };
  if (error.status === 422) return { key: "err_mutation_invalid_request" };
  if (error.status === 429) return { key: "err_rate_limited" };
  if (error.status >= 500) {
    return { key: "err_mutation_service_unavailable" };
  }
  return { key: "err_mutation_generic" };
}
