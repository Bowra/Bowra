import { afterEach, describe, expect, it } from "vitest";

import { ApiError, HttpStatusError } from "./api-client";
import {
  codedErrorMessage,
  genericUserFacingErrorMessage,
  httpFailureKind,
  safeUserFacingErrorMessage,
  withHttpFailureReason,
  attachHttpCause,
} from "./user-facing-error";

afterEach(() => {
  document.documentElement.lang = "";
});

describe("user-facing error fallback", () => {
  it("uses the Chinese generic message for the Chinese UI", () => {
    document.documentElement.lang = "zh-CN";
    expect(genericUserFacingErrorMessage()).toBe(
      "操作失败，请稍后重试；如问题持续，请联系管理员。",
    );
  });

  it("uses the English generic message for the English UI", () => {
    document.documentElement.lang = "en-US";
    expect(genericUserFacingErrorMessage()).toBe(
      "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    );
  });

  it("does not expose a backend code as the visible message", () => {
    document.documentElement.lang = "en-US";
    expect(
      safeUserFacingErrorMessage("team_not_found", "team_not_found"),
    ).not.toContain("team_not_found");
  });

  it("uses the French generic message when an internal code needs a fallback", () => {
    document.documentElement.lang = "fr-FR";
    expect(safeUserFacingErrorMessage("team_not_found", "team_not_found")).toBe(
      "Une erreur s’est produite. Réessayez. Si le problème persiste, contactez votre administrateur.",
    );
  });

  it("preserves a real backend message", () => {
    expect(
      safeUserFacingErrorMessage(
        "This invitation has expired.",
        "invite_expired",
      ),
    ).toBe("This invitation has expired.");
  });
});

describe("uncoded HTTP failure kinds", () => {
  it.each([
    [502, false, "service_unavailable"],
    [503, false, "service_unavailable"],
    [504, false, "gateway_timeout"],
    [500, false, "server_error"],
    [429, false, "rate_limited"],
    [413, false, "payload_too_large"],
    [403, true, "blocked_by_gateway"],
    [403, false, null],
    [404, false, null],
    [409, false, null],
  ] as const)("classifies status %i (HTML page: %s) as %s", (status, nonApiResponse, kind) => {
    expect(httpFailureKind(new ApiError(status, "x", { nonApiResponse }))).toBe(kind);
  });

  it("treats the Gateway's generic code as uncoded but keeps every specific code", () => {
    expect(httpFailureKind(new ApiError(500, "x", { code: "E_USER_VISIBLE_GENERIC" }))).toBe("server_error");
    expect(httpFailureKind(new ApiError(503, "x", { code: "kb_embedding_not_configured" }))).toBeNull();
  });

  it("classifies handwritten fetch failures but not client errors that reuse a status", () => {
    expect(httpFailureKind(new HttpStatusError("load", new Response(null, { status: 503 })))).toBe("service_unavailable");
    expect(httpFailureKind(Object.assign(new Error("Unsupported Subagent response"), { status: 502 }))).toBeNull();
    expect(httpFailureKind(new TypeError("Failed to fetch"))).toBeNull();
  });

  it("adds the reason after the surface's own sentence", () => {
    const error = new ApiError(502, "x", { nonApiResponse: true });
    expect(withHttpFailureReason("你的回答没有提交，选择已保留。", error, { locale: "zh-CN" })).toBe(
      "你的回答没有提交，选择已保留。服务暂时不可用，可能正在重启或维护，请稍后重试。",
    );
    expect(withHttpFailureReason("Couldn't save", error, { locale: "en-US" })).toBe(
      "Couldn't save. The service is temporarily unavailable, possibly restarting or under maintenance. Try again shortly.",
    );
  });

  it("replaces the generic message and any outcome claim after a timeout", () => {
    expect(withHttpFailureReason(
      "Something went wrong. Please try again. If the problem continues, contact your administrator.",
      new ApiError(429, "x"),
      { locale: "en-US" },
    )).toBe("Too many requests. Wait a moment and try again.");
    expect(withHttpFailureReason("Your answer wasn't submitted.", new ApiError(504, "x"), { locale: "en-US" })).toBe(
      "The service took too long to respond, so the result is unconfirmed. Refresh to check before trying again.",
    );
  });

  it("keeps a read failure's sentence after a timeout", () => {
    expect(withHttpFailureReason("Couldn't load members.", new ApiError(504, "x"), { locale: "en-US", read: true })).toBe(
      "Couldn't load members. The service took too long to respond. Refresh to try again shortly.",
    );
    expect(withHttpFailureReason("成员加载失败", new ApiError(502, "x"), { locale: "zh-CN", read: true })).toBe(
      "成员加载失败。服务暂时不可用，可能正在重启或维护，请稍后重试。",
    );
  });

  it("does not repeat a reason the message already carries", () => {
    const reason = "The service is temporarily unavailable, possibly restarting or under maintenance. Try again shortly.";
    expect(withHttpFailureReason(reason, new ApiError(503, reason), { locale: "en-US" })).toBe(reason);
  });

  it("classifies the response a domain error keeps as its cause", () => {
    const response = new ApiError(502, "x", { nonApiResponse: true });
    const domain = attachHttpCause(Object.assign(new Error("members"), { name: "AdminMembersError", status: 502, code: "error" }), response);
    expect(httpFailureKind(domain)).toBe("service_unavailable");
    const coded = attachHttpCause(new Error("tools"), new ApiError(503, "x", { code: "tool_source_unavailable" }));
    expect(httpFailureKind(coded)).toBeNull();
    // A client-side wrapper without a response cause keeps its own copy.
    expect(httpFailureKind(Object.assign(new Error("drafts"), { name: "SubagentDraftApiError", status: 502 }))).toBeNull();
  });

  it("leaves coded and non-HTTP failures unchanged", () => {
    expect(withHttpFailureReason("Couldn't save.", new ApiError(409, "x", { code: "stale" }), { locale: "en-US" })).toBe("Couldn't save.");
    expect(withHttpFailureReason("Couldn't save.", new TypeError("Failed to fetch"), { locale: "en-US" })).toBe("Couldn't save.");
  });

  it("explains an uncoded failure behind a surface fallback", () => {
    document.documentElement.lang = "en-US";
    expect(codedErrorMessage(new ApiError(503, "x"), "Couldn't save the plugin.", ["plugin_"])).toBe(
      "Couldn't save the plugin. The service is temporarily unavailable, possibly restarting or under maintenance. Try again shortly.",
    );
    expect(codedErrorMessage(new ApiError(409, "Plugin is in use.", { code: "plugin_in_use" }), "Couldn't save.", ["plugin_"])).toBe(
      "Plugin is in use.",
    );
  });
});
