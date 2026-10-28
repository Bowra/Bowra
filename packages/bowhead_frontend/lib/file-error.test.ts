import { describe, expect, it } from "vitest";

import enUS from "../messages/en-US.json";
import zhCN from "../messages/zh-CN.json";
import { ApiError } from "./api-client";
import { fileError, fileErrorFromCode } from "./file-error";
import { UploadHttpError, UploadTerminalError } from "./upload-xhr";

type Bundle = {
  files_page: {
    errors: Record<string, string>;
  };
};

const expectedKeys = [
  "sign_in_again",
  "access_denied",
  "file_not_found",
  "file_in_knowledge_base",
  "file_too_large",
  "file_type_unsupported",
  "office_type_mismatch",
  "office_invalid_package",
  "office_active_content_unsupported",
  "office_package_limit",
  "office_multipart_required",
  "invalid_request",
  "upload_rate_limited",
  "upload_pending_limit",
  "upload_expired",
  "upload_not_received",
  "upload_in_progress",
  "file_content_missing",
  "service_unavailable",
  "network",
  "load_failed",
  "upload_failed",
  "delete_failed",
  "restore_failed",
  "purge_failed",
  "preview_failed",
  "category_failed",
  "usages_failed",
] as const;

describe("fileError", () => {
  it.each([
    ["file_office_type_mismatch", "errors.office_type_mismatch"],
    ["file_office_invalid_package", "errors.office_invalid_package"],
    ["file_office_unsafe_xml", "errors.office_invalid_package"],
    ["file_office_active_content_unsupported", "errors.office_active_content_unsupported"],
    ["file_office_package_limit", "errors.office_package_limit"],
    ["file_office_multipart_required", "errors.office_multipart_required"],
  ])("maps Office rejection %s", (code, key) => {
    expect(fileError(new ApiError(415, "UNTRUSTED", { code }), "upload")).toBe(key);
  });

  it("has a non-empty bilingual message for every output key", () => {
    for (const key of expectedKeys) {
      expect((zhCN as Bundle).files_page.errors[key], `zh ${key}`).toBeTruthy();
      expect((enUS as Bundle).files_page.errors[key], `en ${key}`).toBeTruthy();
    }
  });

  it("maps API and XHR codes without returning backend prose or status", () => {
    expect(
      fileError(
        new ApiError(409, "RAW_DB_PATH", {
          code: "file_in_knowledge_base",
        }),
        "delete",
      ),
    ).toBe("errors.file_in_knowledge_base");

    const xhr = new UploadHttpError(
      413,
      JSON.stringify({
        detail: { error: { code: "file_too_large" } },
      }),
    );
    expect(fileError(new UploadTerminalError(xhr), "upload")).toBe(
      "errors.file_too_large",
    );
    expect(xhr.message).not.toContain("413");
    expect(xhr.message).not.toContain("file_too_large");
  });

  it("shows one not-found-or-no-access message for every per-file refusal", () => {
    for (const operation of ["load", "preview", "delete", "restore", "category", "usages"] as const) {
      expect(
        fileError(new ApiError(404, "RAW", { code: "file_not_found" }), operation),
      ).toBe("errors.file_not_found");
    }
    expect(fileErrorFromCode("file_not_found", "preview")).toBe("errors.file_not_found");
  });

  it("uses safe operation fallbacks for unknown errors and HTTP 200 codes", () => {
    expect(
      fileError(
        new ApiError(409, "RAW_CONFIRM_STATE", {
          code: "future_file_code",
        }),
        "restore",
      ),
    ).toBe("errors.restore_failed");
    expect(fileError(new Error("TOKEN_AND_URL"), "upload")).toBe(
      "errors.upload_failed",
    );
    expect(fileErrorFromCode("file_preview_failed", "preview")).toBe(
      "errors.preview_failed",
    );
    expect(fileErrorFromCode("future_preview_code", "preview")).toBe(
      "errors.preview_failed",
    );
  });
});
