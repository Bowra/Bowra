import { describe, expect, it } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { ApiError } from "./api-client";
import {
  emailConfigErrorKeys,
  emailTestErrorKey,
} from "./email-config-error";

describe("email config error contract", () => {
  it("localizes every returned key in zh-CN and en-US", () => {
    const errors = [
      new TypeError("network failed"),
      new ApiError(500, "raw"),
      new ApiError(422, "raw"),
      new ApiError(
        500,
        "raw",
        { code: "email_config_encryption_required" },
      ),
      new ApiError(
        422,
        "raw",
        {
          code: "invalid_smtp_config",
          body: {
            detail: {
              issues: [
                { code: "smtp_host_required" },
                { code: "smtp_port_unsupported" },
              ],
            },
          },
        },
      ),
    ];
    const zh = messagesZh.settings.integrations as Record<string, unknown>;
    const en = messagesEn.settings.integrations as Record<string, unknown>;

    for (const error of errors) {
      for (const key of emailConfigErrorKeys(error)) {
        expect(zh[key], `missing zh-CN settings.integrations.${key}`).toEqual(
          expect.any(String),
        );
        expect(en[key], `missing en-US settings.integrations.${key}`).toEqual(
          expect.any(String),
        );
      }
    }
  });

  it("maps structured validation issues and ignores backend prose", () => {
    const error = new ApiError(
      422,
      "RAW_BACKEND_INVALID_SMTP_CONFIG",
      {
        code: "invalid_smtp_config",
        body: {
          detail: {
            issues: [
              {
                code: "smtp_host_blocked",
                message: "RAW_SSRF_DIAGNOSTIC",
              },
              {
                code: "smtp_password_required",
                message: "RAW_PASSWORD_DIAGNOSTIC",
              },
            ],
          },
        },
      },
    );

    expect(emailConfigErrorKeys(error)).toEqual([
      "email_err_smtp_host_blocked",
      "email_err_smtp_password_required",
    ]);
  });

  it("maps successful test failures by stable error_code", () => {
    expect(emailTestErrorKey("email_test_delivery_failed")).toBe(
      "email_err_test_delivery_failed",
    );
    expect(emailTestErrorKey("email_test_env_delivery_failed")).toBe(
      "email_err_test_env_delivery_failed",
    );
    expect(emailTestErrorKey("future_internal_code")).toBe(
      "email_err_generic",
    );
  });
});
