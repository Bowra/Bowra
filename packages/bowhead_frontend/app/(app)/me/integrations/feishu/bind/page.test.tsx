import type { ReactNode } from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { FeishuBindingSession } from "@/lib/feishu-integration-api";

const mocks = vi.hoisted(() => ({
  sessionId: "fbs_1234567890abcdef",
  fetchBinding: vi.fn(),
  completeBinding: vi.fn(),
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (key: string) => (key === "session" ? mocks.sessionId : null),
  }),
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => ({
    status: "authed",
    me: null,
    isTeamAdmin: false,
    error: null,
  }),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push }),
}));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/feishu-integration-api", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/feishu-integration-api")
  >("@/lib/feishu-integration-api");
  return {
    ...actual,
    fetchFeishuBindingSession: (...args: unknown[]) =>
      mocks.fetchBinding(...args),
    completeFeishuBindingSession: (...args: unknown[]) =>
      mocks.completeBinding(...args),
  };
});

import FeishuBindingPage from "./page";

function bindingSession(
  patch: Partial<FeishuBindingSession> = {},
): FeishuBindingSession {
  return {
    id: "fbs_1234567890abcdef",
    state: "pending",
    tenant_key: "tenant_1",
    feishu_email: "feishu@example.com",
    expires_at: new Date(Date.now() + 30 * 60 * 1_000).toISOString(),
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    completed_at: null,
    current_user_email: "member@example.com",
    current_user_id: "user_1",
    already_bound_to_current_user: false,
    conflict: false,
    can_complete: true,
    ...patch,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.sessionId = "fbs_1234567890abcdef";
});

describe("Feishu member binding page", () => {
  it("loads a pending request and completes the existing binding flow", async () => {
    const pending = bindingSession();
    mocks.fetchBinding.mockResolvedValue(pending);
    mocks.completeBinding.mockResolvedValue(
      bindingSession({
        state: "bound",
        already_bound_to_current_user: true,
        can_complete: true,
        completed_at: new Date().toISOString(),
      }),
    );

    renderIntl(<FeishuBindingPage />);

    expect(await screen.findByText("确认关联这两个账号")).toBeTruthy();
    expect(screen.getByText("fe••••@example.com")).toBeTruthy();
    expect(screen.getByText("member@example.com")).toBeTruthy();
    expect(screen.getByText(/^\d{2}:\d{2}$/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));

    await waitFor(() => {
      expect(mocks.completeBinding).toHaveBeenCalledWith(mocks.sessionId);
    });
    expect(await screen.findByText("飞书账号已绑定")).toBeTruthy();
  });

  it("shows a privacy-safe conflict without offering a fake admin action", async () => {
    mocks.fetchBinding.mockResolvedValue(
      bindingSession({
        feishu_email: null,
        conflict: true,
        can_complete: false,
      }),
    );

    renderIntl(<FeishuBindingPage />);

    expect(await screen.findByText("该飞书账号已被绑定")).toBeTruthy();
    expect(screen.getByText("飞书邮箱暂不可获取")).toBeTruthy();
    expect(screen.getByText(/不会显示已经绑定/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "确认绑定" })).toBeNull();
  });

  it("maps an authoritative 410 response to the expired state", async () => {
    mocks.fetchBinding.mockRejectedValue(
      new ApiError(410, "binding session expired"),
    );

    renderIntl(<FeishuBindingPage />);

    expect(await screen.findByText("绑定请求已过期")).toBeTruthy();
    expect(screen.getByText(/重新发送一条消息/)).toBeTruthy();
  });
});
