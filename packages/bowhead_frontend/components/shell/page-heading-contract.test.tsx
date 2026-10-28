import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import type { ComponentType, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AdminAccessPage from "@/app/(app)/admin/access/page";
import AdminCreditsPage from "@/app/(app)/admin/credits/page";
import CreditUserDrilldownPage from "@/app/(app)/admin/credits/users/page";
import TurnsPage from "@/app/(app)/admin/turns/page";
import KnowledgePage from "@/app/(app)/knowledge/page";
import { MobileTopBar } from "@/components/mobile-topbar";

import { PageStateHeading } from "./PageStateHeading";

const authMock = vi.hoisted(() => ({
  current: { status: "loading", error: null } as { status: string; error: string | null },
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/components/admin-shell", () => ({
  AdminShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
// Knowledge renders through PageFrame, whose AppShell slot is the persistent shell.
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => authMock.current,
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/components/sessions-drawer", () => ({
  useSessionsDrawer: () => ({ open: vi.fn() }),
}));

const PLACEHOLDER_PAGES: Array<[string, ComponentType]> = [
  ["admin access", AdminAccessPage],
  ["admin credits", AdminCreditsPage],
  ["admin credit user", CreditUserDrilldownPage],
  ["admin turn replay", TurnsPage],
  ["knowledge", KnowledgePage],
];

const ADMIN_PERMISSION_PAGES: Array<[string, ComponentType]> = [
  ["admin access", AdminAccessPage],
  ["admin credits", AdminCreditsPage],
  ["admin credit user", CreditUserDrilldownPage],
  ["admin turn replay", TurnsPage],
];

const ADMIN_ERROR_PAGES: Array<[string, ComponentType]> = [
  ...ADMIN_PERMISSION_PAGES,
];

beforeEach(() => {
  authMock.current = { status: "loading", error: null };
});

afterEach(cleanup);

describe("page heading contract", () => {
  it("keeps app chrome out of the outline and gives placeholder states one H1", () => {
    render(
      <>
        <MobileTopBar pageTitle="Knowledge" />
        <PageStateHeading>Knowledge</PageStateHeading>
        <div>Loading…</div>
      </>,
    );

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Knowledge" })).toBeInTheDocument();
    expect(screen.getByTestId("mobile-topbar-title").tagName).toBe("DIV");
  });

  it("keeps a collapsed top-bar title out of the outline and the accessibility tree", () => {
    render(
      <>
        <MobileTopBar pageTitle="Knowledge" titleCollapsed />
        <h1>Knowledge</h1>
      </>,
    );

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByTestId("mobile-topbar-title").tagName).toBe("DIV");
    expect(screen.getByTestId("mobile-topbar-title")).toHaveAttribute("aria-hidden", "true");
  });

  it.each(PLACEHOLDER_PAGES)("renders exactly one H1 in the %s loading state", (_name, Page) => {
    render(<Page />);

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it.each(ADMIN_PERMISSION_PAGES)("renders exactly one H1 in the %s forbidden state", (_name, Page) => {
    authMock.current = { status: "not_admin", error: null };
    render(<Page />);

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("uses translated copy for the Turn Replay forbidden state", () => {
    authMock.current = { status: "not_admin", error: null };
    render(<TurnsPage />);

    expect(screen.getByText("not_authorized")).toBeInTheDocument();
  });

  it.each(ADMIN_ERROR_PAGES)("renders exactly one H1 in the %s error state", (_name, Page) => {
    authMock.current = { status: "error", error: "boom" };
    render(<Page />);

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});
