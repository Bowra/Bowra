import "@testing-library/jest-dom/vitest";
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { HttpStatusError } from "@/lib/api-client";

const state = vi.hoisted(() => ({
  fetchNotifications: vi.fn(),
  diagnosticsEnabled: false,
}));

vi.mock("@/lib/api", () => ({
  fetchNotificationCount: vi.fn(async () => ({ actionRequired: 0, updatesUnread: 0, totalBadge: 0 })),
  fetchNotifications: (...args: unknown[]) => state.fetchNotifications(...args),
  markNotificationRead: vi.fn(),
  archiveNotification: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: () => state.diagnosticsEnabled,
}));

import { NotificationCenter } from "./notification-center";

const STATUS_CODE = /[（(]\s*[1-5]\d\d\s*[)）]|HTTP\s*[1-5]\d\d|:\s*[1-5]\d\d\b/;

function failure(status: number) {
  return new HttpStatusError(
    "notifications",
    new Response("{}", { status, headers: { "x-request-id": "0123456789abcdef0123456789abcdef" } }),
  );
}

afterEach(() => {
  state.diagnosticsEnabled = false;
  state.fetchNotifications.mockReset();
});

describe("NotificationCenter load failure copy", () => {
  it.each([
    ["zh-CN", "通知", "没能加载通知。请稍后再试。"],
    ["en-US", "Notifications", "Couldn't load notifications. Try again later."],
    ["de-DE", "Benachrichtigungen", "Benachrichtigungen konnten nicht geladen werden. Versuchen Sie es später erneut."],
    ["fr-FR", "Notifications", "Impossible de charger les notifications. Réessayez plus tard."],
  ] as const)("says what happened without a status code (%s)", async (locale, _bell, message) => {
    state.fetchNotifications.mockRejectedValue(failure(500));
    renderIntl(<NotificationCenter />, { locale });

    fireEvent.click(screen.getByRole("button", { expanded: false }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    const panel = document.querySelector("[data-notification-center]");
    expect(panel?.textContent ?? "").not.toMatch(STATUS_CODE);
  });

  it("keeps the status and request id in the diagnostics disclosure when diagnostics are on", async () => {
    state.diagnosticsEnabled = true;
    state.fetchNotifications.mockRejectedValue(failure(502));
    renderIntl(<NotificationCenter />);

    fireEvent.click(screen.getByRole("button", { expanded: false }));

    const disclosure = await screen.findByTestId("error-diagnostics");
    expect(disclosure.textContent).toContain("502");
    expect(disclosure.textContent).toContain("0123456789abcdef0123456789abcdef");
  });

  it("offers Copy diagnostics, not the status code, when diagnostics are off", async () => {
    state.fetchNotifications.mockRejectedValue(failure(502));
    renderIntl(<NotificationCenter />);

    fireEvent.click(screen.getByRole("button", { expanded: false }));

    expect(await screen.findByRole("button", { name: "复制诊断" })).toBeInTheDocument();
    expect(screen.queryByTestId("error-diagnostics")).toBeNull();
    expect(document.querySelector("[data-notification-center]")?.textContent ?? "").not.toMatch(/502/);
  });

  it.each([404, 503])("still hides the center when the endpoint is switched off (%i)", async (status) => {
    state.fetchNotifications.mockRejectedValue(failure(status));
    renderIntl(<NotificationCenter />);

    fireEvent.click(screen.getByRole("button", { expanded: false }));

    await vi.waitFor(() => expect(screen.queryByRole("button")).toBeNull());
  });
});
