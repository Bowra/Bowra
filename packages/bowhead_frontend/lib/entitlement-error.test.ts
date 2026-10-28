import { describe, expect, it } from "vitest";

import { entitlementErrorMessage } from "./entitlement-error";

describe("entitlementErrorMessage", () => {
  it("uses the finite feature identifier instead of backend prose", () => {
    expect(
      entitlementErrorMessage(
        {
          code: "feature_unlicensed",
          body: {
            detail: {
              error: { details: { feature: "mcp" } },
            },
          },
        },
        "zh-CN",
      ),
    ).toBe(
      "当前许可证未启用 MCP 功能，暂时无法使用。请联系管理员使用包含该功能的许可证更新授权。",
    );
  });

  it("falls back safely for an unknown feature identifier", () => {
    expect(
      entitlementErrorMessage(
        {
          code: "feature_unlicensed",
          body: {
            detail: {
              error: { details: { feature: "private_internal_feature" } },
            },
          },
        },
        "en-US",
      ),
    ).toBe(
      "The current license does not include this feature, so it is unavailable. Ask an administrator to update the license with this feature.",
    );
  });

  it("maps a non-retryable license failure in English", () => {
    expect(
      entitlementErrorMessage({ code: "needs_license" }, "en-US"),
    ).toBe(
      "This deployment has not completed license activation, so this feature is unavailable. Ask an administrator to upload and activate a license.",
    );
  });

  it("keeps the existing German copy", () => {
    expect(
      entitlementErrorMessage(
        { code: "feature_unlicensed", body: { detail: { error: { details: { feature: "kb" } } } } },
        "de-DE",
      ),
    ).toBe(
      "Die aktuelle Lizenz umfasst Wissensdatenbank nicht. Wenden Sie sich an die Administration, um eine passende Lizenz bereitzustellen.",
    );
    expect(entitlementErrorMessage({ code: "suspended" }, "de-DE")).toBe(
      "Dieses Team wurde vorübergehend deaktiviert. Wenden Sie sich an die Administration, um den Teamzugriff wiederherzustellen.",
    );
  });

  it.each(["zh-CN", "en-US", "de-DE", "fr-FR"] as const)("maps every finite code in %s", (locale) => {
    const codes = [
      "entitlement_unavailable",
      "needs_license",
      "over_grace",
      "suspended",
      "tamper",
      "clock_tamper",
      "seat_exceeded",
      "user_credit_exhausted",
      "team_pool_exhausted",
      "feature_disabled",
    ];
    for (const code of codes) {
      const message = entitlementErrorMessage({ code }, locale);
      expect(message?.trim(), code).toBeTruthy();
      if (locale !== "zh-CN") expect(message, code).not.toMatch(/\p{Script=Han}/u);
    }
    for (const feature of ["mcp", "kb", "scheduling", "audit_export", "custom_branding"] as const) {
      const message = entitlementErrorMessage({ code: "feature_unlicensed" }, locale, { featureFallback: feature });
      if (locale !== "zh-CN") expect(message, feature).not.toMatch(/\p{Script=Han}/u);
    }
    expect(entitlementErrorMessage({ code: "not_an_entitlement_code" }, locale)).toBeNull();
    expect(entitlementErrorMessage({ code: "constructor" }, locale)).toBeNull();
  });

  it("keeps the Chinese copy for a code and a feature fallback", () => {
    expect(entitlementErrorMessage({ code: "seat_exceeded" }, "zh-CN")).toBe(
      "当前团队可用席位已满，无法完成此操作。请联系管理员释放席位或更新许可证扩容。",
    );
    expect(entitlementErrorMessage({ code: "feature_unlicensed" }, "zh-CN", { featureFallback: "scheduling" })).toBe(
      "当前许可证未启用 定时任务功能，暂时无法使用。请联系管理员使用包含该功能的许可证更新授权。",
    );
  });
});
