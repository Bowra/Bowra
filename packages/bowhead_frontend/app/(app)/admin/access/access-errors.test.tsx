/** Access control: show a localized failure reason; raw API text and status codes only enter Copy Diagnostics, outside the announcement container. */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const api = vi.hoisted(() => ({
  fetchAccessGroups: vi.fn(),
  fetchResourceGrants: vi.fn(),
  fetchGroupMembers: vi.fn(),
}));
vi.mock("@/lib/admin-access-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  ...api,
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({ useAuthRedirect: () => ({ status: "authed" }) }));
vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/lib/use-error-diagnostics-capability", () => ({ useErrorDiagnosticsCapability: () => false }));

import AdminAccessPage from "./page";

beforeEach(() => {
  api.fetchAccessGroups.mockResolvedValue({ items: [] });
  api.fetchResourceGrants.mockResolvedValue({ items: [] });
  api.fetchGroupMembers.mockResolvedValue({ items: [] });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ detail: "group already exists" }), {
      status: 409,
      headers: { "content-type": "application/json", "x-request-id": "0123456789abcdef0123456789abcdef" },
    })),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe.each([
  ["zh-CN", "同名分组已经存在", "创建"],
  ["en-US", "A group with this name already exists", "Create"],
] as const)("访问控制失败原因（%s）", (locale, reason, createLabel) => {
  it("创建同名分组：显示「已存在」，不显示接口原文和状态码", async () => {
    const { container } = renderIntl(<AdminAccessPage />, { locale });
    await screen.findAllByRole("button", { name: createLabel });
    const nameInput = container.querySelector("form input") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "财务组" } });
    fireEvent.submit(nameInput.closest("form") as HTMLFormElement);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(reason);
    expect(document.body.textContent).not.toMatch(/group already exists|\b409\b/);
    const copy = screen.getByTestId("error-copy-diagnostics");
    expect(alert.contains(copy)).toBe(false);
  });
});
