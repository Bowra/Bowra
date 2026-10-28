import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { installFakeIntersectionObserver } from "@/__tests__/fake-intersection-observer";
import { renderIntl } from "@/__tests__/intl-render";
import type { CreditUserRow } from "@/lib/admin-credits-api";
import messagesZh from "@/messages/zh-CN.json";

const user: CreditUserRow = {
  user_id: "usr_test",
  email: "member@example.com",
  display_name: "Member",
  tier: "pro",
  status: "active",
  credit_balance: 100,
  credit_quota: 1_000,
  credit_period_start: null,
  spend_30d: 0,
  last_activity_at: null,
  is_team_admin: false,
  attention_score: 0,
};

vi.mock("@/lib/admin-credits-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/admin-credits-api")>();
  return {
    ...actual,
    fetchCreditUsers: vi.fn(async () => ({ items: [user], total: 1, page: 1, size: 20 })),
  };
});

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { UsersTable } from "./users-table";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("UsersTable row menu", () => {
  it("closes when the row scrolls out of view instead of sticking to the viewport edge", async () => {
    const io = installFakeIntersectionObserver();
    renderIntl(
      <UsersTable refreshTick={0} onAfterMutate={() => {}} capabilities={null} onAdjustTeamQuota={() => {}} />,
    );
    const more = await screen.findByRole("button", { name: messagesZh.admin.credits.more_aria });
    fireEvent.click(more);
    expect(screen.getByRole("menu")).toBeTruthy();
    await waitFor(() => expect(io.watching(more)).toBe(1));

    io.setVisible(more, true);
    expect(screen.getByRole("menu")).toBeTruthy();

    io.setVisible(more, false);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

// Behavior batch A: disabled items always show their reason on line two.
describe("UsersTable row menu disabled reasons", () => {
  async function openMenu() {
    fireEvent.click(await screen.findByRole("button", { name: messagesZh.admin.credits.more_aria }));
    return screen.getByRole("menu");
  }
  const reasonOf = (name: string) => {
    const item = screen.getByRole("menuitem", { name });
    const id = item.getAttribute("aria-describedby");
    return { item, reason: id ? document.getElementById(id)?.textContent : null };
  };

  it("items without permission state the reason and do nothing when chosen; permitted items have no reason", async () => {
    renderIntl(
      <UsersTable
        refreshTick={0}
        onAfterMutate={() => {}}
        capabilities={{ credit_grant: true } as never}
        onAdjustTeamQuota={() => {}}
      />,
    );
    await openMenu();
    const c = messagesZh.admin.credits;
    expect(reasonOf(c.row_action_edit).reason).toBe(c.row_reason_no_edit);
    expect(reasonOf(`${c.row_action_reset_user}…`).reason).toBe(c.row_reason_no_reset);
    expect(reasonOf(c.row_action_grant).reason).toBeNull();
    const edit = reasonOf(c.row_action_edit).item;
    fireEvent.click(edit);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("reset user balance is a danger item that confirms first; cancel does not reset", async () => {
    const c = messagesZh.admin.credits;
    renderIntl(
      <UsersTable
        refreshTick={0}
        onAfterMutate={() => {}}
        capabilities={{ credit_user_quota_write: true } as never}
        onAdjustTeamQuota={() => {}}
      />,
    );
    await openMenu();
    const reset = screen.getByRole("menuitem", { name: `${c.row_action_reset_user}…` });
    expect(reset.getAttribute("data-danger")).toBe("true");
    fireEvent.click(reset);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: messagesZh.common.cancel }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
