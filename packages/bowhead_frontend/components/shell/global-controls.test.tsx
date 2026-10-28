/**
 * App-level right controls (space switcher, notifications, credits) mount by viewport: ≥1024 in desktop top bar,
 * 768–1023 all three in the mobile top bar, <768 space switcher/notifications in mobile top bar and credits in the
 * session drawer. All three use the same component set,
 * with matching visibility conditions (notification endpoint off, anonymous) at both widths and only one mounted instance of each at a time.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ShellGlobalControls } from "./global-controls";
import { Topbar } from "./Topbar";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values?.name ? `${key}:${String(values.name)}` : key,
  useLocale: () => "en-US",
}));

const mocks = vi.hoisted(() => ({
  setActiveId: vi.fn(),
  fetchMe: vi.fn(),
  fetchNotificationCount: vi.fn(),
  fetchNotifications: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({
    items: [
      { id: "ws_1", name: "华东区", desc: "" },
      { id: "ws_2", name: "华南区", desc: "" },
    ],
    activeId: "ws_1",
    activeName: "华东区",
    loading: false,
    error: null,
    setActiveId: mocks.setActiveId,
    refresh: vi.fn(),
  }),
}));

vi.mock("@/lib/auth-api", () => {
  class AuthError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
    }
  }
  return { AuthError, fetchMe: mocks.fetchMe };
});

vi.mock("@/lib/api", () => ({
  fetchNotificationCount: mocks.fetchNotificationCount,
  fetchNotifications: mocks.fetchNotifications,
  markNotificationRead: vi.fn(),
  archiveNotification: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push }),
}));

function stubViewport(width: number) {
  vi.stubGlobal(
    "matchMedia",
    (query: string) => {
      const max = /max-width:\s*([\d.]+)px/.exec(query);
      const min = /min-width:\s*([\d.]+)px/.exec(query);
      const matches =
        (!max || width <= Number(max[1])) && (!min || width >= Number(min[1]));
      return {
        matches,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      };
    },
  );
}

/** Three AppShell combinations: desktop Topbar, mobile top-bar right, session-drawer bottom. */
function Surfaces() {
  return (
    <>
      <section aria-label="desktop-topbar">
        <Topbar titleSlot={<span className="title">新工作</span>} />
      </section>
      <section aria-label="mobile-topbar">
        <ShellGlobalControls surface="mobile-topbar" />
      </section>
      <section aria-label="mobile-drawer">
        <ShellGlobalControls surface="mobile-drawer" />
      </section>
    </>
  );
}

/** The bell's accessible name follows the pending counts (`bell_aria*` keys). */
const BELL = /^bell_aria/;
const region = (name: string) => screen.getByRole("region", { name });

beforeEach(() => {
  mocks.setActiveId.mockReset();
  mocks.push.mockReset();
  mocks.fetchMe.mockReset().mockResolvedValue({
    email: "member@example.com",
    tier: "pro",
    credit_balance: 1200,
    credit_quota: 3000,
  });
  mocks.fetchNotificationCount
    .mockReset()
    .mockResolvedValue({ actionRequired: 1, updatesUnread: 2, totalBadge: 3 });
  mocks.fetchNotifications.mockReset().mockResolvedValue({ items: [], nextCursor: null });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ShellGlobalControls placement", () => {
  it("puts space + notifications in the mobile topbar and credits in the drawer below 768px", async () => {
    stubViewport(390);
    render(<Surfaces />);

    const mobile = region("mobile-topbar");
    const drawer = region("mobile-drawer");
    expect(await within(mobile).findByRole("button", { name: "picker_aria:华东区" })).toBeTruthy();
    expect(await within(mobile).findByRole("button", { name: BELL })).toBeTruthy();
    expect(await within(drawer).findByRole("button", { name: "credits_button_aria" })).toBeTruthy();
    expect(within(mobile).queryByTestId("user-credit-badge")).toBeNull();

    // Do not mount a second instance when CSS hides desktop top bar: no duplicate polling, screen readers find only one.
    expect(within(region("desktop-topbar")).queryByRole("button")).toBeNull();
    expect(screen.getAllByRole("button", { name: /picker_aria/ })).toHaveLength(1);
    expect(mocks.fetchMe).toHaveBeenCalledTimes(1);
    expect(mocks.fetchNotificationCount).toHaveBeenCalledTimes(1);
  });

  // The credits badge is the popover trigger button (components/user-badge.test.tsx).
  it.each([1024, 1100, 1440])("keeps all three in the desktop topbar at %ipx", async (width) => {
    stubViewport(width);
    render(<Surfaces />);

    const desktop = region("desktop-topbar");
    expect(await within(desktop).findByRole("button", { name: "picker_aria:华东区" })).toBeTruthy();
    expect(await within(desktop).findByRole("button", { name: BELL })).toBeTruthy();
    expect(await within(desktop).findByRole("button", { name: "credits_button_aria" })).toBeTruthy();
    expect(within(region("mobile-topbar")).queryByRole("button")).toBeNull();
    expect(within(region("mobile-drawer")).queryByRole("button")).toBeNull();
  });

  it.each([768, 900, 1023])("puts all three in the top bar (no drawer) at %ipx", async (width) => {
    stubViewport(width);
    render(<Surfaces />);
    const mobile = region("mobile-topbar");
    expect(await within(mobile).findByRole("button", { name: "picker_aria:华东区" })).toBeTruthy();
    expect(await within(mobile).findByRole("button", { name: "credits_button_aria" })).toBeTruthy();
    expect(within(region("mobile-drawer")).queryByTestId("user-credit-badge")).toBeNull();
    expect(mocks.fetchMe).toHaveBeenCalledTimes(1);
  });

  it("switches spaces, opens notifications and shows the balance at a narrow width", async () => {
    stubViewport(390);
    render(<Surfaces />);
    const mobile = region("mobile-topbar");

    fireEvent.click(await within(mobile).findByRole("button", { name: "picker_aria:华东区" }));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitemradio");
    expect(items[0].getAttribute("aria-checked")).toBe("true");
    fireEvent.click(items[1]);
    // Since 15d873505, switching spaces first awaits draft/canvas leave guards (confirmCanvasNavigation is a Promise),
    // calling setActiveId only in the next microtask.
    await waitFor(() => expect(mocks.setActiveId).toHaveBeenCalledWith("ws_2"));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    const bell = await within(mobile).findByRole("button", { name: BELL });
    expect(bell.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(bell);
    expect(await screen.findByRole("region", { name: "panel_aria" })).toBeTruthy();
    expect(bell.getAttribute("aria-expanded")).toBe("true");
    await waitFor(() => expect(mocks.fetchNotifications).toHaveBeenCalledWith("action", undefined));

    const credits = await within(region("mobile-drawer")).findByRole("button", { name: "credits_button_aria" });
    expect(credits.textContent).toContain("1,200");
  });

  it.each([390, 1440])(
    "hides notifications when the endpoint is off and credits when signed out at %ipx",
    async (width) => {
      stubViewport(width);
      mocks.fetchNotificationCount.mockReset().mockRejectedValue(new Error("notifications: 404"));
      const { AuthError } = await import("@/lib/auth-api");
      mocks.fetchMe.mockReset().mockRejectedValue(new AuthError(401, "unauthorized"));
      render(<Surfaces />);

      expect(await screen.findByRole("button", { name: "picker_aria:华东区" })).toBeTruthy();
      await waitFor(() => expect(mocks.fetchNotificationCount).toHaveBeenCalled());
      await waitFor(() => expect(screen.queryByRole("button", { name: /bell_/ })).toBeNull());
      expect(screen.queryByRole("button", { name: "credits_button_aria" })).toBeNull();
    },
  );
});
