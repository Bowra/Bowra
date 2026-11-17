import { afterEach, describe, expect, it } from "vitest";

import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import fr from "@/messages/fr-FR.json";
import zh from "@/messages/zh-CN.json";

import { ApiError, HttpStatusError } from "./api-client";
import type { Locale } from "./locale";
import {
  codedErrorMessage,
  genericUserFacingErrorMessage,
  httpFailureKind,
  safeUserFacingErrorMessage,
  withHttpFailureReason,
  withoutOwnNextStep,
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

describe("a reason replaces the surface's own next step", () => {
  const unavailable = new ApiError(502, "x", { nonApiResponse: true });
  const reasonEn = "The service is temporarily unavailable, possibly restarting or under maintenance. Try again shortly.";
  const reasonZh = "服务暂时不可用，可能正在重启或维护，请稍后重试。";

  it("drops a retry or refresh instruction written for an unknown cause", () => {
    // Refreshing would discard an unsaved edit; the reason says to try again shortly.
    expect(withHttpFailureReason("Could not save changes. Refresh and try again.", unavailable, { locale: "en-US" })).toBe(
      `Could not save changes. ${reasonEn}`,
    );
    expect(withHttpFailureReason("Delete failed, please retry", unavailable, { locale: "en-US" })).toBe(`Delete failed. ${reasonEn}`);
    expect(withHttpFailureReason("Approve failed — please retry", unavailable, { locale: "en-US" })).toBe(`Approve failed. ${reasonEn}`);
    expect(withHttpFailureReason("保存失败。请刷新后重试。", unavailable, { locale: "zh-CN" })).toBe(`保存失败。${reasonZh}`);
    expect(withHttpFailureReason("暂时无法加载这个智能体，请稍后重试。", unavailable, { locale: "zh-CN" })).toBe(
      `暂时无法加载这个智能体。${reasonZh}`,
    );
  });

  it("keeps the facts the surface states and drops a check that does not apply", () => {
    expect(
      withHttpFailureReason("Send readiness could not be confirmed. Your draft was preserved; try again later.", new ApiError(429, "x"), {
        locale: "en-US",
      }),
    ).toBe("Send readiness could not be confirmed. Your draft was preserved. Too many requests. Wait a moment and try again.");
    expect(withHttpFailureReason("暂时无法确认发送条件，草稿已保留，请稍后重试。", unavailable, { locale: "zh-CN" })).toBe(
      `暂时无法确认发送条件，草稿已保留。${reasonZh}`,
    );
    expect(withHttpFailureReason("暂时无法引用图表数据。草稿已保留，请重新打开图表后重试。", unavailable, { locale: "zh-CN" })).toBe(
      `暂时无法引用图表数据。草稿已保留。${reasonZh}`,
    );
    expect(
      withHttpFailureReason("Couldn't save. Before enabling, check all required fields and the Azure Bot configuration.", unavailable, {
        locale: "en-US",
      }),
    ).toBe(`Couldn't save. ${reasonEn}`);
    expect(withHttpFailureReason("保存失败。启用前请确认所有必填项和 Azure Bot 配置。", unavailable, { locale: "zh-CN" })).toBe(
      `保存失败。${reasonZh}`,
    );
    const statusOnly = "Couldn't load approvals. The page retries automatically, or you can select Refresh.";
    expect(withHttpFailureReason(statusOnly, unavailable, { locale: "en-US" })).toBe(`${statusOnly} ${reasonEn}`);
    expect(withoutOwnNextStep("邀请确认邮件没有发出，申请先由管理员审核。", "zh-CN")).toBe("邀请确认邮件没有发出，申请先由管理员审核。");
  });

  it("applies the same rule in German and French", () => {
    expect(
      withHttpFailureReason(
        "Änderungen konnten nicht gespeichert werden. Aktualisieren Sie die Seite und versuchen Sie es erneut.",
        unavailable,
        { locale: "de-DE" },
      ),
    ).toBe(
      "Änderungen konnten nicht gespeichert werden. Der Dienst ist vorübergehend nicht verfügbar, möglicherweise wegen eines Neustarts oder einer Wartung. Versuchen Sie es in Kürze erneut.",
    );
    expect(
      withHttpFailureReason(
        "Impossible de confirmer que l’envoi est prêt. Votre brouillon est conservé ; réessayez plus tard.",
        unavailable,
        { locale: "fr-FR" },
      ),
    ).toBe(
      "Impossible de confirmer que l’envoi est prêt. Votre brouillon est conservé. Le service est temporairement indisponible, peut-être en cours de redémarrage ou de maintenance. Réessayez dans un instant.",
    );
  });

  it("keeps the next step when no reason is known", () => {
    const message = "Could not save changes. Refresh and try again.";
    expect(withHttpFailureReason(message, new TypeError("Failed to fetch"), { locale: "en-US" })).toBe(message);
    expect(withHttpFailureReason(message, new ApiError(409, "x", { code: "stale" }), { locale: "en-US" })).toBe(message);
  });

  it("drops the next step of the shared settings and composer copy in every interface language", () => {
    const catalogs: Record<Locale, unknown> = { "en-US": en, "zh-CN": zh, "de-DE": de, "fr-FR": fr };
    const read = (catalog: unknown, key: string) =>
      key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], catalog) as string;
    for (const [locale, catalog] of Object.entries(catalogs) as [Locale, unknown][]) {
      for (const key of ["settings.space_general.save_failed", "settings.profile.load_failed", "composer.action_preflight_failed"]) {
        const message = read(catalog, key);
        const failed = withoutOwnNextStep(message, locale);
        expect(failed, `${locale} ${key}`).not.toBe(message);
        expect(message.startsWith(failed), `${locale} ${key}`).toBe(true);
      }
    }
  });

  it("only ever shortens a catalog message and keeps what failed", () => {
    const catalogs: Record<Locale, unknown> = { "en-US": en, "zh-CN": zh, "de-DE": de, "fr-FR": fr };
    const strings = (node: unknown): string[] =>
      typeof node === "string" ? [node] : node && typeof node === "object" ? Object.values(node).flatMap(strings) : [];
    for (const [locale, catalog] of Object.entries(catalogs) as [Locale, unknown][]) {
      for (const message of strings(catalog)) {
        const failed = withoutOwnNextStep(message.trim(), locale);
        expect(message.trim().startsWith(failed), `${locale}: ${message}`).toBe(true);
        if (message.trim()) expect(failed, `${locale}: ${message}`).not.toBe("");
      }
    }
  });
});

describe("a save that kept losing its storage lock", () => {
  it("names the busy server as the reason and drops the page's own next step", () => {
    const busy = new ApiError(503, "catalog copy", { code: "store_busy" });
    expect(httpFailureKind(busy)).toBe("store_busy");
    expect(withHttpFailureReason("Could not save changes. Refresh and try again.", busy, { locale: "en-US" })).toBe(
      "Could not save changes. The server was busy with other work, so this change was not saved and nothing was lost. Try again in a few seconds.",
    );
    expect(withHttpFailureReason("保存失败。请刷新后重试。", busy, { locale: "zh-CN" })).toBe(
      "保存失败。服务器正忙于其他任务，这次修改没有保存，也没有丢失任何内容。请几秒后重试。",
    );
  });

  it("keeps other coded 503 answers on the surface's own copy", () => {
    const coded = new ApiError(503, "catalog copy", { code: "capability_store_busy" });
    expect(httpFailureKind(coded)).toBeNull();
  });
});
