import { describe, expect, it } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { ApiError } from "./api-client";
import {
  MODEL_SETTINGS_ERROR_KEY,
  MODEL_SETTINGS_VALIDATION_ERROR_KEY,
  modelSettingsErrorKey,
  modelSettingsValidationErrorKey,
} from "./model-settings-error";

describe("model settings error contract", () => {
  it("has non-empty zh-CN and en-US copy for every finite mapping", () => {
    const zh = messagesZh.settings.models.errors as Record<string, unknown>;
    const en = messagesEn.settings.models.errors as Record<string, unknown>;
    const keys = new Set([
      ...Object.values(MODEL_SETTINGS_ERROR_KEY),
      ...Object.values(MODEL_SETTINGS_VALIDATION_ERROR_KEY),
      "network_error",
      "err_generic",
      "err_invalid_fields",
      "validation_invalid_config",
      "validation_warning",
    ]);

    for (const key of keys) {
      expect(zh[key], `missing zh-CN settings.models.errors.${key}`).toEqual(
        expect.any(String),
      );
      expect(en[key], `missing en-US settings.models.errors.${key}`).toEqual(
        expect.any(String),
      );
      expect((zh[key] as string).trim()).not.toBe("");
      expect((en[key] as string).trim()).not.toBe("");
    }
  });

  it("maps HTTP failures without reading backend prose", () => {
    expect(
      modelSettingsErrorKey(
        new ApiError(
          409,
          "RAW_BACKEND_VERSION_MESSAGE",
          { code: "config_version_conflict" },
        ),
      ),
    ).toBe("err_config_version_conflict");
    expect(
      modelSettingsErrorKey(
        new ApiError(
          409,
          "RAW_BACKEND_PROVIDER_MESSAGE",
          { code: "provider_not_testable" },
        ),
      ),
    ).toBe("err_provider_not_testable");
    expect(
      modelSettingsErrorKey(
        new ApiError(
          409,
          "RAW_BACKEND_READINESS_MESSAGE",
          { code: "readiness_review_stale" },
        ),
      ),
    ).toBe("err_review_stale");
    for (const code of [
      "verification_plan_expired",
      "verification_plan_not_found",
    ]) {
      expect(
        modelSettingsErrorKey(new ApiError(409, "RAW_PLAN_MESSAGE", { code })),
      ).toBe("err_verification_stale");
    }
    expect(
      modelSettingsErrorKey(
        new ApiError(
          502,
          "RAW_BACKEND_DISCOVERY_MESSAGE",
          { code: "discovery_auth_failed" },
        ),
      ),
    ).toBe("err_discovery_failed");
  });

  it("uses safe validation, unknown API, and network fallbacks", () => {
    expect(
      modelSettingsErrorKey(
        new ApiError(422, "Input should be a valid dictionary"),
      ),
    ).toBe("err_invalid_fields");
    expect(
      modelSettingsErrorKey(
        new ApiError(
          500,
          "traceback detail",
          { code: "future_internal_code" },
        ),
      ),
    ).toBe("err_generic");
    expect(modelSettingsErrorKey(new TypeError("Failed to fetch"))).toBe(
      "network_error",
    );
  });

  it("maps validation issue codes and safely handles future issues", () => {
    expect(modelSettingsValidationErrorKey("blocked_url", "error")).toBe(
      "validation_blocked_url",
    );
    expect(modelSettingsValidationErrorKey("future_warning", "warning")).toBe(
      "validation_warning",
    );
    expect(modelSettingsValidationErrorKey("future_error", "error")).toBe(
      "validation_invalid_config",
    );
  });
});
