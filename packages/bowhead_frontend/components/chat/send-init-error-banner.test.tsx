import "@testing-library/jest-dom/vitest";
import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getCurrentLocale } from "@/lib/locale";
import { LOCALE_MESSAGES } from "@/lib/locale-messages";

import { SendInitErrorBanner } from "./send-init-error-banner";

// Match production: banner copy (pure lookup) and「去配置」(useTranslations) use the same UI language.
function render(ui: ReactElement) {
  const locale = getCurrentLocale();
  return rtlRender(
    <NextIntlClientProvider locale={locale} messages={LOCALE_MESSAGES[locale]}>
      {ui}
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  document.documentElement.lang = "";
});

describe("SendInitErrorBanner", () => {
  it("keeps a rejected send visibly explained after the toast disappears", () => {
    document.documentElement.lang = "en-US";

    render(<SendInitErrorBanner reasonCode="user_credit_exhausted" />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "used up your credits",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(
      "user_credit_exhausted",
    );
  });

  it("renders a Chinese recovery action for a space denial", () => {
    document.documentElement.lang = "zh-CN";

    render(<SendInitErrorBanner reasonCode="ws_access_denied" />);

    expect(screen.getByRole("alert")).toHaveTextContent("返回空间列表");
    expect(screen.getByRole("alert")).not.toHaveTextContent("HTTP 403");
  });

  it("renders nothing without a send-init error", () => {
    const { container } = render(<SendInitErrorBanner reasonCode={null} />);

    expect(container).toBeEmptyDOMElement();
  });
});

// ─── credential-readiness D5: branch team_model_unconfigured on can_act (A0a–A0c DOM) ──
// Mock only auth-api (roles/contacts source); use real presentation logic.

vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn(),
  fetchWithRefresh: vi.fn(),
}));

describe("SendInitErrorBanner · team_model_unconfigured role fork", () => {
  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  async function arrange(teamRole: string, contacts: string[] = []) {
    // Module-level 60s contacts cache persists across cases — advance the clock to invalidate it.
    vi.spyOn(Date, "now").mockReturnValue(new Date().getTime() + 300_000 * Math.random() + 120_000);
    const authApi = await import("@/lib/auth-api");
    vi.mocked(authApi.fetchMe).mockResolvedValue({ team_role: teamRole, is_team_admin: false } as never);
    vi.mocked(authApi.fetchWithRefresh).mockResolvedValue({
      ok: true,
      json: async () => ({ contacts: { model_config: contacts } }),
    } as never);
  }

  it("A0a zh-CN admin: 升级为第一人称文案 + 「去配置」直达链接", async () => {
    document.documentElement.lang = "zh-CN";
    await arrange("owner");
    render(<SendInitErrorBanner reasonCode="team_model_unconfigured" />);
    const cta = await screen.findByTestId("send-init-error-configure");
    expect(cta).toHaveAttribute("href", expect.stringContaining("/team/settings/models/"));
    expect(screen.getByRole("alert")).toHaveTextContent("团队设置 · 模型");
    expect(screen.getByRole("alert")).not.toHaveTextContent("请管理员");
  });

  it("A0a en-US admin: Configure CTA", async () => {
    document.documentElement.lang = "en-US";
    await arrange("team_admin");
    render(<SendInitErrorBanner reasonCode="team_model_unconfigured" />);
    const cta = await screen.findByTestId("send-init-error-configure");
    expect(cta).toHaveTextContent("Configure");
  });

  it("A0a de-DE admin: 德语界面不落回中文", async () => {
    document.documentElement.lang = "de-DE";
    await arrange("owner");
    render(<SendInitErrorBanner reasonCode="team_model_unconfigured" />);
    await screen.findByTestId("send-init-error-configure");
    expect(screen.getByRole("alert").textContent).not.toMatch(/\p{Script=Han}/u);
  });

  it("A0a zh-CN member: 无 CTA，点名联系人", async () => {
    document.documentElement.lang = "zh-CN";
    await arrange("member", ["张三"]);
    render(<SendInitErrorBanner reasonCode="team_model_unconfigured" />);
    await screen.findByText(/张三/);
    expect(screen.queryByTestId("send-init-error-configure")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("团队模型配置权限");
  });

  it("A0c: audit_admin 落无权分支（无 CTA）", async () => {
    document.documentElement.lang = "zh-CN";
    await arrange("audit_admin");
    render(<SendInitErrorBanner reasonCode="team_model_unconfigured" />);
    await screen.findByText(/团队模型配置权限/);
    expect(screen.queryByTestId("send-init-error-configure")).not.toBeInTheDocument();
  });

  it("升级完成前先渲染角色中性基线（不闪 code、不空白）", async () => {
    document.documentElement.lang = "zh-CN";
    const authApi = await import("@/lib/auth-api");
    vi.mocked(authApi.fetchMe).mockReturnValue(new Promise(() => {}) as never); // Never resolves.
    render(<SendInitErrorBanner reasonCode="team_model_unconfigured" />);
    expect(screen.getByRole("alert")).toHaveTextContent("团队还未配置模型");
    expect(screen.getByRole("alert")).not.toHaveTextContent("team_model_unconfigured");
  });
});
