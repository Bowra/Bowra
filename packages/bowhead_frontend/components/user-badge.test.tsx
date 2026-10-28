/**
 * Topbar credits (P2 Sidebar & Topbar spec): the badge is a button that opens the credits popover (narrow
 * screens: the shared bottom sheet).
 * Pixel metrics and contrast are verified in real browsers (docs/design/组件统一-技术设计.md).
 */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { NARROW_SHEET_MEDIA_QUERY } from "@/components/ui/sheet";

const state = vi.hoisted(() => ({
  me: {} as Record<string, unknown>,
  push: vi.fn(),
  narrow: false,
}));

vi.mock("@/lib/auth-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth-api")>()),
  fetchMe: vi.fn(async () => state.me),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: state.push, replace: vi.fn() }) }));

import { UserBadge, creditDetailPath } from "@/components/user-badge";

const YEAR = new Date().getFullYear();
// Mid-month, mid-day UTC: the same calendar day in every test-runner time zone.
const RESETS_AT = `${YEAR}-11-15T12:00:00+00:00`;

function baseMe(): Record<string, unknown> {
  return {
    id: "u_1",
    email: "smoke@example.com",
    tier: "ultra",
    credit_balance: 6_000,
    credit_quota: 10_000,
    credit_period_resets_at: RESETS_AT,
    credit_capabilities: { credits_read: false },
  };
}

beforeEach(() => {
  state.me = baseMe();
  state.push.mockReset();
  state.narrow = false;
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === NARROW_SHEET_MEDIA_QUERY ? state.narrow : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openPopover(locale?: "en-US" | "zh-CN") {
  renderIntl(<UserBadge />, locale ? { locale } : {});
  const button = await screen.findByTestId("user-credit-badge");
  await act(async () => {
    fireEvent.click(button);
  });
  const dialog = await screen.findByRole("dialog");
  return { button, dialog };
}

describe("credits badge", () => {
  it("is a button named by its visible value, with no hover-only tooltip and no inline style", async () => {
      renderIntl(<UserBadge />);
    const button = await screen.findByRole("button", { name: `额度：${(6_000).toLocaleString()}` });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("ui-credit-badge");
    expect(button).toHaveAttribute("aria-haspopup", "dialog");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).not.toHaveAttribute("title");
    expect(button).not.toHaveAttribute("data-level");
    expect(button.getAttribute("style")).toBeNull();
    expect(button.querySelector("svg")).toHaveClass("lucide-gauge");
  });

  it("normal: plan, balance, usage bar with value text, reset date, no state message, no link without credit access", async () => {
    const { button, dialog } = await openPopover();
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button).toHaveAttribute("aria-controls", dialog.id);
    expect(dialog).toHaveAccessibleName("Ultra 套餐");
    expect(dialog).toHaveTextContent(`剩余 ${(6_000).toLocaleString()}`);
    const bar = within(dialog).getByRole("progressbar", { name: "本期已用额度" });
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "10000");
    expect(bar).toHaveAttribute("aria-valuenow", "4000");
    expect(bar).toHaveAttribute("aria-valuetext", `已用 ${(4_000).toLocaleString()} / ${(10_000).toLocaleString()}（40%）`);
    expect(bar.getAttribute("style")).toContain("--ui-credit-used: 40%");
    expect(dialog).toHaveTextContent("11月15日 重置");
    expect(dialog.querySelector(".ui-credit-pop__state")).toBeNull();
    expect(within(dialog).queryByRole("link")).toBeNull();
  });

  it("low: warning badge, warning bar and the remaining share in words", async () => {
    state.me = { ...baseMe(), credit_balance: 1_820 };
    const { button, dialog } = await openPopover();
    expect(button).toHaveAttribute("data-level", "low");
    expect(button).toHaveAccessibleName(`额度：余 18% · ${(1_820).toLocaleString()}`);
    expect(within(dialog).getByRole("progressbar")).toHaveAttribute("data-level", "low");
    expect(dialog.querySelector(".ui-credit-pop__state")).toHaveTextContent("额度偏低：本期配额还剩 18%。");
    expect(dialog.querySelector(".ui-credit-pop__state")).toHaveAttribute("data-level", "low");
  });

  it("critical: almost-out badge and message (English)", async () => {
    state.me = { ...baseMe(), credit_balance: 120 };
    const { button, dialog } = await openPopover("en-US");
    expect(button).toHaveAttribute("data-level", "critical");
    expect(button).toHaveAccessibleName("Credits: Almost out · 120");
    expect(dialog).toHaveAccessibleName("Ultra plan");
    expect(within(dialog).getByRole("progressbar")).toHaveAttribute("aria-valuetext", "9,880 of 10,000 used (99%)");
    expect(dialog).toHaveTextContent("Resets on November 15");
    expect(dialog.querySelector(".ui-credit-pop__state")).toHaveTextContent(
      "Almost out: 1% of this period's allowance is left.",
    );
  });

  it("unknown allowance: no usage bar, says no allowance is set; an empty balance says none is left", async () => {
    state.me = { ...baseMe(), credit_quota: 0, credit_balance: 500 };
    const first = await openPopover();
    expect(within(first.dialog).queryByRole("progressbar")).toBeNull();
    expect(first.dialog).toHaveTextContent("本期未设置配额");
    expect(first.dialog.querySelector(".ui-credit-pop__state")).toBeNull();
    cleanup();

    state.me = { ...baseMe(), credit_quota: 0, credit_balance: 0 };
    const second = await openPopover();
    expect(second.button).toHaveAttribute("data-level", "critical");
    expect(second.dialog.querySelector(".ui-credit-pop__state")).toHaveTextContent("额度已用完。");
  });

  it("no reset date: the reset row is omitted (null, missing or unparseable)", async () => {
    for (const value of [null, undefined, "not-a-date"]) {
      state.me = { ...baseMe(), credit_period_resets_at: value };
      const { dialog } = await openPopover();
      expect(dialog).not.toHaveTextContent("重置");
      expect(within(dialog).getByRole("progressbar")).toBeInTheDocument();
      cleanup();
    }
  });

  it("links to the user's own credit ledger when the role can read credits, closing before navigating", async () => {
    state.me = { ...baseMe(), credit_capabilities: { credits_read: true } };
    const { button, dialog } = await openPopover();
    const link = within(dialog).getByRole("link", { name: "查看用量明细" });
    expect(link.getAttribute("href")).toBe("/admin/credits/users?user_id=u_1");
    await act(async () => {
      fireEvent.click(link);
    });
    expect(state.push).toHaveBeenCalledWith("/admin/credits/users?user_id=u_1");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("keyboard: opening focuses the popover, Esc closes it and returns focus to the badge; a second click closes", async () => {
    const { button, dialog } = await openPopover();
    await waitFor(() => expect(dialog).toHaveFocus());
    await act(async () => {
      fireEvent.keyDown(dialog, { key: "Escape" });
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());

    await act(async () => {
      fireEvent.click(button);
    });
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(button);
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("narrow screens: the same content in the shared bottom sheet; Esc closes and returns focus", async () => {
    state.narrow = true;
    const { button, dialog } = await openPopover();
    expect(dialog).toHaveAttribute("data-dialog-presentation", "sheet");
    expect(dialog).toHaveClass("ui-credit-pop--sheet");
    expect(dialog).toHaveAccessibleName("Ultra 套餐");
    expect(button).toHaveAttribute("aria-controls", dialog.id);
    expect(within(dialog).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4000");
    await act(async () => {
      fireEvent.keyDown(dialog, { key: "Escape" });
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
  });
});

describe("creditDetailPath", () => {
  it("is the caller's own ledger only with credits_read", () => {
    expect(creditDetailPath({ id: "u 1", credit_capabilities: { credits_read: true } as never })).toBe(
      "/admin/credits/users?user_id=u%201",
    );
    expect(creditDetailPath({ id: "u_1", credit_capabilities: { credits_read: false } as never })).toBeNull();
    expect(creditDetailPath({ id: "u_1" })).toBeNull();
  });
});
