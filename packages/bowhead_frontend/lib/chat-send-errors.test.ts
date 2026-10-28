import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CHAT_SEND_BACKEND_CODES,
  CHAT_SEND_REASON_CODES,
  getChatSendErrorMessage,
  resolveChatSendErrorReason,
} from "./chat-send-errors";

afterEach(() => {
  document.documentElement.lang = "";
});

describe("chat send HTTP error mapping", () => {
  it.each(CHAT_SEND_BACKEND_CODES)(
    "maps backend code %s without collapsing it to a status fallback",
    (code) => {
      expect(resolveChatSendErrorReason(code, 500)).toBe(code);
    },
  );

  it.each(["zh-CN", "en-US", "de-DE", "fr-FR"] as const)(
    "has a safe non-code message for every backend code in %s",
    (locale) => {
      document.documentElement.lang = locale;
      for (const code of CHAT_SEND_BACKEND_CODES) {
        const message = getChatSendErrorMessage(code);
        expect(message.trim(), code).not.toBe("");
        expect(message, code).not.toBe(code);
        if (code.includes("_")) {
          expect(message, code).not.toContain(code);
        }
        expect(message, code).not.toMatch(/\bHTTP\s+\d{3}\b/i);
      }
    },
  );

  it.each([
    [400, "chat_send_invalid_request"],
    [401, "auth_required"],
    [402, "chat_send_entitlement_denied"],
    [403, "chat_send_access_denied"],
    [404, "chat_send_resource_unavailable"],
    [409, "chat_send_conflict"],
    [410, "chat_send_resource_unavailable"],
    [422, "chat_send_invalid_request"],
    [429, "chat_send_rate_limited"],
    [503, "chat_send_service_unavailable"],
  ] as const)("maps an unknown HTTP %i to %s", (status, reason) => {
    expect(resolveChatSendErrorReason("future_backend_code", status)).toBe(
      reason,
    );
  });

  it.each(["en-US", "de-DE", "fr-FR"] as const)(
    "never falls back to Chinese for any send reason in %s",
    (locale) => {
      document.documentElement.lang = locale;
      for (const code of [...CHAT_SEND_REASON_CODES, "future_code"]) {
        expect(getChatSendErrorMessage(code), code).not.toMatch(/\p{Script=Han}/u);
      }
    },
  );

  it("keeps the Chinese copy of each send reason", () => {
    document.documentElement.lang = "zh-CN";
    expect(getChatSendErrorMessage("session_deleted")).toBe(
      "该会话已被删除，无法继续发送消息；请新开会话。",
    );
    expect(getChatSendErrorMessage("message_too_long")).toBe(
      "消息不能超过 10,000 个字符，请缩短后再发送。",
    );
    expect(getChatSendErrorMessage(undefined)).toBe("发送失败，请检查网络后重试。");
    expect(getChatSendErrorMessage("future_code")).toBe("发送失败，请检查网络后重试。");
  });

  it("uses a localized network fallback without showing the thrown text", () => {
    document.documentElement.lang = "en-US";

    const reason = resolveChatSendErrorReason(undefined, undefined);

    expect(reason).toBe("send_failed");
    expect(getChatSendErrorMessage(reason)).toBe(
      "The message could not be sent. Check your network and try again.",
    );
  });
});

// ─── credential-readiness D5: team_model_unconfigured role branching (A0a–A0c) ────

vi.mock("./auth-api", () => ({
  fetchMe: vi.fn(),
  fetchWithRefresh: vi.fn(),
}));

describe("resolveModelUnconfiguredPresentation (role fork)", () => {
  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    document.documentElement.lang = "";
  });

  async function arrange(teamRole: string | null, contacts?: string[]) {
    const authApi = await import("./auth-api");
    vi.mocked(authApi.fetchMe).mockResolvedValue({
      team_role: teamRole,
      is_team_admin: false,
    } as never);
    vi.mocked(authApi.fetchWithRefresh).mockResolvedValue({
      ok: true,
      json: async () => ({ contacts: { model_config: contacts ?? [] } }),
    } as never);
    const { resolveModelUnconfiguredPresentation } = await import("./chat-send-errors");
    return resolveModelUnconfiguredPresentation();
  }

  it("A0a/A0b zh-CN: owner 拿到第一人称可操作文案 + canAct（不再被指去找管理员）", async () => {
    document.documentElement.lang = "zh-CN";
    const p = await arrange("owner");
    expect(p.canAct).toBe(true);
    expect(p.message).toContain("团队设置 · 模型");
    expect(p.message).not.toContain("请管理员");
  });

  it("A0a en-US: team_admin gets actionable copy + canAct", async () => {
    document.documentElement.lang = "en-US";
    const p = await arrange("team_admin");
    expect(p.canAct).toBe(true);
    expect(p.message).toContain("Team Settings · Models");
  });

  it.each(["en-US", "de-DE", "fr-FR"] as const)(
    "A0a %s: member copy names contacts without Chinese separators",
    async (locale) => {
      document.documentElement.lang = locale;
      // Past clock: contacts cached by this case will not be reused by later cases.
      vi.spyOn(Date, "now").mockReturnValue(new Date().getTime() - 600_000);
      const p = await arrange("member", ["Alice", "Bob"]);
      expect(p.canAct).toBe(false);
      expect(p.message).toContain("Alice, Bob");
      expect(p.message).not.toMatch(/\p{Script=Han}|、/u);
    },
  );

  it("A0a zh-CN: member 落无权分支，点名最小联系人集合", async () => {
    document.documentElement.lang = "zh-CN";
    const p = await arrange("member", ["张三", "李四"]);
    expect(p.canAct).toBe(false);
    expect(p.message).toContain("团队模型配置权限"); // Q9: state the resource requirement
    expect(p.message).toContain("张三、李四");
    expect(p.message).not.toContain("你不是管理员");
  });

  it("A0c: audit_admin 落无权分支（名字带 admin 不等于能配模型）", async () => {
    document.documentElement.lang = "zh-CN";
    const p = await arrange("audit_admin");
    expect(p.canAct).toBe(false);
    expect(p.message).toContain("团队模型配置权限");
  });

  it("contacts 端点失败不影响主文案（无点名，无报错）", async () => {
    document.documentElement.lang = "zh-CN";
    // Module-level 60s contacts cache retains the previous case's list; advance the clock to expire it (no test-only backdoor).
    vi.spyOn(Date, "now").mockReturnValue(new Date().getTime() + 120_000);
    const authApi = await import("./auth-api");
    vi.mocked(authApi.fetchMe).mockResolvedValue({ team_role: "member", is_team_admin: false } as never);
    vi.mocked(authApi.fetchWithRefresh).mockRejectedValue(new Error("boom"));
    const { resolveModelUnconfiguredPresentation } = await import("./chat-send-errors");
    const p = await resolveModelUnconfiguredPresentation();
    expect(p.canAct).toBe(false);
    expect(p.message).toContain("团队模型配置权限");
    expect(p.message).not.toContain("可联系");
  });

  it("fetchMe 失败回落角色中性基线（never throws）", async () => {
    document.documentElement.lang = "zh-CN";
    const authApi = await import("./auth-api");
    vi.mocked(authApi.fetchMe).mockRejectedValue(new Error("401"));
    const { resolveModelUnconfiguredPresentation } = await import("./chat-send-errors");
    const p = await resolveModelUnconfiguredPresentation();
    expect(p.canAct).toBe(false);
    expect(p.message).toBe(getChatSendErrorMessage("team_model_unconfigured"));
  });

  it.each(["zh-CN", "en-US"] as const)(
    "基线文案角色中性钉住（%s）——不写「请管理员/Ask an administrator」",
    (locale) => {
      document.documentElement.lang = locale;
      const base = getChatSendErrorMessage("team_model_unconfigured");
      expect(base).not.toContain("请管理员");
      expect(base).not.toContain("Ask an administrator");
    },
  );
});
