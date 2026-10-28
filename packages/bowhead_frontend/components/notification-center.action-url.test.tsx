import "@testing-library/jest-dom/vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("@/lib/api", () => ({
  fetchNotificationCount: vi.fn(async () => ({ actionRequired: 1, updatesUnread: 0, totalBadge: 1 })),
  fetchNotifications: vi.fn(async () => ({
    items: [{
      id: "ntf_1",
      teamId: "team_1",
      category: "memory",
      type: "memory.candidate",
      priority: "p2",
      title: "有新的记忆建议待确认",
      body: null,
      subjectType: "memory_candidates",
      subjectId: "u1",
      actorUserId: null,
      // Rows written before Memory moved into Settings keep the removed route.
      actionUrl: "/memory",
      actions: [],
      metadata: { createdCount: 2 },
      state: "open",
      recipientRole: "owner",
      deliveredAt: "2026-10-01T08:00:00Z",
      readAt: "2026-10-01T09:00:00Z",
      archivedAt: null,
      createdAt: "2026-10-01T08:00:00Z",
      updatedAt: "2026-10-01T08:00:00Z",
    }],
    nextCursor: null,
  })),
  markNotificationRead: vi.fn(),
  archiveNotification: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push, replace: vi.fn() }) }));

import { NotificationCenter } from "./notification-center";

describe("NotificationCenter action URLs", () => {
  it("opens a stored legacy /memory link in Settings > Memory", async () => {
    renderIntl(<NotificationCenter />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { expanded: false }));

    const row = await waitFor(() => {
      const button = document.querySelector<HTMLButtonElement>(".notification-row__main");
      expect(button).not.toBeNull();
      return button as HTMLButtonElement;
    });
    fireEvent.click(row);

    await waitFor(() => expect(push).toHaveBeenCalledWith("/settings/memory/"));
  });
});
