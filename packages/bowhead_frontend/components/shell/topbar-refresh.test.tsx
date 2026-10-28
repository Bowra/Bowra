/**
 * P2 batch 3 (Sidebar & Topbar spec): topbar controls. Pixel metrics live in styles/ui-refresh-chrome.css and are verified in real browsers. The credits
 * badge and its popover are covered in components/user-badge.test.tsx.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({
  counts: { actionRequired: 0, updatesUnread: 0, totalBadge: 0 },
  me: {
    id: "u_1",
    email: "smoke@example.com",
    display_name: "Smoke",
    tier: "ultra",
    credit_balance: 99_999_932,
    credit_quota: 100_000_000,
  } as Record<string, unknown>,
}));

vi.mock("@/lib/api", () => ({
  fetchNotificationCount: vi.fn(async () => state.counts),
  fetchNotifications: vi.fn(async () => ({ items: [], nextCursor: null })),
  markNotificationRead: vi.fn(),
  archiveNotification: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));
vi.mock("@/lib/auth-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth-api")>()),
  fetchMe: vi.fn(async () => state.me),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({
    items: [{ id: "ws_1", name: "零售经营分析", desc: "" }],
    activeId: "ws_1",
    activeName: "零售经营分析",
    loading: false,
    error: null,
    errorCause: null,
    setActiveId: vi.fn(),
  }),
}));

import { NotificationCenter } from "@/components/notification-center";
import { SpacePicker } from "@/components/space-picker";

beforeEach(() => {
  state.counts = { actionRequired: 0, updatesUnread: 0, totalBadge: 0 };
  state.me = { ...state.me, credit_balance: 99_999_932, credit_quota: 100_000_000 };
});

afterEach(cleanup);

describe("notification bell", () => {
  it("shows a number only for things that need you", async () => {
    state.counts = { actionRequired: 3, updatesUnread: 2, totalBadge: 5 };
    renderIntl(<NotificationCenter />);
    // the accessible name says the same split as the badge
    const bell = await screen.findByRole("button", { name: "通知，3 个待处理，2 条未读更新" });
    await waitFor(() => expect(bell.querySelector(".notification-bell__badge")?.textContent).toBe("3"));
    expect(bell.querySelector(".notification-bell__dot")).toBeNull();
  });

  it("ordinary updates alone show a dot, and the accessible name calls them updates", async () => {
    state.counts = { actionRequired: 0, updatesUnread: 2, totalBadge: 2 };
    renderIntl(<NotificationCenter />);
    const bell = await screen.findByRole("button", { name: "通知，2 条未读更新" });
    expect(bell.querySelector(".notification-bell__badge")).toBeNull();
    expect(bell.querySelector(".notification-bell__dot")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("space chip", () => {
  it("space icon + name + chevron at 14px, no # and no inline width cap", () => {
    renderIntl(<SpacePicker compact />);
    const button = screen.getByRole("button");
    // no inline `all: unset` (it would wipe the focus ring); the reset lives in the chrome stylesheet
    expect(button.getAttribute("style")).toBeNull();
    expect(button).toHaveClass("v2-ws-picker-button");
    const svgs = button.querySelectorAll("svg");
    expect(Array.from(svgs, (s) => s.getAttribute("class"))).toEqual([
      expect.stringContaining("lucide-building-2"),
      expect.stringContaining("lucide-chevron-down"),
    ]);
    expect(Array.from(svgs, (s) => s.getAttribute("width"))).toEqual(["14", "14"]);
    expect(button.textContent).toBe("零售经营分析");
    expect(button.querySelector(".v2-ws-picker-name")?.getAttribute("style")).toBeNull();
  });
});
