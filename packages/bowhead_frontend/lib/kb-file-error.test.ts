import { describe, expect, it } from "vitest";

import enUS from "../messages/en-US.json";
import zhCN from "../messages/zh-CN.json";
import { AuthError } from "./auth-api";
import { ApiError } from "./api-client";
import {
  KB_FILE_ERROR_KEY,
  kbFileError,
  kbFileErrorFromCode,
} from "./kb-file-error";
import { UploadHttpError, UploadTerminalError } from "./upload-xhr";

type Bundle = {
  knowledge_base: {
    errors: Record<string, string>;
  };
};

describe("kbFileError", () => {
  it("maps every finite backend code to a non-empty message in both locales", () => {
    for (const [code, key] of Object.entries(KB_FILE_ERROR_KEY)) {
      const leaf = key.replace("errors.", "");
      expect(
        (zhCN as Bundle).knowledge_base.errors[leaf],
        `zh missing ${code} -> ${key}`,
      ).toBeTruthy();
      expect(
        (enUS as Bundle).knowledge_base.errors[leaf],
        `en missing ${code} -> ${key}`,
      ).toBeTruthy();
    }
  });

  it("ignores backend prose for known, unknown, and network failures", () => {
    expect(
      kbFileError(
        new ApiError(403, "RAW_GRANT_DIAGNOSTIC", {
          code: "resource_grant_denied",
        }),
        "load",
      ),
    ).toBe("errors.access_denied");
    expect(
      kbFileError(
        new ApiError(500, "SQLITE_PATH_AND_SCHEMA", {
          code: "future_kb_code",
        }),
        "scope",
      ),
    ).toBe("errors.service_unavailable");
    expect(
      kbFileError(new Error("network URL and token diagnostics"), "download"),
    ).toBe("errors.download_failed");
  });

  it.each([
    [401, "errors.sign_in_again"],
    [403, "errors.access_denied"],
    [404, "errors.file_not_found"],
    [413, "errors.file_too_large"],
    [415, "errors.file_type_unsupported"],
    [422, "errors.invalid_request"],
    [429, "errors.rate_limited"],
    [503, "errors.service_unavailable"],
  ])("uses a safe status fallback for HTTP %i", (status, key) => {
    expect(
      kbFileError(
        new ApiError(status, `operation: ${status} RAW_BACKEND`, {
          code: "future_kb_code",
        }),
        "bind",
      ),
    ).toBe(key);
  });

  it("shows one not-found-or-no-access message for unreadable and missing files", () => {
    for (const operation of ["load", "preview", "download", "delete", "scope", "bind", "reparse"] as const) {
      expect(
        kbFileError(new ApiError(404, "RAW", { code: "kb_file_not_found" }), operation),
      ).toBe("errors.file_not_found");
    }
    expect(kbFileErrorFromCode("kb_file_not_found", "delete")).toBe("errors.file_not_found");
    expect(kbFileErrorFromCode("kb_artifact_not_found", "upload")).toBe("errors.file_not_found");
  });

  it("maps HTTP 200 partial-result codes without rendering result.error", () => {
    expect(
      kbFileErrorFromCode("kb_scope_change_reason_required", "scope"),
    ).toBe("errors.scope_reason_required");
    expect(kbFileErrorFromCode("future_kb_code", "scope")).toBe(
      "errors.scope_change_failed",
    );
  });

  it.each(["kb_file_write_failed", "kb_file_operation_failed"])(
    "keeps the operation outcome uncertain for %s instead of asserting a service outage",
    (code) => {
      expect(kbFileError(new ApiError(503, "RAW_STORAGE_DETAIL", { code }), "bind"))
        .toBe("errors.bind_operation_failed");
      expect(kbFileErrorFromCode(code, "upload")).toBe("errors.upload_operation_failed");
    },
  );

  it("distinguishes an unlicensed Knowledge Base from a role denial", () => {
    expect(
      kbFileError(
        new ApiError(403, "未授权功能: kb", {
          code: "feature_unlicensed",
        }),
        "upload",
      ),
    ).toBe("errors.feature_unlicensed");
  });

  it("maps small and large upload transports by code without showing transport prose", () => {
    expect(
      kbFileError(
        new AuthError(
          401,
          "RAW_BEARER_TOKEN_DIAGNOSTIC",
          "file_upload_authorization_failed",
        ),
        "upload",
      ),
    ).toBe("errors.upload_expired");

    const xhr = new UploadHttpError(
      415,
      JSON.stringify({
        detail: { error: { code: "file_type_unsupported" } },
      }),
    );
    expect(kbFileError(new UploadTerminalError(xhr), "upload")).toBe(
      "errors.file_type_unsupported",
    );
    expect(xhr.message).not.toContain("file_type_unsupported");
    expect(xhr.message).not.toContain("415");
  });
});
