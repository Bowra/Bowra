// @vitest-environment node
import { describe, expect, it } from "vitest";

import enUS from "../../messages/en-US.json";
import zhCN from "../../messages/zh-CN.json";
import { MatrixApiError } from "./api-error";
import {
  KB_MATRIX_MUTATION_ERROR_KEY,
  kbMatrixMutationError,
} from "./mutation-error";

type Bundle = Record<string, Record<string, unknown>>;

describe("kbMatrixMutationError", () => {
  it("every finite code maps to a non-empty key in both locales", () => {
    for (const [code, key] of Object.entries(
      KB_MATRIX_MUTATION_ERROR_KEY,
    )) {
      expect((zhCN as Bundle).kbMatrix[key], `zh missing ${code} -> ${key}`)
        .toBeTruthy();
      expect((enUS as Bundle).kbMatrix[key], `en missing ${code} -> ${key}`)
        .toBeTruthy();
    }
  });

  it.each([
    [
      "too_many_files_to_graduate",
      { max: 2000 },
      "err_grad_too_many_files",
      { max: 2000 },
    ],
    [
      "too_many_columns_to_graduate",
      { max: 20, got: 21 },
      "grad_over",
      { max: 20, got: 21 },
    ],
    [
      "duplicate_column_names",
      { columns: ["金额", "日期"] },
      "err_grad_dup_cols",
      { cols: "金额, 日期" },
    ],
    [
      "invalid_column_name",
      { columns: ["金额(元)"] },
      "err_grad_bad_colname",
      { cols: "金额(元)" },
    ],
    [
      "matrix_column_limit",
      { limit: 20, count: 20, requested: 21 },
      "err_matrix_column_limit_add",
      { limit: 20, count: 20, requested: 21 },
    ],
    [
      "matrix_column_limit",
      { limit: 20, count: 0, requested: 25 },
      "err_matrix_column_limit_create",
      { limit: 20, count: 0, requested: 25 },
    ],
    [
      "matrix_file_limit",
      { limit: 500, count: 480, requested: 530 },
      "err_matrix_file_limit_add",
      { limit: 500, count: 480, requested: 530 },
    ],
    [
      "matrix_file_limit",
      { limit: 500, count: 0, requested: 600 },
      "err_matrix_file_limit_create",
      { limit: 500, count: 0, requested: 600 },
    ],
    [
      "column_name_duplicate",
      { name: "金额（万元）", column_id: "mxcol_1" },
      "err_column_name_duplicate",
      { name: "金额（万元）" },
    ],
    [
      "matrix_files_batch_limit",
      { limit: 100, requested: 130 },
      "err_matrix_files_batch_limit",
      { limit: 100, requested: 130 },
    ],
    [
      "matrix_remove_requires_owner",
      { edited: 2, verified: 5 },
      "err_remove_requires_owner",
      { edited: 2, verified: 5 },
    ],
    [
      "matrix_verify_files_not_in_matrix",
      { count: 2 },
      "err_verify_files_not_in_matrix",
      { count: 2 },
    ],
    // Step 1 S8: refusals name the caller's own files and the blocking columns; other people's files are counted.
    [
      "matrix_personal_files_block_space",
      { files: [{ file_id: "kbf_1", name: "批注.docx" }, { file_id: "kbf_2", name: "notes.md" }], count: 2, hidden: 1 },
      "err_visibility_personal_files",
      { count: 2, files: "批注.docx, notes.md" },
    ],
    [
      "matrix_personal_files_block_space",
      { files: [], count: 0, hidden: 3 },
      "err_visibility_personal_files_hidden",
      { count: 3 },
    ],
    [
      "matrix_graduation_personal_files",
      { files: [{ file_id: "kbf_1", name: "批注.docx" }], count: 1 },
      "err_grad_personal_files",
      { count: 1, files: "批注.docx" },
    ],
    ["matrix_visibility_runs_active", { count: 2 }, "err_visibility_runs_active", { count: 2 }],
    ["matrix_navigation_columns_block_private", { columns: ["摘要", "主题"] }, "err_navigation_columns_block_private", { cols: "摘要, 主题" }],
  ])("maps payload code %s with safe interpolation values", (code, payload, key, values) => {
    expect(
      kbMatrixMutationError(
        new MatrixApiError(422, code, "RAW_BACKEND_DIAGNOSTIC", payload),
      ),
    ).toEqual({ key, values });
    expect((zhCN as Bundle).kbMatrix[key], `zh missing ${key}`).toBeTruthy();
    expect((enUS as Bundle).kbMatrix[key], `en missing ${key}`).toBeTruthy();
  });

  it("maps a busy column type change to its name and pending cell count", () => {
    expect(
      kbMatrixMutationError(
        new MatrixApiError(409, "column_busy", "RAW_BACKEND_DIAGNOSTIC", {
          column_id: "mxcol_1",
          name: "金额",
          pending: 3,
        }),
      ),
    ).toEqual({ key: "err_column_busy", values: { name: "金额", count: 3 } });
  });

  it("ignores backend prose for known, unknown, and network failures", () => {
    expect(
      kbMatrixMutationError(
        new MatrixApiError(
          422,
          "options_required",
          "RAW_BACKEND_DIAGNOSTIC",
          {},
        ),
      ),
    ).toEqual({ key: "err_options_required" });
    expect(
      kbMatrixMutationError(
        new MatrixApiError(
          422,
          "future_code",
          "createMatrix: 422 RAW_BACKEND_DIAGNOSTIC",
          {},
        ),
      ),
    ).toEqual({ key: "err_mutation_invalid_request" });
    expect(
      kbMatrixMutationError(new Error("network URL and token diagnostics")),
    ).toEqual({ key: "err_mutation_generic" });
  });

  it.each([
    [401, "err_mutation_sign_in_again"],
    [403, "err_mutation_access_denied"],
    [404, "err_mutation_matrix_changed"],
    [422, "err_mutation_invalid_request"],
    [429, "err_rate_limited"],
    [503, "err_mutation_service_unavailable"],
    [409, "err_mutation_generic"],
  ])("uses a safe status fallback for HTTP %i", (status, key) => {
    expect(
      kbMatrixMutationError(
        new MatrixApiError(status, "future_code", `operation: ${status}`, {}),
      ),
    ).toEqual({ key });
  });
});
