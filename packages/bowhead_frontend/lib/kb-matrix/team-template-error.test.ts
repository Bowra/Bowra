import { describe, expect, it } from "vitest";

import enUS from "../../messages/en-US.json";
import zhCN from "../../messages/zh-CN.json";
import { MatrixApiError } from "./api-error";
import {
  KB_TEAM_TEMPLATE_ERROR_KEY,
  kbTeamTemplateError,
} from "./team-template-error";

type Bundle = Record<string, Record<string, unknown>>;

describe("kbTeamTemplateError", () => {
  it("maps every finite backend code to a non-empty message in both locales", () => {
    for (const [code, key] of Object.entries(
      KB_TEAM_TEMPLATE_ERROR_KEY,
    )) {
      expect((zhCN as Bundle).kbMatrix[key], `zh missing ${code} -> ${key}`)
        .toBeTruthy();
      expect((enUS as Bundle).kbMatrix[key], `en missing ${code} -> ${key}`)
        .toBeTruthy();
    }
  });

  it("ignores backend prose for known, unknown, and network failures", () => {
    expect(
      kbTeamTemplateError(
        new MatrixApiError(
          422,
          "invalid_template_schema",
          "RAW_SCHEMA_DIAGNOSTIC",
          {},
        ),
        "copy",
      ),
    ).toBe("err_invalid_template_schema");
    expect(
      kbTeamTemplateError(
        new MatrixApiError(
          503,
          "team_template_copy_refresh_required",
          "RAW_RELOAD_DIAGNOSTIC",
          {},
        ),
        "copy",
      ),
    ).toBe("err_copy_refresh_required");
    expect(
      kbTeamTemplateError(
        new MatrixApiError(
          422,
          "future_code",
          "publishTemplateToTeam: 422 RAW_BACKEND_DIAGNOSTIC",
          {},
        ),
        "publish",
      ),
    ).toBe("err_publish_failed");
    expect(
      kbTeamTemplateError(
        new Error("network URL and token diagnostics"),
        "copy",
      ),
    ).toBe("err_copy_failed");
  });

  it.each([
    [401, "err_team_template_sign_in_again"],
    [403, "err_team_template_access_denied"],
    [404, "otpl_gone"],
    [429, "err_team_template_rate_limited"],
    [503, "err_team_template_service_unavailable"],
  ])("uses a safe status fallback for HTTP %i", (status, key) => {
    expect(
      kbTeamTemplateError(
        new MatrixApiError(
          status,
          "future_code",
          `operation: ${status} RAW_BACKEND`,
          {},
        ),
        "copy",
      ),
    ).toBe(key);
  });

  it.each([
    ["load", "otpl_error"],
    ["publish", "err_publish_failed"],
    ["revoke", "err_unpublish_failed"],
    ["copy", "err_copy_failed"],
  ] as const)("uses a localized %s fallback", (operation, key) => {
    expect(
      kbTeamTemplateError(
        new MatrixApiError(
          422,
          "future_code",
          "RAW_BACKEND_DIAGNOSTIC",
          {},
        ),
        operation,
      ),
    ).toBe(key);
  });
});
