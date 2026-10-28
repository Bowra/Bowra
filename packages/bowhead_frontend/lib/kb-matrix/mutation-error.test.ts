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
  ])("maps payload code %s with safe interpolation values", (code, payload, key, values) => {
    expect(
      kbMatrixMutationError(
        new MatrixApiError(422, code, "RAW_BACKEND_DIAGNOSTIC", payload),
      ),
    ).toEqual({ key, values });
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
