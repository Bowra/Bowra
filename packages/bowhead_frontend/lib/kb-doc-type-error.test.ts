import { describe, expect, it } from "vitest";

import enUS from "../messages/en-US.json";
import zhCN from "../messages/zh-CN.json";
import { ApiError } from "./api-client";
import {
  KB_DOC_TYPE_ERROR_KEY,
  kbDocTypeError,
} from "./kb-doc-type-error";
import { MatrixApiError } from "./kb-matrix/api-error";

type Bundle = {
  kt: {
    errors: Record<string, string>;
  };
};

describe("kbDocTypeError", () => {
  it("maps every finite backend code to a non-empty message in both locales", () => {
    for (const [code, key] of Object.entries(KB_DOC_TYPE_ERROR_KEY)) {
      const leaf = key.replace("errors.", "");
      expect((zhCN as Bundle).kt.errors[leaf], `zh missing ${code} -> ${key}`)
        .toBeTruthy();
      expect((enUS as Bundle).kt.errors[leaf], `en missing ${code} -> ${key}`)
        .toBeTruthy();
    }
  });

  it("ignores backend prose for API and matrix clients", () => {
    expect(
      kbDocTypeError(
        new ApiError(409, "RAW_SQL_DIAGNOSTIC", {
          code: "doc_type_name_taken",
        }),
        "create",
      ),
    ).toBe("errors.name_taken");
    expect(
      kbDocTypeError(
        new MatrixApiError(
          422,
          "graduated_fields_frozen",
          "RAW_STORE_DIAGNOSTIC",
          {},
        ),
        "edit",
      ),
    ).toBe("errors.fields_frozen");
  });

  it.each([
    [401, "errors.sign_in_again"],
    [403, "errors.access_denied"],
    [404, "errors.not_found"],
    [422, "errors.invalid_schema"],
    [429, "errors.rate_limited"],
    [503, "errors.service_unavailable"],
    [409, "errors.edit_failed"],
  ])("uses a safe fallback for HTTP %i", (status, key) => {
    expect(
      kbDocTypeError(
        new ApiError(status, `operation: ${status} RAW_BACKEND`, {
          code: "future_code",
        }),
        "edit",
      ),
    ).toBe(key);
  });

  it("uses an operation fallback for unknown non-API errors", () => {
    expect(
      kbDocTypeError(new Error("URL, token, and network diagnostics"), "load"),
    ).toBe("errors.load_failed");
  });
});
