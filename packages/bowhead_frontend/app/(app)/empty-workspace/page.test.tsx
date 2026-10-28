/** No-available-spaces page: pending invitations show space-role copy, never raw API values; error text has no status codes. */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";

const invites = vi.hoisted(() => ({
  items: [
    { invite_id: "i1", workspace_name: "华东分析", role: "admin" },
    { invite_id: "i2", workspace_name: "零售看板", role: "viewer" },
    { invite_id: "i3", workspace_name: "财务月报", role: "owner_legacy" },
  ],
}));
const state = vi.hoisted(() => ({
  acceptStatus: 200,
  createWorkspace: vi.fn(),
  diagnosticsEnabled: false,
}));

vi.mock("@/lib/auth-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  fetchMe: vi.fn(async () => ({ display_name: "Ada" })),
  fetchWithRefresh: vi.fn(async (input: string) =>
    input.endsWith("/accept")
      ? new Response(JSON.stringify({ detail: "Internal Server Error" }), {
          status: state.acceptStatus,
          headers: {
            "content-type": "application/json",
            "x-request-id": "0123456789abcdef0123456789abcdef",
          },
        })
      : new Response(JSON.stringify({ items: invites.items }), { status: 200, headers: { "content-type": "application/json" } }),
  ),
}));
vi.mock("@/lib/api", () => ({ createWorkspace: (name: string) => state.createWorkspace(name) }));
vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: () => state.diagnosticsEnabled,
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

import EmptyWorkspacePage from "./page";

// Formats such as “Accept Failed (500)”, “(404)” and “HTTP 502” all expose status codes to users.
const STATUS_CODE = /[（(]\s*[1-5]\d\d\s*[)）]|HTTP\s*[1-5]\d\d|\b[1-5]\d\d\b/;

afterEach(() => {
  cleanup();
  state.acceptStatus = 200;
  state.diagnosticsEnabled = false;
  state.createWorkspace.mockReset();
});

describe("EmptyWorkspacePage 邀请角色", () => {
  it("按空间角色文案显示，未知值显示为成员，不露原始标识", async () => {
    renderIntl(<EmptyWorkspacePage />);
    await screen.findByText("华东分析");
    const body = document.body.textContent ?? "";
    expect(body).not.toMatch(/\badmin\b|\bviewer\b|owner_legacy/);
    expect(screen.getByText("只读成员")).toBeTruthy();
  });
});

describe("EmptyWorkspacePage 出错文字", () => {
  const cases = [
    { locale: "zh-CN", accept: "没能接受这份邀请", create: "没能创建空间", acceptLabel: "接受", createLabel: "创建" },
    { locale: "en-US", accept: "Couldn't accept this invitation", create: "Couldn't create the space", acceptLabel: "Accept", createLabel: "Create" },
    { locale: "de-DE", accept: "Diese Einladung konnte nicht angenommen werden", create: "Der Bereich konnte nicht erstellt werden", acceptLabel: "Annehmen", createLabel: "Erstellen" },
    { locale: "fr-FR", accept: "Impossible d’accepter cette invitation", create: "Impossible de créer l’espace", acceptLabel: "Accepter", createLabel: "Créer" },
  ] as const;

  it.each(cases)("接受邀请失败时说明怎么办，不带状态码（$locale）", async ({ locale, accept, acceptLabel }) => {
    state.acceptStatus = 500;
    renderIntl(<EmptyWorkspacePage />, { locale });
    await screen.findByText("华东分析");

    fireEvent.click(screen.getAllByRole("button", { name: acceptLabel })[0]);

    const message = await screen.findByText(accept, { exact: false });
    expect(message.textContent).not.toMatch(STATUS_CODE);
    expect(document.body.textContent).not.toMatch(STATUS_CODE);
    if (locale !== "zh-CN") expect(document.body.textContent).not.toMatch(/没能|失败/);
  });

  it.each(cases)("创建空间失败时说明怎么办，不带状态码（$locale）", async ({ locale, create, createLabel }) => {
    state.createWorkspace.mockRejectedValue(new ApiError(503, "Service Unavailable"));
    renderIntl(<EmptyWorkspacePage />, { locale });
    await screen.findByText("华东分析");

    fireEvent.click(screen.getByRole("button", { name: createLabel }));

    const message = await screen.findByText(create, { exact: false });
    expect(message.textContent).not.toMatch(STATUS_CODE);
  });

  it("诊断开关打开时，状态码和请求 ID 只出现在「诊断信息」折叠区", async () => {
    state.acceptStatus = 500;
    state.diagnosticsEnabled = true;
    renderIntl(<EmptyWorkspacePage />);
    await screen.findByText("华东分析");

    fireEvent.click(screen.getAllByRole("button", { name: "接受" })[0]);

    const disclosure = await screen.findByTestId("error-diagnostics");
    expect(disclosure.textContent).toContain("500");
    expect(disclosure.textContent).toContain("0123456789abcdef0123456789abcdef");
    // Keep the collapsible area outside the announcement container (role=alert), avoiding duplicate diagnostics and message announcements.
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("没能接受这份邀请");
    expect(alert.contains(disclosure)).toBe(false);
    expect(alert.textContent).not.toMatch(STATUS_CODE);
  });

  it("诊断开关关闭时，只在播报容器外给一个「复制诊断」按钮，页面上没有状态码", async () => {
    state.acceptStatus = 500;
    renderIntl(<EmptyWorkspacePage />);
    await screen.findByText("华东分析");

    fireEvent.click(screen.getAllByRole("button", { name: "接受" })[0]);

    const copy = await screen.findByRole("button", { name: "复制诊断" });
    const alert = screen.getByRole("alert");
    expect(alert.contains(copy)).toBe(false);
    expect(alert).toHaveClass("legacy-ews-error");
    expect(document.body.textContent).not.toMatch(STATUS_CODE);
    expect(screen.queryByTestId("error-diagnostics")).toBeNull();
  });
});
