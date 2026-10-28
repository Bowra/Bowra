/**
 * Team-settings shell permission states: announce identity-fetch failures and denied access with role="alert" (still required after moving inline styles to classes in the acceptance cleanup);
 * identity checking uses role="status".
 */
import "@testing-library/jest-dom/vitest";
import type { ReactNode } from "react";
import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { fetchMe, type MeUser } from "@/lib/auth-api";

vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
vi.mock("@/lib/invite-api", () => ({ getAuthCapabilities: vi.fn(async () => null) }));
vi.mock("@/lib/api", () => ({ fetchScheduledJobs: vi.fn(async () => ({ items: [] })) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <div>{children}</div> }));

import { SettingsShell } from "./settings-shell";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("SettingsShell 团队设置的权限状态", () => {
  it("读取身份失败：role=alert，并给重试", async () => {
    vi.mocked(fetchMe).mockRejectedValue(new Error("network down"));
    renderIntl(<SettingsShell active="people"><div>protected sentinel</div></SettingsShell>);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("暂时无法确认你的权限");
    expect(alert.closest(".legacy-settings-gate")).not.toBeNull();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
    expect(screen.queryByText("protected sentinel")).toBeNull();
  });

  it("普通成员进团队设置：role=alert 说明谁能访问", async () => {
    vi.mocked(fetchMe).mockResolvedValue({ id: "u1", team_role: "member", is_team_admin: false } as MeUser);
    renderIntl(<SettingsShell active="people"><div>protected sentinel</div></SettingsShell>);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("只有团队负责人或团队管理员可以访问团队设置");
    expect(alert).toHaveClass("legacy-settings-gate");
    expect(screen.queryByText("protected sentinel")).toBeNull();
  });
});
