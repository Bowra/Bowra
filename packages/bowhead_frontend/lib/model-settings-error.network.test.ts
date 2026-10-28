/**
 * Not an ApiError ≠ network failure.
 *
 * 2026-08-14 evidence on 116: `settings/models.tsx` called secure-context-only
 * `crypto.randomUUID` over plain HTTP and threw TypeError (**no request was sent**); old criterion classified all such errors as
 * `network_error` → displayed “网络连接失败，请检查网络后重试”. The real issue (capability verification never
 * succeeded, evidence table had 0 rows, model configuration could not save) was disguised as the user's network problem.
 */

import { describe, expect, it } from "vitest";
import { ApiError } from "./api-client";
import { modelSettingsErrorKey } from "./model-settings-error";

describe("modelSettingsErrorKey 对非 ApiError 的归类", () => {
  it("真·网络失败仍报 network_error", () => {
    // Actual browser fetch connection-failure shapes (browser-specific wording).
    for (const message of [
      "Failed to fetch",
      "NetworkError when attempting to fetch resource.",
      "Load failed",
    ]) {
      expect(modelSettingsErrorKey(new TypeError(message))).toBe("network_error");
    }
  });

  it("前端自己崩的 TypeError 不再冒充网络失败", () => {
    // The actual error thrown on 116.
    const crashed = new TypeError("crypto.randomUUID is not a function");

    expect(modelSettingsErrorKey(crashed)).toBe("err_generic");
  });

  it("其它意外异常同样归 err_generic", () => {
    expect(modelSettingsErrorKey(new RangeError("boom"))).toBe("err_generic");
    expect(modelSettingsErrorKey("plain string")).toBe("err_generic");
    expect(modelSettingsErrorKey(undefined)).toBe("err_generic");
  });

  it("ApiError 的既有映射不受影响", () => {
    // Negative control: only the non-ApiError branch should change.
    const conflict = new ApiError(409, "conflict", { code: "config_version_conflict" });

    expect(modelSettingsErrorKey(conflict)).toBe("err_config_version_conflict");
  });
});

describe("网关拦截与应用错误必须分得开", () => {
  it("HTML 错误页（请求没到应用）→ err_gateway_blocked，不得混进 err_generic", () => {
    // A gateway may reject a request with an HTML 403 before it reaches the application.
    const blocked = new ApiError(403, "Forbidden", { nonApiResponse: true });

    expect(modelSettingsErrorKey(blocked)).toBe("err_gateway_blocked");
  });

  it("同为 403、但是本应用答的，仍走业务映射（负对照）", () => {
    const appForbidden = new ApiError(403, "forbidden", { code: "insufficient_role" });

    expect(modelSettingsErrorKey(appForbidden)).toBe("err_insufficient_role");
  });

  it("认不出的应用错误仍是 err_generic，不得冒充网关拦截（负对照）", () => {
    const unknown = new ApiError(500, "boom", { code: "something_new" });

    expect(modelSettingsErrorKey(unknown)).toBe("err_generic");
  });
});
