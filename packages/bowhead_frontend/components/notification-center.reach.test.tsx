import "@testing-library/jest-dom/vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  reach: { on: true },
  items: [] as unknown[],
  fetchNotificationCount: vi.fn(async () => ({ actionRequired: 1, updatesUnread: 0, totalBadge: 1 })),
  markNotificationRead: vi.fn(async (_id: string) => undefined),
  setLastTeam: vi.fn(async (_id: string): Promise<{ ok: string; team_id: string; redirect_url?: string }> => ({ ok: "true", team_id: "team_b" })),
}));

vi.mock("@/lib/api", () => ({
  fetchNotificationCount: () => mocks.fetchNotificationCount(),
  fetchNotifications: vi.fn(async () => ({ items: mocks.items, nextCursor: null })),
  markNotificationRead: (id: string) => mocks.markNotificationRead(id),
  archiveNotification: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));
vi.mock("@/lib/auth-api", () => ({ setLastTeam: (id: string) => mocks.setLastTeam(id) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: mocks.push, replace: vi.fn() }) }));
vi.mock("@/lib/use-notification-reach-capability", () => ({ useNotificationReachCapability: () => mocks.reach.on }));

import { NotificationCenter } from "./notification-center";
import { publishNotificationCount } from "@/lib/attention-loop";

function item(overrides: Record<string, unknown> = {}) {
  return {
    id: "ntf_1",
    teamId: "team_b",
    category: "async_job",
    type: "routine.needs_input",
    priority: "p1",
    title: "stored",
    body: null,
    subjectType: "scheduled_job",
    subjectId: "sj_1",
    actorUserId: null,
    actionUrl: "/?session=ses_1",
    actions: [
      { id: "answer", label: "去回答", kind: "link", url: "/?session=ses_1" },
      { id: "view", label: "查看", kind: "link", url: "/team/settings/scheduled/" },
    ],
    metadata: { jobName: "Daily sales" },
    state: "open",
    actionRequired: true,
    recipientRole: "owner",
    deliveredAt: "2026-11-08T08:00:00Z",
    readAt: null,
    archivedAt: null,
    createdAt: "2026-11-08T08:00:00Z",
    updatedAt: "2026-11-08T08:00:00Z",
    scope: "team",
    teamName: "Team B",
    currentTeam: false,
    ...overrides,
  };
}

async function openPanel() {
  renderIntl(<NotificationCenter />, { locale: "en-US" });
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  return waitFor(() => {
    const row = document.querySelector<HTMLElement>(".notification-row");
    expect(row).not.toBeNull();
    return row as HTMLElement;
  });
}

beforeEach(() => {
  mocks.reach.on = true;
  mocks.items = [item()];
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("NotificationCenter with notification reach", () => {
  it("shows the item's Team and its actions in the interface language", async () => {
    const row = await openPanel();
    expect(row).toHaveTextContent("Team B");
    const buttons = Array.from(row.querySelectorAll<HTMLButtonElement>(".notification-row__action"));
    expect(buttons.map((b) => b.textContent)).toEqual(["Answer", "View"]);
    expect(buttons[0]).toHaveClass("primary");
  });

  it("switches to the item's Team before opening it", async () => {
    const row = await openPanel();
    fireEvent.click(row.querySelectorAll<HTMLButtonElement>(".notification-row__action")[1]);

    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/team/settings/scheduled/"));
    expect(mocks.setLastTeam).toHaveBeenCalledWith("team_b");
    expect(mocks.setLastTeam.mock.invocationCallOrder[0]).toBeLessThan(mocks.push.mock.invocationCallOrder[0]);
  });

  it("stays put and explains when the Team switch fails", async () => {
    mocks.setLastTeam.mockRejectedValueOnce(new Error("team_membership_required"));
    const row = await openPanel();
    fireEvent.click(row.querySelector<HTMLButtonElement>(".notification-row__main") as HTMLButtonElement);

    expect(await screen.findByText(/Couldn't switch to Team B/)).toBeInTheDocument();
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("does not navigate when the switch sends the browser to the other Team's own address", async () => {
    mocks.setLastTeam.mockResolvedValueOnce({ ok: "true", team_id: "team_b", redirect_url: "https://b.example.com/" });
    const row = await openPanel();
    fireEvent.click(row.querySelector<HTMLButtonElement>(".notification-row__main") as HTMLButtonElement);

    await waitFor(() => expect(mocks.setLastTeam).toHaveBeenCalled());
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("opens an item of the current Team without switching", async () => {
    mocks.items = [item({ currentTeam: true })];
    const row = await openPanel();
    fireEvent.click(row.querySelector<HTMLButtonElement>(".notification-row__main") as HTMLButtonElement);

    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/?session=ses_1"));
    expect(mocks.setLastTeam).not.toHaveBeenCalled();
  });

  it("hides update links that only repeat the row", async () => {
    mocks.items = [
      item({
        type: "approval.approved",
        actionRequired: false,
        actionUrl: "/?session=ses_1",
        actions: [{ id: "view", label: "View", kind: "link", url: "/?session=ses_1" }],
      }),
    ];
    const row = await openPanel();
    expect(row.querySelector(".notification-row__actions")).toBeNull();
  });

  it("reads the badge from the attention loop instead of polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderIntl(<NotificationCenter />, { locale: "en-US" });
      act(() => publishNotificationCount({ ok: true, counts: { actionRequired: 4, updatesUnread: 0, totalBadge: 4 } }));
      expect(await screen.findByText("4")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(46_000);
      });
      expect(mocks.fetchNotificationCount).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the previous panel when the switch is off", async () => {
    mocks.reach.on = false;
    const row = await openPanel();
    expect(row).not.toHaveTextContent("Team B");
    expect(row.querySelector(".notification-row__actions")).toBeNull();
    expect(screen.queryByRole("button", { name: "How notifications work" })).toBeNull();
  });
});
